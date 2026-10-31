#!/usr/bin/env python3
"""额度中断恢复扫描的 fixture 测试（临时目录 + 临时 sqlite，不触真实库）。

运行：python3 batch3-tests.py
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.dont_write_bytecode = True

TOOLS = Path(__file__).resolve().parent.parent / "assets" / "tools"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_db(path: Path, *, native: str, ended_us: int | None,
             last_obs_us: int | None, autosave_us: int | None,
             handoff_us: int | None = None,
             session_in_db: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE workspaces(id BLOB PRIMARY KEY, name TEXT);
        CREATE TABLE projects(id BLOB PRIMARY KEY, workspace_id BLOB, name TEXT);
        CREATE TABLE sessions(id BLOB PRIMARY KEY, workspace_id BLOB,
            project_id BLOB, agent_kind TEXT, cwd TEXT,
            started_at INTEGER, ended_at INTEGER);
        CREATE TABLE observations(id BLOB PRIMARY KEY, session_id BLOB,
            workspace_id BLOB, project_id BLOB, kind TEXT, created_at INTEGER);
        CREATE TABLE pages(id BLOB PRIMARY KEY, workspace_id BLOB,
            project_id BLOB, path TEXT, body TEXT, is_latest INTEGER,
            created_at INTEGER);
        CREATE TABLE handoffs(id BLOB PRIMARY KEY, workspace_id BLOB,
            project_id BLOB, from_session_id BLOB, state TEXT,
            summary TEXT, created_at INTEGER);
    """)
    ws, pj = uuid.uuid4().bytes, uuid.uuid4().bytes
    conn.execute("INSERT INTO workspaces VALUES (?,?)", (ws, "ws"))
    conn.execute("INSERT INTO projects VALUES (?,?,?)", (pj, ws, "pj"))
    sid = uuid.UUID(ri.canonical_id(native)).bytes
    if session_in_db:
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                     (sid, ws, pj, "codex", "/tmp/p", 1, ended_us))
    if last_obs_us:
        conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)",
                     (uuid.uuid4().bytes, sid, ws, pj, "post-tool-use", last_obs_us))
    if autosave_us:
        bare = native.removeprefix("session-")
        conn.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?)",
                     (uuid.uuid4().bytes, ws, pj,
                      f"_autosave/codex-{bare}.md", "# x", 1, autosave_us))
    if handoff_us:
        conn.execute("INSERT INTO handoffs VALUES (?,?,?,?,?,?,?)",
                     (uuid.uuid4().bytes, ws, pj, sid, "open", "s", handoff_us))
    conn.commit()
    conn.close()


ri = load("ri", "recover-interrupted.py")


def verdict_for(**kw) -> tuple[str, str]:
    now_us = int(time.time() * 1_000_000)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.sqlite"
        native = "session-" + str(uuid.uuid4())
        build_db(db, native=native, **kw)
        ri.DB_PATH = db
        rows = [{"agent": "codex", "native_id": native,
                 "canonical_id": ri.canonical_id(native),
                 "cwd": "/tmp/p", "log_path": "/tmp/x",
                 "last_log_activity_us": kw.get("_log_us", kw.get("last_obs_us") or 0)}]
        rows = ri.enrich_with_db(rows)
        return ri.verdict(rows[0])


def main() -> int:
    now = int(time.time() * 1_000_000)
    H = 3600 * 1_000_000

    # 1) 中断：已停止活动，存档明显落后 → stale_autosave
    v, d = verdict_for(ended_us=now - 20 * H, last_obs_us=now - 2 * H,
                       autosave_us=now - 3 * H)
    check("中断且存档落后 → stale_autosave", v == "stale_autosave", f"{v}: {d}")

    # 2) 中断但无 open 交接、存档新鲜 → ok
    v, d = verdict_for(ended_us=now - 20 * H, last_obs_us=now - 2 * H,
                       autosave_us=now - 2 * H + 10_000_000)
    check("存档新鲜 → ok", v == "ok", f"{v}: {d}")

    # 3) 旧交接比最新活动旧 → handoff_outdated（关键：不能假绿）
    v, d = verdict_for(ended_us=now - 20 * H, last_obs_us=now - 2 * H,
                       autosave_us=now - 3 * H, handoff_us=now - 18 * H)
    check("旧交接不掩盖新工作 → handoff_outdated", v == "handoff_outdated", f"{v}: {d}")

    # 4) 交接不旧于活动 → has_handoff
    v, d = verdict_for(ended_us=now - 20 * H, last_obs_us=now - 2 * H,
                       autosave_us=now - 2 * H, handoff_us=now - 1 * H)
    check("有效交接 → has_handoff", v == "has_handoff", f"{v}: {d}")

    # 5) 最近仍在活动 → active（存档落后是正常的）
    v, d = verdict_for(ended_us=now - 20 * H, last_obs_us=now - 60_000_000,
                       autosave_us=now - 10 * H)
    check("仍在活动 → active", v == "active", f"{v}: {d}")

    # 6) 日志有活动但 DB 没登记 → no_db_session
    v, d = verdict_for(ended_us=None, last_obs_us=None, autosave_us=None,
                       session_in_db=False)
    check("DB 无登记 → no_db_session", v == "no_db_session", f"{v}: {d}")

    # 7) canonical id 规则
    pure = "01a1876c-0031-7711-bc90-173b7081effe"
    check("纯 UUID canonical 不变", ri.canonical_id(pure) == pure, ri.canonical_id(pure))
    dsh = "session-535bcd88-5c15-46ce-ab87-cf529e582e20"
    check("session- 前缀走 uuid5",
          ri.canonical_id(dsh) == "0c55e8a9-a8de-5a7c-b04e-e41a2effc451",
          ri.canonical_id(dsh))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        if not ok:
            print(f"FAIL {name}  [{detail}]")
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
