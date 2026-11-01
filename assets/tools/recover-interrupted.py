#!/usr/bin/env python3
"""额度中断 / 崩溃后的恢复扫描（只读 + 可选写一份恢复快照）。

背景
----
额度耗尽或崩溃时，agent 进程直接死掉，**不会触发 SessionEnd**，所以：

* 没有自动 handoff；
* 那一轮没有 Stop，阶段存档停在上一轮；
* 数据库里的 `ended_at` 可能早就被写成「已结束」，但它实际还在产出内容
  （实测某会话 ended_at 之后仍有 284 条观测）。

因此「有没有新工作」不能看 `ended_at`，也不能只看 handoff。
本工具直接读 rollout / 会话日志的**磁盘真实活动时间**来判定。

用法
----
    # 扫描：列出所有「日志有近期活动但可能没交接」的会话
    python3 recover-interrupted.py --since-hours 24

    # 只看某一个项目
    python3 recover-interrupted.py --cwd /path/to/project --since-hours 72

    # 为某个会话生成/刷新恢复快照页（覆盖同一 path，不造新页）
    python3 recover-interrupted.py --session <native id> --emit

不写库、不领 handoff、不调用模型。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
DATA_DIR = Path.home() / "Library/Application Support/ai-memory"
DB_PATH = DATA_DIR / "db" / "memory.sqlite"
DSH_ROOT = Path.home() / ".dsh" / "sessions"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def canonical_id(native: str) -> str:
    """对齐 ai-memory 的 SessionId::from_native。"""
    try:
        return str(uuid.UUID(native))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(uuid.NAMESPACE_OID, native))


def open_db_ro():
    if not DB_PATH.is_file():
        return None
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)


def scan_codex(since_us: int) -> list[dict]:
    """扫描 Codex rollout 的磁盘活动时间。"""
    ck = _load("ck_codex", "codex-checkpoint.py")
    out: list[dict] = []
    for path in ck.iter_rollouts():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        mtime_us = int(mtime * 1_000_000)
        if mtime_us < since_us:
            continue
        meta = ck.first_meta(path) or {}
        # payload.id 是**该 thread 自己的**原生 id；session_id 是主会话关联 id
        # （子智能体也沿用）。必须 id 优先，否则子会话会被并进主 native。
        native = meta.get("id") or meta.get("session_id") or path.stem
        out.append({
            "agent": "codex",
            "native_id": native,
            "canonical_id": canonical_id(native),
            "cwd": meta.get("cwd"),
            "log_path": str(path),
            "last_log_activity_us": mtime_us,
        })
    return out


def scan_dsh(since_us: int) -> list[dict]:
    """扫描 DSH 会话日志的磁盘活动时间（不依赖目录名编码）。"""
    out: list[dict] = []
    if not DSH_ROOT.is_dir():
        return out
    for log in DSH_ROOT.glob("*/*/session.v4.jsonl.zstd"):
        try:
            mtime_us = int(log.stat().st_mtime * 1_000_000)
        except OSError:
            continue
        if mtime_us < since_us:
            continue
        session_dir = log.parent
        bare = session_dir.name.removeprefix("session-")
        native = f"session-{bare}"
        # 只读第一行拿 cwd，不解压全文
        cwd = None
        try:
            import subprocess
            import shlex
            proc = subprocess.run(
                f"zstd -dc {shlex.quote(str(log))} 2>/dev/null | head -n 1",
                shell=True, capture_output=True, text=True)
            head = (proc.stdout or "").strip()
            if head:
                cwd = (json.loads(head) or {}).get("cwd")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            cwd = None
        out.append({
            "agent": "dsh",
            "native_id": native,
            "canonical_id": canonical_id(native),
            "cwd": cwd,
            "log_path": str(log),
            "last_log_activity_us": mtime_us,
        })
    return out


def enrich_with_db(rows: list[dict]) -> list[dict]:
    """补上 DB 侧事实：会话是否登记、ended_at、最后观测、最新存档、是否有 open 交接。"""
    conn = open_db_ro()
    if conn is None:
        for r in rows:
            r["db"] = {"error": "数据库不可读"}
        return rows
    try:
        for r in rows:
            sid = uuid.UUID(r["canonical_id"]).bytes
            info: dict = {}
            srow = conn.execute(
                "SELECT ended_at, cwd FROM sessions WHERE id = ?", (sid,)).fetchone()
            if srow is None:
                info["session_in_db"] = False
            else:
                info["session_in_db"] = True
                info["ended_at_us"] = srow[0]
                info["db_cwd"] = srow[1]
            info["last_obs_us"] = conn.execute(
                "SELECT MAX(created_at) FROM observations WHERE session_id = ?",
                (sid,)).fetchone()[0]
            hrow = conn.execute(
                """SELECT COUNT(*), MAX(created_at) FROM handoffs
                   WHERE from_session_id = ? AND state = 'open'""", (sid,)).fetchone()
            info["open_handoffs"] = hrow[0] or 0
            info["latest_open_handoff_us"] = hrow[1]
            bare = r["native_id"].removeprefix("session-")
            info["autosave_us"] = None
            for cand in (f"_autosave/{r['agent']}-{bare}.md",):
                row = conn.execute(
                    "SELECT created_at FROM pages WHERE path = ? AND is_latest = 1",
                    (cand,)).fetchone()
                if row:
                    info["autosave_us"] = row[0]
            r["db"] = info
    finally:
        conn.close()
    return rows


def verdict(r: dict) -> tuple[str, str]:
    """给一行一个结论：需要接手 / 已有交接 / 无需处理。"""
    db = r.get("db") or {}
    if db.get("error"):
        return "unknown", db["error"]
    log_us = r["last_log_activity_us"]
    obs_us = db.get("last_obs_us") or 0
    auto_us = db.get("autosave_us") or 0
    effective = max(log_us, obs_us)

    ACTIVE_WINDOW_US = 5 * 60 * 1_000_000
    is_active = log_us > int(time.time() * 1_000_000) - ACTIVE_WINDOW_US

    # 交接只有在「不旧于真实活动」时才算有效凭证。
    # 实测踩过：某会话 10-30 20:52 有一条 open 交接，但今天 13:58 仍在产出，
    # 照旧交接接手会漏掉整段新工作。
    if db.get("open_handoffs"):
        h_us = db.get("latest_open_handoff_us") or 0
        if not is_active and effective > h_us + 90 * 1_000_000:
            return "handoff_outdated", (
                f"有 open 交接，但它比最后活动旧 "
                f"{(effective - h_us) // 1_000_000} 秒 —— 旧交接不代表最新进度，"
                "必须读 raw 观测")
        return "has_handoff", "已有 open 交接，且不旧于最新活动，正常领取即可"

    if not db.get("session_in_db"):
        return "no_db_session", "日志有活动但数据库没登记（钩子未捕获）"
    lag = effective - auto_us if auto_us else None

    if is_active:
        if auto_us and lag and lag > 90 * 1_000_000:
            return "active", "最近 5 分钟仍在活动，存档落后属本轮未结束（正常）"
        return "active", "最近 5 分钟仍在活动"

    if auto_us and lag is not None and lag > 90 * 1_000_000:
        return "stale_autosave", (
            f"已停止活动，存档落后 {lag // 1_000_000} 秒 —— "
            "很可能额度中断/崩溃，该轮无 Stop，必须读 raw")
    return "ok", "存档与活动时间接近"


def main() -> int:
    ap = argparse.ArgumentParser(description="额度中断/崩溃后的恢复扫描")
    ap.add_argument("--since-hours", type=float, default=24.0,
                    help="只看最近 N 小时有日志活动的会话（默认 24）")
    ap.add_argument("--cwd", help="只保留该目录下的会话")
    ap.add_argument("--agent", choices=("codex", "dsh"))
    ap.add_argument("--session", help="只看这个 native id")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    since_us = int((time.time() - args.since_hours * 3600) * 1_000_000)
    rows: list[dict] = []
    if args.agent in (None, "codex"):
        rows += scan_codex(since_us)
    if args.agent in (None, "dsh"):
        rows += scan_dsh(since_us)

    if args.session:
        want = canonical_id(args.session)
        rows = [r for r in rows if r["canonical_id"] == want]
    if args.cwd:
        target = str(Path(args.cwd).resolve()).rstrip("/")
        rows = [r for r in rows
                if (r.get("cwd") or "").rstrip("/") == target
                or ((r.get("db") or {}).get("db_cwd") or "").rstrip("/") == target]

    # 同一 native_id 可能有多个 rollout/日志（续接、重启），只保留活动最晚的那条
    dedup: dict[tuple, dict] = {}
    for r in rows:
        key = (r["agent"], r["native_id"])
        prev = dedup.get(key)
        if prev is None or r["last_log_activity_us"] > prev["last_log_activity_us"]:
            dedup[key] = r
    rows = list(dedup.values())

    rows = enrich_with_db(rows)
    rows.sort(key=lambda r: r["last_log_activity_us"], reverse=True)

    for r in rows:
        r["verdict"], r["verdict_detail"] = verdict(r)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        if not rows:
            print(f"最近 {args.since_hours} 小时没有日志活动。")
            return 0
        print(f"最近 {args.since_hours} 小时有日志活动的会话（{len(rows)} 条）：\n")
        for r in rows:
            ts = time.strftime("%m-%d %H:%M:%S",
                               time.localtime(r["last_log_activity_us"] / 1_000_000))
            print(f"[{r['verdict']}] {r['agent']} {r['native_id']}")
            print(f"    最后日志活动: {ts}   cwd: {r.get('cwd') or '?'}")
            print(f"    → {r['verdict_detail']}")
            if r["verdict"] in ("stale_autosave", "no_db_session"):
                print(f"    日志: {r['log_path']}")
            print()

    need = [r for r in rows
            if r["verdict"] in ("stale_autosave", "no_db_session", "handoff_outdated")]
    if need and not args.json:
        print(f"⚠ {len(need)} 个会话可能需要按 raw 恢复（不要只看 ended_at 或 handoff）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
