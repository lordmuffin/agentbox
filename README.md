# agentbox

The container image behind the `agentbox` StatefulSet on k3s2: herdr, herd, Claude Code,
Gemini CLI, Copilot CLI and tetherd in one image, plus the entrypoint that resumes herd plans after a restart.

- Image: `ghcr.io/lordmuffin/agentbox:v<VERSION>`, linux/amd64 only.
- Built by `.forgejo/workflows/build.yml` on the in-cluster buildkitd. A push to `main`
  that changes the image builds it, and so does a dispatch. The workflow refuses to
  overwrite a tag that already exists.
- The workload, its secrets and the operator doc live in lordmuffin/homelab-gitops:
  `apps/services/agentbox/` and `docs/agentbox.md`.

## Git over ssh

At start, before tetherd, `entrypoint.sh` installs a deploy key so `git clone git@git-ssh.apj.dev:...`
works with no TTY (tetherd's New session clone). Both inputs are optional mounts; when the key is
absent or empty nothing is touched and one line is logged.

| Env | Default | Installed as |
|---|---|---|
| `AGENTBOX_GIT_SSH_KEY_FILE` | `/run/secrets/agentbox/git-ssh/key` | `~/.ssh/id_ed25519` (0600, in `~/.ssh` 0700) |
| `AGENTBOX_GIT_SSH_KNOWN_HOSTS_FILE` | `/run/secrets/agentbox/git-ssh-known-hosts/known_hosts` | `~/.ssh/known_hosts` (0644) |

`~/.ssh/config` gets a `Host git-ssh.apj.dev` block (`IdentitiesOnly`, `StrictHostKeyChecking yes`,
`BatchMode yes`). Host trust comes only from the mounted known_hosts: no ssh-keyscan, no accept-new.
Without it the key is still installed, a warning is logged, and host verification fails. No ssh-agent
is started. Redone on every start. `docs/agentbox.md` (homelab-gitops) should list the mounts.

## Restored agent sessions

herdr saves every pane it has opened in `~/.config/herdr/session.json`, including the
Claude session each pane was running, and restores all of them at once on start -- one
`claude --resume <id>` per bound pane, about 240 MB each. A box that has accumulated a
pane per feature worktree therefore asks for far more than its memory limit and is
OOM-killed part-way through the restore, on every restart, because the state that causes
it lives on the PVC.

`prune_sessions.py` bounds that. The entrypoint runs it before it starts herdr (a live
herdr rewrites `session.json` from memory and would undo it), keeping the newest
`$AGENTBOX_MAX_LIVE_AGENTS` bindings whose herd plan is still in progress -- the same
statuses `resume.py` resumes -- and stripping the rest. A stripped pane keeps its place,
its directory and its scrollback and comes back as a plain shell; no transcript is
touched, and every stripped id is appended to
`~/.local/state/agentbox/pruned-sessions.tsv` so it can be picked up by hand:

    claude --resume <session-id>      # run in the pane's cwd

To see what a prune would do without writing anything, in the pod:

    /opt/agentbox/prune_sessions.py report

To prune by hand -- recovering a box that is already OOM-looping, or after changing the
cap -- the server has to be down, because herdr would otherwise overwrite the result:

    kubectl -n agentbox scale statefulset agentbox --replicas=0   # wait for the pod to go
    # then, with the PVC mounted in a pod that is not running herdr:
    /opt/agentbox/prune_sessions.py report                        # check first
    /opt/agentbox/prune_sessions.py apply
    kubectl -n agentbox scale statefulset agentbox --replicas=1

`apply` refuses to run while a herdr server is up; `--force` overrides that. Each run
backs `session.json` up beside itself as `session.json.<stamp>.bak`, so a prune can be
undone by moving the backup back with the server still down.

## Bumping

1. Change the pin and its sha256 in the `Dockerfile`, or change the scripts.
2. Bump `VERSION` in the same commit. A change to the scripts alone needs a bump too.
3. Merge to `main` and wait for the build to push `v<VERSION>`.
4. Only then bump the StatefulSet's image tag in homelab-gitops. A pin ahead of its
   image leaves `agentbox-0` in `ImagePullBackOff`.

## Tests

    python3 -m unittest discover -s tests -t .

`tests/test_resume.py` covers which herd plans the entrypoint resumes,
`tests/test_prune_sessions.py` which restored agent bindings survive a prune, and
`tests/test_image.py` holds the Dockerfile, entrypoint and workflow to their rules.

## Actions secrets

| Secret | Source |
|---|---|
| `BUILDKIT_CA`, `BUILDKIT_CERT`, `BUILDKIT_KEY` | k3s2 Secret `buildkit/buildkit-client-tls` (homelab-gitops `docs/buildkit.md` §Clients). Refresh after cert-manager renews it, next on 2027-09-26. |
| `GHCR_TOKEN` | `op://HomeLab/GHCR push token/token` |
| `SOURCE_READ_TOKEN` | `op://HomeLab/agentbox build read token/token`: Forgejo user `agentbox-build`, Read on lordmuffin/tether and lordmuffin/herd, scope `read:repository`. Forgejo refuses secret names that start with `FORGEJO_`. |
