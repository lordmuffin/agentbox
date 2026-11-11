#!/usr/bin/env bash
# agentbox entrypoint: herdr server, tetherd, then resume the herd plans that were
# in progress when the pod last stopped. docs/agentbox.md §Entrypoint.
#
# Start order:
#   1. `herdr server` headless, under setsid (saved SSH machines need a detached
#      daemon); wait until `herdr status` says running.
#   2. tetherd, configured entirely through TETHER_* env vars that hold URLs and
#      file paths, never secrets. The secret files are copied to a private
#      0700 runtime dir at mode 0600 first: tetherd refuses a group-readable
#      secret file, and a Kubernetes secret volume with fsGroup is group-readable.
#      Notifications are Web Push only (tether #48): no Home Assistant token.
#      The HMAC key is required: without it tetherd is not started and that is
#      logged; the harness still runs. The VAPID key comes from its secret when
#      that is mounted, otherwise tetherd generates one on the home volume.
#   2b. sshd, for the PC's herdr (`herdr machine add agentbox`, docs/agentbox.md
#      §herdr machine): as this user, on 127.0.0.1:2222 only, with
#      /etc/agentbox/sshd_config. Its ed25519 host key is generated once on the
#      home volume, so it survives restarts. A supervisor restarts sshd if it
#      exits (10 s apart). Every line is "agentbox: sshd ...", sshd's own log
#      included. sshd failing never stops the harness.
#   3. No herdr Mobile Relay (docs/agentbox.md §Decisions, relay-in-pod: no).
#   4. For each plan resume.py selects: get a pane in its worktree's workspace,
#      `herdr agent start` the agent there (herd -R never starts one), then run
#      `herd plan -R <branch>` once in its own tab of the "agentbox" workspace.
#      Every step logs a line starting "agentbox: resume"; a failure logs
#      "agentbox: resume failed <branch>: <why>" and that plan is not retried.
#   5. SIGTERM/SIGINT: ^C each herd this script started (so no gate runs against
#      an interrupted agent and burns the fix-up turn), Esc every plan's agent,
#      stop sshd, stop tetherd (it denies pending approvals), `herdr server stop` (a clean
#      stop keeps the workspaces for next time), wait up to
#      AGENTBOX_STOP_TIMEOUT seconds, then kill what is left.
#   6. Otherwise wait on the herdr server, so the container lives as long as it does.
#
# Settings (env, with defaults). Stage 4's manifests mount these paths:
#   AGENTBOX_LIB              dir holding resume.py and resume-plan.sh (default: this script's dir)
#   AGENTBOX_WT_ROOT          herd worktrees ($HOME/git/wt)
#   AGENTBOX_PLAN_ARGS_DIR    agent args saved by agentbox-plan ($HOME/.local/state/agentbox/plans)
#   AGENTBOX_EVENTS           resume-plan.sh's result log, echoed to stdout ($HOME/.local/state/agentbox/events.log)
#   AGENTBOX_RUNTIME_DIR      private copies of the secret files (/tmp/agentbox; not the PVC)
#   AGENTBOX_STOP_TIMEOUT     seconds to wait for children on shutdown (100; the pod gets 120)
#   AGENTBOX_AGENT_START_TIMEOUT  ms for `herdr agent start` (90000, as herd's create_agent)
#   AGENTBOX_HMAC_KEY_FILE    tetherd's HMAC key (/run/secrets/agentbox/tether-hmac/key)
#   TETHER_WEBPUSH_KEY        the mounted VAPID key, PKCS#8 PEM (/run/secrets/agentbox/tether-vapid/key).
#                             Replaced by a 0600 copy. If it is missing or empty, tetherd uses
#                             $HOME/.tether/vapid.key on the home volume, generated on first
#                             start and kept across restarts (logged). Switching between the two
#                             changes the key, and every registered device must re-register.
#   AGENTBOX_SSHD_CONFIG      sshd's config (/etc/agentbox/sshd_config, in the image)
#   AGENTBOX_SSHD_DIR         sshd's host key, on the home volume ($HOME/.config/agentbox/sshd)
#   AGENTBOX_SSH_AUTHORIZED_KEYS  the mounted public key(s) sshd accepts; only checked here
#                             for logging, sshd_config names the path (/etc/agentbox/ssh/authorized_keys)
#   AGENTBOX_SSHD             off: don't run sshd at all (default on)
#   TETHER_PROXY_CLIENT_CA    public CA of tether-proxy-ca, from the reflected tetherd-server-tls
#                             Secret (/run/secrets/agentbox/tetherd-server/ca.crt)
#   TETHER_APPROVAL_TIMEOUT   (300s, tetherd's default and the PC's setting)
#   TETHER_LOKI_URL           (http://loki-gateway.observability.svc.cluster.local)
#   POD_IP                    downward-API status.podIP; tetherd's --proxy-addr is $POD_IP:7443.
#                             Unset means no proxy listener (logged).
#   TETHER_SUGGEST_KEY_FILE   litellm-proxy key for suggestions (unset: none). Replaced by a 0600
#                             copy in AGENTBOX_RUNTIME_DIR; if the file is missing or empty,
#                             TETHER_SUGGEST_URL is unset too and suggestions are off (logged).
#   TETHER_ALLOWED_USERS etc. any other TETHER_* setting is passed through to tetherd as is.
set -euo pipefail

: "${HOME:=/home/agent}"
AGENTBOX_LIB=${AGENTBOX_LIB:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
AGENTBOX_WT_ROOT=${AGENTBOX_WT_ROOT:-$HOME/git/wt}
AGENTBOX_PLAN_ARGS_DIR=${AGENTBOX_PLAN_ARGS_DIR:-$HOME/.local/state/agentbox/plans}
AGENTBOX_EVENTS=${AGENTBOX_EVENTS:-$HOME/.local/state/agentbox/events.log}
AGENTBOX_RUNTIME_DIR=${AGENTBOX_RUNTIME_DIR:-/tmp/agentbox}
AGENTBOX_STOP_TIMEOUT=${AGENTBOX_STOP_TIMEOUT:-100}
AGENTBOX_HMAC_KEY_FILE=${AGENTBOX_HMAC_KEY_FILE:-/run/secrets/agentbox/tether-hmac/key}
AGENTBOX_VAPID_KEY_FILE=${TETHER_WEBPUSH_KEY:-/run/secrets/agentbox/tether-vapid/key}
AGENTBOX_SSHD_CONFIG=${AGENTBOX_SSHD_CONFIG:-/etc/agentbox/sshd_config}
AGENTBOX_SSHD_DIR=${AGENTBOX_SSHD_DIR:-$HOME/.config/agentbox/sshd}
AGENTBOX_SSH_AUTHORIZED_KEYS=${AGENTBOX_SSH_AUTHORIZED_KEYS:-/etc/agentbox/ssh/authorized_keys}
AGENTBOX_SSHD=${AGENTBOX_SSHD:-on}
export TETHER_PROXY_CLIENT_CA=${TETHER_PROXY_CLIENT_CA:-/run/secrets/agentbox/tetherd-server/ca.crt}
# Home Assistant is gone (tether #48); tetherd only warns about these, so drop them.
unset TETHER_HA_URL TETHER_HA_TOKEN_FILE TETHER_HA_NOTIFY_SERVICE TETHER_HA_CHANNEL
export TETHER_APPROVAL_TIMEOUT=${TETHER_APPROVAL_TIMEOUT:-300s}
export TETHER_LOKI_URL=${TETHER_LOKI_URL:-http://loki-gateway.observability.svc.cluster.local}
# Panes inherit the server's env: resume-plan.sh and agentbox-plan read these.
export AGENTBOX_LIB AGENTBOX_WT_ROOT AGENTBOX_PLAN_ARGS_DIR AGENTBOX_EVENTS

AGENTBOX_AGENT_START_TIMEOUT=${AGENTBOX_AGENT_START_TIMEOUT:-90000}
# Panes running herd plan -R, one per line. A file, because resume runs in a
# background subshell (so SIGTERM is handled at once, not after a 90 s agent start).
HERD_PANES=$AGENTBOX_RUNTIME_DIR/herd-panes

herdr_pid="" tetherd_pid="" sshd_sup_pid="" tail_pid="" resume_pid="" stopping=0

log() { printf 'agentbox: %s\n' "$*"; }

# --- 1. herdr ------------------------------------------------------------------------
start_herdr() {
  mkdir -p "$HOME/.config/herdr"
  # In a session of its own: herdr counts only a session leader as a detached
  # daemon (`herdr status server --json`: detached_server_daemon), and saved SSH
  # machines refuse a server that isn't one. Without it the PC's `herdr machine
  # add` says "remote server is not ready for saved machines", or offers to
  # replace the server, which kills every pane (docs/agentbox.md §herdr machine).
  # A background job here is never a process-group leader, so setsid execs in
  # place and $! is still herdr's pid, which main() waits on.
  setsid herdr server >>"$HOME/.config/herdr/agentbox-server.log" 2>&1 </dev/null &
  herdr_pid=$!
  local i
  for ((i = 0; i < 150; i++)); do
    if herdr status 2>/dev/null | grep -q 'status: running'; then
      log "herdr server running (pid $herdr_pid, detached daemon: $(herdr status server --json 2>/dev/null |
        jq -r '.capabilities.detached_server_daemon // "unknown"' 2>/dev/null || echo unknown))"
      return 0
    fi
    kill -0 "$herdr_pid" 2>/dev/null || break
    sleep 0.2
  done
  log "herdr server did not start; see ~/.config/herdr/agentbox-server.log"
  return 1
}

# --- 2. tetherd ----------------------------------------------------------------------
private_copy() {  # <src> <name>: copy a secret file to the runtime dir at 0600, print its path
  local dst=$AGENTBOX_RUNTIME_DIR/$2
  [[ -s $1 ]] || return 1
  install -m 600 "$1" "$dst" && printf '%s\n' "$dst"
}

start_tetherd() {
  local hmac vapid
  if ! hmac=$(private_copy "$AGENTBOX_HMAC_KEY_FILE" tether-hmac.key); then
    log "tetherd not started: no HMAC key at $AGENTBOX_HMAC_KEY_FILE"; return 0
  fi
  export TETHER_HMAC_SECRET_FILE=$hmac
  # Web Push's VAPID key. The mounted secret is 0400/0440, so it gets a private copy.
  if vapid=$(private_copy "$AGENTBOX_VAPID_KEY_FILE" vapid.key); then
    export TETHER_WEBPUSH_KEY=$vapid
  else
    export TETHER_WEBPUSH_KEY=$HOME/.tether/vapid.key
    log "tetherd VAPID key: none at $AGENTBOX_VAPID_KEY_FILE; using $TETHER_WEBPUSH_KEY on the home volume (generated on first start)"
  fi
  # Next-step suggestions. tetherd exits on a group-readable key file, and the
  # mounted one is 0440 (fsGroup), so it gets a private copy too. With no key yet
  # suggestions are turned off rather than sent without one.
  if [[ -n ${TETHER_SUGGEST_KEY_FILE:-} ]]; then
    local suggest_key
    if suggest_key=$(private_copy "$TETHER_SUGGEST_KEY_FILE" litellm.key); then
      export TETHER_SUGGEST_KEY_FILE=$suggest_key
    else
      log "tetherd suggestions off: no key at $TETHER_SUGGEST_KEY_FILE"
      unset TETHER_SUGGEST_URL TETHER_SUGGEST_KEY_FILE
    fi
  fi
  # A PEM, not just a non-empty file: the tetherd-server-tls volume is optional,
  # so until reflector copies the Secret there is no CA file at all.
  if [[ -n ${POD_IP:-} ]] && grep -q -- '-----BEGIN CERTIFICATE-----' "$TETHER_PROXY_CLIENT_CA" 2>/dev/null; then
    export TETHER_PROXY_ADDR=$POD_IP:7443
  else
    unset TETHER_PROXY_ADDR
    log "tetherd proxy listener off: needs POD_IP and a PEM certificate in $TETHER_PROXY_CLIENT_CA"
  fi
  tetherd serve </dev/null &
  tetherd_pid=$!
  log "tetherd started (pid $tetherd_pid, proxy ${TETHER_PROXY_ADDR:-off})"
}

# --- 2b. sshd ------------------------------------------------------------------------
# sshd must be run by absolute path (it re-execs itself).
AGENTBOX_SSHD_BIN=${AGENTBOX_SSHD_BIN:-/usr/sbin/sshd}

sshd_log() {  # prefix sshd's own stderr (-e) so it reads as the entrypoint's
  local line
  while IFS= read -r line; do printf 'agentbox: sshd %s\n' "$line"; done
}

sshd_host_key() {  # <key path>: generate the host key the first time; it then stays on the volume
  local key=$1
  (umask 077 && mkdir -p "$AGENTBOX_SSHD_DIR") && chmod 700 "$AGENTBOX_SSHD_DIR" || return 1
  if [[ ! -s $key || ! -s $key.pub ]]; then
    rm -f "$key" "$key.pub"
    ssh-keygen -q -t ed25519 -N '' -C "agentbox-0 sshd host key" -f "$key" </dev/null >/dev/null || return 1
    log "sshd host key generated (first start on this volume)"
  fi
  # sshd refuses a host key that group or others can read.
  chmod 600 "$key"
}

supervise_sshd() {  # <host key>: run sshd in the foreground; restart it 10 s after it exits
  local key=$1 pid="" rc
  trap 'if [[ -n $pid ]]; then kill -TERM "$pid" 2>/dev/null; fi; exit 0' TERM
  while :; do
    "$AGENTBOX_SSHD_BIN" -D -e -f "$AGENTBOX_SSHD_CONFIG" -h "$key" </dev/null 2> >(sshd_log) &
    pid=$!
    log "sshd started (pid $pid)"
    rc=0
    wait "$pid" || rc=$?
    pid=""
    log "sshd exited ($rc); restarting in 10s"
    sleep 10 &
    wait $! || true
  done
}

start_sshd() {
  if [[ $AGENTBOX_SSHD == off ]]; then log "sshd off (AGENTBOX_SSHD=off)"; return 0; fi
  local key=$AGENTBOX_SSHD_DIR/ssh_host_ed25519_key conf listen keys line
  if ! sshd_host_key "$key"; then
    log "sshd not started: could not create a host key in $AGENTBOX_SSHD_DIR"; return 0
  fi
  log "sshd host key $(ssh-keygen -lf "$key.pub")"
  # -T checks the config the way the daemon will read it, and says where it listens.
  if ! conf=$("$AGENTBOX_SSHD_BIN" -T -f "$AGENTBOX_SSHD_CONFIG" -h "$key" 2>&1 </dev/null); then
    log "sshd not started: $AGENTBOX_SSHD_CONFIG failed its check: ${conf//$'\n'/; }"; return 0
  fi
  listen=$(awk 'tolower($1) == "listenaddress" { print $2 }' <<<"$conf" | paste -sd, -)
  keys=$(grep -cE '^[^#[:space:]]' "$AGENTBOX_SSH_AUTHORIZED_KEYS" 2>/dev/null || true)
  if ((${keys:-0} == 0)); then
    log "sshd: no public key in $AGENTBOX_SSH_AUTHORIZED_KEYS yet, so every login is refused (docs/agentbox.md §herdr machine)"
  else
    while IFS= read -r line; do log "sshd authorized key: $line"; done \
      < <(ssh-keygen -lf "$AGENTBOX_SSH_AUTHORIZED_KEYS" 2>/dev/null || true)
  fi
  # The subshell must not run shutdown() on a signal: supervise_sshd sets its own trap.
  ( trap - TERM INT; supervise_sshd "$key" ) &
  sshd_sup_pid=$!
  log "sshd supervisor started (pid $sshd_sup_pid, listening on ${listen:-?} only)"
}

# --- 4. resume -----------------------------------------------------------------------
bootstrap_workspace() {  # the "agentbox" workspace herd runs in; reused if restored
  local ws
  ws=$(herdr workspace list | jq -r '[.result.workspaces[] | select(.label == "agentbox")][0].workspace_id // empty') || return 1
  if [[ -z $ws ]]; then
    ws=$(herdr workspace create --cwd "$HOME" --label agentbox --no-focus | jq -r '.result.workspace.workspace_id // empty') || return 1
  fi
  [[ -n $ws ]] && printf '%s\n' "$ws"
}

worktree_pane() {  # <repo> <worktree> <agent>: a shell pane in the worktree's workspace
  local repo=$1 wt=$2 agent=$3 ws
  ws=$(herdr worktree list --cwd "$repo" |
       jq -r --arg p "$wt" '[.result.worktrees[] | select(.path == $p)][0].open_workspace_id // empty') || return 1
  if [[ -n $ws ]]; then
    herdr pane list --workspace "$ws" | jq -re '.result.panes[0].pane_id'
  else
    herdr worktree open --cwd "$repo" --path "$wt" --label "$agent" --no-focus | jq -re '.result.root_pane.pane_id'
  fi
}

resume_one() {  # <plan JSON line from resume.py>
  local line=$1 branch repo wt agent kind stage status auto pane ws tab cmd
  local -a args
  branch=$(jq -r .branch <<<"$line") repo=$(jq -r .repo <<<"$line") agent=$(jq -r .agent <<<"$line")
  kind=$(jq -r .kind <<<"$line") stage=$(jq -r .stage <<<"$line") status=$(jq -r .status <<<"$line")
  auto=$(jq -r 'if .permission_auto then "yes" else "no" end' <<<"$line")
  wt=$(realpath "$(jq -r .worktree <<<"$line")")
  mapfile -t args < <(jq -r '.agent_args[]' <<<"$line")
  log "resume $branch: stage $stage ($status), agent $agent, kind $kind, permission-mode auto: $auto"

  if ! pane=$(worktree_pane "$repo" "$wt" "$agent"); then
    log "resume failed $branch: no herdr pane for $wt"; return 0
  fi
  if herdr agent get "$agent" >/dev/null 2>&1; then
    log "resume $branch: agent $agent already exists, not starting another"
  elif ! herdr agent start "$agent" --kind "$kind" --pane "$pane" --timeout "$AGENTBOX_AGENT_START_TIMEOUT" -- "${args[@]}" >/dev/null 2>&1; then
    log "resume failed $branch: agent start failed in pane $pane; not running herd plan -R (herdr agent read $agent)"
    return 0
  fi

  if ! { ws=$(bootstrap_workspace) &&
         tab=$(herdr tab create --workspace "$ws" --cwd "$repo" --label "herd $agent" --no-focus) &&
         pane=$(jq -re '.result.root_pane.pane_id' <<<"$tab"); }; then
    log "resume failed $branch: could not open a herd tab in the agentbox workspace"; return 0
  fi
  printf -v cmd '%q ' "$AGENTBOX_LIB/resume-plan.sh" "$repo" "$branch"
  if ! herdr pane run "$pane" "$cmd" >/dev/null; then
    log "resume failed $branch: herdr pane run failed in $pane"; return 0
  fi
  printf '%s\n' "$pane" >>"$HERD_PANES"
  log "resume $branch: herd plan -R running in pane $pane"
}

resume_plans() {
  local plans line action
  if ! plans=$(python3 "$AGENTBOX_LIB/resume.py" scan --root "$AGENTBOX_WT_ROOT" --args-dir "$AGENTBOX_PLAN_ARGS_DIR"); then
    log "resume failed: resume.py scan exited non-zero; nothing resumed"; return 0
  fi
  [[ -n $plans ]] || { log "resume: no herd plans under $AGENTBOX_WT_ROOT"; return 0; }
  while IFS= read -r line; do
    action=$(jq -r .action <<<"$line")
    case $action in
      resume) resume_one "$line" ;;
      skip) log "resume skipped $(jq -r '"\(.branch // .worktree): \(.reason)"' <<<"$line")" ;;
      *) log "resume failed $(jq -r '"\(.branch // .worktree): \(.reason)"' <<<"$line")" ;;
    esac
  done <<<"$plans"
}

# --- 5. shutdown ---------------------------------------------------------------------
shutdown() {  # [exit code]
  local code=${1:-0}
  trap - TERM INT
  stopping=1
  log "shutdown: stopping herd, agents, tetherd and herdr (up to ${AGENTBOX_STOP_TIMEOUT}s)"
  local p agent deadline=$((SECONDS + AGENTBOX_STOP_TIMEOUT))
  # A resume still in progress stops here; its plans keep their saved state. A
  # herdr call it leaves behind ends with the server below.
  if [[ -n $resume_pid ]] && kill -0 "$resume_pid" 2>/dev/null; then
    kill -TERM "$resume_pid" 2>/dev/null || true
  fi
  while IFS= read -r p; do
    [[ -n $p ]] && { herdr pane send-keys "$p" ctrl+c >/dev/null 2>&1 || true; }
  done < <(cat "$HERD_PANES" 2>/dev/null || true)
  # Every plan's agent, whatever its status: interrupt the turn in flight. The
  # transcript keeps it, and the next start resumes with --continue.
  while IFS= read -r agent; do
    [[ -n $agent ]] && { herdr agent send-keys "$agent" esc >/dev/null 2>&1 || true; }
  done < <(python3 "$AGENTBOX_LIB/resume.py" scan --root "$AGENTBOX_WT_ROOT" \
             --args-dir "$AGENTBOX_PLAN_ARGS_DIR" 2>/dev/null | jq -r '.agent // empty' 2>/dev/null || true)
  # sshd first: a herdr client on the PC then sees a closed connection, not a
  # server going away under it. Open sessions end with the herdr server below.
  if [[ -n $sshd_sup_pid ]]; then kill -TERM "$sshd_sup_pid" 2>/dev/null || true; fi
  if [[ -n $tetherd_pid ]]; then kill -TERM "$tetherd_pid" 2>/dev/null || true; fi
  if [[ -n $herdr_pid ]]; then herdr server stop >/dev/null 2>&1 || kill -TERM "$herdr_pid" 2>/dev/null || true; fi

  for p in $sshd_sup_pid $tetherd_pid $herdr_pid; do
    while kill -0 "$p" 2>/dev/null && ((SECONDS < deadline)); do sleep 1; done
    if kill -0 "$p" 2>/dev/null; then log "shutdown: pid $p still running, killing it"; kill -KILL "$p" 2>/dev/null || true; fi
  done
  log "shutdown: done"
  if [[ -n $tail_pid ]]; then kill "$tail_pid" 2>/dev/null || true; fi
  exit "$code"
}

main() {
  trap shutdown TERM INT
  mkdir -p "$(dirname "$AGENTBOX_EVENTS")" "$AGENTBOX_PLAN_ARGS_DIR"
  (umask 077 && mkdir -p "$AGENTBOX_RUNTIME_DIR") && chmod 700 "$AGENTBOX_RUNTIME_DIR"
  touch "$AGENTBOX_EVENTS"
  tail -n0 -F "$AGENTBOX_EVENTS" 2>/dev/null &
  tail_pid=$!

  start_herdr || exit 1
  start_tetherd
  start_sshd
  log "herdr Mobile Relay: not run in the pod (docs/agentbox.md, relay-in-pod)"
  : >"$HERD_PANES"
  # The subshell must not run shutdown() itself on a signal: only the parent does.
  ( trap - TERM INT; resume_plans ) &
  resume_pid=$!

  # `wait` returns early when a trapped signal arrives; shutdown() then exits.
  local rc=0
  wait "$herdr_pid" || rc=$?
  ((stopping)) || { log "herdr server exited ($rc); stopping"; shutdown 1; }
}

main "$@"
