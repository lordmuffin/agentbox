# agentbox

The container image behind the `agentbox` StatefulSet on k3s2: herdr, herd, Claude Code
and tetherd in one image, plus the entrypoint that resumes herd plans after a restart.

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

## Bumping

1. Change the pin and its sha256 in the `Dockerfile`, or change the scripts.
2. Bump `VERSION` in the same commit. A change to the scripts alone needs a bump too.
3. Merge to `main` and wait for the build to push `v<VERSION>`.
4. Only then bump the StatefulSet's image tag in homelab-gitops. A pin ahead of its
   image leaves `agentbox-0` in `ImagePullBackOff`.

## Tests

    python3 -m unittest discover -s tests -t .

`tests/test_resume.py` covers which herd plans the entrypoint resumes. `tests/test_image.py`
holds the Dockerfile, entrypoint and workflow to their rules.

## Actions secrets

| Secret | Source |
|---|---|
| `BUILDKIT_CA`, `BUILDKIT_CERT`, `BUILDKIT_KEY` | k3s2 Secret `buildkit/buildkit-client-tls` (homelab-gitops `docs/buildkit.md` §Clients). Refresh after cert-manager renews it, next on 2027-09-26. |
| `GHCR_TOKEN` | `op://HomeLab/GHCR push token/token` |
| `SOURCE_READ_TOKEN` | `op://HomeLab/agentbox build read token/token`: Forgejo user `agentbox-build`, Read on lordmuffin/tether and lordmuffin/herd, scope `read:repository`. Forgejo refuses secret names that start with `FORGEJO_`. |
