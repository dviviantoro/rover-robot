#!/usr/bin/env python3
"""Record a RAM snapshot for dev-docs/architecture/memory-budget.md.

AGENTS.md rule 4: measure, don't assume. Every milestone ends with a run of this.
memory-budget.md asks for three moments: idle, mid-turn peak, and load spike --
pass --label to say which one this is.

Usage:
    python3 scripts/measure/ram_snapshot.py --label "M0 baseline idle"
    python3 scripts/measure/ram_snapshot.py --label "M1 llama-server 4k ctx" --settle 10

Appends a JSON line to scripts/measure/snapshots.jsonl and prints a summary
plus the top resident processes, so the number in the doc can be attributed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).resolve().parent / "snapshots.jsonl"
MIB = 1024.0  # jtop reports KiB


def top_processes(n: int = 12) -> list[dict]:
    """Top-N by RSS. Reported alongside the total so a surprise has a name."""
    out = subprocess.run(
        ["ps", "-eo", "rss=,args="], capture_output=True, text=True, check=True
    ).stdout
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        rss, _, args = line.partition(" ")
        try:
            rss_kib = int(rss)
        except ValueError:
            continue
        rows.append({"rss_mib": round(rss_kib / MIB, 1), "cmd": args.strip()[:90]})
    rows.sort(key=lambda r: r["rss_mib"], reverse=True)
    return rows[:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, help="what this snapshot is measuring")
    ap.add_argument(
        "--settle",
        type=float,
        default=3.0,
        help="seconds to let the board settle before sampling (default 3)",
    )
    args = ap.parse_args()

    from jtop import jtop  # imported late so --help works without the service

    if args.settle > 0:
        time.sleep(args.settle)

    with jtop() as j:
        if not j.ok():
            print("jtop not ok -- is the jtop service running?")
            return 1
        mem = j.memory
        ram, swap = mem["RAM"], mem["SWAP"]
        snap = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "label": args.label,
            "nvpmodel": str(j.nvpmodel),
            "jetson_clocks": bool(j.jetson_clocks),
            "ram_total_mib": round(ram["tot"] / MIB, 1),
            "ram_used_mib": round(ram["used"] / MIB, 1),
            "ram_free_mib": round(ram["free"] / MIB, 1),
            "ram_buffers_mib": round(ram.get("buffers", 0) / MIB, 1),
            "ram_cached_mib": round(ram.get("cached", 0) / MIB, 1),
            "swap_used_mib": round(swap.get("used", 0) / MIB, 1),
            "largest_free_block_mib": ram.get("lfb"),
            "top_processes": top_processes(),
        }

    with OUT.open("a") as fh:
        fh.write(json.dumps(snap) + "\n")

    print(f"\n{snap['label']}  --  {snap['ts']}")
    print(f"  power mode        {snap['nvpmodel']}  (jetson_clocks={snap['jetson_clocks']})")
    print(f"  RAM used          {snap['ram_used_mib']:>8.1f} MiB "
          f"of {snap['ram_total_mib']:.1f} MiB total")
    print(f"  RAM free          {snap['ram_free_mib']:>8.1f} MiB")
    print(f"  buffers / cached  {snap['ram_buffers_mib']:>8.1f} / {snap['ram_cached_mib']:.1f} MiB")
    print(f"  swap used         {snap['swap_used_mib']:>8.1f} MiB")
    print("\n  top resident processes:")
    for p in snap["top_processes"]:
        print(f"    {p['rss_mib']:>8.1f} MiB  {p['cmd']}")
    print(f"\n  appended to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
