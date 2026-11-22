#!/usr/bin/env bash
# Join NetBird so agentbox can manage the Hetzner prod-k3s cluster (one way: the
# `prod-k3s: agentbox to nodes` policy, tcp 22 and 6443, nothing connects back).
# homelab-gitops docs/prod-cluster.md §Agentbox access.
#
#   netbird-join.sh [netstack|kernel]     (default: $NB_MODE, else netstack)
#
#   netstack  userspace WireGuard, no root, no /dev/net/tun, no capability. The
#             entrypoint runs this one, in the background. It applies NO routes
#             (netbird's route manager is a no-op in netstack mode), so nothing in the
#             pod's network namespace sees 10.60.1.0/24; a SOCKS5 proxy on
#             127.0.0.1:$NB_SOCKS5_PORT is the way in (README §NetBird). It reaches
#             NetBird *peers*, so what a policy must allow is a node's overlay
#             address, not the routed subnet.
#   kernel    a wt0 interface and the real 10.60.1.0/24 route for the whole pod.
#             Needs root, NET_ADMIN and /dev/net/tun, so it is meant for a sidecar
#             container of this image (same network namespace), never the agent
#             container. Runs in the foreground.
#
# The setup key is the only secret. It is read from NB_SETUP_KEY_FILE (a mounted
# Secret; default /run/secrets/agentbox/netbird/setup-key) or NB_SETUP_KEY, put in a
# 0600 file for `netbird up --setup-key-file`, and the file is deleted afterwards. It is
# never on argv, never logged, and never left in the environment of the daemon.
# Without a key this script does nothing and exits 0. A failed join exits 1; the
# entrypoint ignores that. Every line is "agentbox: netbird ...".
#
# Settings (env): NB_MANAGEMENT_URL (https://netbird.apj.dev), NB_HOSTNAME (agentbox),
# NB_STATE_DIR ($HOME/.config/agentbox/netbird: the peer's identity, kept on the home
# volume so a restart reconnects instead of registering another peer), NB_SOCKS5_PORT
# (1080), NB_UP_TIMEOUT (60 s), AGENTBOX_RUNTIME_DIR (/tmp/agentbox, for the key copy).
set -euo pipefail

log() { printf 'agentbox: netbird %s\n' "$*"; }

mode=${1:-${NB_MODE:-netstack}}
case $mode in netstack | kernel) ;; *) log "unknown mode '$mode' (netstack or kernel); off"; exit 0 ;; esac

keyfile=${NB_SETUP_KEY_FILE:-/run/secrets/agentbox/netbird/setup-key}
key=${NB_SETUP_KEY:-}
unset NB_SETUP_KEY NB_SETUP_KEY_FILE  # nothing below, the daemon included, inherits the key
if [[ -z $key && -s $keyfile ]]; then key=$(tr -d '[:space:]' <"$keyfile" 2>/dev/null) || key=""; fi
key=$(tr -d '[:space:]' <<<"$key")
if [[ -z $key ]]; then log "off: no setup key (NB_SETUP_KEY or $keyfile)"; exit 0; fi

: "${HOME:=/home/agent}"
mgmt=${NB_MANAGEMENT_URL:-https://netbird.apj.dev}
host=${NB_HOSTNAME:-agentbox}
port=${NB_SOCKS5_PORT:-1080}
timeout=${NB_UP_TIMEOUT:-60}
runtime=${AGENTBOX_RUNTIME_DIR:-/tmp/agentbox}
state=${NB_STATE_DIR:-$HOME/.config/agentbox/netbird}
if [[ $mode == kernel ]]; then
  if (($(id -u) != 0)); then log "kernel mode needs root, NET_ADMIN and /dev/net/tun; not joining"; exit 1; fi
  if [[ ! -c /dev/net/tun ]]; then log "kernel mode needs /dev/net/tun; not joining"; exit 1; fi
fi
command -v netbird >/dev/null 2>&1 || { log "not installed; not joining"; exit 1; }

(umask 077 && mkdir -p "$state" "$runtime") || { log "cannot create $state; not joining"; exit 1; }
sock=$runtime/netbird.sock
# The daemon reads these from its environment; its default, /var/lib/netbird, is not
# writable by `agent`.
export NB_STATE_DIR=$state NB_CONFIG=$state/default.json
if [[ $mode == netstack ]]; then export NB_USE_NETSTACK_MODE=true NB_SOCKS5_LISTENER_PORT=$port; fi

daemon_pid=""
stop() {
  trap - TERM INT EXIT
  netbird --daemon-addr "unix://$sock" down >/dev/null 2>&1 || true
  if [[ -n $daemon_pid ]]; then kill "$daemon_pid" 2>/dev/null || true; fi
  rm -f "$runtime/netbird-setup-key" "$sock"
  exit "${1:-0}"
}
trap 'stop 0' TERM INT
trap 'rm -f "$runtime/netbird-setup-key"' EXIT

rm -f "$sock"
netbird --daemon-addr "unix://$sock" --log-file "$state/daemon.log" service run >/dev/null 2>&1 &
daemon_pid=$!
for _ in $(seq 1 20); do [[ -S $sock ]] && break; sleep 0.5; done
[[ -S $sock ]] || { log "daemon did not start; see $state/daemon.log"; stop 1; }

# printf is a builtin, so the key is on no argv.
(umask 077 && printf '%s' "$key" >"$runtime/netbird-setup-key")
unset key
args=(--daemon-addr "unix://$sock" up --management-url "$mgmt" --hostname "$host"
      --setup-key-file "$runtime/netbird-setup-key"
      --block-inbound --disable-dns --disable-firewall --disable-server-routes)
if ! timeout "$timeout" env -u NB_CONFIG netbird "${args[@]}" >/dev/null 2>"$runtime/netbird-up.err"; then
  log "WARNING join failed: $(tr '\n' ' ' <"$runtime/netbird-up.err" | cut -c1-300)"
  stop 1
fi
rm -f "$runtime/netbird-setup-key" "$runtime/netbird-up.err"
if [[ $mode == netstack ]]; then
  log "joined $mgmt as $host (netstack, SOCKS5 on 127.0.0.1:$port, no routes: use the proxy)"
else
  log "joined $mgmt as $host (kernel mode, routes installed)"
fi
# A signal interrupts this wait and runs the TERM/INT trap.
wait "$daemon_pid" || true
log "daemon exited"
stop 1
