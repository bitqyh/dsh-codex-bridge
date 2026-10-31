#!/usr/bin/env python3
"""ai-memory 交接/自动存档 验收工具（只读）。

结论分三级：
  pass       该项检查通过
  fail       该项确证有问题
  incomplete 数据不足/无法判定（不能当通过）

退出码：全部 pass → 0；任何 fail 或 incomplete → 非零。

只读保证：SQLite 用 `mode=ro` 打开；不领取 handoff、不改配置、
不启动新会话、不调用模型。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import sqlite3
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path

DATA_DIR = Path.home() / "Library/Application Support/ai-memory"
DB_PATH = DATA_DIR / "db" / "memory.sqlite"
DSH_HOOKS = DATA_DIR / "hooks" / "dsh" / "hooks.json"
CODEX_HOOKS = Path.home() / ".codex" / "hooks.json"
SERVER = "http://127.0.0.1:49374"
TOOLS_DIR = Path(__file__).resolve().parent

REQUIRED_DSH_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse",
                       "PostToolUse", "Stop", "SubagentStart", "SubagentStop")

PASS, FAIL, INCOMPLETE = "pass", "fail", "incomplete"


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.items.append({"check": name, "status": status, "detail": detail})

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i["status"] == status)

    @property
    def ok(self) -> bool:
        return self.count(FAIL) == 0 and self.count(INCOMPLETE) == 0


def canonical_session_id(native: str) -> str:
    """对齐 ai-memory 的 SessionId::from_native：纯 UUID 原样，否则 uuid5。"""
    try:
        return str(uuid.UUID(native))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(uuid.NAMESPACE_OID, native))


def load_resolve_project():
    """复用 dsh-checkpoint.py 的 scope 推导，不复制逻辑。"""
    spec = importlib.util.spec_from_file_location(
        "_ck", TOOLS_DIR / "dsh-checkpoint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.resolve_project


# ------------------------------------------------------------------ 各项检查
def check_service(rep: Report, server: str) -> None:
    try:
        with urllib.request.urlopen(f"{server}/healthz", timeout=3) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        ok = isinstance(parsed, dict) and parsed.get("status") == "ok"
        rep.add("服务", PASS if ok else FAIL,
                "" if ok else f"healthz 返回异常：{body[:80]}")
    except Exception as exc:  # noqa: BLE001 - 任何失败都是不可用
        rep.add("服务", FAIL, f"连不上 {server}（{type(exc).__name__}）")



def _command_script_path(command: str) -> str | None:
    """从 hook command 里解析出实际要执行的脚本路径。

    只认 `sh <script>` / `<script>` 这类形式；`echo x.sh` 这种把 .sh 当参数
    的写法必须被排除，否则会假绿。解析失败返回 None。
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    # 去掉前置的 VAR=value 赋值
    idx = 0
    while idx < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        return None
    head = tokens[idx]
    if Path(head).name in ("sh", "bash", "zsh", "dash", "env"):
        rest = [t for t in tokens[idx + 1:]
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) and not t.startswith("-")]
        if not rest:
            return None
        candidate = rest[0]
    else:
        candidate = head
    if not candidate.endswith(".sh"):
        return None
    return candidate


def check_dsh_hooks(rep: Report, path: Path) -> None:
    """遍历所有事件、所有组、所有 handler。"""
    if not path.is_file():
        rep.add("DSH hooks 定义", FAIL, f"找不到 {path}")
        return
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        rep.add("DSH hooks 定义", FAIL, f"hooks.json 不是合法 JSON（{exc.msg}）")
        return
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        rep.add("DSH hooks 定义", FAIL, "hooks 为空或结构不对")
        return

    problems: list[str] = []
    missing = [e for e in REQUIRED_DSH_EVENTS if e not in hooks]
    if missing:
        problems.append("缺必需事件：" + "、".join(missing))

    total_handlers = 0
    non_script: list[str] = []
    for event, groups in hooks.items():
        if not isinstance(groups, list) or not groups:
            problems.append(f"{event} 的 groups 为空")
            continue
        for gi, group in enumerate(groups):
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list) or not handlers:
                problems.append(f"{event}[{gi}] 没有 handler")
                continue
            for hi, h in enumerate(handlers):
                total_handlers += 1
                if not isinstance(h, dict):
                    problems.append(f"{event}[{gi}][{hi}] handler 不是对象")
                    continue
                if h.get("type") != "command":
                    problems.append(f"{event}[{gi}][{hi}] type 不是 command")
                    continue
                cmd = h.get("command")
                if not isinstance(cmd, str) or not cmd.strip():
                    problems.append(f"{event}[{gi}][{hi}] command 缺失")
                    continue
                # 用 shlex 解析出实际脚本路径并确认存在，
                # 不能只找 ".sh" 子串（`echo not-a-script.sh` 会蒙混过关）
                script = _command_script_path(cmd)
                if script is None:
                    non_script.append(f"{event}[{gi}][{hi}]")
                elif not Path(script).is_file():
                    problems.append(
                        f"{event}[{gi}][{hi}] 脚本不存在：{script}")

    if non_script:
        problems.append("非脚本模式（沙箱会丢事件）：" + "、".join(non_script))
    if total_handlers == 0:
        problems.append("没有任何 handler")

    if problems:
        rep.add("DSH hooks 定义", FAIL, "；".join(problems))
    else:
        rep.add("DSH hooks 定义", PASS, f"{len(hooks)} 事件 / {total_handlers} handler 全脚本模式")



def _codex_hooks_list(timeout: float = 20.0) -> dict | None:
    """跑一次 codex app-server 拿 hooks/list 的响应。

    总超时 `timeout` 秒；超时或异常都返回 None。子进程一定被 kill 并 reap。
    只读：不写配置、不改信任状态。
    """
    import threading
    import time as _time

    try:
        proc = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    result: dict | None = None
    done = threading.Event()

    def reader() -> None:
        nonlocal result
        deadline = _time.monotonic() + timeout
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if _time.monotonic() > deadline:
                    break
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("id") == 1:
                    # initialize 有响应后才发后续请求，避免时序竞态
                    _send({"jsonrpc": "2.0", "method": "initialized"})
                    _send({"jsonrpc": "2.0", "id": 2, "method": "hooks/list",
                           "params": {}})
                elif obj.get("id") == 2:
                    result = obj
                    break
        except (OSError, ValueError):
            pass
        finally:
            done.set()

    def _send(obj: dict) -> None:
        try:
            proc.stdin.write(json.dumps(obj) + "\n")  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    _send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"clientInfo": {"name": "verify-autosave", "version": "0"}}})
    done.wait(timeout)
    try:
        proc.kill()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def check_codex_hooks(rep: Report, hooks_json: Path) -> None:
    """读 Codex 钩子信任状态。结论仅限「磁盘定义/信任」，不代表 GUI 已加载。"""
    note = "（仅磁盘定义/信任检查，不代表当前 GUI 已加载）"
    if not hooks_json.is_file():
        rep.add("Codex hooks 信任", FAIL, "找不到 ~/.codex/hooks.json")
        return
    # 用真正带总超时的 RPC：起子进程 → 写 initialize → 等它的响应 →
    # 再写 initialized 与 hooks/list。不做固定 sleep 猜测；超时后 kill+reap
    # 自己起的进程（绝不影响用户已有的 app-server）。
    payload = _codex_hooks_list(timeout=20)
    if payload is None:
        rep.add("Codex hooks 信任", INCOMPLETE,
                f"codex app-server RPC 超时或不可用{note}")
        return

    if "error" in payload:
        rep.add("Codex hooks 信任", FAIL, f"hooks/list 报错{note}")
        return

    data = (payload.get("result") or {}).get("data") or []
    total = bad = stops = 0
    for grp in data:
        for h in grp.get("hooks") or []:
            total += 1
            if h.get("trustStatus") != "trusted":
                bad += 1
            if h.get("eventName") == "stop":
                stops += 1
    if total == 0:
        rep.add("Codex hooks 信任", FAIL, f"hooks/list 没有返回任何钩子{note}")
    elif bad:
        rep.add("Codex hooks 信任", FAIL, f"{total} 条里 {bad} 条未信任{note}")
    elif stops < 2:
        rep.add("Codex hooks 信任", FAIL, f"Stop 只有 {stops} 条，自动存档那条没加载{note}")
    else:
        rep.add("Codex hooks 信任", PASS, f"{total} 条全 trusted（Stop {stops} 条）{note}")


def open_db_ro(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def check_target(rep: Report, db_path: Path, agent: str, native_id: str,
                 workspace: str, project: str) -> None:
    """按 workspace + project + canonical session id 精确核对采集与存档。"""
    conn = open_db_ro(db_path)
    if conn is None:
        rep.add("目标会话采集", INCOMPLETE, f"数据库不可读：{db_path}")
        rep.add("存档页", INCOMPLETE, "数据库不可读")
        return
    canon = canonical_session_id(native_id)
    label = f"{workspace}/{project} · {native_id} → {canon}"
    ids = conn.execute(
        """SELECT w.id, p.id FROM projects p JOIN workspaces w ON p.workspace_id = w.id
           WHERE w.name = ? AND p.name = ?""",
        (workspace, project),
    ).fetchone()
    if ids is None:
        rep.add("目标会话采集", FAIL, f"没有这个 workspace/project：{workspace}/{project}")
        rep.add("存档页", INCOMPLETE, "scope 不存在")
        conn.close()
        return
    ws_id, pj_id = ids

    try:
        # 1) 会话必须存在，且 agent_kind 必须与请求一致
        row = conn.execute(
            """SELECT s.agent_kind, s.ended_at FROM sessions s
               JOIN projects p ON s.project_id = p.id
               JOIN workspaces w ON s.workspace_id = w.id
               WHERE w.name = ? AND p.name = ? AND s.id = ?""",
            (workspace, project, uuid.UUID(canon).bytes),
        ).fetchone()
        if row is None:
            rep.add("目标会话采集", FAIL, f"数据库里没有这个会话：{label}")
            rep.add("存档页", INCOMPLETE, "会话不存在，无法核对")
            return
        agent_kind, _ended_at = row
        if agent_kind != agent:
            rep.add("目标会话采集", FAIL,
                    f"该会话的 agent_kind 是 {agent_kind!r}，与请求的 {agent!r} 不符：{label}")
            rep.add("存档页", INCOMPLETE, "agent 不符，无法核对")
            return

        # 2) 观测必须同时属于该 session **和** 该 workspace/project
        kinds = dict(conn.execute(
            """SELECT kind, COUNT(*) FROM observations
               WHERE session_id = ? AND workspace_id = ? AND project_id = ?
               GROUP BY kind""",
            (uuid.UUID(canon).bytes, ws_id, pj_id),
        ).fetchall())
        if not kinds:
            rep.add("目标会话采集", FAIL,
                    f"该会话在此 scope 下零观测（观测可能落在别的 scope）：{label}")
        else:
            need = ["user-prompt", "pre-tool-use", "post-tool-use", "stop"]
            absent = [k for k in need if not kinds.get(k)]
            summary = "、".join(f"{k}={v}" for k, v in sorted(kinds.items()))
            if absent:
                rep.add("目标会话采集", FAIL,
                        f"缺 {'、'.join(absent)}；实际 {summary}")
            else:
                rep.add("目标会话采集", PASS, summary)

        # 3) 该会话自己的最新 Stop 时间（用同 scope 约束）
        last_stop = conn.execute(
            """SELECT MAX(created_at) FROM observations
               WHERE session_id = ? AND workspace_id = ? AND project_id = ?
                 AND kind = 'stop'""",
            (uuid.UUID(canon).bytes, ws_id, pj_id),
        ).fetchone()[0]

        # 3b) 该会话最后一条 raw 观测（任何 kind）。额度中断时这一轮没有
        #     Stop，但观测仍在写，所以它是判断「存档是否漏了最新进度」的唯一依据。
        last_obs = conn.execute(
            """SELECT MAX(created_at) FROM observations
               WHERE session_id = ? AND workspace_id = ? AND project_id = ?""",
            (uuid.UUID(canon).bytes, ws_id, pj_id),
        ).fetchone()[0]

        # 4) 存档页：path 用裸 UUID，保持与现有命名兼容
        bare = native_id[len("session-"):] if native_id.startswith("session-") else native_id
        page_path = f"_autosave/{agent}-{bare}.md"
        prow = conn.execute(
            """SELECT pg.body, pg.created_at FROM pages pg
               JOIN projects p ON pg.project_id = p.id
               JOIN workspaces w ON pg.workspace_id = w.id
               WHERE w.name = ? AND p.name = ? AND pg.path = ? AND pg.is_latest = 1""",
            (workspace, project, page_path),
        ).fetchone()
        if prow is None:
            rep.add("存档页", FAIL, f"没有 latest 存档页：{page_path}")
            return
        body, created = prow
        if not body or not str(body).strip():
            rep.add("存档页", FAIL, f"存档正文为空：{page_path}")
            return

        # 5) 存档正文必须确实属于这个目标
        text = str(body)
        if canon not in text and native_id not in text:
            rep.add("存档页", FAIL,
                    f"存档正文既没有 native id 也没有 canonical id，不属于该目标：{page_path}")
            return

        # 6) 有已完成 Stop 时，快照不得早于它（否则上一轮漏存档）
        #
        # 同一轮内 Stop hook 先 POST 观测、再写存档页，所以页面的 created_at
        # 会**略早于**它自己那一轮的 stop 观测（实测 5–15ms，始终同秒）。
        # 这是正常顺序，不是漏存档。只有落后到**更早的秒**才算真的漏了。
        if last_stop is None:
            rep.add("存档页", INCOMPLETE,
                    f"该会话没有已完成的 Stop 记录，无法确认存档覆盖了完成轮次：{page_path}")
        elif created is not None and created < last_stop:
            # 微秒时间戳：同秒内（差值 < 1_000_000）视为同一轮的正常先后
            if (last_stop - created) < 1_000_000:
                rep.add("存档页", PASS,
                        f"{page_path}（与最新 Stop 同秒，属同一轮的正常先后）")
            else:
                rep.add("存档页", FAIL,
                        f"存档早于最新 Stop（存档 {created} < Stop {last_stop}）："
                        "上一轮可能漏存档")
        else:
            rep.add("存档页", PASS, page_path)

        # 7) 存档不得明显落后于最后 raw 观测。
        #    这一项专抓「额度/崩溃导致轮次没跑完、Stop 没触发」的情况：
        #    此时 Stop 检查会通过（上一轮是完整的），但存档已经漏掉最新进度。
        #    容忍 90 秒，避免把正常写入延迟误判成漏存档。
        STALE_US = 90 * 1_000_000
        if last_obs is not None and created is not None and created < last_obs:
            lag = last_obs - created
            if lag > STALE_US:
                rep.add("存档新鲜度", INCOMPLETE,
                        f"存档比最后 raw 观测落后 {lag // 1_000_000} 秒"
                        f"（存档 {created} < 观测 {last_obs}）——"
                        "可能是未完成轮次（额度中断/崩溃），该轮不会有 Stop，"
                        "接手方须读 raw 观测而不是只信存档")
            else:
                rep.add("存档新鲜度", PASS, f"落后 {lag // 1000}ms，在容忍范围内")
        else:
            rep.add("存档新鲜度", PASS, "存档不早于最后 raw 观测")
    except sqlite3.Error as exc:
        rep.add("目标会话采集", INCOMPLETE, f"SQL 失败（{exc}）")
        rep.add("存档页", INCOMPLETE, "SQL 失败")
    finally:
        conn.close()


def _parse_ps_time(text: str):
    import datetime as _dt
    for fmt in ("%a %b %d %H:%M:%S %Y", "%b %d %H:%M:%S %Y"):
        try:
            return _dt.datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


def check_process(rep: Report, hooks_json: Path, agent: str | None = None) -> None:
    """Codex app-server 是否在用旧配置。只报告，不重启。

    * 多个候选时**不挑第一个**——无法确定哪个是目标 GUI，判 incomplete
      并列出候选 PID（挑第一个会假绿）。
    * 只验 DSH 目标时，本项是**参考信息、不计入验收**：Codex GUI 关着
      不该把 DSH 的验收拖成失败。
    """
    check_name = "Codex 进程"
    informational = agent == "dsh"

    def emit(status: str, detail: str) -> None:
        if informational:
            # 只验 DSH 时降级为参考信息，用 pass 承载但明确标注不计入验收
            rep.add(check_name, PASS, f"[参考信息·不计入验收] {detail}")
        else:
            rep.add(check_name, status, detail)

    try:
        pid = subprocess.run(["pgrep", "-f", "codex app-server"],
                             capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        emit(INCOMPLETE, "无法查询 app-server 进程；不代表配置已生效")
        return

    if not pid:
        emit(INCOMPLETE, "没有找到 app-server 进程（Codex GUI 未运行）；不代表配置已生效")
        return

    if len(pid) > 1:
        emit(INCOMPLETE,
             f"发现 {len(pid)} 个 app-server 候选（PID {'、'.join(pid)}），"
             "无法确定哪个是目标 GUI，不判定生效状态")
        return

    try:
        started_raw = subprocess.run(["ps", "-o", "lstart=", "-p", pid[0]],
                                     capture_output=True, text=True,
                                     timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        emit(INCOMPLETE, "无法读取进程启动时间")
        return
    if not hooks_json.is_file():
        emit(INCOMPLETE, "没有 hooks.json 可比对")
        return
    try:
        hmtime = subprocess.run(["stat", "-f", "%Sm", str(hooks_json)],
                                capture_output=True, text=True,
                                timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        emit(INCOMPLETE, "无法读取 hooks.json 修改时间")
        return

    detail = f"app-server(PID {pid[0]}) 启动 {started_raw}；hooks.json 修改 {hmtime}"
    s_dt, h_dt = _parse_ps_time(started_raw), _parse_ps_time(hmtime)
    if s_dt is None or h_dt is None:
        emit(INCOMPLETE, f"{detail} —— 时间格式无法解析（可能受 locale 影响）")
        return
    if s_dt < h_dt:
        emit(INCOMPLETE, f"{detail} —— 启动早于修改，钩子改动尚未生效（需重启 Codex App）")
    else:
        emit(PASS, detail)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ai-memory 交接/自动存档 验收（只读）")
    ap.add_argument("project_dir", nargs="?", help="项目目录（原样保留）")
    ap.add_argument("--agent", choices=("codex", "dsh"), help="目标 agent")
    ap.add_argument("--session", help="完整 native session id")
    ap.add_argument("--workspace")
    ap.add_argument("--project")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--db", default=str(DB_PATH), help="数据库路径（fixture 用）")
    ap.add_argument("--hooks-dir", default=str(DSH_HOOKS.parent),
                    help="DSH hooks 目录（fixture 用）")
    ap.add_argument("--server", default=SERVER)
    ap.add_argument("--skip-network", action="store_true",
                    help="跳过网络/进程检查（fixture 用）")
    args = ap.parse_args()

    rep = Report()
    dsh_hooks = Path(args.hooks_dir) / "hooks.json"

    if not args.skip_network:
        check_service(rep, args.server)
    check_dsh_hooks(rep, dsh_hooks)
    if not args.skip_network:
        check_codex_hooks(rep, CODEX_HOOKS)

    # 5) 只给一半 scope 是参数错误，不能静默用目录推导覆盖用户给的值
    if bool(args.workspace) != bool(args.project):
        ap.error("--workspace 与 --project 必须成对给出（只给一个会产生歧义）")

    # scope 推导
    ws, proj = args.workspace, args.project
    if not (ws and proj):
        if not args.project_dir:
            rep.add("scope", INCOMPLETE,
                    "未给 --workspace/--project，也没给项目目录，无法推导")
            ws = proj = None
        else:
            try:
                resolve = load_resolve_project()
                ws, proj = resolve(args.project_dir)
            except Exception as exc:  # noqa: BLE001
                rep.add("scope", INCOMPLETE, f"推导失败（{type(exc).__name__}）")
                ws = proj = None
            else:
                rep.add("scope", PASS, f"{ws}/{proj}")
    else:
        rep.add("scope", PASS, f"{ws}/{proj}")

    if not args.session or not args.agent:
        rep.add("目标会话采集", INCOMPLETE,
                "未指定 --agent 与 --session：本报告仅为概览，未完整验证指定任务")
        rep.add("存档页", INCOMPLETE, "同上")
    elif ws and proj:
        check_target(rep, Path(args.db), args.agent, args.session, ws, proj)
    else:
        rep.add("目标会话采集", INCOMPLETE, "scope 未知，无法按会话核对")
        rep.add("存档页", INCOMPLETE, "scope 未知")

    if not args.skip_network:
        check_process(rep, CODEX_HOOKS, args.agent)

    # --skip-network 只是数据/局部验证，不能当作完整 live 验收
    complete = rep.ok and not args.skip_network
    if args.json:
        print(json.dumps({
            "summary": {s: rep.count(s) for s in (PASS, FAIL, INCOMPLETE)},
            "complete": complete,
            "live": not args.skip_network,
            "note": ("离线/局部验证：--skip-network 跳过了服务与进程检查，"
                     "不代表完整 live 验收") if args.skip_network else "",
            "items": rep.items,
        }, ensure_ascii=False, indent=2))
    else:
        icon = {PASS: "✓", FAIL: "✗", INCOMPLETE: "?"}
        for item in rep.items:
            print(f"  {icon[item['status']]} [{item['status']}] {item['check']}"
                  + (f"\n      {item['detail']}" if item["detail"] else ""))
        print(f"\n结果: {rep.count(PASS)} pass / {rep.count(FAIL)} fail / "
              f"{rep.count(INCOMPLETE)} incomplete")
        if args.skip_network:
            print("注意：--skip-network 为离线/局部验证，不代表完整 live 验收")
        if not complete:
            print("未完整通过：退出码非零（incomplete 不等于通过）")

    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
