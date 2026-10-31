#!/usr/bin/env python3
"""DSH 阶段存档（autosave）— 从 DSH 会话日志机械提取状态，覆盖写入 ai-memory 页面。

设计要点
--------
* 不调用模型：全部内容来自 `~/.dsh/sessions/<cwd-slug>/<session-id>/session.v4.jsonl.zstd`
  的事件日志，纯解析，零 token。
* 覆盖写入同一个 path：ai-memory 的 write-page 是"新版本"语义，path 不变，
  搜索/读取只返回最新版，等价于编辑器自动保存。
* 走 MCP HTTP 而不是 `ai-memory write-page` CLI：DSH 的 hook 在沙箱里运行，
  写 `~/Library/Application Support/ai-memory` 会被拒绝（原生二进制会先落 spool，
  于是整条事件被丢弃）。MCP 是纯 HTTP，不受文件沙箱限制。

用法
----
    python3 dsh-checkpoint.py --session <id>  # 指定会话（推荐，hook 走这条）
    python3 dsh-checkpoint.py --cwd <dir>     # 仅当该 cwd 下恰好只有一个会话
    python3 dsh-checkpoint.py --dry-run       # 只打印，不写页面

会话定位不再按 mtime 猜：给了 `--session` 就按 id 在 SESSIONS_ROOT 下单层
glob 精确匹配并核对 header 的 cwd；没给就只接受"该 cwd 下唯一会话"，多个
必须报错。目录名编码（中文/空格/emoji → `~XXXX`）不参与定位。
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import uuid
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_ROOT = Path.home() / ".dsh" / "sessions"
DEFAULT_SERVER = os.environ.get("AI_MEMORY_HOOK_URL", "http://127.0.0.1:49374")
PAGE_TEMPLATE = "_autosave/dsh-{session}.md"
SESSION_LOG = "session.v4.jsonl.zstd"

# 写文件的工具名 → 从 arguments 里取路径的字段（DSH 用 file_path）
FILE_TOOLS = {"write": "file_path", "edit": "file_path", "read": "file_path",
              "notebook_edit": "notebook_path", "multi_edit": "file_path"}
# 只有这些才是"改文件"；read 只读不算
WRITE_TOOLS = {"write", "edit", "multi_edit", "notebook_edit"}

# 真实人类任务输入。user-rpc 是 RPC 提交的用户输入，和 user 等价。
USER_TASK_KINDS = {"user", "user-rpc"}
# 审批/补充：保留但单独标记，不能进"任务"区、不能当初始目标。
USER_APPROVAL_KINDS = {"user-approval"}
# 其余 source.kind（runtime-context / skill-catalog / time-context /
# agent-instructions / hooks-claude-code / compact-checkpoint / dsh-mnemon /
# model-selection …）一律排除，不当人类目标。

TASK_LIMIT = 8


def cwd_slug(cwd: str) -> str:
    """DSH 把 cwd 编码成目录名（仅供展示兜底，不再用于定位会话）。

    真实编码还要把非 ASCII 字符转成 `~XXXX`（UTF-16 code unit），这里没有
    复刻；定位会话一律按 session id 或 header 里的 cwd 精确比对。
    """
    return "--" + cwd.strip("/").replace("/", "-") + "--"


def _decompress(path: Path) -> bytes:
    """读会话日志：真实文件是 zstd，测试 fixture 可能是 gzip 或明文。"""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        return subprocess.run(
            ["zstd", "-dc", str(path)], capture_output=True, check=True
        ).stdout
    return raw


def session_header_cwd(session_dir: Path) -> str | None:
    """读会话 header 里的 cwd（header 是日志第一行，type == "session"）。"""
    f = session_dir / SESSION_LOG
    if not f.is_file():
        return None
    try:
        raw = _decompress(f)
    except (OSError, subprocess.SubprocessError, EOFError, gzip.BadGzipFile):
        return None
    for line in raw.decode("utf-8", "replace").splitlines()[:20]:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "session":
            cwd = ev.get("cwd")
            return str(cwd) if cwd else None
    return None


def _same_path(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a.rstrip("/") == b.rstrip("/")


def _all_session_dirs() -> list[Path]:
    """SESSIONS_ROOT 下所有 `<slug>/<session-dir>` 形态的会话目录。"""
    if not SESSIONS_ROOT.is_dir():
        return []
    out: list[Path] = []
    for parent in sorted(SESSIONS_ROOT.iterdir()):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_dir() and (child / SESSION_LOG).is_file():
                out.append(child)
    return out


_BAD_SESSION_CHARS = set("*?[]{}") | {"/", "\\"}


def _validate_session_arg(session: str) -> None:
    """拒绝 glob 元字符、路径分隔符和 .. 遍历。

    `find_session` 会把 session 拼进目录名，任何元字符都可能让它匹配到
    并非用户指定的目录（实测：只有一个候选时 `*` 会被选中）。
    """
    if not session:
        sys.exit("session id 为空")
    if any(ch in _BAD_SESSION_CHARS for ch in session):
        sys.exit(f"session id 含非法字符（glob 元字符或路径分隔符）：{session!r}")
    if ".." in session:
        sys.exit(f"session id 含路径遍历：{session!r}")


def _read_header(session_dir: Path) -> dict | None:
    """只读会话 header（日志第一行），不解压完整正文。

    header 是 `{"type":"session","id":...,"cwd":...}`。zstd 流需要从头上
    解压到第一行为止，这里限制读取量，避免为找一个 header 解压无关会话的
    全部内容。
    """
    f = session_dir / SESSION_LOG
    if not f.is_file():
        return None
    try:
        if f.read_bytes()[:2] == b"\x1f\x8b":
            with gzip.open(f, "rt", encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
        elif f.read_bytes()[:4] == b"\x28\xb5\x2f\xfd":
            # zstd：-dc 到 stdout 后只取第一行即退出（head 会提前关闭管道）
            proc = subprocess.run(
                f"zstd -dc {shlex.quote(str(f))} 2>/dev/null | head -n 1",
                shell=True, capture_output=True, text=True,
            )
            first = proc.stdout
        else:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
    except (OSError, subprocess.SubprocessError, EOFError, gzip.BadGzipFile):
        return None
    first = (first or "").strip()
    if not first:
        return None
    try:
        ev = json.loads(first)
    except json.JSONDecodeError:
        return None
    return ev if isinstance(ev, dict) else None


def _header_identity(session_dir: Path) -> tuple[str | None, str | None]:
    """返回 (header 里的原生 session id, header 里的 cwd)。"""
    ev = _read_header(session_dir)
    if not ev or ev.get("type") != "session":
        return None, None
    sid = ev.get("id")
    cwd = ev.get("cwd")
    return (str(sid) if sid else None), (str(cwd) if cwd else None)


def _norm_session_id(raw: str) -> str:
    """把用户输入归一到带 `session-` 前缀的原生 id 形态用于比较。"""
    return raw if raw.startswith("session-") else f"session-{raw}"


def find_session(cwd: str, session: str | None) -> Path:
    """定位会话目录。

    * 给了 `session`：先拒绝 glob 元字符/路径分隔符/`..`，再在 SESSIONS_ROOT
      下**单层 glob** 按 id 精确匹配；匹配到的目录**必须**有合法 session
      header，且 header 的原生 id 与请求一致、cwd 与请求一致。缺 header、
      header 不符、多个候选一律 sys.exit，绝不退回别的会话。
    * 没给 `session`：只允许"该 cwd 下恰好只有一个会话"。多于一个必须报错
      并提示用 `--session`。**禁止按 mtime 猜测**。

    目录名编码（中文/空格/emoji 会变成 `~XXXX`）不参与定位，所以不依赖
    复刻 DSH 的 projectKey。
    """
    if session:
        _validate_session_arg(session)
        want = _norm_session_id(session)
        bare = want[len("session-"):]
        cands: list[Path] = []
        for pattern in (f"*/session-{bare}", f"*/{bare}"):
            for p in SESSIONS_ROOT.glob(pattern):
                if p.is_dir() and p not in cands:
                    cands.append(p)
        if not cands:
            sys.exit(
                f"找不到会话 {session}（在 {SESSIONS_ROOT} 下按 id 精确查找；"
                "不会退回其他会话）"
            )
        if len(cands) > 1:
            matched = []
            for p in cands:
                hid, hcwd = _header_identity(p)
                if hid and _norm_session_id(hid) == want and _same_path(hcwd or "", cwd):
                    matched.append(p)
            if len(matched) == 1:
                return matched[0]
            listing = "、".join(str(p) for p in cands)
            sys.exit(f"会话 id {session} 对应多个目录（{listing}），无法确定是哪一个")

        only = cands[0]
        hid, hcwd = _header_identity(only)
        if not hid:
            sys.exit(f"会话目录 {only} 缺少合法 session header，拒绝使用")
        if _norm_session_id(hid) != want:
            sys.exit(
                f"会话目录 {only} 的 header id 是 {hid}，与请求的 {want} 不符；拒绝使用"
            )
        if not hcwd:
            sys.exit(f"会话目录 {only} 的 header 没有 cwd，拒绝使用")
        if not _same_path(hcwd, cwd):
            sys.exit(f"会话 {session} 属于 {hcwd}，与请求的 {cwd} 不符；拒绝写错项目")
        return only

    matches: list[Path] = []
    for d in _all_session_dirs():
        hid, hcwd = _header_identity(d)
        if hid and _same_path(hcwd or "", cwd):
            matches.append(d)
    if not matches:
        sys.exit(f"{cwd} 下没有会话目录（不会退回其他项目或其他会话）")
    if len(matches) > 1:
        listing = "\n".join(f"  - {p.name}" for p in matches)
        sys.exit(
            f"{cwd} 下有 {len(matches)} 个会话，无法确定是哪一个；"
            f"请用 --session <id> 指定：\n{listing}"
        )
    return matches[0]


def read_events(session_dir: Path) -> list[dict]:
    f = session_dir / SESSION_LOG
    if not f.exists():
        sys.exit(f"缺少会话日志：{f}")
    raw = _decompress(f)
    events = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def text_of(content) -> str:
    """把 message content（块数组或字符串）拍平成文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text", ""))
        return "\n".join(out)
    return ""


def clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def render_tasks(prompts: list[str], approvals: list[str], lines: list[str]) -> None:
    """任务区：首条目标单独保留 + 最近 N 条 + 省略提示。

    只截最近 N 条会在长会话里丢掉初始目标，所以首条固定留一行。
    """
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
    if approvals:
        lines += ["## 审批 / 补充（不是初始目标，也不代表扩大授权）", ""]
        for p in approvals[-5:]:
            lines.append(f"- {clip(p, 300)}")
        lines.append("")


def _command_status(call_id: str, results: dict[str, dict], jobs: dict[str, dict]) -> str:
    """命令的成败标注。没有明确 exit_code/status 就明说状态未知。

    DSH 的 `job/settled` 带 `status` 和 `detail`（形如 `exit code: 0`），这是
    唯一可靠的执行结果来源；`tool/result.isError == false` 只说明工具调用本身
    没报错，不能推断 shell 命令 exit 0。
    """
    job = jobs.get(call_id)
    if job:
        status = str(job.get("status") or "")
        detail = str(job.get("detail") or "")
        code = None
        if "exit code:" in detail:
            tail = detail.split("exit code:", 1)[1].strip().split()[0:1]
            if tail and tail[0].lstrip("-").isdigit():
                code = int(tail[0])
        if status == "completed":
            if code == 0:
                return "成功（exit 0）"
            if code is not None:
                return f"失败（exit {code}）"
            return "完成（未报告 exit code，状态未知）"
        if status in ("killed", "failed", "cancelled", "canceled", "timeout"):
            return f"未成功（status={status}）" + (f" exit {code}" if code is not None else "")
        return f"状态未知（status={status or '空'}）"
    res = results.get(call_id)
    if res is None:
        return "状态未知（没有对应结果，可能未完成）"
    if res.get("isError") is True:
        return "失败（工具报错）"
    return "调用已返回（未报告 exit code，执行结果未知）"


def canonical_session_id(native_id: str) -> str:
    """native id → ai-memory DB 里的 canonical SessionId。

    对齐 ai-memory-core 的 `SessionId::from_native`：纯 UUID 原样用，
    其他字符串（DSH 的 `session-<uuid>` 带前缀）走 UUIDv5(NAMESPACE_OID, raw)。
    所以数据库里查不到裸 UUID，必须用这个哈希值。
    """
    raw = native_id
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        pass
    return str(uuid.uuid5(uuid.NAMESPACE_OID, raw))


def build(events: list[dict], session_id: str, cwd: str,
          native_id: str | None = None, log_path: str | None = None) -> str:
    prompts: list[str] = []
    approvals: list[str] = []
    files_written: dict[str, int] = {}
    files_read: dict[str, int] = {}
    failed_ops: list[str] = []
    unknown_ops: list[str] = []
    commands: list[tuple[str, str]] = []  # (命令, callId)
    tool_counts: dict[str, int] = {}
    todos: list[str] = []
    last_assistant = ""
    turns = 0
    turn_end_reason = ""
    results: dict[str, dict] = {}
    jobs: dict[str, dict] = {}
    calls: list[dict] = []

    for ev in events:
        t = ev.get("type")
        d = ev.get("data") or {}

        if t == "user/message":
            src = d.get("source") or {}
            kind = src.get("kind")
            txt = text_of(d.get("content")).strip()
            if not txt:
                continue
            if kind in USER_TASK_KINDS:
                # 连续重复（用户点重试）只留一条，否则"重试×5"会挤掉真任务
                if not prompts or prompts[-1] != txt:
                    prompts.append(txt)
            elif kind in USER_APPROVAL_KINDS:
                # 审批/补充单独列出：既不是初始目标，也不扩大任务授权
                if not approvals or approvals[-1] != txt:
                    approvals.append(txt)
            # 其余 kind（runtime-context / skill-catalog / time-context /
            # agent-instructions / hooks-claude-code / compact-checkpoint /
            # dsh-mnemon / model-selection …）是注入，全部丢弃
            continue

        if t == "turn/end":
            turns += 1
            turn_end_reason = d.get("reason") or turn_end_reason
            continue

        if t == "assistant/message":
            # 真实结构是 data.message.content；保留 data.content 作回退。
            # text_of 只取 type == "text"，reasoning 块不会进存档。
            msg = d.get("message") or {}
            txt = text_of(msg.get("content") if msg else None).strip()
            if not txt:
                txt = text_of(d.get("content")).strip()
            if txt:
                last_assistant = txt
            continue

        if t == "tool/result":
            m = d.get("message") or {}
            cid = m.get("toolCallId") or (m.get("source") or {}).get("callId")
            if cid:
                results[str(cid)] = {
                    "isError": m.get("isError"),
                    "text": text_of(m.get("content")),
                }
            continue

        if t == "job/settled":
            tool_call = d.get("toolCall") or {}
            cid = tool_call.get("callId") or tool_call.get("rootCallId")
            if cid:
                jobs[str(cid)] = d
            continue

        if t != "tool/call":
            continue

        name = d.get("name") or "?"
        call_id = str(d.get("callId") or "")
        tool_counts[name] = tool_counts.get(name, 0) + 1
        try:
            args = json.loads(d.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}

        if name == "bash":
            cmd = args.get("command") or ""
            if cmd:
                commands.append((str(cmd), call_id))
            continue

        if name == "todo_write":
            # 只保留最后一次的完整状态，否则每轮都追加会累积重复
            todos.clear()
            for item in args.get("todos") or []:
                if isinstance(item, dict):
                    mark = {"completed": "x", "in_progress": "~"}.get(item.get("status", ""), " ")
                    todos.append(f"- [{mark}] {clip(item.get('content', ''), 120)}")
            continue

        field = FILE_TOOLS.get(name)
        if field and args.get(field):
            calls.append({"callId": call_id, "name": name, "path": str(args[field])})

    # 文件改动必须配对 tool/result 才敢记：只有 isError **明确为 False** 才算
    # 确认成功。isError 缺失说明来源状态不明，归到"状态未知"，不编造成功。
    for call in calls:
        name, path, call_id = call["name"], call["path"], call["callId"]
        if name == "read":
            files_read[path] = files_read.get(path, 0) + 1
            continue
        if name not in WRITE_TOOLS:
            continue
        res = results.get(call_id)
        if res is None:
            unknown_ops.append(f"- `{path}`　（{name}，没有对应结果，可能未完成）")
        elif res.get("isError") is True:
            failed_ops.append(f"- `{path}`　（{name}，工具报错）")
        elif res.get("isError") is False:
            files_written[path] = files_written.get(path, 0) + 1
        else:
            unknown_ops.append(f"- `{path}`　（{name}，结果没有 isError 标志，状态不明）")

    # ---- 渲染 ----
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    lines = [
        f"# DSH 阶段存档 · {Path(cwd).name}",
        "",
        "> 自动生成，每轮覆盖。不是最终交接，只是「我停在这里」的快照。",
        f"> 更新：{now}　轮次：{turns}　目录：`{cwd}`",
        f"> 原生会话：`{native_id or _norm_session_id(session_id)}`",
        f"> canonical DB id：`{canonical_session_id(native_id or _norm_session_id(session_id))}`",
        "",
    ]

    lines += ["## 任务（人类原话，按时间顺序）", ""]
    render_tasks(prompts, approvals, lines)

    lines += ["## 改过的文件（已确认成功）", ""]
    if files_written:
        for p, n in sorted(files_written.items(), key=lambda kv: -kv[1])[:25]:
            lines.append(f"- `{p}`" + (f"　×{n}" if n > 1 else ""))
        lines.append("")
        lines.append("_（只统计工具明确返回成功的写入/编辑；是否已提交需自行核对。）_")
    else:
        lines.append("_（没有确认成功的写入/编辑）_")
    lines.append("")

    if failed_ops:
        lines += ["## 失败的操作", ""] + failed_ops[:20] + [""]

    if unknown_ops:
        lines += ["## 状态未知的操作", ""] + unknown_ops[:20] + [""]

    if files_read:
        lines += ["## 读过的文件", ""]
        for p, n in sorted(files_read.items(), key=lambda kv: -kv[1])[:12]:
            lines.append(f"- `{p}`")
        lines.append("")

    if todos:
        lines += ["## 待办（最后一次 todo_write 的状态）", ""] + todos + [""]

    if commands:
        lines += [
            "## 最近的命令",
            "",
            "> 调用记录，不代表执行成功。状态只在有明确 exit code / status 时标注，",
            "> 否则写「状态未知」；工具没报错不等于命令 exit 0。",
            "",
        ]
        for cmd, call_id in commands[-6:]:
            lines.append(f"- `{clip(cmd, 200)}`")
            lines.append(f"  - 状态：{_command_status(call_id, results, jobs)}")
        lines.append("")

    if last_assistant:
        lines += ["## 最后一条助手输出", "", clip(last_assistant, 1200), ""]

    lines += [
        "## 工具用量",
        "",
        ", ".join(f"{k}×{v}" for k, v in sorted(tool_counts.items(), key=lambda kv: -kv[1])[:10])
        or "_（无）_",
        "",
        "## 接手方式",
        "",
        "下一个 agent（Codex 或新的 DSH 会话）：",
        "",
        f"1. 读这一页，把「任务」里的【初始目标】和最近几条当作要继续的目标；",
        "2. 「已确认修改的文件」逐个看当前内容，别假设已提交；",
        "3. 「失败的操作」「状态未知的操作」不要当成已完成；",
        "4. 「待办」里 `[~]` 和 `[ ]` 是没做完的；",
        "5. 需要完整原始过程时，读本会话的实际日志文件：",
        f"   `zstd -dc {shlex.quote(log_path) if log_path else '<日志路径>'}`",
        "   （路径就是本次生成这一页时实际读取的那份日志，不靠目录名拼接）",
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
    """按 ai-memory 的 scope 规则推导 (workspace, project)。

    优先级与服务器一致：最近的 .ai-memory.toml > git remote 的 hostless 路径
    （upstream 优先、其次 origin；取完整仓库路径，github.com/a/b -> a-b）
    > basename(cwd)。workspace 默认 `default`。

    必须显式传：MCP 是静态 client，不带会话身份，省略 scope 时服务器会用
    它自己的 ActiveProject 槽位 —— 那个槽位是最近一次 hook 刷进去的，可能
    属于另一个项目，页面就写错地方了。
    """
    d = Path(cwd).resolve()
    for parent in [d, *d.parents]:
        marker = parent / ".ai-memory.toml"
        if marker.is_file():
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

    # upstream 优先，其次 origin（对齐 ai-memory 的 canonical 顺序）
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


def mcp_write(server: str, token: str, path: str, body: str, ws: str, proj: str) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "memory_write_page",
            "arguments": {
                "path": path,
                "body": body,
                "workspace": ws,
                "project": proj,
            },
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
        # 只报错误类型/码，不回显可能含凭据或提示正文的 message
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


def main() -> None:
    ap = argparse.ArgumentParser(description="DSH 阶段存档（覆盖写入 ai-memory 页面）")
    ap.add_argument("--session", help="会话 id（可省略 session- 前缀）")
    ap.add_argument("--cwd", default=os.getcwd(), help="项目目录，默认当前目录")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--path", help="覆盖写入的页面路径，默认 _autosave/dsh-<session>.md")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写")
    ap.add_argument("--quiet", action="store_true", help="成功时不打印（hook 里用）")
    args = ap.parse_args()

    cwd = str(Path(args.cwd).resolve())
    session_dir = find_session(cwd, args.session)
    bare = session_dir.name.removeprefix("session-")
    hid, _ = _header_identity(session_dir)
    native_id = hid or _norm_session_id(bare)
    log_path = str(session_dir / SESSION_LOG)
    events = read_events(session_dir)
    body = build(events, bare, cwd, native_id=native_id, log_path=log_path)
    # 页面 path 保持裸 UUID 命名，兼容既有页面，不造新页
    path = args.path or PAGE_TEMPLATE.format(session=bare)

    if args.dry_run:
        print(body)
        return

    ws, proj = resolve_project(cwd)
    result = mcp_write(args.server, load_token(), path, body, ws, proj)
    if not args.quiet:
        print(f"✓ {ws}/{proj}:{path}  page_id={result.get('page_id')}")


if __name__ == "__main__":
    main()
