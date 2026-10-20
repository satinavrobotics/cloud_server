#!/usr/bin/env bash
# Configure Tailscale and the network side of a Pi deployment so peers on the
# same LAN keep reaching each other over Tailscale when the internet or the
# Tailscale control plane is down. See docs/pi_tailscale_offline.md.
#
#   sudo scripts/configure_pi_network.sh check          # read-only report
#   sudo scripts/configure_pi_network.sh apply          # change what can be changed here
#   sudo scripts/configure_pi_network.sh offline-test   # section 6 test (WAN must be unplugged)
#
# Env: TAILSCALE_VERSION (default 1.104.1), EXTRA_DNS (default 1.1.1.1),
#      TIMESYNC_POLL_MAX (default 600), PEER (100.x address, for offline-test).
set -uo pipefail

TS_VERSION="${TAILSCALE_VERSION:-1.104.1}"
EXTRA_DNS="${EXTRA_DNS:-1.1.1.1}"
POLL_MAX="${TIMESYNC_POLL_MAX:-600}"
MIN_MAJOR_MINOR="1.104"

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

  if [ -f /etc/systemd/timesyncd.conf.d/50-satinav-poll.conf ]; then ok "timesyncd PollIntervalMaxSec drop-in present"
  else warn "timesyncd drop-in missing"; fi
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
  systemctl restart systemd-timesyncd 2>/dev/null && ok "PollIntervalMaxSec=$POLL_MAX" || warn "systemd-timesyncd not available (chrony in use?)"
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

case "${1:-check}" in
  check) check_tailscale; check_system; summary ;;
  apply)
    need_root
    apply_tailscale; apply_dns; apply_timesyncd; apply_docker_order
    TODOS=0; WARNINGS=0
    step "Re-check"; check_tailscale; check_system
    echo
    echo "  Then let the device run online once so the cache fills, and run: $0 offline-test"
    summary ;;
  offline-test) need_root; offline_test ;;
  *) echo "usage: $0 [check|apply|offline-test]" >&2; exit 2 ;;
esac
