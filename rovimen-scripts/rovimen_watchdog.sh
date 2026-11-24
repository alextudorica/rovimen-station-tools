#!/usr/bin/env bash
# rovimen_watchdog.sh — Connectivity watchdog for ROVIMEN stations.
#
# Runs every few minutes via systemd timer. Logs system vitals, judges the
# uplink and Tailscale, and acts only where an action on this station can help.
#
# Tailscale health is `tailscale status --json` (bounded by a timeout):
# healthy = BackendState Running AND Self.Online true. The old check pinged
# 100.100.100.100, which the local tailscaled answers with or without a
# network, so on 2026-11-20 gmnro11 (Tărâța) sat 14 h with both NICs down and
# every run logged "tailscale: OK" (the ladder never ran).
#
# What the watchdog does, by case:
#   rescue Wi-Fi up and     nothing: a networking restart would drop it. A
#   reaching the internet   rescue that is up but reaches nothing (no phone
#                           in range) does not stop the ladder below
#   Tailscale logged out    nothing (NeedsLogin, NeedsMachineAuth, Stopped):
#                           only a person can log it in; one event line
#   internet up, Tailscale  restart tailscaled after TS_FAIL_THRESHOLD runs in
#   unhealthy               a row, at most once per TS_RESTART_COOLDOWN. Never
#                           networking, never a reboot: a Tailscale control
#                           plane outage must not restart the fleet
#   no internet, uplink isp nothing: the router WAN or the ISP is down
#   no internet, uplink     after NET_FAIL_THRESHOLD runs in a row, restart
#   link or router          networking, at most once per NET_RESTART_COOLDOWN
#                           (doubling per futile restart, capped at
#                           NET_RESTART_BACKOFF_MAX); class router only while
#                           the sun is up. From a later run, reboot, once the
#                           outage is REBOOT_MIN_OUTAGE old: link after 1 h,
#                           then 2 h, 4 h, 6 h between reboots in one outage;
#                           router at most once per 6 h; never while the sun
#                           is down at the station, never while dawn_process
#                           runs, never with watchdog.allow_reboot=false
# Tailscale online counts as internet: a network that drops ICMP to 8.8.8.8
# and 1.1.1.1 is not restarted while Tailscale reaches its control plane.
#
# Each run also says where a lost uplink breaks (uplink: ok|isp|router|link)
# and records every change in ~/rovimen_scripts/logs/uplink_events.log, so an
# outage can be read back after it ends.
#
# Rescue Wi-Fi (#1432, only where setup_rescue_wifi.sh installed it): when the
# cable path has failed RESCUE_WIFI_AFTER runs in a row, bring up the
# ROVIMEN-RESCUE hotspot profile; take it down after RESCUE_WIFI_CLEAR good
# runs on the cable. watchdog.rescue_wifi=false in config disables both.
# While it carries traffic, ~/rovimen_scripts/rescue_wifi_active exists and
# archive_upload.py skips its upload passes, so a phone hotspot is not drained.
#
# State kept between runs (logs/.watchdog_state) is written before each action,
# so a run killed mid-action still counts it; a missing or unreadable value
# reads as "no history" (counters 0), which needs fresh failed runs to act.
#
# Also heals pipeline services: any *enabled* unit found inactive/failed gets
# reset-failed + restart, budgeted to 3 attempts per hour per unit. Disabled
# units are never touched.
#
# Logs to ~/rovimen_scripts/logs/watchdog.log (rotated, 7 days kept).
#
# Deployed automatically by updater.sh (SYSTEM_ENSURE_UNITS + timer install).
# Manual install:
#   sudo cp rovimen-watchdog.timer rovimen-watchdog.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now rovimen-watchdog.timer

set -uo pipefail

LOGDIR="$HOME/rovimen_scripts/logs"
LOGFILE="$LOGDIR/watchdog.log"
STATEFILE="$LOGDIR/.watchdog_state"
CONFIGFILE="$HOME/rovimen_scripts/config.json"
REBOOT_COOLDOWN=3600         # first wait between reboots for uplink class link (doubles per reboot in one outage)
ROUTER_REBOOT_COOLDOWN=21600 # cap of that, and the wait for class router (link up, gateway silent)
REBOOT_MIN_OUTAGE=1800       # no reboot before an outage is this old
REBOOT_RMS_MAX_OUTAGE=21600  # RMS post-processing defers the reboot only until the outage is this old
REBOOT_RMS_WAIT_UNTIL=13:15  # ... or until this UTC time (the fleet's dawn deadline)
RECOVERY_WAIT="${ROVIMEN_WATCHDOG_RECOVERY_WAIT:-20}"  # seconds to wait after each recovery step
NETWORK_SETTLE_WAIT="${ROVIMEN_WATCHDOG_SETTLE_WAIT:-5}"  # seconds between the network restart and the tailscaled restart
TS_TIMEOUT=15                # seconds for any tailscale CLI call
SUDO_TIMEOUT=120             # seconds for a privileged restart
TS_FAIL_THRESHOLD=3          # unhealthy runs (internet up) before restarting tailscaled
TS_RESTART_COOLDOWN=1800
NET_FAIL_THRESHOLD=2         # no-internet runs in a row before the first action
NET_RESTART_COOLDOWN=1800    # a networking restart can blip the camera LAN
NET_RESTART_BACKOFF_MAX=14400
DAWN_LOCK="${ROVIMEN_DAWN_LOCK:-/tmp/rovimen_dawn.lock}"
DAWN_CRON="${ROVIMEN_DAWN_CRON:-/etc/cron.d/rovimen-dawn}"  # its dawn_process.py line names dawn's interpreter
RMS_UNIT_DIR="${ROVIMEN_RMS_UNIT_DIR:-/etc/systemd/system}"
RESCUE_FLAG="$HOME/rovimen_scripts/rescue_wifi_active"
REBOOT_BIN=$(command -v reboot 2>/dev/null || echo /sbin/reboot)  # same path as the updater's sudoers rule
INET_CHECK_HOSTS=("8.8.8.8" "1.1.1.1")
# DNS is checked separately from raw IP reachability: on 2026-09-23 gmnmd01
# (Cahul) pinged 8.8.8.8 and reached Tailscale for 7.5 h while every name
# lookup failed, so it could not upload a byte and nothing here reacted.
read -r -a DNS_CHECK_NAMES <<< "${ROVIMEN_WATCHDOG_DNS_NAMES:-dashboard.rovimen.org google.com}"
DNS_FAIL_THRESHOLD="${ROVIMEN_WATCHDOG_DNS_FAIL_THRESHOLD:-5}"  # consecutive failed runs (~15 min on the 3-min timer)
DNS_BOUNCE_COOLDOWN=1800  # seconds between uplink re-activations
# Uplink diagnosis: on 2026-10-20 gmnro14 (Chiribiș) lost its uplink at 17:21
# UTC for 13+ h, and nothing on the station said whether the router, the ISP
# or the PC had failed. The event log keeps one line per change of class.
UPLINK_EVENTS="$LOGDIR/uplink_events.log"
UPLINK_EVENTS_KEEP=500  # lines
SYSFS_NET="${ROVIMEN_WATCHDOG_SYSFS_NET:-/sys/class/net}"
# Rescue Wi-Fi (#1432): root-owned helper + its config, both written by
# setup_rescue_wifi.sh. About 6 min of failed cable before it comes up, 9 min
# of working cable before it goes down, so a flapping link does not toggle it.
RESCUE_HELPER="${ROVIMEN_RESCUE_HELPER:-/usr/local/sbin/rovimen-rescue-wifi}"
RESCUE_CONF="${ROVIMEN_RESCUE_CONF:-/etc/rovimen/rescue-wifi/conf}"
RESCUE_WIFI_AFTER="${ROVIMEN_RESCUE_WIFI_AFTER:-2}"
RESCUE_WIFI_CLEAR="${ROVIMEN_RESCUE_WIFI_CLEAR:-3}"

# Service health: enabled-but-dead pipeline units get restarted with a budget.
SYSTEM_HEALTH_UNITS=("color-capture" "rovimen-station-api" "detection-indexer" "rovimen-pusher")
USER_HEALTH_UNITS=("archive-upload")
SERVICE_RESTART_BUDGET=3      # max restart attempts per unit...
SERVICE_RESTART_WINDOW=3600   # ...per this many seconds
SERVICE_RECHECK_WAIT="${ROVIMEN_WATCHDOG_RECHECK_WAIT:-10}"  # seconds before post-restart recheck

mkdir -p "$LOGDIR"

# Tailscale installed as a snap runs as snap.tailscale.tailscaled (gmnro08
# Brașov): asking systemd about "tailscaled" there said inactive on every run,
# and the recovery rungs restarted a unit that does not exist.
tailscaled_unit() {
    local u
    for u in tailscaled snap.tailscale.tailscaled; do
        if systemctl list-unit-files "$u.service" --no-legend 2>/dev/null | grep -q "^$u\.service"; then
            echo "$u"
            return 0
        fi
    done
    echo tailscaled
}
TAILSCALED_UNIT=$(tailscaled_unit)

# --- Logging ---

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [watchdog] $*" >> "$LOGFILE"; }

log_section() {
    echo "" >> "$LOGFILE"
    log "=== $1 ==="
}

# --- Log rotation (keep 7 days) ---

rotate_logs() {
    find "$LOGDIR" -name 'watchdog.log.*' -mtime +7 -delete 2>/dev/null || true
    if [ -f "$LOGFILE" ] && [ "$(stat -c%s "$LOGFILE" 2>/dev/null || echo 0)" -gt 5242880 ]; then
        mv "$LOGFILE" "$LOGFILE.$(date -u '+%Y%m%d_%H%M%S')"
    fi
}

# --- State file helpers ---

# A missing file, a missing key or an unreadable value all read as the
# default, so a corrupt state file means "no history", never "act now".
get_state() {
    local key="$1" default="${2:-}" val
    val=$(grep -a "^${key}=" "$STATEFILE" 2>/dev/null | tail -n 1 | cut -d= -f2-)
    if [ -n "$val" ]; then echo "$val"; else echo "$default"; fi
}

# get_num <key> <default>: a non-negative integer, else the default.
get_num() {
    local val
    val=$(get_state "$1" "$2")
    if [[ "$val" =~ ^[0-9]{1,12}$ ]]; then echo $(( 10#$val )); else echo "$2"; fi
}

# seconds_since <key>: seconds since the epoch time stored under key; a large
# number when there is none, 0 when it lies in the future (clock stepped back).
seconds_since() {
    local last now
    last=$(get_num "$1" 0)
    now=$(date +%s)
    if [ "$last" -eq 0 ]; then echo 999999999
    elif [ "$last" -gt "$now" ]; then echo 0
    else echo $(( now - last )); fi
}

# Written through a temp file and mv, so a run killed mid-write leaves the old
# file or the new one, never half of it.
set_state() {
    local key="$1" val="$2"
    { grep -a -v "^${key}=" "$STATEFILE" 2>/dev/null; echo "${key}=${val}"; } > "${STATEFILE}.tmp"
    mv "${STATEFILE}.tmp" "$STATEFILE"
}

# _sudo <cmd...>: sudo -n under a timeout, output into the log. A command with
# no sudoers rule fails at once ("a password is required") and is logged.
_sudo() {
    local out rc
    out=$(timeout -k 5 "$SUDO_TIMEOUT" sudo -n "$@" 2>&1)
    rc=$?
    [ -n "$out" ] && while IFS= read -r line; do log "  $line"; done <<< "$out"
    [ "$rc" -eq 0 ] || log "  sudo -n $* failed (exit $rc)"
    return "$rc"
}

# --- Vitals collection ---

log_vitals() {
    log_section "SYSTEM VITALS"

    log "hostname: $(hostname)"
    log "uptime: $(uptime)"
    log "load: $(cat /proc/loadavg)"

    local mem
    mem=$(free -m | awk '/^Mem:/ {printf "total=%sMB used=%sMB free=%sMB avail=%sMB", $2, $3, $4, $7}')
    log "memory: $mem"

    local swap
    swap=$(free -m | awk '/^Swap:/ {printf "total=%sMB used=%sMB free=%sMB", $2, $3, $4}')
    log "swap: $swap"

    log "disk:"
    df -h --output=target,size,used,avail,pcent / /home 2>/dev/null | while IFS= read -r line; do
        log "  $line"
    done

    if command -v sensors &>/dev/null; then
        local temp
        temp=$(sensors 2>/dev/null | grep -i 'core\|temp' | head -5)
        [ -n "$temp" ] && log "temps: $temp"
    elif [ -f /sys/class/thermal/thermal_zone0/temp ]; then
        local cpu_temp
        cpu_temp=$(awk '{printf "%.1f", $1/1000}' /sys/class/thermal/thermal_zone0/temp)
        log "cpu_temp: ${cpu_temp}C"
    fi

    local top_procs
    top_procs=$(ps aux --sort=-%mem | head -6)
    log "top processes by memory:"
    echo "$top_procs" | while IFS= read -r line; do
        log "  $line"
    done
}

log_network() {
    log_section "NETWORK STATE"

    log "interfaces:"
    ip -br addr 2>/dev/null | while IFS= read -r line; do
        log "  $line"
    done

    log "default route: $(ip route show default 2>/dev/null | head -1)"

    log "dns resolv.conf:"
    grep '^nameserver' /etc/resolv.conf 2>/dev/null | while IFS= read -r line; do
        log "  $line"
    done
}

# tailscale_probe: one bounded `tailscale status --json`. Sets TS_PROBE to
# healthy | logged_out | offline (Running, Self.Online false) | unhealthy
# (another BackendState) | timeout | no_status (tailscaled not answering) |
# missing (no tailscale binary), and TS_BACKEND, TS_ONLINE, TS_IP.
TS_PROBE="" TS_BACKEND="" TS_ONLINE="" TS_IP=""
tailscale_probe() {
    TS_BACKEND="" TS_ONLINE="" TS_IP=""
    if ! command -v tailscale &>/dev/null; then
        TS_PROBE=missing
        return
    fi
    local out rc parsed
    out=$(timeout -k 5 "$TS_TIMEOUT" tailscale status --json 2>/dev/null)
    rc=$?
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        TS_PROBE=timeout
        return
    fi
    parsed=$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
s = d.get("Self") or {}
ips = s.get("TailscaleIPs") or ["?"]
print(d.get("BackendState") or "unknown", bool(s.get("Online")), ips[0])
' 2>/dev/null) || parsed=""
    read -r TS_BACKEND TS_ONLINE TS_IP <<< "$parsed"
    if [ -z "$TS_BACKEND" ]; then
        TS_PROBE=no_status
        return
    fi
    case "$TS_BACKEND" in
        NeedsLogin|NeedsMachineAuth|Stopped) TS_PROBE=logged_out ;;
        Running) if [ "$TS_ONLINE" = True ]; then TS_PROBE=healthy; else TS_PROBE=offline; fi ;;
        *) TS_PROBE=unhealthy ;;
    esac
}

# The one-line verdict, as logged after "tailscale: ".
tailscale_verdict() {
    case "$TS_PROBE" in
        healthy) echo "OK" ;;
        logged_out) echo "logged out ($TS_BACKEND)" ;;
        offline) echo "UNREACHABLE (backend Running, Self.Online=false)" ;;
        unhealthy) echo "UNREACHABLE (backend $TS_BACKEND)" ;;
        timeout) echo "UNREACHABLE (tailscale status timed out after ${TS_TIMEOUT}s)" ;;
        no_status) echo "UNREACHABLE (no status: tailscaled not answering)" ;;
        missing) echo "NOT INSTALLED (no tailscale binary)" ;;
    esac
}

log_tailscale() {
    log_section "TAILSCALE STATE"

    if [ "$TS_PROBE" = missing ]; then
        log "tailscale binary NOT FOUND"
        return 1
    fi
    case "$TS_PROBE" in
        timeout) log "tailscale status timed out after ${TS_TIMEOUT}s" ;;
        no_status) log "tailscale status returned no status" ;;
        *)
            log "backend_state: $TS_BACKEND"
            log "self: online=$TS_ONLINE ip=$TS_IP"
            ;;
    esac

    local ts_svc
    ts_svc=$(systemctl is-active "$TAILSCALED_UNIT" 2>/dev/null || echo "unknown")
    log "tailscaled service: $ts_svc ($TAILSCALED_UNIT)"

    if [ "$ts_svc" = "failed" ] || [ "$ts_svc" = "inactive" ]; then
        log "tailscaled journal (last 20 lines):"
        journalctl -u "$TAILSCALED_UNIT" --no-pager -n 20 2>/dev/null | while IFS= read -r line; do
            log "  $line"
        done
    fi
}

log_journal_errors() {
    log_section "RECENT JOURNAL ERRORS (last 30 min)"
    journalctl --priority=err --since "30 min ago" --no-pager -n 30 2>/dev/null | while IFS= read -r line; do
        log "  $line"
    done
}

# --- Connectivity checks ---

check_internet() {
    for host in "${INET_CHECK_HOSTS[@]}"; do
        if ping -c 1 -W 3 "$host" &>/dev/null; then
            return 0
        fi
    done
    return 1
}

# getent goes through the system resolver (nsswitch + resolv.conf), exactly the
# path the uploader uses, and ships with glibc. The old host/nslookup check
# reported FAILED forever on stations without bind9-host/dnsutils installed.
check_dns() {
    local name
    for name in "${DNS_CHECK_NAMES[@]}"; do
        if timeout 6 getent hosts "$name" &>/dev/null; then
            return 0
        fi
    done
    return 1
}

# Prints "<gateway> <device>" of the default route; empty fields when absent.
default_route() {
    ip route show default 2>/dev/null | awk '{
        gw = ""; dev = ""
        for (i = 1; i < NF; i++) { if ($i == "via") gw = $(i + 1); if ($i == "dev") dev = $(i + 1) }
        print gw, dev; exit
    }'
}

# classify_uplink <has_internet> <gateway> <device>: where the uplink breaks,
# judged from the station outward.
#   ok      the internet answers by IP
#   isp     the gateway answers but nothing past it does (router WAN or ISP)
#   router  the link is up but the gateway does not answer (router or this NIC)
#   link    no default route, or its device has no carrier (cable, NIC, Wi-Fi)
classify_uplink() {
    local has_internet="$1" gw="$2" dev="$3"
    if [ "$has_internet" = true ]; then
        echo ok
    elif [ -z "$dev" ] || [ "$(cat "$SYSFS_NET/$dev/carrier" 2>/dev/null)" = "0" ]; then
        echo link
    elif [ -n "$gw" ] && ping -c 2 -W 2 "$gw" &>/dev/null; then
        echo isp
    else
        echo router
    fi
}

_duration() {
    local s="$1"
    printf '%dh%02dm' $(( s / 3600 )) $(( s % 3600 / 60 ))
}

# record_uplink <class> <gateway> <device>: one event line per change of class.
record_uplink() {
    local cls="$1" gw="$2" dev="$3" prev since now
    prev=$(get_state "uplink_state" "")
    now=$(date +%s)
    [ "$prev" = "$cls" ] && return 0
    since=$(get_num "uplink_since" "$now")
    set_state "uplink_state" "$cls"
    set_state "uplink_since" "$now"
    if [ -z "$prev" ]; then
        echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') uplink $cls (first run; gateway ${gw:-none} on ${dev:-none})" >> "$UPLINK_EVENTS"
    else
        echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') uplink $prev -> $cls after $(_duration $(( now - since ))) (gateway ${gw:-none} on ${dev:-none})" >> "$UPLINK_EVENTS"
    fi
    if [ "$(wc -l < "$UPLINK_EVENTS")" -gt "$UPLINK_EVENTS_KEEP" ]; then
        tail -n "$UPLINK_EVENTS_KEEP" "$UPLINK_EVENTS" > "$UPLINK_EVENTS.tmp" && mv "$UPLINK_EVENTS.tmp" "$UPLINK_EVENTS"
    fi
}

# wired_uplink_ok <rescue_iface>: a default route on any other device reaches
# the internet. The ping is bound to that device, so this judges the cable
# even while the rescue Wi-Fi carries the traffic.
wired_uplink_ok() {
    local rescue_if="$1" dev host
    for dev in $(ip route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") print $(i + 1)}' | sort -u); do
        [ "$dev" = "$rescue_if" ] && continue
        for host in "${INET_CHECK_HOSTS[@]}"; do
            ping -c 1 -W 3 -I "$dev" "$host" &>/dev/null && return 0
        done
    done
    return 1
}

_rescue_wifi_enabled() {
    python3 - "$CONFIGFILE" <<'PYEOF' 2>/dev/null
import json, sys
try:
    cfg = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
sys.exit(0 if cfg.get("watchdog", {}).get("rescue_wifi", True) else 1)
PYEOF
}

_uplink_event() {
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >> "$UPLINK_EVENTS"
}

# The flag archive_upload.py reads; rewritten every run while the rescue
# carries traffic, so the uploader can ignore a stale one if the watchdog
# stops running.
_rescue_flag() {
    if [ "$1" = up ]; then
        echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') rescue Wi-Fi up on ${2:-?}" > "$RESCUE_FLAG"
    else
        rm -f "$RESCUE_FLAG"
    fi
}

# The helper's `status` answers up (profile live and associated with the
# hotspot), waiting (netplan: profile live, nothing associated, e.g. no phone
# in range) or down. Sets RESCUE_UP (the rescue link is up after this step;
# whether it reaches the internet is measured by the caller), RESCUE_CAME_UP
# (this run brought it up) and RESCUE_IF.
RESCUE_UP=false
RESCUE_CAME_UP=false
RESCUE_IF=""
rescue_wifi_step() {
    if ! { [ -x "$RESCUE_HELPER" ] && [ -r "$RESCUE_CONF" ]; } || ! _rescue_wifi_enabled; then
        return 0
    fi
    local status bad good
    RESCUE_IF=$(sed -n 's/^iface=//p' "$RESCUE_CONF")
    status=$(sudo -n "$RESCUE_HELPER" status 2>/dev/null || echo unknown)
    case "$status" in
        up) RESCUE_UP=true ;;
        down|waiting) ;;
        *)
            # Helper did not answer: keep the last known state.
            status=unknown
            [ -f "$RESCUE_FLAG" ] && RESCUE_UP=true
            ;;
    esac
    if wired_uplink_ok "$RESCUE_IF"; then
        set_state "rescue_bad" "0"
        case "$status" in
            up|waiting) ;;
            *) set_state "rescue_good" "0"; return 0 ;;
        esac
        good=$(( $(get_num "rescue_good" "0") + 1 ))
        set_state "rescue_good" "$good"
        log "rescue wifi: $status, cable uplink working ($good/$RESCUE_WIFI_CLEAR runs)"
        [ "$good" -ge "$RESCUE_WIFI_CLEAR" ] || return 0
        if sudo -n "$RESCUE_HELPER" down 2>&1 | while IFS= read -r line; do log "  $line"; done; [ "${PIPESTATUS[0]}" -eq 0 ]; then
            _uplink_event "rescue wifi down: cable uplink back for $good runs"
            set_state "rescue_good" "0"
            RESCUE_UP=false
        else
            log "rescue wifi: taking $RESCUE_IF down FAILED"
        fi
    else
        set_state "rescue_good" "0"
        bad=$(( $(get_num "rescue_bad" "0") + 1 ))
        set_state "rescue_bad" "$bad"
        case "$status" in
            up) log "rescue wifi: up, cable uplink still failing"; return 0 ;;
            # Not re-applied every run: a netplan apply reconfigures every
            # interface, the camera LAN included.
            waiting) log "rescue wifi: profile live but no hotspot associated, cable uplink still failing"; return 0 ;;
        esac
        log "rescue wifi: $status, cable uplink failing ($bad/$RESCUE_WIFI_AFTER runs)"
        [ "$bad" -ge "$RESCUE_WIFI_AFTER" ] || return 0
        if sudo -n "$RESCUE_HELPER" up 2>&1 | while IFS= read -r line; do log "  $line"; done; [ "${PIPESTATUS[0]}" -eq 0 ]; then
            _uplink_event "rescue wifi up on $RESCUE_IF: cable uplink failed $bad runs"
            RESCUE_UP=true
            RESCUE_CAME_UP=true
        else
            log "rescue wifi: bringing $RESCUE_IF up FAILED (no hotspot in range, or the helper's sudoers rule is missing)"
        fi
    fi
}

# --- Recovery actions ---

recovery_restart_tailscale() {
    log "RECOVERY: restarting tailscaled ($TAILSCALED_UNIT)"
    _sudo systemctl restart "$TAILSCALED_UNIT"
    sleep "$RECOVERY_WAIT"
    tailscale_probe
    if [ "$TS_PROBE" = healthy ]; then
        log "RECOVERY SUCCESS after tailscaled restart"
        set_state "last_recovery" "$(date +%s)"
        set_state "ts_fail_count" "0"
        return 0
    fi
    log "tailscaled restart did not help: tailscale $(tailscale_verdict)"
    return 1
}

# Restarts the network stack, then tailscaled; success = the internet answers.
recovery_restart_networking() {
    log "RECOVERY: restarting networking + tailscaled"

    if systemctl list-units --type=service | grep -q NetworkManager; then
        _sudo systemctl restart NetworkManager
    elif systemctl list-units --type=service | grep -q systemd-networkd; then
        _sudo systemctl restart systemd-networkd
    else
        log "  no NetworkManager or systemd-networkd unit running"
    fi

    sleep "$NETWORK_SETTLE_WAIT"
    _sudo systemctl restart "$TAILSCALED_UNIT"
    sleep "$RECOVERY_WAIT"

    if check_internet; then
        log "RECOVERY SUCCESS after networking + tailscaled restart"
        set_state "last_recovery" "$(date +%s)"
        return 0
    fi
    return 1
}

# Internet reachable by IP but no name resolves: re-activate the NetworkManager
# connection that carries the default route, which re-runs DHCP and rewrites
# the DNS servers. Only that device is touched, so camera links (the eth0
# camera subnet on Pi stations) stay up. The connection is named by UUID, as
# in the exact sudoers rule updater.sh generates for it; a station whose
# sudoers predates that rule falls back to nmcli without sudo, which
# NetworkManager's polkit policy allows on some installs.
recovery_dns() {
    local dev con uuid target
    dev=$(ip route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')
    if [ -z "$dev" ] || ! command -v nmcli &>/dev/null; then
        log "DNS RECOVERY skipped: no default-route device or no nmcli"
        return 1
    fi
    con=$(timeout 15 nmcli -g GENERAL.CONNECTION device show "$dev" 2>/dev/null | head -1)
    if [ -z "$con" ]; then
        log "DNS RECOVERY skipped: no NetworkManager connection on $dev"
        return 1
    fi
    uuid=$(timeout 15 nmcli -g GENERAL.CON-UUID device show "$dev" 2>/dev/null | head -1)
    target=("$con")
    [ -n "$uuid" ] && target=(uuid "$uuid")
    log "DNS RECOVERY: re-activating connection '$con' on $dev (uplink only)"
    if ! _sudo nmcli -w 30 connection up "${target[@]}"; then
        log "  trying nmcli without sudo"
        timeout -k 5 45 nmcli -w 30 connection up "${target[@]}" 2>&1 | while IFS= read -r line; do log "  $line"; done
    fi
    sleep "$RECOVERY_WAIT"
    if check_dns; then
        log "DNS RECOVERY SUCCESS after re-activating '$con'"
        set_state "dns_fail_count" "0"
        return 0
    fi
    log "DNS RECOVERY did not restore name resolution; will retry after ${DNS_BOUNCE_COOLDOWN}s"
    return 1
}

# watchdog.allow_reboot in config.json gates the reboot rung (Bacau: firmware
# hangs at POST on reboot, so a watchdog reboot takes the station down for good).
_reboot_allowed() {
    python3 - "$CONFIGFILE" <<'PYEOF' 2>/dev/null
import json, sys
try:
    cfg = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)  # unreadable config: default allow
allow = cfg.get("watchdog", {}).get("allow_reboot", True)
sys.exit(0 if allow else 1)
PYEOF
}

# The reboot rung waits for daylight. At night RMS and colour capture keep
# recording to local disk whatever the uplink does, so a reboot fixes nothing
# the next morning could not and costs minutes of sky: on 2026-09-08 an
# upstream repeater outage made this rung reboot gmnro01 (Ghirdoveni) every
# hour, and its reboots, the 13:45 one included, were switched off for three
# weeks. The station location comes from config.json (rovimen_config), else
# from the RMS .config of a running or installed camera (camera_fps_guard's
# parser). Prints the sun altitude; exit 3 = sun below the horizon. Any other
# exit (daytime, no location anywhere, a helper that fails) allows the reboot.
# ROVIMEN_WATCHDOG_NOW (ISO 8601 UTC) overrides the clock for tests.
_sun_below_horizon() {
    python3 - "$CONFIGFILE" "$HOME/rovimen_scripts" <<'PYEOF' 2>/dev/null
import glob, json, os, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, sys.argv[2])
loc = None
try:
    from rovimen_config import station_location
    loc = station_location(json.load(open(sys.argv[1])))
except Exception:
    loc = None
if loc is None:
    try:
        import camera_fps_guard as g
        home = os.path.expanduser("~")
        paths = list(g.running_rms_configs())
        paths += [Path(p) for p in sorted(glob.glob(home + "/RMS_cam*/.config"))]
        paths += [Path(p) for p in sorted(glob.glob(home + "/source/Stations/*/.config"))]
        paths.append(Path(home) / "source" / "RMS" / ".config")
        for path in paths:
            cam = g.parse_rms_config(path) if path.is_file() else None
            if cam is not None and cam.lat is not None and cam.lon is not None and (cam.lat, cam.lon) != (0.0, 0.0):
                loc = (cam.lat, cam.lon)
                break
    except Exception:
        loc = None
if loc is None:
    print("unknown")
    sys.exit(0)
from rovimen_night import sun_altitude_deg
now = os.environ.get("ROVIMEN_WATCHDOG_NOW")
when = datetime.fromisoformat(now.replace("Z", "+00:00")) if now else datetime.now(timezone.utc)
alt = sun_altitude_deg(loc[0], loc[1], when)
print("%.1f" % alt)
sys.exit(3 if alt < 0.0 else 0)
PYEOF
}

# dawn_process holds an flock on its lock file while it runs. Read through
# rovimen_night.dawn_running (/proc/locks), never by trying the lock, which
# would make a dawn cron tick starting in that instant exit.
_dawn_running() {
    python3 - "$HOME/rovimen_scripts" "$DAWN_LOCK" <<'PYEOF' 2>/dev/null
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from rovimen_night import dawn_running
sys.exit(0 if dawn_running(Path(sys.argv[2])) else 1)
PYEOF
}

# The interpreter dawn runs under. First the one on the dawn_process.py line
# of the dawn cron file (toggle_rovimen.sh --ensure-crons writes it), then the
# rule that line was written by (toggle_rovimen.sh _venv_python: the RMS unit's
# ExecStart interpreter, then the known venvs), then python3.
_dawn_python() {
    local cand
    if [ -r "$DAWN_CRON" ]; then
        cand=$(grep -m1 'dawn_process\.py' "$DAWN_CRON" 2>/dev/null \
            | grep -oE '[^ ]+ +[^ ]*/dawn_process\.py' | head -n1 | cut -d' ' -f1)
        [ -n "$cand" ] && [ -x "$cand" ] && { printf '%s\n' "$cand"; return 0; }
    fi
    cand=$(grep -h 'ExecStart' "$RMS_UNIT_DIR"/rms-cam*.service "$HOME"/.config/systemd/user/rms-cam*.service 2>/dev/null \
        | grep -oE '/[^ ]+/bin/python[0-9.]*' | head -n1)
    [ -n "$cand" ] && [ -x "$cand" ] && { printf '%s\n' "$cand"; return 0; }
    for cand in "$HOME/vRMS/bin/python3" "$HOME/RMS/venv/bin/python3" "$HOME/RMS/venv/bin/python"; do
        [ -x "$cand" ] && { printf '%s\n' "$cand"; return 0; }
    done
    printf 'python3\n'
}

# RMS post-processes the night after capture ends (detection, ApplyRecalibrate,
# archiving: 10 min to 3 h), and a reboot cuts that short. On 2026-11-22 this
# rung rebooted gmnro09 (Sibiciu) at 08:56 UTC in the middle of RMS's
# ApplyRecalibrate while dawn_process happened not to run, and auto_reprocess
# had to redo the night. The answer is dawn's own RMS gate (dawn_process
# _rms_runs + _rms_night, the .rms_processing_status record), asked for the
# night that just ended: a camera counts only while its RMS process is alive
# and not idle until its next night, and either a session with detections has
# its processing record short of 'completed', or capture has ended and a
# session has frames but no detections yet (detection running). A camera that
# is capturing or waiting for its night never counts. Prints which cameras.
# Exit 3 = defer. Exit 4 = busy, but past REBOOT_RMS_WAIT_UNTIL (13:15 UTC, the
# fleet's dawn deadline) or the outage is older than REBOOT_RMS_MAX_OUTAGE, so
# a hung RMS cannot keep the station offline. Exit 0 = nothing busy (or no
# RMS). Any other exit (an import or config error) also means not processing,
# the old behaviour, and the caller logs it.
# Usage: _rms_postprocessing <interpreter> <outage age s> <stderr file>. The
# interpreter must be dawn's (_dawn_python): dawn_process imports numpy and
# PIL, which the system python3 lacks on 7 of 20 stations (2026-11-22).
_rms_postprocessing() {
    "$1" - "$HOME/rovimen_scripts" "$CONFIGFILE" "$2" "$REBOOT_RMS_MAX_OUTAGE" "$REBOOT_RMS_WAIT_UNTIL" <<'PYEOF' 2>"$3"
import json, os, sys
from datetime import datetime, timezone
sys.path.insert(0, sys.argv[1])
import dawn_process
import rovimen_night
cfg = json.load(open(sys.argv[2]))
age, max_age = int(sys.argv[3]), int(sys.argv[4])
until_h, until_m = (int(x) for x in sys.argv[5].split(":"))
now = os.environ.get("ROVIMEN_WATCHDOG_NOW")
when = datetime.fromisoformat(now.replace("Z", "+00:00")) if now else datetime.now(timezone.utc)
night = rovimen_night.last_completed_night(when)
stations = cfg.get("stations") or {}
busy = []
for sid, run in sorted(dawn_process._rms_runs().items()):
    scfg = stations.get(sid)
    if run is None or run.idle or not isinstance(scfg, dict) or not scfg.get("rms", True):
        continue
    n = dawn_process._rms_night(sid, night, scfg.get("rms_data_path", ""), run)
    if n.postprocessing:
        busy.append(sid + " post-processing")
    elif n.unprocessed and run.capture_state == "ended":
        busy.append(sid + " detection")
if not busy:
    sys.exit(0)
detail = "night %s: %s" % (night, ", ".join(busy))
if (when.hour, when.minute) >= (until_h, until_m):
    print("%s; past %02d:%02d UTC" % (detail, until_h, until_m))
    sys.exit(4)
if age >= max_age:
    print("%s; outage over %d h" % (detail, max_age // 3600))
    sys.exit(4)
print(detail)
sys.exit(3)
PYEOF
}

# recovery_reboot <uplink class>: the last rung, for classes link and router.
# Not before the outage is REBOOT_MIN_OUTAGE old (an owner power-cycling the
# router must not reboot the station). Class link waits REBOOT_COOLDOWN after
# a watchdog reboot, doubling with each reboot in the same outage up to
# ROUTER_REBOOT_COOLDOWN (1 h, 2 h, 4 h, 6 h); class router always waits
# ROUTER_REBOOT_COOLDOWN. Hourly futile reboots at gmnro01 (Ghirdoveni) on
# 2026-09-08 got reboots switched off for three weeks.
recovery_reboot() {
    local cls="$1" cooldown="$REBOOT_COOLDOWN" reboots i age
    reboots=$(get_num "outage_reboots" 0)
    if [ "$cls" = router ]; then
        cooldown="$ROUTER_REBOOT_COOLDOWN"
    else
        for (( i = 0; i < reboots && cooldown < ROUTER_REBOOT_COOLDOWN; i++ )); do
            cooldown=$(( cooldown * 2 ))
        done
        [ "$cooldown" -gt "$ROUTER_REBOOT_COOLDOWN" ] && cooldown="$ROUTER_REBOOT_COOLDOWN"
    fi

    if ! _reboot_allowed; then
        log "RECOVERY: REBOOT DISABLED by config (watchdog.allow_reboot=false)"
        return 1
    fi
    age=$(seconds_since "outage_start")
    if [ "$age" -lt "$REBOOT_MIN_OUTAGE" ]; then
        log "RECOVERY: REBOOT WAITS: outage ${age}s old, a reboot needs ${REBOOT_MIN_OUTAGE}s"
        return 1
    fi
    if _dawn_running; then
        log "RECOVERY: REBOOT DEFERRED while dawn_process runs"
        return 1
    fi

    # Before the cooldown check, so a deferred reboot costs no cooldown and the
    # first run after sunrise reboots.
    local sun sun_rc
    sun=$(_sun_below_horizon)
    sun_rc=$?
    if [ "$sun_rc" -eq 3 ]; then
        log "RECOVERY: REBOOT DEFERRED until sunrise (sun ${sun} deg); capture keeps recording offline"
        return 1
    fi

    local elapsed
    elapsed=$(seconds_since "last_reboot")
    if [ "$elapsed" -lt "$cooldown" ]; then
        log "RECOVERY: REBOOT SKIPPED (cooldown for uplink $cls after $reboots reboot(s) this outage: ${elapsed}s < ${cooldown}s since last watchdog reboot)"
        return 1
    fi

    # Like the sunrise deferral, a deferral here sets no cooldown state.
    local rms rms_rc rms_py rms_err
    rms_py=$(_dawn_python)
    rms_err=$(mktemp 2>/dev/null) || rms_err=/dev/null
    rms=$(_rms_postprocessing "$rms_py" "$age" "$rms_err")
    rms_rc=$?
    if [ "$rms_rc" -eq 4 ]; then
        log "RECOVERY: RMS still post-processing ($rms): rebooting anyway"
    elif [ "$rms_rc" -ne 0 ] && [ "$rms_rc" -ne 3 ]; then
        log "RECOVERY: RMS post-processing check unavailable ($rms_py: $(tail -n1 "$rms_err" 2>/dev/null | tr -d '\r' | cut -c1-200)): not deferring"
    fi
    [ "$rms_err" != /dev/null ] && rm -f "$rms_err"
    if [ "$rms_rc" -eq 3 ]; then
        log "RECOVERY: REBOOT DEFERRED while RMS post-processes the night ($rms)"
        return 1
    fi

    log "RECOVERY: REBOOTING (uplink $cls, outage ${age}s old, reboot $(( reboots + 1 )) of this outage, sun ${sun} deg, last watchdog reboot ${elapsed}s ago)"
    set_state "last_reboot" "$(date +%s)"
    set_state "outage_reboots" "$(( reboots + 1 ))"
    set_state "reboot_reason" "uplink_${cls}_$(date -u '+%Y%m%dT%H%M%SZ')"
    sync
    _sudo "$REBOOT_BIN"
}

# Internet up, Tailscale not healthy. Nothing but tailscaled is restarted: the
# internet works, so the fault is tailscaled or the Tailscale control plane
# (502s for half an hour on 2026-11-16), and restarting networking or
# rebooting the fleet would fix neither.
tailscale_ladder() {
    local n since
    n=$(( $(get_num "ts_fail_count" 0) + 1 ))
    set_state "ts_fail_count" "$n"
    log "Internet up but tailscale $(tailscale_verdict) ($n/$TS_FAIL_THRESHOLD runs)"
    if [ "$n" -lt "$TS_FAIL_THRESHOLD" ]; then
        return 0
    fi
    since=$(seconds_since "last_ts_restart")
    if [ "$since" -lt "$TS_RESTART_COOLDOWN" ]; then
        log "tailscaled restarted ${since}s ago; next restart allowed after ${TS_RESTART_COOLDOWN}s (no networking restart or reboot for this)"
        return 0
    fi
    # Recorded before acting, so a run killed during the restart still spends it.
    set_state "last_ts_restart" "$(date +%s)"
    recovery_restart_tailscale || true
}

_reset_outage() {
    set_state "net_fail_count" "0"
    set_state "net_restart_count" "0"
    set_state "outage_start" "0"
    set_state "outage_reboots" "0"
}

# internet_ladder <class>: no internet, uplink class link or router.
internet_ladder() {
    local cls="$1" n restarts since gap i sun
    n=$(( $(get_num "net_fail_count" 0) + 1 ))
    set_state "net_fail_count" "$n"
    # The outage starts when the counter leaves 0; a missing, corrupt or
    # future start reads as "just begun".
    if [ "$n" -eq 1 ] || [ "$(get_num "outage_start" 0)" -eq 0 ] || [ "$(get_num "outage_start" 0)" -gt "$(date +%s)" ]; then
        set_state "outage_start" "$(date +%s)"
    fi
    log "No internet, uplink $cls ($n/$NET_FAIL_THRESHOLD runs)"
    if [ "$n" -lt "$NET_FAIL_THRESHOLD" ]; then
        log "waiting for another failed run before acting (one failed probe is often a blip)"
        return 0
    fi
    # Class router waits for sunrise even for a networking restart: nothing on
    # the station fixes a dead router, some routers never answer ping (so an
    # ISP outage reads as router there), and a restart at night can interrupt
    # the camera streams that capture otherwise records through untouched.
    if [ "$cls" = router ]; then
        sun=$(_sun_below_horizon)
        if [ "$?" -eq 3 ]; then
            log "RECOVERY: networking restart for uplink router DEFERRED until sunrise (sun ${sun} deg)"
            return 0
        fi
    fi
    restarts=$(get_num "net_restart_count" 0)
    since=$(seconds_since "last_net_restart")
    gap="$NET_RESTART_COOLDOWN"
    for (( i = 1; i < restarts && gap < NET_RESTART_BACKOFF_MAX; i++ )); do
        gap=$(( gap * 2 ))
    done
    [ "$gap" -gt "$NET_RESTART_BACKOFF_MAX" ] && gap="$NET_RESTART_BACKOFF_MAX"
    if [ "$restarts" -eq 0 ] || [ "$since" -ge "$gap" ]; then
        # Recorded before acting, so a run killed during the restart still
        # spends the budget.
        set_state "net_restart_count" "$(( restarts + 1 ))"
        set_state "last_net_restart" "$(date +%s)"
        if recovery_restart_networking; then
            _reset_outage
            return 0
        fi
        log "networking restart $(( restarts + 1 )) of this outage did not bring the internet back"
        return 0
    fi
    log "networking restarted ${since}s ago ($restarts this outage); next restart allowed after ${gap}s"
    recovery_reboot "$cls" || true
}

# --- Service health ---

# _heal_unit <unit> <scope: system|user>
# Restarts an enabled-but-dead unit, budgeted to SERVICE_RESTART_BUDGET
# attempts per SERVICE_RESTART_WINDOW. Never touches disabled units — Buzau
# runs with color-capture deliberately disabled and must stay that way.
_heal_unit() {
    local unit="$1" scope="$2"
    local sysctl=(systemctl)
    if [ "$scope" = "user" ]; then
        sysctl=(env "XDG_RUNTIME_DIR=/run/user/$(id -u)" systemctl --user)
    fi

    # NB: is-enabled/is-active print their state AND exit non-zero for anything
    # but enabled/active, so capture stdout and only default when it is empty.
    local enabled
    enabled=$("${sysctl[@]}" is-enabled "$unit" 2>/dev/null | head -n1)
    enabled=${enabled:-unknown}
    if [ "$enabled" != "enabled" ]; then
        return 0  # disabled / static / not installed: not ours to manage
    fi

    local active
    active=$("${sysctl[@]}" is-active "$unit" 2>/dev/null | head -n1)
    active=${active:-unknown}
    case "$active" in
        active|activating|reloading)
            set_state "svc_${unit}_count" "0"
            return 0
            ;;
    esac

    # Unit is enabled but dead — check restart budget.
    local now window_start count
    now=$(date +%s)
    window_start=$(get_state "svc_${unit}_window" "0")
    count=$(get_state "svc_${unit}_count" "0")
    # get_state returns "" when the state file exists but the key is absent
    window_start=${window_start:-0}
    count=${count:-0}
    if [ $((now - window_start)) -gt "$SERVICE_RESTART_WINDOW" ]; then
        set_state "svc_${unit}_window" "$now"
        count=0
    fi
    if [ "$count" -ge "$SERVICE_RESTART_BUDGET" ]; then
        log "service $unit ($scope): DEAD (state=$active) but restart budget exhausted ($count/$SERVICE_RESTART_BUDGET this hour) — leaving alone"
        return 1
    fi

    log "service $unit ($scope): DEAD (state=$active, enabled) — attempting restart ($((count + 1))/$SERVICE_RESTART_BUDGET)"
    set_state "svc_${unit}_count" "$((count + 1))"

    local restart_cmd=("${sysctl[@]}")
    if [ "$scope" = "system" ]; then
        restart_cmd=(timeout -k 5 "$SUDO_TIMEOUT" sudo -n systemctl)
    fi
    "${restart_cmd[@]}" reset-failed "$unit" 2>/dev/null || true
    "${restart_cmd[@]}" restart "$unit" 2>&1 | while IFS= read -r line; do log "  $line"; done

    sleep "$SERVICE_RECHECK_WAIT"
    active=$("${sysctl[@]}" is-active "$unit" 2>/dev/null | head -n1)
    active=${active:-unknown}
    case "$active" in
        active|activating)
            log "service $unit ($scope): RECOVERED (state=$active)"
            return 0
            ;;
        *)
            log "service $unit ($scope): still dead after restart (state=$active)"
            return 1
            ;;
    esac
}

check_services() {
    log_section "SERVICE HEALTH"

    for unit in "${SYSTEM_HEALTH_UNITS[@]}"; do
        # Pusher is opt-in: without ingest.key it exits immediately by design.
        if [ "$unit" = "rovimen-pusher" ] && [ ! -f "$HOME/.config/rovimen/ingest.key" ]; then
            continue
        fi
        _heal_unit "$unit" system
    done

    # User units need a live session bus; skip when the user runtime dir is absent.
    if [ -d "/run/user/$(id -u)" ]; then
        for unit in "${USER_HEALTH_UNITS[@]}"; do
            _heal_unit "$unit" user
        done
    else
        log "user units: SKIPPED (/run/user/$(id -u) missing — no session bus)"
    fi
}

# --- Camera frame rate ---
# A camera whose settings reverted runs at ~12 fps once the sky is dark
# (DE001E (Johannismühle), 2026-09-27). camera_fps_guard.py reads RMS's block
# timing and, in services.fps_guard.mode "fix", re-applies that camera's
# settings through fix_cam_encoding.sh --only (no camera reboot).
check_camera_fps() {
    local guard="$HOME/rovimen_scripts/camera_fps_guard.py"
    [ -f "$guard" ] || return 0
    log_section "CAMERA FPS"
    timeout 200 python3 "$guard" 2>&1 | while IFS= read -r line; do log "$line"; done
    return 0
}

# --- Camera stall ---
# A camera can stop delivering frames at night while its RTSP port keeps
# answering (RO20XB (Brăila), 22:48-03:23 UTC on 2026-10-04), and only a camera
# reboot brings it back. camera_stall_guard.py reads RMS's grab failures and FF
# files and, in services.cam_stall.mode "fix", reboots the camera through RMS's
# CameraControl (at most 3 times a night, 30 min apart).
check_camera_stall() {
    local guard="$HOME/rovimen_scripts/camera_stall_guard.py"
    [ -f "$guard" ] || return 0
    log_section "CAMERA STALL"
    timeout 200 python3 "$guard" 2>&1 | while IFS= read -r line; do log "$line"; done
    return 0
}

# --- Main ---

# --services-only: run just the service-health pass (tests, manual invocation).
if [ "${1:-}" = "--services-only" ]; then
    rotate_logs
    check_services
    exit 0
fi

rotate_logs

log_section "WATCHDOG RUN START"
log "script: $0"
log "user: $(whoami)"
log "date: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

tailscale_probe
log_vitals
log_network
log_tailscale
log_journal_errors

# Connectivity assessment
has_internet=false
has_dns=false

if check_internet; then
    has_internet=true
    log "internet: OK"
else
    log "internet: UNREACHABLE"
fi

read -r uplink_gw uplink_dev <<< "$(default_route)"
uplink=$(classify_uplink "$has_internet" "$uplink_gw" "$uplink_dev")
log "uplink: $uplink (gateway ${uplink_gw:-none} on ${uplink_dev:-none})"
record_uplink "$uplink" "$uplink_gw" "$uplink_dev"
# Before the ladder: on a dead cable the rescue Wi-Fi is the only way back.
rescue_wifi_step
# The rescue counts only while it carries traffic: a profile that is up with
# no phone in range must not stop the ladder for the whole outage.
RESCUE_CARRIES=false
if $RESCUE_UP; then
    # Measured again, because the internet measured above may predate the rescue.
    if check_internet; then
        has_internet=true
        RESCUE_CARRIES=true
        log "internet over the rescue Wi-Fi: OK"
    else
        log "internet over the rescue Wi-Fi: UNREACHABLE"
    fi
fi
if $RESCUE_CARRIES; then _rescue_flag up "$RESCUE_IF"; else _rescue_flag down; fi

if check_dns; then
    has_dns=true
    log "dns: OK"
    set_state "dns_fail_count" "0"
else
    log "dns: FAILED"
    if $has_internet; then
        set_state "dns_fail_count" "$(( $(get_num "dns_fail_count" "0") + 1 ))"
    fi
fi

log "tailscale: $(tailscale_verdict)"
[ "$TS_PROBE" = healthy ] && set_state "last_ok" "$(date +%s)"
# One event line per change of login state; a logged-out node gets no action.
prev_logged_out=$(get_state "ts_logged_out" "")
if [ "$TS_PROBE" = logged_out ]; then
    if [ "$prev_logged_out" != "$TS_BACKEND" ]; then
        _uplink_event "tailscale logged out ($TS_BACKEND): no recovery action, it needs a login"
        set_state "ts_logged_out" "$TS_BACKEND"
    fi
elif [ -n "$prev_logged_out" ] && [ "$TS_PROBE" != timeout ] && [ "$TS_PROBE" != no_status ]; then
    _uplink_event "tailscale no longer logged out (was $prev_logged_out, now $(tailscale_verdict))"
    set_state "ts_logged_out" ""
fi

check_services
check_camera_fps
check_camera_stall

# Recovery
if $RESCUE_CARRIES; then
    log_section "RECOVERY"
    # Restarting networking would drop the hotspot link, and the cable state
    # is judged by rescue_wifi_step on every run.
    log "rescue wifi carries traffic: recovery ladder skipped"
elif ! $has_internet; then
    set_state "ts_fail_count" "0"
    log_section "RECOVERY"
    if [ "$TS_PROBE" = healthy ]; then
        # Tailscale reached its control plane, so packets do flow: the network
        # probably drops ICMP to the check hosts. Nothing to restart.
        log "internet probes fail but tailscale is online: no action"
        _reset_outage
    elif [ "$uplink" = isp ]; then
        # Restarting networking, or rebooting, cannot bring back a router WAN
        # or an ISP line, and the reboot would cut the dawn run short.
        log "Gateway $uplink_gw answers but the internet does not: the outage is past the router (router WAN or ISP), nothing on this station to restart"
    else
        internet_ladder "$uplink"
    fi
else
    _reset_outage
    case "$TS_PROBE" in
        healthy) set_state "ts_fail_count" "0" ;;
        logged_out)
            set_state "ts_fail_count" "0"
            log "tailscale is logged out ($TS_BACKEND): no recovery action, a person has to log it in"
            ;;
        missing) set_state "ts_fail_count" "0" ;;
        *)
            log_section "RECOVERY"
            tailscale_ladder
            ;;
    esac
fi

# DNS rung: independent of the ladders above, because Tailscale can stay up
# (it dials by IP) while the station cannot resolve its upload host. Skipped
# while the rescue Wi-Fi is up: the default-route connection is the hotspot.
if $has_internet && ! $has_dns && ! $RESCUE_UP; then
    dns_fails=$(get_num "dns_fail_count" "0")
    if [ "$dns_fails" -ge "$DNS_FAIL_THRESHOLD" ] && [ "$(seconds_since "last_dns_bounce")" -ge "$DNS_BOUNCE_COOLDOWN" ]; then
        log_section "DNS RECOVERY"
        log "internet OK but DNS failed $dns_fails runs in a row"
        set_state "last_dns_bounce" "$(date +%s)"
        recovery_dns || true
    fi
fi

log "WATCHDOG RUN COMPLETE"
log "---"
