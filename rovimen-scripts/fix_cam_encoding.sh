#!/bin/bash
# Fix camera encoding settings after reboot.
# Reads camera IPs from rovimen_scripts/config.json — no hardcoded IPs.
#
# TWO camera families are enforced, dispatched per camera (see CAMERA FAMILIES
# below): XMEye/DVRIP on port 34567, and HDIPC/Heroseen over an HTTP JSON API.
#
# XMEye/DVRIP — applied per camera:
#   - Resolution: 720P (MainFormat) — drift to 3M/1080P causes massive RMS frame drops
#   - BitRateControl: CBR, BitRate: 8192, Quality: 6 (overridable per camera via
#     "camera_venc", same key as the HDIPC family — see CONFIG OVERRIDES below)
#   - DayNightColor: 0x00000001 (always color, prevents auto day/night switch to B&W)
#   - IrcutSwap: 1 (correct IR cut orientation for these cameras)
#   - EsShutter: 0x0 (auto electronic shutter, prevents frame throttling at night)
#   - WhiteBalance: 0x2 (consistent white balance mode across cameras)
#   - DncThr: 50 (day/night threshold, consistent across cameras)
#   - ElecLevel: 40, AeSensitivity: 1 (exposure target / metering)
#   - ExposureParam: LeastTime/MostTime 0x9C40 (40ms = full 1/25s frame; without
#     this the shutter caps at ~1ms and night frames are black)
#   - GainParam: AutoGain 1, Gain 60 (auto-gain must be on, or night is black)
#   - ClearFog: disabled, level 30 (consistent across cameras)
#   - AutoMaintain: reboot Everyday at 12:00 CAMERA time (see clock note below;
#     camera_reboot_day / camera_reboot_hour override it, see the camera list)
#   - Camera clock: synced to host UTC every run, with camera NTP turned off,
#     but only while the host clock is NTP-synchronised (see host_clock_synced)
#
# Camera clock / AutoReboot (2026-07-04 fleet audit): the cameras have no NTP,
# so their clocks free-run — found 11 days to 1.5 YEARS off across the fleet.
# The firmware AutoReboot fires at the scheduled hour in CAMERA time, so a
# drifted clock moves the daily reboot into the capture window: one ~850-frame
# (~35-50 s) hole per night per camera, or a fully degraded night when the
# camera comes back with reverted settings. Standard: camera clock = UTC,
# reboot Everyday@12:00 — daytime year-round (December capture in Romania
# starts ~14:30 UTC, so even a correct 15:00 would land in the winter window).
#
# These settings can revert after a camera reboot (firmware behaviour).
# Run this script from cron at boot or after a known camera reboot window.
#
# CAMERA FAMILIES
# ---------------
# The fleet runs two incompatible config surfaces:
#
#   xmeye — XMEye/DVRIP on TCP 34567, driven through dvrip_vendored.py. Every
#           block above applies. This is the majority of the fleet.
#   hdipc — HDIPC/Heroseen (rebranded IMX662 units)
#           have NO port 34567 at all; their config surface is
#           an HTTP JSON API on port 80 with basic auth (see
#           docs/camera_firmware_heroseen.md). Enforced for this family:
#             - Encoder (setVencConf): 720p / 25 fps / 8192 kbps / rc_mode 1
#               (CBR) / gop 12 — the 2026-07-09 fleet standard.
#             - AutoReboot (setRebootConf): Everyday 12:00 camera time, same
#               rationale as the XMEye AutoMaintain block above. The factory
#               default on this firmware is an HOURLY reboot, which cost
#               Chiribis RO001B 3-8k-drop nights.
#           Image/exposure and OSD are deliberately NOT enforced: brightness /
#           contrast / gamma are per-site (light pollution differs) and the
#           2026-07-09 image config measurably cut detections on RO0014 (see
#           docs/imx662_settings_detection_impact.md). Encoder and reboot
#           schedule are the fleet-invariant parts.
#
# Family per camera comes from the "camera_api" config key ("xmeye" | "hdipc");
# when absent it is probed. "openipc" or "none" marks a camera this script must
# not touch at all: no probe, no 34567 subnet scan, no re-IP, no settings. Since
# 2026-11-07 DE0018 (Berlin) runs OpenIPC with its own control (TCP 9600) and clock
# (chrony from the station); probing it found no DVRIP, so every run scanned the
# LAN and would have moved any XM camera found there onto its address. Until this dispatch existed, the XMEye-only script
# logged "not reachable after 5 min, skipping" for every HDIPC camera every day
# — settings enforcement was a silent no-op on those stations.
#
# CONFIG OVERRIDES
# ----------------
# The encoder target is the fleet default unless "camera_venc" says otherwise.
# It is read per camera (stations.<CODE>.camera_venc) with a station-wide
# fallback (top-level camera_venc); the per-camera object wins whole, as it has
# since the key was introduced for the HDIPC family. Recognised fields:
#
#   both families   bitrate            kbps
#   xmeye only      resolution         MainFormat Resolution, e.g. "720P"
#                   bitrate_control    "CBR" | "VBR" (alias: rc)
#                   quality            1-6
#   hdipc only      width height fps rc_mode gop
#
# Any subset may be given; unlisted fields keep the fleet default, so an
# override is a delta and not a second copy of the standard. Example — Bacau
# RO001E, whose night scene needs ~8.4 Mbps at Quality 6 and shed frames
# against the 8192 cap (2026-09-01):
#
#   "stations": { "RO001E": { "camera_venc": { "bitrate": 12288 } } }
#
# An override only matters through the enforced target: a value set by hand on
# the camera is reverted by the next run of this script.
#
# Usage:  bash fix_cam_encoding.sh
#         bash fix_cam_encoding.sh --only <CODE>   (one camera, e.g. RO000E)
#         --only is how camera_fps_guard.py re-applies a camera that dropped to
#         slow-shutter frame rates at night: that one camera only, no subnet
#         re-IP (an automatic trigger must never reprogram an address), exit
#         code = that camera's (0 ok, 2 unreachable, 1 failed), and the fleet
#         status file below is left to the full run.
# Log:    ~/fix_cam_encoding.log
# Status: ~/rovimen_scripts/fixcam_status.json (ok/skipped/failed per camera,
#         plus per-camera family/state and a none_enforced flag).
#         Exits non-zero if any reachable camera failed — a broken run must be
#         loud, not "All done" (silent-failure regression found on gmnro07).

# Paths are overridable so the shell tests can run the real script against a
# fixture config; unset on stations, where the defaults below apply.
CONFIG="${FIXCAM_CONFIG:-$HOME/rovimen_scripts/config.json}"
LOG="${FIXCAM_LOG:-$HOME/fix_cam_encoding.log}"
STATUS_FILE="${FIXCAM_STATUS:-$HOME/rovimen_scripts/fixcam_status.json}"

# Camera-boot wait: 60 x 5 s = 5 min, both families. Lowered only by the tests,
# which must not sit through a real boot window.
WAIT_ATTEMPTS="${FIXCAM_WAIT_ATTEMPTS:-60}"

ONLY=""
if [ "${1:-}" = "--only" ]; then
    ONLY="${2:-}"
    if [ -z "$ONLY" ]; then
        echo "usage: $0 [--only <CODE>]" >&2
        exit 2
    fi
fi

# Resolve the RMS venv python. ExecStart= parsing alone breaks whenever a
# station's unit naming/layout differs (found silently dead on gmnro07 for
# weeks), so fall back through the known venv locations and verify the
# interpreter actually runs. FIXCAM_PYTHON is tried first (and still verified)
# so the shell tests can run without an RMS venv.
VENV_PYTHON=""
for cand in \
    "${FIXCAM_PYTHON:-}" \
    $(grep -hoP 'ExecStart=\K\S+/bin/python3?' /etc/systemd/system/rms*.service ~/.config/systemd/user/rms*.service 2>/dev/null) \
    $(grep -rlE 'ExecStart=.*RMS' /etc/systemd/system ~/.config/systemd/user 2>/dev/null | xargs -r grep -hoP 'ExecStart=\K\S+/bin/python3?' 2>/dev/null | sort -u) \
    $(pgrep -af 'RMS\.StartCapture' 2>/dev/null | grep -oP '\S+/bin/python3?' | sort -u) \
    "$HOME/RMS/venv/bin/python3" "$HOME/RMS/venv/bin/python" \
    "$HOME/vRMS/bin/python3" "$HOME/vRMS/bin/python" \
    "$HOME/source/RMS/venv/bin/python3" \
    "$HOME/gmn_rms/venv_rms/bin/python" "$HOME/gmn_rms/venv/bin/python3"; do
    if [ -x "$cand" ] && "$cand" -c '' 2>/dev/null; then
        VENV_PYTHON="$cand"
        break
    fi
done
if [ -z "$VENV_PYTHON" ]; then
    echo "$(date): ERROR: no working RMS venv python found (checked rms*.service ExecStart + standard venv paths)" >> "$LOG"
    exit 1
fi

# Is the host clock NTP-synchronised? Prints "yes <source>" or "no <source>".
# A Raspberry Pi has no RTC: after a power cut it boots with the clock it had
# at shutdown until NTP answers, and the @reboot run of this script (sleep 120)
# can come first when the uplink is slow to return. Pushing that clock into the
# cameras moves their 12:00 AutoReboot into the night's capture until the next
# run (gmnro16 (Bacău) after the 2026-11-09 power cut; known failure mode 15).
# Sources: timedatectl's NTPSynchronized property; a "no" or no answer there
# is re-checked with chronyc's leap status (chrony is the NTP client on some
# stations); then the "synchronized:" line of timedatectl status (systemd too
# old for `show -p`). Cannot tell = not synchronised.
host_clock_synced() {
    local v leap
    v=$(timeout 5 timedatectl show -p NTPSynchronized --value 2>/dev/null)
    if [ "$v" = "yes" ]; then
        echo "yes timedatectl"; return
    fi
    leap=$(timeout 5 chronyc tracking 2>/dev/null | sed -n 's/^Leap status *: *//p')
    case "$leap" in
        Normal|"Insert second"|"Delete second") echo "yes chronyc"; return ;;
        ?*) echo "no chronyc"; return ;;
    esac
    if [ "$v" = "no" ]; then
        echo "no timedatectl"; return
    fi
    v=$(timeout 5 timedatectl status 2>/dev/null | sed -n 's/^ *\(System clock\|NTP\) synchronized: *//p' | head -n 1)
    case "$v" in
        yes) echo "yes timedatectl-status" ;;
        no) echo "no timedatectl-status" ;;
        *) echo "no unknown" ;;
    esac
}

wait_for_camera() {
    local ip=$1
    local attempts=0
    while [ $attempts -lt "$WAIT_ATTEMPTS" ]; do
        timeout 2 bash -c "echo >/dev/tcp/$ip/34567" 2>/dev/null && return 0
        sleep 5
        attempts=$((attempts + 1))
    done
    return 1
}

enforce_camera_ip() {
    local expected_ip=$1
    local station=$2
    local subnet
    subnet=$(echo "$expected_ip" | sed 's/\.[0-9]*$/./')

    if timeout 2 bash -c "echo >/dev/tcp/$expected_ip/34567" 2>/dev/null; then
        echo "$(date): [$station] Camera at $expected_ip — IP OK" >> "$LOG"
        return 0
    fi

    echo "$(date): [$station] Camera not at $expected_ip, scanning ${subnet}0/24 for port 34567..." >> "$LOG"
    # Re-IP only a camera that is NOT at any *configured* station's address, and
    # only when there is exactly ONE such candidate. On a station with a dead
    # camera the old first-hit logic grabbed a healthy sibling camera and
    # reprogrammed it (Roit 2026-07-05: RO000E/.12 -> .11, then RO000G/.14 ->
    # .12 in a chain). ALL_CAM_IPS is populated by the main loop.
    local candidates c found_ip=""
    candidates=$(nmap -p 34567 "${subnet}0/24" --open -oG - 2>/dev/null \
        | grep '34567/open' | awk '{print $2}')
    for c in $candidates; do
        case " $ALL_CAM_IPS " in *" $c "*) continue ;; esac
        if [ -n "$found_ip" ]; then
            echo "$(date): [$station] Multiple unconfigured cameras on subnet ($found_ip, $c) — ambiguous, not enforcing" >> "$LOG"
            return 1
        fi
        found_ip=$c
    done

    if [ -z "$found_ip" ]; then
        echo "$(date): [$station] No unconfigured camera found on subnet — cannot enforce IP" >> "$LOG"
        return 1
    fi

    echo "$(date): [$station] Found camera at $found_ip, changing to $expected_ip..." >> "$LOG"

    local expected_hex
    # XMEye NetCommon renders IPs as a LITTLE-ENDIAN dword in hex:
    # 192.168.1.12 -> 0x0C01A8C0. A previous version emitted the bytes in
    # network order (0xC0A8010C), which reprogrammed cameras onto garbage
    # subnets like 12.1.168.192 (Roit incident, 2026-07-05).
    expected_hex=$("$VENV_PYTHON" -c "
import socket, struct
print('0x%08X' % struct.unpack('<I', socket.inet_aton('$expected_ip'))[0])
")

    "$VENV_PYTHON" - "$found_ip" "$expected_ip" "$expected_hex" "$station" >> "$LOG" 2>&1 <<'IPEOF'
import sys, time, os, importlib.util
# Prefer the vendored module shipped with the bundle: deterministic across
# stations and immune to a wrong PyPI "dvrip" package shadowing python-dvr in
# user site-packages (found live on gmnro07). Fall back to the venv package,
# then to a flat dvrip.py anywhere in the venv.
_vendored = os.path.expanduser("~/rovimen_scripts/dvrip_vendored.py")
if os.path.isfile(_vendored):
    _spec = importlib.util.spec_from_file_location("dvrip_vendored", _vendored)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    DVRIPCam = _mod.DVRIPCam
else:
    try:
        from dvrip import DVRIPCam
    except ImportError:
        import pathlib
        for p in sorted(pathlib.Path(sys.prefix, "lib").rglob("dvrip.py")):
            if p.parent.name != "dvrip":
                spec = importlib.util.spec_from_file_location("dvrip_flat", str(p))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                DVRIPCam = mod.DVRIPCam
                break
        else:
            raise ImportError("DVRIPCam not found")

found_ip, target_ip, target_hex, station = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
cam = DVRIPCam(found_ip)
if not cam.login():
    print(f"[{station}] Login to {found_ip} failed")
    sys.exit(1)
net = cam.get_info("NetWork.NetCommon")
if net is None:
    print(f"[{station}] Could not read NetCommon from {found_ip}")
    cam.close()
    sys.exit(1)
old_ip = net.get("HostIP", "unknown")
net["HostIP"] = target_hex
cam.set_info("NetWork.NetCommon", net)
cam.close()
print(f"[{station}] Changed IP from {old_ip} ({found_ip}) to {target_hex} ({target_ip})")
IPEOF

    sleep 5
    if timeout 2 bash -c "echo >/dev/tcp/$expected_ip/34567" 2>/dev/null; then
        echo "$(date): [$station] IP enforcement successful — camera now at $expected_ip" >> "$LOG"
        return 0
    else
        echo "$(date): [$station] ERROR: IP enforcement failed — camera not responding at $expected_ip" >> "$LOG"
        return 1
    fi
}

# ── HDIPC / Heroseen HTTP API ────────────────────────────────────────────────
# Endpoints and payload shapes are taken from the working per-station script
# this replaces (gmn0008: /action/getVencConf + /action/setVencConf) and from
# docs/camera_firmware_heroseen.md (/action/getRebootConf + /action/setRebootConf,
# fleet-applied 2026-07-06). Nothing here is guessed.

HDIPC_LAST_REPLY=""

hdipc_get() {
    local ip=$1 cred=$2 endpoint=$3
    timeout 8 curl -s -u "$cred" "http://$ip/action/$endpoint" 2>/dev/null
}

# POST a JSON body. rc 0 = firmware acknowledged ("code":0), rc 2 = it replied
# something else (logged, then left for the next run's read to arbitrate), rc 1
# = transport failure. The reply is kept in HDIPC_LAST_REPLY for the log.
hdipc_post() {
    local ip=$1 cred=$2 endpoint=$3 payload=$4
    HDIPC_LAST_REPLY=$(timeout 15 curl -s -X POST -u "$cred" "http://$ip/action/$endpoint" \
        -H "Content-Type: application/json" -d "$payload" 2>/dev/null) || return 1
    [ -z "$HDIPC_LAST_REPLY" ] && return 1
    case "$HDIPC_LAST_REPLY" in
        *'"code":0'*) return 0 ;;
    esac
    return 2
}

# Print the values of the requested JSON keys read from stdin, space separated,
# "?" for anything missing or unparseable. One interpreter spawn per read
# instead of one per field.
hdipc_fields() {
    "$VENV_PYTHON" -c '
import json, sys
try:
    doc = json.loads(sys.stdin.read())
    if not isinstance(doc, dict):
        doc = {}
except Exception:
    doc = {}
print(" ".join(str(doc.get(k, "?")) for k in sys.argv[1:]))
' "$@"
}

# Every argument a non-negative integer? Guards the printf-built JSON payloads
# against a "?" (missing field) or an HTML error page being spliced into them.
hdipc_all_numeric() {
    local v
    for v in "$@"; do
        case "$v" in
            ''|*[!0-9]*) return 1 ;;
        esac
    done
    return 0
}

# curl exits 0 on a 404 page too, so readiness means getVencConf actually
# returned the encoder document.
hdipc_wait_for_camera() {
    local ip=$1 cred=$2
    local attempts=0
    while [ $attempts -lt "$WAIT_ATTEMPTS" ]; do
        if hdipc_get "$ip" "$cred" getVencConf | grep -q '"pic_width"'; then
            return 0
        fi
        sleep 5
        attempts=$((attempts + 1))
    done
    return 1
}

hdipc_fix_camera() {
    local ip=$1 station=$2 cred=$3
    local t_w=$4 t_h=$5 t_fps=$6 t_br=$7 t_rc=$8 t_gop=$9 t_hour=${10}

    echo "$(date): [$station] Fixing $ip (HDIPC HTTP API)" >> "$LOG"

    hdipc_wait_for_camera "$ip" "$cred" || {
        # rc=2 = unreachable/skipped, same contract as the XMEye path
        echo "$(date): [$station] $ip HTTP API not reachable after 5 min, skipping" >> "$LOG"
        return 2
    }
    sleep 5   # the API answers before the firmware has finished initialising

    local changes=()

    # --- Encoder: resolution / fps / bitrate / rc_mode / gop ---
    local cur c_ch c_w c_h c_fps c_br c_rc c_gop c_et c_ep
    cur=$(hdipc_get "$ip" "$cred" getVencConf | hdipc_fields \
        channel pic_width pic_height frame_rate bitrate rc_mode gop encode_type encode_profile)
    read -r c_ch c_w c_h c_fps c_br c_rc c_gop c_et c_ep <<< "$cur"
    if ! hdipc_all_numeric "$c_ch" "$c_w" "$c_h" "$c_fps" "$c_br" "$c_rc" "$c_gop" "$c_et" "$c_ep"; then
        echo "$(date): [$station] $ip: ERROR getVencConf unreadable (fields: $cur)" >> "$LOG"
        return 1
    fi

    if [ "$c_w" != "$t_w" ] || [ "$c_h" != "$t_h" ] || [ "$c_fps" != "$t_fps" ] \
       || [ "$c_br" != "$t_br" ] || [ "$c_rc" != "$t_rc" ] || [ "$c_gop" != "$t_gop" ]; then
        # Write ONLY on real drift: a setVencConf write restarts the camera
        # (~15-20 s of lost frames, the firmware's own web UI warns about it), so
        # an unconditional daily POST would punch a nightly hole for nothing.
        # encode_type / encode_profile / channel are echoed back as read rather
        # than hardcoded, so this never silently re-codecs a camera.
        local payload
        payload=$(printf '{"channel":%s,"pic_width":%s,"pic_height":%s,"frame_rate":%s,"gop":%s,"bitrate":%s,"encode_type":%s,"encode_profile":%s,"rc_mode":%s}' \
            "$c_ch" "$t_w" "$t_h" "$t_fps" "$t_gop" "$t_br" "$c_et" "$c_ep" "$t_rc")
        hdipc_post "$ip" "$cred" setVencConf "$payload"
        case $? in
            0) changes+=("set Encode ${t_w}x${t_h}/${t_fps}fps/${t_br}k/rc${t_rc}/gop${t_gop} (was ${c_w}x${c_h}/${c_fps}fps/${c_br}k/rc${c_rc}/gop${c_gop}; camera restarts)") ;;
            2) changes+=("WARN setVencConf unexpected reply: $(printf '%.120s' "$HDIPC_LAST_REPLY")") ;;
            *) echo "$(date): [$station] $ip: ERROR setVencConf POST failed (transport)" >> "$LOG"
               return 1 ;;
        esac
        # Deliberately no read-back: the encoder write reboots the camera, so an
        # immediate re-read would time out. The next run's compare is the check.
        sleep 2
    else
        changes+=("Encode OK (${c_w}x${c_h}/${c_fps}fps/${c_br}k/rc${c_rc}/gop${c_gop})")
    fi

    # --- AutoReboot schedule (mode 2 = Everyday) ---
    # Same reasoning as the XMEye AutoMaintain block: the reboot fires in CAMERA
    # time and must land in daylight year-round. A POST failure here is a WARN,
    # not a station failure — the encoder is the contract.
    local rcur r_mode r_hour
    rcur=$(hdipc_get "$ip" "$cred" getRebootConf | hdipc_fields mode hour)
    read -r r_mode r_hour <<< "$rcur"
    if hdipc_all_numeric "$r_mode" "$r_hour"; then
        if [ "$r_mode" != "2" ] || [ "$r_hour" != "$t_hour" ]; then
            hdipc_post "$ip" "$cred" setRebootConf \
                "$(printf '{"mode":2,"day_week":0,"hour":%s,"minute":0}' "$t_hour")"
            case $? in
                0) changes+=("set AutoReboot=Everyday/${t_hour}:00 (was mode=$r_mode hour=$r_hour)") ;;
                2) changes+=("WARN setRebootConf unexpected reply: $(printf '%.120s' "$HDIPC_LAST_REPLY")") ;;
                *) changes+=("WARN setRebootConf POST failed (transport)") ;;
            esac
        else
            changes+=("AutoReboot OK (Everyday/${r_hour}:00)")
        fi
    else
        changes+=("WARN getRebootConf unreadable (fields: $rcur)")
    fi

    echo "$(date): [$station] $ip: ${changes[*]}" >> "$LOG"
    echo "$(date): [$station] done (rc=0)" >> "$LOG"
    return 0
}

# Which config surface does this camera speak? The config key wins, so a camera
# that is merely offline at 15:30 is never reclassified by a failed probe. The
# probe is the fallback for stations whose config predates the key.
detect_camera_api() {
    local ip=$1 configured=$2 cred=$3
    case "$configured" in
        xmeye|hdipc|openipc|none) echo "$configured"; return 0 ;;
    esac
    if timeout 2 bash -c "echo >/dev/tcp/$ip/34567" 2>/dev/null; then
        echo "xmeye"
        return 0
    fi
    if hdipc_get "$ip" "$cred" getVencConf | grep -q '"pic_width"'; then
        echo "hdipc"
        return 0
    fi
    # Unreachable on both surfaces — keep the historical XMEye behaviour
    # (wait_for_camera, then a logged skip) rather than guessing a family.
    echo "xmeye"
}

fix_camera() {
    local ip=$1
    local station=$2
    # Encoder target: fleet default unless camera_venc overrode it (see header).
    local t_res=$3 t_rc=$4 t_br=$5 t_q=$6
    # AutoReboot schedule: camera_reboot_day / camera_reboot_hour (default Everyday/12).
    local r_hour=${7:-12} r_day=${8:-Everyday}
    echo "$(date): [$station] Fixing $ip" >> "$LOG"

    wait_for_camera "$ip" || {
        # rc=2 = unreachable/skipped (distinct from a real fix failure so
        # known-dead cameras don't page every day)
        echo "$(date): [$station] $ip not reachable after 5 min, skipping" >> "$LOG"
        return 2
    }
    sleep 10

    "$VENV_PYTHON" - "$ip" "$station" "$t_res" "$t_rc" "$t_br" "$t_q" "$r_hour" "$r_day" "$HOST_CLOCK_SYNCED" >> "$LOG" 2>&1 <<'PYEOF'
import sys, time, os, importlib.util
# Prefer the vendored module shipped with the bundle: deterministic across
# stations and immune to a wrong PyPI "dvrip" package shadowing python-dvr in
# user site-packages (found live on gmnro07). Fall back to the venv package,
# then to a flat dvrip.py anywhere in the venv.
_vendored = os.path.expanduser("~/rovimen_scripts/dvrip_vendored.py")
if os.path.isfile(_vendored):
    _spec = importlib.util.spec_from_file_location("dvrip_vendored", _vendored)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    DVRIPCam = _mod.DVRIPCam
else:
    try:
        from dvrip import DVRIPCam
    except ImportError:
        import pathlib
        for p in sorted(pathlib.Path(sys.prefix, "lib").rglob("dvrip.py")):
            if p.parent.name != "dvrip":
                spec = importlib.util.spec_from_file_location("dvrip_flat", str(p))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                DVRIPCam = mod.DVRIPCam
                break
        else:
            raise ImportError("DVRIPCam not found")

ip, station = sys.argv[1], sys.argv[2]
# Encoder target, resolved from config by the caller: (Resolution,
# BitRateControl, BitRate kbps, Quality).
target = (sys.argv[3], sys.argv[4], int(sys.argv[5]), int(sys.argv[6]))
target_label = "{}/{}/{}/Q{}".format(*target)
reboot_hour = int(sys.argv[7]) if len(sys.argv) > 7 else 12
reboot_day = sys.argv[8] if len(sys.argv) > 8 else "Everyday"
host_clock_synced = len(sys.argv) > 9 and sys.argv[9] == "yes"

# Retry up to 5 times — cameras can accept TCP before DVRIP is ready,
# causing get_info to return None or throw on the first attempt.
for attempt in range(5):
    try:
        cam = DVRIPCam(ip)
        cam.login()

        # Read ALL data first, before any writes.
        # set_info calls can cause the camera to drop the DVRIP connection,
        # so we collect everything we need upfront in a single session.
        enc = cam.get_info("Simplify.Encode")
        cam_cfg = cam.get_info("Camera")

        if enc is None or cam_cfg is None:
            raise RuntimeError("get_info returned None (camera not ready)")

        break  # success
    except Exception as e:
        try:
            cam.close()
        except Exception:
            pass
        if attempt < 4:
            print(f"[{station}] {ip}: attempt {attempt+1} failed ({e}), retrying in 30s")
            time.sleep(30)
        else:
            print(f"[{station}] {ip}: ERROR after 5 attempts: {e}")
            sys.exit(1)

changes = []

# --- Encoder: Resolution, CBR, BitRate, Quality ---
# Granular sets on Resolution drop the DVRIP session mid-call (tested), so any
# encoder drift is corrected as a single bulk Simplify.Encode write, then we
# reconnect before touching Camera.Param fields.
v = enc[0]["MainFormat"]["Video"]
before = (v.get("Resolution"), v.get("BitRateControl"), v.get("BitRate"), v.get("Quality"))

# Sub-stream (ExtraFormat) must stay OFF. RMS and color_capture both pull the MAIN
# stream (stream=0); the sub-stream is unused, and on these cheap cameras a 2nd live
# encoder steals SoC/encoder budget from the main stream -> main-stream frame drops.
# Folded into the same bulk Simplify.Encode write so it costs no extra stream bounce.
extra = enc[0].get("ExtraFormat")
substream_on = bool(extra) and extra.get("VideoEnable") not in (0, False, None)
if substream_on:
    extra["VideoEnable"] = False

if before != target or substream_on:
    v["Resolution"], v["BitRateControl"], v["BitRate"], v["Quality"] = target
    cam.set_info("Simplify.Encode", enc)
    if before != target:
        changes.append(f"set Encode {target_label} (was {before})")
    if substream_on:
        changes.append("disabled ExtraFormat sub-stream")
    try:
        cam.close()
    except Exception:
        pass
    time.sleep(3)
    for i in range(12):
        try:
            cam = DVRIPCam(ip)
            cam.login()
            cam_cfg = cam.get_info("Camera")
            if cam_cfg is None:
                raise RuntimeError("Camera get_info None after Encode write")
            break
        except Exception as e:
            if i == 11:
                print(f"[{station}] {ip}: ERROR reconnect after Encode write: {e}")
                sys.exit(1)
            time.sleep(5)
else:
    changes.append(f"Encode OK ({target_label}, sub-stream off)")

# --- DayNightColor, IrcutSwap, EsShutter, WhiteBalance, DncThr ---
params = cam_cfg.get("Param", [{}])
p = params[0] if params else {}
if p.get("DayNightColor") != "0x00000001":
    cam.set_info("Camera.Param.[0].DayNightColor", "0x00000001")
    changes.append("set DayNightColor=0x00000001")
else:
    changes.append("DayNightColor OK")
if p.get("IrcutSwap") != 1:
    cam.set_info("Camera.Param.[0].IrcutSwap", 1)
    changes.append("set IrcutSwap=1")
else:
    changes.append("IrcutSwap OK")
if p.get("EsShutter") != "0x00000000":
    cam.set_info("Camera.Param.[0].EsShutter", "0x00000000")
    changes.append("set EsShutter=0x0")
else:
    changes.append("EsShutter OK")
if p.get("WhiteBalance") != "0x00000002":
    cam.set_info("Camera.Param.[0].WhiteBalance", "0x00000002")
    changes.append("set WhiteBalance=0x2")
else:
    changes.append("WhiteBalance OK")
if p.get("DncThr") != 50:
    cam.set_info("Camera.Param.[0].DncThr", 50)
    changes.append("set DncThr=50")
else:
    changes.append("DncThr OK")

# --- Exposure / Gain / ElecLevel ---
# Without these the camera sits in a daylight exposure (MostTime ~1ms, AutoGain
# off, Gain 0) and night frames are black. Lock the shutter to a full 40ms frame
# (1/25s) and let auto-gain ride to Gain=60. These revert on camera power-cycle,
# hence they belong here in the boot fixup alongside EsShutter.
if p.get("ElecLevel") != 40:
    cam.set_info("Camera.Param.[0].ElecLevel", 40)
    changes.append("set ElecLevel=40")
else:
    changes.append("ElecLevel OK")
if p.get("AeSensitivity") != 1:
    cam.set_info("Camera.Param.[0].AeSensitivity", 1)
    changes.append("set AeSensitivity=1")
else:
    changes.append("AeSensitivity OK")
exp = p.get("ExposureParam", {})
exp_target = {"LeastTime": "0x00009C40", "Level": 0, "MostTime": "0x00009C40"}
if (exp.get("LeastTime"), exp.get("MostTime")) != (exp_target["LeastTime"], exp_target["MostTime"]):
    cam.set_info("Camera.Param.[0].ExposureParam", exp_target)
    changes.append("set ExposureParam=40ms")
else:
    changes.append("ExposureParam OK")
gain = p.get("GainParam", {})
if gain.get("AutoGain") != 1 or gain.get("Gain") != 60:
    cam.set_info("Camera.Param.[0].GainParam", {"AutoGain": 1, "Gain": 60})
    changes.append("set GainParam=auto/60")
else:
    changes.append("GainParam OK")

# --- ClearFog ---
clearfog = cam_cfg.get("ClearFog", [{}])
cf = clearfog[0] if clearfog else {}
if cf.get("enable") not in (False, 0) or cf.get("level") != 30:
    cam.set_info("Camera.ClearFog.[0].enable", False)
    cam.set_info("Camera.ClearFog.[0].level", 30)
    changes.append("set ClearFog=off/30")
else:
    changes.append("ClearFog OK")

# --- AutoReboot schedule + camera clock (see header) ---
# Order matters: fix the schedule BEFORE the clock, so the clock jump cannot
# land on a stale reboot slot and power-cycle the camera under us.
from datetime import datetime
am = cam.get_info("General.AutoMaintain")
if isinstance(am, dict) and "AutoRebootDay" in am:
    if am.get("AutoRebootDay") != reboot_day or am.get("AutoRebootHour") != reboot_hour:
        before_am = f"{am.get('AutoRebootDay')}/{am.get('AutoRebootHour')}"
        am["AutoRebootDay"] = reboot_day
        am["AutoRebootHour"] = reboot_hour
        cam.set_info("General.AutoMaintain", am)
        changes.append(f"set AutoReboot={reboot_day}/{reboot_hour} (was {before_am})")
    else:
        changes.append("AutoReboot OK")
else:
    changes.append(f"WARN AutoMaintain unreadable: {am!r}")

# Camera NTP must be off: with it on, the camera ignores set_time and re-syncs
# every UpdatePeriod minutes in its own TimeZone (factory 13 = GMT+8, which the
# firmware will not change to GMT+0, Ret 606). Both gmnro21 (Vârteșcoiu)
# cameras came that way and sat 8 h ahead, so their 12:00 AutoReboot fired at
# 04:00 UTC.
ntp = cam.get_info("NetWork.NetNTP")
if isinstance(ntp, dict) and ntp.get("Enable") is True:
    cam.set_info("NetWork.NetNTP.Enable", False)
    changes.append(f"turned camera NTP off (TimeZone {ntp.get('TimeZone')})")

# The camera clock is set only from a synchronised host clock (see
# host_clock_synced): a wrong host time would move the AutoReboot into the night.
cam_now = None
if not host_clock_synced:
    changes.append("clock not synchronised, camera time left alone")
else:
    try:
        cam_now = cam.get_time()
    except Exception as e:
        changes.append(f"WARN clock read failed: {e}")
if cam_now is not None:
    offset = (cam_now - datetime.utcnow()).total_seconds()
    if abs(offset) > 5:
        cam.set_time(datetime.utcnow())
        changes.append(f"synced clock to UTC (was {offset:+.0f}s off)")
    else:
        changes.append(f"clock OK ({offset:+.0f}s)")

try:
    cam.close()
except Exception:
    pass

print(f"[{station}] {ip}: {', '.join(changes)}")
PYEOF

    local rc=$?
    echo "$(date): [$station] done (rc=$rc)" >> "$LOG"
    return $rc
}

echo "$(date): Starting fix_cam_encoding${ONLY:+ --only $ONLY} (config: $CONFIG)" >> "$LOG"

read -r HOST_CLOCK_SYNCED HOST_CLOCK_SOURCE <<< "$(host_clock_synced)"
if [ "$HOST_CLOCK_SYNCED" != "yes" ]; then
    HOST_CLOCK_SYNCED="no"
    echo "$(date): host clock not synchronised (${HOST_CLOCK_SOURCE:-unknown}): clock not synchronised, camera time left alone" >> "$LOG"
fi

if [ ! -f "$CONFIG" ]; then
    echo "$(date): ERROR: config.json not found at $CONFIG" >> "$LOG"
    exit 1
fi

# Extract station IDs, IPs and per-camera enforcement targets from config.json.
# RTSP format: rtsp://admin:@<IP>:554/... (credentials optional — the old
# '@'-split parser silently skipped credential-less URLs, most cameras on
# one station). Loopback URLs point at the local RTSP relay, not a camera.
#
# One row per camera:
#   ip station api cred width height fps bitrate rc_mode gop hour \
#   resolution bitrate_control bitrate quality
# Columns 5-10 are the HDIPC encoder target and 12-15 the XMEye one; each
# family reads only its own columns (bitrate appears in both, in kbps, and is
# resolved separately so neither family's default constrains the other).
# Optional config keys, per camera under stations.<CODE> or station-wide at the
# top level: "camera_api" ("xmeye" | "hdipc" | "auto" | "openipc" | "none"), "camera_venc" (see
# CONFIG OVERRIDES in the header), "camera_reboot_hour" and, XMEye only,
# "camera_reboot_day" (REBOOT_DAYS; gmnro20 Brăila ran "Sunday" from 2026-10-06
# and runs "Never" since 2026-11-23: the firmware's own scheduled reboot is what
# left its cameras lossy, see known failure mode 65; RMS's sunset mode switch
# still reboots each camera daily).
CAM_LIST=$("$VENV_PYTHON" -c "
import json, re, sys
with open('$CONFIG') as f:
    cfg = json.load(f)

# HDIPC fleet standard: 720p / 25 fps / 8192 kbps CBR / gop 12, applied
# fleet-wide 2026-07-09 (docs/camera_firmware_heroseen.md).
FLEET_VENC = {'width': 1280, 'height': 720, 'fps': 25, 'bitrate': 8192, 'rc_mode': 1, 'gop': 12}
# XMEye fleet standard: 720P / CBR / 8192 kbps / Quality 6 (see header).
XMEYE_VENC = {'resolution': '720P', 'bitrate_control': 'CBR', 'bitrate': 8192, 'quality': 6}
# camera_venc field name -> XMEYE_VENC key, for the fields this family reads.
XMEYE_ALIASES = {'rc': 'bitrate_control'}
DEFAULT_AUTH = 'admin:123456'
# XMEye General.AutoMaintain AutoRebootDay values.
REBOOT_DAYS = ('Everyday', 'Never', 'Sunday', 'Monday', 'Tuesday', 'Wednesday',
               'Thursday', 'Friday', 'Saturday')


def xmeye_venc(raw, station):
    '''XMEye encoder target: fleet default with the camera_venc fields applied.'''
    out = dict(XMEYE_VENC)
    for field, value in raw.items():
        key = XMEYE_ALIASES.get(field, field)
        if key not in out:
            continue
        if isinstance(XMEYE_VENC[key], int):
            out[key] = int(value)
        else:
            # The row is space separated, so a value with whitespace (or any
            # shell-surprising character) would shift every later column. Keep
            # the fleet default rather than emit a corrupt row.
            token = str(value).strip()
            if not re.fullmatch(r'[A-Za-z0-9._+-]+', token):
                print(f'ERROR: ignoring camera_venc.{field}={value!r} for '
                      f'{station}: not a plain token', file=sys.stderr)
                continue
            out[key] = token
    return out

for station, info in cfg['stations'].items():
    rtsp = info.get('camera_rtsp') or ''
    m = re.search(r'rtsp://(?:(?P<cred>[^@/\s]*)@)?(?P<ip>\d+\.\d+\.\d+\.\d+)', rtsp)
    if m and not m.group('ip').startswith('127.'):
        api = str(info.get('camera_api') or cfg.get('camera_api') or 'auto').lower()
        # HTTP basic auth for the HDIPC path: the RTSP credentials when the URL
        # carries a usable pair, else the firmware default.
        # Change the cameras' default password; this fallback exists for old firmware.
        cred = (m.group('cred') or '').strip()
        if cred.count(':') != 1 or cred.endswith(':') or ' ' in cred:
            cred = DEFAULT_AUTH
        # Per-camera override wins whole over the station-wide one, both over
        # the fleet defaults. Read once, applied to whichever family runs.
        raw_venc = info.get('camera_venc') or cfg.get('camera_venc') or {}
        venc = dict(FLEET_VENC)
        venc.update({k: int(v) for k, v in raw_venc.items() if k in FLEET_VENC})
        xvenc = xmeye_venc(raw_venc, station)
        hour = int(info.get('camera_reboot_hour', cfg.get('camera_reboot_hour', 12)))
        day = str(info.get('camera_reboot_day', cfg.get('camera_reboot_day', 'Everyday')))
        if day not in REBOOT_DAYS:
            print(f'ERROR: ignoring camera_reboot_day={day!r} for {station}: '
                  f'not one of {REBOOT_DAYS}', file=sys.stderr)
            day = 'Everyday'
        print(m.group('ip'), station, api, cred, venc['width'], venc['height'],
              venc['fps'], venc['bitrate'], venc['rc_mode'], venc['gop'], hour,
              xvenc['resolution'], xvenc['bitrate_control'], xvenc['bitrate'],
              xvenc['quality'], day)
    elif rtsp:
        print(f'ERROR: could not parse IP for {station}: {rtsp}', file=sys.stderr)
    else:
        print(f'NOTE: {station} has no camera_rtsp, skipping', file=sys.stderr)
")

# A config that parses to no cameras at all (unreadable JSON, no camera_rtsp
# anywhere) used to fall straight through to "All done (ok: none; skipped:
# none)" — the same silent success as a station with nothing to do.
if [ -z "${CAM_LIST//[[:space:]]/}" ]; then
    echo "$(date): ERROR: no camera rows parsed from $CONFIG — nothing was enforced" >> "$LOG"
    exit 1
fi

# Every configured camera IP — enforce_camera_ip must never re-IP a camera
# that legitimately sits at another station's address.
ALL_CAM_IPS=$(printf '%s\n' "$CAM_LIST" | awk '{print $1}' | tr '\n' ' ')

OK_STATIONS=()
SKIPPED_STATIONS=()
FAILED_STATIONS=()
UNMANAGED_STATIONS=()   # camera_api openipc/none: deliberately left alone
CAM_DETAIL=()   # station:api:state rows for the status file
while IFS=' ' read -r ip station api cred t_w t_h t_fps t_br t_rc t_gop t_hour \
                      x_res x_rc x_br x_q r_day; do
    [ -z "$ip" ] && continue
    [ -n "$ONLY" ] && [ "$station" != "$ONLY" ] && continue
    api=$(detect_camera_api "$ip" "$api" "$cred")
    if [ "$api" = "openipc" ] || [ "$api" = "none" ]; then
        echo "$(date): [$station] $ip: camera_api=$api, not managed by this script" >> "$LOG"
        if [ -n "$ONLY" ]; then
            echo "$(date): --only $ONLY done (rc=0, unmanaged)" >> "$LOG"
            exit 0
        fi
        UNMANAGED_STATIONS+=("$station")
        CAM_DETAIL+=("$station:$api:unmanaged")
        continue
    fi
    if [ "$api" = "hdipc" ]; then
        # No IP enforcement for this family: the 34567 nmap scan cannot see an
        # HDIPC camera at all, and a re-IP over the HTTP API is unverified on
        # this firmware — a misplaced camera is reported as skipped, never
        # reprogrammed. (The 2026-07-05 Roit chain-reprogramming incident is why
        # this path stays read-only until it can be tested.)
        hdipc_fix_camera "$ip" "$station" "$cred" \
            "$t_w" "$t_h" "$t_fps" "$t_br" "$t_rc" "$t_gop" "$t_hour"
    else
        [ -z "$ONLY" ] && enforce_camera_ip "$ip" "$station"
        fix_camera "$ip" "$station" "$x_res" "$x_rc" "$x_br" "$x_q" "$t_hour" "$r_day"
    fi
    rc=$?
    if [ -n "$ONLY" ]; then
        echo "$(date): --only $ONLY done (rc=$rc)" >> "$LOG"
        exit "$rc"
    fi
    case $rc in
        0) OK_STATIONS+=("$station"); CAM_DETAIL+=("$station:$api:ok") ;;
        2) SKIPPED_STATIONS+=("$station"); CAM_DETAIL+=("$station:$api:skipped") ;;
        *) FAILED_STATIONS+=("$station"); CAM_DETAIL+=("$station:$api:failed") ;;
    esac
    sleep 30   # give the camera a breather between stations
done <<< "$CAM_LIST"

if [ -n "$ONLY" ]; then
    echo "$(date): --only $ONLY: no camera row with that code in $CONFIG" >> "$LOG"
    exit 1
fi

# Machine-readable outcome for station_api / remote debugging.
"$VENV_PYTHON" - "$STATUS_FILE" "${OK_STATIONS[*]-}" "${SKIPPED_STATIONS[*]-}" "${FAILED_STATIONS[*]-}" "${CAM_DETAIL[*]-}" "$HOST_CLOCK_SYNCED" "${HOST_CLOCK_SOURCE:-unknown}" <<'STATEOF'
import json, sys, time
path, ok, skipped, failed = sys.argv[1], sys.argv[2].split(), sys.argv[3].split(), sys.argv[4].split()
# Per-camera family + state, so the dashboard can tell "enforced" from "the
# script ran and touched nothing" without parsing the log.
cameras = {}
for row in sys.argv[5].split():
    parts = row.split(":")
    if len(parts) == 3:
        cameras[parts[0]] = {"api": parts[1], "state": parts[2]}
total = len(ok) + len(skipped) + len(failed)
with open(path, "w") as f:
    json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "ok": ok, "skipped": skipped, "failed": failed,
               "cameras": cameras,
               # False: the host clock was not NTP-synchronised, so no XMEye
               # camera's clock was set this run (see host_clock_synced).
               "host_clock_synced": sys.argv[6] == "yes",
               "host_clock_source": sys.argv[7],
               # True when the run enforced nothing at all on any camera — the
               # state that hid the HDIPC gap for weeks behind "All done".
               # Unmanaged cameras (camera_api openipc/none) are not counted.
               "none_enforced": total > 0 and not ok}, f, indent=1)
STATEOF

if [ ${#FAILED_STATIONS[@]} -gt 0 ]; then
    echo "$(date): DONE WITH FAILURES: ${FAILED_STATIONS[*]} (ok: ${OK_STATIONS[*]-none}; skipped: ${SKIPPED_STATIONS[*]-none})" >> "$LOG"
    exit 1
fi
if [ ${#OK_STATIONS[@]} -eq 0 ] && [ ${#SKIPPED_STATIONS[@]} -gt 0 ]; then
    # Exit stays 0 — known-dead cameras must not page daily — but the line has to
    # read as a problem, not as success. "All done (ok: none)" is how a station
    # whose settings had not been checked in weeks still looked healthy.
    echo "$(date): NOTHING ENFORCED: no camera was reachable, settings unchecked on this station (skipped: ${SKIPPED_STATIONS[*]})" >> "$LOG"
    exit 0
fi
echo "$(date): All done (ok: ${OK_STATIONS[*]-none}; skipped: ${SKIPPED_STATIONS[*]-none}; unmanaged: ${UNMANAGED_STATIONS[*]-none})" >> "$LOG"
