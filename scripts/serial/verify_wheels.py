#!/usr/bin/env python3
"""Verify each wheel's direction individually, before trusting composite motion.

dev-docs/architecture/serial-protocol.md § Verifying wheel direction, and ROADMAP M5.

Mecanum kinematics map three body velocities onto four wheels, so no DRIVE command can
isolate a single wheel -- a wrong sign shows up as "the robot veers" and is miserable to
attribute. This drives one wheel at a time with WHEEL and asks what you saw.

    ⚠ PUT THE ROBOT ON BLOCKS. Every wheel gets driven.

At the end it prints the mecanum_config_t.direction[] values to paste into
firmware/stm32/src/mecanum.c, and the table to tick in serial-protocol.md.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from robot.core.serial_link import WHEEL_NAMES, SerialLink

PROMPT = """
  Wheel {i} ({name}) is about to spin at {speed:+.1f} rad/s for {ms} ms.
  Watch the TOP of the wheel and answer which way it drove the robot.
"""


async def ask(question: str, options: dict[str, str]) -> str:
    keys = "/".join(options)
    loop = asyncio.get_running_loop()
    while True:
        answer = (await loop.run_in_executor(
            None, input, f"  {question} [{keys}] ")).strip().lower()
        if answer in options:
            return answer
        print(f"    please answer one of: {keys}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="e.g. /dev/ttyACM0, or a simulator PTY")
    ap.add_argument("--speed", type=float, default=5.0, help="rad/s")
    ap.add_argument("--ms", type=int, default=1200)
    ap.add_argument("--yes", action="store_true", help="skip the on-blocks confirmation")
    args = ap.parse_args()

    if not args.yes:
        print("\n  ⚠  Every wheel will be driven. Is the robot on blocks, wheels free?")
        if (await ask("ready?", {"y": "", "n": ""})) != "y":
            print("  aborted.")
            return 1

    link = SerialLink(args.port)
    runner = asyncio.create_task(link.run())
    for _ in range(50):
        if link.connected:
            break
        await asyncio.sleep(0.05)
    if not link.connected:
        print(f"  could not open {args.port}: {link.last_error}")
        return 1

    directions: list[float] = []
    try:
        for i, name in enumerate(WHEEL_NAMES):
            print(PROMPT.format(i=i, name=name, speed=args.speed, ms=args.ms))
            input("  press Enter to spin...")
            resp = await link.wheel(i, args.speed, args.ms)
            if not resp.ok:
                print(f"  STM32 rejected it: {resp.reason}")
                return 1
            await asyncio.sleep(args.ms / 1000 + 0.4)

            answer = await ask(
                f"which way did wheel {i} ({name}) try to drive the robot?",
                {"f": "forward", "b": "backward", "n": "did not move"},
            )
            if answer == "n":
                print(f"    !! wheel {i} did not move. Check wiring, the driver's STBY")
                print(f"       pin, and the encoder before going further.")
                return 1
            # The convention: a positive command must turn the wheel so that all four
            # positive together drive the robot forward.
            directions.append(1.0 if answer == "f" else -1.0)
            print(f"    recorded: direction[{i}] = {directions[-1]:+.0f}")

        await link.stop()
    finally:
        runner.cancel()
        await link.close()

    print("\n" + "=" * 66)
    print("  Paste into mecanum_default_config() in firmware/stm32/src/mecanum.c:\n")
    for i, (name, d) in enumerate(zip(WHEEL_NAMES, directions)):
        print(f"      cfg->direction[{i}] = {d:+.1f}f;   /* {name} */")
    print("\n  And tick the table in dev-docs/architecture/serial-protocol.md:\n")
    print("  | Wheel | Index | Positive command should turn | Verified | `direction[]` |")
    print("  |---|---|---|---|---|")
    for i, (name, d) in enumerate(zip(WHEEL_NAMES, directions)):
        print(f"  | {name.replace('_', ' ').title()} | {i} | forward | ☑ verified | `{d:+.0f}` |")
    print("=" * 66)
    print("\n  Then re-flash and confirm the composite motions in the same section:")
    print("    DRIVE 0.3 0 0 1000   -> straight forward")
    print("    DRIVE 0 0.3 0 1000   -> strafes right, no rotation")
    print("    DRIVE 0 0 1.0 1000   -> rotates in place, no translation")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
