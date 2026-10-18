#!/usr/bin/env bash
# Caller side of the bridge, for transport kind "command":
#
#   command = ["bash", "/path/to/qbd-ledger-archive/bridge/windows/remote.sh", "<ssh-host>", "{request}", "{response}", "{timeout}"]
#
# Copies the request to the QuickBooks machine, runs it through
# qbxml-request.ps1 over SSH, copies the response back, and deletes the
# copies left on the QuickBooks machine.
# BRIDGE_DIR is where the bridge scripts live on the QuickBooks machine
# (forward slashes, no spaces), C:/qbd-bridge by default.
set -euo pipefail
host="$1"; request="$2"; response="$3"; timeout="$4"
BRIDGE_DIR="${BRIDGE_DIR:-C:/qbd-bridge}"
if [[ ! "$BRIDGE_DIR" =~ ^[A-Za-z]:/[A-Za-z0-9_./-]+$ ]]; then
  echo "STATUS: ERR: BRIDGE_DIR must look like C:/qbd-bridge (no spaces)"; exit 1
fi
if [[ ! "$timeout" =~ ^[0-9]+$ ]]; then echo "STATUS: ERR: bad timeout"; exit 1; fi
name="req_$$_$(date +%s).xml"
SSH_OPTS=(-o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=4)

scp -q "${SSH_OPTS[@]}" -- "$request" "$host:$BRIDGE_DIR/io/$name"
out=$(ssh "${SSH_OPTS[@]}" -- "$host" \
  "powershell -NoProfile -ExecutionPolicy Bypass -File \"$BRIDGE_DIR/qbxml-request.ps1\" -Request \"$BRIDGE_DIR/io/$name\" -TimeoutSec $timeout" \
  | tr -d '\r' || true)
# SSH into Windows wraps PowerShell output in CLIXML noise; only these lines matter.
status=$(printf '%s\n' "$out" | grep '^STATUS:' | head -n1 || true)
remote=$(printf '%s\n' "$out" | grep '^RESPONSE:' | head -n1 | sed 's/^RESPONSE: *//' | tr '\\' '/' || true)
if [[ "$status" != "STATUS: OK" || -z "$remote" ]]; then
  echo "${status:-STATUS: ERR: no status from bridge}"
  exit 1
fi
scp -q "${SSH_OPTS[@]}" -- "$host:$remote" "$response"
ssh "${SSH_OPTS[@]}" -- "$host" "del /q \"$(printf '%s' "$remote" | tr '/' '\\')\"" >/dev/null 2>&1 || true
