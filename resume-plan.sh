#!/usr/bin/env bash
# resume-plan.sh <repo> <branch>: what entrypoint.sh runs in a herdr pane to resume
# one herd plan. Runs `herd plan -R <branch>` once from the source checkout (herd's
# find_wt needs a cwd inside it) and appends how it ended to $AGENTBOX_EVENTS,
# which the entrypoint echoes to the container log. Never retries: a plan that
# fails here needs a look (this pane's output, `herd ls`), not another -R, because
# each failed -R can spend the plan's one fix-up turn.
set -uo pipefail
repo=$1 branch=$2
events=${AGENTBOX_EVENTS:-$HOME/.local/state/agentbox/events.log}
report() { printf 'agentbox: %s\n' "$*" | tee -a "$events"; }

cd "$repo" || { report "resume failed $branch: no source checkout at $repo"; exit 1; }
herd plan -R "$branch"
rc=$?
if ((rc == 0)); then report "resume $branch: herd plan -R finished (exit 0)"
elif ((rc == 130)); then report "resume $branch: herd plan -R interrupted (pod stopping); the plan keeps its saved state"
else report "resume failed $branch: herd plan -R exited $rc; not retried (see this pane, or herd ls)"; fi
exit "$rc"
