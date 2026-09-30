#!/usr/bin/env bash
# Build the host simulator against the real firmware sources.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
FW="$HERE/../../firmware/stm32"
OUT="${1:-$HERE/stm32_sim}"
gcc -std=c11 -Wall -Wextra -O1 -I"$FW/include" \
    "$FW/src/protocol.c" "$FW/src/mecanum.c" "$FW/src/watchdog.c" "$FW/src/robot.c" \
    "$HERE/main.c" -lm -o "$OUT"
echo "built $OUT"
