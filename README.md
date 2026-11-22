# agentbox

The container image behind the `agentbox` StatefulSet on k3s2: herdr, herd, Claude Code,
Gemini CLI, Copilot CLI and tetherd in one image, plus the entrypoint that resumes herd plans after a restart.

- Image: `git.apj.dev/lordmuffin/agentbox:v<VERSION>` (Forgejo build) and `ghcr.io/lordmuffin/agentbox:v<VERSION>` (GitHub build), linux/amd64 only.
- Built by `.forgejo/workflows/build.yml` on the in-cluster buildkitd. A push to `main`
  that changes the image (i.e. a merged PR) builds it, and so does a dispatch. It never overwrites a
  tag: a push whose version is already published skips the build, and a dispatch fails.
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

## Browser testing

The image carries Chromium (Debian's `chromium`, plus fonts), `playwright-core` and
`@playwright/mcp`, so an agent can test tether's PWA, or any other web app, in a real browser.
The pod has no display and no user namespaces, so Chromium always runs headless with
`--no-sandbox --disable-dev-shm-usage`; every entry point below sets that.

- **Claude Code MCP.** At start the entrypoint registers a `playwright` MCP server (user scope):
  the agent navigates, clicks, types, reads the page's accessibility tree and takes screenshots
  with the `mcp__playwright__*` tools. `AGENTBOX_BROWSER_MCP=0` turns that off.
- **`agentbox-browser`**, for the shell and for scripts:

      agentbox-browser check                                   # the install works
      agentbox-browser smoke http://localhost:7000 --device "Pixel 7"
      agentbox-browser shot http://localhost:7000 /tmp/home.png --viewport 390x844

  `smoke` exits 1 on any console error, uncaught page error, failed request or HTTP >= 400,
  and reports whether the PWA manifest loads and a service worker registered. `--expect-text`,
  `--expect-selector`, `--ignore REGEX` and `--screenshot` tune it.
- **Scripted flows**: `require('agentbox-browser')` gives `launch`, `newPage` (with device
  emulation), `watch` (collects the errors above) and `smoke`. `browser/examples/pwa-flow.cjs`
  is a template.
- A site behind a private CA needs `AGENTBOX_BROWSER_IGNORE_HTTPS_ERRORS=1`; the MCP server picks
  it up at the next start. Headed mode needs a display and is not supported.

## NetBird (prod-k3s access)

The image carries the NetBird client (0.81.3, sha256-checked in the `Dockerfile`) and
`netbird-join.sh`, so agentbox can manage the Hetzner `prod-k3s` cluster over the overlay.
The policy is one way (`prod-k3s: agentbox to nodes`, group `prod-k3s-agentbox`, tcp 22 and
6443; homelab-gitops `docs/prod-cluster.md` §Agentbox access). **Off by default**: nothing
starts unless a setup key is present, and a failed join is logged and ignored, so it can never
stop agentbox from starting.

- **Setup key.** Made by hand in NetBird (auto-assigns `prod-k3s-agentbox`, reusable, short
  expiry) and kept in 1Password. It reaches the pod as a mounted Secret file,
  `/run/secrets/agentbox/netbird/setup-key` (`NB_SETUP_KEY_FILE` to move it), or as
  `NB_SETUP_KEY` (prefer the file: an env var shows in the pod spec). The entrypoint lifts it
  out of its own environment before herdr starts, so panes and agents do not inherit it; the
  script writes it to a 0600 file for `--setup-key-file`, deletes that after `netbird up`, and
  never puts it on argv or in a log. The peer's identity is kept on the home volume
  (`~/.config/agentbox/netbird`), so a restart reconnects instead of registering another peer.
  `AGENTBOX_NETBIRD=off` disables it even with a key.
- **Mode: userspace (netstack), no privileges.** The pod is non-root with every capability
  dropped and no `/dev/net/tun`, so the entrypoint runs the client in netstack mode: no root,
  no capability, no device. Netstack installs **no routes**, so `10.60.1.0/24` is not on the
  pod's network; reach the cluster through the SOCKS5 proxy on `127.0.0.1:1080`
  (`NB_SOCKS5_PORT`), the way `netbird-ci-join.sh` does in CI. Peers are reached, so use a
  node's overlay address (`netbird status` lists the `prod-k3s-*` peers):

      kubectl config set-cluster prod-k3s --server=https://<overlay-ip>:6443 \
        --proxy-url=socks5://127.0.0.1:1080 --certificate-authority=... --embed-certs
      ssh -o ProxyCommand='python3 -I /opt/agentbox/socks5-connect.py 127.0.0.1:1080 %h %p' root@<overlay-ip>
      curl -sk --socks5-hostname 127.0.0.1:1080 https://<overlay-ip>:6443/ping

  The merged policy's destination is the routed *subnet* group, not the nodes' overlay
  addresses, and netstack does not use subnet routes, so the overlay-address path needs a rule
  from `prod-k3s-agentbox` to the nodes group (as `prod-k3s: CI to nodes` has), and the API
  certificate must carry that address (or use `--tls-server-name`).
- **Mode: kernel, for the real `10.60.1.0/24` route.** `netbird-join.sh kernel` (or
  `NB_MODE=kernel`) installs a `wt0` interface and the route for the whole pod, so
  `https://10.60.1.11:6443` works unmodified and the policy needs no change. That needs root,
  `NET_ADMIN` and `/dev/net/tun`, which the agent container must never get. Run it as a
  **sidecar container of this same image** (a pod shares one network namespace), with
  `command: ["/opt/agentbox/netbird-join.sh", "kernel"]`, `runAsUser: 0`, `NET_ADMIN`, the
  tun device, and the same key Secret. It joins with `--disable-dns` (so the pod's cluster DNS
  is untouched), `--block-inbound` and `--disable-server-routes`. The k3s2 nodes are LXC
  containers, so `/dev/net/tun` must also be passed to the node CT.

`netbird-join.sh` is covered by `tests/test_image.py` against a fake `netbird`. A real join
needs a setup key and was not run by the change that added it.

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
statuses `resume.py` resumes -- plus the `$AGENTBOX_MAX_ADHOC_AGENTS` (6) most recently
active sessions with no herd plan (the ones started from Tether or by hand), as long as
their Claude transcript was written to in the last `$AGENTBOX_ADHOC_MAX_AGE_HOURS` (24).
Everything else is stripped. Every pod replacement, including each image bump, ends all
running Claudes; the kept ones come back with `claude --resume`, the stripped ones as a
shell. A stripped pane keeps its place,
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
| `REGISTRY_TOKEN` | Forgejo token for user `lordmuffin`, scope `write:package`: the Forgejo build pushes `git.apj.dev/lordmuffin/agentbox`. (`GHCR_TOKEN`, `op://HomeLab/GHCR push token/token`, is now only for the GitHub mirror's build.) |
| `SOURCE_READ_TOKEN` | `op://HomeLab/agentbox build read token/token`: Forgejo user `agentbox-build`, Read on lordmuffin/tether and lordmuffin/herd, scope `read:repository`. Forgejo refuses secret names that start with `FORGEJO_`. |
