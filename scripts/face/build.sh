#!/usr/bin/env bash
# Build the robot's panel.
#
#   ./scripts/face/build.sh              release, for the robot's own screen (DRM/KMS)
#   ./scripts/face/build.sh desktop      a window on a developer laptop
#   ./scripts/face/build.sh --check      just the fast tests, no Slint, robot untouched
#
# Two things this script exists to get right, both of which have already bitten:
#
# DISK. The root filesystem is 94% full. Cargo's default CARGO_HOME (~917 MB already) and
# a Slint target/ (1.5-3 GB) both live there unless told otherwise. Everything goes on
# the DATA partition instead.
#
# MEMORY. The board routinely has under 300 MiB free with the stack up, and linking
# robot-face pulls 211 object files. Building with the LLM resident got the LLM
# OOM-killed twice. So this stops it first and puts it back afterwards, unless told not
# to -- and drops the page cache on the way back, because CUDA on Tegra cannot reclaim
# the gigabytes of object files a build leaves cached (see drop_model_cache.py --all).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

export CARGO_HOME="${CARGO_HOME:-$ROOT/.cache/cargo}"
# CARGO_TARGET_DIR as an environment variable, NOT only face/.cargo/config.toml. Cargo
# reads .cargo/config.toml from the CURRENT DIRECTORY upward, not from the manifest's
# directory -- so `cargo --manifest-path face/...` run from the repo root ignores it and
# quietly builds into face/target instead. That produced two 1.4 GB target trees and a
# binary the service could not find.
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$ROOT/.cache/cargo-target}"
export PATH="$HOME/.cargo/bin:$PATH"
mkdir -p "$CARGO_HOME"

# The pure crate needs no Slint, no display and no memory to speak of. It is where every
# interesting decision lives, so this is the loop to use while iterating.
if [ "${1:-}" = "--check" ]; then
  exec cargo test --manifest-path "$ROOT/face/Cargo.toml" -p robot-face-protocol -j2
fi

FEATURES="${1:-kms}"
KEEP_LLM="${KEEP_LLM:-0}"

stopped=""
if [ "$KEEP_LLM" != "1" ] && "$ROOT/scripts/run_stack.sh" status 2>/dev/null | grep -q '^robot-llm *[0-9]'; then
  echo "==> stopping the LLM so rustc has room (KEEP_LLM=1 to skip)"
  "$ROOT/scripts/run_stack.sh" svc stop llm >/dev/null 2>&1 && stopped=1
fi
python3 "$ROOT/scripts/measure/drop_model_cache.py" --all >/dev/null 2>&1

echo "==> building robot-face [$FEATURES]"
cargo build --manifest-path "$ROOT/face/Cargo.toml" -p robot-face \
  --release --no-default-features --features "$FEATURES" -j2
status=$?

if [ -n "$stopped" ]; then
  echo "==> restarting the LLM"
  python3 "$ROOT/scripts/measure/drop_model_cache.py" --all >/dev/null 2>&1
  "$ROOT/scripts/run_stack.sh" svc start llm 2>&1 | tail -1
fi

[ $status -eq 0 ] && echo "==> $ROOT/.cache/cargo-target/release/robot-face"
exit $status
