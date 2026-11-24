#!/usr/bin/env bash
# updater.sh — Station script auto-updater (codename: hivemind)
#
# Pulls the latest station scripts from the VPS bundle (which CI keeps in sync
# with the rovimen-station-tools repo).  Safe: preserves config.json, backs up before
# updating, restarts affected services.
#
# CI pipeline:
#   main branch push        → deploy-dashboard.yml   → VPS /opt/rovimen/station-bundle/
#
# Channel selection (set in config.json):
#   "update_channel": "main"  (default) — stable releases
#   "update_channel": "dev"             — retired 2026-09-26 (the dev tier is gone);
#                                         treated as "main"
#
# Install as a systemd timer (recommended):
#   bash updater.sh --install-timer
#
# Or install as a cron job:
#   bash updater.sh --install-cron
#
# Run manually (no jitter delay):
#   bash ~/rovimen_scripts/updater.sh --now
#
# Preflight — would this station accept the bundle the VPS is serving, under
# enforce? Read-only, installs nothing, safe to run any time:
#   bash ~/rovimen_scripts/updater.sh --verify-only
#
# Cron/timer runs sleep a random 0-600s first so the fleet does not hit the VPS
# in one burst; ROVIMEN_UPDATE_JITTER_MAX=0 disables it.

set -euo pipefail

VPS_USER="root"
# Run as the station user. The manual `sudo bash updater.sh --install-sudoers`
# recovery runs as root: resolve the invoking user's home, or the sudoers rules
# would be written for root and point at /root/rovimen_scripts.
STATION_HOME="$HOME"
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    STATION_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    STATION_HOME="${STATION_HOME:-$HOME}"
fi
LOCAL_DIR="$STATION_HOME/rovimen_scripts"

# Read vps_host and update_channel from config.json
_read_cfg() {
python3 -c "
import json, sys
try:
    cfg = json.load(open('$LOCAL_DIR/config.json'))
    print(cfg.get('vps_host') or '')
    print(cfg.get('update_channel', 'main'))
except Exception:
    print('')
    print('main')
" 2>/dev/null || { echo ''; echo 'main'; }
}
{ read -r VPS_HOST; read -r UPDATE_CHANNEL; } <<< "$(_read_cfg)"

if [ -z "$VPS_HOST" ]; then
    echo "$(ts) [updater] No vps_host configured — skipping update"
    exit 0
fi

# One channel since 2026-09-26, when the dev tier and its bundle were retired:
# a station still set to "dev" (or anything else) follows main rather than
# being stranded on a bundle that no longer updates.
UPDATE_CHANNEL="main"
BUNDLE_PATH="/opt/rovimen/station-bundle"
VERSION_FILE="$LOCAL_DIR/.version"
BACKUP_DIR="$HOME/rovimen_scripts.bak"
STAGING_DIR="$HOME/rovimen_scripts.staging"
MODE_FILE="/etc/rovimen/update_verify_mode"
VERIFY_DIR="$HOME/rovimen_scripts.verify"
# Stable grep token for "this station refused a bundle" — kept identical to
# bundle_verify.REJECT_MARKER so one sweep over ~/logs/updater.log catches a
# rejection from either layer.
REJECT_MARKER="ROVIMEN_BUNDLE_REJECTED"
LOG_DIR="$HOME/logs"
LOG_FILE="$LOG_DIR/updater.log"

SERVICES=(
    color-capture
    rovimen-station-api
    # Restart on every update so long-running processes pick up shipped code —
    # detection-indexer applies its schema migrations only at startup, and a
    # stale indexer serving a station_api that queries new columns breaks
    # /api/detections-index ("no such column"), silencing detection pushes.
    detection-indexer
    rovimen-pusher
)

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG_FILE"; }

mkdir -p "$LOG_DIR"

# ── Sudoers reconciliation ─────────────────────────────────────────────────────
# Keeps /etc/sudoers.d/rovimen up to date so all station management operations
# (service control, cron installs, service file updates, reboot) work without
# password prompts, even when running unattended.  Safe to run on every update.

_ensure_sudoers() {
    local sudoers_file="/etc/sudoers.d/rovimen"
    local _systemctl _cp _cat _chmod _mkdir _rm _tee _reboot _loginctl
    _systemctl=$(command -v systemctl)
    _cp=$(command -v cp)
    _cat=$(command -v cat)
    _mkdir=$(command -v mkdir)
    _chmod=$(command -v chmod)
    _rm=$(command -v rm)
    _tee=$(command -v tee)
    _reboot=$(command -v reboot 2>/dev/null || echo /sbin/reboot)
    _loginctl=$(command -v loginctl 2>/dev/null || echo /usr/bin/loginctl)
    local U
    # The station user: SUDO_USER only under `sudo updater.sh` (running as
    # root). Under `sudo -u <station user>` SUDO_USER is the admin who ran
    # sudo; honouring it wrote the admin's rules over rmsuser's on gmnro08
    # (2026-09-25), leaving rmsuser without its restart rights.
    if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
        U=$SUDO_USER
    else
        U=$(whoami)
    fi

    # Modern sudo (>= 1.9.10) rejects wildcards in command arguments, so the old
    # "cp * /etc/systemd/system/*.service" catch-all is enumerated per unit file.
    local svc_rules="" f
    for f in "$LOCAL_DIR"/*.service "$LOCAL_DIR"/*.timer; do
        [ -f "$f" ] || continue
        svc_rules+="$U ALL=(root) NOPASSWD: $_cp $f /etc/systemd/system/$(basename "$f")"$'\n'
    done

    # VAAPI ground rule: exact-argument apt-get/usermod rules, generated by the
    # script that runs them so the two can never disagree.
    local vaapi_rules=""
    if [ -f "$LOCAL_DIR/ensure_vaapi.sh" ]; then
        vaapi_rules=$(bash "$LOCAL_DIR/ensure_vaapi.sh" --print-sudoers "$U" 2>/dev/null || true)
        [ -n "$vaapi_rules" ] && vaapi_rules+=$'\n'
    fi

    # DNS recovery (rovimen_watchdog.sh recovery_dns) re-activates the
    # NetworkManager connection that carries the default route. One exact rule
    # naming that connection by UUID; none without NetworkManager or a route.
    local dns_rules="" _nmcli _uplink_dev _uplink_uuid
    _nmcli=$(command -v nmcli 2>/dev/null || true)
    if [ -n "$_nmcli" ]; then
        _uplink_dev=$(ip route show default 2>/dev/null \
            | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}' || true)
        if [ -n "$_uplink_dev" ]; then
            _uplink_uuid=$(timeout 15 "$_nmcli" -g GENERAL.CON-UUID device show "$_uplink_dev" \
                2>/dev/null | head -1 || true)
        fi
        if [ -n "${_uplink_uuid:-}" ]; then
            dns_rules="# DNS recovery: re-activate the uplink connection (rovimen_watchdog.sh)"$'\n'
            dns_rules+="$U ALL=(root) NOPASSWD: $_nmcli -w 30 connection up uuid $_uplink_uuid"$'\n'
        fi
    fi

    local desired
    desired=$(cat <<EOF
# ROVIMEN — passwordless operations for station management scripts
# systemctl: all service control + daemon-reload
$U ALL=(root) NOPASSWD: $_systemctl
# cron.d file management (toggle_rovimen.sh stages the files in $LOCAL_DIR).
# The /tmp sources are the old staging path, kept for the changeover (the
# toggle_rovimen.sh of a reverted bundle still uses them); a later cleanup drops them.
$U ALL=(root) NOPASSWD: $_cp $LOCAL_DIR/.rovimen_cron_dawn /etc/cron.d/rovimen-dawn
$U ALL=(root) NOPASSWD: $_cp $LOCAL_DIR/.rovimen_cron_janitor /etc/cron.d/rovimen-janitor
$U ALL=(root) NOPASSWD: $_cp $LOCAL_DIR/.rovimen_cron_reboot /etc/cron.d/rovimen-reboot
$U ALL=(root) NOPASSWD: $_cp /tmp/rovimen_cron_dawn /etc/cron.d/rovimen-dawn
$U ALL=(root) NOPASSWD: $_cp /tmp/rovimen_cron_janitor /etc/cron.d/rovimen-janitor
$U ALL=(root) NOPASSWD: $_cp /tmp/rovimen_cron_reboot /etc/cron.d/rovimen-reboot
$U ALL=(root) NOPASSWD: $_chmod 644 /etc/cron.d/rovimen-dawn /etc/cron.d/rovimen-janitor /etc/cron.d/rovimen-reboot
$U ALL=(root) NOPASSWD: $_rm /etc/cron.d/rovimen-dawn
$U ALL=(root) NOPASSWD: $_rm /etc/cron.d/rovimen-janitor
$U ALL=(root) NOPASSWD: $_rm /etc/cron.d/rovimen-reboot
$U ALL=(root) NOPASSWD: $_rm /etc/cron.d/daily-reboot
# systemd service file updates (updater.sh — one explicit rule per bundled unit)
$U ALL=(root) NOPASSWD: $_tee /etc/systemd/system/rovimen-updater.service
$U ALL=(root) NOPASSWD: $_tee /etc/systemd/system/rovimen-updater.timer
# Enable lingering so operator --user units (archive-upload) survive logout and
# run at boot — required for the updater to reconcile user units unattended.
$U ALL=(root) NOPASSWD: $_loginctl enable-linger $U
# Focus helper retirement (updater.sh _retire_camera_focus)
$U ALL=(root) NOPASSWD: $_rm -f /etc/systemd/system/camera-focus.service
# Persistent journal drop-in (updater.sh _ensure_persistent_journal)
$U ALL=(root) NOPASSWD: $_mkdir -p /etc/systemd/journald.conf.d
$U ALL=(root) NOPASSWD: $_tee /etc/systemd/journald.conf.d/50-rovimen-persistent.conf
${svc_rules}${vaapi_rules}${dns_rules}# Self-update this sudoers file (updater.sh). Reading it back is what lets an
# unchanged file be left alone; without it a station with only these rules
# rewrote the file on every run.
$U ALL=(root) NOPASSWD: $_cat $sudoers_file
$U ALL=(root) NOPASSWD: $_tee $sudoers_file
$U ALL=(root) NOPASSWD: $_chmod 440 $sudoers_file
# Station reboot (station_api.py)
$U ALL=(root) NOPASSWD: $_reboot
EOF
)

    local current
    current=$(sudo -n cat "$sudoers_file" 2>/dev/null || true)
    if [ "$current" = "$desired" ]; then
        log "Sudoers already current"
        return
    fi

    # A syntax error in a sudoers.d file disables sudo for everyone, and this
    # file is written unattended — parse it before installing it.
    local _visudo
    _visudo=$(command -v visudo 2>/dev/null || { [ -x /usr/sbin/visudo ] && echo /usr/sbin/visudo; } || true)
    if [ -n "$_visudo" ]; then
        local _check
        _check=$(mktemp)
        echo "$desired" > "$_check"
        if ! "$_visudo" -c -q -f "$_check" > /dev/null 2>&1; then
            rm -f "$_check"
            log "ERROR: generated sudoers does not parse — keeping the current file"
            return
        fi
        rm -f "$_check"
    fi

    if echo "$desired" | sudo -n tee "$sudoers_file" > /dev/null 2>&1; then
        # tee keeps the mode of a file it overwrites; only a new file needs it.
        # /etc/sudoers.d is world-readable, so both checks need no sudo.
        if [ "$(stat -c %a "$sudoers_file" 2>/dev/null)" != 440 ]; then
            sudo -n chmod 440 "$sudoers_file" 2>/dev/null || true
        fi
        # Old narrower rule; full-sudo stations only, so no rule for it here.
        if [ -e /etc/sudoers.d/rovimen-restart ]; then
            sudo -n rm -f /etc/sudoers.d/rovimen-restart 2>/dev/null || true
        fi
        log "Sudoers updated"
    else
        log "WARN: Could not update sudoers (sudo -n failed) — run 'sudo bash ~/rovimen_scripts/updater.sh --install-sudoers' manually"
    fi
}

# ── Persistent journal ────────────────────────────────────────────────────────
# The journal is the record of who changed a station: sshd logs the key
# fingerprint of every login and sudo logs every command, and all humans log in
# as the same station user. Raspberry Pi OS ships Storage=volatile
# (/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf) and every
# station reboots at least daily, so without this drop-in that record ends at
# the last boot (gmnro16 Bacău, 2026-09-25). A drop-in that already sets
# Storage=persistent is left as it is, size cap included (gmnmd01 Cahul). It
# takes effect at the next boot; journald is not restarted here.
_ensure_persistent_journal() {
    local dropin=/etc/systemd/journald.conf.d/50-rovimen-persistent.conf
    if grep -qx 'Storage=persistent' "$dropin" 2>/dev/null; then
        return
    fi
    if sudo -n mkdir -p /etc/systemd/journald.conf.d 2>/dev/null \
        && printf '%s\n' '# ROVIMEN (updater.sh): keep the journal across reboots.' \
            '[Journal]' 'Storage=persistent' \
            | sudo -n tee "$dropin" > /dev/null 2>&1; then
        log "Persistent journal configured in $dropin (active from the next boot)"
    else
        log "WARN: could not write $dropin (sudo -n failed)"
    fi
}

# ── Focus helper retirement ───────────────────────────────────────────────────
# camera_focus.py (camera-focus.service) was a legacy focus helper.
# It left the bundle on 2026-11-22. Stations that still run it stop and disable
# it here (the systemctl rule, every station) and drop its unit file (the exact
# rm rule from _ensure_sudoers, which a restricted-sudo station has from its
# second pass on). The stale copies in ~/rovimen_scripts go too: the apply
# rsync has no --delete, and the unit copy kept a cp-install sudoers rule.
_retire_camera_focus() {
    local unit=camera-focus.service
    local unit_file=/etc/systemd/system/camera-focus.service
    local listed
    rm -f "$LOCAL_DIR/camera_focus.py" "$LOCAL_DIR/$unit"
    listed=$(systemctl list-unit-files "$unit" --no-legend 2>/dev/null || true)
    case "$listed" in "$unit"*) ;; *) return 0 ;; esac
    if sudo -n systemctl disable --now "$unit" 2>/dev/null; then
        log "Focus helper stopped and disabled ($unit)"
    else
        log "WARN: could not disable $unit (sudo -n refused)"
    fi
    if sudo -n rm -f "$unit_file" 2>/dev/null; then
        sudo -n systemctl daemon-reload 2>/dev/null || true
        log "Focus helper unit file removed ($unit_file)"
    else
        log "WARN: could not remove $unit_file yet (its sudoers rule lands at the end of this run)"
    fi
}

# ── VAAPI ground rule ─────────────────────────────────────────────────────────
# A station with a usable GPU encodes on VAAPI: ensure_vaapi.sh installs the VA
# driver packages if missing, adds the user to render,video and verifies with a
# real h264_vaapi encode (verdict in ~/rovimen_scripts/vaapi_status.json). Runs
# on every updater pass so the fleet converges without an operator; the sudo
# rules it needs come from _ensure_sudoers.
#
# Gate: granting render access flips services.reencode.full_night "auto" to ON
# (dawn_process._full_night_reencode_enabled keys on a working VAAPI device).
# The unattended rollout therefore waits for the dawn_process that carries the
# full-night deadline policy, which declares FULL_NIGHT_DEADLINE_POLICY at
# module level. Without it the step only logs and skips.
VAAPI_POLICY_MARKER="FULL_NIGHT_DEADLINE_POLICY"

_ensure_vaapi_ground_rule() {
    local script="$LOCAL_DIR/ensure_vaapi.sh"
    [ -f "$script" ] || return 0
    if ! grep -qE "^${VAAPI_POLICY_MARKER}\b" "$LOCAL_DIR/dawn_process.py" 2>/dev/null; then
        log "VAAPI ground rule: skipped — dawn_process.py lacks $VAAPI_POLICY_MARKER (full-night deadline policy not in this bundle)"
        return 0
    fi
    local out line
    out=$(timeout 600 bash "$script" --non-interactive 2>&1) || true
    while IFS= read -r line; do log "$line"; done <<< "$out"
}

# ── Install helpers ────────────────────────────────────────────────────────────

install_timer() {
    local unit_dir="/etc/systemd/system"
    local service_file="$unit_dir/rovimen-updater.service"
    local timer_file="$unit_dir/rovimen-updater.timer"
    local script="$LOCAL_DIR/updater.sh"

    INSTALL_USER=$(whoami)

    sudo -n tee "$service_file" > /dev/null <<EOF || { echo "ERROR: sudo -n refused writing $service_file (no sudoers rule?)"; exit 1; }
[Unit]
Description=ROVIMEN station script updater
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$INSTALL_USER
ExecStart=/usr/bin/bash $script
StandardOutput=journal
StandardError=journal
EOF

    sudo -n tee "$timer_file" > /dev/null <<EOF || { echo "ERROR: sudo -n refused writing $timer_file (no sudoers rule?)"; exit 1; }
[Unit]
Description=Run ROVIMEN updater after the 13:45 reboot (14:00, 14:15) with 16:45 retry

[Timer]
OnCalendar=*-*-* 14:00:00
OnCalendar=*-*-* 14:15:00
OnCalendar=*-*-* 16:45:00
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF

    sudo -n systemctl daemon-reload || { echo "ERROR: sudo -n systemctl daemon-reload refused"; exit 1; }
    sudo -n systemctl enable --now rovimen-updater.timer || { echo "ERROR: sudo -n systemctl enable refused"; exit 1; }
    echo "Installed and enabled rovimen-updater.timer (14:00, 14:15 + 16:45 retry)"
    systemctl status rovimen-updater.timer --no-pager
}

install_cron() {
    local script="$LOCAL_DIR/updater.sh"
    # Two attempts after the fixed 13:45 UTC reboot (14:00, 14:15) plus a
    # late-afternoon retry at 16:45.
    local cron_entries=(
        "0  14 * * * /usr/bin/bash $script >> $LOG_FILE 2>&1"
        "15 14 * * * /usr/bin/bash $script >> $LOG_FILE 2>&1"
        "45 16 * * * /usr/bin/bash $script >> $LOG_FILE 2>&1"
    )
    if crontab -l 2>/dev/null | grep -qF "updater.sh"; then
        echo "Cron entries already exist."
    else
        local tmp
        tmp=$(crontab -l 2>/dev/null || true)
        for entry in "${cron_entries[@]}"; do
            tmp="${tmp}"$'\n'"${entry}"
        done
        echo "$tmp" | crontab -
        echo "Cron jobs installed: 14:00, 14:15 + 16:45 retry."
    fi
}

# ── Service reconciliation ─────────────────────────────────────────────────────
# Shared by the update path AND the already-up-to-date path (self-heal):
# a station can gain its push identity (pusher onboard) or lose a unit
# AFTER reaching the latest bundle, so drift must never wait for the next
# release. Both functions are idempotent and restart nothing unless a
# unit or env file actually changed.
reconcile_units() {
# ── Systemd unit reconciliation ────────────────────────────────────────────────
# Reconcile every bundled unit to its desired state so a config flag flip (or a
# newly-shipped agent) takes effect fleet-wide on a plain update — no re-onboard.
# Handles BOTH system units (/etc/systemd/system) and operator --user units
# (~/.config/systemd/user, e.g. archive-upload). Idempotent: installs missing
# units, refreshes changed ones, reloads the right scope. Enable/start happens
# AFTER config migration (see below) so runtime gates like archive.enabled are
# read post-flip.
svc_reload=false
user_reload=false
svc_user=$(whoami)
svc_uid=$(id -u)
USER_UNIT_DIR="$HOME/.config/systemd/user"

_retire_camera_focus

# Units that run in the operator's --user scope.
# archive-upload is %h-templated (no User=, no venv), so a fresh install is
# station-agnostic — safe to create anywhere.
# sky-dome-render is the same shape: %h-templated, ExecStart is /usr/bin/env
# python3 over a stdlib-only script (celestial_dome needs numpy + PIL, both
# present in system python3 on the fleet), so it is venv-independent and safe
# to install anywhere. Installing the unit does NOT start it — enablement is
# gated on services.sky_dome_render.enabled below, default OFF, so the file
# lands fleet-wide and each station is switched on deliberately.
# rovimen-fireball-watch is the same shape (%h-templated, system python3,
# stdlib-only) and is a user unit so it installs on the first updater pass:
# a net-new SYSTEM unit needs two passes (the sudoers cp rule for it is only
# written at the end of the first).
USER_UNITS=("archive-upload.service" "sky-dome-render.service" "rovimen-fireball-watch.service")
# System units we ENSURE are present even if a station never had them. Only
# units whose ExecStart is venv-independent (/usr/bin/env python3, stdlib-only
# scripts) belong here — a fresh install of a venv-pinned unit from the bundle
# default would mis-path the interpreter. detection-indexer + rovimen-pusher
# are the push-architecture pair every station needs; the pusher runs under
# system python3. detection-indexer runs under the RMS venv (its FF thumbnails
# need numpy) and is in SYSTEM_PYTHON_FALLBACK_UNITS: with no venv found it is
# localised to /usr/bin/env python3 instead, so it still installs anywhere.
# Other system units are only *refreshed* when they already exist; net-new
# venv-pinned units are provisioned at onboard. rovimen-watchdog runs the
# bundled shell script under /usr/bin/bash — venv-independent, ensure-safe.
# midnight-lock IS venv-pinned (needs numpy/PIL/flags_manager), but the install
# loop runs _localise_unit -> _unit_exec_ok BEFORE the ensure branch, so it is
# only installed once the localised venv interpreter actually exists; if the
# venv is not found the install is skipped (no 203/EXEC) and self-heals next
# pass. The pass is additive-only, so a delayed install is harmless.
# spectro-pipeline follows the midnight-lock precedent: venv-pinned but
# ensure-safe because _localise_unit gates the install on the venv existing,
# and the script itself exits immediately on stations with no
# stations.<code>.spectro config block, so fleet-wide presence is inert.
SYSTEM_ENSURE_UNITS=("detection-indexer.service" "rovimen-pusher.service" "rovimen-watchdog.service" "midnight-lock.service" "spectro-pipeline.service")
# Venv-pinned units whose script also runs under system python3 (with less:
# no FF thumbnails for the indexer), localised to it when no venv is found.
SYSTEM_PYTHON_FALLBACK_UNITS=("detection-indexer.service")

_is_in_list() { local n="$1"; shift; local x; for x in "$@"; do [ "$x" = "$n" ] && return 0; done; return 1; }

# Resolve THIS station's RMS venv interpreter for a venv-pinned unit. The bundle
# template hardcodes /home/gmn/RMS/venv/bin/python, but real installs put the venv
# at ~/vRMS, ~/RMS/venv, ~/gmn_rms/venv_rms, … . We MUST end up with a path that
# actually exists: a non-existent interpreter makes systemd crash-loop the unit
# with status=203/EXEC (the color-capture clobber trap). Prefer the interpreter of
# an already-installed reference unit, but ONLY if it still exists — otherwise the
# previous "lift verbatim" behaviour just perpetuates a broken path. Fall back to
# probing the standard locations by existence. Empty result => no venv here.
_detect_venv_python() {
    local ref="$1" cand
    if [ -n "$ref" ] && [ -f "$ref" ]; then
        cand=$(grep -oE '/[^ ]*/bin/python[0-9.]*' "$ref" 2>/dev/null | head -n1 || true)
        [ -n "$cand" ] && [ -x "$cand" ] && { printf '%s\n' "$cand"; return 0; }
    fi
    # Then the interpreter RMS itself runs under, which is what dawn uses
    # (toggle_rovimen.sh _venv_python). Probing alone can pick the wrong venv
    # where a station has two (gmnro09 Sibiciu and gmnde00 Johannismühle: RMS
    # runs ~/RMS/venv, the probe finds ~/vRMS first).
    cand=$(grep -h 'ExecStart' /etc/systemd/system/rms-cam*.service "$HOME"/.config/systemd/user/rms-cam*.service 2>/dev/null \
        | grep -oE '/[^ ]+/bin/python[0-9.]*' | head -n1 || true)
    [ -n "$cand" ] && [ -x "$cand" ] && { printf '%s\n' "$cand"; return 0; }
    for cand in "$HOME/vRMS/bin/python" "$HOME/RMS/venv/bin/python" \
                "$HOME/gmn_rms/venv_rms/bin/python" "$HOME/source/RMS/vRMS/bin/python"; do
        [ -x "$cand" ] && { printf '%s\n' "$cand"; return 0; }
    done
    return 1
}

# Localise a bundle unit (templated for gmn / /home/gmn) to THIS station in
# place. Rewrites the venv interpreter to one that exists here (see
# _detect_venv_python); a unit with no venv found keeps the bundle default (the
# pusher/archive agents use system python3, so they don't depend on a venv).
# Editing $svc_src in place keeps the copy source path identical so the NOPASSWD
# sudoers cp rule matches.
_localise_unit() {
    local src="$1" ref="$2" venv_py="" args
    venv_py=$(_detect_venv_python "$ref") || venv_py=""
    if [ -z "$venv_py" ] && _is_in_list "$(basename "$src")" "${SYSTEM_PYTHON_FALLBACK_UNITS[@]}"; then
        venv_py="/usr/bin/env python3"
    elif [ -z "$venv_py" ] && grep -q '/bin/python' "$src"; then
        log "WARNING: no RMS venv found for venv-pinned unit $(basename "$src"); keeping bundle default (may 203/EXEC)"
    fi
    args=(
        -e "s|^User=gmn\$|User=$svc_user|"
        -e "s|^Group=gmn\$|Group=$svc_user|"
        -e "s|/home/gmn/rovimen_scripts|$HOME/rovimen_scripts|g"
    )
    # Rewrite the interpreter regardless of the template's venv layout
    # (/home/gmn/RMS/venv/bin/python, /home/gmn/vRMS/bin/python, …). [^ ]* cannot
    # cross the space to the script arg, so only the interpreter token matches.
    # The second rule re-targets an interpreter ALREADY localised to $HOME on a
    # previous run: units are edited in place, so a run that transiently could not
    # find the venv (e.g. RMS venv mid-rebuild) freezes a dead $HOME/RMS/venv path
    # into the template — the /home/gmn rule can no longer re-home it, and the
    # frozen unit would be re-copied over a healthy installed one every run. Re-
    # pointing $HOME/*/bin/python lets the next run self-heal to the live venv.
    if [ -n "$venv_py" ]; then
        args+=(-e "s|/home/gmn/[^ ]*/bin/python[0-9.]*|$venv_py|")
        args+=(-e "s|$HOME/[^ ]*/bin/python[0-9.]*|$venv_py|")
        # Same self-heal for a fallback unit localised to system python3 on a
        # run that found no venv: re-pin it once the venv is back.
        if _is_in_list "$(basename "$src")" "${SYSTEM_PYTHON_FALLBACK_UNITS[@]}"; then
            args+=(-e "s|^ExecStart=/usr/bin/env python3 |ExecStart=$venv_py |")
        fi
    fi
    args+=(-e "s|/home/gmn/|$HOME/|g")
    sed -i "${args[@]}" "$src"
}

# Guard against installing a unit whose ExecStart interpreter/binary is an
# absolute path that does not exist: systemd would crash-loop it with 203/EXEC
# and take the service down (for the station API, that also drops the station
# off the dashboard). Belt-and-braces alongside _detect_venv_python — even if
# localisation produced a dead interpreter (transient venv miss), never clobber
# a possibly-working installed unit with a broken one. Relative interpreters
# (e.g. `/usr/bin/env python3`) and %h specifiers are left to systemd.
_unit_exec_ok() {
    local f="$1" interp
    interp=$(sed -n 's/^ExecStart=\([^ ]*\).*/\1/p' "$f" | head -n1)
    case "$interp" in
        /*) [ -x "$interp" ] ;;
        *)  return 0 ;;
    esac
}

for svc_src in "$LOCAL_DIR"/*.service; do
    [ -f "$svc_src" ] || continue
    svc_name=$(basename "$svc_src")
    # The updater's own units are managed by --install-timer, not bundle edits.
    case "$svc_name" in rovimen-updater.service|rovimen-updater.timer) continue;; esac

    if _is_in_list "$svc_name" "${USER_UNITS[@]}"; then
        # ---- operator --user unit ----
        mkdir -p "$USER_UNIT_DIR"
        svc_dst="$USER_UNIT_DIR/$svc_name"
        _localise_unit "$svc_src" "$svc_dst"
        if ! _unit_exec_ok "$svc_src"; then
            log "WARN: $svc_name localised to a missing interpreter ($(sed -n 's/^ExecStart=\([^ ]*\).*/\1/p' "$svc_src" | head -n1)) — keeping existing user unit, not (re)installing"
            continue
        fi
        if [ ! -f "$svc_dst" ]; then
            cp "$svc_src" "$svc_dst" && { log "Installed user unit: $svc_name"; user_reload=true; }
        elif ! diff -q "$svc_src" "$svc_dst" >/dev/null 2>&1; then
            cp "$svc_src" "$svc_dst" && { log "Updated user unit: $svc_name"; user_reload=true; }
        fi
        continue
    fi

    # ---- system unit ----
    svc_dst="/etc/systemd/system/$svc_name"
    _localise_unit "$svc_src" "$svc_dst"
    if ! _unit_exec_ok "$svc_src"; then
        log "WARN: $svc_name localised to a missing interpreter ($(sed -n 's/^ExecStart=\([^ ]*\).*/\1/p' "$svc_src" | head -n1)) — keeping existing unit, not (re)installing"
        continue
    fi
    if [ -f "$svc_dst" ]; then
        if ! diff -q "$svc_src" "$svc_dst" >/dev/null 2>&1; then
            log "Updating service file: $svc_name"
            sudo -n cp "$svc_src" "$svc_dst" 2>/dev/null && svc_reload=true || log "WARN: Failed to update $svc_name (no sudo?)"
        fi
    elif _is_in_list "$svc_name" "${SYSTEM_ENSURE_UNITS[@]}"; then
        log "Installing missing system unit: $svc_name"
        sudo -n cp "$svc_src" "$svc_dst" 2>/dev/null && svc_reload=true || log "WARN: Failed to install $svc_name (no sudoers rule?)"
    fi
done

# Some stations run color-capture as a --user unit fitted by hand at onboard
# (gmnro06 Cluj), which the loop above never reconciles: copying the system
# template over it would bring User= and WantedBy=multi-user.target into a user
# unit. Those copies still carry the bundle's old StartLimit keys in [Service],
# where systemd ignores StartLimitIntervalSec ("Unknown key"). Move just those
# keys into [Unit] as the bundle now has them, and leave the rest of the file.
_fix_user_start_limit() {
    local f="$1" tmp have
    [ -f "$f" ] || return 1
    awk '/^\[/{s=$0} /^StartLimit(IntervalSec|Burst)=/ && s!="[Unit]"{bad=1} END{exit !bad}' "$f" || return 1
    have=$(awk '/^\[/{s=$0} /^StartLimitIntervalSec=/ && s=="[Unit]"{print 1; exit}' "$f")
    tmp="$f.tmp.$$"
    awk -v done="${have:-0}" '
        /^\[/ { s = $0 }
        /^StartLimit(IntervalSec|Burst)=/ && s != "[Unit]" { next }
        { print }
        $0 == "[Unit]" && !done { print "StartLimitIntervalSec=0"; done = 1 }
    ' "$f" > "$tmp" && mv "$tmp" "$f"
}
if _fix_user_start_limit "$USER_UNIT_DIR/color-capture.service"; then
    log "Moved color-capture (--user) StartLimit keys into [Unit]"
    user_reload=true
fi

# ---- bundled system timers ----
# Reconcile every *.timer in the bundle alongside its paired .service (a timer
# is installed only once that service unit exists on disk). Covers
# rovimen-watchdog.timer and midnight-lock.timer. The sudoers cp rule for .timer
# files is generated by _ensure_sudoers at the end of this run, so on the first
# pass with a net-new timer the cp can fail — it self-heals on the next updater
# pass (same two-pass model as new .service units).
for tmr_src in "$LOCAL_DIR"/*.timer; do
    [ -f "$tmr_src" ] || continue
    tmr_name=$(basename "$tmr_src")
    case "$tmr_name" in rovimen-updater.timer) continue;; esac
    paired_service="/etc/systemd/system/${tmr_name%.timer}.service"
    [ -f "$paired_service" ] || continue
    tmr_dst="/etc/systemd/system/$tmr_name"
    if [ ! -f "$tmr_dst" ] || ! diff -q "$tmr_src" "$tmr_dst" >/dev/null 2>&1; then
        log "Installing timer: $tmr_name"
        sudo -n cp "$tmr_src" "$tmr_dst" 2>/dev/null && svc_reload=true || log "WARN: Failed to install $tmr_name (sudoers rule lands after this run)"
    fi
done

if $svc_reload; then
    sudo -n systemctl daemon-reload 2>/dev/null && log "systemd daemon reloaded" || log "WARN: daemon-reload failed"
fi
if $user_reload; then
    XDG_RUNTIME_DIR="/run/user/$svc_uid" systemctl --user daemon-reload 2>/dev/null && log "systemd --user daemon reloaded" || log "WARN: --user daemon-reload failed"
fi

# Enable each bundled system timer once its unit files are on disk (blanket
# systemctl sudoers rule covers this even on the first pass).
for tmr_src in "$LOCAL_DIR"/*.timer; do
    [ -f "$tmr_src" ] || continue
    tmr_name=$(basename "$tmr_src")
    case "$tmr_name" in rovimen-updater.timer) continue;; esac
    tmr_dst="/etc/systemd/system/$tmr_name"
    paired_service="/etc/systemd/system/${tmr_name%.timer}.service"
    [ -f "$tmr_dst" ] && [ -f "$paired_service" ] || continue
    if [ "$(systemctl is-enabled "$tmr_name" 2>/dev/null)" != "enabled" ]; then
        sudo -n systemctl enable --now "$tmr_name" 2>/dev/null \
            && log "Enabled $tmr_name" \
            || log "WARN: could not enable $tmr_name"
    fi
done
}

reconcile_service_state() {
# ── Enable/start reconciliation ────────────────────────────────────────────────
# Unit files + config are now current; bring services to their desired
# ENABLED/ACTIVE state. This is what makes a config flag flip take effect on a
# plain update (previously only a full re-onboard would install/enable a unit).

# archive-upload: operator --user unit gated by services.archive_upload.enabled.
if [ -f "$USER_UNIT_DIR/archive-upload.service" ]; then
    au_enabled=$(python3 - "$LOCAL_DIR/config.json" <<'PY' 2>/dev/null
import json,sys
try:
    c=json.load(open(sys.argv[1]))
    print("true" if ((c.get("services") or {}).get("archive_upload") or {}).get("enabled", True) else "false")
except Exception:
    print("true")
PY
)
    [ -z "$au_enabled" ] && au_enabled=true
    if [ "$au_enabled" = "true" ]; then
        # Propagate the pusher's ingest identity to the byte uploader. host_key +
        # ingest base URL live in rovimen-pusher.service's Environment (set
        # per-station at onboard); archive_upload reuses the same resolvers, so
        # mirror them into a shared env file the archive-upload unit loads. Without
        # this the uploader can't resolve its host_key and silently uploads nothing.
        penv="$(systemctl show rovimen-pusher.service -p Environment --value 2>/dev/null)"
        [ -z "$penv" ] && penv="$(XDG_RUNTIME_DIR=/run/user/$svc_uid systemctl --user show rovimen-pusher.service -p Environment --value 2>/dev/null)"
        ig_hk=$(printf '%s\n' "$penv" | tr ' ' '\n' | sed -n 's/^ROVIMEN_HOST_KEY=//p' | head -1)
        ig_url=$(printf '%s\n' "$penv" | tr ' ' '\n' | sed -n 's/^ROVIMEN_INGEST_BASE_URL=//p' | head -1)
        if [ -n "$ig_hk" ]; then
            mkdir -p "$HOME/.config/rovimen"
            ig_file="$HOME/.config/rovimen/ingest.env"
            ig_new="ROVIMEN_HOST_KEY=$ig_hk"
            [ -n "$ig_url" ] && ig_new="$ig_new"$'\n'"ROVIMEN_INGEST_BASE_URL=$ig_url"
            if [ "$(cat "$ig_file" 2>/dev/null)" != "$ig_new" ]; then
                printf '%s\n' "$ig_new" > "$ig_file"; chmod 600 "$ig_file"
                log "archive-upload: wrote ingest.env from pusher (host_key set)"
                user_reload=true
            fi
        fi
        sudo -n loginctl enable-linger "$svc_user" 2>/dev/null || log "WARN: sudo -n loginctl enable-linger $svc_user refused (no sudoers rule?)"
        if XDG_RUNTIME_DIR="/run/user/$svc_uid" systemctl --user enable --now archive-upload.service 2>/dev/null; then
            log "archive-upload.service enabled + active (--user)"
        else
            log "WARN: could not enable archive-upload.service (--user, XDG_RUNTIME_DIR=/run/user/$svc_uid)"
        fi
        if $user_reload; then
            XDG_RUNTIME_DIR="/run/user/$svc_uid" systemctl --user restart archive-upload.service 2>/dev/null \
                && log "archive-upload.service restarted (unit changed)" || true
        fi
    else
        XDG_RUNTIME_DIR="/run/user/$svc_uid" systemctl --user disable --now archive-upload.service 2>/dev/null || true
        log "archive-upload.service disabled (config: services.archive_upload.enabled=false)"
    fi
fi

# sky-dome-render: operator --user unit gated by services.sky_dome_render.enabled.
# Composites this station's own all-sky dome and pushes it over the ingest
# byte-plane; the dashboard prefers that upload and skips its own render. The
# VPS render is ~3.2 CPU-s per pass for a 2-camera station and runs for every
# station on a 10-minute night cadence — roughly 2 CPU-hours a night on a
# 4-core box doing work each station can do for itself.
# Defaults to FALSE, unlike archive_upload: this daemon has never run in
# production, so it is opted into per station rather than switched on
# fleet-wide by an update. Flip services.sky_dome_render.enabled in the
# station's config.json to enable.
if [ -f "$USER_UNIT_DIR/sky-dome-render.service" ]; then
    sd_enabled=$(python3 - "$LOCAL_DIR/config.json" <<'PY' 2>/dev/null
import json,sys
try:
    c=json.load(open(sys.argv[1]))
    print("true" if ((c.get("services") or {}).get("sky_dome_render") or {}).get("enabled", False) else "false")
except Exception:
    print("false")
PY
)
    [ -z "$sd_enabled" ] && sd_enabled=false
    if [ "$sd_enabled" = "true" ]; then
        sudo -n loginctl enable-linger "$svc_user" 2>/dev/null || log "WARN: sudo -n loginctl enable-linger $svc_user refused (no sudoers rule?)"
        if XDG_RUNTIME_DIR="/run/user/$svc_uid" systemctl --user enable --now sky-dome-render.service 2>/dev/null; then
            log "sky-dome-render.service enabled + active (--user)"
        else
            log "WARN: could not enable sky-dome-render.service (--user, XDG_RUNTIME_DIR=/run/user/$svc_uid)"
        fi
        if $user_reload; then
            XDG_RUNTIME_DIR="/run/user/$svc_uid" systemctl --user restart sky-dome-render.service 2>/dev/null \
                && log "sky-dome-render.service restarted (unit changed)" || true
        fi
    else
        XDG_RUNTIME_DIR="/run/user/$svc_uid" systemctl --user disable --now sky-dome-render.service 2>/dev/null || true
    fi
fi

# rovimen-fireball-watch: operator --user unit gated by
# services.fireball_watch.enabled (default true). Read-only on RMS's capture
# files; it posts live fieldsum/FR events for the VPS fireball alert. It needs
# the ingest key like the pusher, and restarts itself when its code changes.
if [ -f "$USER_UNIT_DIR/rovimen-fireball-watch.service" ]; then
    fw_enabled=$(python3 - "$LOCAL_DIR/config.json" <<'PY' 2>/dev/null
import json,sys
try:
    c=json.load(open(sys.argv[1]))
    print("true" if ((c.get("services") or {}).get("fireball_watch") or {}).get("enabled", True) else "false")
except Exception:
    print("true")
PY
)
    [ -z "$fw_enabled" ] && fw_enabled=true
    if [ "$fw_enabled" = "true" ] && [ -f "$HOME/.config/rovimen/ingest.key" ]; then
        sudo -n loginctl enable-linger "$svc_user" 2>/dev/null || log "WARN: sudo -n loginctl enable-linger $svc_user refused (no sudoers rule?)"
        if XDG_RUNTIME_DIR="/run/user/$svc_uid" systemctl --user enable --now rovimen-fireball-watch.service 2>/dev/null; then
            log "rovimen-fireball-watch.service enabled + active (--user)"
        else
            log "WARN: could not enable rovimen-fireball-watch.service (--user, XDG_RUNTIME_DIR=/run/user/$svc_uid)"
        fi
        if $user_reload; then
            XDG_RUNTIME_DIR="/run/user/$svc_uid" systemctl --user restart rovimen-fireball-watch.service 2>/dev/null \
                && log "rovimen-fireball-watch.service restarted (unit changed)" || true
        fi
    else
        XDG_RUNTIME_DIR="/run/user/$svc_uid" systemctl --user disable --now rovimen-fireball-watch.service 2>/dev/null || true
    fi
fi

# rovimen-pusher: the uniform push agent — run it wherever the ingest key exists.
if [ -f "$HOME/.config/rovimen/ingest.key" ] && [ -f /etc/systemd/system/rovimen-pusher.service ]; then
    if ! systemctl is-enabled --quiet rovimen-pusher.service 2>/dev/null; then
        sudo -n systemctl enable --now rovimen-pusher.service 2>/dev/null \
            && log "rovimen-pusher.service enabled + active (ingest key present)" \
            || log "WARN: could not enable rovimen-pusher.service"
    fi
fi

# detection-indexer: the pusher's metadata source — every station needs it under
# the push model (no index -> /api/detections-index ready:false -> the pusher
# has nothing to POST and the station silently shows "no detections").
if [ -f /etc/systemd/system/detection-indexer.service ]; then
    if ! systemctl is-enabled --quiet detection-indexer.service 2>/dev/null; then
        sudo -n systemctl enable --now detection-indexer.service 2>/dev/null \
            && log "detection-indexer.service enabled + active" \
            || log "WARN: could not enable detection-indexer.service"
    fi
fi

# Reconcile cron files — install any that are missing or outdated
if [ -f "$LOCAL_DIR/toggle_rovimen.sh" ]; then
    bash "$LOCAL_DIR/toggle_rovimen.sh" --ensure-crons 2>&1 | while IFS= read -r line; do log "$line"; done || log "WARN: cron reconciliation failed (non-fatal)"
fi
}

# ── Update outcome record ──────────────────────────────────────────────────────
# How the latest update attempt ended goes into .bundle_state.json under
# "update" (bundle_verify.py --record-update), which station_api serves in its
# bundle telemetry, so the dashboard shows a failed or unhealthy update.
_record_update() {
    # $1 status, $2 version, $3 failed services (space-separated), $4 detail.
    # Never fatal: a missing or old verifier just means no record.
    [ -f "$LOCAL_DIR/bundle_verify.py" ] || return 0
    python3 "$LOCAL_DIR/bundle_verify.py" --record-update "$1" --update-version "$2" \
        --failed-services "${3:-}" --detail "${4:-}" > /dev/null 2>&1 \
        || log "WARN: could not record update status $1"
}

# An "unhealthy" verdict for the installed version would otherwise stand until
# the next bundle. On the up-to-date path, look at those services again (no
# wait: hours have passed) and clear the verdict once they all run.
_recheck_unhealthy() {
    local state_file="${ROVIMEN_BUNDLE_STATE:-$LOCAL_DIR/.bundle_state.json}"
    local info status ver svcs svc bad=()
    info=$(python3 - "$state_file" <<'PY' 2>/dev/null
import json, sys
try:
    u = json.load(open(sys.argv[1])).get("update") or {}
    print(u.get("status") or "")
    print(u.get("version") or "")
    print(" ".join(u.get("failed_services") or []))
except Exception:
    print(""); print(""); print("")
PY
) || return 0
    { read -r status; read -r ver; read -r svcs; } <<< "$info" || true
    [ "$status" = unhealthy ] && [ "$ver" = "$LOCAL_VERSION" ] || return 0
    for svc in $svcs; do
        systemctl is-active --quiet "$svc" 2>/dev/null && continue
        XDG_RUNTIME_DIR="/run/user/$(id -u)" systemctl --user is-active --quiet "$svc" 2>/dev/null && continue
        bad+=("$svc")
    done
    if [ ${#bad[@]} -eq 0 ]; then
        log "Services unhealthy after the update to $ver are active now: $svcs"
        _record_update ok "$ver" "" "recovered after the update"
    else
        log "WARN: still not active since the update to $ver: ${bad[*]}"
        _record_update unhealthy "$ver" "${bad[*]}"
    fi
}

if [[ "${1:-}" == "--install-timer"   ]]; then install_timer;    exit 0; fi
if [[ "${1:-}" == "--install-cron"    ]]; then install_cron;     exit 0; fi
if [[ "${1:-}" == "--install-sudoers" ]]; then _ensure_sudoers;  exit 0; fi

check_version() {
    local local_ver remote_ver
    local_ver=$(cat "$VERSION_FILE" 2>/dev/null || echo "none")
    remote_ver=$(ssh -o ConnectTimeout=8 -o BatchMode=yes "$VPS_USER@$VPS_HOST" \
        "cat $BUNDLE_PATH/.version 2>/dev/null || echo 'unknown'" 2>/dev/null || echo 'unreachable')
    echo "Channel:  $UPDATE_CHANNEL"
    echo "Local:    $local_ver"
    echo "Remote:   $remote_ver"
    if [ "$remote_ver" = "unreachable" ] || [ "$remote_ver" = "unknown" ]; then
        echo "Status:   VPS not reachable"
    elif [ "$local_ver" = "$remote_ver" ]; then
        echo "Status:   up to date"
    else
        echo "Status:   update available"
    fi
}
if [[ "${1:-}" == "--check" ]]; then check_version; exit 0; fi

# ── Preflight: would this station accept the bundle the VPS is serving? ────────
#
# Answers the one question an operator has before flipping a station to
# enforce, before the first deploy after a signing-key rotation, and when a
# station rejoins the fleet after weeks offline. Read-only in every sense:
# stages into its OWN directory (never $STAGING_DIR, so a timer run racing this
# is unaffected), never touches the live install, never restarts a service, and
# always discards what it staged.
#
# Two deliberate choices make the verdict mean what the operator thinks:
#
#   * The mode file is bypassed — the check always applies ENFORCE semantics.
#     Asking "would enforce accept this?" on a station currently in warn is the
#     entire point; reading the real mode would answer PASS for any bundle.
#   * Anti-rollback is skipped (state and installed-version paths point at
#     nothing, plus --no-state). Rollback is a sequencing rule, not a statement
#     about this bundle's integrity, so the preflight reports on signature,
#     channel binding and the exact-set check.
#
# --no-state keeps the preflight from leaving a verify record for a bundle it
# never installs (under the old verified-build floor that is how gmnro02
# stranded itself on build 218 in 2026-08).
verify_only() {
    local rc=0 modetmp
    echo "Channel:  $UPDATE_CHANNEL"
    echo "Bundle:   $VPS_USER@$VPS_HOST:$BUNDLE_PATH"
    echo "Mode now: $(cat "$MODE_FILE" 2>/dev/null || echo 'warn (no mode file)')"
    # Mirror bundle_verify.py's env override so the reported pin is the one the
    # verifier actually used (identical to the literal path on a real station).
    echo "Pin:      $(sha256sum "${ROVIMEN_BUNDLE_PUBKEY:-/etc/rovimen/bundle_signing_pub.pem}" \
        2>/dev/null | cut -c1-16 || echo 'ABSENT')"

    if [ ! -f "$LOCAL_DIR/bundle_verify.py" ]; then
        echo "Result:   NO VERIFIER — $LOCAL_DIR/bundle_verify.py is missing."
        echo "          This station cannot verify anything; enforce would refuse"
        echo "          every update. Land a bundle in warn mode first."
        return 2
    fi

    rm -rf "$VERIFY_DIR"
    if ! rsync -az --delete --timeout=60 \
        -e "ssh -o ConnectTimeout=10 -o BatchMode=yes" \
        "$VPS_USER@$VPS_HOST:$BUNDLE_PATH/" "$VERIFY_DIR/"; then
        echo "Result:   VPS UNREACHABLE — could not stage the bundle to check it."
        rm -rf "$VERIFY_DIR"
        return 2
    fi

    modetmp=$(mktemp)
    echo enforce > "$modetmp"
    python3 "$LOCAL_DIR/bundle_verify.py" \
        --staging "$VERIFY_DIR" \
        --channel "$UPDATE_CHANNEL" \
        --mode-file "$modetmp" \
        --state "${modetmp}.state-that-does-not-exist.json" \
        --installed-version "${modetmp}.version-that-does-not-exist" \
        --no-state || rc=$?
    rm -f "$modetmp"
    rm -rf "$VERIFY_DIR"

    if [ "$rc" -eq 0 ]; then
        echo "Result:   PASS — this station would accept the current" \
             "$UPDATE_CHANNEL bundle under enforce."
    else
        echo "Result:   FAIL — this station would REJECT the current" \
             "$UPDATE_CHANNEL bundle under enforce."
        echo "          Do NOT flip this station to enforce until the failures"
        echo "          above are resolved. Nothing was changed by this check."
    fi
    return "$rc"
}
if [[ "${1:-}" == "--verify-only" ]]; then verify_only; exit $?; fi

# ── Main update logic ──────────────────────────────────────────────────────────

# Spread the fleet's pull across the window. Every station runs this from the
# same three cron minutes (14:00/14:15/16:45 UTC), so 20+ boxes open an SSH
# connection and start an rsync against the VPS in the same second. On
# 2026-08-20 at 16:45 gmnro10's run died with "Connection closed by <vps> port
# 22" while its neighbours succeeded — the signature of sshd MaxStartups
# shedding connections, which costs that station a whole day of updates.
#
# Skipped for an interactive or API-triggered run (--now, or a tty): a human
# waiting on the dashboard's "update now" button should not sit through a random
# delay. Uses $RANDOM seeded per-process, so stations land on different offsets
# without needing per-station crontabs.
UPDATE_JITTER_MAX="${ROVIMEN_UPDATE_JITTER_MAX:-600}"
# A non-numeric override would abort the whole run under `set -e`, which would
# silently stop that station updating. Fall back to no jitter instead.
if ! [[ "$UPDATE_JITTER_MAX" =~ ^[0-9]+$ ]]; then
    UPDATE_JITTER_MAX=0
fi
if [[ "${1:-}" == "--now" ]]; then
    shift || true
elif [ -t 0 ] || [ "$UPDATE_JITTER_MAX" -le 0 ]; then
    :
else
    _jitter=$(( RANDOM % UPDATE_JITTER_MAX ))
    log "Jitter: sleeping ${_jitter}s before contacting the VPS"
    sleep "$_jitter"
fi

# Safety guard — skip entirely if morning processing is running
if pgrep -f dawn_process.py > /dev/null 2>&1; then
    log "SKIP: morning processing is running — deferring update"
    exit 0
fi

# Note whether ffmpeg capture is active — scripts/config will still be updated,
# but color-capture will not be restarted mid-segment to avoid recording gaps.
_ffmpeg_active=false
if pgrep -f 'ffmpeg.*_color\.mkv' > /dev/null 2>&1; then
    log "NOTE: ffmpeg capture is active — scripts will be updated but color-capture restart will be skipped"
    _ffmpeg_active=true
fi

log "Starting update check (channel: $UPDATE_CHANNEL)..."

# VPS reachable?
if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "$VPS_USER@$VPS_HOST" true 2>/dev/null; then
    log "WARN: VPS unreachable at $VPS_HOST — skipping update"
    exit 0
fi

# Emergency freeze — if FREEZE file exists on VPS, abort update
if ssh -o ConnectTimeout=8 -o BatchMode=yes "$VPS_USER@$VPS_HOST" \
    "test -f $BUNDLE_PATH/FREEZE" 2>/dev/null; then
    log "WARN: Update frozen by VPS — skipping (remove $BUNDLE_PATH/FREEZE to unfreeze)"
    exit 0
fi

# Station-local freeze — survives a VPS compromise (the VPS-side sentinel can
# be deleted by an attacker; this one is on the station, under /etc).
if [ -f /etc/rovimen/FREEZE ]; then
    log "WARN: Update frozen locally — skipping (remove /etc/rovimen/FREEZE to unfreeze)"
    exit 0
fi

# Compare versions
REMOTE_VERSION=$(ssh -o ConnectTimeout=8 -o BatchMode=yes "$VPS_USER@$VPS_HOST" \
    "cat $BUNDLE_PATH/.version 2>/dev/null || echo 'unknown'")

if [ "$REMOTE_VERSION" = "unknown" ]; then
    log "WARN: No version file on VPS — bundle not deployed yet"
    exit 0
fi

LOCAL_VERSION=$(cat "$VERSION_FILE" 2>/dev/null || echo "none")
log "Local: $LOCAL_VERSION | Remote: $REMOTE_VERSION"

if [ "$LOCAL_VERSION" = "$REMOTE_VERSION" ]; then
    log "Already up to date."
    # Self-heal even without a version change: a station can gain its push
    # identity (pusher onboarded) or lose a unit AFTER reaching the latest
    # bundle, and waiting for the next release would leave it drifted for
    # days (found live: gmnro14 pulled the bundle before its pusher existed,
    # so ingest.env was never generated). The live install already matches
    # the bundle, so reconcile from it — no download, no restarts unless a
    # unit or env file actually changed.
    reconcile_units
    reconcile_service_state
    # Sudoers first: a station that took the VAAPI bundle while the previous
    # updater.sh was still running got the old rule set, and this path is the
    # only one it will run until the next bundle.
    _ensure_sudoers
    _ensure_persistent_journal
    _ensure_vaapi_ground_rule
    _recheck_unhealthy
    exit 0
fi

log "New version available: $REMOTE_VERSION — updating..."

# Download the FULL bundle into a staging directory — nothing touches the
# live install until the signature verifies. --delete keeps staging an exact
# mirror of the VPS bundle so the exact-set check sees what will be applied.
if ! rsync -az --delete --timeout=60 \
    -e "ssh -o ConnectTimeout=10 -o BatchMode=yes" \
    "$VPS_USER@$VPS_HOST:$BUNDLE_PATH/" "$STAGING_DIR/"; then
    log "ERROR: rsync to staging failed — live install untouched"
    rm -rf "$STAGING_DIR"
    exit 1
fi

# Verify the staged bundle with the LIVE verifier (never the staged copy —
# that would let a tampered bundle approve itself). CI signs MANIFEST with
# the GitHub-held key; the pin at /etc/rovimen/bundle_signing_pub.pem was
# delivered over SSH. warn mode logs failures and proceeds; enforce rejects.
if [ -f "$LOCAL_DIR/bundle_verify.py" ]; then
    verify_rc=0
    verify_out=$(python3 "$LOCAL_DIR/bundle_verify.py" \
        --staging "$STAGING_DIR" --channel "$UPDATE_CHANNEL" 2>&1) || verify_rc=$?
    while IFS= read -r line; do log "$line"; done <<< "$verify_out"
    if [ "$verify_rc" -ne 0 ]; then
        log "ERROR: $REJECT_MARKER bundle verification FAILED — discarding staged bundle, live install untouched"
        rm -rf "$STAGING_DIR"
        exit 1
    fi
else
    # First rollout: the live install predates the verifier. Never run the
    # STAGED verifier (attacker-controlled); fall back to the mode file.
    verify_mode=$(cat "$MODE_FILE" 2>/dev/null || echo "warn")
    if [ "$verify_mode" = "enforce" ]; then
        log "ERROR: $REJECT_MARKER no live bundle_verify.py but mode=enforce — refusing unverified update"
        rm -rf "$STAGING_DIR"
        exit 1
    fi
    log "WARN: no live bundle_verify.py — applying unverified (mode=$verify_mode)"
fi

# The version this run installs, read from what was verified (REMOTE_VERSION
# came from a separate ssh call and could name a newer deploy).
STAGED_VERSION=$(tr -d '[:space:]' < "$STAGING_DIR/.version" 2>/dev/null || true)
STAGED_VERSION="${STAGED_VERSION:-$REMOTE_VERSION}"

# Backup: one generation of the code and the small state a human needs for a
# manual rollback (config.json, the state JSONs, mediamtx.yml, bin/). Runtime
# data stays out: the FF thumbnails, the detection index and the logs made the
# copy take 387-589 s on gmnro05 (Bistrița), and the janitor deleting
# thumbnails mid-copy made `cp -a` fail, which aborted the update after the
# verify had already recorded it (2026-11-06, 2026-11-17). A failed backup is
# logged and the update goes on; files that vanish during the copy (rsync exit
# 24) are normal. Restore by overlaying, never by replacing the directory:
#   rsync -a ~/rovimen_scripts.bak/ ~/rovimen_scripts/
BACKUP_EXCLUDES=(
    --exclude='ffthumbs/' --exclude='logs/' --exclude='__pycache__/'
    --exclude='calibration_candidates/' --exclude='upload_state/'
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' --exclude='*.db-journal'
    --exclude='*.log' --exclude='*.tmp'
)
backup_ok=false
if [ -d "$LOCAL_DIR" ]; then
    rm -rf "${BACKUP_DIR}.new"
    backup_rc=0
    rsync -a "${BACKUP_EXCLUDES[@]}" "$LOCAL_DIR/" "${BACKUP_DIR}.new/" 2>&1 \
        | while IFS= read -r line; do log "backup: $line"; done || backup_rc=$?
    # pipefail hands rsync's exit code through the log loop.
    if [ "$backup_rc" -eq 0 ] || [ "$backup_rc" -eq 24 ]; then
        rm -rf "$BACKUP_DIR"
        mv "${BACKUP_DIR}.new" "$BACKUP_DIR"
        backup_ok=true
        log "Backed up to $BACKUP_DIR"
    else
        rm -rf "${BACKUP_DIR}.new"
        log "WARN: backup failed (rsync exit $backup_rc) — updating without a fresh backup; $BACKUP_DIR still holds the previous one"
    fi
fi

# Apply the verified staging over the live install. No --delete: live keeps
# config.json, logs and local state. config.json excluded as belt-and-braces
# (the bundle must never carry one). .version is held back and written last,
# after migration, unit installation and the restarts, so a run that dies
# anywhere in between leaves the old version and the next run repeats the whole
# update instead of reporting "Already up to date".
if ! rsync -a \
    --exclude='config.json' \
    --exclude='*.log' \
    --exclude='hardware.json' \
    --exclude='.version' \
    "$STAGING_DIR/" "$LOCAL_DIR/"; then
    if $backup_ok; then
        log "ERROR: apply failed — restoring the code from $BACKUP_DIR"
        rsync -a "$BACKUP_DIR/" "$LOCAL_DIR/" || log "ERROR: restore from $BACKUP_DIR failed"
    else
        log "ERROR: apply failed and there is no fresh backup — .version unchanged, the next run retries"
    fi
    _record_update failed "$STAGED_VERSION" "" "apply rsync failed"
    rm -rf "$STAGING_DIR"
    exit 1
fi
rm -rf "$STAGING_DIR"

log "Files applied for $STAGED_VERSION (version is recorded once the update completes)"

reconcile_units

# Migrate config — add any new default fields, preserve existing values
if command -v python3 > /dev/null 2>&1; then
    cp "$LOCAL_DIR/config.json" "$LOCAL_DIR/config.json.bak" 2>/dev/null || true
    python3 "$LOCAL_DIR/config_migrate.py" 2>&1 | while IFS= read -r line; do log "$line"; done || \
        log "WARN: config migration failed (non-fatal)"
fi

reconcile_service_state

# Reconcile sudoers — keep NOPASSWD rules current as new services are added
_ensure_sudoers

_ensure_persistent_journal

# VAAPI ground rule — before the restarts below, so restarted services start
# with the render group.
_ensure_vaapi_ground_rule

_user_ctl() { XDG_RUNTIME_DIR="/run/user/$svc_uid" systemctl --user "$@"; }

# systemd's restart counter for a unit; empty where systemd predates it.
_nrestarts() {
    if [ "${2:-}" = user ]; then
        _user_ctl show -p NRestarts --value "$1" 2>/dev/null || true
    else
        systemctl show -p NRestarts --value "$1" 2>/dev/null || true
    fi
}

# Restart running services: every unit that is active, whether or not it is
# enabled. A service turned off (toggle_rovimen off, dashboard toggle) is
# stopped as well as disabled, so it is inactive and left alone. A unit can be
# disabled and still run every boot, pulled in by a dependency: on gmnro06
# (Cluj) the pusher and indexer start rovimen-station-api. Skipping it left the
# API on the previous bundle while the files on disk changed under it; its
# lazy imports then mixed versions and every clip render failed (2026-11-23,
# "module 'color_calibration' has no attribute 'station_location'").
# Appends to the caller's `restarted` and `restart_base`.
_restart_system_services() {
    local svc
    for svc in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            if [[ "$_ffmpeg_active" == "true" && "$svc" == "color-capture" ]]; then
                log "SKIP restart: color-capture skipped while ffmpeg is capturing"
                continue
            fi
            if sudo -n systemctl restart "$svc" 2>/dev/null; then
                restarted+=("$svc")
                restart_base["$svc"]=$(_nrestarts "$svc")
                log "Restarted $svc"
            else
                log "WARN: Failed to restart $svc (sudo -n refused or unit error)"
            fi
        fi
    done
}

restarted=()
declare -A restart_base=()
_restart_system_services

# The --user units (archive-upload, fireball watch, sky dome) run bundle code
# too. They were restarted only when their unit file changed, so a fix to
# archive_upload.py waited for the next reboot: the 13:45 reboot guard runs
# before the 14:00 update, so in practice a day (gmnro22 (Ferigile),
# 2026-10-21: the re-upload fix of #1390 sat idle until a manual restart).
for unit in "${USER_UNITS[@]}"; do
    if _user_ctl is-active --quiet "$unit" 2>/dev/null; then
        if _user_ctl restart "$unit" 2>/dev/null; then
            restarted+=("$unit (--user)")
            restart_base["$unit (--user)"]=$(_nrestarts "$unit" user)
            log "Restarted $unit (--user)"
        else
            log "WARN: Failed to restart $unit (--user)"
        fi
    fi
done

# Everything the version stands for is in place: record it. Atomic, so a
# reader never sees an empty .version.
printf '%s\n' "$STAGED_VERSION" > "$VERSION_FILE.tmp" && mv "$VERSION_FILE.tmp" "$VERSION_FILE"
_record_update applied "$STAGED_VERSION"
log "Scripts updated to $STAGED_VERSION"

if [ ${#restarted[@]} -eq 0 ]; then
    log "No running services to restart."
    _record_update ok "$STAGED_VERSION"
    exit 0
fi

# Health check. Long enough to catch a crash loop: the units restart after
# 10-60 s (RestartSec), so a unit that dies at start is either not active or
# has a higher NRestarts after this wait. Nothing capture-critical waits on
# the updater, and it runs in daylight. The verdict goes into the bundle
# state, which station_api reports to the dashboard. No automatic rollback.
log "Done. Restarted: ${restarted[*]}"
HEALTH_WAIT="${ROVIMEN_UPDATE_HEALTH_WAIT:-90}"
[[ "$HEALTH_WAIT" =~ ^[0-9]+$ ]] || HEALTH_WAIT=90
log "Health check in ${HEALTH_WAIT}s"
sleep "$HEALTH_WAIT"
failed=()
for svc in "${restarted[@]}"; do
    name="${svc% (--user)}"
    scope=system
    [[ "$svc" == *" (--user)" ]] && scope=user
    if [ "$scope" = user ]; then
        _user_ctl is-active --quiet "$name" 2>/dev/null && active=true || active=false
    else
        systemctl is-active --quiet "$name" 2>/dev/null && active=true || active=false
    fi
    before="${restart_base[$svc]:-}"
    after=$(_nrestarts "$name" "$scope")
    if ! $active; then
        log "WARN: $svc is not active ${HEALTH_WAIT}s after restart"
        failed+=("$name")
    elif [[ "$before" =~ ^[0-9]+$ && "$after" =~ ^[0-9]+$ ]] && [ "$after" -gt "$before" ]; then
        log "WARN: $svc restarted by systemd $((after - before)) time(s) within ${HEALTH_WAIT}s"
        failed+=("$name")
    fi
done
if [ ${#failed[@]} -gt 0 ]; then
    log "ERROR: ${#failed[@]} service(s) failed health check after update to $STAGED_VERSION: ${failed[*]}"
    _record_update unhealthy "$STAGED_VERSION" "${failed[*]}"
else
    log "Health check passed — all restarted services are active"
    _record_update ok "$STAGED_VERSION"
fi
