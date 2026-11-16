#!/usr/bin/env python3
"""THE safety test: kill the Jetson mid-drive and prove the wheels stop.

dev-docs/architecture/serial-protocol.md § Heartbeat watchdog, and ROADMAP M5:
"Test the watchdog by killing the Jetson process mid-drive -- motors must stop."

    1. start the STM32 firmware core
    2. start a separate Jetson-side process: heartbeat + DRIVE
    3. confirm the wheels are actually turning
    4. SIGKILL the Jetson process -- no cleanup, no STOP, no chance to be polite
    5. watch the motors

Step 4 is the point. SIGKILL cannot be caught, so nothing on the Jetson side
participates in stopping the robot. If the wheels stop, it is because the STM32
decided to stop them.

Against the simulator (`--sim`) this exercises the real firmware C -- protocol.c,
watchdog.c, robot.c compiled for the host. Against `--port /dev/ttyACM*` it exercises
the same code on the actual microcontroller.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tools" / "stm32_sim" / "stm32_sim"
TIMEOUT_MS = 500          # what the firmware enforces
TOLERANCE_MS = 400        # scheduling slack on a loaded Linux box


def read_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def wait_for(predicate, timeout_s: float, poll_s: float = 0.01):
    """Poll until predicate returns a truthy value. Returns (value, elapsed_s)."""
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        value = predicate()
        if value:
            return value, time.monotonic() - start
        time.sleep(poll_s)
    return None, time.monotonic() - start


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def manual_check(port: str, driver: "subprocess.Popen") -> int:
    """The watchdog test on a REAL board, where the script cannot see the wheels.

    The simulator writes run/stm32_state.json; a real board does not, so this used to fail at
    "wheels never started turning" (Known issue #21). Here a person is the observer: a scope or
    an LED on the PWM or driver-standby pin, or the wheels off the ground. The script still does
    the part that matters, which is killing the Jetson side with SIGKILL, no cleanup, no STOP.
    """
    def ask(question: str) -> bool:
        return input(f"    {question} [y/N] ").strip().lower() in ("y", "yes")

    print("\n  MANUAL MODE: nothing here can see the wheels, so you are the sensor.")
    print("  Watch ONE of: a scope or LED on a motor PWM / driver-standby pin, or the wheels")
    print("  (robot ON BLOCKS, or the motors unpowered and only the pin watched).")
    time.sleep(1.0)
    if not ask("Is the observable ACTIVE now (pin toggling / wheels turning)?"):
        print("    FAIL: nothing to observe, so the test cannot prove anything.")
        driver.kill()
        return 1
    print("\n[4] SIGKILL -> the Jetson-side process (no cleanup, no STOP command)")
    kill_at = time.monotonic()
    os.kill(driver.pid, signal.SIGKILL)
    driver.wait(timeout=5)
    stopped = ask("It should stop BY ITSELF within about half a second. Did it?")
    print(f"    (you answered {1000 * (time.monotonic() - kill_at):.0f} ms after the kill; "
          "that includes your reaction time, so it is an upper bound, not a measurement)")
    time.sleep(1.0)
    held = ask("One second later: is it STILL stopped (no auto-resume)?")
    print()
    if stopped and held:
        print("PASS (observed by a person): the body stopped the motors by itself.")
        return 0
    print("FAIL: do not trust this body with motors until it stops on its own.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="real STM32 port, e.g. /dev/ttyACM0")
    ap.add_argument("--sim", action="store_true", help="run against the host simulator")
    ap.add_argument("--drive-ms", type=int, default=10000)
    args = ap.parse_args()

    if not args.sim and not args.port:
        print("give --port for real hardware, or --sim for the host firmware core")
        return 2

    run_dir = ROOT / "run"
    run_dir.mkdir(exist_ok=True)
    state_path = run_dir / "stm32_state.json"
    state_path.unlink(missing_ok=True)

    sim_proc = None
    sim_log = open(run_dir / "stm32_sim.log", "w")
    try:
        # ---- 1. the STM32 side ------------------------------------------
        if args.sim:
            if not SIM.exists():
                print(f"simulator not built -- run tools/stm32_sim/build.sh")
                return 2
            port_file = run_dir / "stm32_port"
            port_file.unlink(missing_ok=True)
            sim_proc = subprocess.Popen(
                [str(SIM), "--state", str(state_path), "--port-file", str(port_file)],
                stdout=subprocess.PIPE, stderr=sim_log, text=True,
            )
            port = sim_proc.stdout.readline().strip()
            print(f"[1] firmware core running, serial port {port}")
        else:
            port = args.port
            print(f"[1] using real STM32 on {port}")
            print("    manual mode: a person observes the wheels or a pin (no state file on real hardware)")
            if not sys.stdin.isatty():
                print("    --port needs an interactive terminal: it asks what you see")
                return 2

        # ---- 2. the Jetson side, as its own killable process -------------
        driver = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "serial" / "drive_and_hold.py"),
             "--port", port, "--vx", "0.5", "--ms", str(args.drive_ms)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        print(f"[2] Jetson-side process started, pid {driver.pid} "
              f"(heartbeat every 200 ms)")

        if not args.sim:
            return manual_check(port, driver)

        # ---- 3. confirm the wheels are actually turning ------------------
        moving, elapsed = wait_for(
            lambda: (s := read_state(state_path)) and s.get("moving") and s,
            timeout_s=10.0,
        )
        if not moving:
            print("[3] FAIL: wheels never started turning")
            print(driver.stdout.read() if driver.poll() is not None else "")
            return 1
        w = moving["wheels"]
        print(f"[3] wheels TURNING after {elapsed*1000:.0f} ms: "
              f"FL={w['front_left']:.2f} FR={w['front_right']:.2f} "
              f"RL={w['rear_left']:.2f} RR={w['rear_right']:.2f}")

        # ---- 4. kill the brain, hard ------------------------------------
        print(f"[4] SIGKILL -> pid {driver.pid}  (no cleanup, no STOP command)")
        kill_at = time.monotonic()
        os.kill(driver.pid, signal.SIGKILL)
        driver.wait(timeout=5)
        assert not alive(driver.pid), "driver survived SIGKILL"
        print(f"    confirmed dead, exit {driver.returncode} "
              f"(-{signal.SIGKILL} = killed)")

        # ---- 5. the motors must stop, on the STM32's own authority -------
        stopped, _ = wait_for(
            lambda: (s := read_state(state_path)) and not s.get("moving") and s,
            timeout_s=3.0,
        )
        stop_ms = (time.monotonic() - kill_at) * 1000

        print()
        if not stopped:
            final = read_state(state_path)
            print(f"*** FAIL: wheels still turning {stop_ms:.0f} ms after the kill")
            print(f"    state: {final}")
            return 1

        w = stopped["wheels"]
        print(f"[5] wheels STOPPED {stop_ms:.0f} ms after the kill")
        print(f"    FL={w['front_left']:.2f} FR={w['front_right']:.2f} "
              f"RL={w['rear_left']:.2f} RR={w['rear_right']:.2f}")

        ok = True
        if stop_ms > TIMEOUT_MS + TOLERANCE_MS:
            print(f"    !! slower than the {TIMEOUT_MS} ms contract "
                  f"(+{TOLERANCE_MS} ms tolerance)")
            ok = False
        if stop_ms < TIMEOUT_MS - TOLERANCE_MS:
            print(f"    !! suspiciously fast -- did something else stop the motors?")
            ok = False

        # ---- 6. and it must NOT come back on its own ---------------------
        time.sleep(1.0)
        after = read_state(state_path)
        if after and after.get("moving"):
            print("    !! motion resumed on its own -- the contract forbids this")
            ok = False
        else:
            print("[6] still stopped 1 s later; motion did not auto-resume")

        print()
        print("PASS: the STM32 stopped the motors by itself." if ok else "FAIL")
        return 0 if ok else 1

    finally:
        if sim_proc and sim_proc.poll() is None:
            sim_proc.terminate()
            try:
                sim_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                sim_proc.kill()
        sim_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
