#!/usr/bin/env python3
"""批次1 回归测试：准确 session 绑定 + 存档语义。

只用内存事件与临时目录，不写库、不读其他活动会话内容、不调用模型。
运行：python3 batch1-tests.py   退出码 0 表示全通过。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

TOOLS = Path(__file__).resolve().parent.parent / "assets" / "tools"
RUNTIME = Path.home() / "Library/Application Support/ai-memory/hooks"

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sections(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    key, text = "", []
    for line in body.splitlines():
        if line.startswith("## "):
            out[key] = "\n".join(text)
            key, text = line[3:], []
        else:
            text.append(line)
    out[key] = "\n".join(text)
    return out


def section_named(parts: dict[str, str], *needles: str) -> str:
    return "\n".join(v for k, v in parts.items() if all(n in k for n in needles))


# ---------------------------------------------------------------- 语义（DSH）
def test_dsh_semantics(mod, label: str) -> None:
    events = [
        {"type": "user/message", "data": {"source": {"kind": "user"},
                                          "content": [{"type": "text", "text": "FIRST_TASK_ANCHOR"}]}},
        {"type": "user/message", "data": {"source": {"kind": "user-rpc"},
                                          "content": [{"type": "text", "text": "RPC_USER_REQUEST"}]}},
        {"type": "assistant/message", "data": {"message": {"role": "assistant",
                                                           "content": [{"type": "text", "text": "VISIBLE_FINAL_REPLY"}]}}},
        {"type": "tool/call", "data": {"callId": "ok", "name": "edit",
                                       "arguments": '{"file_path":"SUCCESSFUL_FILE"}'}},
        {"type": "tool/result", "data": {"message": {"toolCallId": "ok", "isError": False,
                                                     "content": [{"type": "text", "text": "edited"}]}}},
        {"type": "tool/call", "data": {"callId": "bad", "name": "edit",
                                       "arguments": '{"file_path":"FAILED_FILE"}'}},
        {"type": "tool/result", "data": {"message": {"toolCallId": "bad", "isError": True,
                                                     "content": [{"type": "text", "text": "denied"}]}}},
        {"type": "tool/call", "data": {"callId": "unknown", "name": "edit",
                                       "arguments": '{"file_path":"UNKNOWN_FILE"}'}},
    ]
    body = mod.build(events, "test-native-id", "/tmp/normal-test")
    parts = sections(body)
    confirmed = section_named(parts, "改过", "文件")

    check(f"{label}: rpc 用户输入可见", "RPC_USER_REQUEST" in body)
    check(f"{label}: 嵌套 assistant 可见", "VISIBLE_FINAL_REPLY" in body)
    check(f"{label}: 成功 edit 计入确认", "SUCCESSFUL_FILE" in confirmed)
    check(f"{label}: 失败 edit 不计入确认", "FAILED_FILE" not in confirmed)
    check(f"{label}: 未知 edit 不计入确认", "UNKNOWN_FILE" not in confirmed)

    long_events = [{"type": "user/message",
                    "data": {"source": {"kind": "user"},
                             "content": [{"type": "text", "text": t}]}}
                   for t in ["FIRST_TASK_ANCHOR"] + [f"followup {i}" for i in range(10)] + ["LATEST_CORRECTION"]]
    body2 = mod.build(long_events, "test-native-id", "/tmp/normal-test")
    check(f"{label}: 初始目标与最新修正都保留",
          "FIRST_TASK_ANCHOR" in body2 and "LATEST_CORRECTION" in body2)

    # reasoning 不得泄漏
    ev_reason = [
        {"type": "user/message", "data": {"source": {"kind": "user"},
                                          "content": [{"type": "text", "text": "GOAL"}]}},
        {"type": "assistant/message", "data": {"message": {"role": "assistant", "content": [
            {"type": "reasoning", "text": "SECRET_REASONING_TEXT"},
            {"type": "text", "text": "VISIBLE_ONLY"},
        ]}}},
    ]
    body3 = mod.build(ev_reason, "test-native-id", "/tmp/normal-test")
    check(f"{label}: 不泄漏 reasoning", "SECRET_REASONING_TEXT" not in body3)
    check(f"{label}: 可见文本仍保留", "VISIBLE_ONLY" in body3)

    # user-approval 不得被当成初始目标
    ev_appr = [
        {"type": "user/message", "data": {"source": {"kind": "user"},
                                          "content": [{"type": "text", "text": "REAL_GOAL"}]}},
        {"type": "user/message", "data": {"source": {"kind": "user-approval"},
                                          "content": [{"type": "text", "text": "APPROVAL_NOTE"}]}},
    ]
    body4 = mod.build(ev_appr, "test-native-id", "/tmp/normal-test")
    goal_sec = section_named(sections(body4), "任务")
    check(f"{label}: user-approval 不算初始目标", "APPROVAL_NOTE" not in goal_sec)
    check(f"{label}: 真实目标仍在", "REAL_GOAL" in goal_sec)


# ---------------------------------------------------------------- 语义（Codex）
def test_codex_semantics(mod, label: str) -> None:
    body = mod.build([
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "FileChange", "changes": {"SUCCESSFUL_FILE": {"type": "update"}}, "status": "Completed"}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "FileChange", "changes": {"FAILED_FILE": {"type": "update"}}, "status": "Failed"}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "FileChange", "changes": {"UNKNOWN_FILE": {"type": "update"}}}}},
    ], "test-native-id", "/tmp/normal-test")
    parts = sections(body)
    confirmed = section_named(parts, "改过", "文件")

    check(f"{label}: Completed 计入确认", "SUCCESSFUL_FILE" in confirmed)
    check(f"{label}: Failed 不计入确认", "FAILED_FILE" not in confirmed)
    check(f"{label}: 无 status 不计入确认", "UNKNOWN_FILE" not in confirmed)

    # HTML 真实输入保留，注入排除
    check(f"{label}: HTML 用户输入保留",
          mod.is_injected("<div>请修改这个HTML组件</div>") is False)
    for inj in ("<user_instructions>x", "# AGENTS.md instructions",
                "The following is the Codex agent history"):
        check(f"{label}: 注入被排除 {inj[:24]!r}", mod.is_injected(inj) is True)

    # 初始目标与最新修正
    ev = []
    for t in ["FIRST_TASK_ANCHOR"] + [f"f{i}" for i in range(10)] + ["LATEST_CORRECTION"]:
        ev.append({"type": "response_item",
                   "payload": {"role": "user", "content": [{"type": "input_text", "text": t}]}})
    b2 = mod.build(ev, "test-native-id", "/tmp/normal-test")
    check(f"{label}: 初始目标与最新修正都保留",
          "FIRST_TASK_ANCHOR" in b2 and "LATEST_CORRECTION" in b2)

    # 命令不谎报成功
    b3 = mod.build([{"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "CommandExecution", "command": ["/bin/zsh", "-lc", "false"],
        "status": "Failed", "exit_code": 1}}}], "test-native-id", "/tmp/normal-test")
    check(f"{label}: 命令区标注不代表成功",
          "不代表" in b3 or "调用记录" in b3 or "状态未知" in b3)


# ---------------------------------------------------------------- session 选择
def test_session_selection(mod, label: str) -> None:
    with tempfile.TemporaryDirectory(prefix="batch1-") as tmp:
        root = Path(tmp).resolve()
        sessions = root / "sessions"
        proj_a = sessions / "--proj-a--"
        proj_b = sessions / "--proj-b--"
        for d in (proj_a, proj_b):
            d.mkdir(parents=True)

        def mk(parent: Path, sid: str, cwd: str) -> Path:
            d = parent / f"session-{sid}"
            d.mkdir()
            f = d / "session.v4.jsonl.zstd"
            import gzip
            payload = json.dumps({"type": "session", "id": f"session-{sid}", "cwd": cwd}) + "\n"
            with gzip.open(f, "wb") as fh:
                fh.write(payload.encode())
            return d

        a1 = mk(proj_a, "aaaa1111-0000-0000-0000-000000000001", "/tmp/proj-a")
        a2 = mk(proj_a, "aaaa2222-0000-0000-0000-000000000002", "/tmp/proj-a")
        b1 = mk(proj_b, "bbbb1111-0000-0000-0000-000000000001", "/tmp/proj-b")

        mod.SESSIONS_ROOT = sessions

        # 精确 session id：同 cwd 双会话必须选中指定的那个
        try:
            got = mod.find_session("/tmp/proj-a", "aaaa1111-0000-0000-0000-000000000001")
            check(f"{label}: 精确 id 选中正确会话", got.resolve() == a1.resolve(), str(got))
        except SystemExit as e:
            check(f"{label}: 精确 id 选中正确会话", False, f"SystemExit: {e}")

        # 同 cwd 双会话 + 只给 cwd → 必须拒绝，不能 mtime 猜
        try:
            mod.find_session("/tmp/proj-a", None)
            check(f"{label}: 同cwd双会话拒绝猜测", False, "未报错，可能猜了最新")
        except SystemExit:
            check(f"{label}: 同cwd双会话拒绝猜测", True)

        # 缺失 id → 必须拒绝
        try:
            mod.find_session("/tmp/proj-a", "does-not-exist-9999")
            check(f"{label}: 缺失 id 拒绝", False, "未报错")
        except SystemExit:
            check(f"{label}: 缺失 id 拒绝", True)

        # 单会话 + cwd → 允许（唯一无歧义）
        try:
            got = mod.find_session("/tmp/proj-b", None)
            check(f"{label}: 唯一会话可按cwd选", got.resolve() == b1.resolve(), str(got))
        except SystemExit as e:
            check(f"{label}: 唯一会话可按cwd选", False, f"SystemExit: {e}")

        # 不能跨项目串
        try:
            mod.find_session("/tmp/proj-b", "aaaa1111-0000-0000-0000-000000000001")
            check(f"{label}: 不跨项目选错", False, "选到了别的项目")
        except SystemExit:
            check(f"{label}: 不跨项目选错", True)


# ---------------------------------------------------------------- 中文/空格路径
def test_unicode_paths(mod, label: str) -> None:
    with tempfile.TemporaryDirectory(prefix="batch1-uni-") as tmp:
        root = Path(tmp).resolve()
        sessions = root / "sessions"
        # 用真实 DSH 编码规则产生 slug（~XXXX，UTF-16 code unit）
        def project_key(cwd: str) -> str:
            readable, sep = "", False
            for ch in cwd:
                code = ord(ch)
                if ch in "/\\:":
                    if not sep:
                        readable += "-"
                    sep = True
                elif ch != "~" and (ch.isascii() and (ch.isalnum() or ch in "._-")):
                    readable += ch
                    sep = False
                else:
                    readable += "~" + format(code, "04X")
                    sep = False
            core = readable.lstrip("-") or "root"
            return f"--{core[:251]}--"

        for cwd in ("/tmp/中文 测试", "/tmp/with space", "/tmp/emoji-😀"):
            d = sessions / project_key(cwd) / "session-cccc1111-0000-0000-0000-000000000001"
            d.mkdir(parents=True)
            import gzip
            with gzip.open(d / "session.v4.jsonl.zstd", "wb") as fh:
                fh.write((json.dumps({"type": "session",
                                      "id": "session-cccc1111-0000-0000-0000-000000000001",
                                      "cwd": cwd}) + "\n").encode())

        mod.SESSIONS_ROOT = sessions
        for cwd in ("/tmp/中文 测试", "/tmp/with space", "/tmp/emoji-😀"):
            try:
                got = mod.find_session(cwd, "cccc1111-0000-0000-0000-000000000001")
                ok = "session-cccc1111" in got.name
                check(f"{label}: 中文/空格/emoji 路径可定位 {cwd!r}", ok, str(got))
            except SystemExit as e:
                check(f"{label}: 中文/空格/emoji 路径可定位 {cwd!r}", False, f"SystemExit: {e}")


def main() -> int:
    for path, label, tester in (
        (TOOLS / "dsh-checkpoint.py", "tools/dsh", None),
        (RUNTIME / "dsh/dsh-checkpoint.py", "runtime/dsh", None),
        (TOOLS / "codex-checkpoint.py", "tools/codex", None),
        (RUNTIME / "codex/codex-checkpoint.py", "runtime/codex", None),
    ):
        if not path.exists():
            check(f"{label}: 文件存在", False, str(path))
            continue
        mod = load(path, "m_" + label.replace("/", "_"))
        if "dsh" in label:
            test_dsh_semantics(mod, label)
            test_session_selection(mod, label)
            test_unicode_paths(mod, label)
        else:
            test_codex_semantics(mod, label)

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        if not ok:
            print(f"FAIL {name}" + (f"  [{detail}]" if detail else ""))
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
