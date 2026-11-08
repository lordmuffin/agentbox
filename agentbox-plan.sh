#!/usr/bin/env bash
# agentbox-plan: use it instead of `herd plan` inside agentbox, with the same arguments.
#
#   agentbox-plan [opts] <branch> <plan.md|-|text> -- --permission-mode auto
#
# herd's .herd/run/state does not record the agent args given after `--`, so a
# plan resumed after a pod restart would lose them. This saves them first, one
# per line, to $AGENTBOX_PLAN_ARGS_DIR/<agent>.args (resume.py record-args), then
# execs `herd plan` unchanged. No args after `--` removes an old file for that
# branch; -R, -n and -h leave it alone. Saving never blocks the plan.
set -euo pipefail
lib=${AGENTBOX_LIB:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")}
python3 "$lib/resume.py" record-args -- "$@" || true
exec herd plan "$@"
