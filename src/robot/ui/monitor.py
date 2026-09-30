"""Board metrics for the Monitor page.

The dashboard samples the board itself rather than asking the orchestrator for it. That
keeps the cost of a page left open -- and any bug in the sampling -- inside this process,
which is the whole reason the UI is separate (system-overview.md).

Everything here reads /proc and /sys directly. `jtop` is used only if it happens to be
importable, and never required: a missing jetson-stats degrades the page, it does not
break it.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from robot.common import services

log = logging.getLogger("robot-ui")

MIB = 1024.0

# Which process is which comes from robot.common.services -- the same table the Services
# panel starts and stops things with. Two llama-server processes exist (the LLM and the
# embedding server), and matching on the binary name alone silently sums them into one
# row; keeping the rule in one place is what stops this page and that panel disagreeing
# about which one just grew.
PROCESSES = tuple(
    (svc.label, svc.needle, svc.excludes, svc.requires) for svc in services.SERVICES
)


def _is(args: str, needle: str, excludes: str, requires: str) -> bool:
    return (needle in args
            and not (excludes and excludes in args)
            and not (requires and requires not in args))


# memory-budget.md: the ~7 GB steady-state target, and the real ceiling is what jtop
# reports (~7620 MiB), not 8192 -- the rest is Tegra carveout.
BUDGET_TARGET_MIB = 7168.0

_prev_cpu: dict[str, tuple[int, int]] = {}


def _meminfo() -> dict[str, float]:
    values = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            values[key] = float(rest.split()[0]) / MIB
    return values


def _cpu_percent() -> dict[str, float]:
    """Busy% per CPU since the previous call, plus an aggregate."""
    out: dict[str, float] = {}
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if not line.startswith("cpu"):
                    continue
                parts = line.split()
                name = parts[0]
                values = [int(x) for x in parts[1:]]
                idle = values[3] + (values[4] if len(values) > 4 else 0)
                total = sum(values)
                prev_total, prev_idle = _prev_cpu.get(name, (0, 0))
                _prev_cpu[name] = (total, idle)
                dt, di = total - prev_total, idle - prev_idle
                out[name] = round(100.0 * (dt - di) / dt, 1) if dt > 0 else 0.0
    except OSError:
        pass
    return out


def _thermals() -> dict[str, float]:
    out: dict[str, float] = {}
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            name = (zone / "type").read_text().strip()
            milli = int((zone / "temp").read_text().strip())
        except Exception:
            continue      # several Tegra zones are unreadable or return nothing
        if -50_000 < milli < 200_000:
            out[name] = round(milli / 1000.0, 1)
    return out


def _gpu() -> dict[str, float | None]:
    """GPU load and frequency from the Tegra devfreq node, if present."""
    result: dict[str, float | None] = {"load": None, "freq_mhz": None}
    for base in Path("/sys/class/devfreq").glob("*"):
        try:
            name = (base / "device/of_node/name").read_text().strip("\x00\n ")
        except Exception:
            name = base.name
        if "gpu" not in name.lower() and "gv11b" not in base.name and "ga10b" not in base.name:
            continue
        try:
            result["freq_mhz"] = round(int((base / "cur_freq").read_text()) / 1e6, 0)
        except Exception:
            pass
        try:
            load = (base / "device/load").read_text().strip()
            result["load"] = round(int(load) / 10.0, 1)   # per-mille on Tegra
        except Exception:
            pass
        break
    return result


def _power() -> dict[str, float]:
    """INA3221 rails, in watts."""
    rails: dict[str, float] = {}
    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        for volt in sorted(hwmon.glob("in*_input")):
            index = volt.name[2:-6]
            curr = hwmon / f"curr{index}_input"
            label = hwmon / f"in{index}_label"
            if not curr.is_file():
                continue
            try:
                mv = int(volt.read_text())
                ma = int(curr.read_text())
                name = label.read_text().strip() if label.is_file() else f"rail{index}"
            except Exception:
                continue
            rails[name] = round(mv * ma / 1e6, 2)
    return rails


def _processes() -> list[dict]:
    try:
        out = subprocess.run(["ps", "-eo", "pid=,rss=,pcpu=,args="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found: dict[str, dict] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "ps -eo" in line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, rss, pcpu, args = parts
        for label, needle, excludes, requires in PROCESSES:
            if _is(args, needle, excludes, requires):
                row = found.setdefault(label, {"name": label, "pid": int(pid),
                                               "rss_mib": 0.0, "cpu": 0.0})
                try:
                    row["rss_mib"] += int(rss) / MIB
                    row["cpu"] += float(pcpu)
                except ValueError:
                    pass
                break
    for row in found.values():
        row["rss_mib"] = round(row["rss_mib"], 1)
        row["cpu"] = round(row["cpu"], 1)
    return [found[label] for label, *_ in PROCESSES if label in found]


def snapshot() -> dict:
    info = _meminfo()
    total = info.get("MemTotal", 7620.0)
    used = (total - info.get("MemFree", 0.0) - info.get("Buffers", 0.0)
            - info.get("Cached", 0.0) - info.get("SReclaimable", 0.0))
    swap_total = info.get("SwapTotal", 0.0)
    cpus = _cpu_percent()
    return {
        "t": time.time(),
        "memory": {
            "total_mib": round(total, 1),
            "used_mib": round(used, 1),
            "free_mib": round(info.get("MemFree", 0.0), 1),
            "cached_mib": round(info.get("Cached", 0.0), 1),
            "target_mib": BUDGET_TARGET_MIB,
        },
        "swap": {
            "total_mib": round(swap_total, 1),
            "used_mib": round(swap_total - info.get("SwapFree", 0.0), 1),
        },
        "cpu": {
            "total": cpus.get("cpu", 0.0),
            "cores": [v for k, v in sorted(cpus.items()) if k != "cpu"],
        },
        "gpu": _gpu(),
        "thermal": _thermals(),
        "power": _power(),
        "processes": _processes(),
    }
