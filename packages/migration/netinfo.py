"""The addresses this server can be reached on, for a destination that has to pull from it (a robot sending maps up).

The API runs on the host network, so the interfaces it sees are the host's. A destination tries the candidates in order and keeps
the first whose answer carries this server's own instance id (see jobs.TransferManager), so a wrong guess costs a few seconds, not
a wrong copy.
"""
import fcntl
import ipaddress
import re
import socket
import struct
from typing import Iterable, List, Optional, Tuple

SIOCGIFADDR = 0x8915
SKIP_IFACE = re.compile(r"^(lo|docker|br-|veth|virbr|vmnet|vboxnet)")
TAILNET = ipaddress.ip_network("100.64.0.0/10")


def interface_addresses() -> List[Tuple[str, str]]:
    """[(interface, IPv4)] of this host (Linux)."""
    out: List[Tuple[str, str]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in socket.if_nameindex():
            try:
                packed = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, struct.pack("256s", name.encode()[:15]))
                out.append((name, socket.inet_ntoa(packed[20:24])))
            except OSError:
                continue                                   # an interface without an IPv4 address
    finally:
        sock.close()
    return out


def order_candidates(addresses: Iterable[Tuple[str, str]], port: int = 80, client_host: Optional[str] = None) -> List[str]:
    """Candidate base URLs: Tailscale first (reachable from anywhere on the tailnet), then private LAN addresses, then the host the
    client itself used (the app's view of this server), without duplicates, loopback or container bridges."""
    tail: List[str] = []
    lan: List[str] = []
    for name, ip in addresses:
        if SKIP_IFACE.match(name):
            continue
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            continue
        (tail if addr in TAILNET else lan).append(ip)
    hosts = tail + lan
    if client_host:
        host = client_host.split(":")[0] if client_host.count(":") == 1 else client_host
        if host and host not in hosts and not host.startswith("127.") and host != "localhost":
            hosts.append(host)
    suffix = "" if port == 80 else f":{port}"
    return [f"http://{h}{suffix}" for h in hosts]


def own_urls(port: int = 80, client_host: Optional[str] = None) -> List[str]:
    return order_candidates(interface_addresses(), port, client_host)


def is_own_address(host: str) -> bool:
    """Whether `host` (an IP or name) is one of this machine's own addresses (or loopback)."""
    h = (host or "").strip("[]").lower()
    if h in ("localhost", "::1") or h.startswith("127."):
        return True
    return any(h == ip for _, ip in interface_addresses())
