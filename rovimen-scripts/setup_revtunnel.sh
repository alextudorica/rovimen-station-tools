#!/usr/bin/env bash
# Reverse SSH tunnel to the VPS: a way into the station that does not depend
# on Tailscale (#1432). Keeps `ssh -N -R 127.0.0.1:<port>:localhost:22` open
# to the VPS's tunnel-only account; an admin then reaches the station with
#   ssh -o ProxyJump=<admin>@<vps> -p <port> <station user>@127.0.0.1
#
# Same unit as gmnmd01 (Cahul, since 2026-09-03) and gmnro15 (Sighetu Marmatiei), plus:
# the VPS host key can be pinned (--host-key) instead of trusted on first use,
# and systemd never stops restarting it (StartLimitIntervalSec=0).
#
# Idempotent: reuses ~/.ssh/revtunnel_ed25519 and rewrites the unit only when
# it changed. Prints the line to add to ~revssh/.ssh/authorized_keys on the
# VPS, pinned to this station's port with permitlisten, so one station's key
# cannot open another station's listener.
#
# Ports: one per station, from a range the VPS sshd allows for the tunnel
# account (Match block with PermitOpen none and AllowTcpForwarding remote).
#
# Usage: setup_revtunnel.sh --port N --vps HOST [--host-key 'ssh-ed25519 AAAA...']
#                           [--vps-user revssh] [--dry-run]
# Prints one line: REVTUNNEL: PASS|FAIL ...
set -u

PORT="" VPS="" HOST_KEY="" VPS_USER="revssh" DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --vps) VPS="$2"; shift 2 ;;
    --host-key) HOST_KEY="$2"; shift 2 ;;
    --vps-user) VPS_USER="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) echo "REVTUNNEL: FAIL unknown argument $1"; exit 2 ;;
  esac
done
case "$PORT" in ''|*[!0-9]*) echo "REVTUNNEL: FAIL --port N required"; exit 2 ;; esac
[ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || { echo "REVTUNNEL: FAIL port $PORT out of range"; exit 2; }
[ -n "$VPS" ] || { echo "REVTUNNEL: FAIL --vps HOST required"; exit 2; }

ME=$(id -un)
SSH_DIR="$HOME/.ssh"
KEY="$SSH_DIR/revtunnel_ed25519"
KNOWN="$SSH_DIR/revtunnel_known_hosts"
SYSTEM_DIR="${ROVIMEN_SYSTEMD_DIR:-/etc/systemd/system}"
USER_DIR="${ROVIMEN_USER_SYSTEMD_DIR:-$HOME/.config/systemd/user}"
UNIT=rovimen-revtunnel.service

mkdir -p "$SSH_DIR" && chmod 700 "$SSH_DIR"
if [ ! -f "$KEY" ]; then
  ssh-keygen -q -t ed25519 -N '' -C "$(hostname)-revtunnel" -f "$KEY" \
    || { echo "REVTUNNEL: FAIL ssh-keygen"; exit 1; }
fi

if [ -n "$HOST_KEY" ]; then
  printf '%s %s\n' "$VPS" "$HOST_KEY" > "$KNOWN"
  chmod 600 "$KNOWN"
  HOSTKEY_OPTS="-o UserKnownHostsFile=$KNOWN -o StrictHostKeyChecking=yes"
else
  HOSTKEY_OPTS="-o UserKnownHostsFile=$KNOWN -o StrictHostKeyChecking=accept-new"
fi

# A system unit where sudo works without a password; otherwise a user unit,
# which needs linger to run without a login (gmnro07, gmnro08, gmnde00 have
# only the updater's exact sudo rules).
if sudo -n true 2>/dev/null; then
  MODE=system; DIR="$SYSTEM_DIR"; USER_LINE="User=$ME"; WANTED=multi-user.target
else
  MODE=user; DIR="$USER_DIR"; USER_LINE=""; WANTED=default.target
fi

UNIT_TEXT="[Unit]
Description=Reverse SSH tunnel to the VPS (Tailscale-independent access, #1432)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
${USER_LINE:+$USER_LINE
}ExecStart=/usr/bin/ssh -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes $HOSTKEY_OPTS -o BatchMode=yes -o IdentitiesOnly=yes -i $KEY -R 127.0.0.1:$PORT:localhost:22 $VPS_USER@$VPS
Restart=always
RestartSec=15

[Install]
WantedBy=$WANTED
"

# restrict + port-forwarding re-enables forwarding only, permitlisten pins the
# one -R listener this station may open, and command="/bin/false" refuses any
# shell or command (ssh -N opens no session, so the tunnel is unaffected).
# No permitopen option: in authorized_keys it takes only [host:]port, and
# permitopen="none" (valid only as the sshd_config keyword PermitOpen) makes
# sshd discard the whole key line, which broke every tunnel installed on
# 2026-11-22. Local forwards (-L) are refused by the VPS Match block instead
# (AllowTcpForwarding remote, PermitOpen none).
AUTH_LINE="restrict,port-forwarding,permitlisten=\"127.0.0.1:$PORT\",command=\"/bin/false\" $(cat "$KEY.pub")"

if [ "$DRY" = 1 ]; then
  printf '%s\n' "--- $DIR/$UNIT ($MODE)" "$UNIT_TEXT"
  echo "VPS authorized_keys line for ~$VPS_USER/.ssh/authorized_keys:"
  echo "$AUTH_LINE"
  echo "REVTUNNEL: PASS dry-run mode=$MODE port=$PORT"
  exit 0
fi

changed=0
if [ "$MODE" = system ]; then
  if ! printf '%s' "$UNIT_TEXT" | sudo -n cmp -s - "$DIR/$UNIT" 2>/dev/null; then
    printf '%s' "$UNIT_TEXT" | sudo -n tee "$DIR/$UNIT" >/dev/null || { echo "REVTUNNEL: FAIL cannot write $DIR/$UNIT"; exit 1; }
    changed=1
  fi
  SCTL="sudo -n systemctl"
else
  mkdir -p "$DIR"
  if ! printf '%s' "$UNIT_TEXT" | cmp -s - "$DIR/$UNIT" 2>/dev/null; then
    printf '%s' "$UNIT_TEXT" > "$DIR/$UNIT"; changed=1
  fi
  SCTL="systemctl --user"
  if [ "$(loginctl show-user "$ME" -p Linger --value 2>/dev/null)" != yes ]; then
    echo "REVTUNNEL: FAIL user unit needs linger: sudo loginctl enable-linger $ME"; exit 1
  fi
fi

$SCTL daemon-reload
if [ "$changed" = 1 ]; then
  $SCTL enable "$UNIT" >/dev/null 2>&1
  $SCTL restart "$UNIT"
else
  $SCTL enable --now "$UNIT" >/dev/null 2>&1
fi

echo "VPS authorized_keys line for ~$VPS_USER/.ssh/authorized_keys:"
echo "$AUTH_LINE"
state=$($SCTL is-active "$UNIT" 2>/dev/null)
if [ "$state" = active ]; then
  echo "REVTUNNEL: PASS mode=$MODE port=$PORT unit=$state changed=$changed"
else
  echo "REVTUNNEL: FAIL mode=$MODE port=$PORT unit=$state (the VPS key line may be missing)"
  exit 1
fi
