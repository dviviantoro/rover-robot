#!/usr/bin/env bash
# Start or stop the whole robot stack by hand, for development.
# Production uses systemd (scripts/install_services.sh); this exists so a dev run
# can be started and -- importantly -- fully stopped, including child processes.
#
#   ./scripts/run_stack.sh start [--ctx 2048] [--wav FILE] [--sim] [--ui] [--face]
#   ./scripts/run_stack.sh stop
#   ./scripts/run_stack.sh status
#
# One service at a time, which is what the dashboard's Services panel drives through
# robot.common.services when no systemd unit is installed:
#
#   ./scripts/run_stack.sh svc start|stop|restart llm|embed|tts|core|asr|body|ui|face
#
# The per-service commands are the SAME spawn functions the full start uses, so a
# service restarted from the dashboard gets the identical command line, environment and
# oom_score_adj it would have got at boot. Two copies of that would drift within a week.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
RUN="$ROOT/run"; LOGS="$ROOT/.cache/tmp"; mkdir -p "$RUN" "$LOGS"
PIDFILE="$RUN/stack.pids"

CMD="${1:-}"; [ $# -gt 0 ] && shift
CTX=""; WAV=""; SIM=""; UI=""; FACE=""; NO_ASR=""; NO_EMBED=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ctx) CTX="${2:-}"; shift 2 ;;
    --wav) WAV="${2:-}"; shift 2 ;;
    --sim) SIM=1; shift ;;
    --no-asr) NO_ASR=1; shift ;;
    --no-embed) NO_EMBED=1; shift ;;
    --ui) UI=1; shift ;;
    --face) FACE=1; shift ;;
    -*) shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

# ---------------------------------------------------------------- bookkeeping

track() { untrack "$1"; echo "$1 $2" >> "$PIDFILE"; }

untrack() {
  [ -f "$PIDFILE" ] || return 0
  local keep; keep="$(grep -v "^$1 " "$PIDFILE" 2>/dev/null)"
  printf '%s\n' "$keep" | sed '/^$/d' > "$PIDFILE"
}

tracked_pid() { [ -f "$PIDFILE" ] && awk -v n="$1" '$1==n{print $2}' "$PIDFILE" | tail -1; }

# Forget processes that are already gone, so the file describes the stack as it is.
prune_pidfile() {
  [ -f "$PIDFILE" ] || return 0
  local name pid keep=""
  while read -r name pid; do
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && keep+="$name $pid"$'\n'
  done < "$PIDFILE"
  printf '%s' "$keep" > "$PIDFILE"
}

# Every process belonging to one service, found by reading /proc directly.
#
# `pgrep -f` matches whole command lines, so an editor, a `less` or a log tail that
# merely MENTIONS a service matches too -- and this function's callers kill what it
# returns. The executable is checked as well as the command line for that reason.
# The same three-part rule lives in src/robot/common/services.py; keep them in step.
pids_for() {
  local name="$1" pid cmd exe
  for d in /proc/[0-9]*; do
    pid="${d#/proc/}"
    [ "$pid" = "$$" ] && continue
    # The braces matter: a process that exits mid-scan makes the redirection itself
    # fail, and bash reports that before any `2>` on the command inside would apply.
    cmd="$( { tr '\0' ' ' < "$d/cmdline"; } 2>/dev/null )" || continue
    [ -n "${cmd// /}" ] || continue
    exe="$(readlink -f "$d/exe" 2>/dev/null)" || continue
    case "$name" in
      llm)   [[ "$cmd" == *"bin/llama-server"* && "$cmd" != *"--embeddings"* && "$exe" == *llama-server ]] || continue ;;
      embed) [[ "$cmd" == *"bin/llama-server"* && "$cmd" == *"--embeddings"*  && "$exe" == *llama-server ]] || continue ;;
      tts)   [[ "$cmd" == *"robot.tts.service"* && "$exe" == *python* ]] || continue ;;
      asr)   [[ "$cmd" == *"robot.asr.service"* && "$exe" == *python* ]] || continue ;;
      whisper) [[ "$cmd" == *"bin/whisper-server"* && "$exe" == *whisper-server ]] || continue ;;
      core)  [[ "$cmd" == *"robot.core.app"*    && "$exe" == *python* ]] || continue ;;
      ui)    [[ "$cmd" == *"robot.ui.app"*      && "$exe" == *python* ]] || continue ;;
      face)  [[ "$cmd" == *"robot-face"*         && "$exe" == *robot-face ]] || continue ;;
      body)  [[ "$cmd" == *"stm32_sim"*         && "$exe" == *stm32_sim ]] || continue ;;
      *) continue ;;
    esac
    echo "$pid"
  done
}

running() { [ -n "$(pids_for "$1")" ]; }

# --------------------------------------------------------------- environment

ENV_LOADED=""
load_env() {
  [ -n "$ENV_LOADED" ] && return 0
  . "$ROOT/scripts/bringup/env.sh"
  set -a; . "$ROOT/config/llm.env"; . "$ROOT/config/tts.env"
  . "$ROOT/config/asr.env"; . "$ROOT/config/core.env"
  [ -f "$ROOT/config/embed.env" ] && . "$ROOT/config/embed.env"
  [ -f "$ROOT/config/whisper.env" ] && . "$ROOT/config/whisper.env"
  [ -f "$ROOT/config/ui.env" ] && . "$ROOT/config/ui.env"
  # Credentials and the websocket token live in .env, which is gitignored.
  [ -f "$ROOT/.env" ] && . "$ROOT/.env"; set +a
  # A simulated body writes its PTY path here. Without this a `svc restart core`
  # would bring the orchestrator back with STM32_PORT empty -- it would run happily
  # and silently without a body, which looks exactly like a broken serial link.
  if [ -z "${STM32_PORT:-}" ] && [ -s "$RUN/stm32_port" ] && running body; then
    STM32_PORT="$(cat "$RUN/stm32_port")"; export STM32_PORT
  fi
  ENV_LOADED=1
}

# -------------------------------------------------------------------- spawns
#
# One function per service. Each is the single definition of how that service is
# started: command line, log file and oom_score_adj. `start` calls them in order;
# `svc start` calls exactly one.

spawn_body() {
  [ -x "$ROOT/tools/stm32_sim/stm32_sim" ] || "$ROOT/tools/stm32_sim/build.sh" >/dev/null
  rm -f "$RUN/stm32_port"
  setsid "$ROOT/tools/stm32_sim/stm32_sim" \
    --port-file "$RUN/stm32_port" --state "$RUN/stm32_state.json" \
    > "$LOGS/sim_out" 2> "$LOGS/stm32_sim.log" &
  track body $!
  for _ in $(seq 1 40); do [ -s "$RUN/stm32_port" ] && break; sleep 0.1; done
  STM32_PORT="$(cat "$RUN/stm32_port" 2>/dev/null)"
  export STM32_PORT
  echo "  simulated body on ${STM32_PORT:-?}"
}

spawn_llm() {
  # Drop the page cache held by EVERY model file, not just this one. CUDA on Tegra will
  # not force the kernel to reclaim page cache, so a service restarting finds gigabytes
  # of it held and dies on cudaMalloc -- see TROUBLESHOOTING.md. The systemd unit does
  # this in ExecStartPre; a dev restart needs it just as much.
  #
  # It used to pass "$LLM_MODEL", dropping only the model about to be loaded. That is
  # not where the pressure comes from: it is the model just UNLOADED, the whisper and
  # Supertonic files, and anything recently downloaded. Measured while swapping models
  # for the bake-off -- Qwen3-4B loaded its weights and then failed with "failed to
  # allocate buffer for kv cache", with 1.3 GB of other models' pages held in cache.
  python3 "$ROOT/scripts/measure/drop_model_cache.py" --all >/dev/null 2>&1
  setsid "$ROOT/vendor/llama.cpp/build/bin/llama-server" \
    --model "$LLM_MODEL" --ctx-size "${CTX:-$LLM_CTX}" --n-gpu-layers "$LLM_NGL" \
    --flash-attn "$LLM_FLASH_ATTN" --parallel "$LLM_PARALLEL" \
    --host "$LLM_HOST" --port "$LLM_PORT" --no-webui \
    > "$LOGS/llama-server.log" 2>&1 &
  track llm $!
  echo 500 > "/proc/$!/oom_score_adj" 2>/dev/null
  echo "  llama-server starting (${LLM_MODEL##*/}, ctx ${CTX:-$LLM_CTX})..."
}

# Wait for the LLM before starting anything else. It allocates through CUDA, which on
# Tegra cannot reclaim memory another process has already taken -- start the ASR
# (~1.2 GB) alongside it and the KV cache allocation loses the race.
# Bounded at roughly `tries` seconds: the dashboard runs this through
# robot.common.services with its own timeout, and a wait longer than that would be
# reported as "did not start" while the model was still loading perfectly well.
# llama-server takes ~45 s in the worst measured case (M9).
# `curl -s` exits 0 for ANY HTTP response, and llama-server answers /health with
# 503 {"error":{"message":"Loading model"}} from the moment it binds the port. So a bare
# exit-status check reports "healthy" the second the socket opens and the model is still
# minutes from loaded. Measured on this board: 503 at t=2 s, {"status":"ok"} at t=7 s.
#
# That five-second lie is not cosmetic. Whatever runs next -- whisper, on the GPU -- takes
# the memory llama-server has not finished allocating, and the KV cache loses the race:
#
#   ggml_backend_cuda_buffer_type_alloc_buffer: allocating 288.00 MiB: cudaMalloc failed
#   llama_init_from_model: failed to allocate buffer for kv cache
#
# and the stack comes up with no LLM at all, answering the unavailable line to everything,
# after printing "llama-server healthy". Match on the body, the way wait_whisper does.
wait_llm() {
  local tries="${1:-120}"
  for _ in $(seq 1 "$tries"); do
    curl -s -m 1 "http://${LLM_HOST}:${LLM_PORT}/health" 2>/dev/null | grep -q '"ok"' && {
      echo "  llama-server healthy"; return 0; }
    running llm || break
    sleep 1
  done
  echo "  !! llama-server did not come up -- see $LOGS/llama-server.log" >&2
  tail -3 "$LOGS/llama-server.log" >&2
  return 1
}

spawn_embed() {
  if [ -z "${EMBED_MODEL:-}" ] || [ ! -f "$EMBED_MODEL" ]; then
    echo "  no embedding model -- running without retrieval (run scripts/models/fetch_embed.sh)"
    export EMBED_URL=""
    return 1
  fi
  # All model pages, not just this one -- see spawn_llm for why.
  python3 "$ROOT/scripts/measure/drop_model_cache.py" --all >/dev/null 2>&1
  CUDA_VISIBLE_DEVICES="" setsid "$ROOT/vendor/llama.cpp/build/bin/llama-server" \
    --model "$EMBED_MODEL" --embeddings --pooling "$EMBED_POOLING" \
    --ctx-size "$EMBED_CTX" --batch-size "$EMBED_BATCH" --ubatch-size "$EMBED_BATCH" \
    --parallel "$EMBED_PARALLEL" --n-gpu-layers "$EMBED_NGL" \
    --host "$EMBED_HOST" --port "$EMBED_PORT" --no-webui \
    > "$LOGS/embed.log" 2>&1 &
  track embed $!
  echo 400 > "/proc/$!/oom_score_adj" 2>/dev/null
  echo "  embedding server starting on :$EMBED_PORT"
}

# "Started" is not "can answer": llama-server takes seconds to load the model, and the
# caller here may be robot-core waking it for an ingestion job that is about to POST to
# it. Reporting success early made the first document of every wake-up fail.
wait_embed() {
  local tries="${1:-60}"
  for _ in $(seq 1 "$tries"); do
    # Same server, same 503-while-loading lie as wait_llm. Match on the body.
    curl -s -m 1 "http://${EMBED_HOST}:${EMBED_PORT}/health" 2>/dev/null | grep -q '"ok"' && {
      echo "  embedding server healthy"; return 0; }
    running embed || break
    sleep 1
  done
  echo "  !! embedding server did not come up -- see $LOGS/embed.log" >&2
  tail -3 "$LOGS/embed.log" >&2
  return 1
}

# The speech decoder. The only one there is since Qwen3-ASR was removed, so this is not
# optional: without it robot-asr keeps the microphone and loses every utterance.
spawn_whisper() {
  if [ ! -x "$ROOT/vendor/whisper.cpp/build/bin/whisper-server" ]; then
    echo "  !! whisper-server not built -- run scripts/models/build_whisper.sh" >&2
    return 1
  fi
  if [ ! -f "${WHISPER_MODEL:-}" ]; then
    echo "  !! WHISPER_MODEL ${WHISPER_MODEL:-unset} missing -- run scripts/models/fetch_whisper.sh" >&2
    return 1
  fi
  # All model pages, not just this one -- see spawn_llm for why.
  python3 "$ROOT/scripts/measure/drop_model_cache.py" --all >/dev/null 2>&1
  # -ng/-nfa are opt-OUT flags: whisper-server defaults both on. Building the argument
  # list rather than passing a value keeps config/whisper.env readable as 1/0 like the
  # other services.
  local gpu=() ; [ "${WHISPER_USE_GPU:-1}" = "1" ] || gpu+=(--no-gpu)
  [ "${WHISPER_FLASH_ATTN:-1}" = "1" ] || gpu+=(--no-flash-attn)
  setsid "$ROOT/vendor/whisper.cpp/build/bin/whisper-server" \
    --model "$WHISPER_MODEL" --host "$WHISPER_HOST" --port "$WHISPER_PORT" \
    --threads "$WHISPER_THREADS" --language "$WHISPER_LANGUAGE" "${gpu[@]}" \
    > "$LOGS/whisper.log" 2>&1 &
  track whisper $!
  # Between the LLM and the dashboard: losing it costs hearing, which matters more
  # than the dashboard and less than the ability to answer at all.
  echo 300 > "/proc/$!/oom_score_adj" 2>/dev/null
  echo "  whisper-server starting (${WHISPER_MODEL##*/}, $([ "${WHISPER_USE_GPU:-1}" = "1" ] && echo GPU || echo CPU)) on :$WHISPER_PORT"
}

# Did the weights actually land on CUDA, or did whisper.cpp quietly use the CPU?
#
# `--no-gpu` is the only thing that makes it not TRY, but trying is not succeeding: on a
# full board it initialises the CUDA backend, fails to allocate, and carries on at
# RTF 8 instead of 0.17. "use gpu = 1" in the log is the request, not the outcome.
# `CUDA0 total size` is the outcome -- it is printed by whisper_model_load only when the
# tensors are on the device.
whisper_on_gpu() {
  grep -q "CUDA0 total size" "$LOGS/whisper.log" 2>/dev/null \
    && ! grep -q "cudaMalloc failed" "$LOGS/whisper.log" 2>/dev/null
}

wait_whisper() {
  local tries="${1:-90}"
  for _ in $(seq 1 "$tries"); do
    curl -s -m 1 "http://${WHISPER_HOST}:${WHISPER_PORT}/health" 2>/dev/null | grep -q '"ok"' && {
      if [ "${WHISPER_USE_GPU:-1}" = "1" ] && [ "${WHISPER_REQUIRE_GPU:-1}" = "1" ] \
         && ! whisper_on_gpu; then
        echo "  !! whisper-server came up on the CPU, not the GPU." >&2
        echo "     At this size that is ~8x slower than real time and the robot will" >&2
        echo "     appear to hang on every utterance. Free GPU memory (a smaller LLM in" >&2
        echo "     config/llm.env, or a smaller checkpoint here), or set" >&2
        echo "     WHISPER_REQUIRE_GPU=0 in config/whisper.env to accept it." >&2
        grep -m2 -E "cudaMalloc failed|CPU total size" "$LOGS/whisper.log" >&2 2>/dev/null
        stop_one whisper 2>/dev/null || true
        return 1
      fi
      echo "  whisper-server healthy$([ "${WHISPER_USE_GPU:-1}" = "1" ] && whisper_on_gpu && echo " (on the GPU)")"; return 0; }
    running whisper || break
    sleep 1
  done
  echo "  !! whisper-server did not come up -- see $LOGS/whisper.log" >&2
  tail -3 "$LOGS/whisper.log" >&2
  return 1
}

spawn_tts() {
  setsid "$ROOT/.venv/bin/python" -m robot.tts.service > "$LOGS/tts.log" 2>&1 &
  track tts $!
  echo "  robot-tts starting"
}

spawn_core() {
  # robot-core reads the listen-language filter from the ASR's config: the two have to
  # agree about which languages exist, and one file owning it is how they stay agreed.
  export ASR_LANGUAGES="${ASR_LANGUAGES:-id,en,ar}"
  setsid "$ROOT/.venv/bin/python" -m robot.core.app > "$LOGS/core.log" 2>&1 &
  track core $!
  echo "  robot-core starting${STM32_PORT:+ (body on $STM32_PORT)}"
}

spawn_asr() {
  # With --wav it starts replaying the moment it is ready, so anything that needs to
  # observe the resulting turns has to already be connected.
  setsid "$ROOT/.venv/bin/python" -m robot.asr.service ${WAV:+--wav "$WAV"} \
    > "$LOGS/asr.log" 2>&1 &
  track asr $!
  echo "  robot-asr starting"
}

spawn_face() {
  # The panel. Renders to DRM/KMS directly, so it needs no desktop -- but it does need a
  # connected display, and it waits patiently for one rather than exiting (which under
  # Restart=on-failure would burn the start limit on a robot with the panel unplugged).
  local bin="$ROOT/.cache/cargo-target/release/robot-face"
  [ -x "$bin" ] || bin="$ROOT/.cache/cargo-target/debug/robot-face"
  if [ ! -x "$bin" ]; then
    echo "  !! robot-face is not built -- ./scripts/face/build.sh" >&2
    return 1
  fi
  setsid env SLINT_BACKEND="${SLINT_BACKEND:-linuxkms-software}" "$bin" \
    > "$LOGS/face.log" 2>&1 &
  track face $!
  echo 700 > "/proc/$!/oom_score_adj" 2>/dev/null
  echo "  robot-face starting (${bin##*/}, $(cat /sys/class/drm/card*-*/status 2>/dev/null | grep -c '^connected') display(s) connected)"
}

spawn_ui() {
  setsid "$ROOT/.venv/bin/python" -m robot.ui.app > "$LOGS/ui.log" 2>&1 &
  track ui $!
  echo 800 > "/proc/$!/oom_score_adj" 2>/dev/null
  echo "  dashboard on http://$(hostname -I | awk '{print $1}'):${UI_PORT:-8081}"
}

# --------------------------------------------------------------------- stops

# Stop one service and wait for it to actually be gone.
#
# Kill the process GROUP: a bare `kill` on a wrapper shell leaves the real service
# running and holding its model in RAM, which is the whole reason this script exists.
stop_one() {
  local name="$1" pid pids gone
  pid="$(tracked_pid "$name")"
  [ -n "$pid" ] && kill -TERM -- "-$pid" 2>/dev/null
  for pid in $(pids_for "$name"); do
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  done
  untrack "$name"

  gone=""
  for _ in $(seq 1 40); do
    running "$name" || { gone=1; break; }
    sleep 0.25
  done
  if [ -z "$gone" ]; then
    # A llama-server mid-allocation ignores SIGTERM for a while. Do not leave 3 GB
    # of model resident because the polite signal was not answered.
    for pid in $(pids_for "$name"); do kill -9 "$pid" 2>/dev/null; done
    sleep 1
  fi
  running "$name" && { echo "  !! $name is still running" >&2; return 1; }
  echo "  stopped $name"
  return 0
}

stop_all() {
  [ -f "$PIDFILE" ] || { echo "no stack running"; return 0; }
  while read -r name pid; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
      echo "  stopped $name ($pid)"
    fi
  done < "$PIDFILE"
  rm -f "$PIDFILE"
  sleep 2
  # Only ever kill real service processes -- see pids_for.
  for name in llm embed whisper tts core asr ui body; do
    for pid in $(pids_for "$name"); do
      kill -9 "$pid" 2>/dev/null && echo "  reaped stray $name ($pid)"
    done
  done
}

# ---------------------------------------------------------- one service at a time

svc_start() {
  local name="$1"
  running "$name" && { echo "  $name is already running (pid $(pids_for "$name" | tr '\n' ' '))"; return 0; }
  load_env
  case "$name" in
    llm)   spawn_llm; wait_llm 90 || return 1 ;;
    embed) spawn_embed || return 1; wait_embed 60 || return 1 ;;
    whisper) spawn_whisper || return 1; wait_whisper 90 || return 1 ;;
    tts)   spawn_tts ;;
    core)  spawn_core ;;
    asr)   spawn_asr ;;
    body)  spawn_body ;;
    ui)    spawn_ui ;;
    face)  spawn_face ;;
    *) echo "unknown service $name" >&2; return 1 ;;
  esac
  # Everything without its own readiness probe: confirm it is still alive a moment
  # later rather than reporting success for a process that exited on startup.
  case "$name" in
    llm|embed|whisper) ;;
    *) sleep 3
       running "$name" || { echo "  !! $name exited on startup -- see $LOGS/" >&2; return 1; } ;;
  esac
  echo "  $name is up"
}

svc() {
  local action="${1:-}" name="${2:-}"
  prune_pidfile
  case "$name" in
    llm|embed|whisper|tts|core|asr|body|ui|face) ;;
    *) echo "usage: $0 svc {start|stop|restart} {llm|embed|whisper|tts|core|asr|body|ui|face}" >&2; exit 2 ;;
  esac
  case "$action" in
    start)   svc_start "$name" ;;
    stop)    stop_one "$name" ;;
    restart) stop_one "$name"; svc_start "$name" ;;
    *) echo "usage: $0 svc {start|stop|restart} <service>" >&2; exit 2 ;;
  esac
}

# ------------------------------------------------------------------- commands

case "$CMD" in
  stop) stop_all ;;
  svc)  svc "${ARGS[0]:-}" "${ARGS[1]:-}" ;;
  status)
    for name in llm embed whisper tts core asr ui face body; do
      printf "%-12s %s\n" "robot-$name" "$(pids_for "$name" | tr '\n' ' ')"
    done
    free -m | head -2
    ;;
  start)
    stop_all
    load_env

    # CUDA on Tegra cannot reclaim page cache -- see TROUBLESHOOTING.md.
    python3 "$ROOT/scripts/measure/drop_model_cache.py" --all

    # Optional simulated body: the real firmware core on a PTY, for when no STM32 is
    # attached. tools/stm32_sim/README covers what this does and does not prove.
    [ -n "$SIM" ] && spawn_body

    spawn_llm
    wait_llm

    # Embedding server (M7). CPU-only and optional -- skipped if the model is absent,
    # in which case robot-core runs without retrieval or save_memory.
    #
    # Under EMBED_MODE=on-demand robot-core starts it itself when knowledge is queued
    # and stops it again afterwards, so starting it here would just hold 810 MiB that
    # the orchestrator is about to reclaim.
    if [ -z "$NO_EMBED" ] && [ "${EMBED_MODE:-on-demand}" != "on-demand" ]; then
      spawn_embed && wait_embed
    else
      echo "  embedding server left to robot-core (EMBED_MODE=${EMBED_MODE:-on-demand})"
    fi

    # The decoder, before robot-asr connects to it.
    spawn_whisper && wait_whisper

    spawn_tts
    sleep 8

    spawn_core
    sleep 4

    # ASR last, for the --wav reason in spawn_asr.
    if [ -z "$NO_ASR" ]; then spawn_asr; sleep 2; else echo "  ASR skipped (--no-asr)"; fi

    # The two optional faces, started last and never depended on: the robot runs
    # without either. They are independent websocket clients of robot-core, so both can
    # run at once -- ./run.sh just does not, because on this board the memory is better
    # spent elsewhere.
    if [ -n "$UI" ]; then spawn_ui; sleep 5; fi
    if [ -n "$FACE" ]; then spawn_face; sleep 2; fi
    echo "  stack up. logs in $LOGS/, sockets in $RUN/"
    ;;
  *) echo "usage: $0 {start|stop|status|svc} [--ctx N] [--wav FILE] [--sim] [--ui] [--face] [--no-asr] [--no-embed]"; exit 1 ;;
esac
