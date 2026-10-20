"""Memory, disk and load of the machine this server runs on (read from /proc; None where it cannot be read)."""

import os
import shutil
from typing import Dict, List, Optional

from .analyzer import Finding

LOW_MEMORY_WARN, LOW_MEMORY_ERROR = 0.10, 0.05       # fraction of memory still available
LOW_DISK_WARN, LOW_DISK_ERROR = 0.10, 0.05           # fraction of the disk still free


def _meminfo(path: str = "/proc/meminfo") -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        with open(path) as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    out[key] = int(parts[0]) * 1024
    except OSError:
        pass
    return out


THROTTLED_PATH = "/sys/devices/platform/soc/soc:firmware/get_throttled"   # Raspberry Pi firmware flags (same as vcgencmd get_throttled)
OVERCOMMIT_WARN = 1.10             # limits are ceilings, not reservations: a little more than the machine's memory is normal, 10% more is not


def _read_throttled(path: str) -> Optional[int]:
    try:
        with open(path) as fh:
            return int(fh.read().strip(), 16)
    except (OSError, ValueError):
        return None                # not a Raspberry Pi, or not readable from this container


def read_host(meminfo_path: str = "/proc/meminfo", loadavg_path: str = "/proc/loadavg", disk_path: str = "/",
              throttled_path: str = THROTTLED_PATH) -> Dict:
    mem = _meminfo(meminfo_path)
    swap_total = mem.get("SwapTotal")
    swap_free = mem.get("SwapFree")
    try:
        du = shutil.disk_usage(disk_path)
        disk_total, disk_free = du.total, du.free
    except OSError:
        disk_total = disk_free = None
    try:
        with open(loadavg_path) as fh:
            load1: Optional[float] = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        load1 = None
    return {
        "mem_total_bytes": mem.get("MemTotal"), "mem_available_bytes": mem.get("MemAvailable"),
        "swap_total_bytes": swap_total,
        "swap_used_bytes": (swap_total - swap_free) if swap_total is not None and swap_free is not None else None,
        "disk_total_bytes": disk_total, "disk_free_bytes": disk_free,
        "load1": load1, "cpus": os.cpu_count(),
        "throttled": _read_throttled(throttled_path),
        "container_limits_bytes": None,      # filled in by the service: the sum of the memory limits of its containers
    }


def _mb(n: Optional[int]) -> str:
    return f"{n / 1048576:.0f} MB" if n is not None else "?"


def host_findings(host: Dict) -> List[Finding]:
    out: List[Finding] = []
    total, avail = host.get("mem_total_bytes"), host.get("mem_available_bytes")
    if total and avail is not None:
        frac = avail / total
        if frac < LOW_MEMORY_WARN:
            out.append(Finding("host-memory-low", "error" if frac < LOW_MEMORY_ERROR else "warn", "The machine is running out of memory",
                               f"{_mb(avail)} of {_mb(total)} are still available ({frac * 100:.0f}%).",
                               "Stop what you do not need, lower the memory limits of heavy services (MEM_* in .env.robot) or add memory; "
                               "when it runs out the kernel kills services at random."))
    swap_total, swap_used = host.get("swap_total_bytes"), host.get("swap_used_bytes")
    if swap_total and swap_used is not None and swap_used / swap_total > 0.5:
        out.append(Finding("host-swap-high", "warn", "The machine is swapping",
                           f"{_mb(swap_used)} of {_mb(swap_total)} of swap are in use.",
                           "Heavy swapping makes everything slow (and wears an SD card). Reduce memory use."))
    dtotal, dfree = host.get("disk_total_bytes"), host.get("disk_free_bytes")
    if dtotal and dfree is not None:
        frac = dfree / dtotal
        if frac < LOW_DISK_WARN:
            out.append(Finding("host-disk-low", "error" if frac < LOW_DISK_ERROR else "warn", "The disk is almost full",
                               f"{dfree / 1073741824:.1f} GB of {dtotal / 1073741824:.1f} GB are free ({frac * 100:.0f}%).",
                               "Delete old images and logs (docker system prune), old recordings or move the data folder; databases stop working when it is full."))
    limits = host.get("container_limits_bytes")
    if total and limits and limits > total * OVERCOMMIT_WARN:
        out.append(Finding("host-memory-overcommitted", "warn", "The memory limits add up to more than the machine has",
                           f"The containers may use {_mb(limits)} in total (their limits) but the machine has {_mb(total)}.",
                           "If several services peak together the kernel kills one at random instead of the one over its own limit. "
                           "Lower the MEM_* limits (see .env.robot.example) so they fit, or move a service off this machine."))
    flags = host.get("throttled")
    if flags:
        now_bits = [name for bit, name in ((0x1, "under-voltage"), (0x2, "frequency capped"), (0x4, "throttled"), (0x8, "soft temperature limit")) if flags & bit]
        before = [name for bit, name in ((0x10000, "under-voltage"), (0x20000, "frequency capped"), (0x40000, "throttled"), (0x80000, "soft temperature limit")) if flags & bit]
        if now_bits:
            out.append(Finding("host-power-throttled", "error" if flags & 0x1 else "warn", "The Raspberry Pi is slowed down right now",
                               f"The firmware reports {', '.join(now_bits)} (flags 0x{flags:x}).",
                               "Under-voltage means a weak power supply or cable: use the official 5 V 3 A supply and a short, thick cable. "
                               "Everything runs slower while it lasts, at random moments."))
        elif before:
            out.append(Finding("host-power-throttled-earlier", "warn", "The Raspberry Pi was slowed down since it started",
                               f"The firmware reports {', '.join(before)} happened earlier (flags 0x{flags:x}).",
                               "Check the power supply and cable; reboot to clear the flag once fixed."))
    load, cpus = host.get("load1"), host.get("cpus")
    if load is not None and cpus and load > 2 * cpus:
        out.append(Finding("host-load-high", "warn", "The machine is overloaded",
                           f"The 1-minute load is {load:.1f} on {cpus} CPU(s).",
                           "Something uses all the CPU; find it with docker stats."))
    return out
