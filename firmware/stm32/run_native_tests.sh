#!/usr/bin/env bash
# Compile and run the portable firmware core on the host.
# Needs only gcc -- no PlatformIO, no ARM toolchain, no hardware.
set -euo pipefail
cd "$(dirname "$0")"
OUT="${TMPDIR:-/tmp}/robot_firmware_tests"
gcc -std=c11 -Wall -Wextra -Werror -O1 \
    -Iinclude src/protocol.c src/mecanum.c src/watchdog.c src/robot.c src/pid.c \
    test/test_native/test_main.c -lm -o "$OUT"
exec "$OUT"
