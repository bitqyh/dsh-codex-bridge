#!/bin/sh
# DeepSeek Harness (DSH) user-prompt hook, run by the official
# `@deepseek-ai/dsh-hooks-claude-code` bridge (Claude Code-shaped payload).
#
# Two jobs:
#  1. Forward the event JSON to the ai-memory server (capture).
#  2. Fetch a pending cross-agent handoff and return it as
#     `hookSpecificOutput.additionalContext`, which the bridge folds into the
#     turn. This is the DSH replacement for SessionStart injection: the bridge
#     fires SessionStart detached, so its context can miss the first request,
#     while UserPromptSubmit is awaited in `agent/pre-step` before the turn.
#     Handoffs are single-use, so only the first prompt of a session carries one.
#
# LOCAL PATCH (qyh): upstream's dsh bundle only captures here and leaves handoff
# recovery to the MCP `memory_handoff_accept` tool. Restore the .orig backup to
# return to upstream behaviour; `ai-memory install-hooks --agent dsh --apply`
# re-stages from ~/.local/share/ai-memory/hooks and would overwrite this file,
# so patch that source copy (which is what this file is) and re-apply.
_lib_dir="$(dirname "$0")"
[ -f "$_lib_dir/_lib.sh" ] || _lib_dir="$_lib_dir/.."
. "$_lib_dir/_lib.sh"

SERVER="${AI_MEMORY_HOOK_URL:-http://127.0.0.1:49374}"
PAYLOAD=$(cat)
CWD=$(ai_memory_extract_cwd "$PAYLOAD")
QS=$(ai_memory_marker_qs "$CWD")
BRIEF_QS=$(ai_memory_briefing_qs "$CWD")
SESSION_ID=$(ai_memory_extract_session_id "$PAYLOAD")
SESSION_QS=""
[ -n "$SESSION_ID" ] && SESSION_QS="&session_id=$(ai_memory_url_encode "$SESSION_ID")"

printf '%s' "$PAYLOAD" \
    | ai_memory_post_hook "$SERVER/hook?event=user-prompt&agent=dsh${QS}${SESSION_QS}" >/dev/null 2>&1 || true

HANDOFF=$(ai_memory_get_handoff "$SERVER/handoff?agent=dsh${QS}${SESSION_QS}${BRIEF_QS}" 2>/dev/null || true)
if [ -n "$HANDOFF" ]; then
    printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":%s}}\n' \
        "$(printf '%s' "$HANDOFF" | ai_memory_json_string)"
else
    printf '{}\n'
fi
exit 0
