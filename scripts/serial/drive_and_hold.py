#!/usr/bin/env python3
"""Start a DRIVE and then just keep the heartbeat going. Nothing else.

Exists to be killed. `scripts/serial/watchdog_test.py` spawns this, waits for the
wheels to turn, then SIGKILLs it -- which is the closest honest simulation of the
Jetson being OOM-killed mid-motion. Deliberately has no signal handlers and no
cleanup: if this process could stop the robot on its way out, the test would prove
nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from robot.core.serial_link import SerialLink


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--vx", type=float, default=0.5)
    ap.add_argument("--vy", type=float, default=0.0)
    ap.add_argument("--omega", type=float, default=0.0)
    ap.add_argument("--ms", type=int, default=10000,
                    help="drive duration; long, so the watchdog is what stops it")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    link = SerialLink(args.port)
    runner = asyncio.create_task(link.run())
    for _ in range(50):
        if link.connected:
            break
        await asyncio.sleep(0.05)
    if not link.connected:
        print("could not open serial port", file=sys.stderr)
        return 1

    resp = await link.drive(args.vx, args.vy, args.omega, args.ms)
    print(f"DRIVE -> {resp.raw or resp.reason}", flush=True)
    if not resp.ok:
        return 1

    print("driving; heartbeat running. kill -9 me.", flush=True)
    await runner       # heartbeat forever, until something kills this process
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
