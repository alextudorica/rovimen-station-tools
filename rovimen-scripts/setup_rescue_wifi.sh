#!/usr/bin/env bash
# Rescue Wi-Fi (#1432): a Wi-Fi profile that comes up only when the cable
# uplink is down, so an operator can bring a station back online with a phone
# hotspot named ROVIMEN-RESCUE and the station's own password.
#
# Install stages the profile switched OFF and adds a root-owned helper,
#   /usr/local/sbin/rovimen-rescue-wifi up|down|status
# with an exact-argument sudoers rule for the watchdog's user. The watchdog
# (rovimen_watchdog.sh, rescue_wifi_step) calls `up` after the cable path has
# failed RESCUE_WIFI_AFTER runs in a row and `down` once the cable has worked
# RESCUE_WIFI_CLEAR runs in a row. A station never joins the hotspot while its
# cable works, so the shared SSID does not put it on someone else's network.
#
# Each station has its own password: --generate-password makes one, prints it
# once (record it in the operator access notes "Rescue Wi-Fi" and give it to the
# operator), and it is otherwise only in the root-only profile. Without that
# flag the password is read from stdin. It never goes onto a command line.
#
# Backends: NetworkManager when it is active and manages the Wi-Fi device,
# else netplan (gmnro10 Roit runs NetworkManager with every device unmanaged).
#   NetworkManager (e.g. gmnro15 (Sighetu Marmației)): keyfile with autoconnect=false,
#     metric 600; up = nmcli connection up, then the route metric lowered to
#     5 on the device (runtime only), down = nmcli connection down.
#   netplan/networkd (e.g. gmnro14 Chiribiș): YAML staged in
#     /etc/rovimen/rescue-wifi/ with metric 600; up = copy into /etc/netplan
#     with metric 5 + netplan apply, down = remove + netplan apply.
#     `netplan generate` checks it at install.
# While up, the rescue owns the default route (metric 5 beats every DHCP
# route), because when the cable link is up but the router or the ISP is dead
# the wired route would otherwise keep the traffic and the hotspot carry none.
# A station whose default route already runs over Wi-Fi (Pi on wlan0) is
# skipped: a second profile on the same radio would fight the home network.
#
# Usage: setup_rescue_wifi.sh [--generate-password | < password]
#            [--ssid ROVIMEN-RESCUE] [--iface IF] [--country RO]
#            [--user U] [--dry-run]
# Prints one line: RESCUE-WIFI: PASS|SKIP|FAIL ...
set -u

SSID="ROVIMEN-RESCUE" IFACE="" COUNTRY="RO" DRY=0 GEN=0 SVC_USER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ssid) SSID="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    --country) COUNTRY="$2"; shift 2 ;;
    --user) SVC_USER="$2"; shift 2 ;;
    --generate-password) GEN=1; shift ;;
    --dry-run) DRY=1; shift ;;
    *) echo "RESCUE-WIFI: FAIL unknown argument $1"; exit 2 ;;
  esac
done

ETC="${ROVIMEN_ETC:-/etc}"
NET="${ROVIMEN_SYS_CLASS_NET:-/sys/class/net}"
SBIN="${ROVIMEN_SBIN:-/usr/local/sbin}"
SUDO="${ROVIMEN_SUDO-sudo -n}"
STAGE="$ETC/rovimen/rescue-wifi"
HELPER="$SBIN/rovimen-rescue-wifi"

case "$SSID" in *[!A-Za-z0-9._-]*|'') echo "RESCUE-WIFI: FAIL SSID must be [A-Za-z0-9._-]"; exit 2 ;; esac
case "$COUNTRY" in [A-Z][A-Z]) ;; *) echo "RESCUE-WIFI: FAIL --country must be two capital letters"; exit 2 ;; esac

if [ -z "$IFACE" ]; then
  for d in "$NET"/*; do
    [ -d "$d/wireless" ] && { IFACE=$(basename "$d"); break; }
  done
fi
[ -n "$IFACE" ] || { echo "RESCUE-WIFI: SKIP no Wi-Fi adapter"; exit 0; }
if ip route show default 2>/dev/null | grep -qw "dev $IFACE"; then
  echo "RESCUE-WIFI: SKIP $IFACE already carries the default route (Wi-Fi is this station's uplink)"; exit 0
fi

# The watchdog's user gets the sudoers rule (gmnro08 Brașov: SSH lands as the
# admin account, the station tooling runs as rmsuser).
if [ -z "$SVC_USER" ]; then
  SVC_USER=$(sed -n 's/^User=//p' "$ETC/systemd/system/rovimen-watchdog.service" 2>/dev/null | head -1)
  [ -n "$SVC_USER" ] || SVC_USER=$(id -un)
fi

if [ "$GEN" = 1 ]; then
  # Easy to type on a phone: 3 groups of 4 from a-z/2-9 without look-alikes.
  PSK=$(LC_ALL=C tr -dc 'abcdefghjkmnpqrstuvwxyz23456789' < /dev/urandom | head -c 12 | sed 's/.\{4\}/&-/g; s/-$//')
else
  IFS= read -r PSK || true
fi
[ ${#PSK} -ge 8 ] && [ ${#PSK} -le 63 ] || { echo "RESCUE-WIFI: FAIL password must be 8-63 characters"; exit 2; }
case "$PSK" in *\"*|*\\*) echo "RESCUE-WIFI: FAIL password must not contain quote or backslash"; exit 2 ;; esac

# NetworkManager can be running while netplan/networkd owns every interface
# (gmnro10 Roit: all devices "unmanaged"); nmcli cannot bring a profile up on a
# device it does not manage, so such a station needs the netplan backend.
NM_ACTIVE=0 BACKEND=netplan
if systemctl is-active --quiet NetworkManager 2>/dev/null; then
  NM_ACTIVE=1
  NM_STATE=$(nmcli -t -f DEVICE,STATE device status 2>/dev/null | awk -F: -v d="$IFACE" '$1 == d {print $2; exit}')
  if [ -n "$NM_STATE" ] && [ "$NM_STATE" != unmanaged ]; then BACKEND=nm; fi
fi
if [ "$BACKEND" = netplan ] && [ "$DRY" = 0 ] && ! command -v wpa_supplicant >/dev/null 2>&1; then
  echo "RESCUE-WIFI: FAIL netplan Wi-Fi needs wpa_supplicant (apt install wpasupplicant)"; exit 1
fi

write_root() {  # write_root PATH MODE  (content on stdin)
  if [ "$DRY" = 1 ]; then
    echo "--- would write $1 ($2)"
    sed 's/^\(\s*\(psk\|password\)\s*[:=]\s*\).*/\1<redacted>/'
    return 0
  fi
  $SUDO install -d -m 755 "$(dirname "$1")" && $SUDO install -m "$2" /dev/stdin "$1"
}

fail() { echo "RESCUE-WIFI: FAIL $*"; exit 1; }

# A re-run that changes backend removes what an earlier run staged for the
# other one, so only one profile can ever come up. A rescue profile that is
# live under netplan is left alone: removing it needs `netplan apply`, which
# reconfigures the camera LAN too; `rovimen-rescue-wifi down` first.
remove_root() {  # remove_root PATH
  $SUDO test -e "$1" 2>/dev/null || return 1
  if [ "$DRY" = 1 ]; then echo "--- would remove $1 (left by an earlier run)"; return 0; fi
  $SUDO rm -f "$1" && echo "removed $1 (left by an earlier run with the other backend)"
}
STALE_NM="$ETC/NetworkManager/system-connections/$SSID.nmconnection"
if [ "$BACKEND" = netplan ]; then
  if remove_root "$STALE_NM" && [ "$DRY" = 0 ] && [ "$NM_ACTIVE" = 1 ]; then
    $SUDO nmcli connection reload || echo "nmcli connection reload failed after removing $STALE_NM"
  fi
else
  remove_root "$STAGE/90-rovimen-rescue-wifi.yaml" || true
  if $SUDO test -e "$ETC/netplan/90-rovimen-rescue-wifi.yaml" 2>/dev/null; then
    echo "WARN $ETC/netplan/90-rovimen-rescue-wifi.yaml is live from an earlier netplan run; left in place"
  fi
fi

printf 'options cfg80211 ieee80211_regdom=%s\n' "$COUNTRY" \
  | write_root "$ETC/modprobe.d/rovimen-wifi-regdom.conf" 644 || fail "regdom"

if [ "$BACKEND" = nm ]; then
  write_root "$ETC/NetworkManager/system-connections/$SSID.nmconnection" 600 <<EOF || fail "cannot write the NetworkManager profile"
[connection]
id=$SSID
type=wifi
interface-name=$IFACE
autoconnect=false

[wifi]
mode=infrastructure
ssid=$SSID

[wifi-security]
key-mgmt=wpa-psk
psk=$PSK

[ipv4]
method=auto
route-metric=600

[ipv6]
method=auto
route-metric=600
EOF
  [ "$DRY" = 1 ] || $SUDO nmcli connection reload || fail "nmcli connection reload"
else
  write_root "$STAGE/90-rovimen-rescue-wifi.yaml" 600 <<EOF || fail "cannot stage the netplan profile"
# Rescue Wi-Fi (#1432), written by setup_rescue_wifi.sh. Copied into
# /etc/netplan only while the cable uplink is down (rovimen-rescue-wifi up).
network:
  version: 2
  wifis:
    $IFACE:
      optional: true
      regulatory-domain: $COUNTRY
      dhcp4: true
      dhcp4-overrides:
        route-metric: 600
      dhcp6: true
      dhcp6-overrides:
        route-metric: 600
      access-points:
        "$SSID":
          password: "$PSK"
EOF
  if [ "$DRY" = 0 ]; then
    # Check the profile the way netplan will read it, without activating it:
    # generate against a scratch root holding the live config plus this file.
    tmp=$($SUDO mktemp -d) || fail "mktemp"
    $SUDO cp -a "$ETC/netplan" "$tmp/netplan" 2>/dev/null || $SUDO mkdir -p "$tmp/netplan"
    $SUDO cp "$STAGE/90-rovimen-rescue-wifi.yaml" "$tmp/netplan/"
    if ! out=$($SUDO netplan generate --root-dir "$tmp" 2>&1); then
      $SUDO rm -rf "$tmp" "$STAGE/90-rovimen-rescue-wifi.yaml"
      echo "$out" | tail -3
      fail "netplan rejected the profile; removed it"
    fi
    $SUDO rm -rf "$tmp"
  fi
fi

printf 'backend=%s\niface=%s\nssid=%s\n' "$BACKEND" "$IFACE" "$SSID" \
  | write_root "$STAGE/conf" 644 || fail "cannot write $STAGE/conf"

write_root "$HELPER" 755 <<'HELPER' || fail "cannot install $HELPER"
#!/usr/bin/env bash
# rovimen-rescue-wifi up|down|status — installed by setup_rescue_wifi.sh (#1432).
# Root-owned; the watchdog calls it through an exact sudoers rule.
# status: up (profile live and associated with the hotspot), waiting (netplan:
# profile live, nothing associated, e.g. no phone in range), down.
set -u
ETC="${ROVIMEN_ETC:-/etc}"
NET="${ROVIMEN_SYS_CLASS_NET:-/sys/class/net}"
STAGE="$ETC/rovimen/rescue-wifi"
. "$STAGE/conf" 2>/dev/null || { echo "no rescue Wi-Fi configured"; exit 1; }
LIVE="$ETC/netplan/90-rovimen-rescue-wifi.yaml"
RESCUE_METRIC=5
ASSOC_WAIT="${ROVIMEN_RESCUE_ASSOC_WAIT:-20}"  # seconds for the radio to associate after netplan apply
is_live() {
  if [ "$backend" = nm ]; then nmcli -t -f NAME connection show --active 2>/dev/null | grep -qxF "$ssid"
  else [ -f "$LIVE" ]; fi
}
# A Wi-Fi interface reports carrier 1 only while associated with an access point.
associated() { [ "$(cat "$NET/$iface/carrier" 2>/dev/null)" = 1 ]; }
state() {
  if ! is_live; then echo down
  elif [ "$backend" = nm ] || associated; then echo up
  else echo waiting; fi
}
case "${1:-}" in
  status) state ;;
  up)
    # While up, the rescue carries the default route: metric RESCUE_METRIC
    # beats any DHCP route (NetworkManager 100 for ethernet, networkd 1024),
    # so a link that is up behind a dead router or ISP does not keep the
    # traffic. The profile itself keeps metric 600; `down` restores it.
    case "$(state)" in
      up) echo up; exit 0 ;;
      # Already applied: not re-applied, because netplan apply reconfigures
      # every interface, the camera LAN included.
      waiting) echo waiting; exit 1 ;;
    esac
    if [ "$backend" = nm ]; then
      nmcli -w 30 connection up "$ssid" || exit 1
      nmcli device modify "$iface" ipv4.route-metric "$RESCUE_METRIC" ipv6.route-metric "$RESCUE_METRIC" \
        || echo "rescue Wi-Fi up, but its route metric could not be lowered"
    else
      sed "s/route-metric: 600/route-metric: $RESCUE_METRIC/" "$STAGE/90-rovimen-rescue-wifi.yaml" \
        | install -m 600 /dev/stdin "$LIVE" && netplan apply || exit 1
      for _ in $(seq 1 "$ASSOC_WAIT"); do associated && break; sleep 1; done
      associated || { echo waiting; exit 1; }
    fi
    echo up ;;
  down)
    is_live || { echo down; exit 0; }
    if [ "$backend" = nm ]; then nmcli connection down "$ssid"
    else rm -f "$LIVE" && netplan apply; fi ;;
  *) echo "usage: rovimen-rescue-wifi up|down|status"; exit 2 ;;
esac
HELPER

RULE="$SVC_USER ALL=(root) NOPASSWD: $HELPER up, $HELPER down, $HELPER status"
printf '%s\n' "$RULE" | write_root "$ETC/sudoers.d/rovimen-rescue-wifi" 440 || fail "cannot write the sudoers rule"
if [ "$DRY" = 0 ] && command -v visudo >/dev/null 2>&1 \
    && ! $SUDO visudo -cf "$ETC/sudoers.d/rovimen-rescue-wifi" >/dev/null 2>&1; then
  $SUDO rm -f "$ETC/sudoers.d/rovimen-rescue-wifi"
  fail "visudo rejected the sudoers rule; removed it"
fi

[ "$GEN" = 1 ] && [ "$DRY" = 0 ] && echo "RESCUE-WIFI password for $(hostname) ($SSID): $PSK  (record it in the operator access notes; shown once)"
suffix=""; [ "$DRY" = 1 ] && suffix=" dry-run"
echo "RESCUE-WIFI: PASS backend=$BACKEND iface=$IFACE ssid=$SSID user=$SVC_USER off until the cable fails$suffix"
