#!/usr/bin/env python3
"""Exercise every line of the serial contract, both sides, over a real serial port.

dev-docs/architecture/serial-protocol.md is implemented twice -- once in C on the
microcontroller, once in Python here. This drives the Python client against the C
firmware through an actual serial device and checks the wire behaviour matches the doc:
every command answered, the documented response shapes, the limits enforced, and timed
actions reporting DONE.

    python scripts/serial/protocol_test.py --sim          # host firmware core
    python scripts/serial/protocol_test.py --port /dev/ttyACM0
"""
from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys
import time
from pathlib import Path

from robot.core.serial_link import SerialLink

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tools" / "stm32_sim" / "stm32_sim"

passed = failed = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


async def run(port: str) -> None:
    events: list[dict] = []
    link = SerialLink(port, on_event=events.append)
    runner = asyncio.create_task(link.run())
    for _ in range(60):
        if link.connected:
            break
        await asyncio.sleep(0.05)
    if not link.connected:
        print(f"could not open {port}: {link.last_error}")
        sys.exit(1)

    try:
        print("\n-- documented commands are accepted --")
        r = await link.drive(0.5, 0.0, 0.0, 1500)
        check(r.ok and r.raw == "OK DRIVE", "DRIVE 0.5 0.0 0.0 1500", r.raw)
        check((await link.stop()).raw == "OK STOP", "STOP")
        check((await link.set_face("happy")).raw == "OK FACE", "FACE happy")
        check((await link.gesture("wave")).raw == "OK HAND", "HAND wave")
        check((await link.drive(0.0, 0.3, 0.0, 1000)).ok, "DRIVE strafe")
        check((await link.drive(0.0, 0.0, 1.2, 800)).ok, "DRIVE rotate")
        await link.stop()

        print("\n-- rejections carry a reason --")
        r = await link.set_face("sideways")
        check(not r.ok and r.reason == "unknown", "FACE sideways -> ERR unknown", r.raw)
        r = await link.gesture("backflip")
        check(not r.ok and r.reason == "unknown", "HAND backflip -> ERR unknown", r.raw)
        r = await link._command("FLY 1 2 3", "FLY")
        check(not r.ok and r.reason == "unknown", "unknown verb -> ERR unknown", r.raw)
        r = await link._command("DRIVE 0.5 0.0", "DRIVE")
        check(not r.ok and r.reason == "badargs", "too few args -> ERR badargs", r.raw)
        r = await link._command("DRIVE 99 0 0 100", "DRIVE")
        check(not r.ok and r.reason == "range", "over-limit -> ERR range", r.raw)

        print("\n-- the Jetson clamps before the wire --")
        r = await link.drive(99.0, 0.0, 0.0, 500)
        check(r.ok, "an absurd velocity is clamped here, not rejected there", r.raw)
        await link.stop()

        print("\n-- STATUS returns the documented STAT shape --")
        status = await link.request_status()
        check(status is not None, "STATUS answered")
        if status:
            stat_events = [e for e in events if e["type"] == "serial_status"]
            check(bool(stat_events), "STAT parsed into fields")
            check(status.motors in ("ok", "stall"), f"motors={status.motors}")
            check(status.uptime_ms > 0, f"up={status.uptime_ms}ms")

        print("\n-- bounded duration auto-stops and reports DONE --")
        events.clear()
        await link.drive(0.3, 0.0, 0.0, 400)
        await asyncio.sleep(1.0)
        done = [e for e in events if e["type"] == "serial_done" and e["verb"] == "DRIVE"]
        check(bool(done), "DONE DRIVE after the duration elapsed",
              f"saw {[e['type'] for e in events]}")

        print("\n-- WHEEL is bring-up only, and still bounded --")
        r = await link.wheel(0, 5.0, 300)
        check(r.ok and r.raw == "OK WHEEL", "WHEEL 0 5.0 300", r.raw)
        r = await link.wheel(9, 5.0, 300)
        check(not r.ok, "wheel index 9 refused before it reaches the wire", r.raw)
        await asyncio.sleep(0.6)

        print("\n-- the heartbeat is actually flowing --")
        # OK PING lines are unmatched by design; the link's last_rx proves they arrive.
        before = link.last_rx
        await asyncio.sleep(0.6)
        check(link.last_rx > before, "STM32 keeps answering while we PING")
    finally:
        await link.stop()
        runner.cancel()
        await link.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--sim", action="store_true")
    args = ap.parse_args()

    sim = None
    try:
        if args.sim:
            if not SIM.exists():
                print("simulator not built -- run tools/stm32_sim/build.sh")
                return 2
            log = open(ROOT / "run" / "stm32_sim_protocol.log", "w")
            sim = subprocess.Popen([str(SIM)], stdout=subprocess.PIPE, stderr=log, text=True)
            port = sim.stdout.readline().strip()
            print(f"firmware core on {port}")
        elif args.port:
            port = args.port
        else:
            print("give --port or --sim")
            return 2

        asyncio.run(run(port))
    finally:
        if sim and sim.poll() is None:
            sim.terminate()
            sim.wait(timeout=3)

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
