#!/usr/bin/env python3
"""Sample RAM continuously and report the peak, with the per-process split at that peak.

ROADMAP M4: "measure peak RAM with all three models loaded simultaneously". Steady
state is not the number that matters -- the peak happens mid-turn, when the ASR is
decoding the next utterance while the LLM generates and Piper synthesises.
"""
from __future__ import annotations

import argparse
import subprocess
import time

MIB = 1024.0
# Two llama-server processes now: the LLM and the embedding server. Matching on the
# binary name alone silently sums them into one row, which made the LLM look like it
# had grown by 2 GB.
WATCH = {
    "llama-server (LLM)": lambda a: "llama-server" in a and "--embeddings" not in a,
    "llama-server (embed)": lambda a: "llama-server" in a and "--embeddings" in a,
    "robot-asr": lambda a: "robot.asr.service" in a,
    "robot-tts": lambda a: "robot.tts.service" in a,
    "robot-core": lambda a: "robot.core.app" in a,
    "stm32_sim": lambda a: a.strip().endswith("stm32_sim") or "/stm32_sim " in a,
}


def sample() -> tuple[float, float, dict[str, float]]:
    with open("/proc/meminfo") as fh:
        info = {k.strip(): float(v.split()[0]) / MIB
                for k, v in (line.split(":", 1) for line in fh)}
    used = info["MemTotal"] - info["MemFree"] - info["Buffers"] - info["Cached"] \
        - info.get("SReclaimable", 0.0)
    swap = info["SwapTotal"] - info["SwapFree"]

    out = subprocess.run(["ps", "-eo", "rss=,args="], capture_output=True, text=True).stdout
    procs: dict[str, float] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        rss, _, args = line.partition(" ")
        if "ps -eo" in args or "peak_ram" in args:
            continue
        for name, match in WATCH.items():
            if match(args):
                try:
                    procs[name] = procs.get(name, 0.0) + int(rss) / MIB
                except ValueError:
                    pass
                break
    return used, swap, procs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=40.0)
    ap.add_argument("--interval", type=float, default=0.4)
    args = ap.parse_args()

    peak_used, peak_swap, peak_procs, samples = 0.0, 0.0, {}, 0
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        used, swap, procs = sample()
        samples += 1
        if used > peak_used:
            peak_used, peak_procs = used, procs
        peak_swap = max(peak_swap, swap)
        time.sleep(args.interval)

    total = 7619.8
    print(f"\n{samples} samples over {args.seconds:.0f}s\n")
    print(f"  peak RAM in use   {peak_used:8.1f} MiB  of {total:.0f} MiB "
          f"({peak_used/total*100:.0f}%)")
    print(f"  headroom at peak  {total - peak_used:8.1f} MiB")
    print(f"  peak swap in use  {peak_swap:8.1f} MiB")
    print("\n  at that peak:")
    robot = 0.0
    for name in WATCH:
        if name in peak_procs:
            print(f"    {name:<22} {peak_procs[name]:8.1f} MiB")
            robot += peak_procs[name]
    print("\n  (RSS includes each model's mmapped GGUF, which is evictable page cache.")
    print("   The 'peak RAM in use' figure above excludes cache and is the real number.)")
    print(f"    {'robot services total':<22} {robot:8.1f} MiB")
    print(f"    {'everything else':<22} {peak_used - robot:8.1f} MiB "
          "(OS + dev tooling)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
