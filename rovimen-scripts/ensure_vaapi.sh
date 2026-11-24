#!/usr/bin/env bash
# ensure_vaapi.sh — installation ground rule: a station with a usable GPU
# encodes on VAAPI.
#
# Idempotent. Runs as the station user (never as root) and uses sudo only for
# the two privileged steps, both of which have exact-argument NOPASSWD rules in
# /etc/sudoers.d/rovimen (see --print-sudoers):
#
#   1. apt-get install the VA driver packages for the GPU vendor, only when one
#      of them is missing (Intel: intel-media-va-driver + i965-va-driver; AMD:
#      mesa-va-drivers; plus vainfo for diagnosis).
#   2. usermod -aG render,video <user>, only when the user is not already in
#      render. The render node is root:render 0660, so without the group the
#      encoder silently falls back to libx264 (known_failure_modes.md §52).
#
# Then it VERIFIES with a real 2-frame h264_vaapi encode, run with the render
# group even when this shell predates the usermod (via sg). The libva driver is
# chosen empirically: the default first, then i965 on Intel (Haswell/Broadwell
# and older need i965; libva auto-picks iHD there and iHD fails to init).
#
# Callers:
#   updater.sh           — --non-interactive (sudo -n), every run; converges
#                          the existing fleet without an operator
#   operator by hand     — bash ~/rovimen_scripts/ensure_vaapi.sh
#   hardware_assessment  — --check (verify only, changes nothing)
#
# Last line of output is machine-readable:
#   VAAPI: PASS device=/dev/dri/renderD128 driver=default|i965 vendor=intel|amd|...
#   VAAPI: NOT-CAPABLE reason=...     (no GPU, or GPU without H.264 encode)
#   VAAPI: FAIL reason=...            (GPU can encode but this station cannot yet)
# Exit status: 0 for PASS and NOT-CAPABLE (both converged), 1 for FAIL.
# The same verdict is written to ~/rovimen_scripts/vaapi_status.json.
#
# Group membership only reaches NEW processes: system services pick it up at
# their next (re)start, cron jobs at their next run, and user units once the
# user's systemd manager restarts (in practice the next reboot).

set -uo pipefail

STATION_USER="$(id -un)"
# Overridable for tests only.
DRI_DIR="${ROVIMEN_DRI_DIR:-/dev/dri}"
DRM_SYSFS="${ROVIMEN_DRM_SYSFS:-/sys/class/drm}"
STATUS_FILE="${HOME}/rovimen_scripts/vaapi_status.json"
MODE="ensure"          # ensure | check
SUDO=(sudo)

INTEL_PKGS=(intel-media-va-driver i965-va-driver vainfo)
AMD_PKGS=(mesa-va-drivers vainfo)
APT_OPTS=(-o DPkg::Lock::Timeout=120 install -y --no-install-recommends)

log() { echo "[vaapi] $*"; }

_bin() {
    # Absolute path of a binary, also looking in sbin dirs a user PATH may lack.
    local name="$1" d
    if command -v "$name" >/dev/null 2>&1; then command -v "$name"; return; fi
    for d in /usr/sbin /sbin /usr/bin /bin; do
        [ -x "$d/$name" ] && { echo "$d/$name"; return; }
    done
    echo "/usr/sbin/$name"
}

APT_GET="$(_bin apt-get)"
USERMOD="$(_bin usermod)"

_sudoers_escape() {
    # sudoers requires ',' ':' '=' and '\' inside command arguments to be
    # backslash-escaped; sudo matches against the unescaped command line.
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//,/\\,}"
    s="${s//:/\\:}"
    s="${s//=/\\=}"
    printf '%s' "$s"
}

print_sudoers() {
    # Exact-argument rules (sudo >= 1.9.10 rejects wildcards in arguments).
    local u="$1"
    echo "# VAAPI ground rule (ensure_vaapi.sh): VA driver packages + render/video groups"
    echo "$u ALL=(root) NOPASSWD: $APT_GET $(_sudoers_escape "${APT_OPTS[*]} ${INTEL_PKGS[*]}")"
    echo "$u ALL=(root) NOPASSWD: $APT_GET $(_sudoers_escape "${APT_OPTS[*]} ${AMD_PKGS[*]}")"
    echo "$u ALL=(root) NOPASSWD: $USERMOD $(_sudoers_escape "-aG render,video $u")"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --non-interactive) SUDO=(sudo -n) ;;
        --check)           MODE="check" ;;
        --status-file)     STATUS_FILE="$2"; shift ;;
        --print-sudoers)   print_sudoers "${2:-$STATION_USER}"; exit 0 ;;
        -h|--help)         sed -n '2,37p' "$0"; exit 0 ;;
        *) log "unknown argument: $1"; exit 2 ;;
    esac
    shift
done

if [ "$(id -u)" -eq 0 ]; then
    log "run as the station user, not root (the render group is for that user)"
    exit 2
fi

# ── Detection ────────────────────────────────────────────────────────────────
DEVICE=""
VENDOR="none"
for node in "$DRI_DIR"/renderD*; do
    [ -e "$node" ] || continue
    DEVICE="$node"
    vid=$(cat "$DRM_SYSFS/$(basename "$node")/device/vendor" 2>/dev/null || echo "")
    case "$vid" in
        0x8086) VENDOR="intel" ;;
        0x1002) VENDOR="amd" ;;
        "")     VENDOR="unknown" ;;
        *)      VENDOR="other-$vid" ;;
    esac
    # Prefer an Intel/AMD node if a box has several.
    if [ "$VENDOR" = "intel" ] || [ "$VENDOR" = "amd" ]; then break; fi
done

in_render_db() { id -nG "$STATION_USER" 2>/dev/null | tr ' ' '\n' | grep -qx render; }

pkgs_missing() {
    local p
    for p in "$@"; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || return 0
    done
    return 1
}

# Run a command with access to the render node: directly when this process can
# already open it (group or seat ACL), else through `sg render` when the user
# was added to the group after this shell started.
gpu_run() {
    if [ -z "$DEVICE" ] || { [ -r "$DEVICE" ] && [ -w "$DEVICE" ]; }; then
        "$@"
    elif in_render_db && command -v sg >/dev/null 2>&1; then
        local cmd
        printf -v cmd '%q ' "$@"
        sg render -c "$cmd" </dev/null
    else
        "$@"
    fi
}

encode_test() {
    # $1: libva driver name, or "default" to let libva choose.
    local drv="$1" envs=()
    [ "$drv" != "default" ] && envs=(env "LIBVA_DRIVER_NAME=$drv")
    gpu_run "${envs[@]}" ffmpeg -hide_banner -loglevel error \
        -vaapi_device "$DEVICE" \
        -f lavfi -i 'testsrc=duration=0.08:size=1280x720:rate=25' \
        -vf 'format=nv12,hwupload' -c:v h264_vaapi -qp 20 \
        -f null - >/dev/null 2>&1
}

has_h264_encode_entrypoint() {
    # True when any libva driver reports an H.264 encode entrypoint.
    command -v vainfo >/dev/null 2>&1 || return 1
    local drv envs out
    for drv in default i965; do
        envs=()
        [ "$drv" != "default" ] && envs=(env "LIBVA_DRIVER_NAME=$drv")
        out=$(gpu_run "${envs[@]}" vainfo --display drm --device "$DEVICE" 2>/dev/null || true)
        echo "$out" | grep -E 'H264' | grep -q 'EncSlice' && return 0
    done
    return 1
}

write_status() {
    local verdict="$1" driver="$2" reason="$3"
    mkdir -p "$(dirname "$STATUS_FILE")" 2>/dev/null || return 0
    printf '{\n  "checked_at": "%s",\n  "verdict": "%s",\n  "device": "%s",\n  "vendor": "%s",\n  "driver": "%s",\n  "in_render_group": %s,\n  "reason": "%s"\n}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$verdict" "$DEVICE" "$VENDOR" "$driver" \
        "$(in_render_db && echo true || echo false)" "$reason" \
        > "${STATUS_FILE}.tmp" 2>/dev/null && mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
}

finish() {
    # finish VERDICT DRIVER REASON
    local verdict="$1" driver="$2" reason="$3"
    [ "$MODE" = "ensure" ] && write_status "$verdict" "$driver" "$reason"
    if [ "$verdict" = "PASS" ]; then
        echo "VAAPI: PASS device=$DEVICE driver=$driver vendor=$VENDOR"
        exit 0
    elif [ "$verdict" = "NOT-CAPABLE" ]; then
        echo "VAAPI: NOT-CAPABLE reason=$reason"
        exit 0
    elif [ "$verdict" = "PENDING" ]; then
        echo "VAAPI: PENDING reason=$reason"
        exit 0
    fi
    echo "VAAPI: FAIL reason=$reason"
    exit 1
}

if [ -z "$DEVICE" ]; then
    finish NOT-CAPABLE "" "no-render-node"
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
    finish FAIL "" "ffmpeg-not-installed"
fi
log "render node $DEVICE (vendor: $VENDOR)"

# ── Ensure (privileged, idempotent) ──────────────────────────────────────────
if [ "$MODE" = "ensure" ]; then
    pkgs=()
    case "$VENDOR" in
        intel) pkgs=("${INTEL_PKGS[@]}") ;;
        amd)   pkgs=("${AMD_PKGS[@]}") ;;
    esac
    if [ ${#pkgs[@]} -gt 0 ] && [ -x "$APT_GET" ] && command -v dpkg-query >/dev/null 2>&1; then
        if pkgs_missing "${pkgs[@]}"; then
            log "installing VA driver packages: ${pkgs[*]}"
            if "${SUDO[@]}" "$APT_GET" "${APT_OPTS[@]}" "${pkgs[@]}" >/dev/null 2>&1; then
                log "VA driver packages installed"
            else
                log "WARN: could not install ${pkgs[*]} (no sudo rule yet, apt lock, or offline)"
            fi
        else
            log "VA driver packages already installed"
        fi
    fi

    if getent group render >/dev/null 2>&1; then
        if in_render_db; then
            log "$STATION_USER already in render"
        elif "${SUDO[@]}" "$USERMOD" -aG render,video "$STATION_USER" >/dev/null 2>&1; then
            log "added $STATION_USER to render,video — services need a restart, cron picks it up at next run"
        else
            log "WARN: could not add $STATION_USER to render,video (no sudo rule yet?)"
        fi
    fi
fi

# ── Verify ───────────────────────────────────────────────────────────────────
if encode_test default; then
    finish PASS default ""
fi
if [ "$VENDOR" = "intel" ] && encode_test i965; then
    log "default libva driver failed, i965 works (pre-Gen9 Intel) — the encoder's auto driver selection retries i965"
    finish PASS i965 ""
fi

if ! { [ -r "$DEVICE" ] && [ -w "$DEVICE" ]; }; then
    if in_render_db; then
        # The user is in render in /etc/group, but this process started before
        # that (typically: usermod just ran above). Groups are fixed at login,
        # and the sg fallback cannot be relied on under cron, so no test in
        # this process can see the device. New processes (restarted services,
        # the next cron run) do; the next updater pass re-verifies.
        finish PENDING "" "render-group-granted-verify-next-pass"
    fi
    finish FAIL "" "no-access-to-$DEVICE-user-not-in-render"
fi
if has_h264_encode_entrypoint; then
    finish FAIL "" "driver-reports-h264-encode-but-test-encode-failed"
fi
finish NOT-CAPABLE "" "gpu-has-no-vaapi-h264-encode"
