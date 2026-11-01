#!/usr/bin/env python3
"""Codex 阶段存档（autosave）— 从 Codex rollout 日志机械提取状态，覆盖写入 ai-memory 页面。

和 `dsh-checkpoint.py` 同一套思路，只是数据源换成 Codex 的
`~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session-id>.jsonl`。

* 不调用模型：纯解析 JSONL，零 token。
* 覆盖写入同一个 path：ai-memory 的 write-page 是新版本语义，path 不变，
  搜索/读取只返回最新版，等价于编辑器自动保存。
* 显式传 workspace/project：MCP 是静态 client，省略 scope 会用到服务器
  自己的 ActiveProject 槽位（可能属于别的项目）。

用法
----
    python3 codex-checkpoint.py                      # 自动挑最新 rollout
    python3 codex-checkpoint.py --session <id>
    python3 codex-checkpoint.py --cwd <dir>
    python3 codex-checkpoint.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
DEFAULT_SERVER = os.environ.get("AI_MEMORY_HOOK_URL", "http://127.0.0.1:49374")
PAGE_TEMPLATE = "_autosave/codex-{session}.md"


def iter_rollouts() -> list[Path]:
    return sorted(
        CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def read_jsonl(path: Path) -> list[dict]:
    out = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def pick_rollout(cwd: str | None, session: str | None) -> Path:
    """挑一个 rollout。

    `session` 优先按 rollout 文件名或 session_meta 里的 id/session_id 精确匹配。
    `cwd` 只接受**精确匹配**的 rollout —— 匹配不到就报错，绝不退回全局最新，
    否则会把别的项目的会话写到当前项目下（实测踩过）。
    """
    candidates = iter_rollouts()
    if not candidates:
        sys.exit(f"没有找到任何 rollout：{CODEX_SESSIONS}")

    if session:
        # 文件名里带（rollout 自己的 id）
        hits = [p for p in candidates if session in p.name]
        if hits:
            return hits[0]
        # 文件名不带时，退一步查 session_meta。
        # **id 优先**：payload.id 是该 thread 自己的原生 id；
        # session_id 是主会话关联 id，子智能体也沿用，拿它匹配会误选关联子 log。
        # 所以先只按 id 精确匹配。
        for p in candidates:
            meta = first_meta(p) or {}
            if meta.get("id") == session:
                return p
        # session_id 只作**兼容回退**：仅当该 log 自己**没有 id** 时才允许用它匹配。
        # 若 log 有 id 却不是目标，说明它是别的 thread（例如关联的子智能体），
        # 不能借来冒充目标——否则主 log 缺失时会静默选中子 log。
        for p in candidates:
            meta = first_meta(p) or {}
            if not meta.get("id") and meta.get("session_id") == session:
                return p
        sys.exit(f"找不到含 {session} 的 rollout（文件名和 session_meta 都不匹配）")

    if cwd:
        want = str(Path(cwd).resolve()).rstrip("/")
        for p in candidates:
            meta = first_meta(p)
            if meta and str(meta.get("cwd", "")).rstrip("/") == want:
                return p
        sys.exit(f"{want} 下没有 rollout（不退回全局最新，避免写错项目）")

    return candidates[0]


def first_meta(path: Path) -> dict | None:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "session_meta":
                return d.get("payload") or {}
    return None


def clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


INJECTED_PREFIXES = (
    # 只列已知注入前缀。不能一刀切所有 `<`：用户会直接贴 HTML/XML 组件
    # （例如 `<div>请修改这个HTML组件</div>`），那是真实任务输入。
    "<user_instructions",
    "<external_",
    "<INSTRUCTIONS>",
    "<environment_context",
    "<user_shell_command",
    "<turn_aborted",
    "<permissions instructions",
    "# AGENTS.md instructions",
    "# CLAUDE.md instructions",
    "AGENTS.md instructions",
    "The following is the Codex agent history",
)


def is_injected(text: str) -> bool:
    """Codex 把自己的注入（AGENTS.md、环境说明、<external_...>）也写成 user 消息。

    只按**已知注入前缀**过滤；普通 HTML/XML 文本仍算用户输入。
    """
    t = text.lstrip()
    return any(t.startswith(p) for p in INJECTED_PREFIXES)


TASK_LIMIT = 8


def render_tasks(prompts: list[str], lines: list[str]) -> None:
    """任务区：首条目标单独保留 + 最近 N 条 + 省略提示。"""
    if prompts:
        first = prompts[0]
        rest = prompts[1:]
        keep = TASK_LIMIT - 1
        shown = rest[-keep:] if len(rest) > keep else rest
        omitted = len(rest) - len(shown)
        lines.append(f"- 【初始目标】{clip(first, 400)}")
        if omitted > 0:
            lines.append(f"- _（中间 {omitted} 条已省略）_")
        for p in shown:
            lines.append(f"- {clip(p, 400)}")
    else:
        lines.append("_（本次会话还没有人类输入）_")
    lines.append("")


def _command_status(item: dict) -> str:
    """命令成败标注：只有明确的 status/exit_code 才报成功/失败。"""
    status = item.get("status")
    code = item.get("exit_code")
    if isinstance(code, str) and code.lstrip("-").isdigit():
        code = int(code)
    if not isinstance(code, int):
        code = None
    st = str(status).lower() if status is not None else ""
    if st in ("completed", "success", "succeeded", "ok"):
        if code is None:
            return "完成（未报告 exit code，状态未知）"
        return "成功（exit 0）" if code == 0 else f"失败（exit {code}）"
    if st in ("failed", "failure", "error", "errored"):
        return f"失败（exit {code}）" if code is not None else "失败（status=Failed）"
    if st in ("running", "in_progress", "pending"):
        return f"未完成（status={status}）"
    if code is not None:
        return "成功（exit 0）" if code == 0 else f"失败（exit {code}）"
    return "状态未知（没有 exit_code / status）"


def build(events: list[dict], session_id: str, cwd: str) -> str:
    prompts: list[str] = []
    files_changed: dict[str, str] = {}
    failed_ops: list[str] = []
    unknown_ops: list[str] = []
    commands: list[tuple[str, str]] = []  # (命令, 状态标注)
    tool_counts: dict[str, int] = {}
    last_agent = ""
    turns = 0

    for ev in events:
        t = ev.get("type")
        payload = ev.get("payload") or {}

        if t == "response_item":
            role = payload.get("role")
            if role == "user":
                txt = " ".join(
                    c.get("text", "")
                    for c in (payload.get("content") or [])
                    if isinstance(c, dict) and c.get("type") in ("input_text", "text")
                ).strip()
                if txt and not is_injected(txt) and (not prompts or prompts[-1] != txt):
                    prompts.append(txt)
            elif role == "assistant":
                txt = " ".join(
                    c.get("text", "")
                    for c in (payload.get("content") or [])
                    if isinstance(c, dict) and c.get("type") in ("output_text", "text")
                ).strip()
                if txt:
                    last_agent = txt
            continue

        if t != "event_msg":
            continue

        sub = payload.get("type")

        if sub == "task_complete":
            turns += 1
            msg = (payload.get("last_agent_message") or "").strip()
            if msg:
                last_agent = msg
            continue

        if sub == "item_completed":
            item = payload.get("item") or {}
            it = item.get("type")
            if it:
                tool_counts[it] = tool_counts.get(it, 0) + 1

            if it == "CommandExecution":
                cmd = item.get("command")
                if isinstance(cmd, list):
                    cmd = " ".join(str(x) for x in cmd)
                if cmd:
                    commands.append((str(cmd), _command_status(item)))
            elif it == "FileChange":
                # item.status 决定这条改动能不能算"已确认"：
                # 只有 Completed 才算；Failed 进失败区；缺失/未知进状态未知区。
                # 大小写不敏感：真实 rollout 写的是小写 `completed`，早期格式
                # /测试 fixture 写的是 `Completed`，两者必须等价。
                status = item.get("status")
                changes = item.get("changes") or {}
                norm = str(status).strip().lower() if status is not None else ""
                if norm == "completed":
                    for path, change in changes.items():
                        kind = (change or {}).get("type", "?")
                        files_changed[path] = kind
                elif norm in ("failed", "failure", "error", "errored"):
                    for path in changes:
                        failed_ops.append(f"- `{path}`　（FileChange status=Failed）")
                else:
                    for path in changes:
                        shown = status if status is not None else "缺失"
                        unknown_ops.append(f"- `{path}`　（FileChange status={shown}，未确认）")
            elif it == "McpToolCall":
                name = item.get("name") or item.get("tool") or ""
                if name:
                    tool_counts[f"mcp:{name}"] = tool_counts.get(f"mcp:{name}", 0) + 1
            continue

        # Codex 也会把失败/未完成的条目单独报出来（item_failed / item_updated）
        if sub in ("item_failed", "item_updated"):
            item = payload.get("item") or {}
            if item.get("type") == "FileChange":
                status = item.get("status")
                norm = str(status).strip().lower() if status is not None else ""
                for path in (item.get("changes") or {}):
                    if sub == "item_failed" or norm in ("failed", "failure", "error", "errored"):
                        failed_ops.append(f"- `{path}`　（FileChange status=Failed）")
                    else:
                        shown = status if status is not None else "缺失"
                        unknown_ops.append(f"- `{path}`　（FileChange status={shown}，未确认）")
            continue

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    lines = [
        f"# Codex 阶段存档 · {Path(cwd).name}",
        "",
        "> 自动生成，每轮覆盖。不是最终交接，只是「我停在这里」的快照。",
        f"> 更新：{now}　会话：`{session_id}`　完成轮次：{turns}　目录：`{cwd}`",
        "",
        "## 任务（人类原话，按时间顺序）",
        "",
    ]
    render_tasks(prompts, lines)

    lines += ["## 改过的文件（已确认成功）", ""]
    if files_changed:
        mark = {"add": "新增", "update": "修改", "delete": "删除", "edit": "修改"}
        for p, kind in list(files_changed.items())[-25:]:
            lines.append(f"- `{p}`　{mark.get(kind, kind)}")
        lines.append("")
        lines.append("_（只统计 status=Completed 的改动；是否已提交需自行核对。）_")
    else:
        lines.append("_（没有确认成功的文件改动）_")
    lines.append("")

    if failed_ops:
        lines += ["## 失败的操作", ""] + failed_ops[:20] + [""]

    if unknown_ops:
        lines += ["## 状态未知的操作", ""] + unknown_ops[:20] + [""]

    if commands:
        lines += [
            "## 最近的命令",
            "",
            "> 调用记录，不代表执行成功。状态只在有明确 exit_code / status 时标注，",
            "> 否则写「状态未知」。",
            "",
        ]
        for cmd, status in commands[-6:]:
            lines.append(f"- `{clip(cmd, 200)}`")
            lines.append(f"  - 状态：{status}")
        lines.append("")

    if last_agent:
        lines += ["## 最后一条助手输出", "", clip(last_agent, 1200), ""]

    lines += [
        "## 工具用量",
        "",
        ", ".join(f"{k}×{v}" for k, v in sorted(tool_counts.items(), key=lambda kv: -kv[1])[:10])
        or "_（无）_",
        "",
        "## 接手方式",
        "",
        "下一个 agent（DSH 或新的 Codex 会话）：",
        "",
        "1. 读这一页，把「任务」里的【初始目标】和最近几条当作要继续的目标；",
        "2. 「改过的文件」逐个看当前内容，别假设已提交；",
        "3. 「失败的操作」「状态未知的操作」不要当成已完成；",
        "4. 需要完整原始过程时，读 Codex rollout：",
        f"   `~/.codex/sessions/**/rollout-*-{session_id}.jsonl`",
        "",
    ]
    return "\n".join(lines)


def _strip_port(host: str) -> str:
    """URL 形态下 `:` 只可能是端口；scp 形态里它分隔 host 和 path。"""
    idx = host.rfind(":")
    if idx >= 0 and host[idx + 1:].isdigit() and host[idx + 1:]:
        return host[:idx]
    return host


def _tidy(s: str) -> str:
    """转小写、去尾 `.git`、折叠斜线噪音 —— 逐条对齐 canonical `tidy()`。"""
    out = s.lower()
    while out.endswith("/"):
        out = out[:-1]
    if out.endswith(".git"):
        out = out[: -len(".git")]
    while out.endswith("/"):
        out = out[:-1]
    while "//" in out:
        out = out.replace("//", "/")
    return out


def normalize_remote(raw: str) -> str | None:
    """把 git remote URL 归一成 `host/owner/repo`，或 None（非网络可达仓库）。

    逐行对齐 `crates/ai-memory-core/src/repository_identity.rs::normalize_remote_url`。
    以下都产出 `github.com/acme/api`：
        git@github.com:Acme/API.git
        https://github.com/acme/api/
        https://user@github.com:443/Acme/API
        ssh://git@github.com:22/Acme/API.git
        git://github.com/acme/api
    以下产出 None（是文件系统路径）：
        /srv/git/api.git      ../sibling      file:///srv/git/api.git      C:\repos\api
    """
    raw = raw.strip()
    if not raw:
        return None

    # 拆 scheme；有无 scheme 决定了后面 `:` 怎么读
    idx = raw.find("://")
    if idx >= 0:
        scheme = raw[:idx].lower()
        rest = raw[idx + 3:]
    else:
        scheme = None
        rest = raw

    if scheme == "file":
        return None

    # 凭据：剥到路径前最后一个 `@`（密码里可能含 `@`）
    path_start = rest.find("/")
    if path_start < 0:
        path_start = len(rest)
    at = rest[:path_start].rfind("@")
    host_and_path = rest[at + 1:] if at >= 0 else rest

    if scheme is not None:
        host, _, path = host_and_path.partition("/")
        host = _strip_port(host)
        if not host or not path:
            return None
        normalized = f"{host}/{path}"
    else:
        # 无 scheme：scp 形态 `host:path` 或文件系统路径。
        # `:` 出现在任何 `/` 之前才算是 scp。
        colon = host_and_path.find(":")
        if colon < 0:
            return None
        slash = host_and_path.find("/")
        if slash >= 0 and slash < colon:
            return None
        host = host_and_path[:colon]
        path = host_and_path[colon + 1:]
        # 单字符 host 是 Windows 盘符，不是主机名
        if len(host) <= 1 or not path:
            return None
        if path.startswith("\\") or "\\" in path:
            return None
        normalized = f"{host}/{path}"

    identity = _tidy(normalized)
    if not identity or "/" not in identity:
        return None
    return identity


def project_name_from(raw: str) -> str:
    """逐字符对齐 ai-memory 的 `project_name_from`（repository_identity.rs）。

    保留字母数字和 `-` `_` `.`，其余变 `-`；连续 `-` 折叠；去首尾 `-`；
    空则 `repository`。**不改大小写**（大小写已在 normalize_remote 里折叠过）。
    """
    out: list[str] = []
    for ch in raw:
        c = ch if (ch.isalnum() or ch in "-_.") else "-"
        if c == "-" and out and out[-1] == "-":
            continue
        out.append(c)
    name = "".join(out).strip("-")
    return name or "repository"


def resolve_project(cwd: str) -> tuple[str, str]:
    """推导 (workspace, project)，严格对齐 ai-memory 的 canonical 规则。

    优先级（与 `resolve_project_name` / `discover_remote_identity` 一致）：
      1. 最近的 `.ai-memory.toml` 里显式声明的 workspace/project
      2. git remote —— **upstream 优先，其次 origin**；取去掉 host 后的
         **完整仓库路径**，`Acme/Group/API` -> `acme-group-api`
         （不是只取最后两段）
      3. `basename(cwd)`

    必须显式传：MCP 是静态 client，不带会话身份，省略 scope 时服务器会用
    它自己的 ActiveProject 槽位 —— 那个槽位属于谁是不确定的。
    """
    d = Path(cwd).resolve()

    for parent in [d, *d.parents]:
        marker = parent / ".ai-memory.toml"
        if not marker.is_file():
            continue
        ws = proj = None
        for line in marker.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("\"'")
            if k == "workspace" and v:
                ws = v
            elif k == "project" and v:
                proj = v
        if proj:
            return ws or "default", proj
        break

    # upstream 优先，其次 origin
    for remote_name in ("upstream", "origin"):
        try:
            url = subprocess.run(
                ["git", "-C", str(d), "remote", "get-url", remote_name],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            url = ""
        identity = normalize_remote(url) if url else None
        if identity:
            _, _, path = identity.partition("/")
            if path:
                return "default", project_name_from(path)

    return "default", d.name


def load_token() -> str:
    tok = os.environ.get("AI_MEMORY_AUTH_TOKEN")
    if tok:
        return tok
    data_dir = Path(
        os.environ.get("AI_MEMORY_DATA_DIR")
        or (Path.home() / "Library/Application Support/ai-memory")
    )
    for name in ("auth-header", "auth-token"):
        f = data_dir / name
        if not f.is_file():
            continue
        line = f.read_text().strip()
        if line.lower().startswith("authorization:"):
            line = line.split(":", 1)[1].strip()
        if line.lower().startswith("bearer "):
            line = line[7:].strip()
        if line:
            return line
    sys.exit(f"找不到 ai-memory token（设 AI_MEMORY_AUTH_TOKEN 或检查 {data_dir}/auth-header）")


def mcp_write(server: str, token: str, path: str, body: str, ws: str, proj: str) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "memory_write_page",
            "arguments": {"path": path, "body": body, "workspace": ws, "project": proj},
        },
    }
    req = urllib.request.Request(
        f"{server}/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MCP 响应不是合法 JSON（{exc.msg}）") from None

    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        code = err.get("code") if isinstance(err, dict) else None
        raise RuntimeError(f"MCP JSON-RPC 错误（code={code}）")

    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("MCP 响应缺少 result")
    if result.get("isError") is True:
        raise RuntimeError("MCP 工具返回 isError=true，写入失败")

    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise RuntimeError("MCP 响应缺少 content")
    text = content[0].get("text") if isinstance(content[0], dict) else None
    if not isinstance(text, str):
        raise RuntimeError("MCP 响应 content[0].text 不是字符串")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError("MCP content[0].text 不是合法 JSON") from None
    if not isinstance(parsed, dict) or not parsed.get("page_id"):
        raise RuntimeError("MCP 写入结果缺少有效 page_id")
    return parsed


def main() -> None:
    ap = argparse.ArgumentParser(description="Codex 阶段存档（覆盖写入 ai-memory 页面）")
    ap.add_argument("--session", help="会话 id 片段")
    ap.add_argument("--cwd", help="按项目目录匹配 rollout")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--path", help="覆盖写入的页面路径，默认 _autosave/codex-<session>.md")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写")
    ap.add_argument("--quiet", action="store_true", help="成功时不打印（hook 里用）")
    args = ap.parse_args()

    rollout = pick_rollout(args.cwd, args.session)
    meta = first_meta(rollout) or {}
    session_id = meta.get("id") or meta.get("session_id") or rollout.stem
    cwd = meta.get("cwd") or args.cwd or os.getcwd()

    body = build(read_jsonl(rollout), session_id, cwd)
    path = args.path or PAGE_TEMPLATE.format(session=session_id)

    if args.dry_run:
        print(body)
        return

    ws, proj = resolve_project(cwd)
    result = mcp_write(args.server, load_token(), path, body, ws, proj)
    if not args.quiet:
        print(f"✓ {ws}/{proj}:{path}  page_id={result.get('page_id')}")


if __name__ == "__main__":
    main()
