#!/usr/bin/env bash
# Give a Pi's eth0 one fixed address on the robot LAN, so it and the Jetson see each other locally. Run on the Pi, as root
# (copy it over and run it through Tailscale, never over eth0):
#
#   scp scripts/configure_pi_lan.sh satipi@rasp:/tmp/ && ssh -t satipi@rasp 'sudo bash /tmp/configure_pi_lan.sh apply'
#
#   configure_pi_lan.sh [--profile pi4|pi5] [--ip A.B.C.D] [--jetson A.B.C.D] [--iface IF] [--wlan-off [--gateway A.B.C.D]] check|apply|rollback
#
# --profile NAME reads the robot LAN settings from docker_compose/profiles/NAME.env, the one place they are written down for both Pis:
#   ROBOT_LAN_IP (this Pi's address; the gateway and broker listen on it too), LAN_JETSON_IP, LAN_IFACE, LAN_WLAN_OFF=true|false,
#   LAN_GATEWAY. Explicit options win over the profile. Never installs or upgrades a package.
#
#   check      read-only report: managers, addresses, routes, the Jetson, whether the address is free
#   apply      back up the config, set eth0 to a static <ip>/24, verify, and undo by itself if the check fails
#   rollback   restore the last backup (config file + the address added; no dhcpcd/NetworkManager restart beyond the one apply did)
#
# Defaults: --ip 192.168.1.70, --jetson 192.168.1.50, --iface eth0. eth0 gets no gateway and no DNS, so the default route
# (internet, Tailscale) stays on wlan0. Whoever owns eth0 is edited: NetworkManager if it has a connection on the interface, else
# dhcpcd. The stale 192.168.50.x address is removed from eth0; files that mention 192.168.50.x are only listed. Never reboots.
# Idempotent: running apply again with the same arguments changes nothing.
#
# --wlan-off: the Pi is then reachable on the robot LAN only (a client on that LAN sees one address, no second network). The
# internet and Tailscale have to come through eth0 then, so eth0 also gets the gateway (--gateway, default: the router eth0's DHCP
# reported, else <subnet>.1) with that router and 1.1.1.1 as DNS. The order is: eth0 static + gateway, check that the internet and
# Tailscale work through eth0 while wlan0 is still up, only then wlan0 off (rfkill soft block through /sys, no package needed;
# systemd-rfkill keeps it over a reboot when it is there, check after one), check again; any failed check restores everything,
# wlan0 included. rollback turns wlan0 back on. Run it detached and over Tailscale: the connection drops while the route moves.
set -euo pipefail

IP="192.168.1.70"; JETSON="192.168.1.50"; IFACE="eth0"; CMD=""; WLAN_OFF=0; GATEWAY=""; WLAN_IF="wlan0"; EXTRA_DNS="1.1.1.1"
STATE_DIR="/var/lib/sati-lan"
DHCPCD_CONF="/etc/dhcpcd.conf"
NM_DIR="/etc/NetworkManager/system-connections"
NM_RUN_DIR="/run/NetworkManager/system-connections"   # NetworkManager's generated (unsaved, DHCP) profiles live here

die() { printf "configure_pi_lan: %b\n" "$*" >&2; exit 1; }
log() { printf "configure_pi_lan: %b\n" "$*" >&2; }
usage() { sed -n '2,/^set -e/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'; }

# --profile first: its values are the defaults that explicit options then override
PROFILE=""
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
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --profile) [ $# -ge 2 ] || die "--profile needs a value"; shift ;;
        --ip) [ $# -ge 2 ] || die "--ip needs a value"; IP="$2"; shift ;;
        --jetson) [ $# -ge 2 ] || die "--jetson needs a value"; JETSON="$2"; shift ;;
        --iface) [ $# -ge 2 ] || die "--iface needs a value"; IFACE="$2"; shift ;;
        --wlan-off) WLAN_OFF=1 ;;
        --gateway) [ $# -ge 2 ] || die "--gateway needs a value"; GATEWAY="$2"; shift ;;
        check|apply|rollback) [ -z "$CMD" ] || die "one command only"; CMD="$1" ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument $1 (see --help)" ;;
    esac
    shift
done
[ -n "$CMD" ] || { usage; exit 2; }

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
