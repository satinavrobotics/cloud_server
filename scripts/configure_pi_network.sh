#!/usr/bin/env bash
# One script for a Pi's network setup: the robot LAN address (fixed eth0), Tailscale, DNS, the NTP server (chrony) and the
# docker/tailscaled start order. See docs/pi_setup.md (start there) and docs/pi_tailscale_offline.md. Run on the Pi, as root,
# through Tailscale, never over eth0:
#
#   scp scripts/configure_pi_network.sh satipi@rasp:/tmp/ && ssh -t satipi@rasp 'sudo bash /tmp/configure_pi_network.sh --profile pi5 apply'
#
#   configure_pi_network.sh [--profile pi4|pi5] [--ip A.B.C.D] [--jetson A.B.C.D] [--iface IF] [--wlan-off [--gateway A.B.C.D]]
#                           [--ntp-server] [check|apply|rollback|offline-test]
#
#   check         read-only report: Tailscale, DNS, clock/NTP, docker order and (with --profile or --ip) the LAN address
#   apply         Tailscale client + prefs, second DNS, clock-sync drop-in, docker order (+ chrony NTP server with --ntp-server); then the LAN address (last: it can move the
#                 routes, and its checks need Tailscale up). Undoes the LAN change by itself if its checks fail. Idempotent.
#   rollback      LAN address only: restore the last backup (config file + the address added; wlan0 back on)
#   offline-test  restart tailscaled with the WAN unplugged (docs/pi_tailscale_offline.md section 6)
#
# --profile NAME reads docker_compose/profiles/NAME.env, the one place the robot LAN is written down for both Pis:
#   ROBOT_LAN_IP (this Pi's address; the gateway and broker listen on it too), LAN_JETSON_IP, LAN_IFACE, LAN_WLAN_OFF=true|false,
#   LAN_GATEWAY. Explicit options win over the profile. Without --profile or --ip the LAN address is left alone.
#
# LAN: defaults --jetson 192.168.1.50, --iface eth0. eth0 gets no gateway and no DNS, so the default route (internet,
# Tailscale) stays on wlan0. Whoever owns eth0 is edited: NetworkManager if it has a connection on the interface, else dhcpcd.
# The stale 192.168.50.x address is removed from eth0; files that mention 192.168.50.x are only listed. Never reboots.
#
# --wlan-off: the Pi is then reachable on the robot LAN only (a client on that LAN sees one address, no second network). The
# internet and Tailscale have to come through eth0 then, so eth0 also gets the gateway (--gateway, default: the router eth0's DHCP
# reported, else <subnet>.1) with that router and 1.1.1.1 as DNS. The order is: eth0 static + gateway, check that the internet and
# Tailscale work through eth0 while wlan0 is still up, only then wlan0 off (rfkill soft block through /sys, no package needed;
# systemd-rfkill keeps it over a reboot when it is there, check after one), check again; any failed check restores everything,
# wlan0 included. Run it detached and over Tailscale: the connection drops while the route moves.
#
# --ntp-server: ALSO make this Pi the LAN's NTP server (chrony). Off by default: the Pi 5 has no RTC battery yet, so its clock after
# a power loss is wrong, and the Jetson must not follow it (robot team, 2026-10-24: do not apply for the clock until a battery is fitted and
# the power-off test repeated). Even then it refuses while the clock is before the build date (NTP_CLOCK_FLOOR, default the HEAD commit time).
#
# Packages: apt installs only tailscale (pinned, if older than 1.104) and, with --ntp-server, chrony. Nothing is upgraded (docs/robot_deploy.md).
#
# Env: TAILSCALE_VERSION (default 1.104.1), EXTRA_DNS (default 1.1.1.1), TIMESYNC_POLL_MAX (default 600),
#      NTP_ALLOW (LAN served by chrony; default the /24 of the LAN address, else 192.168.1.0/24), PEER (100.x address, for offline-test).
set -uo pipefail

IP="192.168.1.70"; JETSON="192.168.1.50"; IFACE="eth0"; WLAN_OFF=0; GATEWAY=""; WLAN_IF="wlan0"; PROFILE=""; LAN_WANTED=0; NTP_SERVER=0; CMD=""
STATE_DIR="/var/lib/sati-lan"
DHCPCD_CONF="/etc/dhcpcd.conf"
NM_DIR="/etc/NetworkManager/system-connections"
NM_RUN_DIR="/run/NetworkManager/system-connections"   # NetworkManager's generated (unsaved, DHCP) profiles live here
TS_VERSION="${TAILSCALE_VERSION:-1.104.1}"
EXTRA_DNS="${EXTRA_DNS:-1.1.1.1}"
POLL_MAX="${TIMESYNC_POLL_MAX:-600}"
MIN_MAJOR_MINOR="1.104"

die() { printf "configure_pi_network: %b\n" "$*" >&2; exit 1; }
log() { printf "configure_pi_network: %b\n" "$*" >&2; }
usage() { sed -n '2,/^set -/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'; }

# --profile first: its values are the defaults that explicit options then override
for ((i = 1; i < $#; i++)); do [ "${!i}" = "--profile" ] && { j=$((i + 1)); PROFILE="${!j}"; }; done
if [ -n "$PROFILE" ]; then
    PF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docker_compose/profiles/$PROFILE.env"
    [ -f "$PF" ] || die "no profile $PF"
    pv() { sed -n "s/^$1=\(.*\)$/\1/p" "$PF" | tail -1; }
    v="$(pv ROBOT_LAN_IP)"; [ -z "$v" ] || IP="$v"
    v="$(pv LAN_JETSON_IP)"; [ -z "$v" ] || JETSON="$v"
    v="$(pv LAN_IFACE)"; [ -z "$v" ] || IFACE="$v"
    v="$(pv LAN_GATEWAY)"; [ -z "$v" ] || GATEWAY="$v"
    [ "$(pv LAN_WLAN_OFF)" != true ] || WLAN_OFF=1
    LAN_WANTED=1
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --profile) [ $# -ge 2 ] || die "--profile needs a value"; shift ;;
        --ip) [ $# -ge 2 ] || die "--ip needs a value"; IP="$2"; LAN_WANTED=1; shift ;;
        --jetson) [ $# -ge 2 ] || die "--jetson needs a value"; JETSON="$2"; shift ;;
        --iface) [ $# -ge 2 ] || die "--iface needs a value"; IFACE="$2"; shift ;;
        --wlan-off) WLAN_OFF=1 ;;
        --ntp-server) NTP_SERVER=1 ;;
        --gateway) [ $# -ge 2 ] || die "--gateway needs a value"; GATEWAY="$2"; shift ;;
        check|apply|rollback|offline-test) [ -z "$CMD" ] || die "one command only"; CMD="$1" ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument $1 (see --help)" ;;
    esac
    shift
done
CMD="${CMD:-check}"
[ "$CMD" != rollback ] || [ "$LAN_WANTED" = 1 ] || die "rollback restores the LAN address: pass --profile or --ip"
NTP_ALLOW="${NTP_ALLOW:-$([ "$LAN_WANTED" = 1 ] && echo "${IP%.*}.0/24" || echo 192.168.1.0/24)}"   # LAN served by this Pi's NTP server (chrony)

ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; WARNINGS=$((WARNINGS + 1)); }
todo() { printf '  TODO  %s\n' "$*"; TODOS=$((TODOS + 1)); }
step() { printf '\n== %s\n' "$*"; }
WARNINGS=0
TODOS=0

need_root() { [ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }; }
have() { command -v "$1" >/dev/null 2>&1; }

ts_version() { tailscale version 2>/dev/null | head -1; }
version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

# ---------- checks (also run after apply) ----------

check_tailscale() {
  step "Tailscale client"
  have tailscale || { todo "tailscale is not installed"; return; }
  local v; v="$(ts_version)"
  if version_ge "$v" "$MIN_MAJOR_MINOR"; then ok "version $v (netmap caching needs >= $MIN_MAJOR_MINOR)"
  else todo "version $v is below $MIN_MAJOR_MINOR: no netmap cache (run apply)"; fi
  if apt-mark showhold 2>/dev/null | grep -qx tailscale; then ok "apt hold on tailscale"
  else warn "tailscale is not held; an apt upgrade can change the version"; fi

  local info
  info="$(tailscale status --json 2>/dev/null | python3 -c '
import sys, json
s = json.load(sys.stdin).get("Self") or {}
caps = s.get("CapMap") or {}
print("CACHE=%s" % ("yes" if "cache-network-maps" in caps else "no"))
print("TAGS=%s" % ",".join(s.get("Tags") or []))
print("EXPIRY=%s" % (s.get("KeyExpiry") or "none"))
' 2>/dev/null)" || info=""
  if [ -z "$info" ]; then warn "tailscale status unavailable (tailscaled down or not logged in)"; return; fi
  eval "$(printf '%s\n' "$info" | grep -E '^(CACHE|TAGS|EXPIRY)=' | sed "s/=\(.*\)/='\1'/")"
  if [ "$CACHE" = yes ]; then ok "node attribute cache-network-maps granted"
  else todo "cache-network-maps missing from CapMap: add a nodeAttrs entry in the admin console (docs section 5)"; fi
  [ -n "$TAGS" ] && ok "tags: $TAGS" || warn "no tags; the nodeAttrs target must match this device's user"
  if [ "$EXPIRY" = none ]; then ok "node key has no expiry"
  else todo "node key expires $EXPIRY: Machines page > Disable key expiry"; fi

  if journalctl -u tailscaled -b 0 --no-pager 2>/dev/null | grep -i 'loaded netmap' | tail -1 | grep -q .; then
    ok "$(journalctl -u tailscaled -b 0 --no-pager | grep -i 'loaded netmap' | tail -1 | sed 's/^.*\(loaded netmap\)/\1/')"
  else warn "no 'loaded netmap' line this boot (cache not filled yet, or tailscaled not restarted)"; fi
}

check_system() {
  step "Network side"
  if tailscale debug prefs 2>/dev/null | grep -q '"NetfilterMode": 0'; then ok "netfilter-mode off"
  else warn "netfilter-mode not off (only matters if an iptables health error shows up)"; fi
  if tailscale debug prefs 2>/dev/null | grep -q '"AutoUpdate"' && \
     tailscale debug prefs 2>/dev/null | python3 -c 'import sys,json;sys.exit(0 if not json.load(sys.stdin).get("AutoUpdate",{}).get("Apply") else 1)' 2>/dev/null
  then ok "auto-update off"; else warn "auto-update on or unreadable"; fi

  if [ -f /etc/chrony/conf.d/50-satinav-server.conf ]; then
    ok "chrony serves the LAN ($(grep -h '^allow' /etc/chrony/conf.d/50-satinav-server.conf | tr '\n' ' '))"
    chronyc tracking 2>/dev/null | grep -E 'Reference ID|Leap status|System time' | sed 's/^/        /'
  elif [ -f /etc/systemd/timesyncd.conf.d/50-satinav-poll.conf ]; then ok "timesyncd PollIntervalMaxSec drop-in present"
  else warn "timesyncd drop-in missing"; fi
  have timedatectl && timedatectl 2>/dev/null | grep -E 'RTC time|Local time' | sed 's/^ */        /'
  if [ -f /etc/systemd/system/docker.service.d/10-after-tailscaled.conf ]; then ok "docker starts after tailscaled"
  else warn "docker.service has no After=tailscaled drop-in"; fi
  if have timedatectl; then
    timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes && ok "clock synchronised" || warn "clock not synchronised"
  fi
  dns_report
}

dns_report() {
  if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    local con; con="$(default_nm_connection)"
    if [ -n "$con" ] && nmcli -g ipv4.dns con show "$con" 2>/dev/null | grep -q "$EXTRA_DNS"; then
      ok "NetworkManager '$con' has DNS $EXTRA_DNS"
    else warn "NetworkManager default-route profile lacks DNS $EXTRA_DNS"; fi
  elif [ -f /etc/systemd/resolved.conf.d/50-satinav-dns.conf ]; then ok "systemd-resolved extra DNS drop-in present"
  else warn "no extra DNS configured (NetworkManager not running, no resolved drop-in)"; fi
}

default_nm_connection() {
  local dev; dev="$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="dev"){print $(i+1); exit}}')"
  [ -n "$dev" ] && nmcli -g GENERAL.CONNECTION device show "$dev" 2>/dev/null
}

# ---------- apply ----------

apply_tailscale() {
  step "Tailscale client"
  have tailscale || { echo "  tailscale missing: install it from https://tailscale.com/download first" >&2; return 1; }
  if version_ge "$(ts_version)" "$MIN_MAJOR_MINOR"; then ok "already $(ts_version)"
  else
    echo "  installing tailscale=$TS_VERSION (restarts tailscaled)"
    apt-mark unhold tailscale >/dev/null 2>&1 || true
    apt-get install -y --allow-downgrades "tailscale=$TS_VERSION" || { warn "install failed"; return 1; }
  fi
  apt-mark hold tailscale >/dev/null && ok "apt-mark hold tailscale"
  tailscale set --netfilter-mode=off --auto-update=false && ok "netfilter-mode off, auto-update off"
}

apply_dns() {
  step "Second DNS server"
  if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    local con; con="$(default_nm_connection)"
    [ -n "$con" ] || { warn "no default-route NetworkManager profile"; return; }
    local cur; cur="$(nmcli -g ipv4.dns con show "$con")"
    if printf '%s' "$cur" | grep -q "$EXTRA_DNS"; then ok "'$con' already has $EXTRA_DNS"
    else
      nmcli con mod "$con" +ipv4.dns "$EXTRA_DNS" ipv4.ignore-auto-dns no && \
        nmcli con up "$con" >/dev/null && ok "added $EXTRA_DNS to '$con'" || warn "nmcli failed for '$con'"
    fi
  elif systemctl is-active --quiet systemd-resolved 2>/dev/null; then
    install -d /etc/systemd/resolved.conf.d
    printf '[Resolve]\nFallbackDNS=%s\n' "$EXTRA_DNS" > /etc/systemd/resolved.conf.d/50-satinav-dns.conf
    systemctl restart systemd-resolved && ok "resolved FallbackDNS=$EXTRA_DNS"
  else
    warn "neither NetworkManager nor systemd-resolved is active; add $EXTRA_DNS to your DHCP client (dhcpcd: static domain_name_servers) by hand"
  fi
}

apply_timesyncd() {
  step "Clock sync after an offline boot"
  install -d /etc/systemd/timesyncd.conf.d
  printf '[Time]\nPollIntervalMaxSec=%s\n' "$POLL_MAX" > /etc/systemd/timesyncd.conf.d/50-satinav-poll.conf
  if systemctl is-active --quiet chrony; then ok "chrony is the clock client, timesyncd drop-in not needed"; return; fi
  systemctl restart systemd-timesyncd 2>/dev/null && ok "PollIntervalMaxSec=$POLL_MAX" || warn "systemd-timesyncd not available"
}

# NTP server for the LAN: the Jetson trusts whatever this Pi serves. Keeps serving while unsynchronised
# (local stratum 10) so a Pi that lost its time and has no internet is still the one clock on the LAN.
# Replaces systemd-timesyncd (chrony conflicts with it). Check the Pi's clock and RTC battery first.
apply_chrony() {
  step "NTP server for the LAN (chrony)"
  # floor: a Pi that lost its time (no RTC battery) must not serve it; the build date is the earliest time that can be right
  local floor now; floor="${NTP_CLOCK_FLOOR:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" log -1 --format=%ct 2>/dev/null)}"; now="$(date +%s)"
  [ -n "$floor" ] || { warn "no clock floor (not a git checkout): set NTP_CLOCK_FLOOR=<epoch seconds>; chrony not set up"; return; }
  [ "$now" -ge "$floor" ] || { warn "clock $(date -u +%F) is before the build date $(date -u -d "@$floor" +%F): chrony NOT set up; set the time first"; return; }
  if ! have chronyd; then
    have apt-get || { warn "chrony not installed and no apt-get; install it by hand"; return; }
    DEBIAN_FRONTEND=noninteractive apt-get install -y chrony >/dev/null 2>&1 || { warn "apt-get install chrony failed (no internet?)"; return; }
  fi
  local dir=/etc/chrony/conf.d
  if ! grep -qs '^confdir /etc/chrony/conf.d' /etc/chrony/chrony.conf; then
    printf '\nconfdir /etc/chrony/conf.d\n' >> /etc/chrony/chrony.conf
  fi
  install -d "$dir"
  printf 'allow %s\nlocal stratum 10\n' "$NTP_ALLOW" > "$dir/50-satinav-server.conf"
  systemctl disable --now systemd-timesyncd >/dev/null 2>&1   # one clock client only
  systemctl enable chrony >/dev/null 2>&1; systemctl restart chrony && ok "chrony serves $NTP_ALLOW (local stratum 10)" || warn "chrony did not start"
}

apply_docker_order() {
  step "Docker after tailscaled"
  have docker || { ok "docker not installed, skipped"; return; }
  install -d /etc/systemd/system/docker.service.d
  printf '[Unit]\nAfter=tailscaled.service\nWants=tailscaled.service\n' > /etc/systemd/system/docker.service.d/10-after-tailscaled.conf
  systemctl daemon-reload && ok "docker.service After=tailscaled.service (applies from next restart)"
}

# ---------- offline test ----------

offline_test() {
  step "Offline restart test (docs section 6)"
  echo "  Unplug the router WAN first and make sure no order is active."
  read -r -p "  WAN unplugged and robot idle? [y/N] " a; [ "$a" = y ] || exit 1
  if ping -c1 -W2 1.1.1.1 >/dev/null 2>&1; then echo "  1.1.1.1 still answers: the WAN is not down" >&2; exit 1; fi
  ok "internet unreachable"
  systemctl restart tailscaled
  sleep 30
  tailscale status | head -20
  journalctl -u tailscaled -b 0 --no-pager | grep -i 'loaded netmap' | tail -1
  if [ -n "${PEER:-}" ]; then
    tailscale ping -c 3 "$PEER" && ok "peer $PEER answers" || warn "peer $PEER did not answer"
    tailscale status | grep -F "$PEER" | grep -q direct && ok "direct path" || warn "not 'direct <lan-ip>:41641' (relay or no path)"
  else
    echo "  set PEER=<100.x address> to ping a peer and check the path"
  fi
  echo "  Pass: peers above 0 in the log line and the peer reachable. Plug the WAN back in."
}

summary() {
  step "Summary"
  echo "  $TODOS action(s) needed outside this script or still open, $WARNINGS warning(s)"
  [ "$TODOS" -eq 0 ]
}

# ---------- robot LAN address (runs in a subshell: its set -e and exit stay its own) ----------

lan() (
set -euo pipefail
valid_ip() { printf '%s' "$1" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$' && ! printf '%s' "$1" | tr . '\n' | grep -qE '^(25[6-9]|2[6-9][0-9]|[3-9][0-9]{2})$'; }
valid_ip "$IP" || die "--ip $IP is not an IPv4 address"
valid_ip "$JETSON" || die "--jetson $JETSON is not an IPv4 address"
[ "$IP" != "$JETSON" ] || die "the Pi's address cannot be the Jetson's"
PREFIX="${IP%.*}"
[ "${JETSON%.*}" = "$PREFIX" ] || die "$IP and $JETSON are not in the same /24"
case "${IP##*.}" in 0|1|255) die "$IP is a network, router or broadcast address" ;; esac
[ -z "$GATEWAY" ] || valid_ip "$GATEWAY" || die "--gateway $GATEWAY is not an IPv4 address"
[ -z "$GATEWAY" ] || [ "$WLAN_OFF" = 1 ] || die "--gateway only makes sense with --wlan-off (without it eth0 gets no gateway)"
[ -z "$GATEWAY" ] || [ "${GATEWAY%.*}" = "$PREFIX" ] || die "--gateway $GATEWAY is not in the same /24 as $IP"
[ "$CMD" = check ] || [ "$(id -u)" = 0 ] || die "$CMD needs root (sudo)"

# --- who owns the interface
nm_con() {
    systemctl is-active --quiet NetworkManager 2>/dev/null || return 0
    nmcli -g GENERAL.CONNECTION device show "$IFACE" 2>/dev/null | head -1 | grep -v '^--$' || true
}
owner() {
    if [ -n "$(nm_con)" ]; then echo nm
    elif systemctl is-active --quiet dhcpcd 2>/dev/null; then echo dhcpcd
    else echo none; fi
}
nm_file() {
    local uuid f; uuid="$(nmcli -g connection.uuid con show "$1")"
    for f in "$NM_DIR"/* "$NM_RUN_DIR"/*; do
        if [ -f "$f" ] && grep -qx "uuid=$uuid" "$f"; then printf '%s\n' "$f"; return 0; fi
    done
    return 0
}

has_addr() { ip -4 -o addr show dev "$IFACE" | grep -qE "inet $1/"; }
default_dev() { ip -4 route show default | awk '{for(i=1;i<NF;i++) if($i=="dev"){print $(i+1)}}' | sort -u | tr '\n' ' '; }

# Is anybody else answering ARP for $IP? (arping is not installed on the factory image; a raw socket needs no package.)
arp_taken() {
    python3 - "$IFACE" "$IP" <<'PY'
import socket, struct, sys, time
iface, ip = sys.argv[1], sys.argv[2]
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806))
s.bind((iface, 0))
mac = s.getsockname()[4]
target = socket.inet_aton(ip)
pkt = (b"\xff" * 6 + mac + b"\x08\x06" + struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
       + mac + b"\x00" * 4 + b"\x00" * 6 + target)  # ARP probe: sender 0.0.0.0
end = time.time() + 3
for _ in range(3):
    s.send(pkt)
    t = time.time() + 1
    s.settimeout(0.3)
    while time.time() < t:
        try:
            d = s.recv(1500)
        except socket.timeout:
            continue
        if len(d) >= 42 and d[20:22] == b"\x00\x02" and d[28:32] == target and d[22:28] != mac:
            print(":".join("%02x" % b for b in d[22:28])); sys.exit(1)
sys.exit(0)
PY
}

# --- Wi-Fi off / on (rfkill through /sys: the rfkill binary is not on a factory image)
wlan_rfkill_devs() { local r; for r in /sys/class/rfkill/rfkill*; do [ -r "$r/type" ] && [ "$(cat "$r/type")" = wlan ] && echo "$r"; done; return 0; }
wlan_off() {
    local r; : > "$STATE_DIR/wlan"
    for r in $(wlan_rfkill_devs); do echo "$r $(cat "$r/soft")" >> "$STATE_DIR/wlan"; echo 1 > "$r/soft"; done
    [ -s "$STATE_DIR/wlan" ] || log "warning: no wlan rfkill device found; only taking $WLAN_IF down (that does not survive a reboot)"
    ip link set "$WLAN_IF" down 2>/dev/null || true
}
wlan_on() {   # put back what wlan_off changed; no state file = it was never switched off here
    [ -f "$STATE_DIR/wlan" ] || return 0
    local r prev; while read -r r prev; do [ -n "$r" ] && echo "${prev:-0}" > "$r/soft"; done < "$STATE_DIR/wlan"
    rm -f "$STATE_DIR/wlan"; ip link set "$WLAN_IF" up 2>/dev/null || true
    log "$WLAN_IF switched back on (it takes ~10 s to get its address and route back)"
}
# Tailscale really connected: `tailscale ip` answers even with the tunnel down
ts_online() { command -v tailscale >/dev/null || return 0
    tailscale status --json 2>/dev/null | python3 -c 'import sys,json; s=json.load(sys.stdin); sys.exit(0 if s.get("BackendState")=="Running" and (s.get("Self") or {}).get("Online") else 1)' 2>/dev/null; }
wait_for() { local n="$1"; shift; local i; for i in $(seq 1 "$n"); do "$@" && return 0; sleep 1; done; return 1; }
internet_via_iface() { ping -c2 -W2 -I "$IP" 1.1.1.1 >/dev/null 2>&1; }
dev_default_via_iface() { ip -4 route show default | grep -q "dev $IFACE"; }
dev_default_via_wlan() { ip -4 route show default | grep -q "dev $WLAN_IF"; }

report() {
    local own; own="$(owner)"
    log "interface $IFACE, owner: $own, target $IP/24, Jetson $JETSON"
    log "NetworkManager: $(systemctl is-active NetworkManager 2>&1 || true), dhcpcd: $(systemctl is-active dhcpcd 2>&1 || true)"
    ip -br addr show dev "$IFACE" >&2 || true
    ip route >&2
    log "default route device(s): $(default_dev), $WLAN_IF: $(cat "/sys/class/net/$WLAN_IF/operstate" 2>/dev/null || echo absent)$([ -f "$STATE_DIR/wlan" ] && echo ' (switched off by this script)')"
    ping -c1 -W2 "$JETSON" >/dev/null 2>&1 && log "Jetson $JETSON answers" || log "Jetson $JETSON does not answer (from the current address)"
    # bounded: a recursive grep over a big /home (builds, backups, docker) must not stall the run
    local f; f="$(timeout 20 grep -rIl --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=build --exclude-dir=install --exclude-dir=backups '192\.168\.50\.' /etc /home /opt 2>/dev/null || true)"
    [ -z "$f" ] || { log "files that still mention 192.168.50.x (not edited):"; printf '  %s\n' $f >&2; }
}

if [ "$CMD" = check ]; then
    report
    if has_addr "$IP"; then log "$IP is already on $IFACE"
    elif [ "$(id -u)" = 0 ]; then
        if mac="$(arp_taken)"; then log "$IP looks free"; else log "$IP is TAKEN by $mac"; fi
    else log "run with sudo to probe whether $IP is free"; fi
    exit 0
fi

# --- state / backup
mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"

backup_nm() { cp -p "$1" "$STATE_DIR/nm.orig"; echo "$1" > "$STATE_DIR/nm.path"; }   # a /run profile is only reverted to DHCP, see revert()

revert() {
    local mode; mode="$(cat "$STATE_DIR/mode" 2>/dev/null || true)"
    case "$mode" in
        nm)
            case "$(cat "$STATE_DIR/nm.path")" in
                "$NM_RUN_DIR"/*)   # the generated DHCP profile: put it back to DHCP
                    nmcli con mod "$(cat "$STATE_DIR/nm.con")" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns "" \
                        ipv4.never-default no ;;
                *) cp -p "$STATE_DIR/nm.orig" "$(cat "$STATE_DIR/nm.path")"; nmcli con reload ;;
            esac
            nmcli con up "$(cat "$STATE_DIR/nm.con")" >/dev/null 2>&1 || log "nmcli con up failed; check the profile by hand"
            ;;
        dhcpcd)
            cp -p "$STATE_DIR/dhcpcd.conf.orig" "$DHCPCD_CONF"
            has_addr "$IP" && ip addr del "$IP/24" dev "$IFACE" || true
            log "dhcpcd.conf restored; its restart is yours to decide (dhcpcd -n/-N and aliases are not used)"
            ;;
        *) die "nothing to roll back (no $STATE_DIR/mode)" ;;
    esac
    [ ! -f "$STATE_DIR/dhcpcd.conf.orig" ] || { cp -p "$STATE_DIR/dhcpcd.conf.orig" "$DHCPCD_CONF"; log "dhcpcd.conf restored"; }
    wlan_on
    log "rolled back; if the old 192.168.50.x address is wanted again, a dhcpcd restart or reboot brings it back"
}

if [ "$CMD" = rollback ]; then revert; report; exit 0; fi

# --- apply
own="$(owner)"
[ "$own" != none ] || die "neither NetworkManager nor dhcpcd manages $IFACE"
report
if ! has_addr "$IP"; then
    if mac="$(arp_taken)"; then :; else die "$IP is already in use by $mac; pick another with --ip"; fi
fi
ip link show "$IFACE" | grep -q 'state UP' || die "$IFACE is not up"
if [ "$WLAN_OFF" = 1 ]; then
    if [ -z "$GATEWAY" ]; then   # the router eth0's DHCP reported, read before anything is changed
        GATEWAY="$(ip -4 route show default dev "$IFACE" 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="via"){print $(i+1); exit}}')"
        [ -n "$GATEWAY" ] || GATEWAY="$PREFIX.1"
    fi
    log "--wlan-off: eth0 gets gateway $GATEWAY and DNS $GATEWAY $EXTRA_DNS; $WLAN_IF goes off after eth0 is checked"
    ping -c2 -W2 "$GATEWAY" >/dev/null 2>&1 || die "the gateway $GATEWAY does not answer on $IFACE; nothing changed"
else
    dev_default_via_wlan || log "warning: no default route on $WLAN_IF; the internet will go away if eth0's gateway does"
fi

echo "$own" > "$STATE_DIR/mode"
rm -f "$STATE_DIR/dhcpcd.conf.orig"
[ ! -f "$DHCPCD_CONF" ] || cp -p "$DHCPCD_CONF" "$STATE_DIR/dhcpcd.conf.orig"

if [ "$own" = nm ]; then
    con="$(nm_con)"; file="$(nm_file "$con")"
    [ -n "$file" ] || die "no keyfile for the NetworkManager connection '$con' in $NM_DIR or $NM_RUN_DIR"
    echo "$con" > "$STATE_DIR/nm.con"; backup_nm "$file"
    if [ "$WLAN_OFF" = 1 ]; then
        log "NetworkManager connection '$con' ($file): static $IP/24, gateway $GATEWAY, DNS $GATEWAY $EXTRA_DNS"
        nmcli con mod "$con" ipv4.method manual ipv4.addresses "$IP/24" ipv4.gateway "$GATEWAY" ipv4.dns "$GATEWAY $EXTRA_DNS" \
            ipv4.ignore-auto-dns no ipv4.never-default no
    else
        log "NetworkManager connection '$con' ($file): static $IP/24, no gateway, no DNS"
        nmcli con mod "$con" ipv4.method manual ipv4.addresses "$IP/24" ipv4.gateway "" ipv4.dns "" \
            ipv4.ignore-auto-dns no ipv4.never-default yes
    fi
    nmcli con up "$con" >/dev/null
    # dhcpcd runs too: stop it from putting its own (stale) static addresses back on this interface
    if [ -f "$DHCPCD_CONF" ] && systemctl is-active --quiet dhcpcd 2>/dev/null; then
        python3 - "$DHCPCD_CONF" "$IFACE" <<'PY'
import re, sys
path, iface = sys.argv[1], sys.argv[2]
out, in_block = [], False
for line in open(path).read().splitlines():
    if re.match(r"\s*(interface|profile|ssid)\s", line): in_block = re.match(r"\s*interface\s+%s\s*$" % re.escape(iface), line) is not None
    if in_block and re.match(r"\s*static\s", line): line = "#sati-lan# " + line
    out.append(line)
if not any(re.match(r"\s*denyinterfaces\s.*\b%s\b" % re.escape(iface), l) for l in out):
    # denyinterfaces is a global option: it goes before the first interface block, not inside one
    at = next((i for i, l in enumerate(out) if re.match(r"\s*(interface|profile|ssid)\s", l)), len(out))
    out.insert(at, "denyinterfaces %s   # sati-lan: NetworkManager owns it" % iface)
open(path, "w").write("\n".join(out) + "\n")
PY
        log "dhcpcd.conf: static lines of '$IFACE' commented out and denyinterfaces added (effective at dhcpcd's next restart; not restarted here)"
    fi
else
    python3 - "$DHCPCD_CONF" "$IFACE" "$IP" "${GATEWAY:-}" "$EXTRA_DNS" <<'PY'
import re, sys
path, iface, ip, gw, dns = sys.argv[1:6]
tail = ["static ip_address=%s/24" % ip] + (["static routers=%s" % gw, "static domain_name_servers=%s %s" % (gw, dns)] if gw else ["nogateway"])
lines = open(path).read().splitlines()
out, in_block, done = [], False, False
for line in lines:
    if re.match(r"\s*(interface|profile|ssid)\s", line):
        if in_block and not done:
            out += tail; done = True
        in_block = re.match(r"\s*interface\s+%s\s*$" % re.escape(iface), line) is not None
    elif in_block and re.match(r"\s*(static\s|nogateway\s*$)", line):
        continue
    out.append(line)
if not done:
    if not in_block: out.append("interface %s" % iface)
    out += tail
open(path, "w").write("\n".join(out) + "\n")
PY
    log "dhcpcd.conf: '$IFACE' = static $IP/24, $([ "$WLAN_OFF" = 1 ] && echo "gateway $GATEWAY" || echo nogateway); restarting dhcpcd once (wlan0's routes can take ~10 s to return)"
    systemctl restart dhcpcd
fi

for _ in $(seq 1 30); do has_addr "$IP" && break; sleep 1; done
# the stale 192.168.50.x address left on the interface (a dhcpcd restart does not remove it)
for old in $(ip -4 -o addr show dev "$IFACE" | awk '{print $4}' | grep '^192\.168\.50\.' || true); do
    ip addr del "$old" dev "$IFACE" && log "removed stale $old"
done

# --- verify, or undo
fail=""
has_addr "$IP" || fail="$IP is not on $IFACE"
[ -n "$fail" ] || ping -c3 -W2 -I "$IP" "$JETSON" >/dev/null 2>&1 || fail="the Jetson $JETSON does not answer from $IP"
if [ -z "$fail" ] && [ "$WLAN_OFF" = 0 ]; then
    wait_for 20 dev_default_via_wlan || fail="no default route on $WLAN_IF any more"
    wait_for 30 ts_online || fail="Tailscale is not online any more"
fi
if [ -z "$fail" ] && [ "$WLAN_OFF" = 1 ]; then
    # 1. through eth0 while wlan0 is still up: the internet and the tunnel must already work this way
    wait_for 20 dev_default_via_iface || fail="no default route on $IFACE"
    [ -n "$fail" ] || wait_for 20 internet_via_iface || fail="no internet through $IFACE ($GATEWAY)"
    [ -n "$fail" ] || { log "internet works through $IFACE; switching $WLAN_IF off"; wlan_off
        # 2. wlan0 gone: everything has to survive on eth0 alone
        wait_for 20 dev_default_via_iface || fail="no default route on $IFACE after $WLAN_IF went off"
        [ -n "$fail" ] || wait_for 20 internet_via_iface || fail="no internet after $WLAN_IF went off"
        [ -n "$fail" ] || wait_for 60 ts_online || fail="Tailscale did not come back through $IFACE"
        [ -n "$fail" ] || ping -c2 -W2 -I "$IP" "$JETSON" >/dev/null 2>&1 || fail="the Jetson does not answer after $WLAN_IF went off"; }
fi
if [ -n "$fail" ]; then
    log "verification FAILED: $fail; rolling back"
    revert
    exit 1
fi
log "done: $IFACE has $IP/24, the Jetson $JETSON answers, default route on: $(default_dev)$([ "$WLAN_OFF" = 1 ] && echo ", $WLAN_IF off")"
ip -br addr show dev "$IFACE"; ip route
)

case "$CMD" in
  check)
    check_tailscale; check_system
    if [ "$LAN_WANTED" = 1 ]; then step "Robot LAN address"; lan; else todo "robot LAN address not checked: pass --profile pi4|pi5"; fi
    summary ;;
  apply)
    need_root
    apply_tailscale; apply_dns
    if [ "$NTP_SERVER" = 1 ]; then apply_chrony; fi
    apply_timesyncd; apply_docker_order
    if [ "$LAN_WANTED" = 1 ]; then step "Robot LAN address"; lan || exit 1; fi
    TODOS=0; WARNINGS=0
    step "Re-check"; check_tailscale; check_system
    echo
    echo "  Then let the device run online once so the cache fills, and run: $0 offline-test"
    summary ;;
  rollback) need_root; lan ;;
  offline-test) need_root; offline_test ;;
esac
