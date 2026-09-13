#!/usr/bin/env bash
# Runs INSIDE the microVM as (or just after) init. The host hands us a "job drive" (/dev/vdb): a
# small ext4 with the task, the SCOPED credentials (only the target cloud's + the agent's own auth),
# and the MCP config. We run the agent headless and write the result + transcript back to the same
# drive, then power off so the host can read them and discard the VM. Nothing cloud-specific lives in
# the image; the job drive is the only source of what cloud this is.
set -u
# We are booted as PID 1 (init=/usr/local/bin/vm-runner), so mount the pseudo-filesystems ourselves.
mount -t proc  proc  /proc 2>/dev/null || true
mount -t sysfs sys   /sys  2>/dev/null || true
mount -t devtmpfs dev /dev  2>/dev/null || true
mount -t tmpfs tmp   /tmp  2>/dev/null || true

log(){ echo "[vm-runner] $*" ; }

JOB=/mnt/job
mkdir -p "$JOB"
mount /dev/vdb "$JOB" 2>/dev/null || mount -o ro /dev/vdb "$JOB" 2>/dev/null || true

# SMOKE MODE: no job drive / no prompt -> just prove the boot chain + toolchain, then power off.
if [ ! -f "$JOB/prompt.txt" ]; then
  log "SMOKE: no job drive; toolchain check"
  log "claude=$(claude --version 2>&1 | head -1)"
  log "codex=$(codex --version 2>&1 | head -1)"
  log "node=$(node --version 2>&1)"
  log "uvx=$(uvx --version 2>&1)"
  log "SMOKE OK; powering off"
  sync; reboot -f 2>/dev/null || echo b > /proc/sysrq-trigger
  sleep 5; exit 0
fi

# bring up networking (the host configures a tap; kernel cmdline ip= sets the address). DNS via the job.
if [ -f "$JOB/resolv.conf" ]; then cp "$JOB/resolv.conf" /etc/resolv.conf; fi

# The agent runs as NON-ROOT (Claude Code refuses --dangerously-skip-permissions as root). Install the
# SCOPED agent config into the agent user's HOME: ONLY the target cloud's creds + the agent's own
# subscription auth. This credential scoping is the substrate isolation (C9).
AG=/home/agent
mkdir -p "$AG/.claude" "$AG/.codex" "$AG/.aws" "$AG/.config/gcloud" "$AG/.azure" "$AG/app"
if [ -d "$JOB/dot-claude" ]; then cp -a "$JOB/dot-claude/." "$AG/.claude/"; fi
# codex: auth.json + a config.toml holding ONLY this run's MCP servers (the host stages both)
if [ -d "$JOB/dot-codex" ];  then cp -a "$JOB/dot-codex/."  "$AG/.codex/";  fi
if [ -d "$JOB/dot-aws" ];    then cp -a "$JOB/dot-aws/."    "$AG/.aws/";    fi
# generalized per-cloud creds (only the target cloud's dir is staged, so scoping/C9 holds):
# gcp -> ~/.config/gcloud (Application Default Credentials); azure -> ~/.azure (DefaultAzureCredential).
if [ -d "$JOB/dot-config-gcloud" ]; then cp -a "$JOB/dot-config-gcloud/." "$AG/.config/gcloud/"; fi
if [ -d "$JOB/dot-azure" ];         then cp -a "$JOB/dot-azure/."         "$AG/.azure/";         fi
if [ -f "$JOB/app.tar" ];    then tar -xf "$JOB/app.tar" -C "$AG/app"; fi
# the rootfs was unpacked without root, so /home/agent may be owned by the wrong uid; fix it all.
chown -R agent:agent "$AG"

PROMPT="$(cat "$JOB/prompt.txt" 2>/dev/null)"
MODEL="$(cat "$JOB/model.txt" 2>/dev/null)"
MCP="$JOB/mcp.json"
MAXTURNS="$(cat "$JOB/max_turns.txt" 2>/dev/null || echo 200)"
AGENT="$(cat "$JOB/agent.txt" 2>/dev/null || echo claude)"
RESUME="$(cat "$JOB/resume.txt" 2>/dev/null)"

OUT="$JOB/out.json"
log "running agent '$AGENT' as user 'agent' (model=$MODEL, cwd=$AG/app)"

# Each CLI takes a different invocation and writes its transcript somewhere different. Everything
# below this point branches on $AGENT and nothing else; the measurement is identical either way,
# because acspeed normalizes both transcript formats before any split is computed.
if [ "$AGENT" = "codex" ]; then
  BIN=codex
  # codex has no --mcp-config: its servers come from ~/.codex/config.toml, staged by the host.
  ARGS=( exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox )
  [ -n "$MODEL" ] && ARGS+=( -m "$MODEL" )
  [ -n "$RESUME" ] && ARGS+=( resume "$RESUME" )
  # codex has no append-system flag, so the system text is prepended to the prompt instead
  if [ -f "$JOB/system.txt" ]; then PROMPT="$(cat "$JOB/system.txt")

$PROMPT"; fi
  ARGS+=( "$PROMPT" )
  TRANSCRIPT_GLOB="$AG/.codex/sessions"
else
  BIN=claude
  ARGS=( -p "$PROMPT" --output-format json --max-turns "$MAXTURNS"
         --permission-mode bypassPermissions )
  [ -s "$MCP" ] && ARGS+=( --mcp-config "$MCP" --strict-mcp-config )
  [ -n "$MODEL" ] && ARGS+=( --model "$MODEL" )
  [ -f "$JOB/system.txt" ] && ARGS+=( --append-system-prompt "$(cat "$JOB/system.txt")" )
  [ -n "$RESUME" ] && ARGS+=( --resume "$RESUME" )
  TRANSCRIPT_GLOB="$AG/.claude/projects"
fi

mount -o remount,rw "$JOB" 2>/dev/null || true

# URL RELAY: the host cannot see the agent's live transcript (it is inside this VM), but the readiness
# poll must run CONCURRENTLY and EXTERNALLY (from the host, a neutral vantage). So we tail the agent's
# transcript here and echo any URL it produces to the serial console; the host reads the console live
# and polls the URL itself. URL discovered in-VM, URL polled from the host.
( while :; do
    for f in $(find "$TRANSCRIPT_GLOB" -name '*.jsonl' 2>/dev/null); do
      [ -f "$f" ] && grep -hoE 'https?://[a-zA-Z0-9._~:/?#@%+-]+' "$f" 2>/dev/null
    done | sort -u | sed 's/^/ACSPEED_URL /'
    sleep 3
  done ) > /dev/console 2>/dev/null &
RELAY_PID=$!

# run the agent as 'agent' with a scoped HOME/config; runuser avoids PAM password prompts
runuser -u agent -- env HOME="$AG" CLAUDE_CONFIG_DIR="$AG/.claude" CODEX_HOME="$AG/.codex" \
  PATH="/usr/local/bin:/usr/bin:/bin" \
  bash -c 'cd "$1/app" && exec "$2" "${@:3}"' _ "$AG" "$BIN" "${ARGS[@]}" > "$OUT" 2>"$JOB/err.txt"
echo "$?" > "$JOB/exit_code"
kill "$RELAY_PID" 2>/dev/null || true

# hand the transcript back: copy the whole projects tree the agent just wrote
if [ -d "$AG/.claude/projects" ]; then
  tar -cf "$JOB/transcripts.tar" -C "$AG/.claude" projects 2>/dev/null || true
fi
# codex writes rollout-*.jsonl under ~/.codex/sessions/YYYY/MM/DD/; hand back the same way
if [ -d "$AG/.codex/sessions" ]; then
  tar -cf "$JOB/codex_sessions.tar" -C "$AG/.codex" sessions 2>/dev/null || true
fi
# recover the deploy's SSH key(s): the deploy may mint a per-app keypair (redu-<app>-deploy) whose PRIVATE
# key lives only in this microVM, so the HOST needs it to reach the deploy VM off-clock for capability C.
# The user's own key, going to the user's own host; nothing cloud-crossing leaves here.
if [ -d "$AG/.ssh" ]; then
  tar -cf "$JOB/agent_ssh.tar" -C "$AG/.ssh" . 2>/dev/null || true
fi
sync
log "done; powering off"
# clean poweroff so Firecracker exits
reboot -f 2>/dev/null || echo b > /proc/sysrq-trigger
