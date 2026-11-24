#!/usr/bin/env bash
# toggle_rovimen.sh — Enable, disable, restart, or check ROVIMEN services and crons.
#
# Usage:
#   toggle_rovimen.sh on       — enable + start all services, install crons
#   toggle_rovimen.sh off      — stop + disable all services, remove crons
#   toggle_rovimen.sh restart  — restart currently running services (no cron changes)
#   toggle_rovimen.sh status   — show service and cron state

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_USER=$(whoami)
CONFIG="$SCRIPTS_DIR/config.json"

# ── Terminal colours ──────────────────────────────────────────────────────────
GRN='\033[0;32m'; YLW='\033[1;33m'; RED='\033[0;31m'; BLU='\033[0;34m'
BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

info() { echo -e "${GRN}  ✓${NC}  $*"; }
warn() { echo -e "${YLW}  ⚠${NC}  $*"; }
err()  { echo -e "${RED}  ✗${NC}  $*" >&2; }
dim()  { echo -e "${DIM}     $*${NC}"; }

# ── Services ──────────────────────────────────────────────────────────────────
SERVICES=(color-capture rovimen-station-api)
[ -f /etc/systemd/system/detection-indexer.service ] && SERVICES+=(detection-indexer)
# station-api is never stopped by 'off' — dashboard must stay reachable
STOPPABLE_SERVICES=(color-capture)
[ -f /etc/systemd/system/detection-indexer.service ] && STOPPABLE_SERVICES+=(detection-indexer)

# ── Cron files ────────────────────────────────────────────────────────────────
CRON_FILES=(rovimen-dawn rovimen-janitor rovimen-reboot)

# ── Venv detection (authoritative: read from RMS service file) ────────────────
_venv_python() {
    local venv
    # Try system-level RMS service files first, then user-level
    venv=$(grep -h 'ExecStart' /etc/systemd/system/rms-cam*.service 2>/dev/null \
        | grep -oP '/[^\s]+/bin/python3?' | head -1 || true)
    if [[ -z "$venv" ]]; then
        venv=$(grep -h 'ExecStart' "$HOME/.config/systemd/user"/rms-cam*.service 2>/dev/null \
            | grep -oP '/[^\s]+/bin/python3?' | head -1 || true)
    fi
    # Fall back to known venv locations
    if [[ -z "$venv" ]]; then
        for candidate in "$HOME/vRMS/bin/python3" "$HOME/RMS/venv/bin/python3" "$HOME/RMS/venv/bin/python"; do
            if [[ -x "$candidate" ]]; then venv="$candidate"; break; fi
        done
    fi
    echo "${venv:-python3}"
}

# ── Cron content ──────────────────────────────────────────────────────────────
# Root copies the cron files into /etc/cron.d, so they are staged in this
# directory, which only the station user can write. A fixed /tmp name can be
# created first by any local user, who then chooses what root installs.

# Copy one staged cron file into /etc/cron.d with the exact command that
# /etc/sudoers.d/rovimen allows (updater.sh _ensure_sudoers). A station with no
# other sudo (gmnro07 București, gmnro08 Brașov, gmnde00 Johannismühle) has the
# rule for this source only once the updater has rewritten that file, which it
# does after this step in the same pass; until then only the old /tmp source is
# allowed, so fall back to it. The /tmp copy is created with O_EXCL and no
# symlink following, so a name someone else made first fails the install
# instead of being copied by root.
_install_cron_file() {
    local job="$1"; shift
    local src="$SCRIPTS_DIR/.rovimen_cron_$job" dst="/etc/cron.d/rovimen-$job"
    local legacy="/tmp/rovimen_cron_$job" rc=0
    "$@" cp "$src" "$dst" 2>/dev/null && return 0
    rm -f "$legacy" 2>/dev/null || true
    python3 - "$src" "$legacy" <<'PY' || return 1
import os, sys
try:
    fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
except OSError as exc:
    sys.exit("cannot create %s: %s" % (sys.argv[2], exc))
with open(sys.argv[1], "rb") as src, os.fdopen(fd, "wb") as dst:
    dst.write(src.read())
PY
    "$@" cp "$legacy" "$dst" || rc=$?
    rm -f "$legacy"
    return "$rc"
}

_write_crons() {
    local venv_python
    venv_python=$(_venv_python)

    # Both wall-clock schedules below are stated in UTC and are only in UTC
    # because the fleet standard is a UTC host clock. Debian/Ubuntu cron
    # (ISC/Vixie 3.0pl1) evaluates schedules in the daemon's timezone and has no
    # per-file override: CRON_TZ is a cronie extension the binary does not
    # implement, and TZ= reaches the child process only, never the scheduler.
    # A station left on local time therefore runs these at the wrong UTC hour,
    # and the only fix is `timedatectl set-timezone UTC` on the host.
    cat > "$SCRIPTS_DIR/.rovimen_cron_dawn" <<EOF
# ROVIMEN morning processing — every 15 min from 04:00-10:00 UTC
# Times are host-local; the fleet standard is a UTC host clock.
# Processes post-RMS pipeline: EON lock → stack → timelapse → reencode → upload
*/15 4-12 * * * $INSTALL_USER nice -n 10 ionice -c 3 $venv_python $SCRIPTS_DIR/dawn_process.py >> $HOME/logs/rovimen_dawn.log 2>&1
EOF

    cat > "$SCRIPTS_DIR/.rovimen_cron_janitor" <<EOF
# ROVIMEN janitor — disk retention + pressure escalation every 10 min
# Deprioritised: the sweep walks every retention tier, and on a constrained
# host that I/O burst stalls the RTSP capture pipeline. Measured at Cahul
# (Pi 5, 2 cams): capture stalls ran 22% per chunk in minutes 0-2 of each
# 10-minute period against 4.6% in minutes 3-9 — a 4.6-5.0x excess aligned
# with this job, reproduced on both cameras across three nights, while the
# same janitor on a roomier host (Ghirdoveni, i7) costs nothing.
# Best-effort class 7, NOT idle (ionice -c 3) like the dawn cron: the janitor
# owns disk-pressure escalation, so it has to keep making progress on exactly
# the full-disk host where an idle-class job would be starved indefinitely.
*/10 * * * * $INSTALL_USER nice -n 19 ionice -c 2 -n 7 $venv_python $SCRIPTS_DIR/janitor_storage_watchdog.py -c $CONFIG >> $HOME/logs/rovimen_janitor.log 2>&1
EOF

    cat > "$SCRIPTS_DIR/.rovimen_cron_reboot" <<EOF
# ROVIMEN reboot guard — waits for morning_done, does not reboot on timeout
# 13:45 UTC every day: 30 min after the 13:15 dawn deadline, 34 min before the
# earliest sunset in the fleet (about 14:20 UTC, in December). Times are host-local;
# the fleet standard is a UTC host clock.
45 13 * * * $INSTALL_USER $SCRIPTS_DIR/reboot_guard.sh >> $HOME/logs/rovimen_reboot.log 2>&1
EOF

    # Only write cron.d files (requires sudo) if content has changed
    local need_write=0
    for pair in "dawn:/etc/cron.d/rovimen-dawn" "janitor:/etc/cron.d/rovimen-janitor" "reboot:/etc/cron.d/rovimen-reboot"; do
        local key="${pair%%:*}" dst="${pair##*:}"
        if ! diff -q "$SCRIPTS_DIR/.rovimen_cron_${key}" "$dst" > /dev/null 2>&1; then
            need_write=1
            break
        fi
    done

    local legacy_cron_exists=0
    [ -f /etc/cron.d/daily-reboot ] && legacy_cron_exists=1

    chmod +x "$SCRIPTS_DIR/reboot_guard.sh" 2>/dev/null || true

    # Run the exact commands that /etc/sudoers.d/rovimen allows (updater.sh
    # _ensure_sudoers). Probing with `sudo -n true` needs blanket sudo, so on
    # a station that has only those rules (gmnro07 București, gmnro08 Brașov,
    # gmnde00 Johannismühle) the probe failed and the crons stayed months old.
    # Prompt for a password only when someone is at the terminal.
    local sudo=(sudo -n)
    [ -t 0 ] && sudo=(sudo)

    if [ "$need_write" -eq 1 ] || [ "$legacy_cron_exists" -eq 1 ]; then
        if _install_cron_file dawn "${sudo[@]}" \
            && _install_cron_file janitor "${sudo[@]}" \
            && _install_cron_file reboot "${sudo[@]}" \
            && "${sudo[@]}" chmod 644 /etc/cron.d/rovimen-dawn /etc/cron.d/rovimen-janitor /etc/cron.d/rovimen-reboot; then
            if [ "$legacy_cron_exists" -eq 1 ] && "${sudo[@]}" rm /etc/cron.d/daily-reboot; then
                info "Removed old /etc/cron.d/daily-reboot"
            fi
            info "Cron files updated"
        else
            warn "Cron files need updating but sudo requires a password — run 'toggle_rovimen.sh on' manually to apply"
        fi
    else
        info "Cron files already current"
    fi

    rm -f "$SCRIPTS_DIR/.rovimen_cron_dawn" "$SCRIPTS_DIR/.rovimen_cron_janitor" "$SCRIPTS_DIR/.rovimen_cron_reboot"

    # User crontab — fix_cam_encoding @reboot + updater
    # Always strip existing rovimen entries first so re-running toggle on is idempotent.
    local existing
    existing=$(crontab -l 2>/dev/null || true)
    existing=$(echo "$existing" | grep -v "fix_cam_encoding" | grep -v "updater.sh" | grep -v "dawn_process" || true)
    existing="${existing}"$'\n'"@reboot sleep 120 && bash $SCRIPTS_DIR/fix_cam_encoding.sh"
    existing="${existing}"$'\n'"@reboot sleep 300 && nice -n 10 ionice -c 3 $venv_python $SCRIPTS_DIR/dawn_process.py >> $HOME/logs/rovimen_dawn.log 2>&1"
    existing="${existing}"$'\n'"30 15 * * * bash $SCRIPTS_DIR/fix_cam_encoding.sh >> $HOME/logs/rovimen_fixcam.log 2>&1"
    # After the 13:45 reboot (never during it), around the earliest sunset
    # (capture starts later, once the sun is well down); the updater adds up to
    # 10 min of jitter.
    existing="${existing}"$'\n'"0  14 * * * /usr/bin/bash $SCRIPTS_DIR/updater.sh >> $HOME/logs/updater.log 2>&1"
    existing="${existing}"$'\n'"15 14 * * * /usr/bin/bash $SCRIPTS_DIR/updater.sh >> $HOME/logs/updater.log 2>&1"
    # Late-afternoon retry for a station that missed both (offline, or its
    # updater deferred). 16:45 is after processing; on winter evenings it is
    # after dusk, but the updater only restarts rovimen services, never RMS.
    existing="${existing}"$'\n'"45 16 * * * /usr/bin/bash $SCRIPTS_DIR/updater.sh >> $HOME/logs/updater.log 2>&1"
    echo "$existing" | crontab -
    info "fix_cam_encoding @reboot + updater.sh crontab installed"
}

# ── Remove crons ──────────────────────────────────────────────────────────────
_remove_crons() {
    for f in "${CRON_FILES[@]}"; do
        if [ -f "/etc/cron.d/$f" ]; then
            sudo rm "/etc/cron.d/$f"
            info "Removed /etc/cron.d/$f"
        fi
    done

    local existing
    existing=$(crontab -l 2>/dev/null || true)
    local changed=false
    # fix_cam_encoding is intentionally NOT removed here — it must survive rovimen off
    # because cameras reboot independently of ROVIMEN services. Use 'fixcam off' to remove.
    if echo "$existing" | grep -q "updater.sh"; then
        existing=$(echo "$existing" | grep -v "updater.sh")
        changed=true
        info "Removed updater.sh from crontab"
    fi
    $changed && echo "$existing" | crontab - || true
}

# ── Fix-cam cron management (independent of rovimen on/off) ───────────────────
cmd_fixcam_off() {
    local existing
    existing=$(crontab -l 2>/dev/null || true)
    if echo "$existing" | grep -q "fix_cam_encoding"; then
        echo "$existing" | grep -v "fix_cam_encoding" | crontab -
        warn "fix_cam_encoding crons removed (cameras will not recover encoding after reboot)"
    else
        dim "fix_cam_encoding crons not installed — nothing to remove"
    fi
}

# ── Commands ──────────────────────────────────────────────────────────────────
cmd_on() {
    echo -e "\n${BOLD}${BLU}  ROVIMEN — Enabling${NC}\n"
    for svc in "${SERVICES[@]}"; do
        if [ -f "/etc/systemd/system/${svc}.service" ]; then
            sudo systemctl enable --now "$svc" 2>/dev/null \
                && info "Enabled + started $svc" \
                || warn "Could not start $svc"
        else
            warn "$svc.service not found — skipping"
        fi
    done
    echo
    _write_crons
    echo
    info "ROVIMEN is ON"
    echo
}

cmd_off() {
    local svcs=("${STOPPABLE_SERVICES[@]}")
    [[ "${2:-}" == "--include-api" ]] && svcs=("${SERVICES[@]}")
    echo -e "\n${BOLD}${BLU}  ROVIMEN — Disabling${NC}\n"
    for svc in "${svcs[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null \
                || systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            sudo systemctl stop    "$svc" 2>/dev/null || true
            sudo systemctl disable "$svc" 2>/dev/null || true
            info "Stopped + disabled $svc"
        else
            dim "$svc already inactive"
        fi
    done
    echo
    _remove_crons
    echo
    warn "ROVIMEN is OFF"
    echo
}

cmd_restart() {
    echo -e "\n${BOLD}${BLU}  ROVIMEN — Restarting services${NC}\n"
    local any=false
    for svc in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            sudo systemctl restart "$svc" 2>/dev/null \
                && info "Restarted $svc" \
                || err "Failed to restart $svc"
            any=true
        else
            dim "$svc not running — skipping"
        fi
    done
    $any || warn "No services were running — nothing restarted"
    echo
}

_svc_status() {
    # Returns "active/enabled [system]" or "active/enabled [user]" or "inactive/disabled"
    local svc="$1"
    local active enabled scope
    active=$(systemctl is-active  "$svc" 2>/dev/null || echo "inactive")
    enabled=$(systemctl is-enabled "$svc" 2>/dev/null || echo "disabled")
    if [ "$active" = "active" ]; then
        scope="system"
    else
        # Fallback: check user-level (for stations where services were installed --user)
        local u_active u_enabled
        u_active=$(systemctl --user is-active  "$svc" 2>/dev/null || echo "inactive")
        u_enabled=$(systemctl --user is-enabled "$svc" 2>/dev/null || echo "disabled")
        if [ "$u_active" = "active" ]; then
            active="$u_active"; enabled="$u_enabled"; scope="user"
        else
            scope=""
        fi
    fi
    echo "$active" "$enabled" "$scope"
}

cmd_status() {
    echo -e "\n${BOLD}${BLU}  ROVIMEN — Status${NC}\n"

    echo -e "  ${BOLD}Services:${NC}"
    for svc in "${SERVICES[@]}"; do
        local active enabled scope
        read -r active enabled scope <<< "$(_svc_status "$svc")"
        local scope_tag=""
        [ -n "$scope" ] && scope_tag=" ${DIM}[$scope]${NC}"
        if [ "$active" = "active" ]; then
            echo -e "    ${GRN}●${NC}  $svc — ${GRN}$active${NC} / $enabled${scope_tag}"
        else
            echo -e "    ${YLW}●${NC}  $svc — ${YLW}$active${NC} / $enabled"
        fi
    done

    echo
    echo -e "  ${BOLD}Cron files (/etc/cron.d/):${NC}"
    for f in "${CRON_FILES[@]}"; do
        if [ -f "/etc/cron.d/$f" ]; then
            echo -e "    ${GRN}✓${NC}  $f"
        else
            echo -e "    ${YLW}–${NC}  $f (not installed)"
        fi
    done

    echo
    echo -e "  ${BOLD}User crontab:${NC}"
    local tab
    tab=$(crontab -l 2>/dev/null || true)
    if echo "$tab" | grep -q "fix_cam_encoding"; then
        echo -e "    ${GRN}✓${NC}  fix_cam_encoding @reboot + 15:30 UTC"
    else
        echo -e "    ${YLW}–${NC}  fix_cam_encoding @reboot + 15:30 UTC (not installed)"
    fi
    if echo "$tab" | grep -q "updater.sh"; then
        echo -e "    ${GRN}✓${NC}  updater.sh (14:00, 14:15 + 16:45 retry)"
    else
        echo -e "    ${YLW}–${NC}  updater.sh (not installed)"
    fi
    echo
}

# ── Entry point ───────────────────────────────────────────────────────────────
case "${1:-}" in
    on)             cmd_on ;;
    off)            cmd_off "$@" ;;
    restart)        cmd_restart ;;
    status)         cmd_status ;;
    fixcam-off)     cmd_fixcam_off ;;
    --ensure-crons) _write_crons; info "Crons reconciled" ;;
    *)
        echo -e "Usage: $(basename "$0") ${BOLD}on${NC} | ${BOLD}off${NC} [--include-api] | ${BOLD}restart${NC} | ${BOLD}status${NC} | ${BOLD}fixcam-off${NC} | ${BOLD}--ensure-crons${NC}"
        exit 1
        ;;
esac
