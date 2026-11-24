#!/usr/bin/env bash
# reboot_guard.sh — Safe reboot gate for ROVIMEN stations.
#
# Waits until morning_done is True in state.json for all stations, then
# reboots.  Replaces a bare sudo /sbin/reboot cron entry.
#
# Cron entry (runs as station user, uses sudo for reboot).  The hour is
# host-local — Debian/Ubuntu cron has no per-file timezone — and reads as UTC
# only because the fleet standard is a UTC host clock:
#   45 13 * * * gmn /home/gmn/rovimen_scripts/reboot_guard.sh >> /var/log/rovimen_reboot.log 2>&1
#
# How it works:
#   1. Reads state.json for each configured station under videocapture_path.
#   2. Checks that morning_done == true for the night that ended this morning.
#   3. Reboots once all stations are done.
#
# What it deliberately does NOT do: reboot on timeout.  A timeout means morning
# processing is still running or has wedged, which is precisely the state this
# guard exists to protect — and at 12:00 + 4 h the reboot would land near winter
# capture start.  On timeout the station is left up and an ERROR is logged.
#
# watchdog.allow_reboot=false in config.json disables the reboot entirely, for
# stations whose firmware hangs at POST and that only come back with an on-site
# power cycle.  Same flag, same meaning as in rovimen_watchdog.sh.

set -euo pipefail

CONFIG="${ROVIMEN_CONFIG:-$HOME/rovimen_scripts/config.json}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Overridable so the tests can exercise the timeout path without waiting 4 h.
# 20 min: dawn_process stops at 13:15 and its hard stop marks the night done by
# 13:20, so a guard starting at 13:45 normally reboots on its first poll. The
# wait ends at 14:05, before the earliest fleet sunset (14:19 UTC) either way.
MAX_WAIT_S="${ROVIMEN_REBOOT_MAX_WAIT:-1200}"
POLL_S="${ROVIMEN_REBOOT_POLL:-60}"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [reboot-guard] $*"; }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Return the night date of the most recently completed night — always
# yesterday, whatever the clock says.
#
# Night directories are named for the date capture STARTED, not the date it
# ended: color_capture.py rolls over at noon UTC, so a night running 21:00 on
# the 22nd through 04:00 on the 23rd lives entirely under 20260322.  The guard
# runs at 13:45 UTC, after dawn processing and before dusk, so the only night it
# can possibly be waiting on is the one that ended this morning — and that night
# began yesterday evening.
#
# Reading the clock here was the bug.  The cron fires at exactly 12:00, the old
# branch was `hour < 12`, so every run took the "night just starting" arm and
# waited on today's directory — which does not exist until dusk.  The wait could
# only ever end in the 4-hour timeout, which then rebooted anyway.
#
# Kept as a function so the naming rule has one documented home.
night_date() {
    date -u -d 'yesterday' '+%Y%m%d'
}

# Echo the station config as lines, in this order:
#   1. watchdog.allow_reboot as 1/0
#   2. one "<camera> <state.json path>" line per camera dawn_process marks done
# One parse for the whole run: every field the guard needs comes from the same
# read, so they can never disagree about which config was on disk.
#
# The state.json path comes from flags_manager._state_path, the function
# dawn_process writes through. The guard used to rebuild it from the configured
# capture path, which stops matching as soon as the colour disk dies and
# capture moves to the fallback root: gmnro01 (Ghirdoveni) waited on
# /mnt/data/color_capture/... while dawn marked ~/color_capture/... done, and
# had no 13:45 reboot from 2026-10-19 to 2026-10-26.
read_config() {
    python3 - "$CONFIG" "$SCRIPT_DIR" "$1" <<'PYEOF'
import json, sys

cfg = json.load(open(sys.argv[1]))
sys.path.insert(0, sys.argv[2])
import flags_manager

print('1' if cfg.get('watchdog', {}).get('allow_reboot', True) else '0')
# The cameras dawn_process marks done: it skips decommissioned ones
# (dawn_process._active_stations), so waiting on them would never end.
for sid, scfg in cfg.get('stations', {}).items():
    if not (scfg or {}).get('decommissioned'):
        print(sid, flags_manager._state_path(sid, sys.argv[3], cfg))
PYEOF
}

# Return 0 if morning_done is True for this station/date.
all_complete() {
    local state_file="$1"
    [ -f "$state_file" ] || return 1
    python3 - "$state_file" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
sys.exit(0 if data.get('morning_done', False) else 1)
PYEOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

log "Reboot guard starting  config=$CONFIG  max_wait=${MAX_WAIT_S}s  poll=${POLL_S}s"

NIGHT=$(night_date)
log "Night date: $NIGHT"

CONFIG_BLOB=$(read_config "$NIGHT")
mapfile -t CFG <<<"$CONFIG_BLOB"
ALLOW_REBOOT="${CFG[0]}"
# "<camera> <state.json>" per camera; a station-less config prints none.
STATE_LINES=("${CFG[@]:1}")

log "Stations: $(for l in "${STATE_LINES[@]}"; do printf '%s ' "${l%% *}"; done)"
for line in "${STATE_LINES[@]}"; do
    log "  ${line%% *} state: ${line#* }"
done

# Honour the same opt-out the watchdog honours.  On a station that hangs at POST
# a reboot costs an on-site visit, so there is nothing worth waiting for here.
if [ "$ALLOW_REBOOT" != "1" ]; then
    log "Reboot disabled by config (watchdog.allow_reboot=false) — nothing to guard"
    exit 0
fi

[ "${#STATE_LINES[@]}" -eq 0 ] && { log "No stations in config — skipping reboot"; exit 0; }

elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT_S" ]; do
    all_ok=true
    for line in "${STATE_LINES[@]}"; do
        station="${line%% *}"
        state_file="${line#* }"
        if ! all_complete "$state_file"; then
            log "  Waiting for $station/$NIGHT (state: $state_file)"
            all_ok=false
        else
            log "  OK: $station/$NIGHT"
        fi
    done

    if $all_ok; then
        log "All processes complete — rebooting"
        sudo /sbin/reboot
        exit 0
    fi

    sleep "$POLL_S"
    elapsed=$((elapsed + POLL_S))
done

log "ERROR: morning processing still incomplete after ${MAX_WAIT_S}s — NOT rebooting"
log "ERROR: station left running; investigate dawn_process for night $NIGHT"
exit 1
