#!/bin/sh
# DeepSeek Harness (DSH) stop hook, run by the official
# `@deepseek-ai/dsh-hooks-claude-code` bridge (Claude Code-shaped payload).
# Forwards the event JSON to the ai-memory server, fire-and-forget.
# Walks up from the payload's cwd for a .ai-memory.toml marker file;
# if found, appends marker query params to the URL so the server
# applies the declared workspace/project/strategy instead of
# bucketing by basename(cwd) under the default workspace.
# At runtime (after `install-hooks --apply`) `_lib.sh` is staged
# alongside this script. From the source tree it lives one dir up.
_lib_dir="$(dirname "$0")"
[ -f "$_lib_dir/_lib.sh" ] || _lib_dir="$_lib_dir/.."
. "$_lib_dir/_lib.sh"

SERVER="${AI_MEMORY_HOOK_URL:-http://127.0.0.1:49374}"
PAYLOAD=$(cat)
# Assistant/Stop capture (#196) is native-only: the script fallback cannot
# sanitize the assistant message, so if the raw payload still carries the field
# we drop the whole Stop rather than POST it verbatim. A literal substring check
# (POSIX `case`, no bash-isms per _lib.sh) — conservative on purpose: any Stop
# mentioning the key is dropped. Move to a native install to capture it safely.
case "$PAYLOAD" in
    *'"last_assistant_message"'*)
        printf '{}\n'
        exit 0
        ;;
esac
CWD=$(ai_memory_extract_cwd "$PAYLOAD")
QS=$(ai_memory_marker_qs "$CWD")

printf '%s' "$PAYLOAD" \
    | ai_memory_post_hook "$SERVER/hook?event=stop&agent=dsh${QS}" >/dev/null 2>&1 || true

# --- LOCAL PATCH (qyh): autosave -------------------------------------------
# Refresh the rolling "阶段存档" page for this session. `agent/turn-stopping`
# is the one point DSH fires on EVERY completed turn, and it is serial+awaited,
# so a write here lands before the turn closes. No model tokens are spent: the
# body is parsed mechanically out of the on-disk session log
# (~/.dsh/sessions/<slug>/<session>/session.v4.jsonl.zstd).
#
# Runs synchronously (measured ~0.3s) rather than in a background subshell:
# the bridge awaits this hook, and a detached child can be reaped when the
# hook process exits, which would silently skip the save. The hook timeout is
# 600s, so a synchronous 0.3s write is well within budget.
# Restore stop.sh.orig to return to upstream behaviour.
#
# The session id MUST come from the payload: `base()` in the bridge
# (@deepseek-ai/dsh-hooks-claude-code) emits `session_id` for every event, so
# the Stop hook knows exactly which session fired it. Without it the checkpoint
# had to guess by cwd+mtime and could archive the wrong session when several
# share one cwd. Extracted with POSIX sh + sed (no jq dependency). The full
# native id is passed through unchanged; the script itself matches it
# case-sensitively and refuses to fall back to another session.
if [ -n "$CWD" ] && [ -f "$_lib_dir/dsh-checkpoint.py" ]; then
    if command -v python3 >/dev/null 2>&1; then
        SESSION_ID=$(printf '%s' "$PAYLOAD" \
            | sed -e 's/"session_id"[[:space:]]*:[[:space:]]*"/\n__AI_MEMORY_SID__/' \
            | sed -n '/^__AI_MEMORY_SID__/{s/^__AI_MEMORY_SID__\([^"]*\)".*/\1/p;q;}')
        if [ -n "$SESSION_ID" ]; then
            AI_MEMORY_HOOK_URL="$SERVER" \
                python3 "$_lib_dir/dsh-checkpoint.py" \
                --session "$SESSION_ID" --cwd "$CWD" --quiet \
                >/dev/null 2>&1 || true
        else
            # No id in the payload: do NOT guess. Passing no --session leaves
            # the script's "exactly one session under this cwd" rule in charge,
            # which errors out (and skips the save) when the cwd is ambiguous.
            AI_MEMORY_HOOK_URL="$SERVER" \
                python3 "$_lib_dir/dsh-checkpoint.py" --cwd "$CWD" --quiet \
                >/dev/null 2>&1 || true
        fi
    fi
fi

# Acknowledge with an empty JSON object. This hook only POSTs
# (fire-and-forget) and injects no context; without "{" on stdout the
# bridge logs "Hook output does not start with {, treating as plain text".
printf '{}\n'
exit 0
