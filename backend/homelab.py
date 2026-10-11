# Health report for the machine the backend runs on (the Raspberry Pi), using only the standard
# library: CPU temperature, load, memory, disks, Pi throttling, and systemd service states.
# Each check is compared against a threshold so the model gets a verdict, not just raw numbers.
# Checks that don't apply on this machine (e.g. no /sys on macOS) are skipped.
import asyncio
import os
import shutil
import socket
from pathlib import Path

from config import HOMELAB_DISKS, HOMELAB_SERVICES

TEMP_WARN, TEMP_HOT = 70.0, 80.0  # °C; the Pi starts throttling at 80-85
DISK_WARN, DISK_CRIT = 80.0, 90.0  # percent used
MEM_LOW = 10.0  # percent available
LOAD_HIGH = 2.0  # 1-minute load per core

# Bits reported by `vcgencmd get_throttled`
THROTTLE_NOW = {0: "under-voltage", 1: "CPU frequency capped", 2: "throttled", 3: "soft temperature limit"}
THROTTLE_PAST = {16: "under-voltage", 17: "CPU frequency capping", 18: "throttling", 19: "soft temperature limit"}


async def _run(*cmd: str) -> str | None:
    """Output of a fixed command, or None if it isn't installed or fails."""
    if not shutil.which(cmd[0]):
        return None
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    return out.decode().strip()


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _gb(n: int) -> str:
    return f"{n / 1024**3:.1f} GB"


async def report() -> str:
    lines, issues, notes = [f"Host: {socket.gethostname()}"], [], []

    if uptime := _read("/proc/uptime"):
        secs = float(uptime.split()[0])
        lines.append(f"Uptime: {int(secs // 86400)}d {int(secs % 86400 // 3600)}h")

    if raw := _read("/sys/class/thermal/thermal_zone0/temp"):
        temp = int(raw) / 1000
        lines.append(f"CPU temperature: {temp:.1f}°C")
        if temp >= TEMP_HOT:
            issues.append(f"CPU is hot ({temp:.0f}°C)")
        elif temp >= TEMP_WARN:
            notes.append(f"CPU is warm ({temp:.0f}°C)")

    load1, load5, _ = os.getloadavg()
    cores = os.cpu_count() or 1
    lines.append(f"Load average: {load1:.2f} (1 min), {load5:.2f} (5 min) on {cores} cores")
    if load1 / cores >= LOAD_HIGH:
        issues.append(f"high CPU load ({load1:.1f} on {cores} cores)")

    if meminfo := _read("/proc/meminfo"):
        mem = {k: int(v.split()[0]) * 1024 for k, v in
               (line.split(":", 1) for line in meminfo.splitlines()) if k in ("MemTotal", "MemAvailable")}
        if len(mem) == 2:
            free_pct = 100 * mem["MemAvailable"] / mem["MemTotal"]
            lines.append(f"Memory: {_gb(mem['MemAvailable'])} available of {_gb(mem['MemTotal'])} ({free_pct:.0f}%)")
            if free_pct < MEM_LOW:
                issues.append(f"low memory ({free_pct:.0f}% available)")

    for path in HOMELAB_DISKS:
        if not os.path.exists(path):
            continue
        du = shutil.disk_usage(path)
        used = 100 * du.used / du.total
        lines.append(f"Disk {path}: {used:.0f}% used, {_gb(du.free)} free of {_gb(du.total)}")
        if used >= DISK_CRIT:
            issues.append(f"disk {path} almost full ({used:.0f}%)")
        elif used >= DISK_WARN:
            notes.append(f"disk {path} filling up ({used:.0f}%)")

    if (out := await _run("vcgencmd", "get_throttled")) and "=" in out:
        flags = int(out.split("=")[1], 16)
        now = [name for bit, name in THROTTLE_NOW.items() if flags & (1 << bit)]
        past = [name for bit, name in THROTTLE_PAST.items() if flags & (1 << bit)]
        lines.append(f"Throttling: {out.split('=')[1]}"
                     + (f" (now: {', '.join(now)})" if now else "")
                     + (f" (since boot: {', '.join(past)})" if past else ""))
        if now:
            issues.append(f"Pi is currently affected by {', '.join(now)}")
        elif past:
            notes.append(f"{', '.join(past)} happened since boot (check the power supply)")

    if shutil.which("systemctl"):
        states = await asyncio.gather(*(_run("systemctl", "is-active", s) for s in HOMELAB_SERVICES))
        for service, state in zip(HOMELAB_SERVICES, states):
            state = state or "unknown"
            lines.append(f"Service {service}: {state}")
            if state != "active":
                issues.append(f"service {service} is {state}")
    elif HOMELAB_SERVICES:
        lines.append("Services: systemctl isn't available on this machine, so service health wasn't checked")

    if issues:
        lines.append(f"Overall: {len(issues)} issue(s): " + "; ".join(issues))
    else:
        lines.append("Overall: healthy")
    if notes:
        lines.append("Worth watching: " + "; ".join(notes))
    return "\n".join(lines)
