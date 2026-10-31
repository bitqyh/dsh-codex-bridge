#!/usr/bin/env python3
"""批次2 fixture 测试：证明 verifier 不会假绿。

全部用临时目录与临时 sqlite，不触真实库、不联网、不调用模型。
运行：python3 batch2-tests.py
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.dont_write_bytecode = True

TOOLS = Path(__file__).resolve().parent.parent / "assets" / "tools"
VERIFIER = TOOLS / "verify-autosave.py"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(VERIFIER), *args],
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def write_hooks(directory: Path, payload) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / "hooks.json"
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return p


def good_hooks(script_dir: Path | None = None) -> dict:
    """所有必需事件都指向**真实存在**的 .sh（verifier 会 stat 它）。"""
    d = script_dir or Path.home() / "Library/Application Support/ai-memory/hooks/dsh"
    ev = {}
    mapping = {
        "SessionStart": "session-start.sh", "UserPromptSubmit": "user-prompt-submit.sh",
        "PreToolUse": "pre-tool-use.sh", "PostToolUse": "post-tool-use.sh",
        "Stop": "stop.sh", "SubagentStart": "subagent-start.sh",
        "SubagentStop": "subagent-stop.sh",
    }
    for name, fname in mapping.items():
        ev[name] = [{"matcher": "", "hooks": [
            {"type": "command", "command": f"sh '{d / fname}'"}]}]
    return {"hooks": ev}


def make_db(path: Path, *, ws: str, proj: str, native: str,
            kinds: list[str], page_body: str | None,
            other_scope: bool = False) -> None:
    """建一个最小 ai-memory 形状的库。"""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE workspaces (id BLOB PRIMARY KEY, name TEXT);
        CREATE TABLE projects (id BLOB PRIMARY KEY, workspace_id BLOB, name TEXT);
        CREATE TABLE sessions (id BLOB PRIMARY KEY, workspace_id BLOB,
                               project_id BLOB, agent_kind TEXT, cwd TEXT,
                               started_at INTEGER, ended_at INTEGER);
        CREATE TABLE observations (id BLOB PRIMARY KEY, session_id BLOB,
                                   workspace_id BLOB, project_id BLOB,
                                   kind TEXT, created_at INTEGER);
        CREATE TABLE pages (id BLOB PRIMARY KEY, workspace_id BLOB,
                            project_id BLOB, path TEXT, body TEXT,
                            is_latest INTEGER, created_at INTEGER);
    """)
    ws_id = uuid.uuid4().bytes
    pj_id = uuid.uuid4().bytes
    conn.execute("INSERT INTO workspaces VALUES (?,?)", (ws_id, ws))
    conn.execute("INSERT INTO projects VALUES (?,?,?)", (pj_id, ws_id, proj))

    canon = (str(uuid.UUID(native)) if _is_uuid(native)
             else str(uuid.uuid5(uuid.NAMESPACE_OID, native)))
    sid = uuid.UUID(canon).bytes
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                 (sid, ws_id, pj_id, "dsh", "/tmp/p", 1, None))
    for i, k in enumerate(kinds):
        conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)",
                     (uuid.uuid4().bytes, sid, ws_id, pj_id, k, 1000 + i))
    # 页面时间要晚于最后一次 stop，否则会被判「上一轮漏存档」

    if page_body is not None:
        bare = native[len("session-"):] if native.startswith("session-") else native
        # 空正文用例保持空（测的是「空正文必须 fail」）；
        # 非空正文才补 id，否则会被判「不属于该目标」
        if page_body.strip():
            full_body = page_body + f"\n原生会话：{native}\ncanonical DB id：{canon}\n"
        else:
            full_body = page_body
        conn.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?)",
                     (uuid.uuid4().bytes, ws_id, pj_id,
                      f"_autosave/dsh-{bare}.md", full_body, 1, 4000))

    if other_scope:
        # 另一个 scope 里放健康数据，证明不会被借用
        ws2, pj2 = uuid.uuid4().bytes, uuid.uuid4().bytes
        conn.execute("INSERT INTO workspaces VALUES (?,?)", (ws2, "other-ws"))
        conn.execute("INSERT INTO projects VALUES (?,?,?)", (pj2, ws2, "other-proj"))
        sid2 = uuid.uuid4().bytes
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                     (sid2, ws2, pj2, "dsh", "/tmp/o", 1, None))
        for k in ("user-prompt", "pre-tool-use", "post-tool-use"):
            conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)",
                         (uuid.uuid4().bytes, sid2, ws2, pj2, k, 1500))
    conn.commit()
    conn.close()


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except ValueError:
        return False


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="batch2-") as tmp:
        root = Path(tmp).resolve()

        # 1) 畸形 hooks JSON → fail
        d = root / "h1"
        write_hooks(d, "{not json")
        rc, out = run(["--skip-network", "--hooks-dir", str(d)])
        check("畸形 hooks JSON 判 fail", rc != 0 and "fail" in out, out[:120])

        # 2) 缺必需事件 → fail
        d = root / "h2"
        h = good_hooks()
        del h["hooks"]["Stop"]
        write_hooks(d, h)
        rc, out = run(["--skip-network", "--hooks-dir", str(d)])
        check("缺 Stop 事件判 fail", rc != 0 and "Stop" in out, out[:160])

        # 3) 空 groups → fail
        d = root / "h3"
        h = good_hooks()
        h["hooks"]["PreToolUse"] = []
        write_hooks(d, h)
        rc, out = run(["--skip-network", "--hooks-dir", str(d)])
        check("空 groups 判 fail", rc != 0 and "groups" in out, out[:160])

        # 4) 非脚本模式 → fail
        d = root / "h4"
        h = good_hooks()
        h["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = "/usr/bin/ai-memory hook"
        write_hooks(d, h)
        rc, out = run(["--skip-network", "--hooks-dir", str(d)])
        check("非脚本模式判 fail", rc != 0 and "非脚本" in out, out[:160])

        # 5) 健康 hooks + 健康目标 → 全绿
        d = root / "hok"
        write_hooks(d, good_hooks())
        db = root / "ok.sqlite"
        native = "session-" + str(uuid.uuid4())
        make_db(db, ws="default", proj="p1", native=native,
                kinds=["user-prompt", "pre-tool-use", "post-tool-use", "stop"],
                page_body="# 存档\n有内容")
        rc, out = run(["--skip-network", "--hooks-dir", str(d), "--db", str(db),
                       "--agent", "dsh", "--session", native,
                       "--workspace", "default", "--project", "p1"])
        check("健康目标 0 fail 0 incomplete（skip-network 下不算 live 完成）",
              "0 fail" in out and "0 incomplete" in out and rc != 0, out[-300:])

        # 6) 只有其他 scope 的历史 → 不能全绿
        db2 = root / "other.sqlite"
        make_db(db2, ws="default", proj="p1", native=native, kinds=[],
                page_body=None, other_scope=True)
        rc, out = run(["--skip-network", "--hooks-dir", str(d), "--db", str(db2),
                       "--agent", "dsh", "--session", native,
                       "--workspace", "default", "--project", "p1"])
        check("只有其他 scope 历史不全绿", rc != 0, out[-200:])

        # 7) 存档页空正文 → fail
        db3 = root / "empty.sqlite"
        make_db(db3, ws="default", proj="p1", native=native,
                kinds=["user-prompt", "pre-tool-use", "post-tool-use", "stop"],
                page_body="   \n  ")
        rc, out = run(["--skip-network", "--hooks-dir", str(d), "--db", str(db3),
                       "--agent", "dsh", "--session", native,
                       "--workspace", "default", "--project", "p1"])
        check("存档正文为空判 fail", rc != 0 and "空" in out, out[-200:])

        # 8) 目标会话不存在 → fail
        db4 = root / "nosess.sqlite"
        make_db(db4, ws="default", proj="p1", native=native,
                kinds=["user-prompt"], page_body=None)
        rc, out = run(["--skip-network", "--hooks-dir", str(d), "--db", str(db4),
                       "--agent", "dsh", "--session", "session-" + str(uuid.uuid4()),
                       "--workspace", "default", "--project", "p1"])
        check("目标会话不存在判 fail", rc != 0 and "没有这个会话" in out, out[-200:])

        # 9) 缺工具事件 → fail（不能靠 user-prompt 顶替）
        db5 = root / "notools.sqlite"
        make_db(db5, ws="default", proj="p1", native=native,
                kinds=["user-prompt"], page_body="x")
        rc, out = run(["--skip-network", "--hooks-dir", str(d), "--db", str(db5),
                       "--agent", "dsh", "--session", native,
                       "--workspace", "default", "--project", "p1"])
        check("缺工具事件判 fail", rc != 0 and "缺" in out, out[-200:])

        # 10) 无 --session → incomplete，不能说健康
        rc, out = run(["--skip-network", "--hooks-dir", str(d), "--db", str(db),
                       "--workspace", "default", "--project", "p1"])
        check("无 session 时只报概览且非零",
              rc != 0 and "incomplete" in out and "未完整验证" in out, out[-200:])

        # 11) 位置参数原样保留（旧版 set -- 覆盖它）
        rc, out = run(["--skip-network", "--hooks-dir", str(d), "--db", str(db),
                       "/Users/qyh/Documents/deepseek-harness/default-workspace"])
        check("位置参数未被覆盖", "default-workspace" in out, out[:200])

        # 12) 只读：不给写权限也能跑（库文件设为只读）
        ro_db = root / "ro.sqlite"
        make_db(ro_db, ws="default", proj="p1", native=native,
                kinds=["user-prompt", "pre-tool-use", "post-tool-use", "stop"],
                page_body="ok")
        ro_db.chmod(0o444)
        rc, out = run(["--skip-network", "--hooks-dir", str(d), "--db", str(ro_db),
                       "--agent", "dsh", "--session", native,
                       "--workspace", "default", "--project", "p1"])
        check("只读库可正常验收", "0 fail" in out and "0 incomplete" in out, out[-200:])

        # 13) 多候选 app-server 不得挑第一个判 pass（P1-6 假绿）
        import importlib.util as _il
        _spec = _il.spec_from_file_location("_v", TOOLS / "verify-autosave.py")
        vmod = _il.module_from_spec(_spec); _spec.loader.exec_module(vmod)
        from unittest.mock import patch as _patch
        hj = root / "hj.json"; hj.write_text("{}")

        def _proc_case(pids, lstart, agent):
            rep = vmod.Report()
            def fake_run(cmd, **kw):
                class P: pass
                q = P()
                q.stdout = (" ".join(pids) if cmd[0] == "pgrep"
                            else lstart if cmd[0] == "ps"
                            else "Oct 30 11:46:09 2026")
                return q
            with _patch.object(vmod.subprocess, "run", side_effect=fake_run):
                vmod.check_process(rep, hj, agent=agent)
            return rep

        r = _proc_case(["1", "2", "3"], "Fri Oct 30 20:00:00 2026", "codex")
        check("多候选 app-server 判 incomplete",
              any(i["status"] == vmod.INCOMPLETE and "候选" in i["detail"]
                  for i in r.items), str(r.items)[:150])

        r = _proc_case([], "", "dsh")
        check("dsh 目标下进程项为参考信息",
              all("[参考信息" in i["detail"] for i in r.items), str(r.items)[:150])
        check("dsh 目标下进程项不计入失败", r.ok, str(r.items)[:150])

        r = _proc_case([], "", "codex")
        check("codex 目标无进程判 incomplete", not r.ok, str(r.items)[:150])

        r = _proc_case(["9"], "Fri Oct 30 20:00:00 2026", "codex")
        check("codex 单候选较新判 pass", r.ok, str(r.items)[:150])

        # 14) 存档新鲜度：额度中断（无 Stop、观测仍在写）必须暴露
        def _fresh_case(lag_seconds, label):
            dbf = root / f"fresh-{label}.sqlite"
            make_db(dbf, ws="default", proj="p1", native=native,
                    kinds=["user-prompt", "pre-tool-use", "post-tool-use", "stop"],
                    page_body="# x")
            c = sqlite3.connect(dbf)
            # 页面时间固定；把最后一条观测推到 lag 秒之后
            page_t = c.execute("SELECT created_at FROM pages").fetchone()[0]
            c.execute("UPDATE observations SET created_at = ? WHERE kind='stop'",
                      (page_t - 1_000_000,))
            c.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)",
                      (uuid.uuid4().bytes,
                       uuid.UUID(str(uuid.uuid5(uuid.NAMESPACE_OID, native))).bytes,
                       c.execute("SELECT id FROM workspaces").fetchone()[0],
                       c.execute("SELECT id FROM projects").fetchone()[0],
                       "post-tool-use",
                       page_t + lag_seconds * 1_000_000))
            c.commit(); c.close()
            rc, out = run(["--skip-network", "--hooks-dir", str(d),
                           "--db", str(dbf), "--agent", "dsh", "--session", native,
                           "--workspace", "default", "--project", "p1"])
            return rc, out

        rc, out = _fresh_case(900, "stale")
        check("存档落后 15 分钟判 incomplete",
              "存档新鲜度" in out and "incomplete" in out, out[-250:])

        rc, out = _fresh_case(30, "ok")
        check("存档落后 30 秒仍判 pass",
              "存档新鲜度" in out and "容忍范围" in out, out[-250:])

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        if not ok:
            print(f"FAIL {name}  [{detail}]")
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
