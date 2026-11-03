#!/usr/bin/env python3
"""批次4 失败测试：resume 恢复上下文构建（纯只读，临时 fixture）。

先于实现编写，初始应大量失败。
运行：python3 tests/batch4-resume-tests.py
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

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "assets" / "tools"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def load_resume():
    spec = importlib.util.spec_from_file_location("resume", TOOLS / "resume.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- fixture 工具
def write_rollout(path: Path, meta: dict, events: list[dict]) -> None:
    """写一个 Codex 形状的 rollout（明文 JSONL，read_jsonl 能读）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session_meta", "payload": meta}, ensure_ascii=False) + "\n")
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def codex_user(text: str) -> dict:
    return {"type": "response_item",
            "payload": {"role": "user", "content": [{"type": "input_text", "text": text}]}}


def codex_assistant(text: str) -> dict:
    return {"type": "event_msg",
            "payload": {"type": "item_completed",
                        "item": {"type": "AgentMessage",
                                 "content": [{"type": "Text", "text": text}]}}}


def codex_task_complete(**kw) -> dict:
    p = {"type": "task_complete"}
    p.update(kw)
    return {"type": "event_msg", "payload": p}


def codex_cmd(cmd: str, status: str = "Completed", exit_code: int | None = 0) -> dict:
    item = {"type": "CommandExecution",
            "command": ["/bin/zsh", "-lc", cmd], "status": status}
    if exit_code is not None:
        item["exit_code"] = exit_code
    return {"type": "event_msg", "payload": {"type": "item_completed", "item": item}}


def codex_file(path: str, status: str = "Completed") -> dict:
    return {"type": "event_msg", "payload": {"type": "item_completed",
            "item": {"type": "FileChange", "changes": {path: {"type": "update"}},
                     "status": status}}}


def quota_complete() -> dict:
    return codex_task_complete(
        last_agent_message=None,
        error={"message": "You've hit your usage limit.",
               "codex_error_info": "usage_limit_exceeded"})


# ---------------------------------------------------------------- 各场景
def test_quota_with_task_complete(mod) -> None:
    """quota 中断时 task_complete 仍然存在，必须识别为 quota 而非完成。"""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "rollout-a.jsonl"
        write_rollout(f, {"id": "aaaa-1", "session_id": "aaaa-1", "cwd": "/tmp/proj"},
                      [codex_user("做一件事"), codex_cmd("echo hi"), quota_complete()])
        st = mod.classify_codex(f)
        check("quota: 识别为 quota 中断", st["outcome"] == "quota", str(st.get("outcome")))
        check("quota: 不误判为完成", st["outcome"] != "completed")
        check("quota: 带结构化码", "usage_limit_exceeded" in str(st.get("evidence")))


def test_clean_turn_not_old_quota(mod) -> None:
    """最新 turn 干净完成时，不能因为历史上出现过 quota 就报中断。"""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "rollout-b.jsonl"
        write_rollout(f, {"id": "bbbb-1", "session_id": "bbbb-1", "cwd": "/tmp/proj"},
                      [codex_user("第一件事"), quota_complete(),
                       codex_user("继续做第二件"), codex_cmd("echo ok"),
                       codex_task_complete(last_agent_message="完成了")])
        st = mod.classify_codex(f)
        check("干净完成: 判为 completed", st["outcome"] == "completed", str(st.get("outcome")))
        check("干净完成: 不因旧 quota 报中断", st["outcome"] != "quota")


def test_unpaired_tool(mod) -> None:
    """有 tool/call 无 tool/result 时，必须标为未知/进行中，不能当成功。"""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "rollout-c.jsonl"
        write_rollout(f, {"id": "cccc-1", "session_id": "cccc-1", "cwd": "/tmp/proj"},
                      [codex_user("改文件"),
                       {"type": "response_item",
                        "payload": {"type": "custom_tool_call", "call_id": "call_x",
                                    "name": "exec", "input": "edit file"}}])
        ctx = mod.build_resume(f, "cccc-1", "/tmp/proj", "default", "proj")
        check("未配对工具: 标为未知/进行中",
              "未知" in ctx["body"] or "进行中" in ctx["body"] or "未配对" in ctx["body"],
              ctx["body"][:200])


def test_truncated_tail(mod) -> None:
    """日志尾行被截断（非法 JSON）时不能崩，且要标记。"""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "rollout-d.jsonl"
        write_rollout(f, {"id": "dddd-1", "session_id": "dddd-1", "cwd": "/tmp/proj"},
                      [codex_user("任务")])
        with f.open("a", encoding="utf-8") as fh:
            fh.write('{"type":"event_msg","payload":{"type":"task_comp')  # 半行
        try:
            st = mod.classify_codex(f)
            check("截断尾行: 不崩溃", True, str(st.get("outcome")))
            check("截断尾行: 标记损坏", st.get("truncated") is True or "截断" in str(st.get("evidence")))
        except Exception as exc:  # noqa: BLE001
            check("截断尾行: 不崩溃", False, f"{type(exc).__name__}: {exc}")


def test_cross_scope_page(mod) -> None:
    """DB 里同 native 但不同 scope 的数据不能被采用。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.sqlite"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE workspaces(id BLOB PRIMARY KEY, name TEXT);
            CREATE TABLE projects(id BLOB PRIMARY KEY, workspace_id BLOB, name TEXT);
            CREATE TABLE sessions(id BLOB PRIMARY KEY, workspace_id BLOB,
                project_id BLOB, agent_kind TEXT, cwd TEXT, started_at INTEGER, ended_at INTEGER);
            CREATE TABLE pages(id BLOB PRIMARY KEY, workspace_id BLOB, project_id BLOB,
                path TEXT, body TEXT, is_latest INTEGER, created_at INTEGER);
            CREATE TABLE observations(id BLOB PRIMARY KEY, session_id BLOB,
                workspace_id BLOB, project_id BLOB, kind TEXT, created_at INTEGER);
        """)
        ws, pj_other = uuid.uuid4().bytes, uuid.uuid4().bytes
        conn.execute("INSERT INTO workspaces VALUES (?,?)", (ws, "default"))
        conn.execute("INSERT INTO projects VALUES (?,?,?)", (pj_other, ws, "other-proj"))
        native = "eeee-1"
        canon = str(uuid.uuid5(uuid.NAMESPACE_OID, native))
        sid = uuid.UUID(canon).bytes
        conn.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?)",
                     (uuid.uuid4().bytes, ws, pj_other,
                      f"_autosave/codex-{native}.md", "别的项目的内容", 1, 1000))
        conn.commit(); conn.close()

        f = Path(tmp) / "rollout-e.jsonl"
        write_rollout(f, {"id": native, "session_id": native, "cwd": "/tmp/proj"},
                      [codex_user("我的任务")])
        ctx = mod.build_resume(f, native, "/tmp/proj", "default", "my-proj",
                               db_path=db)
        check("跨 scope: 不采用别的项目 page",
              "别的项目的内容" not in ctx["body"], ctx["body"][:200])


def test_missing_db(mod) -> None:
    """DB 不可用仍能构建上下文，并明确标注缺 DB 校验。"""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "rollout-f.jsonl"
        write_rollout(f, {"id": "ffff-1", "session_id": "ffff-1", "cwd": "/tmp/proj"},
                      [codex_user("任务内容"), codex_cmd("echo 1")])
        ctx = mod.build_resume(f, "ffff-1", "/tmp/proj", "default", "proj",
                               db_path=Path(tmp) / "nope.sqlite")
        check("缺 DB: 仍能构建", bool(ctx.get("body")), "")
        check("缺 DB: 标注未校验", "缺" in ctx["body"] or "未校验" in ctx["body"],
              ctx["body"][:200])


# ---------------------------------------------------------------- DB 侧一致性
def _mk_db(path: Path, native: str, *, obs_us=None, auto_us=None,
           in_db=True, agent_kind="codex", ended_us=None) -> Path:
    """造一个只含 DB 一致性所需列的最小 ai-memory 库。"""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE workspaces(id BLOB PRIMARY KEY, name TEXT);
        CREATE TABLE projects(id BLOB PRIMARY KEY, name TEXT, workspace_id BLOB);
        CREATE TABLE sessions(id BLOB PRIMARY KEY, agent_kind TEXT, cwd TEXT,
                              ended_at INTEGER, project_id BLOB, workspace_id BLOB);
        CREATE TABLE observations(session_id BLOB, created_at INTEGER,
                                  workspace_id BLOB, project_id BLOB);
        CREATE TABLE pages(path TEXT, created_at INTEGER, is_latest INTEGER,
                           project_id BLOB, workspace_id BLOB);
    """)
    w, pj = uuid.uuid4().bytes, uuid.uuid4().bytes
    conn.execute("INSERT INTO workspaces VALUES(?,?)", (w, "default"))
    conn.execute("INSERT INTO projects VALUES(?,?,?)", (pj, "proj", w))
    sid = uuid.uuid5(uuid.NAMESPACE_OID, native).bytes if len(native) < 32 else uuid.UUID(native).bytes
    if in_db:
        conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                     (sid, agent_kind, "/tmp/proj", ended_us, pj, w))
    if obs_us:
        conn.execute("INSERT INTO observations VALUES(?,?,?,?)", (sid, obs_us, w, pj))
    if auto_us:
        conn.execute("INSERT INTO pages VALUES(?,?,?,?,?)",
                     (f"_autosave/{agent_kind}-{native}.md", auto_us, 1, pj, w))
    conn.commit()
    conn.close()
    return path


def _rollout_with_time(tmp: Path, native: str, log_us: int) -> Path:
    """写一个带**统一时间戳**的 rollout，便于精确控制「日志末尾」。"""
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp(log_us / 1e6, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    f = tmp / f"rollout-{native}.jsonl"

    def with_ts(e):
        return {**e, "timestamp": ts}

    write_rollout(f, {"id": native, "session_id": native, "cwd": "/tmp/proj"},
                  [with_ts(codex_user("任务")), with_ts(codex_cmd("echo 1"))])
    return f


def test_db_autosave_stale(mod) -> None:
    """存档明显落后于日志 → 必须点明「按日志重建的才是最新的」。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        now = int(time.time() * 1_000_000)
        log_us = now - 2 * 3600 * 1_000_000      # 日志末尾 2 小时前
        old = now - 20 * 3600 * 1_000_000        # 存档 20 小时前
        native = "01a30000-0000-7000-8000-000000000001"
        f = _rollout_with_time(tmp, native, log_us)
        db = _mk_db(tmp / "m.sqlite", native, obs_us=old, auto_us=old)
        ctx = mod.build_resume(f, native, "/tmp/proj", "default", "proj", db_path=db)
        body = ctx["body"]
        check("DB: 存档落后被点出", "存档落后" in body, body[:400])
        check("DB: 说明按日志重建才最新", "按日志重建" in body, body[:400])
        check("DB: 观测落后被点出", "观测落后" in body, body[:400])


def test_db_autosave_fresh_is_quiet(mod) -> None:
    """存档跟得上日志 → 不要再抱怨（避免狼来了）。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        now = int(time.time() * 1_000_000)
        log_us = now - 2 * 3600 * 1_000_000
        near = log_us - 60 * 1_000_000
        native = "01a30000-0000-7000-8000-000000000002"
        f = _rollout_with_time(tmp, native, log_us)
        db = _mk_db(tmp / "m.sqlite", native, obs_us=near, auto_us=near)
        ctx = mod.build_resume(f, native, "/tmp/proj", "default", "proj", db_path=db)
        body = ctx["body"]
        check("DB: 存档正常时不误报", "存档落后" not in body, body[:400])
        check("DB: 观测正常时不误报", "观测落后" not in body, body[:400])


def test_db_active_session_not_flagged(mod) -> None:
    """最近仍在活动时，存档落后属本轮未结束，不能说成崩溃。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        now = int(time.time() * 1_000_000)
        log_us = now - 60 * 1_000_000             # 1 分钟前仍在动
        old = now - 5 * 3600 * 1_000_000
        native = "01a30000-0000-7000-8000-000000000003"
        f = _rollout_with_time(tmp, native, log_us)
        db = _mk_db(tmp / "m.sqlite", native, obs_us=log_us, auto_us=old)
        ctx = mod.build_resume(f, native, "/tmp/proj", "default", "proj", db_path=db)
        body = ctx["body"]
        # 只看 _db_lines 产出的那几行（以「存档」开头），
        # 排除既有的「结束判定：…未完成或崩溃」文案
        db_part = "\n".join(
            ln for ln in body.splitlines() if ln.startswith("- 存档") or ln.startswith("- ⚠ **存档"))
        check("DB: 活跃会话不说成崩溃",
              "额度中断" not in db_part and "崩溃" not in db_part, db_part)
        check("DB: 点明本轮尚未收尾", "仍在活动" in db_part, db_part)


def test_db_no_autosave_page(mod) -> None:
    """没有存档页时如实说明，而不是静默当成正常。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        now = int(time.time() * 1_000_000)
        log_us = now - 3600 * 1_000_000
        native = "01a30000-0000-7000-8000-000000000004"
        f = _rollout_with_time(tmp, native, log_us)
        db = _mk_db(tmp / "m.sqlite", native, obs_us=log_us, auto_us=None)
        ctx = mod.build_resume(f, native, "/tmp/proj", "default", "proj", db_path=db)
        check("DB: 无存档页被说明", "没有自动存档页" in ctx["body"], ctx["body"][:400])


def test_db_ended_at_not_used_for_freshness(mod) -> None:
    """`ended_at` 与日志末尾无关，**不能**被当成进度新旧依据。

    实测：同一会话 ended_at 与日志末尾可差 ±16 小时、方向不定，
    所以即便 ended_at 很新也不该改变「存档落后」的判断。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        now = int(time.time() * 1_000_000)
        log_us = now - 2 * 3600 * 1_000_000
        old = now - 20 * 3600 * 1_000_000
        native = "01a30000-0000-7000-8000-000000000005"
        f = _rollout_with_time(tmp, native, log_us)
        # ended_at 很新，但存档仍旧 → 仍应报落后
        db = _mk_db(tmp / "m.sqlite", native, obs_us=old, auto_us=old, ended_us=now)
        ctx = mod.build_resume(f, native, "/tmp/proj", "default", "proj", db_path=db)
        check("DB: ended_at 新也不掩盖存档落后",
              "存档落后" in ctx["body"], ctx["body"][:400])


def test_user_text_and_injection(mod) -> None:
    """真实 HTML 用户输入要保留，system/skill/hook 注入要排除。"""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "rollout-g.jsonl"
        write_rollout(f, {"id": "gggg-1", "session_id": "gggg-1", "cwd": "/tmp/proj"},
                      [codex_user("<div>请修改这个HTML组件</div>"),
                       codex_user("# AGENTS.md instructions\n<INSTRUCTIONS>内部规则"),
                       codex_user("The following is the Codex agent history"),
                       codex_user("真正的新指令")])
        ctx = mod.build_resume(f, "gggg-1", "/tmp/proj", "default", "proj")
        b = ctx["body"]
        check("文本: 保留 HTML 用户任务", "<div>请修改这个HTML组件</div>" in b)
        check("文本: 保留真实指令", "真正的新指令" in b)
        check("文本: 排除 AGENTS.md 注入", "AGENTS.md instructions" not in b)
        check("文本: 排除 Codex history 注入", "The following is the Codex agent history" not in b)


def test_no_secret(mod) -> None:
    """输出不得包含 token / 认证头。

    用**明显是合成**的假 token，不写任何真实凭据：这个文件会进公开仓库。
    假值也要有足够长度和字符集，否则测不出清洗规则。
    """
    fake = "FAKE-TEST-TOKEN-0000000000000000"
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "rollout-h.jsonl"
        write_rollout(f, {"id": "hhhh-1", "session_id": "hhhh-1", "cwd": "/tmp/proj"},
                      [codex_user("任务"),
                       codex_cmd(f"export AI_MEMORY_AUTH_TOKEN={fake}")])
        ctx = mod.build_resume(f, "hhhh-1", "/tmp/proj", "default", "proj")
        check("秘密: 不泄漏 token", fake not in ctx["body"])


def test_unicode_path(mod) -> None:
    """中文/空格路径可正常处理。"""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "中文 项目"
        d.mkdir()
        f = d / "rollout-i.jsonl"
        write_rollout(f, {"id": "iiii-1", "session_id": "iiii-1", "cwd": str(d)},
                      [codex_user("中文任务")])
        ctx = mod.build_resume(f, "iiii-1", str(d), "default", "中文 项目")
        check("路径: 中文空格可处理", "中文任务" in ctx["body"])


def test_selection_no_guess(mod) -> None:
    """同 cwd 多个候选时不得擅自取 latest。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        cwd = "/tmp/proj"
        for sid in ("s1", "s2"):
            write_rollout(root / f"--tmp-proj--" / f"session-{sid}" / "rollout-x.jsonl",
                          {"id": sid, "session_id": sid, "cwd": cwd},
                          [codex_user(f"任务{sid}")])
        out = mod.select_candidate(root, cwd, agent="codex", session=None)
        check("选择: 多候选返回 needs-selection",
              out["status"] == "needs-selection", str(out["status"]))
        check("选择: 列出候选", len(out.get("candidates") or []) == 2,
              str(len(out.get("candidates") or [])))

        out2 = mod.select_candidate(root, cwd, agent="codex", session="s1")
        check("选择: 显式 session 可消歧",
              out2["status"] == "ready" and out2.get("native_id") == "s1",
              str(out2.get("native_id")))


def test_no_candidate(mod) -> None:
    """没有候选时给 no-candidate。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        root.mkdir()
        out = mod.select_candidate(root, "/tmp/nothing", agent="codex", session=None)
        check("选择: 无候选返回 no-candidate", out["status"] == "no-candidate", str(out["status"]))


def test_dedup_by_cursor(mod) -> None:
    """去重必须按 cursor/fingerprint，不是永久 seen。"""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "rollout-j.jsonl"
        write_rollout(f, {"id": "jjjj-1", "session_id": "jjjj-1", "cwd": "/tmp/proj"},
                      [codex_user("第一轮"), quota_complete()])
        c1 = mod.build_resume(f, "jjjj-1", "/tmp/proj", "default", "proj")
        # 同一 native 追加新事件（新一轮）
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(codex_user("第二轮新任务"), ensure_ascii=False) + "\n")
            fh.write(json.dumps(quota_complete(), ensure_ascii=False) + "\n")
        c2 = mod.build_resume(f, "jjjj-1", "/tmp/proj", "default", "proj")
        check("去重: 新事件后 cursor 变化",
              c1["cursor"] != c2["cursor"], f"{c1['cursor']} vs {c2['cursor']}")
        check("去重: 新任务可见", "第二轮新任务" in c2["body"])



def test_zstd_truncated_prefix(mod) -> None:
    """zstd 尾部坏帧时，必须保住已解码的有效前缀，并标记不完整。"""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        plain = d / "a.jsonl"
        lines = [json.dumps({"type": "session", "id": "zzzz-1", "cwd": "/tmp/proj"})]
        lines += [json.dumps(codex_user(f"任务{i}")) for i in range(20)]
        plain.write_text("\n".join(lines) + "\n")
        z = d / "session.v4.jsonl.zstd"
        subprocess.run(["zstd", "-q", "-f", str(plain), "-o", str(z)], check=True)
        z.write_bytes(z.read_bytes() + b"\x00\x00\x00\x00BROKEN")

        evs, incomplete = mod.read_dsh_events(z)
        check("zstd截断: 保住有效前缀", len(evs) >= 20, f"只解出 {len(evs)} 条")
        check("zstd截断: 标记不完整", incomplete is True, str(incomplete))


def test_cli_injectable(mod) -> None:
    """CLI 支持 --codex-root / --db 注入，不依赖 HOME。"""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "codex"
        cwd = "/tmp/proj-cli"
        d = root / "--tmp-proj-cli--" / "session-cccc-1"
        d.mkdir(parents=True)
        write_rollout(d / "rollout-x.jsonl",
                      {"id": "cccc-1", "session_id": "cccc-1", "cwd": cwd},
                      [codex_user("CLI 注入测试任务"), quota_complete()])
        out = subprocess.run(
            [sys.executable, str(TOOLS / "resume.py"),
             "--cwd", cwd, "--from-agent", "codex",
             "--codex-root", str(root),
             "--db", str(Path(tmp) / "nope.sqlite")],
            capture_output=True, text=True)
        check("CLI注入: 退出码 0", out.returncode == 0, str(out.returncode))
        check("CLI注入: 输出含任务", "CLI 注入测试任务" in out.stdout, out.stdout[:200])
        check("CLI注入: 识别 quota", "quota" in out.stdout.lower(), "")

def main() -> int:
    try:
        mod = load_resume()
    except FileNotFoundError:
        print("FAIL assets/tools/resume.py 不存在（预期：先失败）")
        return 1

    for fn in (test_quota_with_task_complete, test_clean_turn_not_old_quota,
               test_unpaired_tool, test_truncated_tail, test_cross_scope_page,
               test_missing_db,
               test_db_autosave_stale, test_db_autosave_fresh_is_quiet,
               test_db_active_session_not_flagged, test_db_no_autosave_page,
               test_db_ended_at_not_used_for_freshness,
               test_user_text_and_injection, test_no_secret,
               test_unicode_path, test_selection_no_guess, test_no_candidate,
               test_dedup_by_cursor, test_zstd_truncated_prefix, test_cli_injectable):
        try:
            fn(mod)
        except Exception as exc:  # noqa: BLE001
            check(f"{fn.__name__}: 未抛异常", False, f"{type(exc).__name__}: {exc}")

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        if not ok:
            print(f"FAIL {name}  [{detail}]")
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
