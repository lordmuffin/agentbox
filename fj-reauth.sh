#!/usr/bin/env bash
# fj-reauth: reload fj's login for git.apj.dev from the mounted Forgejo token, the same
# step the entrypoint runs at start (setup_forgejo_cli). Run it after rotating the
# 1Password item HomeLab / agentbox-forgejo-token: the kubelet refreshes the mounted
# Secret within a minute or two, without a pod restart. The token is never printed.
set -euo pipefail
here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# Sourcing does not run main (see the end of entrypoint.sh).
# shellcheck source=entrypoint.sh
source "$here/entrypoint.sh"
setup_forgejo_cli
