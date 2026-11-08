# agentbox

The container image behind the `agentbox` StatefulSet on k3s2: herdr, herd, Claude Code
and tetherd in one image, plus the entrypoint that resumes herd plans after a restart.

- Image: `ghcr.io/lordmuffin/agentbox:v<VERSION>`, linux/amd64 only.
- Built by `.forgejo/workflows/build.yml` on the in-cluster buildkitd. A push to `main`
  that changes the image builds it, and so does a dispatch. The workflow refuses to
  overwrite a tag that already exists.
- The workload, its secrets and the operator doc live in lordmuffin/homelab-gitops:
  `apps/services/agentbox/` and `docs/agentbox.md`.

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
