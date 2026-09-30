# Source this before any pip install / model download / build in this project.
#   source scripts/bringup/env.sh
#
# The root filesystem is ~94% full (7 GB free). Everything large -- pip's cache,
# build temp dirs, HuggingFace downloads -- defaults to / and will fill it.
# This pins all of it to the DATA partition (260 GB free).

ROBOT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export ROBOT_ROOT
export PIP_CACHE_DIR="$ROBOT_ROOT/.cache/pip"
export TMPDIR="$ROBOT_ROOT/.cache/tmp"
export HF_HOME="$ROBOT_ROOT/.cache/hf"
export ROBOT_MODELS="$ROBOT_ROOT/models"

# ARM64 wheels are frequently missing from PyPI -- AGENTS.md says try NVIDIA's index first.
export PIP_EXTRA_INDEX_URL="https://pypi.jetson-ai-lab.io/jp6/cu126"

mkdir -p "$PIP_CACHE_DIR" "$TMPDIR" "$HF_HOME" "$ROBOT_MODELS"
[ -f "$ROBOT_ROOT/.venv/bin/activate" ] && . "$ROBOT_ROOT/.venv/bin/activate"
