#!/usr/bin/env bash
# Push the fireball alert channel settings to the VPS.
#
# Fill in ~/.config/rovimen/alert_channels.env (0600; created with comments on
# the first run), then run this script. Mail settings go to
# /etc/rovimen-mail.env, the rest to /etc/rovimen-fireball.env (both
# root:rovimen 0640). Only keys with a value are written; an existing key is
# replaced, every other line is kept. The values travel over SSH stdin, never
# on a command line. Then the alert daemon restarts (it loads its env at
# start; the dashboard reads both files at use) and the channels and their
# routes are listed. Nothing is sent: run fireball_test_alert.py for that.
#
# Usage: bash tools/setup_alert_channels.sh [root@VPS]
set -euo pipefail

VPS="${1:-root@100.64.0.1}"
SRC="${ROVIMEN_ALERT_SETTINGS:-$HOME/.config/rovimen/alert_channels.env}"

if [ ! -f "$SRC" ]; then
  mkdir -p "$(dirname "$SRC")"
  umask 077
  cat > "$SRC" <<'EOF'
# Fireball alert channels. Fill in what you have; empty lines are skipped.
# Then: bash tools/setup_alert_channels.sh

# Email (Gmail): the account address and a 16-character app password
# (Google account -> Security -> 2-Step Verification -> App passwords).
ROVIMEN_SMTP_USER=
ROVIMEN_SMTP_PASSWORD=
# Optional extra recipients besides the dashboard subscribers, comma separated.
ROVIMEN_FIREBALL_EMAIL_TO=

# Discord: the channel's webhook URL (secret) and an invite link for members.
ROVIMEN_FIREBALL_DISCORD_WEBHOOK_URL=
ROVIMEN_FIREBALL_DISCORD_INVITE_URL=

# Telegram members' channel: its chat id (-100...) and its invite link.
ROVIMEN_FIREBALL_TELEGRAM_COMMUNITY_CHAT_ID=
ROVIMEN_FIREBALL_TELEGRAM_JOIN_URL=
EOF
  chmod 600 "$SRC"
  echo "Created $SRC: fill it in, then run this script again."
  exit 0
fi

# key=value lines with a value, comments and blanks dropped
VALUES="$(grep -E '^[A-Z_]+=.+' "$SRC" || true)"
if [ -z "$VALUES" ]; then
  echo "$SRC has no values yet."
  exit 1
fi
echo "Keys to set: $(echo "$VALUES" | cut -d= -f1 | tr '\n' ' ')"

# The program goes on the remote command line (no secrets in it); the values
# go through stdin.
MERGE=$(cat <<'PY'
import grp, os, sys
pairs = [l.rstrip("\n").split("=", 1) for l in sys.stdin if "=" in l]
MAIL = {"ROVIMEN_SMTP_USER", "ROVIMEN_SMTP_PASSWORD", "ROVIMEN_SMTP_HOST", "ROVIMEN_SMTP_PORT",
        "ROVIMEN_FIREBALL_EMAIL_FROM_NAME", "ROVIMEN_FIREBALL_EMAIL_TO"}
targets = {"/etc/rovimen-mail.env": {k: v for k, v in pairs if k in MAIL},
           "/etc/rovimen-fireball.env": {k: v for k, v in pairs if k not in MAIL}}
gid = grp.getgrnam("rovimen").gr_gid
for path, new in targets.items():
    if not new:
        continue
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    seen, out = set(), []
    for line in lines:
        k = line.split("=", 1)[0].strip()
        if k in new:
            out.append(f"{k}={new[k]}")
            seen.add(k)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in new.items() if k not in seen]
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(out) + "\n")
    os.chown(tmp, 0, gid)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)
    print(f"{path}: set {', '.join(sorted(new))}")
PY
)
echo "$VALUES" | ssh -o BatchMode=yes "$VPS" "python3 -c $(printf '%q' "$MERGE")"

ssh -o BatchMode=yes "$VPS" 'systemctl restart rovimen-fireball-alert && systemctl is-active rovimen-fireball-alert && journalctl -u rovimen-fireball-alert -n 5 --no-pager | grep -i "alert channels\|no alert channel" ; /opt/rovimen/venv/bin/python /opt/rovimen/fireball_test_alert.py --list'
