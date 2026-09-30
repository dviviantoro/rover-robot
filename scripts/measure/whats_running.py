#!/usr/bin/env python3
"""What is actually loaded right now, and where that disagrees with what you chose.

    python scripts/measure/whats_running.py
    python scripts/measure/whats_running.py --json

A model choice passes through three layers before anything loads it, and each of them
can be the one that is out of date:

    chosen      run/settings.json    what you picked in the dashboard
    configured  config/*.env         what the service will read the NEXT time it starts
    running     /proc/<pid>          what the live process actually has open

The Memory page now writes all three at once -- choosing a model applies it and restarts
what has to reload -- so in normal use these agree. They used to not: picking a model
only staged it, and on this board all three disagreed at once, with Qwen3-4B chosen,
gemma-3-1b configured and gemma-3-1b running. Every layer was behaving as designed and
the answer to "which model am I talking to?" was still wrong. They can still drift after
a failed restart, a hand-edited env file, or a service that was never bounced.

It also reports an ASR decoder that is running when nothing is reading it -- exactly one
is ever meant to be loaded, and a spare one is 2 GB of a 7.6 GB board held for nothing.

The running column is read from the process itself -- its command line, or the
environment it was handed at exec -- never from the same file as the configured column.
Two readings of one file would agree by construction and prove nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from robot.common import services  # noqa: E402

BOLD, DIM, RED, YELLOW, GREEN, RESET = (
    ("\033[1m", "\033[2m", "\033[31m", "\033[33m", "\033[32m", "\033[0m")
    if sys.stdout.isatty() else ("", "", "", "", "", "")
)


# Below this a service has a few pages out and nobody would notice. Above it, measured
# on this board, the synthesiser had 801 MiB out and ran at RTF ~4.
SWAP_WARN_MIB = 50.0


def _swap_device() -> str:
    """Where the pages actually have to come back FROM -- a file on the data disk is a
    different cost from zram, and the remedy differs too."""
    try:
        lines = Path("/proc/swaps").read_text().splitlines()[1:]
        return ", ".join(line.split()[0] for line in lines) or "swap"
    except OSError:
        return "swap"


def collect() -> dict:
    """All three layers, from `robot.common.services` -- the same source the dashboard
    reads. Two implementations of "are these in sync?" would be two answers."""
    return {"services": services.snapshot(),
            "staged": services.chosen_models(),
            "supervisor": services.supervisor(),
            "supervisor_label": services.SUPERVISOR_LABEL.get(services.supervisor(), "")}


def render(data: dict) -> int:
    print(f"\n{BOLD}Supervisor{RESET}  {data['supervisor_label']}\n")
    header = (f"{'service':10} {'state':9} {'on':4} {'RSS':>7} {'swap':>7} "
              f"{'running':32} {'configured':32} {'chosen':22}")
    print(BOLD + header + RESET)
    print(DIM + "-" * len(header) + RESET)

    problems: list[str] = []
    for row in data["services"]:
        if not (row["model"] or row["configured"] or row.get("chosen")):
            continue
        state = "running" if row["running"] else "stopped"
        running = row["model"] or ("-" if not row["running"] else "?")
        configured = row["configured"] or "-"
        chosen = row.get("chosen") or ""

        colour = ""
        if row["stale"]:
            colour = RED
            problems.append(
                f"{row['key']}: running {running}, but config says {configured}. "
                f"Restart it to apply -- Services page, or "
                f"./scripts/run_stack.sh svc restart {row['key']}")
        elif row.get("unapplied"):
            colour = YELLOW
            problems.append(
                f"{row['key']}: {chosen} is chosen but {configured} is configured. "
                f"Press Apply on the dashboard's Memory page, which writes the config "
                f"and restarts the service in one go.")
        elif row.get("unexpected"):
            colour = YELLOW
            problems.append(
                f"{row['key']}: running, but the selected ASR model does not use this "
                f"decoder -- it is holding {row.get('rss_mib')} MiB nothing reads. "
                f"./scripts/run_stack.sh svc stop {row['key']}")
        elif row.get("missing"):
            colour = RED
            problems.append(
                f"{row['key']}: the selected ASR model needs this decoder and it is not "
                f"running. The robot cannot hear until it is.")
        # Read from the process's own file descriptors, not from its flags: a
        # llama-server told to put 99 layers on the GPU still runs on the CPU if the
        # allocation failed, and on this board that is the difference that matters.
        where = row.get("accelerator") or ("-" if not row["running"] else "?")
        rss = f"{row['rss_mib']:.0f}M" if row.get("rss_mib") else "-"
        swap = f"{row['swap_mib']:.0f}M" if row.get("swap_mib") else "-"
        print(f"{colour}{row['key']:10} {state:9} {where:4} {rss:>7} {swap:>7} "
              f"{running:32} {configured:32} {chosen:22}{RESET}")

    # Two CUDA processes on a board with one unified pool is not an error, but it is
    # the thing that took the LLM's KV cache away, so it is worth saying out loud.
    on_gpu = [r["key"] for r in data["services"]
              if r["running"] and r.get("accelerator") == "GPU"]
    if len(on_gpu) > 1:
        problems.append(
            f"{len(on_gpu)} processes are on the GPU ({', '.join(on_gpu)}). They share "
            f"one unified pool with the CPU, and NvMap will not reclaim page cache for "
            f"either -- this is what makes `cudaMalloc failed` show up at startup. "
            f"WHISPER_USE_GPU=0 in config/whisper.env is the cheapest way to free it.")

    # A model engine whose pages are in swap is the single best predictor of a slow
    # turn on this board, and it is invisible in every other reading: the GPU reports
    # itself healthy, RSS looks reasonable, and the reply just takes ten seconds. Every
    # page the synthesiser touches has to come back from the swapfile first.
    swapped = [(r["key"], r["swap_mib"]) for r in data["services"]
               if r["running"] and (r.get("swap_mib") or 0) >= SWAP_WARN_MIB]
    if swapped:
        worst = ", ".join(f"{k} {v:.0f} MiB" for k, v in
                          sorted(swapped, key=lambda kv: -kv[1]))
        problems.append(
            f"paged out to swap: {worst}. This is latency you cannot tune away -- the "
            f"pages come back from {_swap_device()} on every utterance. The board is "
            f"over-committed: check `free -m` and ./scripts/models/list.sh, and shrink "
            f"a model or stop what else is resident.")

    print()
    if not problems:
        print(f"{GREEN}Everything running matches what is configured and chosen.{RESET}")
        return 0
    for problem in problems:
        print(f"{YELLOW}!{RESET} {problem}")
    print(f"\n{DIM}red = the process disagrees with the config, or something the "
          f"selected model needs is missing;  yellow = chosen but not applied, or "
          f"running with nothing reading it{RESET}")
    return 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv[1:])

    data = collect()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    return render(data)


if __name__ == "__main__":
    os.chdir(services.root())
    raise SystemExit(main(sys.argv))
