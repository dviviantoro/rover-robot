#!/usr/bin/env python3
"""M9 soak: run the robot for a long time and prove nothing drifts.

ROADMAP M9: "24-hour soak test -- no OOM kill, no RAM creep, no stuck motors" and
"thermals verified under sustained MAXN SUPER load".

Drives a continuous conversation while sampling memory, swap, temperature and clocks,
then reports on three things that only show up over time:

  * **OOM kills** -- scanned from dmesg, not inferred.
  * **RAM creep** -- least-squares slope of used memory over the run, extrapolated to
    24 h. A robot that leaks 40 MB/h looks fine for an hour and is dead by morning.
  * **Stuck motors** -- the body must never be left driving between turns.
  * **Thermal throttling** -- MAXN SUPER is uncapped, so sustained load throttles
    without airflow. Throttling looks like "the robot got slow", not like a fault.

    python scripts/hardening/soak.py --minutes 60
    python scripts/hardening/soak.py --minutes 1440 --out run/soak-24h.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
MIB = 1024.0

PROMPTS = [
    ("en", "What is your name?"),
    ("id", "Siapa nama kamu?"),
    ("ar", "ما اسمك؟"),
    ("en", "Move forward a little."),
    ("id", "Tolong maju sedikit."),
    ("en", "What colour is the sky?"),
    ("ar", "كيف حالك اليوم؟"),
    ("en", "Turn to your left."),
]

SERVICES = {
    "llm": lambda a: "llama-server" in a and "--embeddings" not in a,
    "embed": lambda a: "llama-server" in a and "--embeddings" in a,
    "asr": lambda a: "robot.asr.service" in a,
    "tts": lambda a: "robot.tts.service" in a,
    "core": lambda a: "robot.core.app" in a,
    "ui": lambda a: "robot.ui.app" in a,
}


def meminfo() -> dict[str, float]:
    values = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            values[key] = float(rest.split()[0]) / MIB
    return values


def service_rss() -> dict[str, float]:
    out = subprocess.run(["ps", "-eo", "rss=,args="], capture_output=True, text=True).stdout
    found: dict[str, float] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "ps -eo" in line:
            continue
        rss, _, args = line.partition(" ")
        for name, match in SERVICES.items():
            if match(args):
                try:
                    found[name] = found.get(name, 0.0) + int(rss) / MIB
                except ValueError:
                    pass
                break
    return found


def thermals() -> dict[str, float]:
    """Zone temperatures straight from sysfs -- jtop is not needed and costs less here."""
    out: dict[str, float] = {}
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            name = (zone / "type").read_text().strip()
            milli = int((zone / "temp").read_text().strip())
        except Exception:
            # Several Tegra zones are unreadable or return nothing at all.
            continue
        if -50_000 < milli < 200_000:
            out[name] = milli / 1000.0
    return out


def throttling() -> bool:
    """True only when a zone is genuinely into its throttling range.

    Tegra zones carry several trip points: a low `passive` hint (70 C here) that merely
    asks the cooling governor to engage, a high `passive` one (99 C) that actually cuts
    clocks, and `critical` (104 C). Treating the low hint as throttling reported
    "throttled in 134 of 270 samples" on a run whose peak was 71.8 C -- 27 C below
    anything that costs performance. Use the HIGHEST passive trip per zone.
    """
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            temp = int((zone / "temp").read_text()) / 1000.0
        except Exception:
            continue
        limits = []
        for trip in zone.glob("trip_point_*_type"):
            try:
                kind = trip.read_text().strip()
                point = int(trip.with_name(
                    trip.name.replace("_type", "_temp")).read_text()) / 1000.0
            except Exception:
                continue
            if kind in ("passive", "hot", "critical"):
                limits.append(point)
        if limits and temp >= max(limits) * 0.95:
            return True
    return False


def oom_kills() -> list[str]:
    try:
        out = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.splitlines()
            if "killed process" in line.lower() or "out of memory" in line.lower()]


def slope_mib_per_hour(samples: list[tuple[float, float]],
                       warmup_s: float = 0.0) -> float:
    """Least-squares slope of used memory. The number that matters over 24 h.

    Samples inside `warmup_s` are discarded. Models page in and caches fill over the
    first few minutes, which is a step change, not a leak -- including it reported
    +5481 MiB/hour on a run that was in fact flat.
    """
    samples = [(t, v) for t, v in samples if t >= warmup_s]
    if len(samples) < 8:
        return 0.0
    n = len(samples)
    mean_t = sum(t for t, _ in samples) / n
    mean_v = sum(v for _, v in samples) / n
    num = sum((t - mean_t) * (v - mean_v) for t, v in samples)
    den = sum((t - mean_t) ** 2 for t, _ in samples)
    if den == 0:
        return 0.0
    return (num / den) * 3600.0


async def talk(url: str, stop: asyncio.Event, stats: dict) -> None:
    """Hold a conversation for the whole run, reconnecting as needed."""
    index = 0
    async with aiohttp.ClientSession() as session:
        while not stop.is_set():
            try:
                async with session.ws_connect(url, heartbeat=30) as ws:
                    await ws.receive_json()
                    while not stop.is_set():
                        lang, text = PROMPTS[index % len(PROMPTS)]
                        index += 1
                        started = time.monotonic()
                        await ws.send_json({"type": "say", "text": text})
                        turn_id = None
                        while not stop.is_set():
                            msg = await asyncio.wait_for(ws.receive_json(), timeout=150)
                            kind = msg.get("type")
                            if kind == "transcript" and msg.get("text") == text:
                                turn_id = msg.get("turn_id")
                            elif msg.get("turn_id") != turn_id:
                                continue
                            elif kind == "turn_done":
                                stats["turns"] += 1
                                stats["latency"].append(time.monotonic() - started)
                                if msg.get("timed_out"):
                                    stats["timeouts"] += 1
                                if msg.get("language") == lang:
                                    stats["lang_ok"] += 1
                                break
                            elif kind == "turn_timeout":
                                stats["timeouts"] += 1
                                break
                        await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stats["errors"] += 1
                stats["last_error"] = repr(exc)
                await asyncio.sleep(5)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--warmup", type=float, default=5.0,
                    help="minutes to exclude from the RAM-creep fit (startup transient)")
    ap.add_argument("--core-ws", default="http://127.0.0.1:8090")
    ap.add_argument("--token", default=os.environ.get("CORE_WS_TOKEN"))
    ap.add_argument("--out", default="run/soak.jsonl")
    args = ap.parse_args()

    url = f"{args.core_ws}?token={args.token}" if args.token else args.core_ws
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    baseline_ooms = len(oom_kills())
    stats = {"turns": 0, "errors": 0, "timeouts": 0, "lang_ok": 0,
             "latency": [], "last_error": None}
    stop = asyncio.Event()
    talker = asyncio.create_task(talk(url, stop, stats))

    started = time.monotonic()
    duration = args.minutes * 60
    used_series: list[tuple[float, float]] = []
    peak_used = peak_swap = peak_temp = 0.0
    stuck_motor_samples = 0     # longest consecutive run of "body moving"
    consecutive_moving = 0
    throttled_samples = 0
    samples = 0

    print(f"soaking for {args.minutes:.0f} min, sampling every {args.interval:.0f}s")
    print(f"{'elapsed':>8} {'used MiB':>9} {'swap':>7} {'max °C':>7} {'turns':>6} {'err':>4}")

    with out.open("w") as fh:
        while time.monotonic() - started < duration:
            await asyncio.sleep(args.interval)
            now = time.monotonic() - started
            info = meminfo()
            used = (info["MemTotal"] - info["MemFree"] - info.get("Buffers", 0.0)
                    - info.get("Cached", 0.0) - info.get("SReclaimable", 0.0))
            swap = info.get("SwapTotal", 0.0) - info.get("SwapFree", 0.0)
            temps = thermals()
            hottest = max(temps.values()) if temps else 0.0
            body = {}
            try:
                body = json.loads((ROOT / "run" / "stm32_state.json").read_text())
            except (OSError, json.JSONDecodeError):
                pass

            samples += 1
            used_series.append((now, used))
            peak_used = max(peak_used, used)
            peak_swap = max(peak_swap, swap)
            peak_temp = max(peak_temp, hottest)
            if throttling():
                throttled_samples += 1
            # A stuck motor is the body still driving after the turn that commanded it.
            # A single sample catching a legitimate 1 s DRIVE mid-flight is not a fault,
            # so only CONSECUTIVE moving samples count -- the drills already prove the
            # STM32 stops the wheels on its own.
            if body.get("moving"):
                consecutive_moving += 1
                stuck_motor_samples = max(stuck_motor_samples, consecutive_moving)
            else:
                consecutive_moving = 0

            fh.write(json.dumps({
                "t": round(now, 1), "used_mib": round(used, 1),
                "swap_mib": round(swap, 1), "temps": temps,
                "services": {k: round(v, 1) for k, v in service_rss().items()},
                "turns": stats["turns"], "errors": stats["errors"],
                "moving": bool(body.get("moving")),
            }) + "\n")
            fh.flush()

            if samples % 6 == 0 or samples == 1:
                print(f"{now/60:7.1f}m {used:9.0f} {swap:7.0f} {hottest:7.1f} "
                      f"{stats['turns']:6d} {stats['errors']:4d}")

    stop.set()
    talker.cancel()
    try:
        await talker
    except asyncio.CancelledError:
        pass

    warmup_s = min(args.warmup * 60, duration / 3)
    creep = slope_mib_per_hour(used_series, warmup_s=warmup_s)
    new_ooms = oom_kills()[baseline_ooms:]
    latency = sorted(stats["latency"])
    median = latency[len(latency) // 2] if latency else 0.0

    print("\n" + "=" * 66)
    print(f"soak: {args.minutes:.0f} min, {samples} samples, {stats['turns']} turns")
    print("=" * 66)
    print(f"  peak memory in use     {peak_used:8.0f} MiB of {meminfo()['MemTotal']:.0f}")
    print(f"  peak swap              {peak_swap:8.0f} MiB")
    print(f"  RAM creep              {creep:+8.1f} MiB/hour  "
          f"({creep * 24:+.0f} MiB over 24 h)   [first {warmup_s/60:.0f} min excluded]")
    print(f"  peak temperature       {peak_temp:8.1f} °C")
    print(f"  thermally throttled    {throttled_samples:8d} of {samples} samples")
    print(f"  OOM kills              {len(new_ooms):8d}")
    print(f"  longest continuous move{stuck_motor_samples:8d} samples "
          f"({stuck_motor_samples * args.interval:.0f}s -- a stuck motor never stops)")
    if used_series:
        first_svc = json.loads(out.read_text().splitlines()[0])["services"]
        last_svc = json.loads(out.read_text().splitlines()[-1])["services"]
        drift = {k: last_svc.get(k, 0) - first_svc.get(k, 0)
                 for k in set(first_svc) | set(last_svc)}
        worst = sorted(drift.items(), key=lambda kv: -abs(kv[1]))[:3]
        print("  per-service drift      " +
              ", ".join(f"{k} {v:+.0f} MiB" for k, v in worst))
    print(f"  turns                  {stats['turns']:8d}  "
          f"median {median:.1f}s, {stats['timeouts']} timed out, "
          f"{stats['errors']} link errors")
    if stats["turns"]:
        print(f"  language correct       {stats['lang_ok']}/{stats['turns']}")
    if stats["last_error"]:
        print(f"  last link error        {stats['last_error'][:70]}")

    verdict = []
    if new_ooms:
        verdict.append(f"OOM KILLS: {new_ooms[0][:80]}")
    if abs(creep) * 24 > 512:
        verdict.append(f"RAM CREEP: {creep * 24:+.0f} MiB projected over 24 h")
    # A DRIVE is capped at 10 s by the protocol, so anything past ~30 s of continuous
    # motion means the body was left running.
    if stuck_motor_samples * args.interval > 30:
        verdict.append(f"STUCK MOTORS: body moved continuously for "
                       f"{stuck_motor_samples * args.interval:.0f}s")
    if stats["timeouts"]:
        verdict.append(f"{stats['timeouts']} turns timed out")
    if throttled_samples:
        verdict.append(f"thermal throttling in {throttled_samples} samples")

    print()
    if verdict:
        print("FAIL:")
        for line in verdict:
            print(f"  - {line}")
    else:
        print(f"PASS: nothing drifted over {args.minutes:.0f} minutes.")
        if args.minutes < 1440:
            print(f"      This is not the 24-hour run the roadmap asks for -- "
                  f"rerun with --minutes 1440 on a clean boot.")
    print(f"\nsamples written to {out}")
    return 1 if verdict else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
