#!/usr/bin/env bash
#
# setup_rtsp_relay.sh - install a loopback RTSP relay (MediaMTX) on a station.
#
# Why: on bandwidth-starved sites the camera ethernet links top out around
# ~10 Mbps. RMS detection and color_capture each open their own RTSP pull of
# the camera main stream (~8 Mbps CBR), so two simultaneous pulls (~16 Mbps)
# saturate the link and RMS drops frames. This relay pulls each camera ONCE and
# fans the stream out to both consumers over loopback (unmetered), so the
# camera link only ever carries a single stream.
#
#   Camera .210 --8Mbps--> 127.0.0.1:8554/cam1 --+--> rms-cam1   (loopback)
#                                                 +--> color_capture (loopback)
#
# MediaMTX does pure H.264 passthrough (no re-encode -> no CPU / quality cost)
# and reconnects automatically. On-demand semantics mean the camera is only
# pulled while RMS / color_capture are actually reading (zero daytime load).
#
# By default MediaMTX pulls each camera itself (source: + sourceOnDemand).
# --restamp publishes each path through an ffmpeg shim instead
# (-use_wallclock_as_timestamps 1 -c copy), which stamps frames with the host
# clock. On cameras that run slightly slow (~24.98 fps), pass-through leaves
# RMS time up to 2 s behind the host clock, with a 2 s jump every ~44 min, and
# GMN's Dropped Frame Rate reads ~19% (known failure mode 54). Restamp kept it
# at 0.0-0.4% on gmnro10 (Roit) and gmnro12 (Iași) from 07-06 to 09-27; they
# run it again since 2026-09-29, together with gmnro11 (Tărâța). The
# "280-2,700 dropped frames per 10 min" that made it opt-in on 2026-09-28 was
# RMS's raw BufferedCapture counter, which does not measure lost frames.
#
# Idempotent: safe to re-run. Reads camera URLs from config.json; the camera
# map and the repointing live in rtsp_relay.py.
#   --repoint-rms      point every RMS service's .config device line at the
#                      relay (backed up first) and restart that service. RMS
#                      gets rtsp://localhost:8554/<path>?camera=<IP>, so its
#                      DVRIP camera control (day/night mode switch) still
#                      reaches the camera; see rtsp_relay.py.
#   --repoint-capture  set stations.<CAM>.capture_rtsp to the relay and
#                      restart colour capture.
#   --user-unit        install the relay as a systemd user unit. Chosen
#                      automatically where sudo cannot install a system unit
#                      (gmnro07 (București), gmnde00 (Johannismühle)); needs
#                      linger so it starts at boot.
#
# Usage:
#   bash setup_rtsp_relay.sh [--config PATH] [--version vX.Y.Z]
#                            [--repoint-rms] [--repoint-capture] [--user-unit]
#                            [--no-start] [--restamp]
#
set -euo pipefail

CONFIG="${HOME}/rovimen_scripts/config.json"
SCRIPTS_DIR="${HOME}/rovimen_scripts"
VERSION="v1.19.2"
BIND="127.0.0.1:8554"
REPOINT_RMS=0
REPOINT_CAPTURE=0
USER_UNIT=0
NO_START=0
RESTAMP=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)      CONFIG="$2"; shift 2 ;;
        --version)     VERSION="$2"; shift 2 ;;
        --bind)        BIND="$2"; shift 2 ;;
        --repoint-rms) REPOINT_RMS=1; shift ;;
        --repoint-capture) REPOINT_CAPTURE=1; shift ;;
        --user-unit)   USER_UNIT=1; shift ;;
        --no-start)    NO_START=1; shift ;;
        --restamp)     RESTAMP=1; shift ;;
        --no-restamp)  RESTAMP=0; shift ;;   # the default; kept for old call sites
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ "$RESTAMP" -eq 1 ]] && ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg not found; --restamp needs it" >&2
    exit 1
fi

BIN_DIR="${SCRIPTS_DIR}/bin"
BIN="${BIN_DIR}/mediamtx"
YAML="${SCRIPTS_DIR}/mediamtx.yml"
RELAY_PY="${SCRIPTS_DIR}/rtsp_relay.py"
UNIT="rovimen-rtsp-relay.service"
SERVICE_DST="/etc/systemd/system/${UNIT}"
USER_SERVICE_DST="${HOME}/.config/systemd/user/${UNIT}"
ARCH_RAW="$(uname -m)"
case "$ARCH_RAW" in
    x86_64)  ARCH="amd64" ;;
    aarch64) ARCH="arm64" ;;
    armv7l)  ARCH="armv7" ;;
    *) echo "Unsupported arch: $ARCH_RAW" >&2; exit 1 ;;
esac

log() { echo "[setup_rtsp_relay] $*"; }

[[ -f "$CONFIG" ]] || { echo "Config not found: $CONFIG" >&2; exit 1; }
[[ -f "$RELAY_PY" ]] || { echo "rtsp_relay.py not found: $RELAY_PY" >&2; exit 1; }

# A system unit needs passwordless sudo for install + systemctl. gmnro07
# (București) and gmnde00 (Johannismühle) allow only systemctl without a
# password, so they get a user unit. (sudo -l <cmd> is no test: it also
# succeeds for a rule that needs a password.)
if [[ "$USER_UNIT" -eq 0 ]] && ! sudo -n true >/dev/null 2>&1; then
    log "sudo cannot install a system unit here; installing a user unit"
    USER_UNIT=1
fi
if [[ "$USER_UNIT" -eq 1 ]]; then
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    if systemctl is-active --quiet "$UNIT"; then
        echo "A system $UNIT is active; a user unit would clash on the port" >&2; exit 1
    fi
    [[ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" == "yes" ]] ||
        log "WARNING: linger is off, so the user unit will not start at boot (loginctl enable-linger)"
    sc() { systemctl --user "$@"; }
    unit_journal() { journalctl --user -u "$UNIT" -n 30 --no-pager; }
else
    sc() { sudo -n systemctl "$@"; }
    unit_journal() { journalctl -u "$UNIT" -n 30 --no-pager; }
fi

# --- 1. download MediaMTX binary (idempotent) -----------------------------
mkdir -p "$BIN_DIR"
if [[ -x "$BIN" ]] && "$BIN" --version 2>/dev/null | grep -q "$VERSION"; then
    log "MediaMTX $VERSION already installed at $BIN"
else
    url="https://github.com/bluenviron/mediamtx/releases/download/${VERSION}/mediamtx_${VERSION}_linux_${ARCH}.tar.gz"
    tmp="$(mktemp -d)"
    log "Downloading $url"
    curl -fsSL --retry 3 -o "$tmp/m.tgz" "$url"
    tar -xzf "$tmp/m.tgz" -C "$tmp" mediamtx
    install -m 0755 "$tmp/mediamtx" "$BIN"
    rm -rf "$tmp"
    log "Installed $("$BIN" --version 2>&1 | head -1) -> $BIN"
fi

# --- 2. generate mediamtx.yml from config.json ----------------------------
# One relay path per camera with a camera_rtsp (rtsp_relay.relay_paths).
log "Generating $YAML from $CONFIG"
restamp_arg=()
[[ "$RESTAMP" -eq 1 ]] && restamp_arg=(--restamp)
python3 "$RELAY_PY" yaml --config "$CONFIG" --bind "$BIND" --out "$YAML" "${restamp_arg[@]}"

# --- 3. install + (re)start the service -----------------------------------
tmp_unit="$(mktemp)"
cat > "$tmp_unit" <<UNIT_EOF
[Unit]
Description=ROVIMEN RTSP relay (MediaMTX) - single shared camera pull for RMS + color capture
Documentation=https://github.com/bluenviron/mediamtx
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# Loopback-only relay: pulls each camera main stream ONCE and fans it out to
# RMS detection + color capture over localhost. Generated by setup_rtsp_relay.sh.
ExecStart=${BIN} ${YAML}
Restart=always
RestartSec=5
MemoryMax=512M
UNIT_EOF
if [[ "$USER_UNIT" -eq 1 ]]; then
    printf '\n[Install]\nWantedBy=default.target\n' >> "$tmp_unit"
    mkdir -p "$(dirname "$USER_SERVICE_DST")"
    install -m 0644 "$tmp_unit" "$USER_SERVICE_DST"
else
    # User=/Group= and Nice= only apply to a system unit.
    sed -i "s#^Type=simple#Type=simple\nUser=$(id -un)\nGroup=$(id -gn)\n\# Detection reads through this relay: keep it responsive, but bounded.\nNice=-5#" "$tmp_unit"
    printf '\n[Install]\nWantedBy=multi-user.target\n' >> "$tmp_unit"
    sudo -n install -m 0644 "$tmp_unit" "$SERVICE_DST"
fi
rm -f "$tmp_unit"
sc daemon-reload
sc enable "$UNIT" >/dev/null 2>&1 || true
if [[ "$NO_START" -eq 0 ]]; then
    sc restart "$UNIT"
    sleep 3
    if ! sc is-active --quiet "$UNIT"; then
        echo "Relay failed to start. Recent log:" >&2
        unit_journal >&2
        exit 1
    fi
    log "Relay active ($([[ "$USER_UNIT" -eq 1 ]] && echo user || echo system) unit)"
fi

# --- 4. optionally repoint RMS and colour capture at the relay -------------
if [[ "$REPOINT_RMS" -eq 1 ]]; then
    [[ "$(getent ahostsv4 localhost | awk 'NR==1 {print $1}')" == "127.0.0.1" ]] ||
        { echo "localhost does not resolve to 127.0.0.1; not repointing RMS" >&2; exit 1; }
    python3 "$RELAY_PY" rms --config "$CONFIG" --bind "$BIND"
fi
if [[ "$REPOINT_CAPTURE" -eq 1 ]]; then
    python3 "$RELAY_PY" capture --config "$CONFIG" --bind "$BIND"
fi

log "Done."
