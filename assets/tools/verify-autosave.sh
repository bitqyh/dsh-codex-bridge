#!/bin/sh
# 验收工具入口（只做转发）。
#
# 实现在 verify-autosave.py。保留这个 .sh 是因为文档和历史命令都用它；
# 逻辑一律在 Python 里，避免 shell 的参数/退出码陷阱
# （旧版本用 `set -- $TRUST` 覆盖了位置参数，导致传入的项目目录完全失效）。
#
# 用法：
#   sh verify-autosave.sh                       # 概览（未完整验证）
#   sh verify-autosave.sh <项目目录>             # 概览 + 推导 scope
#   sh verify-autosave.sh --agent dsh --session session-<uuid>
#   sh verify-autosave.sh <项目目录> --agent codex --session <uuid> --json
#
# 退出码：全部 pass 才是 0；有 fail 或 incomplete 都非零。
exec python3 "$(dirname "$0")/verify-autosave.py" "$@"
