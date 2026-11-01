#!/usr/bin/env python3
"""resume — 为接手 agent 构建「可接续聊天」的恢复上下文（纯只读）。

「可接续」包含本轮已收尾之后的后续需求，不限于中断恢复。

设计原则
--------
* **只读**：不写 ai-memory 数据库、不创建 handoff、不调模型。
* **解析原生事件判完成**，不靠 mtime / ended_at / 旧 handoff。
* **结构化错误码**区分 quota 与其他错误：
    - DSH:  turn/end.reason.kind == "error" 且 reason.error.code ∈ {QUOTA, ACCOUNT_QUOTA}
    - Codex: task_complete.error.codex_error_info == "usage_limit_exceeded"
* **崩溃不凭空确证**：用户手动接续一个未结束来源时，标「非正常结束/原因未知」。
* **不猜 latest**：0 个候选给 no-candidate，多个给 needs-selection。

用法
----
    python3 resume.py                      # 当前 cwd 推导 scope
    python3 resume.py --session <native>   # 显式指定
    python3 resume.py --from-agent dsh
    python3 resume.py --json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.environ.get("AI_MEMORY_DATA_DIR")
    or (Path.home() / "Library" / "Application Support" / "ai-memory")
)
DB_PATH = Path(os.environ.get("BRIDGE_DB") or (DATA_DIR / "db" / "memory.sqlite"))
DSH_ROOT = Path(os.environ.get("BRIDGE_DSH_ROOT") or (Path.home() / ".dsh" / "sessions"))
CODEX_ROOT = Path(os.environ.get("BRIDGE_CODEX_ROOT") or (Path.home() / ".codex" / "sessions"))
# Codex 聊天命名索引（thread_name）。默认与 CODEX_ROOT 同级，可用 env 覆盖。
CODEX_INDEX = Path(os.environ.get("BRIDGE_CODEX_INDEX")
                   or (CODEX_ROOT.parent / "session_index.jsonl"))

# 结构化 quota 码（不靠文本）
DSH_QUOTA_CODES = {"QUOTA", "ACCOUNT_QUOTA"}
CODEX_QUOTA_INFO = {"usage_limit_exceeded"}

# 用户任务输入的 source.kind
DSH_USER_KINDS = {"user", "user-rpc"}
DSH_APPROVAL_KINDS = {"user-approval"}

# Codex 注入前缀（真实用户贴 HTML 不能被误杀）
CODEX_INJECT_PREFIXES = (
    "# AGENTS.md instructions",
    "# CLAUDE.md instructions",
    "AGENTS.md instructions",
    "The following is the Codex agent history",
    "<user_instructions",
    "<external_",
    # 系统/环境注入（不是人类任务；真实用户贴 HTML 如 <div> 不受影响）
    "<environment_context",
    "<environment_details",
    "<runtime_context",
    "<system_reminder",
    "<time_context",
)

# 只清洗「凭据形状」，不碰普通内容哈希（sha256 不能误伤）
SECRET_PATTERNS = [
    # 只清洗「凭据形状」，不碰普通内容哈希（sha256 不能被误伤）
    re.compile(r'(Bearer\s+)[A-Za-z0-9._-]{12,}', re.I),
    re.compile(r'((?:AI_MEMORY_AUTH_TOKEN|AUTH_TOKEN|API_KEY|SECRET|PASSWORD)\s*=\s*)\S+', re.I),
    re.compile(r'((?:api[_-]?key|apikey|token|secret|password)\s*[:=]\s*)[\'"]?[A-Za-z0-9._-]{8,}[\'"]?', re.I),
    re.compile(r'\bsk-[A-Za-z0-9._-]{8,}'),
    re.compile(r'(auth-token\s*[:=]\s*)\S+', re.I),
    # JSON 形状："api_key": "value" / "password": "value"
    re.compile(r'("(?:api[_-]?key|apikey|token|secret|password|authorization)"\s*:\s*)"[^"]*"', re.I),
]

TRUNC = 4000


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def canonical_id(native: str) -> str:
    try:
        return str(uuid.UUID(native))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(uuid.NAMESPACE_OID, native))


SENSITIVE_KEYS = ("api_key", "apikey", "token", "secret", "password",
                  "authorization", "auth", "credential", "private_key")


def scrub_structure(obj, depth: int = 0):
    """递归清洗结构化数据里的敏感 key（比文本正则更可靠）。"""
    if depth > 12:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and any(sk in k.lower() for sk in SENSITIVE_KEYS):
                out[k] = "«已脱敏»"
            else:
                out[k] = scrub_structure(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [scrub_structure(x, depth + 1) for x in obj]
    return obj


def scrub(text: str) -> str:
    out = text
    for pat in SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "«已脱敏»", out)
    return out


def clip(s: str, n: int = 400) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- 读取日志
def read_jsonl_lenient(path: Path) -> tuple[list[dict], bool]:
    """读 JSONL，容忍尾行截断。返回 (事件, 是否截断)。"""
    events: list[dict] = []
    truncated = False
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], True
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            truncated = True
            continue
        # 合法 JSON 但不是对象（[]/null/42）→ 不是有效事件，标损坏
        if not isinstance(obj, dict):
            truncated = True
            continue
        events.append(obj)
    return events, truncated


def read_dsh_events(log: Path) -> tuple[list[dict], bool]:
    """DSH 会话日志是 zstd。

    崩溃可能把最后一帧截断。此时 `zstd -dc` 会以非零码退出，但**stdout 里
    已经解出的完整帧仍然有效**——实测用 check=True 会丢掉 1186 字节、51 行
    有效数据。所以这里不看退出码，只用 stdout，并把非零退出标记为不完整。
    """
    import gzip
    import subprocess

    try:
        head = log.read_bytes()[:4]
    except OSError:
        return [], True

    incomplete = False
    raw = ""
    try:
        if head[:4] == b"\x28\xb5\x2f\xfd":
            proc = subprocess.run(["zstd", "-dc", str(log)], capture_output=True)
            raw = proc.stdout.decode("utf-8", "replace")
            if proc.returncode != 0:
                incomplete = True          # 尾帧坏了，但前缀有效
        elif head[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(log.read_bytes()).decode("utf-8", "replace")
            except (OSError, EOFError, gzip.BadGzipFile):
                # 多成员 gzip 的尾部坏帧：逐行尽量解出前缀
                raw = _gzip_prefix(log)
                incomplete = True
        else:
            raw = log.read_text(encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return [], True

    events, truncated = [], False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            truncated = True
            continue
        if not isinstance(obj, dict):
            truncated = True
            continue
        events.append(obj)
    return events, (truncated or incomplete)


def _gzip_prefix(log: Path) -> str:
    """尽量解出 gzip 流里已完整的前缀。"""
    import gzip
    import io

    data = log.read_bytes()
    out = []
    buf = io.BytesIO(data)
    while True:
        start = buf.tell()
        try:
            with gzip.GzipFile(fileobj=buf) as fh:
                out.append(fh.read().decode("utf-8", "replace"))
        except (OSError, EOFError, gzip.BadGzipFile):
            break
        if buf.tell() <= start:
            break
    return "".join(out)


# ---------------------------------------------------------------- Codex 分类
def codex_user_texts(events: list[dict]) -> list[str]:
    """真实用户输入：两种投影共用识别，重复投影去重。"""
    out: list[str] = []
    for e in events:
        if not _is_real_codex_user_event(e):
            continue
        txt = _codex_user_text_of(e)
        if txt and (not out or out[-1] != txt):
            out.append(txt)
    return out


def classify_codex(path: Path) -> dict:
    events, truncated = read_jsonl_lenient(path)
    st = _classify_codex_events(events)
    st["truncated"] = truncated
    st["events"] = [e for e in events if isinstance(e, dict)]
    st["event_count"] = len(st["events"])
    st["cursor"] = _cursor_of(st["events"])
    st["path"] = path
    return st


def _classify_codex_events(events: list[dict]) -> dict:
    """按原生事件判 Codex 结束状态（事件级，供单文件与多片段共用）。

    * 额度中断时 `task_complete` 仍然存在，带
      `error.codex_error_info == "usage_limit_exceeded"`。
    * `task_complete` 之后只要出现**新的用户事件**（含 typed UserMessage），
      就算开了新任务，旧 quota 不能继承。
    * 纯 environment/time 注入不算新任务。
    """
    meta = {}
    for e in events:
        if isinstance(e, dict) and e.get("type") == "session_meta":
            meta = e.get("payload") or {}
            break

    last_task_idx, last_task = -1, None
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            continue
        p = e.get("payload") or {}
        if e.get("type") == "event_msg" and p.get("type") == "task_complete":
            last_task, last_task_idx = p, i

    after = events[last_task_idx + 1:] if last_task_idx >= 0 else events
    new_work = False
    for e in after:
        if not isinstance(e, dict):
            continue
        p = e.get("payload") or {}
        if _is_real_codex_user_event(e):
            new_work = True
            break
        if e.get("type") == "event_msg" and p.get("type") == "task_started":
            new_work = True
            break

    outcome, evidence = "unfinished", ""
    if last_task is None:
        outcome, evidence = "unfinished", "没有 task_complete（未完成或崩溃）"
    else:
        err = last_task.get("error") or {}
        raw_info = err.get("codex_error_info")
        # 真实数据里 codex_error_info 不总是字符串：实测出现过
        # {"http_connection_failed": {"http_status_code": 502}} 这类嵌套 dict。
        # 先标准化成字符串再判配额，否则 `dict in set` 会抛 TypeError。
        # 只有**确属配额**的才判 quota；其他结构化错误仍是 error。
        info = _error_info_key(raw_info)
        if new_work:
            outcome = "unfinished"
            evidence = "最后一个 task_complete 之后又出现新的用户输入/新一轮"
        elif info in CODEX_QUOTA_INFO:
            outcome, evidence = "quota", f"task_complete.error.codex_error_info={info}"
        elif err:
            outcome, evidence = "error", f"task_complete.error.codex_error_info={info or '(未标注)'}"
        elif last_task.get("last_agent_message") is not None:
            outcome, evidence = "completed", "task_complete 无 error 且有 last_agent_message"
        else:
            outcome, evidence = "unfinished", "task_complete 无 error 但 last_agent_message 为空"

    return {"agent": "codex", "outcome": outcome, "evidence": evidence,
            "meta": meta, "truncated": False}


def _event_time(e: dict):
    """取事件时间（微秒或 ISO 字符串）。无法判定返回 None。"""
    if not isinstance(e, dict):
        return None
    t = e.get("timestamp") or e.get("time")
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1_000_000
        except ValueError:
            return None
    return None


def _merge_fragments(agent: str, paths: list[Path]) -> tuple[list[dict], bool]:
    """把同一 native 的多个片段按事件时间合并。

    返回 (事件列表, 顺序是否可靠, 是否有片段被截断)。顺序不可靠时调用方必须标
    incomplete，不能拿旧片段当 ready。截断标记必须向上传播——否则多片段正文会
    假称 truncated=False，把损坏片段当完整证据。

    排序原则（**不用片段级区间判断冲突**）：
    * 真实日志里长期存活的主片段会横跨数天，区间必然与后续片段重叠，
      这是正常现象，不是冲突。
    * 因此改为**事件级全局时间排序**；同 timestamp 时保留各片段内部已知先后。
    * 只有当同一 timestamp 上出现**跨片段且无法定序的相反终态**（例如一个
      completed、一个 quota 的 task_complete）时，才判为不可靠。
    """
    buckets = []
    any_truncated = False
    for p in paths:
        evs, tr = (read_dsh_events(p) if agent == "dsh" else read_jsonl_lenient(p))
        if tr:
            any_truncated = True
        buckets.append({"path": p, "events": evs})

    # 片段内部顺序是日志写入顺序，天然可信：记下每条的片段内序号
    items = []
    for bi, b in enumerate(buckets):
        for idx, e in enumerate(b["events"]):
            if not isinstance(e, dict):
                continue
            t = _event_time(e)
            items.append({"t": t, "frag": bi, "idx": idx, "e": e,
                          "fname": b["path"].name})

    # 任一事件缺时间戳 → 无法全局定序，退回片段名+片段内顺序并标记不可靠
    if any(it["t"] is None for it in items):
        items.sort(key=lambda it: (it["fname"], it["idx"]))
        merged, _dropped = _dedup_replayed(items)
        return merged, False, any_truncated

    # 排序**之前**先判源内时间是否单调：一旦排序，片段内原始顺序就被打乱，
    # 再检查就查不出「时间戳倒退」了。
    source_order_ok = _source_order_monotonic(items)

    # 事件级全局排序；同 timestamp 用「片段内序号」保源内先后，
    # 再以片段名兜底（只为稳定，不作为冲突裁决）
    items.sort(key=lambda it: (it["t"], it["idx"], it["fname"]))

    order_ok = _order_is_reliable(items, source_order_ok)
    merged, _dropped = _dedup_replayed(items)
    return merged, order_ok, any_truncated


def _source_order_monotonic(items: list[dict]) -> bool:
    """每个片段内部，时间戳是否随写入顺序单调不减。

    时间戳可能抖动或回填。若源内顺序与时间顺序冲突，**必须保留源内顺序**
    （那是实际写入次序），并判为不可靠——不能拿时间替用户改历史。
    """
    for frag in {it["frag"] for it in items}:
        prev_t = None
        for it in items:
            if it["frag"] != frag:
                continue
            t = it["t"]
            if t is None:
                continue
            if prev_t is not None and t < prev_t:
                return False
            prev_t = t
    return True


# 终态类事件：同 timestamp 上跨片段出现不同终态时才算真冲突
_TERMINAL_KINDS = ("task_complete", "turn/end")


def _error_info_key(v) -> str:
    """把 `codex_error_info` 规范成可哈希字符串。

    真实数据里它**不总是字符串**：实测出现过
    `{"http_connection_failed": {"http_status_code": null}}` 这种嵌套 dict。
    直接放进签名会让签名不可哈希，因此统一压成稳定字符串。
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, dict):
        return ",".join(sorted(f"{k}={_error_info_key(x)}" for k, x in v.items()))
    if isinstance(v, (list, tuple)):
        return "|".join(_error_info_key(x) for x in v)
    return str(v)


def _terminal_signature(e: dict):
    """把终态事件压成一个可比较的签名；非终态返回 None。

    签名元素必须是可哈希的标量——真实日志里 `error` 字段结构不固定。
    """
    if not isinstance(e, dict):
        return None
    typ = e.get("type")
    if typ == "event_msg":
        p = e.get("payload") or {}
        if p.get("type") == "task_complete":
            err = p.get("error") or {}
            return ("task_complete", bool(err),
                    _error_info_key(err.get("codex_error_info")))
    if typ == "turn/end":
        d = e.get("data") or {}
        reason = d.get("reason") or {}
        kind = reason.get("kind") if isinstance(reason, dict) else str(reason)
        err = (reason.get("error") or {}) if isinstance(reason, dict) else {}
        code = err.get("code") if isinstance(err, dict) else None
        return ("turn/end", str(kind or ""), _error_info_key(code))
    return None


def _is_success_terminal(sig) -> bool:
    """该终态签名是否表示「干净完成」。

    `task_complete` 的第二项是 `bool(error)`：False 才是成功。
    `turn/end` 的第二项是 reason.kind，'completed' 才是成功。
    """
    if sig is None:
        return False
    kind, flag = sig[0], sig[1]
    if kind == "task_complete":
        return flag is False
    if kind == "turn/end":
        return flag == "completed"
    return False


def _is_header_event(e: dict) -> bool:
    """会话头/元数据事件：只用于定位 provenance，不参与工作事件冲突判断。

    `session_meta` 里的 `client_version` 等字段在不同片段间可能不同，
    那是运行环境快照差异，不是用户/工具工作内容的冲突。
    """
    if not isinstance(e, dict):
        return False
    return e.get("type") in ("session_meta", "session")


def _work_signature(e: dict):
    """工作事件签名：只有「真实用户输入」与「终态」参与顺序裁决。"""
    if not isinstance(e, dict) or _is_header_event(e):
        return None
    return _terminal_signature(e)


def _order_is_reliable(items: list[dict], source_order_ok: bool = True) -> bool:
    """事件级排序是否可靠。

    判为不可靠的情形（都必须让调用方人工核验，不能替用户猜）：
    1. **同 timestamp 跨片段的不同终态** —— 完成了还是额度中断无法确定。
    2. **同稳定键、同时间戳、但内容不同** —— 同一 item/call 两份不同内容。
    3. **时间排序会逆转片段内已知顺序** —— 源内写入顺序可信，时间戳可能
       抖动/回填；用时间覆盖源内顺序等于替用户改历史。
    4. **跨片段同 timestamp 出现「新真实用户输入」与「旧 task_complete」** ——
       谁先谁后决定任务是「已完成」还是「有新任务未完成」，不能随便排。

    header/元数据事件不参与以上判断；同片段内的先后由 idx 保留。
    """
    import hashlib
    import json as _json

    # 3) 源内顺序不得被时间逆转（由调用方在排序前判好传入）
    if not source_order_ok:
        return False

    # 1) 同 timestamp 跨片段不同终态
    by_time: dict = {}
    for it in items:
        sig = _work_signature(it["e"])
        if sig is None:
            continue
        by_time.setdefault(it["t"], []).append((it["frag"], sig))
    for t, group in by_time.items():
        frags = {f for f, _s in group}
        sigs = {s for _f, s in group}
        if len(frags) > 1 and len(sigs) > 1:
            return False

    # 4) 跨片段同 timestamp：新用户输入 vs 旧完成
    #    同一时间点上，「task_complete」与「真实用户输入」分别来自不同片段时，
    #    谁先谁后决定任务是「已完成」还是「有新任务未完成」，不能靠 idx/fname 猜。
    for t in {it["t"] for it in items if it["t"] is not None}:
        same = [it for it in items if it["t"] == t and not _is_header_event(it["e"])]
        if len({it["frag"] for it in same}) < 2:
            continue
        completions = [(it["frag"], _terminal_signature(it["e"])) for it in same
                       if _terminal_signature(it["e"]) is not None
                       and _is_success_terminal(_terminal_signature(it["e"]))]
        if not completions:
            continue
        user_frags = {it["frag"] for it in same if _is_real_codex_user_event(it["e"])}
        if any(cf != uf for cf, _s in completions for uf in user_frags):
            return False

    # 2) 同稳定键 + 同时间戳 + 不同内容
    by_key: dict = {}
    for it in items:
        if _is_header_event(it["e"]):
            continue
        key = _stable_event_key(it["e"])
        if key is None:
            continue
        try:
            body = _json.dumps(it["e"], ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            body = str(it["e"])
        digest = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()
        bucket = (key, it["t"])
        prev = by_key.get(bucket)
        if prev is None:
            by_key[bucket] = digest
        elif prev != digest:
            return False
    return True


def _hashable(v):
    """把任意值压成可哈希、可比较的标量。真实日志字段结构不固定。"""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}={_hashable(x)}" for k, x in sorted(v.items())) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(str(_hashable(x)) for x in v) + "]"
    return str(v)


def _stable_event_key(e: dict):
    """稳定事件键，用于识别**精确重放**（同一条事件在多个片段里重复出现）。

    只有真正同一事件才应共用键：call 与 result、同一 item 的不同阶段
    都不能折成一个，所以键里带上事件类型与阶段。

    **缺少可靠 id 时返回 None**，而不是拿 `None` 占位——
    否则两条同时间、都没有 id 的真实用户事件会被当成「同一个事件」，
    进而被误判成内容冲突。
    """
    if not isinstance(e, dict):
        return None
    p = e.get("payload") or {}
    typ = e.get("type")
    if typ == "response_item":
        pt = p.get("type")
        ident = p.get("id") or p.get("call_id")
        if pt in ("function_call", "custom_tool_call"):
            cid = p.get("call_id") or ident
            return ("call", _hashable(cid), _hashable(p.get("name"))) if cid else None
        if pt in ("function_call_output", "custom_tool_call_output"):
            cid = p.get("call_id") or ident
            return ("result", _hashable(cid)) if cid else None
        if pt == "message":
            return ("msg", _hashable(p.get("role")), _hashable(ident), _hashable(p.get("channel"))) if ident else None
        return ("ri", _hashable(pt), _hashable(ident)) if ident else None
    if typ == "event_msg":
        pt = p.get("type")
        if pt == "item_completed":
            it = p.get("item") or {}
            iid = it.get("id")
            return ("item", _hashable(it.get("type")), _hashable(iid)) if iid else None
        if pt == "task_complete":
            err = p.get("error") or {}
            return ("tc", _error_info_key(err.get("codex_error_info")), _hashable(p.get("turn_id")))
        tid = p.get("turn_id") or p.get("id")
        return ("em", _hashable(pt), _hashable(tid)) if tid else None
    if typ in ("user/message", "turn/start", "turn/end"):
        d = e.get("data") or {}
        return (typ, d.get("turnId") or d.get("turn_id") or d.get("id"))
    return (typ, e.get("timestamp"))


def _dedup_replayed(items: list[dict]) -> tuple[list[dict], int]:
    """去掉**精确重放**的公共前缀，保留可追溯性。

    只有「稳定键 + 时间戳 + 内容指纹」全同才算重放；同 turn 里两次内容相同的
    「继续」因 call_id/timestamp 不同不会被误删。
    同键但内容不同 → 不删，交由 `_order_is_reliable` 之外的人工核验（保留两条）。
    """
    import hashlib
    import json as _json

    seen = {}
    out = []
    dropped = 0
    for it in items:
        e = it["e"]
        key = _stable_event_key(e)
        if key is None:
            out.append(e)
            continue
        try:
            body = _json.dumps(e, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            body = str(e)
        digest = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()
        full = (key, it["t"], digest)
        if full in seen:
            dropped += 1
            continue
        seen[full] = it
        out.append(e)
    return out, dropped


def _classify_events(agent: str, events: list[dict], order_ok: bool,
                     truncated: bool = False) -> dict:
    """对已合并的事件列表做结束状态判定。"""
    if agent == "dsh":
        st = _classify_dsh_events(events)
    else:
        st = _classify_codex_events(events)
    st["order_ok"] = order_ok
    st["truncated"] = bool(truncated)
    if not order_ok:
        st["outcome"] = "unfinished"
        st["evidence"] = (st.get("evidence", "") +
                          "；片段事件时间无法可靠排序，只能给未完成（不可当 ready 旧上下文）")
    st["events"] = events
    st["event_count"] = len(events)
    st["cursor"] = _cursor_of(events)
    return st



REPLY_OPEN = "<send_user_message_question_reply"
REPLY_CLOSE = "</send_user_message_question_reply>"
# 选项回复里的 answer 短到这种程度时，单独看没有意义，要带上问题一起呈现
SHORT_ANSWER_CHARS = 12


def parse_question_reply(text: str) -> dict | None:
    """解析 `<send_user_message_question_reply>[{...}]</send_user_message_question_reply>`。

    这是**用户真实选项回复**（用户点了选项或填了答案），不是垃圾：
    answer 里常有范围级修正，例如「不需要自动切换，只需要手动切换…」。
    所以不能整段丢弃，只丢掉重复的 question 协议外壳。

    返回 {"answers": [str], "items": [{"question","answer"}]}；
    不是该形态、或 JSON 解析失败时返回 None（调用方据此区分「非回复」与「坏回复」）。
    """
    import json

    s = text.lstrip()
    if not s.startswith(REPLY_OPEN):
        return None
    body = s[len(REPLY_OPEN):]
    # 去掉开头标签剩余部分（可能带属性）与结尾闭合标签
    if body.startswith(">"):
        body = body[1:]
    if REPLY_CLOSE in body:
        body = body.split(REPLY_CLOSE, 1)[0]
    body = body.strip()
    if not body:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(body)
    except (ValueError, IndexError):
        return None
    if not isinstance(obj, list):
        obj = [obj]
    items, answers = [], []
    for it in obj:
        if not isinstance(it, dict):
            continue
        ans = it.get("answer")
        if isinstance(ans, str) and ans.strip():
            ans = ans.strip()
            answers.append(ans)
            items.append({"question": (it.get("question") or "").strip(),
                          "answer": ans})
    if not answers:
        return None
    return {"answers": answers, "items": items}


def _reply_text(parsed: dict) -> str:
    """把选项回复渲染成一行人类可读的补充。

    短答案（「是」「第二项」）单独看没有意义，带上问题做消歧；
    长答案本身就是完整表述，只留 answer，不把整段 question 当新授权。
    """
    out = []
    for it in parsed["items"]:
        ans, q = it["answer"], it["question"]
        if len(ans) <= SHORT_ANSWER_CHARS and q:
            out.append(f"{q} → {ans}")
        else:
            out.append(ans)
    return "；".join(out)


def _is_codex_echo(text: str, typed: bool = False) -> bool:
    """Codex 会把历史回合以 `role=user` 回显进日志，这不是人类输入。

    实测形态（真实日志 `01a1876c`）：
    * `[214] tool exec call: ...`
    * `[331] tool send_message call: {...}`
    * `[221] assistant: Agent message from ...`

    **不包含** `<send_user_message_question_reply>`：那是用户真实选项回复，
    由 `parse_question_reply` 单独处理，见该函数说明。

    `typed=True`（event_msg.item_completed.UserMessage）是 harness 记下的
    实际用户输入：用户可能**粘贴**了 `[214] tool ...` 并接着问问题，
    这种不能仅凭前缀丢掉。所以前缀规则只对 `response_item` 历史回显生效。
    """
    import re

    low = text.lstrip()
    if typed:
        return False
    if re.match(r"^\[\d+\]\s*(tool|assistant)\b", low):
        return True
    return False


def _is_real_codex_user_event(e: dict) -> bool:
    """判断一个事件是否是**真实用户输入**（供分类与提取共用）。

    覆盖两种真实形态：
    * `response_item` + `payload.role == "user"`
    * `event_msg.item_completed.item.type == "UserMessage"`
    纯注入（environment/time/AGENTS）一律不算；历史回显不算；
    结构化选项回复**算**（它有真实 answer）。
    """
    if not isinstance(e, dict):
        return False
    p = e.get("payload") or {}
    if e.get("type") == "response_item" and p.get("role") == "user":
        txt = " ".join(c.get("text", "") for c in (p.get("content") or [])
                       if isinstance(c, dict))
        if not txt.strip():
            return False
        if parse_question_reply(txt) is not None:
            return True          # 真实选项回复
        return not _is_codex_injected(txt) and not _is_codex_echo(txt, typed=False)
    if e.get("type") == "event_msg" and p.get("type") == "item_completed":
        it = p.get("item") or {}
        if it.get("type") == "UserMessage":
            txt = " ".join(c.get("text", "") for c in (it.get("content") or [])
                           if isinstance(c, dict))
            if not txt.strip():
                return False
            if parse_question_reply(txt) is not None:
                return True      # typed 投影的真实选项回复
            return not _is_codex_injected(txt) and not _is_codex_echo(txt, typed=True)
    return False


def _codex_user_text_of(e: dict) -> str | None:
    """从一个真实用户事件里取文本。

    结构化选项回复只取 answer（短答案带上问题做消歧），丢掉协议外壳——
    `questionItemId` 和重复的 question 不是用户意图。
    """
    p = e.get("payload") or {}
    raw = None
    if e.get("type") == "response_item" and p.get("role") == "user":
        raw = " ".join(c.get("text", "") for c in (p.get("content") or [])
                       if isinstance(c, dict)).strip() or None
    elif e.get("type") == "event_msg" and p.get("type") == "item_completed":
        it = p.get("item") or {}
        if it.get("type") == "UserMessage":
            raw = " ".join(c.get("text", "") for c in (it.get("content") or [])
                           if isinstance(c, dict)).strip() or None
    if not raw:
        return None
    parsed = parse_question_reply(raw)
    if parsed is not None:
        return _reply_text(parsed)
    return raw


def _is_codex_injected(text: str) -> bool:
    low = text.lstrip()
    return any(low.startswith(pfx) for pfx in CODEX_INJECT_PREFIXES)


# ---------------------------------------------------------------- DSH 分类
def dsh_user_texts(events: list[dict]) -> tuple[list[str], list[str]]:
    """返回 (真实任务输入, 审批补充)。"""
    tasks: list[str] = []
    approvals: list[str] = []
    for e in events:
        if e.get("type") != "user/message":
            continue
        d = e.get("data") or {}
        kind = (d.get("source") or {}).get("kind")
        text = " ".join(
            c.get("text", "") for c in (d.get("content") or [])
            if isinstance(c, dict) and c.get("type") == "text"
        ).strip()
        if not text:
            continue
        if kind in DSH_USER_KINDS:
            if not tasks or tasks[-1] != text:
                tasks.append(text)
        elif kind in DSH_APPROVAL_KINDS:
            approvals.append(text)
    return tasks, approvals


def _classify_dsh_events(events: list[dict]) -> dict:
    """按原生事件判 DSH 结束状态（事件级，供单文件与多片段共用）。

    与 `_classify_codex_events` 对称：只依赖事件列表，不依赖文件。
    """
    meta = {}
    for e in events:
        if isinstance(e, dict) and e.get("type") == "session":
            meta = e
            break

    last_end, last_end_idx = None, -1
    for i, e in enumerate(events):
        if isinstance(e, dict) and e.get("type") == "turn/end":
            last_end, last_end_idx = e, i

    outcome, evidence = "unfinished", ""
    if last_end is not None:
        reason = (last_end.get("data") or {}).get("reason") or {}
        kind = reason.get("kind") if isinstance(reason, dict) else str(reason)
        if kind == "completed":
            outcome, evidence = "completed", "turn/end.reason.kind=completed"
        elif kind == "aborted":
            outcome, evidence = "aborted", "turn/end.reason.kind=aborted（用户取消）"
        elif kind == "blocked":
            outcome, evidence = "blocked", "turn/end.reason.kind=blocked（等待审批）"
        elif kind == "error":
            err = reason.get("error") or {}
            code = err.get("code")
            if code in DSH_QUOTA_CODES:
                outcome = "quota"
                evidence = f"turn/end.error.code={code}"
            else:
                outcome = "error"
                evidence = f"turn/end.error.code={code or '(未标注)'}"

    # turn/end 之后只要有「新的用户输入」或 turn/start，就说明开了新任务
    if last_end is not None:
        after = events[last_end_idx + 1:]
        new_work = False
        for e in after:
            if not isinstance(e, dict):
                continue
            if e.get("type") == "turn/start":
                new_work = True
                break
            if e.get("type") == "user/message":
                d = e.get("data") or {}
                kind = (d.get("source") or {}).get("kind")
                txt = " ".join(c.get("text", "") for c in (d.get("content") or [])
                               if isinstance(c, dict) and c.get("type") == "text")
                if kind in DSH_USER_KINDS and txt.strip():
                    new_work = True
                    break
        if new_work and outcome in ("completed", "quota", "error", "aborted", "blocked"):
            outcome = "unfinished"
            evidence = ("最后一个 turn/end 之后又出现新的用户输入/turn/start，"
                        "旧状态不再代表当前进度")

    return {"agent": "dsh", "outcome": outcome, "evidence": evidence,
            "meta": meta, "truncated": False}


def classify_dsh(log: Path) -> dict:
    events, truncated = read_dsh_events(log)
    st = _classify_dsh_events(events)
    st["truncated"] = truncated
    st["events"] = events
    st["event_count"] = len(events)
    st["cursor"] = _cursor_of(events)
    return st


def _same_path(a: str, b: str) -> bool:
    """路径比较：容忍 /tmp vs /private/tmp 这类符号链接差异。"""
    if not a or not b:
        return False
    na, nb = str(a).rstrip("/"), str(b).rstrip("/")
    if na == nb:
        return True
    try:
        if Path(na).resolve() == Path(nb).resolve():
            return True
    except OSError:
        pass
    # 去掉 macOS 的 /private 前缀再比
    strip = lambda x: x[8:] if x.startswith("/private/") else x
    return strip(na) == strip(nb)


def _cursor_of(events: list[dict]) -> str:
    """事件游标：最后一条的 seq+time（DSH）或行号（Codex）。"""
    if not events:
        return "empty"
    last = events[-1]
    seq = last.get("seq")
    t = last.get("time") or last.get("timestamp")
    if seq is not None:
        return f"seq={seq}@{t}"
    return f"n={len(events)}@{t}"


# ---------------------------------------------------------------- 候选选择
def _iter_codex_sessions(root: Path):
    """兼容真实布局 YYYY/MM/DD/ 与更浅的 fixture 布局。"""
    seen = set()
    for pattern in ("*/*/*/rollout-*.jsonl", "*/*/rollout-*.jsonl", "*/rollout-*.jsonl"):
        for p in sorted(root.glob(pattern)):
            if p not in seen:
                seen.add(p)
                yield p


def _iter_dsh_sessions(root: Path):
    seen = set()
    for pattern in ("*/*/session.v4.jsonl.zstd", "*/session.v4.jsonl.zstd"):
        for p in sorted(root.glob(pattern)):
            if p not in seen:
                seen.add(p)
                yield p


HEADER_READ_BYTES = 64 * 1024   # header 只在前几行，读这么多足够
HEADER_TIMEOUT = 10             # 秒；解码器异常时不能挂住


def _read_magic(path: Path) -> bytes:
    """只读前 4 字节判断压缩格式——**绝不** read_bytes() 全文。"""
    try:
        with path.open("rb") as fh:
            return fh.read(4)
    except OSError:
        return b""


def _read_header_bounded(path: Path, max_bytes: int = HEADER_READ_BYTES) -> list[dict]:
    """只读日志**开头有限字节**，解析出 header 事件。

    两条硬约束（root 守卫测试）：
    * 不能用 `Path.read_bytes()` —— 那会先读全文；
    * zstd 不能用 `subprocess.run(capture_output=True)` —— 那会解压全文再切片。

    所以压缩流用 Popen 逐块读到「够 header」或上限，然后 terminate + wait 回收。
    """
    import gzip

    magic = _read_magic(path)
    raw = ""

    if magic[:4] == b"\x28\xb5\x2f\xfd":
        raw = _zstd_prefix(path, max_bytes)
    elif magic[:2] == b"\x1f\x8b":
        try:
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                raw = fh.read(max_bytes)
        except (OSError, EOFError, gzip.BadGzipFile):
            return []
    else:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read(max_bytes)
        except OSError:
            return []

    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue          # 截断在边界处，忽略
        if isinstance(obj, dict):
            out.append(obj)
            if len(out) >= 3:
                break
    return out


def _zstd_prefix(path: Path, max_bytes: int) -> str:
    """流式解压 zstd，只取前 max_bytes，然后终止并回收子进程。"""
    import subprocess
    import threading

    try:
        proc = subprocess.Popen(["zstd", "-dc", str(path)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""

    chunks: list[bytes] = []
    total = 0
    done = threading.Event()

    def reader():
        nonlocal total
        try:
            while total < max_bytes:
                block = proc.stdout.read(min(8192, max_bytes - total))
                if not block:
                    break
                chunks.append(block)
                total += len(block)
        except (OSError, ValueError):
            pass
        finally:
            done.set()

    t = threading.Thread(target=reader, daemon=True)
    try:
        t.start()
        done.wait(HEADER_TIMEOUT)

        try:
            proc.terminate()
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
    finally:
        # 子进程已退出后 read() 会返回 EOF，等 reader 真正收尾再关管道。
        # 不 join/不 close 的话，管道对象最后由 GC 回收会抛 ResourceWarning，
        # 而且扫多个会话时会一直占着 FD。
        t.join(timeout=5)
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass

    return b"".join(chunks).decode("utf-8", "replace")


def _is_subagent_source(src) -> bool:
    """判断 `payload.source` 是否表示**子智能体/内部会话**。

    真实形态：
    * `{"subagent": {"other": "guardian"}}`
    * `{"subagent": {"thread_spawn": {"parent_thread_id": "..."}}}`
    主会话是 `"vscode"` / `"cli"` 这类字符串。
    """
    if isinstance(src, dict):
        return "subagent" in src
    return False


def _session_meta_of(agent: str, path: Path) -> dict:
    """只读头部，判断 cwd、native id 与会话来源。

    **Codex 的 id 语义（实测）**：
    * `payload.id` 是**该 thread 自己的原生 id**（每个 rollout 唯一）。
    * `payload.session_id` 是**主会话关联 id**：子智能体/guardian 也沿用主会话的值。
    * `payload.parent_thread_id` 指向父 thread。

    因此 native **必须优先取 `payload.id`**。若先取 `session_id`，同一主会话下的
    子智能体会被误归成主 native，子任务正常 `task_complete` 会把主任务误判 completed，
    候选反复消失。只在 `id` 缺失时才回退 `session_id`。
    """
    events = _read_header_bounded(path)
    for e in events:
        if agent == "codex" and e.get("type") == "session_meta":
            m = e.get("payload") or {}
            pid = m.get("id")
            sid = m.get("session_id")
            return {
                # 自己的 thread id 优先；缺失才兼容 session_id
                "native": pid or sid,
                "cwd": m.get("cwd"),
                # 定位 provenance：主会话关联 id 与父 thread，不当作 native
                "session_id": sid,
                "parent_thread_id": m.get("parent_thread_id"),
                "subagent": _is_subagent_source(m.get("source")),
            }
        if agent == "dsh" and e.get("type") == "session":
            return {"native": e.get("id"), "cwd": e.get("cwd")}
    return {}


def _native_from_name(agent: str, path: Path) -> str | None:
    """header 读不到时，从文件名/目录名回退取 native id。

    真实布局里 codex rollout 名带完整 UUID、dsh 目录名就是 native id。
    这样头部损坏的片段仍能归到同一会话，不会被静默丢掉。
    """
    import re

    if agent == "codex":
        m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                      r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", path.name)
        if m:
            return m.group(1)
        return None
    # dsh: <project>/<native>/session.v4.jsonl.zstd
    name = path.parent.name
    if name and name != "dsh":
        return name
    return None


def _has_real_task(agent: str, events: list[dict]) -> bool:
    """是否存在**真实人类任务输入**。

    只有 header、没有任何用户任务事件的空闲会话，不能当「可接续聊天」：
    它没有目标可继承，接续方无从下手。判定与提取共用同一套识别，
    避免出现「正文写没有任务、状态却 ready」的矛盾。
    """
    if agent == "dsh":
        tasks, _approvals = dsh_user_texts(events)
        return bool(tasks)
    return bool(codex_user_texts(events))


TITLE_FALLBACK_CHARS = 20

# Codex Desktop 的项目注册表（只读）。
# `project_roots.path` 只是**目录登记**：用 cwd 落在某个根**之下**只能推测
# 「这个目录属于哪个项目」，不等于「这个会话归属于该项目」。
# 真实会话归属是 `threads.project_id`（人工移入/移出项目时它变、cwd 不变），
# 但它在本机库里整列为空，所以当前只能退到目录匹配，且必须如实标注。
CODEX_STATE_DB = Path(os.environ.get("BRIDGE_CODEX_STATE_DB")
                      or (Path.home() / ".codex" / "state_5.sqlite"))


def load_project_registry(db_path: Path | None = None) -> dict:
    """读 Codex 的项目/线程归属元数据（只读）。

    返回 dict：
      * `ok`        —— 是否成功读到可用元数据
      * `threads`   —— {thread_id: project_id}（**真实会话归属**，可能为空）
      * `projects`  —— {project_id: name}
      * `roots`     —— [(root_path, project_name)]（**目录匹配**用，非会话归属）
      * `reason`    —— 失败原因（ok=False 时）

    **重要区分**：
    * `threads[id].project_id` 是**会话所属项目**的外键。人工把会话移入/移出项目时
      它变化，而 cwd 不变。有值时必须优先采信。
    * `project_roots` 只是**目录登记**，用 cwd 前缀匹配只能**推测**目录属于哪个项目，
      不能当作会话归属。仅在没有可用 project_id 时作为**明确标注的兼容依据**。
    """
    p = Path(db_path) if db_path else CODEX_STATE_DB
    out = {"ok": False, "threads": {}, "projects": {}, "roots": [], "reason": "",
           "threads_basis": False}
    if not p.is_file():
        out["reason"] = "state db 不存在"
        return out
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        out["reason"] = f"无法只读打开: {exc}"
        return out
    try:
        # 必要表 1/2：projects（id → name）。
        # 读不到 = schema 不兼容 → 元数据不可用，**不能**当成「没有项目」。
        try:
            for pid, name in conn.execute("SELECT id, name FROM projects"):
                if pid:
                    out["projects"][str(pid)] = str(name)
        except sqlite3.Error as exc:
            out["reason"] = f"projects 表不可读: {exc}"
            return out
        # 必要表 2/2：project_roots（目录登记，仅目录匹配用）。
        try:
            for path, pid in conn.execute(
                "SELECT path, project_id FROM project_roots WHERE path IS NOT NULL AND path != ''"
            ):
                nm = out["projects"].get(str(pid))
                if nm:
                    out["roots"].append((str(path).rstrip("/"), nm))
        except sqlite3.Error as exc:
            out["reason"] = f"project_roots 表不可读: {exc}"
            return out
        # 可选兼容来源：threads.project_id（真实会话归属，可能整列为空或缺列）。
        # 缺列/无表只是「拿不到更权威的归属」，不影响目录登记可用性。
        try:
            for tid, pid in conn.execute(
                "SELECT id, project_id FROM threads WHERE project_id IS NOT NULL AND project_id != ''"
            ):
                if tid and pid:
                    out["threads"][str(tid)] = str(pid)
            out["threads_basis"] = True
        except sqlite3.Error:
            out["threads_basis"] = False
        # 两张必要表都读到了：即使全为空，也是「合法的空注册表」，不是错误。
        out["ok"] = True
        return out
    except sqlite3.Error as exc:
        out["reason"] = f"查询失败: {exc}"
        return out
    finally:
        conn.close()


def load_project_roots(db_path: Path | None = None) -> list[tuple[str, str]]:
    """兼容入口：只返回目录登记表（roots）。

    注意这是**目录匹配**依据，不是会话归属。需要真实归属请用
    `load_project_registry()` 并读 `threads`。
    """
    return load_project_registry(db_path)["roots"]


def project_of(cwd: str, roots: list[tuple[str, str]]) -> tuple[str, str] | None:
    """cwd 落在哪个**注册目录根**之下；返回 (root, name)，否则 None。

    这是**目录匹配**，不是会话归属：只说明 cwd 位于某个登记目录内，
    不代表该会话被归入该项目（会话可被人工移入/移出项目而不改 cwd）。

    用路径边界匹配（`/a/b` 不匹配 `/a/bc`），嵌套时取最长根。
    不把「不是 home」当作项目，也不按目录名/日期形状推断。

    **同一路径登记给多个项目时返回 None。** 这种情况（实测本机存在：
    `.../Liner-transformer` 同时挂在「论文复现」和「PPT制作」下）没有任何
    可靠依据能判定该 cwd 属于哪个项目——注册表里两个根的路径长度相同，
    谁先出现只取决于 SQLite 的返回顺序。此时**猜一个**等于让行序静默决定
    归属，与本模块「归属未知就标未知」的原则冲突。调用方据此标
    `basis='ambiguous'` 并如实提示。
    """
    if not cwd:
        return None
    c = str(cwd).rstrip("/")
    best = None
    for root, name in roots:
        if c == root or c.startswith(root + "/"):
            if best is None or len(root) > len(best[0]):
                best = (root, name)
    if best is None:
        return None
    # 同一个最长根登记给了多个项目 → 没有可靠依据，宁可说不知道
    names = {n for r, n in roots if r == best[0]}
    if len(names) > 1:
        return None
    return best

# 面向用户的「阶段」标识（描述**本轮**，不是整个任务）。
# 不存在「已完成的任务」——只区分本轮怎么结束的。
_PHASE_LABEL = {
    "settled": "本轮已收尾",
    "interrupted": "因故中断",
    "unsettled": "未收尾/需核验",
}

# 有**明确中断证据**的终态。其余（尤其 unfinished）不硬说成中断。
_INTERRUPTED_OUTCOMES = {"quota", "error", "aborted", "blocked"}


def _round_phase(outcome: str) -> str:
    """把底层 outcome 归并成三档面向用户的阶段。

    * `completed` → `settled`：本轮正常收尾，后续可能还有新需求。
    * `quota` / `error` / `aborted` / `blocked` → `interrupted`：
      有明确的中断证据。
    * 其余（`unfinished` 等）→ `unsettled`：**缺终态不等于一定崩溃**，
      可能只是没记录结束。诚实地说「未收尾/需核验」，不猜是否仍在运行。
    """
    if outcome == "completed":
        return "settled"
    if outcome in _INTERRUPTED_OUTCOMES:
        return "interrupted"
    return "unsettled"



def load_titles(index_path: Path | None = None) -> dict[str, str]:
    """读 Codex 聊天命名索引，返回 native_id → thread_name。

    同一 id 可能有多条历史记录，**按 updated_at 取最新**，不能假定 JSONL
    行顺序（实测尾部还可能有损坏行）。索引缺失或损坏时返回空表，调用方回退。
    """
    import json as _json

    p = Path(index_path) if index_path else CODEX_INDEX
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    best: dict[str, tuple[str, str]] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = _json.loads(line)
        except ValueError:
            continue                      # 损坏行跳过，不影响其余
        if not isinstance(row, dict):
            continue
        ident = row.get("id")
        name = row.get("thread_name")
        if not isinstance(ident, str) or not isinstance(name, str) or not name.strip():
            continue
        stamp = row.get("updated_at") or ""
        prev = best.get(ident)
        if prev is None or str(stamp) > prev[0]:
            best[ident] = (str(stamp), name.strip())
    return {k: v[1] for k, v in best.items()}


def _short_id(native: str) -> str:
    """短 id：取 UUID 前 8 位，便于人读；完整 id 仍在 native_id 字段。"""
    bare = native[len("session-"):] if native.startswith("session-") else native
    return bare[:8]


def _clip_text(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _resolve_project(native: str, cwd: str, registry: dict | None) -> tuple:
    """决定一条会话的项目归属，返回 (name, root, basis)。

    优先级（严格按可靠性）：
    1. `threads.project_id` → 项目名。这是**会话所属项目**，权威；
       即使 cwd 在 home/日期目录/别的根下，也以它为准。
    2. 元数据可读但没有该 thread 的 project_id → 退回**目录匹配**，
       basis 标 `dir`，明确它只是「cwd 落在登记目录内」，不是会话归属。
    3. 元数据**不可读** → basis 标 `unavailable`，project 为 None。
       这表示**归属未知**，绝不能当作「确认非项目」。
    4. cwd 落在某个根下，但**该根同时登记给多个项目** → basis 标
       `ambiguous`，project 为 None。同样是「归属未知」：没有可靠依据，
       不能让注册表行序决定归属。
    """
    if registry is None:
        return (None, None, "unavailable")
    if not registry.get("ok"):
        return (None, None, "unavailable")

    # 1) 真实会话归属
    pid = (registry.get("threads") or {}).get(native)
    if pid:
        name = (registry.get("projects") or {}).get(pid)
        if name:
            return (name, None, "thread")

    # 2) 目录匹配（明确标注，非权威）
    roots = registry.get("roots") or []
    hit = project_of(cwd, roots)
    if hit:
        return (hit[1], hit[0], "dir")

    # 3) 落在某根下但该根登记给多个项目 → 歧义，不猜
    c = str(cwd or "").rstrip("/")
    if c:
        covered = [r for r, _n in roots if c == r or c.startswith(r + "/")]
        if covered:
            return (None, None, "ambiguous")

    return (None, None, "none")


def _candidate_row(agent: str, native: str, paths: list[Path],
                   titles: dict[str, str],
                   registry: dict | None = None) -> dict | None:
    """构造一条**可读**候选（只有摘要与标识，不含恢复正文）。

    返回 None 表示不该列入：无真实任务（空白/仅系统注入/内部子智能体）。
    本轮已收尾的**仍然列出**——没有「已完成的任务」，后续还会有新需求。
    """
    if len(paths) > 1:
        merged, order_ok, tr = _merge_fragments(agent, paths)
        st = _classify_events(agent, merged, order_ok, tr)
        events = merged
    else:
        st = classify_dsh(paths[0]) if agent == "dsh" else classify_codex(paths[0])
        events = st["events"]

    if not _has_real_task(agent, events):
        return None                        # 空闲/仅 header：不列

    # **没有「已完成的任务」**：日志里只有「这一轮正常结束」和「因故中断」。
    # `completed` 只说明最后一轮没异常，不代表用户目标达成——后续还会有新需求。
    # 所以不再过滤，改为标注，让用户自己判断要不要接着做。
    phase = _round_phase(st["outcome"])

    if agent == "dsh":
        tasks, _ap = dsh_user_texts(events)
    else:
        tasks = codex_user_texts(events)

    meta = _session_meta_of(agent, paths[0])
    _cwd = meta.get("cwd") or ""
    proj_name, proj_root, proj_basis = _resolve_project(native, _cwd, registry)
    times = [x for x in (_event_time(e) for e in events) if x is not None]
    title = titles.get(native) or f"未命名会话 · {_short_id(native)}"
    last_us = max(times) if times else None
    # 摘要与标题都要过一遍清洗：候选可能被前端直接渲染或喂给模型，
    # 不能把 api_key / token 这类凭据带出去。
    return {
        "agent": agent,
        "native_id": native,
        "title": scrub(_clip_text(title, 80)),
        "cwd": meta.get("cwd") or "",
        # 项目归属。`project_basis` 说明这个值是怎么来的：
        #   thread = Codex 记录的会话所属项目（权威，人工移动会变）
        #   dir    = 注册目录根 + cwd 边界匹配（**目录匹配，非会话归属**）
        #   none   = 未命中任何依据
        #   unavailable = 元数据不可读，归属**未知**（不等于「确认非项目」）
        "project": proj_name,
        "project_root": proj_root,
        "project_basis": proj_basis,
        # 列表分组：project=已归入项目；unassigned=未归入/未知
        "scope": "project" if proj_name else "unassigned",
        "last_activity": _iso_from_us(last_us) if last_us is not None else "",
        # 排序用：保留完整微秒精度，避免同一秒内的先后被格式化抹平
        "last_activity_us": last_us,
        "goal_head": scrub(_clip_text(tasks[0], 160)) if tasks else "",
        "goal_tail": scrub(_clip_text(tasks[-1], 160)) if tasks else "",
        "fragments": len(paths),
        "outcome": st["outcome"],
        # **来源可用性**：available=可作为来源；incomplete=来源待核验。
        # 不能用 unfinished 表达「本轮已收尾」——那是 phase 的职责。
        "status": "available",
        # 本轮阶段：settled / interrupted / unsettled（与 resume 正文同一套语义）
        "phase": phase,
        "phase_label": _PHASE_LABEL.get(phase, ""),
    }


def _iso_from_us(us: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(us / 1e6, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_candidates(root: Path, cwd: str, agent: str, all_cwds: bool,
                     titles: dict[str, str] | None = None) -> dict:
    """列出可交接会话（**只有摘要，不含恢复正文**）。

    `all_cwds=False` 时只列 header cwd 等于 `cwd` 的会话——普通 resume 的
    当前目录边界不变；只有显式 `--all-cwds` 才跨目录。
    """
    root = Path(root)
    titles = titles if titles is not None else (load_titles() if agent == "codex" else {})
    # 项目归属只在 codex 侧有来源；dsh 侧无 → 明确标 unavailable
    registry = load_project_registry() if agent == "codex" else None
    it = _iter_codex_sessions(root) if agent == "codex" else _iter_dsh_sessions(root)

    # 按 **(native, cwd)** 归组，不能只按 native。
    # 同一个 native 可能在多个 cwd 都有有效 header（续接换了目录），
    # 混成一条会把两个 scope 的 head/tail 拼在一起、cwd 只取一个，
    # 等于假装单一来源 scope 已确定。全局列表允许跨项目，但不允许混淆来源。
    by_scope: dict[tuple[str, str], list[Path]] = {}
    unproven = []
    skipped_subagent = 0
    for p in it:
        m = _session_meta_of(agent, p)
        native = m.get("native")
        mc = m.get("cwd") or ""
        if not native or not mc:
            # header 读不出归属：不解析正文，只记诊断
            unproven.append({"log": str(p),
                             "native_id": native or _native_from_name(agent, p)})
            continue
        # 自动候选是**聊天来源**：明确标为子智能体/内部会话的不列入。
        # 用户显式指定时仍可按其自身 id 精确选择（见 select_candidate）。
        if m.get("subagent"):
            skipped_subagent += 1
            continue
        if not all_cwds and not _same_path(mc, cwd):
            continue
        by_scope.setdefault((native, mc), []).append(p)

    rows = []
    # 同一 native 出现在多个 scope 时，该 native 的候选要显式标注来源不唯一
    per_native: dict[str, int] = {}
    for (native, _mc) in by_scope:
        per_native[native] = per_native.get(native, 0) + 1
    for (native, mc), paths in by_scope.items():
        paths.sort(key=lambda x: x.name)
        row = _candidate_row(agent, native, paths, titles, registry)
        if row is None:
            continue
        row["cwd"] = mc
        if per_native.get(native, 0) > 1:
            # 同一 native 跨多个 cwd：来源不唯一，提示调用方按 native+cwd 精确选择
            row["status"] = "incomplete"   # 来源异常：待核验，与 phase 无关
            row["note"] = ("同一 native 在多个 cwd 都有有效 header，"
                           "本条只代表该 cwd 的来源；请按 native_id + cwd 精确选择")
        rows.append(row)

    # 严格按最后活动时间**微秒倒序**；阶段只做标识，不参与排序
    # （否则两天前中断的会排在刚刚聊完的前面）。无时间的排最后。
    rows.sort(key=lambda r: (
        r.get("last_activity_us") is None,
        -(r.get("last_activity_us") or 0),
    ))
    # badge = **可接续聊天数**（按 native 去重），不是 scope 行数。
    # 含「本轮已收尾」的聊天——没有「已完成的任务」，后续还会有新需求。
    # 同一 native 跨两个 cwd 会有两行，但用户看到的是「一个聊天」，
    # 计 2 会让徽标虚高。incomplete 只是**来源待核验**（不是「未完成」），
    # 这类行仍列在 sessions 里，可按 native 去重计入。
    badge = len({r["native_id"] for r in rows})
    # 元数据不可读时明确提示：此时的「非项目」只是未知，不是确认结果
    if agent == "codex" and registry is not None and not registry.get("ok"):
        unproven = list(unproven) + [{
            "kind": "project-metadata-unavailable",
            "reason": registry.get("reason") or "未知原因",
            "note": "项目归属元数据不可读，列表中的「非项目会话」为未确认归属，不代表确实不属于任何项目",
        }]
    # 有会话落在「同时登记给多个项目」的根下：同样只是未知，不是确认非项目
    ambiguous = [r for r in rows if r.get("project_basis") == "ambiguous"]
    if ambiguous:
        unproven = list(unproven) + [{
            "kind": "project-ambiguous",
            "count": len(ambiguous),
            "note": "有会话所在的目录同时登记给多个项目，无法判定归属；"
                    "这些会话列在非项目会话中，不代表确实不属于任何项目",
        }]
    return {"sessions": rows, "total": len(rows), "badge": badge,
            "warnings": unproven,
            "project_basis": ("thread" if (registry or {}).get("threads")
                              else ("dir" if (registry or {}).get("roots")
                                    else ("none" if (registry or {}).get("ok")
                                          else "unavailable")))}

def select_candidate(root: Path, cwd: str, agent: str, session: str | None) -> dict:
    """挑一个候选。**不猜 latest**。"""
    root = Path(root)
    it = _iter_codex_sessions(root) if agent == "codex" else _iter_dsh_sessions(root)
    all_paths = list(it)

    if session:
        # 只接受完整 native id（或去掉 session- 前缀的完整 UUID），不接受子串
        want = session if session.startswith("session-") else f"session-{session}"
        bare = want[len("session-"):]
        exact, broken = [], []
        for p in all_paths:
            m = _session_meta_of(agent, p)
            native = m.get("native")
            if native in (session, want, bare):
                exact.append((p, m))
            elif not native and _native_from_name(agent, p) in (session, want, bare):
                # header 缺失/损坏：仅按文件名精确命中候选，**不解析正文**，
                # 等下面用「同 native 有有效 header 证明归属」来决定是否可收
                broken.append((p, m))
        if not exact and not broken:
            return {"status": "no-candidate", "candidates": [],
                    "reason": f"没有 native id 完全等于 {session} 的会话"}
        # 只有**有界有效 header** 才能证明归属：
        #   - header 里 native 精确匹配 且 cwd 与请求一致 → 允许
        #   - header 缺失/损坏 → 不能凭文件名 UUID 跨 cwd 拉入本项目
        #     仅当同 native 已有有效 header 证明属于本 cwd 时，才收作
        #     「需人工核对的片段」，并标 cwd 未校验
        same, cwd_unverified = [], False
        proven_native = None
        for p, m in exact:
            mc = m.get("cwd") or ""
            if _same_path(mc, cwd):
                same.append((p, m))
                proven_native = m.get("native") or proven_native
        if not same:
            got = exact[0][1].get("cwd") if exact else ""
            detail = (f"会话 {session} 属于 {got}，与请求的 {cwd} 不符"
                      if got else
                      f"会话 {session} 的 header 缺失/损坏，无法证明属于 {cwd}；"
                      f"已拒绝（未解析正文）")
            return {"status": "no-candidate", "candidates": [],
                    "reason": detail,
                    "unproven_logs": [{"log": str(p), "native_id": session}
                                      for p, _m in broken]}
        # 同 native 的损坏片段：有有效 header 证明归属时才并入，且标未校验
        for p, m in exact:
            if (p, m) in same:
                continue
            if not (m.get("cwd") or "") and proven_native:
                same.append((p, m))
                cwd_unverified = True
        for p, m in broken:
            if proven_native:
                same.append((p, m))
                cwd_unverified = True
        # 同一 native 可能有多个片段（续接会新建 rollout）。
        # 交给 build_resume 按**原生事件时间**合并，不能只看文件 mtime。
        # 返回 **header 里的真实完整 native id**（裸 UUID 别名不能直接当结果）。
        real_native = (same[0][1].get("native") or session)
        paths = [x[0] for x in same]
        # 用户**显式点名**了会话，就不在候选层悄悄跳过：如实返回 incomplete，
        # 让调用方知道「这个会话没有可继承的目标」，而不是给一个空壳 ready。
        tr = False
        st = None
        if len(paths) > 1:
            merged, order_ok, tr = _merge_fragments(agent, paths)
            if not _has_real_task(agent, merged):
                return {"status": "incomplete", "path": paths,
                        "native_id": real_native, "multi": True, "order_ok": order_ok,
                        "truncated": tr, "cwd_unverified": cwd_unverified,
                        "reason": "该会话没有真实人类任务输入（只有 header/系统注入），"
                                  "没有可继承的目标；请补充任务目标后再接续"}
        else:
            st = classify_dsh(paths[0]) if agent == "dsh" else classify_codex(paths[0])
            if not _has_real_task(agent, st["events"]):
                return {"status": "incomplete", "path": paths[0],
                        "native_id": real_native, "multi": False,
                        "truncated": st.get("truncated", False),
                        "cwd_unverified": cwd_unverified,
                        "reason": "该会话没有真实人类任务输入（只有 header/系统注入），"
                                  "没有可继承的目标；请补充任务目标后再接续"}
        return {"status": "ready", "path": paths,
                "native_id": real_native, "multi": len(paths) > 1,
                "truncated": (tr if len(paths) > 1 else (st or {}).get("truncated", False)),
                "cwd_unverified": cwd_unverified}
    matched = []
    orphans = []          # header 缺失/损坏的片段：先不解析，等 scope 证据
    for p in all_paths:
        m = _session_meta_of(agent, p)
        mc = m.get("cwd") or ""
        native = m.get("native")
        # 自动候选是**聊天来源**：明确标为子智能体/内部会话的不列入。
        # 显式 --session 仍可按它自身 id 精确选择（见上面的 session 分支）。
        if m.get("subagent"):
            continue
        if native and _same_path(mc, cwd):
            # 唯一可信来源：**有界有效 header** 证明该 native 属于本 cwd
            matched.append((p, native))
        elif not native or not mc:
            # header 读不出 native 或 cwd。**不得**凭文件名 UUID 拉进本项目，
            # 也不得全量解析其正文（可能属于别的项目）。
            orphans.append((p, native or _native_from_name(agent, p)))

    # 同 native 的孤儿片段：只有在「同 native 已被有效 header 证明属于本 cwd」
    # 时才收进来，并标记 cwd 未校验、必须人工核对。否则只作诊断，不输出正文。
    proven = {n for _p, n in matched}
    adopted, unproven = [], []
    for p, native in orphans:
        if native and native in proven:
            adopted.append(p)
        else:
            unproven.append({"log": str(p), "native_id": native})

    # 同一 native 可能有多个片段（续接/续写会新建 rollout 文件）。
    # **不能只按 st_mtime 留一个**：旧片段被 touch 过就会盖掉新进度。
    # 这里保留全部片段，交给 build_resume 按原生事件时间合并。
    by_native: dict[str, list[Path]] = {}
    unverified = bool(adopted)
    for p, native in matched:
        by_native.setdefault(native, []).append(p)
    for p in adopted:
        by_native.setdefault(_session_meta_of(agent, p).get("native")
                             or _native_from_name(agent, p), []).append(p)
    for native in by_native:
        by_native[native].sort(key=lambda x: x.name)

    # 可接续 = **有真实目标的普通主聊天**，包含「本轮已收尾」的。
    # 没有「已完成的任务」：收尾只是这一轮结束，后续还会有新需求。
    # 排除的是**没有任何真实任务输入**的空闲会话：它没有目标可继承，
    # 不该出现在候选里逼调用方消歧。
    # 没有「已完成的任务」：收尾只是这一轮结束，后续还会有新需求。
    # 空白 / 仅系统注入 / 内部子智能体仍不提供继承目标（已在上面排除）。
    usable = []
    idle = []
    for native, paths in by_native.items():
        if len(paths) > 1:
            merged, order_ok, tr = _merge_fragments(agent, paths)
            st = _classify_events(agent, merged, order_ok, tr)
            events = merged
        else:
            st = classify_dsh(paths[0]) if agent == "dsh" else classify_codex(paths[0])
            events = st["events"]
        if not _has_real_task(agent, events):
            idle.append(native)
            continue
        usable.append((native, paths, st))
    cands = usable
    if not cands:
        reason = f"{cwd} 下没有 {agent} 会话"
        if idle:
            reason = (f"{cwd} 下有 {len(idle)} 个 {agent} 会话，"
                      f"但都没有真实任务输入（空闲/仅 header），没有可继承的目标")
        elif unproven:
            reason = (f"{cwd} 下没有可确认归属的 {agent} 会话；"
                      f"有 {len(unproven)} 个 header 缺失/损坏的日志无法证明属于本项目，"
                      f"已跳过（未解析正文）")
        return {"status": "no-candidate", "candidates": [],
                "reason": reason, "unproven_logs": unproven}
    if len(cands) > 1:
        titles = load_titles() if agent == "codex" else {}
        registry = load_project_registry() if agent == "codex" else None
        rows = []
        for n, ps, _st in cands:
            row = _candidate_row(agent, n, ps, titles, registry)
            if row is None:            # 理论上不会（cands 已过滤），兜底
                row = {"agent": agent, "native_id": n, "title": f"未命名会话 · {_short_id(n)}",
                       "cwd": "", "last_activity": "", "goal_head": "", "goal_tail": "",
                       "fragments": len(ps), "outcome": "unfinished", "status": "available"}
            rows.append(row)
        rows.sort(key=lambda r: r["last_activity"], reverse=True)
        return {"status": "needs-selection",
                "candidates": rows,
                "reason": f"{cwd} 下有 {len(cands)} 个 {agent} 会话，请用 --session 指定"}
    native, paths, st = cands[0]
    return {"status": "ready", "path": paths if len(paths) > 1 else paths[0],
            "native_id": native, "multi": len(paths) > 1,
            "order_ok": st.get("order_ok", True),
            "truncated": st.get("truncated", False),
            "cwd_unverified": unverified}


# ---------------------------------------------------------------- DB 只读校验
def open_db_ro(db_path: Path | None = None):
    p = Path(db_path) if db_path else DB_PATH
    if not p.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def db_context(conn, ws: str, proj: str, agent: str, native: str) -> dict:
    """按 workspace/project/agent/session 精确取 DB 侧事实。"""
    if conn is None:
        return {"available": False}
    canon = canonical_id(native)
    try:
        row = conn.execute(
            """SELECT s.agent_kind, s.cwd, s.ended_at FROM sessions s
               JOIN projects p ON s.project_id = p.id
               JOIN workspaces w ON s.workspace_id = w.id
               WHERE w.name = ? AND p.name = ? AND s.id = ?""",
            (ws, proj, uuid.UUID(canon).bytes),
        ).fetchone()
        info: dict = {"available": True, "session_in_db": row is not None}
        if row:
            info["agent_kind"] = row[0]
            info["cwd"] = row[1]
            info["ended_at_us"] = row[2]
            info["agent_matches"] = (row[0] == agent)
        # 该会话在此 scope 的最后成功工具观测
        info["last_obs_us"] = conn.execute(
            """SELECT MAX(created_at) FROM observations
               WHERE session_id = ? AND workspace_id = (SELECT id FROM workspaces WHERE name=?)
                 AND project_id = (SELECT id FROM projects WHERE name=? AND workspace_id=(SELECT id FROM workspaces WHERE name=?))""",
            (uuid.UUID(canon).bytes, ws, proj, ws),
        ).fetchone()[0]
        # 存档页（同 scope）
        bare = native.removeprefix("session-")
        prow = conn.execute(
            """SELECT pg.created_at FROM pages pg
               JOIN projects p ON pg.project_id = p.id
               JOIN workspaces w ON pg.workspace_id = w.id
               WHERE w.name = ? AND p.name = ? AND pg.path = ? AND pg.is_latest = 1""",
            (ws, proj, f"_autosave/{agent}-{bare}.md"),
        ).fetchone()
        info["autosave_us"] = prow[0] if prow else None
        return info
    except sqlite3.Error as exc:
        return {"available": True, "error": str(exc)}


# ---------------------------------------------------------------- 构建正文
def build_resume(path, native: str, cwd: str, ws: str, proj: str,
                 db_path: Path | None = None, agent: str | None = None,
                 cwd_unverified: bool = False) -> dict:
    """构建恢复上下文正文（只读）。

    `path` 可以是单个 Path，也可以是**同一 native 的多个片段**列表。
    多片段按原生事件时间排序合并——不能只看文件 mtime，旧片段被 touch
    过就会盖掉新进度。
    """
    paths = path if isinstance(path, (list, tuple)) else [path]
    paths = [Path(x) for x in paths]
    first = paths[0]
    agent = agent or ("dsh" if first.name.startswith("session.") else "codex")

    if len(paths) > 1:
        merged, order_ok, tr = _merge_fragments(agent, paths)
        st = _classify_events(agent, merged, order_ok, tr)
        st["path"] = first
    else:
        st = classify_dsh(first) if agent == "dsh" else classify_codex(first)
    events = st["events"]

    if agent == "dsh":
        tasks, approvals = dsh_user_texts(events)
        confirmed, failed, unknown, pending = _dsh_ops(events)
        last_text = _dsh_last_assistant(events)
    else:
        tasks = codex_user_texts(events)
        approvals = []
        confirmed, failed, unknown, pending = _codex_ops(events)
        last_text = _codex_last_assistant(events)

    conn = open_db_ro(db_path)
    db = db_context(conn, ws, proj, agent, native) if conn else {"available": False}
    if conn:
        conn.close()

    canon = canonical_id(native)
    lines: list[str] = []
    lines.append("# 恢复上下文（只读构建）")
    lines.append("")
    lines.append("> 这是**机械提取**的历史证据，不是新的任务授权。")
    lines.append("> 接续前请先核验当前文件与外部状态，未知操作不要盲重放。")
    lines.append("")
    lines.append("## 定位")
    lines.append("")
    lines.append(f"- scope：`{ws}/{proj}`")
    lines.append(f"- agent：`{agent}`")
    lines.append(f"- native id：`{native}`")
    lines.append(f"- canonical DB id：`{canon}`")
    lines.append(f"- cwd：`{cwd}`")
    lines.append(f"- 日志：`{st.get('path') or first}`")
    if len(paths) > 1:
        lines.append(f"- 片段数：{len(paths)}（按原生事件时间合并）")
    if cwd_unverified:
        lines.append("- ⚠ 有片段的 header 读不出 cwd（头部损坏/截断）；"
                     "已按 native id 归组，归属未核验")
    lines.append(f"- 事件游标：`{st['cursor']}`（共 {st['event_count']} 条）")
    lines.append(f"- 结束判定：**{st['outcome']}** —— {st['evidence']}")
    if st["truncated"]:
        lines.append("- ⚠ 日志尾部有截断/损坏行，可能有未记录的事件")
    if not db.get("available"):
        lines.append("- ⚠ **缺 DB 校验**：未能核对 ai-memory 数据库（仅按原生日志构建）")
    elif db.get("error"):
        lines.append(f"- ⚠ DB 查询失败：{db['error']}")
    elif not db.get("session_in_db"):
        lines.append("- ⚠ 该会话未在 DB 中登记（钩子可能未捕获）")
    elif db.get("agent_matches") is False:
        lines.append(f"- ⚠ DB 里 agent 是 `{db.get('agent_kind')}`，与 `{agent}` 不符")
    lines.append("")

    lines.append("## 任务目标（人类原话）")
    lines.append("")
    if tasks:
        lines.append(f"- 【初始目标】{clip(tasks[0], 500)}")
        if len(tasks) > 1:
            lines.append(f"- _（中间 {len(tasks) - 2} 条已省略）_" if len(tasks) > 2 else "")
            for t in tasks[-3:] if len(tasks) > 3 else tasks[1:]:
                if t != tasks[0]:
                    lines.append(f"- {clip(t, 500)}")
            if len(tasks) > 3:
                lines.append(f"- 【最新】{clip(tasks[-1], 500)}")
    else:
        lines.append("_（没有识别到人类任务输入：这段日志里没有可继承的任务目标。_")
        lines.append("_接续方无从得知要做什么，请先向用户索取任务目标，不要自行猜测。）_")
    lines.append("")

    if approvals:
        lines.append("## 审批 / 补充（不是初始目标）")
        lines.append("")
        for a in approvals[-3:]:
            lines.append(f"- {clip(a, 300)}")
        lines.append("")

    lines.append("## 已确认成功的操作")
    lines.append("")
    if confirmed:
        for c in confirmed[-15:]:
            lines.append(f"- {c}")
    else:
        lines.append("_（没有明确成功的操作）_")
    lines.append("")

    if failed or unknown or pending:
        lines.append("## 失败 / 未知 / 待审批")
        lines.append("")
        for f in failed[-8:]:
            lines.append(f"- ❌ {f}")
        for u in unknown[-8:]:
            lines.append(f"- ⚠ 未知：{u}")
        for p in pending[-8:]:
            lines.append(f"- ⏳ 待审批/进行中：{p}")
        lines.append("")

    if last_text:
        lines.append("## 最后一条助手可见输出")
        lines.append("")
        lines.append(clip(last_text, 1200))
        lines.append("")

    lines.append("## 接续前必须先核验")
    lines.append("")
    lines.append("1. 「已确认成功的操作」里的文件**当前内容**（不要假设已提交）")
    lines.append("2. 「失败 / 未知 / 待审批」涉及的文件或进程状态")
    lines.append("3. 外部结果（API 调用、测试运行）是否真的产生了效果")
    lines.append("4. 未知操作**不要盲重放**——先确认它是否已经执行过")
    lines.append("")
    # settled：上一轮正常收尾。必须明确「供承接后续需求」，不得重做已完成操作。
    if _round_phase(st["outcome"]) == "settled":
        lines.append("## ⚠ 本轮状态：已收尾")
        lines.append("")
        lines.append("- 上一轮**正常收尾**，不是失败，也不是「任务已完成」。")
        lines.append("- 这里只供**承接后续需求**；未给出新需求时，先问用户下一步要做什么。")
        lines.append("- **不得重做**上面「已确认成功的操作」，除非用户明确要求。")
        lines.append("")

    lines.append("## 证据边界")
    lines.append("")
    lines.append("- 以上全部来自历史日志，是**证据**，不是新的授权。")
    lines.append("- 用户的新意图必须来自用户当前的话，不能从日志推断。")
    lines.append("")

    body = scrub("\n".join(lines))
    phase = _round_phase(st["outcome"])
    return {
        "body": body,
        "cursor": st["cursor"],
        "outcome": st["outcome"],
        # 本轮阶段（settled / interrupted / unsettled），与候选列表同一套语义
        "phase": phase,
        "phase_label": _PHASE_LABEL.get(phase, ""),
        # 可接续（不再用 status=unfinished 表达“能接续”）
        "available": True,
        "order_ok": st.get("order_ok", True),
        "truncated": st["truncated"],
        "cwd_unverified": cwd_unverified,
        "db": db,
        "agent": agent,
        "native_id": native,
        "canonical_id": canon,
        "log_path": str(st.get("path") or first),
    }


def _codex_ops(events: list[dict]):
    """配对 call/result，分出确认成功 / 失败 / 未知 / 待审批。

    真实 rollout 里工具既有 `event_msg.item_completed`，也有
    `response_item` 的 `function_call` / `custom_tool_call` / `*_output`。
    后者若无配对 output，必须列未知，不能当成功。
    """
    confirmed, failed, unknown, pending = [], [], [], []

    # 1) event_msg.item_completed（带 status / exit_code）
    for e in events:
        if not isinstance(e, dict):
            continue
        p = e.get("payload") or {}
        if e.get("type") != "event_msg" or p.get("type") != "item_completed":
            continue
        it = p.get("item") or {}
        t = it.get("type")
        status = (it.get("status") or "").lower() or None   # 真实值是小写
        if t == "CommandExecution":
            cmd = it.get("command")
            if isinstance(cmd, list):
                cmd = " ".join(str(x) for x in cmd)
            cmd = clip(str(cmd), 160)
            ec = it.get("exit_code")
            if status == "completed" and ec == 0:
                confirmed.append(f"`{cmd}`（exit 0）")
            elif status == "completed" and ec not in (None, 0):
                failed.append(f"`{cmd}`（exit {ec}）")
            elif status == "failed":
                failed.append(f"`{cmd}`（失败）")
            else:
                unknown.append(f"`{cmd}`（状态 {status or '未标注'}）")
        elif t == "FileChange":
            paths = list((it.get("changes") or {}).keys())
            desc = "、".join(f"`{x}`" for x in paths[:3])
            if status == "completed":
                confirmed.append(f"改文件 {desc}")
            elif status == "failed":
                failed.append(f"改文件 {desc}（失败）")
            else:
                unknown.append(f"改文件 {desc}（状态 {status or '未标注'}）")

    # 2) response_item 的 function_call / custom_tool_call 及其 output
    calls: dict[str, dict] = {}
    outputs: set[str] = set()
    for e in events:
        if not isinstance(e, dict):
            continue
        p = e.get("payload") or {}
        if e.get("type") != "response_item":
            continue
        t = p.get("type")
        if t in ("function_call", "custom_tool_call"):
            cid = p.get("call_id") or p.get("id")
            if cid:
                calls[cid] = p
        elif t in ("function_call_output", "custom_tool_call_output"):
            cid = p.get("call_id")
            if cid:
                outputs.add(cid)

    for cid, p in calls.items():
        name = p.get("name") or "?"
        raw = p.get("arguments") or p.get("input") or ""
        text = clip(str(raw), 160)
        if cid in outputs:
            unknown.append(f"{name} `{text}`（有输出但未标注成败，请核验）")
        else:
            unknown.append(f"{name} `{text}`（**无配对结果**，可能未完成，不要盲重放）")
    return confirmed, failed, unknown, pending


def _dsh_ops(events: list[dict]):
    """配对 call/result，分出确认成功 / 失败 / 未知 / 待审批。

    严格规则：
    * `isError` **缺失**时不能算确认成功（只是「有响应」）。
    * bash 要看 `job/settled` 的 status/detail 判 exit；没有 settled 就是未知。
    * `todo_write` 未配对 result 不能把 completed 当已做。
    """
    confirmed, failed, unknown, pending = [], [], [], []
    calls: dict[str, dict] = {}
    results: dict[str, dict] = {}
    jobs: dict[str, dict] = {}

    for e in events:
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        d = e.get("data") or {}
        if t == "tool/call":
            calls[d.get("callId")] = d
        elif t == "tool/result":
            m = d.get("message") or {}
            cid = m.get("toolCallId") or d.get("toolCallId")
            if cid:
                results[cid] = m
        elif t == "job/settled":
            tc = d.get("toolCall") or {}
            cid = tc.get("callId")
            if cid:
                jobs[cid] = d

    write_tools = {"write", "edit", "multi_edit", "notebook_edit"}
    for cid, c in calls.items():
        name = c.get("name")
        try:
            args = json.loads(c.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        res = results.get(cid)
        job = jobs.get(cid)

        if name in write_tools:
            target = args.get("file_path") or args.get("notebook_path") or "?"
            if res is None:
                unknown.append(f"改文件 `{target}`（无结果，可能未完成）")
            elif res.get("isError") is True:
                failed.append(f"改文件 `{target}`（工具报错）")
            elif res.get("isError") is False:
                confirmed.append(f"改文件 `{target}`")
            else:
                unknown.append(f"改文件 `{target}`（响应未标注 isError，不能确认成功）")

        elif name == "bash":
            cmd = clip(str(args.get("command") or ""), 140)
            status = (job or {}).get("status")
            detail = str((job or {}).get("detail") or "")
            m = re.search(r"exit code:\s*(-?\d+)", detail)
            ec = int(m.group(1)) if m else None
            if status == "completed" and ec == 0:
                confirmed.append(f"命令 `{cmd}`（exit 0）")
            elif ec not in (None, 0):
                failed.append(f"命令 `{cmd}`（exit {ec}）")
            elif status in ("failed", "error"):
                failed.append(f"命令 `{cmd}`（{status}）")
            elif job is None:
                unknown.append(f"命令 `{cmd}`（无 job/settled，成功与否未知）")
            else:
                unknown.append(f"命令 `{cmd}`（status={status}，exit 未标注）")

        elif name == "todo_write":
            if res is None:
                unknown.append("待办更新（无结果，完成状态不可信）")
        else:
            # 通用兜底：任何工具（含 mcp_* 外部副作用）都不能静默丢弃
            safe_args = scrub(json.dumps(args, ensure_ascii=False))[:140] if args else ""
            label = f"{name}({safe_args})" if safe_args else str(name)
            if res is None:
                unknown.append(f"工具 `{label}`（**无结果**，外部效果未知，不要盲重放）")
            elif res.get("isError") is True:
                failed.append(f"工具 `{label}`（报错）")
            elif res.get("isError") is False:
                confirmed.append(f"工具 `{label}` 返回成功（外部效果需自行核验）")
            else:
                unknown.append(f"工具 `{label}`（未标注 isError）")

    return confirmed, failed, unknown, pending


def _codex_last_assistant(events: list[dict]) -> str:
    """只取可见 text：channel 为 final/commentary（或缺省）。

    `channel == "analysis"` 是模型私有推理，绝不能进恢复上下文。
    """
    last = ""
    for e in events:
        if not isinstance(e, dict):
            continue
        p = e.get("payload") or {}
        if e.get("type") == "response_item" and p.get("role") == "assistant":
            if p.get("channel") == "analysis":
                continue
            txt = " ".join(c.get("text", "") for c in (p.get("content") or [])
                           if isinstance(c, dict) and c.get("type") in ("output_text", "text"))
            if txt.strip():
                last = txt.strip()
        # typed AgentMessage：content 里是 type="Text"（大写 T），phase 标可见性
        if e.get("type") == "event_msg" and p.get("type") == "item_completed":
            it = p.get("item") or {}
            if it.get("type") == "AgentMessage":
                phase = it.get("phase")
                if phase not in (None, "commentary", "final_answer"):
                    continue
                txt = " ".join(c.get("text", "") for c in (it.get("content") or [])
                               if isinstance(c, dict) and c.get("type") in ("Text", "text"))
                if txt.strip():
                    last = txt.strip()
        if e.get("type") == "event_msg" and p.get("type") == "task_complete":
            msg = (p.get("last_agent_message") or "").strip()
            if msg:
                last = msg
    return last


def _dsh_last_assistant(events: list[dict]) -> str:
    last = ""
    for e in events:
        if e.get("type") != "assistant/message":
            continue
        d = e.get("data") or {}
        msg = d.get("message") or d
        txt = " ".join(c.get("text", "") for c in (msg.get("content") or [])
                       if isinstance(c, dict) and c.get("type") == "text")
        if txt.strip():
            last = txt.strip()
    return last


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="构建可接续聊天的恢复上下文（只读）")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--from-agent", choices=("codex", "dsh"))
    ap.add_argument("--session")
    ap.add_argument("--workspace")
    ap.add_argument("--project")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all-cwds", action="store_true",
                    help="列出所有 cwd 的会话（仅 sessions 子命令；默认只看当前 cwd）")
    ap.add_argument("--list-sessions", action="store_true",
                    help="内部用：由 CLI 的 sessions 子命令置位")
    ap.add_argument("--db", help="覆盖数据库路径（测试用）")
    ap.add_argument("--codex-root", help="覆盖 Codex 日志根（测试用）")
    ap.add_argument("--dsh-root", help="覆盖 DSH 日志根（测试用）")
    args = ap.parse_args()

    # 只给一半 scope 是参数错误，不能静默重算覆盖
    if bool(args.workspace) != bool(args.project):
        ap.error("--workspace 与 --project 必须成对给出")

    cwd = str(Path(args.cwd).resolve())
    ws, proj = args.workspace, args.project
    if not (ws and proj):
        try:
            ck = _load("ck_scope", "codex-checkpoint.py")
            ws, proj = ck.resolve_project(cwd)
        except Exception:  # noqa: BLE001
            ws, proj = "default", Path(cwd).name

    codex_root = Path(args.codex_root) if args.codex_root else CODEX_ROOT
    dsh_root = Path(args.dsh_root) if args.dsh_root else DSH_ROOT

    agents = [args.from_agent] if args.from_agent else ["codex", "dsh"]

    # sessions 子命令：只列摘要，不构建恢复正文
    if args.list_sessions:
        merged = {"sessions": [], "total": 0, "badge": 0, "warnings": [],
                  "project_basis": None}
        for ag in agents:
            root = codex_root if ag == "codex" else dsh_root
            one = build_candidates(root, cwd, ag, args.all_cwds)
            merged["sessions"].extend(one["sessions"])
            merged["warnings"].extend(one.get("warnings") or [])
            # 项目归属只有 codex 侧有来源；dsh 侧不参与，避免把「无来源」说成不可用
            if ag == "codex":
                merged["project_basis"] = one.get("project_basis")
        # 与 helper 同语义：严格按最后活动时间微秒倒序
        merged["sessions"].sort(
            key=lambda r: (
                r.get("last_activity_us") is None,
                -(r.get("last_activity_us") or 0),
            ))
        merged["total"] = len(merged["sessions"])
        # badge = **可接续会话数**（按 native 去重，不是 scope 行数）。
        # 不再叫「未完成」——没有已完成的任务，只有「这一轮收尾」和「因故中断」。
        merged["badge"] = len({r["native_id"] for r in merged["sessions"]})
        if args.json:
            print(json.dumps(merged, ensure_ascii=False, indent=2))
        else:
            rows = merged["sessions"]
            if not rows:
                print(f"[no-candidate] {cwd} 下没有可接续的 {','.join(agents)} 聊天")
            else:
                print(f"可接续聊天 {len(rows)} 个（按最后活动倒序）：")
                for i, r in enumerate(rows, 1):
                    when = r.get("last_activity") or "时间未知"
                    print(f"  {i}. [{r.get('phase_label','')}] {r['title']}  ·  {when}  ·  {_short_id(r['native_id'])}")
                    print(f"     {r.get('cwd') or '（cwd 未知）'}  ·  {r.get('outcome')}")
        return 0

    all_cands: list[dict] = []
    ready_one: dict | None = None
    reasons: list[str] = []
    incomplete_one: dict | None = None
    unproven_logs: list[dict] = []

    for ag in agents:
        root = codex_root if ag == "codex" else dsh_root
        sel = select_candidate(root, cwd, ag, args.session)
        if sel["status"] == "ready":
            if ready_one is None:
                ready_one = {"agent": ag, "path": sel["path"], "native": sel["native_id"],
                             "cwd_unverified": sel.get("cwd_unverified", False)}
            else:
                all_cands.append({"agent": ag, "native_id": sel["native_id"],
                                  "log": str(sel["path"])})
        elif sel["status"] == "incomplete":
            # 显式点名但没有可继承目标：如实报 incomplete，不降级成 ready。
            if incomplete_one is None:
                incomplete_one = {"agent": ag, "native_id": sel["native_id"],
                                  "reason": sel.get("reason", "")}
        elif sel["status"] == "needs-selection":
            for c in (sel.get("candidates") or []):
                if isinstance(c, dict):
                    all_cands.append({"agent": ag, **c})
                else:
                    all_cands.append({"agent": ag, "native_id": str(c), "log": ""})
        else:
            reasons.append(f"{ag}: {sel.get('reason','')}")
            for u in (sel.get("unproven_logs") or []):
                unproven_logs.append({"agent": ag, **u})

    # 有多个候选时，即使某 agent 只有一个也必须让用户选
    total = (1 if ready_one else 0) + len(all_cands)
    if total == 0 and incomplete_one is not None:
        note = incomplete_one["reason"]
        if args.json:
            print(json.dumps({"status": "incomplete",
                              "message": note,
                              "agent": incomplete_one["agent"],
                              "native_id": incomplete_one["native_id"],
                              "reasons": reasons}, ensure_ascii=False, indent=2))
        else:
            print(f"[incomplete] {note}")
            for r in reasons:
                print(f"  · {r}")
        return 4
    if total == 0:
        if args.json:
            print(json.dumps({"status": "no-candidate",
                              "message": f"{ws}/{proj} 下没有可接续的聊天",
                              "reasons": reasons,
                              "unproven_logs": unproven_logs},
                             ensure_ascii=False, indent=2))
        else:
            print(f"[no-candidate] {ws}/{proj}（{cwd}）下没有可接续的聊天")
            for r in reasons:
                print(f"  · {r}")
        return 2

    if total > 1:
        cands = all_cands + ([{"agent": ready_one["agent"],
                               "native_id": ready_one["native"],
                               "log": str(ready_one["path"])}] if ready_one else [])
        if args.json:
            print(json.dumps({"status": "needs-selection",
                              "message": f"找到 {total} 个候选，请用 --session 指定",
                              "candidates": cands}, ensure_ascii=False, indent=2))
        else:
            print(f"[needs-selection] 找到 {total} 个候选，请用 --session 指定：")
            for c in cands:
                print(f"  - {c['agent']} {c.get('native_id')}  {c.get('log','')}")
        return 3

    # 唯一候选
    f = ready_one
    ctx = build_resume(f["path"], f["native"], cwd, ws, proj,
                       db_path=Path(args.db) if args.db else None,
                       agent=f["agent"],
                       cwd_unverified=f.get("cwd_unverified", False))

    # 片段事件时间无法可靠排序时，**不能**报 ready：那等于宣称「最新进度明确」，
    # 实际可能把旧片段当结尾。返回 incomplete + 非零码，但仍给出已提取内容
    # 和来源清单，让调用方自己核验后再决定。
    if not ctx.get("order_ok", True):
        status = "incomplete"
        note = ("片段事件时间缺失或区间重叠，无法确定片段先后；"
                "下面是按文件名顺序拼接的内容，**不代表最新进度**，接续前必须自行核验")
        if args.json:
            meta = {k: v for k, v in ctx.items() if k != "body"}
            print(json.dumps({"status": status, "message": note, **meta,
                              "body": scrub(ctx["body"])},
                             ensure_ascii=False, indent=2))
        else:
            print(f"[incomplete] {note}")
            print()
            print(ctx["body"])
        return 4

    if args.json:
        meta = {k: v for k, v in ctx.items() if k != "body"}
        print(json.dumps({"status": "ready", **meta, "body": scrub(ctx["body"])},
                         ensure_ascii=False, indent=2))
    else:
        print(ctx["body"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
