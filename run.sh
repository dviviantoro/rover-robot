#!/usr/bin/env bash
# Launcher for the robot.
#
#   ./run.sh all         everything, with the dashboard in a browser
#   ./run.sh headless     everything, no UI at all -- how the robot ships
#   ./run.sh slint        everything, with the panel on the robot's own screen
#
#   ./run.sh stop         stop everything
#   ./run.sh status       what is running, and what it has loaded
#   ./run.sh info         the long version
#
# The three modes differ ONLY in which face is started. The voice pipeline -- LLM, ASR,
# TTS, orchestrator, knowledge -- is identical in all three, because both UIs are
# independent websocket clients of robot-core and neither is depended on by anything.
# system-overview.md: "a UI bug must not be able to stop the robot."
#
# Nothing is configured here. Everything tuned in the dashboard is persisted -- hot
# settings to run/settings.json, which robot-core replays before the first turn, and
# restart-scoped choices to config/*.env, written when you press Apply -- so every mode
# reads the same config. The banner below prints what that is.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
ROOT="$(pwd)"

MODE="${1:-}"; [ $# -gt 0 ] && shift
PASS=("$@")

usage() {
  cat <<'EOF'

  ./run.sh all         everything + the browser dashboard   (http://<ip>:8081)
  ./run.sh headless    everything, no UI                    (how the robot ships)
  ./run.sh slint       everything + the panel on the robot's own screen

  ./run.sh stop | status | info

  Extra flags are passed through, e.g.:
      ./run.sh headless --sim        also start the simulated STM32
      ./run.sh all --no-asr          skip the microphone
      ./run.sh slint --check         print the config, start nothing

EOF
}

info() {
  cat <<'EOF'

WHICH MODE
  all        The voice pipeline plus robot-ui, the browser dashboard on :8081. Nine
             pages: Chat, Headless, Knowledge, Memory, Monitor, Logs, Services, Config.
             This is the one to use when you are setting the robot up or debugging it.
             Costs ~34 MiB and needs a browser somewhere on the network.

  headless   The voice pipeline and nothing else. No browser, no panel. This is how the
             robot is meant to run in production: speak to it, it answers. Configure it
             once from `all` mode, then never start a UI again -- the settings persist.

  slint      The voice pipeline plus robot-face, a native panel rendered straight to
             DRM/KMS on a screen attached to the robot. No desktop, no compositor, no
             X11, no Wayland -- AGENTS.md constraint 5 still holds. Shows the
             conversation, the voice state, the level meter and the robot's expression;
             it does NOT replace the dashboard's operator pages. Costs ~11 MiB, needs a
             connected display, and waits patiently if there is not one.

  The dashboard and the panel are both optional and both independent websocket clients
  of robot-core, so nothing stops you running both -- `./scripts/run_stack.sh start
  --ui --face`. These modes do not, because on a 7620 MiB board the memory is usually
  better spent on a larger model.

WHAT DECIDES BEHAVIOUR
  config/*.env      per-service settings, written by the dashboard's Apply button
  run/settings.json anything tuned live in the dashboard; replayed at startup
  .env              secrets only, gitignored (AGENTS.md constraint 8)

AFTERWARDS
  ./scripts/measure/whats_running.py    what is loaded, and on GPU or CPU
  tail -f .cache/tmp/core.log           the conversation as it happens
  ./run.sh stop

EOF
}

# What this run will actually use. Read through the same modules the services read, not
# by grepping the files -- two implementations of "what is configured" is two answers.
banner() {
  local mode="$1"
  "$ROOT/.venv/bin/python" - "$mode" <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, "src")
for env in sorted(Path("config").glob("*.env")):
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from robot.core.settings import BY_KEY, Settings
from robot.core.app import reply_budget

mode = sys.argv[1]
saved = {k: v for k, v in Settings().all().items() if v is not None}

def show(label, value, note=""):
    print(f"  {label:24} {value}{'   ' + note if note else ''}")

face = {"all": "browser dashboard on :8081",
        "headless": "none -- speak to it",
        "slint": "panel on the robot's own screen"}.get(mode, mode)

print(f"\nRobot, {mode} mode\n" + "-" * 62)
show("Face", face)
show("LLM", Path(os.environ.get("LLM_MODEL", "?")).name)
ctx = int(os.environ.get("LLM_CTX", "2048"))
prompt_budget = max(256, int(ctx * 0.66))
pinned = saved.get("max_tokens")
reply = pinned if pinned is not None else reply_budget(
    os.environ.get("CORE_MAX_TOKENS", "auto"), ctx, prompt_budget)
show("Context", f"{ctx} tokens = prompt {prompt_budget} + reply {int(reply)}"
                + ("   PINNED from the dashboard" if pinned is not None else ""))
gpu_req = []
if os.environ.get("WHISPER_REQUIRE_GPU", "1") == "1":
    gpu_req.append("STT")
if os.environ.get("TTS_REQUIRE_GPU", "1") == "1" and os.environ.get("TTS_USE_GPU") == "1":
    gpu_req.append("TTS")
show("Listening", f"whisper: {Path(os.environ.get('WHISPER_MODEL') or '?').name}")
tts = os.environ.get("TTS_ENGINE", "piper")
show("Speaking", tts + (f": {os.environ.get('SUPERTONIC_VOICE', 'M1')}" if tts == "supertonic" else ""))
show("Microphone", f"{saved.get('listen_mode', os.environ.get('CORE_LISTEN_MODE', 'no_interrupt'))}"
                   f", tail {saved.get('listen_tail_ms', os.environ.get('CORE_LISTEN_TAIL_MS', 700))} ms")
show("Languages", os.environ.get("ASR_LANGUAGES", "id,en,ar"))
gate = saved.get("asr_noise_gate_db", os.environ.get("ASR_NOISE_GATE_DB", 6.0))
suppression = saved.get("asr_denoise", os.environ.get("ASR_DENOISE", "off"))
show("Noisy rooms", f"suppression {suppression}, drop below room{float(gate):+.0f} dB"
                    f", whisper no-speech > "
                    f"{float(saved.get('asr_max_no_speech', os.environ.get('ASR_MAX_NO_SPEECH', 0.6))):.2f}")
show("GPU required", ", ".join(gpu_req) + " -- will not run on the CPU" if gpu_req
                      else "nothing -- CPU fallbacks allowed")
show("Body", os.environ.get("STM32_PORT") or "none -- no heartbeat, no motion")

if mode == "slint":
    # The panel is the one mode with a hardware precondition, so check it here rather
    # than let the user find out from a log line.
    drm = sorted(Path("/sys/class/drm").glob("card*-*"))
    live = [d.name for d in drm if (d / "status").read_text().strip() == "connected"]
    if live:
        show("Display", ", ".join(live))
    elif drm:
        show("Display", "none connected -- the panel will wait for one")
    else:
        show("Display", "NO CONNECTORS -- is nvidia-drm loaded?")

tuned = [k for k, v in saved.items() if BY_KEY.get(k) and v != BY_KEY[k].default]
print("-" * 62)
print(f"  from the dashboard: {', '.join(sorted(tuned))}" if tuned
      else "  nothing tuned from the dashboard -- running env-file defaults")
print()
PY
}

# `--check` prints the banner and starts nothing. Useful in every mode, not just
# headless: "did my change stick?" is the same question whichever face you run.
check_only() {
  for arg in ${PASS[@]+"${PASS[@]}"}; do
    [ "$arg" = "--check" ] && return 0
  done
  return 1
}

strip_check() {
  local out=()
  for arg in ${PASS[@]+"${PASS[@]}"}; do
    [ "$arg" = "--check" ] || out+=("$arg")
  done
  PASS=(${out[@]+"${out[@]}"})
}

start() {
  local mode="$1"; shift
  banner "$mode"
  if check_only; then
    echo "  --check: nothing started."
    exit 0
  fi
  strip_check
  "$ROOT/scripts/run_stack.sh" start "$@" ${PASS[@]+"${PASS[@]}"} || exit 1
}

case "$MODE" in
  all)
    start all --ui
    echo
    echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):${UI_PORT:-8081}"
    echo "  ./run.sh stop   to stop everything"
    ;;

  headless)
    start headless
    cat <<'EOF'

  Headless. Speak to it -- there is no page to open.

    ./scripts/measure/whats_running.py    what is loaded, and on GPU or CPU
    tail -f .cache/tmp/core.log           the conversation as it happens
    ./run.sh stop
EOF
    ;;

  slint)
    # The panel needs a binary. Say so plainly rather than starting the whole voice
    # stack and then failing on the last step.
    if [ ! -x "$ROOT/.cache/cargo-target/release/robot-face" ] \
       && [ ! -x "$ROOT/.cache/cargo-target/debug/robot-face" ]; then
      echo "robot-face is not built yet:" >&2
      echo "    ./scripts/face/build.sh        (~8 min; stops the LLM while it runs)" >&2
      exit 1
    fi
    start slint --face
    cat <<'EOF'

  The panel is on the robot's screen.

    tail -f .cache/tmp/face.log           what the panel is doing
    ./run.sh stop
EOF
    ;;

  stop)   exec "$ROOT/scripts/run_stack.sh" stop ;;
  status) exec "$ROOT/scripts/run_stack.sh" status ;;
  info)   info ;;
  ""|-h|--help|help) usage ;;
  *) echo "unknown mode: $MODE" >&2; usage; exit 2 ;;
esac
