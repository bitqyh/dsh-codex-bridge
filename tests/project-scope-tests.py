#!/usr/bin/env python3
"""项目归属分组测试（纯只读，临时 fixture）。

覆盖「项目会话 / 非项目会话」两组分类所依赖的**只读元数据**语义：
  * 两组分类正确（project / unassigned）
  * 路径边界匹配与嵌套根（取最长根，/a/b 不匹配 /a/bc）
  * 日期形状路径：已登记则归项目，未登记则非项目（不按形状猜）
  * 元数据不可读（schema 不兼容）→ unavailable + 明确 reason + warning，
    且会话**不被丢弃**、也不被说成「确认非项目」
  * 合法但为空的注册表 → ok=True（空表不是错误）
  * threads.project_id 有值时**优先于**目录匹配
  * 正常目录匹配必须如实标 basis='dir'，不能冒充会话归属

运行：python3 tests/project-scope-tests.py
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "assets" / "tools"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def load_resume():
    spec = importlib.util.spec_from_file_location("resume_scope", TOOLS / "resume.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- fixture 工具
def write_rollout(path: Path, meta: dict, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session_meta", "payload": meta}, ensure_ascii=False) + "\n")
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def codex_user(text: str) -> dict:
    return {"type": "response_item",
            "payload": {"role": "user", "content": [{"type": "input_text", "text": text}]}}


def codex_task_complete() -> dict:
    return {"type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "done"}}


def make_registry_db(path: Path, *, projects=(), roots=(), threads=(),
                     skip=()) -> Path:
    """造一个 Codex 形状的 state db。

    projects/roots 是**必要**表，threads.project_id 是**可选**兼容来源。
    skip 用来模拟 schema 不兼容（缺表）。
    """
    conn = sqlite3.connect(path)
    if "projects" not in skip:
        conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT)")
        for pid, name in projects:
            conn.execute("INSERT INTO projects VALUES(?,?)", (pid, name))
    if "project_roots" not in skip:
        conn.execute("CREATE TABLE project_roots(project_id TEXT, path TEXT, position INTEGER)")
        for i, (p, pid) in enumerate(roots):
            conn.execute("INSERT INTO project_roots VALUES(?,?,?)", (pid, p, i))
    conn.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, project_id TEXT)")
    for tid, pid in threads:
        conn.execute("INSERT INTO threads VALUES(?,?)", (tid, pid))
    conn.commit()
    conn.close()
    return path


def with_registry(mod, db: Path, fn):
    """把全局 CODEX_STATE_DB 指到临时库，跑完恢复。"""
    prev = mod.CODEX_STATE_DB
    mod.CODEX_STATE_DB = db
    try:
        return fn()
    finally:
        mod.CODEX_STATE_DB = prev


def build(mod, root: Path) -> dict:
    return mod.build_candidates(root, "/", "codex", True, titles={})


# ---------------------------------------------------------------- 各场景
def test_two_groups(mod) -> None:
    """已归入项目的进 project 组，未命中任何依据的进 unassigned 组。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "sessions"
        write_rollout(root / "2026" / "11" / "01" / "rollout-a.jsonl",
                      {"id": "aaaa-1", "cwd": "/Users/q/Projects/alpha/src"},
                      [codex_user("做项目里的事"), codex_task_complete()])
        write_rollout(root / "2026" / "11" / "01" / "rollout-b.jsonl",
                      {"id": "bbbb-1", "cwd": "/Users/q/Loose"},
                      [codex_user("随便聊聊"), codex_task_complete()])
        db = make_registry_db(tmp / "s.sqlite",
                              projects=[("P1", "alpha")],
                              roots=[("/Users/q/Projects/alpha", "P1")])
        out = with_registry(mod, db, lambda: build(mod, root))
        rows = {r["native_id"]: r for r in out["sessions"]}
        check("两组: 共两条", len(rows) == 2, str(len(rows)))
        check("两组: 项目内会话进 project",
              rows["aaaa-1"]["scope"] == "project", rows["aaaa-1"]["scope"])
        check("两组: 项目内会话带项目名",
              rows["aaaa-1"]["project"] == "alpha", str(rows["aaaa-1"]["project"]))
        check("两组: 未命中进 unassigned",
              rows["bbbb-1"]["scope"] == "unassigned", rows["bbbb-1"]["scope"])
        check("两组: 未命中 project 为空",
              rows["bbbb-1"]["project"] is None, str(rows["bbbb-1"]["project"]))
        check("两组: 正常目录匹配标 dir（不冒充会话归属）",
              rows["aaaa-1"]["project_basis"] == "dir", rows["aaaa-1"]["project_basis"])
        check("两组: 未命中标 none",
              rows["bbbb-1"]["project_basis"] == "none", rows["bbbb-1"]["project_basis"])
        check("两组: 顶层 project_basis=dir", out["project_basis"] == "dir",
              str(out["project_basis"]))
        check("两组: 正常可读时无元数据警告",
              not [w for w in out["warnings"]
                   if w.get("kind") == "project-metadata-unavailable"],
              str(out["warnings"]))


def test_path_boundary_and_nested(mod) -> None:
    """路径边界：/a/b 不匹配 /a/bc；嵌套时取最长根。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = make_registry_db(
            tmp / "s.sqlite",
            projects=[("P1", "alpha"), ("P2", "nested")],
            roots=[("/Users/q/Projects/alpha", "P1"),
                   ("/Users/q/Projects/alpha/nested", "P2")])
        reg = mod.load_project_registry(db)

        def resolve(cwd):
            return mod._resolve_project("tid", cwd, reg)

        check("边界: 根本身命中", resolve("/Users/q/Projects/alpha")[0] == "alpha")
        check("边界: 子目录命中", resolve("/Users/q/Projects/alpha/src")[0] == "alpha")
        check("边界: 同前缀不同目录不命中（/alphaX）",
              resolve("/Users/q/Projects/alphaX")[0] is None,
              str(resolve("/Users/q/Projects/alphaX")))
        check("嵌套: 取最长根", resolve("/Users/q/Projects/alpha/nested/x")[0] == "nested",
              str(resolve("/Users/q/Projects/alpha/nested/x")))
        check("边界: 父目录不命中",
              resolve("/Users/q")[0] is None, str(resolve("/Users/q")))
        check("边界: 尾斜杠归一",
              resolve("/Users/q/Projects/alpha/")[0] == "alpha")


def test_ambiguous_root_is_unknown(mod) -> None:
    """同一路径登记给多个项目 → 归属未知（ambiguous），不按行序猜一个。

    真实场景：`.../Liner-transformer` 同时挂在「论文复现」和「PPT制作」下。
    注册表里两个根的路径长度相同，谁先出现只取决于 SQLite 返回顺序，
    所以任何「取第一个」的做法都是让行序静默决定归属。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shared = "/Users/q/Projects/gptwork/Liner-transformer"
        db = make_registry_db(
            tmp / "s.sqlite",
            projects=[("P1", "论文复现"), ("P2", "PPT制作"), ("P3", "独有项目")],
            roots=[(shared, "P1"),
                   (shared, "P2"),
                   ("/Users/q/Projects/gptwork/only-one", "P3")])

        def resolve(cwd):
            return mod._resolve_project("tid", cwd, mod.load_project_registry(db))

        check("歧义: 同一路径两个项目 → project 为 None",
              resolve(shared)[0] is None, str(resolve(shared)))
        check("歧义: basis 标 ambiguous",
              resolve(shared)[2] == "ambiguous", str(resolve(shared)))
        check("歧义: 子目录同样判为 ambiguous",
              resolve(shared + "/src")[2] == "ambiguous",
              str(resolve(shared + "/src")))
        check("歧义: 不因此影响别的根",
              resolve("/Users/q/Projects/gptwork/only-one")[0] == "独有项目",
              str(resolve("/Users/q/Projects/gptwork/only-one")))
        # 行序反过来也必须得到同样结果，否则说明仍在看顺序
        db2 = make_registry_db(
            tmp / "s2.sqlite",
            projects=[("P1", "论文复现"), ("P2", "PPT制作")],
            roots=[(shared, "P2"), (shared, "P1")])
        check("歧义: 行序反转结果不变",
              mod._resolve_project("tid", shared, mod.load_project_registry(db2))[2]
              == "ambiguous",
              str(mod._resolve_project("tid", shared, mod.load_project_registry(db2))))


def test_ambiguous_emits_warning(mod) -> None:
    """歧义会话要出现在非项目组，并单独提示——不计入「已跳过」。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shared = "/Users/q/Projects/gptwork/Liner-transformer"
        db = make_registry_db(
            tmp / "s.sqlite",
            projects=[("P1", "论文复现"), ("P2", "PPT制作")],
            roots=[(shared, "P1"), (shared, "P2")])
        sessions = tmp / "sessions"
        sessions.mkdir()
        write_rollout(
            sessions / "2026" / "11" / "01" / "rollout-amb.jsonl",
            {"id": "01a10000-0000-7000-8000-000000000001", "cwd": shared + "/src"},
            [codex_user("在歧义目录里干活"), codex_task_complete()])
        out = with_registry(mod, db, lambda: build(mod, sessions))

        row = out["sessions"][0]
        check("歧义: 会话仍在列表里（没被丢弃）", out["total"] == 1, str(out["total"]))
        check("歧义: scope 是 unassigned", row["scope"] == "unassigned", row["scope"])
        check("歧义: project 为 None", row["project"] is None, str(row["project"]))
        warn = [w for w in out["warnings"] if w.get("kind") == "project-ambiguous"]
        check("歧义: 发出 project-ambiguous 警告", len(warn) == 1, str(out["warnings"]))
        check("歧义: 警告带计数", bool(warn) and warn[0].get("count") == 1, str(warn))
        check("歧义: 警告不带 log 字段（不能被算成已跳过）",
              all("log" not in w for w in warn), str(warn))


def test_date_paths(mod) -> None:
    """日期形状路径按**是否登记**判定，不按形状猜。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = make_registry_db(
            tmp / "s.sqlite",
            projects=[("P1", "out")],
            roots=[("/Users/q/Documents/GPToutput/2026-11-01", "P1")])
        reg = mod.load_project_registry(db)
        registered = mod._resolve_project("t", "/Users/q/Documents/GPToutput/2026-11-01", reg)
        unregistered = mod._resolve_project("t", "/Users/q/Documents/GPToutput/2026-11-02", reg)
        check("日期路径: 已登记 → 归项目", registered[0] == "out", str(registered))
        check("日期路径: 已登记标 dir", registered[2] == "dir", str(registered))
        check("日期路径: 未登记 → 非项目", unregistered[0] is None, str(unregistered))
        check("日期路径: 未登记标 none", unregistered[2] == "none", str(unregistered))


def test_metadata_unavailable(mod) -> None:
    """schema 不兼容 → 不可用；会话仍列出、不被丢弃，也不说成「确认非项目」。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "sessions"
        write_rollout(root / "2026" / "11" / "01" / "rollout-c.jsonl",
                      {"id": "cccc-1", "cwd": "/Users/q/Projects/alpha/src"},
                      [codex_user("任务"), codex_task_complete()])
        # 缺 projects 表：不兼容 schema
        db = make_registry_db(tmp / "bad.sqlite", skip=("projects",))
        out = with_registry(mod, db, lambda: build(mod, root))
        check("不可用: 元数据 ok=False", mod.load_project_registry(db)["ok"] is False)
        check("不可用: 带具体 reason",
              "projects" in mod.load_project_registry(db)["reason"],
              mod.load_project_registry(db)["reason"])
        check("不可用: 会话不被丢弃", len(out["sessions"]) == 1, str(len(out["sessions"])))
        check("不可用: 归属标 unavailable",
              out["sessions"][0]["project_basis"] == "unavailable",
              out["sessions"][0]["project_basis"])
        check("不可用: 顶层 project_basis=unavailable",
              out["project_basis"] == "unavailable", str(out["project_basis"]))
        warn = [w for w in out["warnings"] if w.get("kind") == "project-metadata-unavailable"]
        check("不可用: 发出元数据警告", len(warn) == 1, str(out["warnings"]))
        check("不可用: 警告带 reason", bool(warn and warn[0].get("reason")), str(warn))
        # 关键：不可用的会话确实没有 log 字段 → 前端不会把它算进「已跳过」
        check("不可用: 警告不是「日志缺失」语义",
              all("log" not in w for w in warn), str(warn))

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # 缺 project_roots 表同样不可用
        db = make_registry_db(tmp / "bad2.sqlite", skip=("project_roots",))
        reg = mod.load_project_registry(db)
        check("不可用: 缺 project_roots 也 ok=False", reg["ok"] is False, str(reg["reason"]))
        check("不可用: reason 指明 project_roots",
              "project_roots" in reg["reason"], reg["reason"])


def test_empty_registry_is_valid(mod) -> None:
    """合法但为空（有表无行）不是错误，且不能被当成「元数据不可用」。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "sessions"
        write_rollout(root / "2026" / "11" / "01" / "rollout-d.jsonl",
                      {"id": "dddd-1", "cwd": "/Users/q/whatever"},
                      [codex_user("任务"), codex_task_complete()])
        db = make_registry_db(tmp / "empty.sqlite")
        reg = mod.load_project_registry(db)
        check("空注册表: ok=True", reg["ok"] is True, str(reg))
        check("空注册表: reason 为空", reg["reason"] == "", str(reg["reason"]))
        out = with_registry(mod, db, lambda: build(mod, root))
        check("空注册表: 不发元数据警告",
              not [w for w in out["warnings"]
                   if w.get("kind") == "project-metadata-unavailable"],
              str(out["warnings"]))
        check("空注册表: 归属标 none",
              out["sessions"][0]["project_basis"] == "none",
              out["sessions"][0]["project_basis"])
        check("空注册表: 顶层 basis=none", out["project_basis"] == "none",
              str(out["project_basis"]))


def test_thread_project_id_wins(mod) -> None:
    """threads.project_id 有值时优先于目录匹配（人工移入/移出项目不被覆盖）。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = make_registry_db(
            tmp / "s.sqlite",
            projects=[("P1", "alpha"), ("P2", "moved")],
            roots=[("/Users/q/Projects/alpha", "P1")],
            threads=[("eeee-1", "P2")])
        reg = mod.load_project_registry(db)
        moved = mod._resolve_project("eeee-1", "/Users/q/Projects/alpha/src", reg)
        check("优先: project_id 覆盖目录匹配", moved[0] == "moved", str(moved))
        check("优先: basis 标 thread", moved[2] == "thread", str(moved))
        # 没有 project_id 的会话仍退回目录匹配
        fallback = mod._resolve_project("ffff-1", "/Users/q/Projects/alpha/src", reg)
        check("优先: 无 project_id 时退回 dir",
              fallback[2] == "dir" and fallback[0] == "alpha", str(fallback))

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "sessions"
        write_rollout(root / "2026" / "11" / "01" / "rollout-e.jsonl",
                      {"id": "eeee-1", "cwd": "/Users/q/Projects/alpha/src"},
                      [codex_user("任务"), codex_task_complete()])
        db = make_registry_db(
            tmp / "s.sqlite",
            projects=[("P1", "alpha"), ("P2", "moved")],
            roots=[("/Users/q/Projects/alpha", "P1")],
            threads=[("eeee-1", "P2")])
        out = with_registry(mod, db, lambda: build(mod, root))
        check("优先: 顶层 basis=thread", out["project_basis"] == "thread",
              str(out["project_basis"]))
        check("优先: 行归属跟随 project_id",
              out["sessions"][0]["project"] == "moved",
              str(out["sessions"][0]["project"]))


def test_missing_db(mod) -> None:
    """库文件不存在 → unavailable，会话仍列出。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "sessions"
        write_rollout(root / "2026" / "11" / "01" / "rollout-f.jsonl",
                      {"id": "ffff-1", "cwd": "/Users/q/x"},
                      [codex_user("任务"), codex_task_complete()])
        db = tmp / "nope.sqlite"
        out = with_registry(mod, db, lambda: build(mod, root))
        check("缺库: 会话仍列出", len(out["sessions"]) == 1, str(len(out["sessions"])))
        check("缺库: 标 unavailable",
              out["sessions"][0]["project_basis"] == "unavailable",
              out["sessions"][0]["project_basis"])


def main() -> int:
    mod = load_resume()
    for fn in (test_two_groups, test_path_boundary_and_nested,
               test_ambiguous_root_is_unknown, test_ambiguous_emits_warning,
               test_date_paths,
               test_metadata_unavailable, test_empty_registry_is_valid,
               test_thread_project_id_wins, test_missing_db):
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
