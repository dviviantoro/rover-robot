# TROUBLESHOOTING.md

Things that went wrong and how they were fixed. One heading per symptom,
written so it can be found by searching for the error text.

---

<!-- Add entries newest-first. Include: symptom, cause, fix, milestone. -->

## `cudaMalloc failed: out of memory` while `free` shows GB available

**Symptom.** `llama-server` exits at startup:

```
ggml_backend_cuda_buffer_type_alloc_buffer: allocating 2375.91 MiB on device 0:
    cudaMalloc failed: out of memory
NvMapMemAllocInternalTagged: 1075072515 error 12
```

…while `free -m` reports several GB *available*.

**Cause.** On Tegra, CUDA allocates through NvMap, which does **not** force the kernel to
reclaim page cache. After downloading or reading multi-GB model files, `available` is mostly
page cache holding those files, and CUDA can only see genuinely free pages.

**Fix.** Drop the page cache for the model files:

```bash
python3 scripts/measure/drop_model_cache.py
```

It uses `posix_fadvise(POSIX_FADV_DONTNEED)` and needs no privileges, unlike
`sudo sysctl vm.drop_caches=3`. Run it after fetching a model and before starting a service
that loads one.

**Note.** This is not a developer-only artifact — any service restart following heavy file I/O
can hit it. Milestone: M1.

## `pkill -f <pattern>` kills the shell running it

**Symptom.** A command chain dies with exit code 143/144 partway through and later steps
silently never run.

**Cause.** `pkill -f` matches against full command lines, including the shell that is executing
the `pkill` itself when the pattern appears in that command line.

**Fix.** Match a narrower pattern, or resolve the PID first with `pgrep` and `kill` that.
Milestone: M2.

## `aplay: audio open error: Device or resource busy` after a barge-in

**Symptom.** The first spoken reply works. Then the user interrupts, and every reply after
that fails to produce audio. `fuser /dev/snd/*` shows nothing holding the card by the time
you look.

**Cause.** PulseAudio owns the USB sound card (`pactl list short sinks` shows it). Opening
`plughw:CARD=...` bypasses PulseAudio and takes the PCM directly. That works while the card
happens to be free — but the moment barge-in kills `aplay`, PulseAudio re-acquires the PCM,
and the next `aplay` cannot open it. Retrying does not help: PulseAudio holds it through its
idle timeout, far longer than any sensible retry window.

**Fix.** Route through PulseAudio instead of fighting it — `TTS_ALSA_DEVICE=pulse` in
`config/tts.env`, `ASR_ALSA_DEVICE=pulse` in `config/asr.env`. Reopen after a kill then
succeeds immediately. This also hands full-duplex mixing to PulseAudio, and is where
`module-echo-cancel` would go if barge-in ever needs acoustic echo cancellation.

Milestone: M4.

## `llama-server` fails to allocate its KV cache when other services are starting

**Symptom.** `failed to allocate buffer for kv cache` at startup, even though the model
weights allocated fine a moment earlier, and even at a reduced context size.

**Cause.** CUDA on Tegra allocates through NvMap and cannot reclaim memory another process
already holds. Starting `robot-asr` (~1.2 GB) at the same time as `llama-server` means the
KV cache allocation loses the race.

**Fix.** Start `llama-server` first and wait for `/health` before starting anything else.
`scripts/run_stack.sh` does this; the systemd units express it with `After=`.

Milestone: M4.

## A killed service keeps holding its model in RAM

**Symptom.** `free` shows GB still in use after stopping a service; a later start fails for
lack of memory. `pgrep` finds a `python -m robot.asr.service` nobody started.

**Cause.** The services are launched behind a wrapper shell. Killing the PID that was
launched kills the *shell*, not the Python process holding the model.

**Fix.** `scripts/run_stack.sh stop` kills the process group and then reaps strays. When
killing by hand, kill the process group (`kill -- -PID`) or find the real PID with `pgrep -f`.

**Related trap:** `pgrep -f` / `pkill -f` match whole command lines, so a shell whose command
line merely *mentions* a service name matches itself. `run_stack.sh` checks `/proc/PID/exe`
before killing anything for this reason.

Milestone: M4.

## `LL_GPIO_PULL_NO undeclared` building STM32duino, in a file you did not write

**Symptom.** `pio run` fails inside the framework's own `PeripheralPins_*.c` with
undeclared LL GPIO constants, and `PinMap_USB_OTG_FS undeclared` in `usbd_conf.c`. Your
own code compiles fine.

**Cause.** A project header shadowing a framework header. `build_flags = -Iinclude` puts
the project's include directory *before* the framework's, so `firmware/stm32/include/board.h`
was picked up by `Arduino.h` instead of `cores/arduino/board.h`. The framework's board.h
never loaded, so the HAL/LL definitions it pulls in were missing — and the error surfaced
far from the cause.

**Fix.** Prefix project headers. `board.h` → `robot_board.h`, `pins.h` → `robot_pins.h`.

Milestone: M5.

## `conflicting declaration 'typedef struct pid_t pid_t'`

**Cause.** POSIX already defines `pid_t` (process id) in `sys/types.h`, which newlib pulls
in under the Arduino framework. A PID *controller* type of the same name collides.

**Fix.** `robot_pid_t`. Watch for the same trap with other short, generic type names.

Milestone: M5.

## `/dev/ttyACM0` exists but is not the microcontroller

**Symptom.** A board check reports the STM32 is present when it is not; serial commands go
somewhere and are never answered.

**Cause.** USB composite devices. The USB soundcard on this robot (`cafe:4002 Audio Tools`)
exposes Audio *and* CDC-ACM interfaces, so it claims `/dev/ttyACM0` and looks exactly like a
microcontroller to anything matching on the device name.

**Fix.** Match on the vendor id, not the node name — `0483` is STMicroelectronics.
`scripts/bringup/check_board.sh` does this, and `scripts/bringup/setup_udev.sh` installs a
rule giving the STM32 a stable `/dev/robot-stm32`.

Milestone: M5.

## The embedding server fails to start with `cudaMalloc failed: out of memory`

**Symptom.** `robot-embed` exits at load. It is configured with `-ngl 0`, so it should not
be touching the GPU at all.

**Cause.** `-ngl 0` keeps the *weights* off the GPU but llama.cpp still allocates its
compute buffer through CUDA when the backend is compiled in. At ctx/batch 1024 that was
761 MiB out of the same unified pool the main LLM had already taken.

**Fix.** Take the GPU away from the process entirely: `CUDA_VISIBLE_DEVICES=` in
`robot-embed.service`, and a smaller `EMBED_CTX`/`EMBED_BATCH` (512). Also pass
`--parallel 1` — llama-server defaults to auto and picked 4 slots, each with its own
buffers, for a workload that only ever has one request in flight.

Milestone: M7.

## The model stops emitting `[lang:xx]` tags and `[do:...]` markers after a few turns

**Symptom.** Early turns are fine. Later ones come back with no language tag and no action
marker — a movement request answered verbatim in the wrong language, and the robot does not
move. Reproducible only in a running conversation, never in a single-shot prompt.

**Cause.** Conversation history was storing the *stripped* reply — the text after the tag
and markers had been removed for TTS. So the model saw its own past turns apparently
written without them, inferred that was the format, and copied it.

**Fix.** Store the raw reply in history. Movement requests went from 1/3 to 3/3 on this
change alone.

**General lesson:** whatever you put in the assistant history is a worked example the model
will imitate. Post-processed output does not belong there.

Milestone: M7.

## Retrieved notes change the reply's language

**Symptom.** An Arabic question answered in Indonesian, but only when retrieval returned
something. With no context in the prompt, the same question answers correctly.

**Cause.** Retrieved chunks sit close to the user's turn and read like an example of how to
reply. A block of English there pulls the reply towards English (or towards whatever the
document's language is).

**Fix.** Put the context *before* the language rules, not after, and state explicitly that
the notes' language is irrelevant. Also move the `[lang:xx]` declaration rule to the very
end of the system prompt — recency does real work when the prompt is long.

Milestone: M7.

## A crashed llama-server cannot restart: `cudaMalloc failed` on every retry

**Symptom.** `robot-llm` is killed (or OOMs), systemd restarts it, and every attempt
fails at the KV cache. After five tries `StartLimitBurst` is reached and the unit stays
down — the robot is left permanently without an LLM despite `Restart=on-failure`.

**Cause.** The model it just died with is still in the page cache, and CUDA on Tegra
will not force the kernel to reclaim it. This is the same NvMap behaviour as the
first-boot case, but on the restart path, where it matters far more.

**Fix.** `ExecStartPre=` on `robot-llm.service` and `robot-embed.service` drops that
model's page cache before loading it:

    ExecStartPre=.../python .../scripts/measure/drop_model_cache.py ${LLM_MODEL}

After this the same SIGKILL is recovered from in ~45 s. Milestone: M9.

**Related:** `StartLimitBurst=5` over 300 s is deliberate (memory-budget.md rule 5), but
llama-server takes ~45 s to start, so five attempts is most of that window. If you are
testing restarts by hand, `systemctl reset-failed robot-llm` between them.

## A turn never finishes, and the robot goes quiet

**Symptom.** The robot answers in the dashboard chat but no `turn_done` ever arrives; it
then ignores everything else, because a new turn cancels one that never completes. No
error is logged. Seen sitting in this state for 4.5 minutes.

**Cause.** Three separate unbounded waits, found one after another during the M9 drills:

1. `robot-tts` kept a **single** peer reference. The dashboard's hot-reload opens a
   short-lived connection to change the voice map; that transient connection replaced
   the orchestrator's and then closed, so every later `started`/`finished` went nowhere.
2. `Player.drain()` waited on `aplay` with no timeout, so a suspended PulseAudio sink
   blocked the worker for ever.
3. Nothing bounded a turn as a whole. A turn ends when the speaker reports back, so any
   unreported failure downstream hung it silently.

**Fix.** TTS broadcasts to every connected peer; playback, synthesis and writes are all
bounded; and `Orchestrator._turn_deadline` force-completes any turn that overruns
`turn_timeout_s` (90 s), logging it and emitting `turn_timeout`.

**Lesson:** "do not hang the turn" needs a deadline on the *turn*, not only on its parts.
Bounding each step still leaves the gap between them. Milestone: M9.

## Logs vanish on reboot

**Symptom.** Something failed overnight and `journalctl` has nothing from before the
reboot.

**Cause.** `/var/log/journal` does not exist on this image, so journald runs volatile —
everything lives in `/run` and is lost on restart. Exactly the wrong behaviour for a
machine you are meant to leave running unattended.

**Fix.** `sudo scripts/bringup/setup_logging.sh` creates the directory, sets
`Storage=persistent` and caps the journal at 200 MB (the root filesystem is ~94% full,
so an uncapped journal is its own hazard). Milestone: M9.

**Note:** `systemctl --user` units have no journal at all on this box. The test harness
`scripts/hardening/user_units.sh` redirects their output to `.cache/tmp/<unit>.log` for
that reason; production uses the system units, which log to journald normally.

## The dashboard session never persists — no cookie is ever set

**Symptom.** Logging in returns the right redirect, but the next request is
unauthenticated again and the browser websocket is refused. No `Set-Cookie` on the login
response.

**Cause.** Two `SessionMiddleware` instances. NiceGUI installs one during `ui.run` for
its own `app.storage`, and adding a second silently fights it: the inner instance
overwrites `scope["session"]`, so nothing is ever persisted.

**Fix.** Add exactly one, before `ui.run`. NiceGUI checks for an existing
`SessionMiddleware` and reuses its secret instead of adding another
(`nicegui.storage.set_storage_secret`).

**Related trap:** the auth guard must run *inside* the session middleware. Because
NiceGUI installs that middleware during `ui.run` — after your routes are registered —
a middleware of your own ends up outside it and sees an empty session. `robot.ui.auth`
therefore guards per-route rather than as middleware.

**And a testing trap, not a bug:** `aiohttp`'s default cookie jar silently drops cookies
for a bare IP host, so a script hitting `http://127.0.0.1:8081` will look unauthenticated
no matter what. Use `aiohttp.CookieJar(unsafe=True)`. `curl` has no such rule, which is
why the two disagreed.

Milestone: M8.

## The robot hears nothing, and nothing reports an error

**Symptom.** The microphone is plugged in, `robot-asr` is running and healthy, no errors
anywhere — and the robot never responds to speech. The dashboard's level meter reads
**-90 dB**: not quiet, *perfect digital silence*.

**Cause.** `ASR_ALSA_DEVICE=pulse` means "PulseAudio's **default** source", and on this
board that default is `alsa_input.platform-sound.analog-stereo` — the Tegra onboard
analog input, with nothing connected to it. Capturing from it succeeds and returns
zeros, so every layer above reports success.

    pulse (default source)     -> -240 dBFS   silence
    plughw:CARD=Audio,DEV=0    ->  -49 dBFS   real room tone

**Fix.** Name the source explicitly in `config/asr.env`:

    PULSE_SOURCE=alsa_input.usb-Audio_Tools_USB_Audio_000001-02.mono-fallback

List them with `pactl list short sources`.

**Why it went unnoticed:** this was introduced in M4 when capture moved to PulseAudio to
solve a playback contention problem, and every test after that fed the ASR a wav file
through `--wav` rather than the microphone. It surfaced only when the dashboard grew a
level meter and someone looked at it.

`robot-asr` now watches for this: eight seconds of pure digital silence logs an error
naming the device, and the level event carries `silent: true` so the meter says so. A
microphone that hears nothing should never again be a silent failure.

Milestone: M8 follow-up.

## The robot listens but never speaks

**Symptom.** Conversation works: the transcript is right, the reply is right, the turn
completes with a real `audio_s` and a `first_audio_ms`. `robot-tts` is up, Piper logs the
phonemes, `aplay` exits 0. Nothing anywhere reports an error. The robot is simply mute.

**Cause.** The exact twin of the deaf-microphone entry above, and for exactly the same
reason. `TTS_ALSA_DEVICE=pulse` means "PulseAudio's **default sink**", which on this board
is `alsa_output.platform-sound.analog-stereo` — the Tegra onboard analog output, with
nothing plugged into it. `aplay` opens it, accepts every sample, and reports success. The
speaker is on the *USB* device, the same one carrying the microphone.

Watch the sinks while something plays — the one that goes `RUNNING` is the one receiving
audio:

    pactl list short sinks

    0  alsa_output.usb-Audio_Tools_...mono-fallback    SUSPENDED   <- the real speaker
    1  alsa_output.platform-sound.analog-stereo        RUNNING     <- where it was going

**Fix.** Pin the sink in `config/tts.env`, exactly as `asr.env` pins the source:

    PULSE_SINK=alsa_output.usb-Audio_Tools_USB_Audio_000001-02.mono-fallback

**Why `pulse` is a trap in both directions.** `pulse` and `default` do not name hardware.
They name "whatever PulseAudio currently prefers", which changes on a reboot or a hotplug,
and which on this board defaults to a codec wired to nothing. Both services now resolve
their endpoint at startup and say so:

    robot-tts: audio output: pulse -> alsa_output.usb-...mono-fallback (pinned by PULSE_SINK)
    robot-asr: audio input:  pulse -> alsa_input.usb-...mono-fallback  (pinned by PULSE_SOURCE)

If an endpoint is *not* pinned, the line says `NOT pinned` and a warning follows naming
the device it resolved to. `scripts/bringup/check_board.sh` fails on an unpinned endpoint,
`tests/test_audio_endpoint.py` fails if the committed config ships one, and the dashboard's
health panel shows the speaker next to the `tts` service — because "tts is UP" was true
throughout this bug and told us nothing.

Milestone: M8 follow-up.

## The robot says "Give me a moment" to everything, and never recovers

**Symptom.** Chat and speech both answer with the canned unavailable line. `llama-server`
is running and `curl http://127.0.0.1:8080/health` returns `{"status":"ok"}`, but
`core.log` keeps saying:

```
ERROR robot-core: llm unavailable: llama.cpp returned HTTP 400
```

and `llama-server.log` has:

```
got exception: {"error":{"code":400,"message":"Unable to generate parser for this
template. ... Error: Jinja Exception: Conversation roles must alternate
user/assistant/user/assistant/..."}}
```

Restarting `robot-core` fixes it — until the LLM is unavailable again.

**Cause.** The orchestrator produces the offending shape itself. `turn.py` records the
question with `history.add_user()` on every turn, but only calls `history.add_assistant()`
when a reply actually arrived. While the LLM is down, each turn adds a user message and
nothing else, so the history ends up holding user messages back to back. Gemma-3's chat
template refuses to render that; llama.cpp answers 400, the orchestrator treats the 400
the same as the LLM being unreachable, speaks the canned line, records another user
message — and the conversation can never recover on its own.

Qwen's template tolerates non-alternating roles, which is why this appeared only after
the dashboard's Memory page was used to switch to Gemma-3-1B. Nothing about the failure
points at the model choice.

**Fix.** `prompt.alternate()` merges consecutive same-role messages, and
`History.add_user()` folds an unanswered question into the next one, so the invariant
holds no matter which caller is involved and no content is lost. Covered by
`tests/test_language.py::TestAlternation`.

**Related.** Some templates reject the `system` role outright (Gemma-3 and several
Mistral variants). `LlamaClient` retries once with the system message folded into the
first user turn and remembers the answer for the rest of the session — see
`llm.fold_system`. Milestone: M8 follow-up.

## The dashboard's restart buttons say nothing happened, and nothing did

**Symptom.** A model is picked on the Memory page, or a staged setting is edited on
Config, and pressing the restart button reports a failure — or, worse, the service simply
never comes back. `robot-llm` stays down and every turn speaks the canned line.

**Cause.** Restarting was hard-coded to `systemctl restart robot-<component>`. That is
right in production, but the systemd units are installed by
`scripts/install_services.sh`, which needs root and had never been run on this board. The
stack in front of you was started by `scripts/run_stack.sh`, so `systemctl` had no unit to
restart — and `sudo scripts/bringup/setup_sudoers.sh` having been run made this *more*
confusing, because the sudoers rule existed and the command was permitted. It just had
nothing to act on.

**Fix.** `robot.common.services` works out which supervisor actually holds the processes
— system units, `systemctl --user` units, or `scripts/run_stack.sh` — and sends the action
there. `run_stack.sh svc start|stop|restart <service>` is the dev-stack half of that, and
it reuses the same spawn functions as a full `start`, so a service restarted from the
dashboard gets the identical command line, environment and `oom_score_adj`.

The dashboard's **Services** page shows which supervisor is in charge, so a button that
cannot work says why before you press it. Milestone: M9 follow-up.

## A service start is reported as timed out, but the service is up

**Symptom.** The Services page reports `did not start within 120s`. The service is
running and healthy, and `run_stack.sh svc start <name>` from a terminal returns in
seconds.

**Cause.** The dashboard was collecting the supervisor command's output through a pipe.
The command being run is a supervisor: it spawns daemons that outlive it, and a pipe stays
open for as long as *any* descendant holds the write end. Measured: the script exited at
7 s, `llama-server` answered `/health` at 8 s, and the read blocked until the timeout.

**Fix.** `services._run` sends output to a temporary file rather than a pipe, and waits
for the child in a worker thread (`asyncio.to_thread`) rather than through the event
loop's child watcher — the dashboard runs under uvicorn's uvloop, and this has to behave
the same there. Milestone: M9 follow-up.

## Still "Give me a moment" after the alternation fix — a history that has rolled

**Symptom.** Exactly the HTTP 400 above, `Conversation roles must alternate`, on a robot
whose `prompt.alternate()` already merges consecutive same-role turns.

**Cause.** A second shape, and a subtler one. `History` is a bounded deque that drops from
the left. Its entries alternate, so evicting an *even* number leaves a user turn in front
and everything is fine — but one unanswered turn (the LLM was down, `add_user` ran and
`add_assistant` did not) makes the count odd, the next eviction takes a `user`, and the
window now opens on an assistant reply whose question has been forgotten.

Gemma-3's template strips a leading system message and then requires the rest to start
with `user`:

```jinja
{%- if messages[0]['role'] == 'system' -%}{%- set loop_messages = messages[1:] -%}
{%- for message in loop_messages -%}
    {%- if (message['role'] == 'user') != (loop.index0 % 2 == 0) -%}
        {{ raise_exception("Conversation roles must alternate ...") }}
```

so an orphaned assistant turn at index 0 raises. It takes 13 appends against the default
`history_turns=12` to reach, which is why it survived the first fix.

**Fix.** `prompt.alternate()` drops leading assistant messages after the system one. That
turn is a fragment of a conversation whose beginning is already gone; there is nothing
else to do with it. `tests/test_language.py::TestAlternation` covers both shapes.

## The first ingestion after the embedding server wakes fails with connection refused

**Symptom.** With `EMBED_MODE=on-demand`, `core.log` shows `embedding server up` and then
immediately `ingestion failed`, with `URLError: [Errno 111] Connection refused` from
`rag.Embedder._post`.

**Cause.** Every supervisor returns as soon as the *process* exists — `systemctl start`
with `Type=exec`, and `run_stack.sh` alike — while `llama-server` still has hundreds of
megabytes to read. "Started" is not "can answer", and the caller here is an ingestion job
that POSTs immediately.

**Fix.** Two places, because both callers deserve the truth: `run_stack.sh` gained
`wait_embed`, the twin of `wait_llm`, so `svc start embed` means it answers; and
`EmbedServer._await_ready()` polls `/health` for up to 60 s after any supervisor reports
success, which also covers the systemd path. Milestone: M9 follow-up.

## The dashboard can start services but the embedding server never wakes

**Symptom.** Under systemd, `EMBED_MODE=on-demand` logs `embedding server would not
start` with a sudo error, while the Config page's restart buttons work fine.

**Cause.** `setup_sudoers.sh` used to grant `systemctl restart` on the robot units and
nothing else. Demand-starting needs `start` and `stop`, and eviction needs them on the
LLM too.

**Fix.** Re-run `sudo scripts/bringup/setup_sudoers.sh`. It now grants start/stop/restart
on `robot-llm robot-asr robot-tts robot-embed robot-whisper` — and still nothing on
`robot-core`, which must not be able to restart the process it is running in.

## `pkill -f whisper-server` killed the shell again

Worth restating because it happened twice more while building this: `pkill -f` and
`pgrep -f` match against **full command lines**, including the shell running them and any
`bash -c` whose script text merely mentions the name. Both bit during this work — once
killing the session outright (exit 144), once quietly polluting a measurement by matching
the measuring loop's own subshells.

Use the executable, not the command line:

```bash
for p in /proc/[0-9]*; do
  case "$(readlink -f "$p/exe" 2>/dev/null)" in
    */whisper-server) kill "${p#/proc/}" ;;
  esac
done
```

or `./scripts/run_stack.sh svc stop whisper`, which does exactly that. `pids_for` in
`run_stack.sh` and `Managed.matches` in `robot/common/services.py` both check the resolved
executable for this reason.

## Choosing a whisper model in the dashboard leaves the robot deaf, and robot-whisper refuses to start

**Symptom.** A whisper checkpoint is picked on the Memory or Config page, its Restart
button is pressed, and `robot-asr` dies on startup:

```
ERROR robot-asr: ASR model dir models/asr/whisper/ggml-small.bin missing
                 -- run scripts/models/fetch_asr.sh
```

Starting `robot-whisper` from the Services page then also fails, with
`whisper not started (ASR_ENGINE=sherpa)`. Neither message mentions the other, and the
config genuinely does contain the model that was chosen.

**Cause.** Choosing an ASR model was only ever a *path* change. The `asr_model` setting
carries one `env_file`/`env_key` pair — `config/asr.env` / `ASR_MODEL_DIR` — so picking a
whisper checkpoint wrote a ggml **file** into the variable that names a sherpa export
**directory**, and left `ASR_ENGINE=sherpa` pointing at the engine that cannot read it.
`robot-whisper` then correctly refused to start, because the engine in the config said it
was not wanted.

The Memory page lists both engines' checkpoints under one `asr` kind on purpose: they
compete for the same slot in the budget and must be priceable against each other. That
was right; making the *selection* engine-blind was not.

**Fix.** `catalog.asr_engine_for()` decides from the path which engine can load it, and
`asr_env_updates()` / `asr_services_for()` return every write and every service move that
choice implies. `app._apply_asr_choice()` performs them: it writes `ASR_ENGINE` in both
directions, writes `WHISPER_MODEL` or `ASR_MODEL_DIR` as appropriate, stops the other
engine's server, and restarts `robot-whisper` before `robot-asr` so the listener has
something to reconnect to. Covered by `tests/test_catalog.py::TestAsrEngineSelection`.

**If you hit this before the fix,** `config/asr.env` will have a `.bin` path in
`ASR_MODEL_DIR`. Re-picking the model applies both keys correctly, but `ASR_MODEL_DIR`
keeps its bad value until you switch back to a sherpa model — set it back to
`models/asr/qwen3-asr-0.6b-int8` by hand.

## "Which model am I actually talking to?"

**Symptom.** The dashboard shows one model selected, the robot behaves like another, and
every file you check looks correct.

**Cause.** Not a bug — three layers, each legitimately able to lag the next:

| layer | lives in | changes when |
|---|---|---|
| chosen | `run/settings.json` | you pick it in the dashboard |
| configured | `config/*.env` | you press that setting's Restart button |
| running | `/proc/<pid>` | the service actually restarts |

`Kind.RESTART` settings are staged deliberately — picking a model must not cut the robot
off mid-sentence. Observed on this board, all three disagreeing at once: Qwen3-4B chosen,
gemma-3-1b configured, gemma-3-1b running. Every layer was behaving as designed.

**Fix.** Ask:

```bash
python scripts/measure/whats_running.py          # or --json
```

It prints all three columns side by side and exits non-zero when they disagree, naming
the command that closes each gap. The Services page shows the same comparison per row and
marks a process running something other than its config with a **not applied** badge next
to the button that fixes it. The running column is read from the live process — its
command line, or `/proc/<pid>/environ` — never from the same file as the configured
column, because two readings of one file would agree by construction and prove nothing.

## Two ASR decoders running at once, or the wrong one

**Symptom.** `robot-whisper` is running while the selected ASR model is a Qwen3-ASR
export, or the reverse — `ASR_ENGINE=whisper` and no decoder up, so the robot hears
nothing while `robot-asr` looks perfectly healthy.

**Cause.** They read as alternatives and are not. `robot-asr` always runs: it owns the
microphone, the Silero endpointing and the barge-in signal. `ASR_ENGINE` selects who
*decodes* — Qwen3-ASR inside that process, or `robot-whisper` over loopback. One calls
the other, so "robot-asr is up" says nothing about whether anything can be transcribed.

**Fix.** Choose the model on the **Memory** page and press Apply. That writes
`ASR_ENGINE`, writes the model into whichever variable that engine reads, stops the other
decoder and restarts `robot-asr` — in that order. It is the only supported way to switch,
and it cannot leave the two disagreeing.

To check by hand:

```bash
python scripts/measure/whats_running.py
```

It flags a decoder running with nothing reading it (yellow, with the memory it is
holding) and one that the selected model needs but is absent (red). The Services page
shows the same, and disables the Start button on a decoder nothing would read rather than
offering one that fails.

`./scripts/run_stack.sh svc start whisper` refuses under `ASR_ENGINE=sherpa` on purpose.
That refusal is not the bug — it is what stops a stale start putting a second 2 GB model
on a board with room for one.

## The robot interrupts itself, and transcribes its own voice as your next question

**Symptom.** You ask something, the robot starts answering, and it cuts itself off
mid-sentence. `core.log` shows `barge-in: user spoke during speaking` followed by a turn
whose transcript is a fragment of what the robot just said.

**Cause.** The speaker and the microphone are one USB device (`cafe:4002 Audio Tools`).
Whatever the robot says arrives back a few milliseconds later looking exactly like
someone talking, so the VAD opens, barge-in fires, and the reply is cancelled — then the
echo is decoded and starts a new turn. M4 measured the acoustic coupling on this hardware
as negligible and set `CORE_BARGE_IN_GRACE_MS=0` accordingly; that measurement no longer
holds at the volumes and checkpoints in use now.

**Fix.** `CORE_LISTEN_MODE`, live on Config → Listening:

| mode | while the robot is speaking |
|---|---|
| `interrupt` | barge-in — anything cancels the reply. Most responsive; the mode that hears itself |
| `no_interrupt` | the microphone is ignored until `CORE_LISTEN_TAIL_MS` after audio stops |
| `wake_word` | as `no_interrupt`, and nothing starts a turn unless the transcript names `CORE_WAKE_WORD` |

The **tail** matters more than the older grace window: room echo and the speaker's own
decay arrive after the last sample, and an utterance that began during playback is still
being decoded when playback ends. The grace window only covered the leading edge.

Every path that ends playback gives the microphone back — a completed turn, a cancel, a
speaker error, the turn deadline. A mode that gates on the speaker would otherwise leave
the robot permanently deaf the first time a `finished` went missing.

## The robot answers something nobody said, often in Chinese

**Cause.** whisper reports a language for everything it decodes, including a cough, the
fan, or a burst of speaker echo — and it labels most of those Chinese. Those transcripts
went to the LLM as if somebody had said them.

**Fix.** `ASR_LANGUAGES=id,en,ar` in `config/asr.env`. Anything outside that set is
dropped before it becomes a turn, and reported as an `ignored` event so the activity log
shows what was heard and why it went nowhere. Only affects whisper; Qwen3-ASR reports no
language at all, so nothing can be filtered on and everything passes.

whisper itself takes one language or `auto` — it has no "these three" setting, which is
why the filter runs after detection rather than as a decoder flag.

## A model I downloaded is not in the list

**Check where it landed first** — the catalogue globs fixed locations, and a file in the
wrong one is invisible with no error:

| kind | must be in | recognised by |
|---|---|---|
| LLM | `models/llm/` | `*.gguf` |
| embeddings | `models/embed/` | `*.gguf` |
| ASR | `models/asr/whisper/` | `ggml-*.bin` |
| Piper voice | `models/tts/piper/` | `*.onnx` **plus** a matching `*.onnx.json` |

`./scripts/models/list.sh` shows what the catalogue can actually see.
`./scripts/models/fetch_hf.sh --dry-run <repo> <file>` shows where a download would go
before you spend the bandwidth.

**Three things that hide a file that is in the right place:**

* **A half-finished download.** Anything with a `.part` beside it is skipped on purpose —
  its size would misprice it. `hf_get` resumes: run the same fetch again.
* **A Piper voice with no `.onnx.json`.** The catalogue skips it, because a voice without
  its config cannot load. `fetch_hf.sh` fetches both.
* **A `.safetensors` file.** Nothing here loads those (AGENTS.md constraint 4 — no
  PyTorch or transformers in the runtime). You need a GGUF or ONNX conversion.

**It should not need a restart.** robot-core rescans every health poll and logs
`new model installed: <path>`; the dashboard's pickers repopulate within ~3 s. If that is
not happening, `grep 'new model installed' .cache/tmp/core.log` — silence there means
robot-core is not seeing the directory at all, which usually means it is running with a
different working directory than the one you downloaded into.

## Voice replies feel slow, and tuning the dashboard does not help

**Symptom.** Several seconds between the end of your sentence and the first spoken word.
Lowering `vad_min_silence` or `first_max_chars` on the Config page changes nothing after
a restart.

**Measured on this board**, from the moment speech actually ended: **8209 ms → 2181 ms
median, 1554 ms best**, by fixing three things that were each invisible.

**1. Every latency number was optimistic by the endpointing silence.** `speech_end` was
timestamped when Silero *declared* the utterance over, which is `VAD_MIN_SILENCE` after
the person stopped. robot-asr now reports `endpoint_s` and robot-core back-dates by it,
so the numbers are what a person experiences.

**2. Settings tuned for robot-asr never reached it.** They were applied once during
robot-core's startup — and `run_stack.sh` starts core *before* robot-asr, so the apply
went to a socket nothing was listening on and the failure was swallowed. `vad_min_silence`
pinned at 0.2 s had been running at the env default of **0.65 s** the whole time: 450 ms
of dead time on the front of every turn. They are now pushed when robot-asr connects,
which also covers it being restarted later.

**3. Registry defaults were written over the env files at startup.** `settings.all()`
fills in a default for every key, and applying those overwrote values this process had
already read from `config/core.env`. That is why `CORE_FIRST_MAX_CHARS=40` did nothing
(the registry said 90) and `CORE_MAX_TOKENS=260` did nothing (the registry said 200).
Startup now applies `settings.saved()` — only what somebody actually chose.

**Where the time goes now**, and the lever for each:

| stage | median | lever |
|---|---|---|
| endpointing silence | 200 ms | `vad_min_silence` — pure dead time; below ~0.2 s it clips people who pause |
| ASR decode | ~510 ms | a smaller `WHISPER_MODEL` (`base` is ~240 ms) |
| first token | +92 ms | prompt size: `rag_k`, persona length |
| first clause | +926 ms | the LLM writing something speakable |
| synthesis | +963 ms | `CORE_FIRST_MAX_CHARS` — time to first word is roughly proportional to the first chunk's length |

**Check what is actually in force, not what you set:**

```bash
./run.sh headless --check                      # lists what the dashboard has pinned
./scripts/measure/voice_latency.py --wav <a recording> -n 5
```

A pinned value beats both env files, and pins accumulate: `first_max_chars` pinned at 90
is what made `CORE_FIRST_MAX_CHARS=40` inert even after the bugs above were fixed.

## Answers stop in the middle of a sentence

**Symptom.** The robot starts answering and just stops — mid-word, sometimes mid-syllable
— and under the JSON reply format it also fails to move, change expression, or set the
reply language. No error anywhere.

**Cause.** The reply hit `CORE_MAX_TOKENS`. llama.cpp reports this on every turn as
`finish_reason: "length"`, and this pipeline read it and threw it away, so a cut-off reply
was indistinguishable from a finished one.

It is worse than it sounds under `json`. The grammar is still inside the `answer` string
when the budget runs out, so the object never closes and every field after `answer` —
`language`, `foot_direction`, `foot_step`, `hand_movement`, `face`, `style` — is never
generated. That is why a truncated reply is also a motionless one.

**Fix.** Three things, all in place now:

1. **The budget follows the window.** `CORE_MAX_TOKENS=auto` (the default) derives it as
   `LLM_CTX - prompt budget - 128`, capped at 1024. It used to be a flat 200 with no
   relationship to `LLM_CTX` at all — raising the window from 2048 to 4096 bought ~1390
   tokens of headroom that nothing was allowed to use.
2. **Truncation is reported.** A WARNING naming the split, a `truncated` event on the
   dashboard, and `truncated` in the turn metrics beside `timed_out`.
3. **The split is visible before you start.** `./run.sh <mode> --check` prints
   `Context 4096 tokens = prompt 2703 + reply 1024`, and robot-core logs the same line.

Measured on this board: a "tell me everything about Tazakka" answer ran to **487 tokens**.
At the old cap of 200 that reply lost about 60% of itself, silently.

**If it still truncates**, raise `LLM_CTX` in `config/llm.env` — and price the larger KV
cache on the Memory page first, because the window is not free.

**And check nothing has pinned it.** A value set on the dashboard beats both env files:

```
./run.sh headless --check     # "from the dashboard: ..." lists what is pinned
```

`max_tokens` pinned to 200 is what made `CORE_MAX_TOKENS=260` in `config/core.env` inert
for as long as it was there.

## whisper says `use gpu = 1` and decodes at CPU speed

**Symptom.** Every utterance takes seconds. `config/whisper.env` says
`WHISPER_USE_GPU=1`, the log says `use gpu = 1`, and the robot still appears to hang.

**Cause.** `use gpu = 1` is the **request**, not the outcome. `--no-gpu` is the only
thing that stops whisper.cpp *trying* — on a full board it initialises the CUDA backend,
fails to allocate its compute buffer, and carries on with the CPU at RTF ~8 instead of
0.17. The failure is one line at startup and nothing at all afterwards:

```
whisper_model_load:        CUDA0 total size =  1623.92 MB
ggml_backend_cuda_buffer_type_alloc_buffer: allocating 32.96 MiB: cudaMalloc failed
```

**Fix.** `WHISPER_REQUIRE_GPU=1` (the default). `run_stack.sh` now checks the *outcome*
— `CUDA0 total size` present and no `cudaMalloc failed` — before calling whisper-server
healthy, and stops it with an explanation rather than leaving a slow decoder running.

Make room by shrinking something. The checkpoints are listed smallest-first on the
Memory page with what each costs; `small` is the largest that leaves headroom beside a
3B LLM and Supertonic on this board.

## The robot stopped speaking, and the Services page is red

**Symptom.** Turns complete, the reply appears on the dashboard, no audio comes out.
`tts.log` ends with `GpuUnavailable`.

**Cause.** Working as designed, and this is the design changing. Supertonic used to
rebuild itself on the CPU whenever CUDA failed — one log line, then a robot that was
merely slower (RTF ~1.0 against ~0.7) for no visible reason, hours after the line
scrolled away. `TTS_REQUIRE_GPU=1` makes that an error instead: the sentence is dropped,
the fault is visible, and **the next utterance retries the GPU** — so freeing memory
recovers it without a restart.

**Fix.** Free GPU memory, or accept the CPU deliberately:

| lever | where |
|---|---|
| a smaller LLM | `LLM_MODEL` in `config/llm.env` — the largest line, and the one meant to be adjusted |
| a smaller whisper checkpoint | the Memory page, or `WHISPER_MODEL` in `config/whisper.env` |
| accept CPU synthesis | `TTS_REQUIRE_GPU=0` in `config/tts.env` |

Measured on this board: whisper `large-v3-turbo` + Qwen2.5-3B + Supertonic all load on
the GPU and then swap 658 MiB, and Supertonic loses its workspace mid-utterance. The same
stack with whisper `small` holds 510 MiB free and never falls back.

## The Memory page disagrees with `free -m`

**It no longer should, and if it does the page is wrong.** Every line carries
**Estimated** beside **Measured now**: the estimate is disk size times a measured factor
(the only thing available for a model that is not loaded), the measurement is that
process's RSS from `/proc`, and the "Actually in use" tile is `/proc/meminfo`. All three
come from one sample, pushed every 3 s with the health poll.

A drift of more than ~32 MiB is flagged in the table. Where the two disagree the
measurement is right and the estimate needs fixing in `catalog.py` — which is exactly how
the Supertonic line was found to be 884 MiB light, reporting a selection as fitting while
the board swapped.

## The stack prints "llama-server healthy" and comes up with no LLM

**Symptom.** `./scripts/run_stack.sh start` reports every service healthy. The robot
answers the unavailable line to everything. `core.log` says `llama.cpp is NOT reachable`,
and `llama-server.log` ends with:

```
ggml_backend_cuda_buffer_type_alloc_buffer: allocating 288.00 MiB on device 0:
    cudaMalloc failed: out of memory
llama_init_from_model: failed to allocate buffer for kv cache
```

**Cause.** `wait_llm` used `curl -s ... >/dev/null && echo healthy`. **`curl -s` exits 0
for any HTTP response**, and llama-server answers `/health` with
`503 {"error":{"message":"Loading model"}}` from the moment it binds the port. Measured
on this board: 503 at t=2 s, `{"status":"ok"}` at t=7 s.

So the script declared it healthy five seconds early and started whisper-server, which
took the GPU that llama-server had not finished allocating. The KV cache then lost the
race. `wait_whisper` never had the bug — it greps the body for `"ok"`.

**Fix.** Match on the body, not the exit status. `wait_llm` and `wait_embed` now do:

```bash
curl -s -m 1 "http://$HOST:$PORT/health" 2>/dev/null | grep -q '"ok"'
```

`robot.core.llm.health()` was always correct (it checks `status == 200`), which is why
robot-core reported the truth while the start script did not.

## The robot goes permanently deaf, and nothing logs anything

**Symptom.** It answered fine, then stopped responding to speech entirely. The level
meter still moves. robot-asr is up, whisper is up, and there is no error anywhere.
Restarting robot-core fixes it until it happens again.

**Diagnosis, in one command.** The ASR's own `pong` says whether the microphone is open:

```
ASR gate_open = False | speaking = False | utterances = 0
```

**Cause.** In `no_interrupt` and `wake_word` modes the orchestrator shuts the capture
gate when robot-tts reports `started`, and reopens it on `finished`. All three clearing
paths -- `finished`, `error`, cancel -- come **from robot-tts**. Nothing cleared it when
robot-tts never reported:

* robot-tts killed, OOM-killed or restarted mid-sentence
* a **second client on its socket** whose playback was interrupted --
  `scripts/measure/tts_smoke.py` is a shipped script that talks to the live robot-tts,
  and that is how this was found

`_speaking_audio` then stayed `True` for ever, and `attach_asr` re-closed the gate on
every reconnect. `turn_timeout_s` already protected the *turn* from this exact class of
hang; the microphone had no equivalent.

**Fix.** Two, belt and braces:

* **`Orchestrator.forget_speaker()`** — called when the robot-tts connection drops, the
  one moment we know no `finished` is coming. Reopens after the normal tail.
* **A deaf watchdog** — the gate cannot stay shut longer than `turn_timeout_s` (90 s).
  If it does, that is logged at ERROR and the microphone is forced open. A reply's audio
  cannot legitimately outlast a whole turn, and a false alarm costs one echoed utterance
  that the transcript-side check already refuses.

## Supertonic says it is on the GPU and synthesises at CPU speed

**Symptom.** `robot-tts` logs `loaded Supertonic ... on GPU`, `pong` reports
`accelerator: GPU`, and replies still take seconds to start.

**Cause, measured 2026-09-14.** Two different things, both real.

**1. It falls back, silently by design, and the log scrolls away.** The CUDA session is
attempted once per process. With llama-server and whisper-server already on the GPU it
fails — `CUBLAS_STATUS_ALLOC_FAILED`, or `CUDNN_STATUS_INTERNAL_ERROR` at the first
synthesis — and robot-tts rebuilds on the CPU and keeps talking, which is the right
behaviour and easy to miss. `grep 'on CPU' .cache/tmp/tts.log` is the check.

The trigger is page cache, not memory: NvMap on Tegra will not reclaim it for a CUDA
allocation, so `free` shows a gigabyte "available" and the allocation still fails.
**robot-tts now drops the page cache immediately before attempting CUDA**, the same way
`spawn_llm` does for llama-server. Because Supertonic loads lazily, doing it once at
startup is not enough — by the first sentence the cache has refilled. `TTS_PRELOAD=1`
moves that whole dance to service start, where nobody is waiting.

**2. "On the GPU" is not worth what the M10 note says, once the board is full.** That
RTF 0.06 was measured **with whisper stopped**. With the LLM and whisper also resident:

| first chunk | audio | time to first audio | RTF |
|---|---|---|---|
| 5 chars | 1.32 s | **235 ms** | 0.18 |
| 44 chars | 3.34 s | **2258 ms** | 0.68 |
| 130 chars | 9.12 s | **7551 ms** | 0.83 |

Same sentence on the CPU: 3414 ms. So the GPU is worth about **1.5x here, not 13x**, and
`SUPERTONIC_STEPS` — the cheapest speed control when Supertonic was CPU-bound — moves it
barely at all (2246 ms at 4 steps, 2219 ms at 2).

**What actually controls it is the length of the first chunk**, which is why
`first_max_chars` is now a setting (Config → Speaking, `CORE_FIRST_MAX_CHARS`) instead of
a constant buried in `SentenceChunker`. It was 90, chosen when 90 characters cost 0.4 s
to synthesise; at the RTF above they cost about 5 s before the first word. `first_min_chars`
is the floor on the same break — at 4 a short opener like "Baik," goes out on its own.
**Listen before settling on values:** the cost is where the breath falls, not a pause,
because the remainder renders while the first chunk plays and the player is gapless.

## Transcription is fine in a quiet room and garbage in a busy one

**Symptom.** The microphone works, the level meter moves, every measurement in M3 says
word-perfect — and in a room with people in it the robot answers things nobody said,
mishears the things they did, or both.

**Cause.** Two separate failures that look like one, and only one of them is the
recogniser.

1. **Silero opens utterances on the room.** A chair, a door, a cough. An utterance
   closes, and a decoder obliged to return *something* returns something. Measured here
   on twenty seconds of clattering with nobody speaking, whisper produced `(knocking)`
   and `(thud)` — both labelled **English**, so `ASR_LANGUAGES` passed them straight
   through to the LLM as if they were questions.
2. **Real speech decodes worse**, because it arrives buried in the same noise.

**Fix.** Three defences, all in `config/asr.env` and live on Config → Listening.
`src/robot/asr/noise.py` carries the measurements behind every default.

| knob | default | what it does |
|---|---|---|
| `ASR_NOISE_GATE_DB` | `6.0` | drops an utterance whose speech level is not this far above the room's own floor, **before** it is decoded |
| `ASR_MAX_NO_SPEECH` | `0.6` | drops a transcript whisper itself scored as having no speech in it |
| `ASR_MIN_LOGPROB` | `-1.0` | drops a transcript whisper found incoherent |
| `ASR_DENOISE` | `off` | GTCRN suppression. See below — the default is measured, not assumed |
| `vad_threshold` | `0.5` | raise to 0.6–0.7 first; it is the cheapest change and needs no restart |

The confidence gate is the one that does the work, because whisper reports how much it
believed itself and this pipeline used to throw that away. Measured on this board:

| | `no_speech_prob` | `avg_logprob` |
|---|---|---|
| speech, clean down to 0 dB SNR | 0.006 – 0.087 | −0.06 – −0.63 |
| room noise, no speech in it | **0.820 – 0.911** | −0.70 – −0.76 |

Plus two shapes whisper only ever writes on noise: a bracketed sound effect, and one
token repeated to the end of the segment. Qwen3-ASR reports neither number, so under
`ASR_ENGINE=sherpa` only the shape checks and the level gate apply.

**Nothing is ever dropped quietly.** Every rejection is one INFO line with the numbers it
was rejected on, and an `ignored` event on the dashboard and the panel:

```
grep 'robot-asr: dropped' .cache/tmp/asr.log
robot-asr: dropped 0.46s of audio: no speech in it (whisper says 0.88, limit 0.60)
            -- it decoded as '(knocking)'
```

If the robot has gone *deaf* instead, that grep is also the answer: a gate set too tight
says so on every utterance it eats. `ASR_NOISE_GATE_DB=0` and `ASR_MAX_NO_SPEECH=1.0`
turn the two gates off completely.

## Noise suppression made it worse, and that is why it is off

**Cause.** The obvious fix for a noisy room is to denoise the microphone, and it was
built: GTCRN inside sherpa-onnx, no new dependency, 523 KiB, `ASR_DENOISE=stream`. Then
it was measured against whisper, and it **lost at every SNR tried** — +27% to +314% word
error rate on stationary noise, +58% to +215% on babble. Against Qwen3-ASR it came out
roughly neutral.

That is the known artefact problem: whisper was trained on hours of genuinely noisy
audio and is robust to it, while a denoiser's output is a *different* kind of damaged
that the model has never seen. Cleaning the audio moved it out of distribution.

**Fix.** It stays off, and it stays available, because none of that was measured in
**your** room — synthetic mixtures have no reverberation and no far-field speech.
Settle it there, in about a minute:

```bash
arecord -f S16_LE -c1 -r16000 -d 30 -D pulse /tmp/busy.wav   # the room that is failing
./scripts/models/fetch_denoise.sh
./scripts/measure/denoise_ab.py --noise /tmp/busy.wav
```

It prints, per clip and per SNR, the word error rate with suppression off and on, and
separately how many utterances the endpointer opens on the noise alone. If it wins
there, `ASR_DENOISE=stream` (before the VAD, RTF 0.12 of one core, +4 MiB) or
`utterance` (after it, +19 MiB, ~0.1× the utterance on every decode).

## `request (N tokens) exceeds the available context size` — and the robot says it is not ready

**Symptom.** Turns intermittently fall back to the canned unavailable line. `core.log`:

```
ERROR robot-core: llm unavailable: llama.cpp returned HTTP 400:
      request (2065 tokens) exceeds the available context size (2048 tokens)
```

**Cause.** The prompt outgrew the window. Four retrieved chunks of up to `CHUNK_CHARS`
each is roughly 1600 tokens, which does not fit a 2048-token context beside a persona,
the tool rules and any history. `LLM_CTX` was cut to 2048 in M9 so a crashed LLM could
restart at all (memory-budget.md), so the window is not going back up. **A full knowledge
store was indistinguishable from a broken model.**

**Fix.** The orchestrator now fits the prompt before sending it rather than after being
refused: `prompt.trim_context` drops the worst-ranked retrieved notes, then
`prompt.trim_history` drops the oldest exchanges, against a budget derived from `LLM_CTX`
at startup. The system prompt and the question being answered are never dropped.
Trimming runs before `alternate()`, because dropping an exchange can leave the window
opening on an assistant turn — the shape Gemma's template refuses.

Watch for `dropped N retrieved chunk(s) to fit the context` in `core.log`. Persistent
dropping means `RAG_K` is too high for the context, not that anything is broken.

## The Supertonic voice cannot be found on either page

**Symptom.** `TTS_ENGINE=supertonic` is set and the robot speaks with it, but there is
no way to choose which of its voices — nothing on Config → Speaking, nothing on Memory.

**Cause.** Supertonic was added as an engine without a control for the thing that makes
it worth having. The catalogue held one row for the model with its ten styles in
`meta`, but `Model.as_dict()` did not serialise `meta`, so the dashboard never saw them;
Config offered only the three Piper `voice_*` settings, which name checkpoints that do
not exist under this engine; and the Memory page's three per-language pickers were the
wrong shape for an engine whose entire point is one voice for all three.

**Fix.** A `supertonic_voice` setting whose options come from the style files actually
on disk, so a voice added later appears without a code change. It is disabled with a
reason when Supertonic is not installed, and when it is installed but not selected —
a control that silently applies to an engine that is not speaking is worse than none.

Preview is now driven by `Setting.preview_langs` rather than a `voice_` name prefix, so
one control can offer several buttons: the Piper voices get one, Supertonic gets three
(id, en, ar), because which voice suits Arabic is not obvious from how it reads English.
Hearing one never adopts it — the style travels with the utterance, not the config.

To compare all ten outside the dashboard: `python scripts/measure/voice_samples.py`
writes `run/voice_samples/<style>_<lang>.wav`.

**A test caught the other half of this.** `ui_smoke.py` previewed the hard-coded name
`en_US-lessac-medium`, which failed the moment the engine changed — correctly, with
`no voice 'en_US-lessac-medium'; have F1, F2, ...`. It now auditions whatever the
configured engine actually offers.

## The robot still answers its own voice in no_interrupt mode

**Symptom.** `CORE_LISTEN_MODE=no_interrupt` is set, and the robot still transcribes
what it just said and answers it.

**Cause.** The first version of this gated the *transcript*, and that is too late. An
utterance that starts while the robot is talking is still being decoded when it stops:
whisper `large-v3-turbo` takes the best part of a second, and 650 ms of endpointing
silence comes before that. By the time the text arrives, playback has ended and the tail
has passed, so the check sees a quiet room and lets the echo through.

**Fix.** The microphone is now shut rather than ignored. `robot-asr` takes a
`{"type":"gate","open":false}` message and discards frames while it is closed; the
orchestrator closes it when audio starts and reopens it `CORE_LISTEN_TAIL_MS` after the
last sample. Closing also rebuilds the endpointer, so a half-captured utterance is
forgotten rather than resuming when the gate reopens.

Look for this in `asr.log`, which is what proves it is working:

```
robot-asr: microphone open again (882 frames dropped while the robot spoke)
```

The level meter keeps moving while the gate is shut — you can still *see* the robot
hearing itself on Config → Listening — it simply cannot become an utterance.

`interrupt` mode never closes the gate. Hearing yourself is the price of being able to
talk over the robot, and that is the trade that mode exists to make.

## Which processes are on the GPU

`python scripts/measure/whats_running.py` has an `on` column, read from each process's
open file descriptors (`nvgpu`, `nvhost`, `nvmap`) rather than from its flags — a
`llama-server` told to put 99 layers on the GPU still runs on the CPU if the allocation
failed, and that is exactly the case worth catching. The Services page shows the same
per row.

It also warns when more than one process is on the GPU:

```
! 2 processes are on the GPU (llm, whisper). They share one unified pool with the CPU,
  and NvMap will not reclaim page cache for either -- this is what makes
  `cudaMalloc failed` show up at startup.
```

That is not an error, but it is the reason the LLM intermittently cannot allocate its KV
cache on a board running whisper with `WHISPER_USE_GPU=1`.

## The robot pauses after every sentence

Fixed in M10, but the shape is worth knowing because it recurs anywhere a renderer feeds
a real-time sink through a pipe.

`aplay` consumes at real time behind a ~64 KB buffer, so writing a rendered sentence into
it blocks until that sentence has nearly finished playing. When rendering and playback
were one loop, the next sentence therefore did not start rendering until the previous one
had almost ended -- every sentence boundary cost a whole synthesis, about two seconds at
Supertonic's original RTF of 0.75.

`robot.tts.service` now runs `run_synth` and `run_player` as separate tasks with a bounded
queue between them. If it ever comes back, check that both are started in `main()` and
that `_audio` is not being awaited from inside the render path.

Two knobs if synthesis is still not keeping up:

    SUPERTONIC_STEPS=4        flow-matching iterations; cost is linear, 8 -> 4 is ~1.8x
    TTS_BATCH_SENTENCES=4     join queued sentences into one call

## `CUDNN_STATUS_INTERNAL_ERROR` from robot-tts

    Supertonic failed on the GPU (... CUDNN failure 4000 ...); rebuilding on the CPU

`TTS_USE_GPU=1` and there is not enough GPU memory. **This is handled** -- robot-tts
rebuilds its ONNX sessions on the CPU in ~1.4 s and finishes the sentence -- so it costs
one slow reply, not a mute robot. It tries the GPU once per process, not once per
sentence.

Two things make it confusing:

* **It fails at the first `run()`, not at session creation.** cuDNN allocates its
  workspace lazily, so the sessions report `CUDAExecutionProvider` and *then* fail. Never
  conclude from a provider list that the GPU is working.
* **`free` is what matters, not `available`.** NvMap will not reclaim page cache for
  CUDA, so `free -m` can show 1.4 GB "available" and still refuse an 884 MiB allocation.
  Look at the `free` column.

Cheapest fixes, in order: `WHISPER_USE_GPU=0` in `config/whisper.env` (frees ~280 MiB),
a smaller whisper checkpoint, a smaller LLM, or `TTS_USE_GPU=0` -- which costs nothing
extra now that the GPU wheel is installed, since the CUDA libraries only load when the
CUDA provider is used.

Check where it actually ended up with `./scripts/measure/whats_running.py`, which reads
each process's open file descriptors rather than its flags.

## The robot answers Indonesian questions from a document but invents answers in Arabic

The symptom is specific and it looks like a model problem. It is not.

`EMBED_MODE=on-demand` puts the embedding server to sleep after `EMBED_IDLE_S`, and
`EMBED_RETRIEVAL=warm` means "use vectors when it happens to be up, keywords otherwise".
So after 90 idle seconds, retrieval silently becomes an FTS5 keyword search.

Keyword search works fine for a question asked in the same language as the document --
Indonesian words match Indonesian text -- so nothing looks broken. An Arabic question
against an Indonesian document shares no words at all, retrieves **nothing**, and the
model then answers from its own head. Measured on the Tazakka set: 0 chunks retrieved,
and two plausible invented names.

Check which mode a turn used -- it is in the log for every retrieval:

    grep 'robot-rag: retrieved' .cache/tmp/core.log | tail
    ... retrieved 4 chunks for '...' by vector (best 0.694)     <- good
    ... retrieved 4 chunks for '...' by keyword (best -1.769)   <- monolingual

There was also a bug here, fixed in M10: `EmbedServer.warm` reported the pool's memory of
what **it** started, so a `robot-embed` started by `run_stack.sh` or systemd was ignored
and retrieval fell back to keywords with a perfectly healthy server sitting idle. The
health poll now calls `EmbedServer.adopt()` with what is actually running. If you see
`adopted a running embedding server this process did not start`, that is it working.

If cross-lingual questions matter for your install, the fix is to keep the server
resident -- `EMBED_MODE=always` in `config/embed.env`, about 810 MiB -- and that is
usually paid for by dropping the LLM from 4B to a 2-3B alternative. See the bake-off
table in `memory-budget.md`.

## A document ingested but the robot says it does not know

Check it actually stored chunks:

    ./scripts/ingest.sh --list

A source with 0 chunks means the text never made it. For a PDF this is almost always a
scanned one -- images, no text layer. `read_pdf` refuses those with a reason rather than
storing an empty document, so look for `has no extractable text` in the log. There is no
OCR on this robot.


## The robot got slow, and every gauge says it is healthy

Symptom: "hearing" and "transcribing" go by at a normal speed, then it sits on
"speaking" for ten or fifteen seconds before a word comes out. The Services page is
green, `accelerator` says GPU for all four engines, and nothing is in the logs.

Look at swap, per service, not at RSS:

    ./scripts/measure/whats_running.py

    service    state     on       RSS    swap
    tts        running   GPU    1013M    799M

Measured on the robot, 2026-09-15: with 799 MiB of robot-tts paged out, a ~21-character
first chunk took **10.5 s** to reach the speaker. The same chunk, same GPU, same
`supertonic_steps`, with its pages resident: **609-909 ms**. The process never reports an
error because nothing failed -- every page just has to come back from
`/media/jetson/DATA/swapfile2` first, and synthesis touches nearly all of them.

`free -m` shows the total but not the victim, which is why the per-service column exists.
The Memory page now names it too: *"tts has 799 MiB paged out to swap"*.

**Do not tune the pipeline against this.** `CORE_FIRST_MAX_CHARS`, `supertonic_steps` and
`vad_min_silence` are all real levers on a board that fits; against swap they do nothing.
Steps 4 → 3 → 2 measured 909 / 609 / 749 ms -- less than the noise between runs.

The fix is room. `./scripts/models/list.sh` prices every installed model; the sum of the
selected ones plus ~2.3 GB of JetPack-and-tooling has to stay under 7620 MiB. Note what
that 2.3 GB contains: with the stack stopped, a VS Code Remote server was holding about
1400 MiB and an agent session another ~500 MiB. **A selection that thrashes while you are
developing over SSH may fit perfectly headless**, so measure `free -m` with the stack
stopped before blaming a model.

## whisper-server dies at startup with a ggml backtrace

    ggml_backend_cuda_buffer_type_alloc_buffer: allocating 1548.69 MiB on device 0:
        cudaMalloc failed: out of memory
    alloc_tensor_range: failed to allocate CUDA0 buffer of size 1623915520
    ggml/src/ggml-backend.cpp:194: GGML_ASSERT(buffer) failed

Not a corrupt model and not a build problem: `large-v3-turbo` wants **1548 MiB in one
contiguous CUDA allocation**, and the board did not have it. This is an abort, not a
fallback -- the process is gone and the robot is deaf, with `WHISPER_REQUIRE_GPU` never
reached because loading never finished.

It is decided by load order, so it bites hardest when restarting one service by hand into
a board the others have already filled:

    ./scripts/run_stack.sh svc start whisper     # 997 MiB free -> aborts
    ./run.sh all                                 # LLM, then whisper, then the rest -> fits

If a full restart also fails, the selection genuinely does not fit; pick a smaller
`WHISPER_MODEL` or a smaller `LLM_MODEL` on the Memory page.

## `tts_smoke.py` hangs, or reports `KeyError: 'first_audio_ms'`

Both were bugs in the harness, fixed 2026-09-15, and both are worth knowing if you write
another one against this protocol:

* `first_audio_ms` rides on **`started`**, not on `finished`. Reading it off `finished`
  raises `KeyError` on the first utterance.
* `finished` is only sent once the turn is closed. A client that sends `speak` and then
  waits **hangs forever** -- it has to send `speak_end` too, which is what robot-core
  does. The symptom is a smoke test that produces one `started` line and never returns.

The script now also prints RTF. Above 1.0 the robot cannot synthesise as fast as it
speaks, and every chunk after the first arrives late.

## The robot says "Baiklah, sebentar ya" and then answers in English

Two different languages in one turn, and they come from two different decisions.

The acknowledgement is chosen from the recogniser's language id the instant the
transcript lands -- before the LLM has written a token, because that is the point of it.
The answer's language is decided later, by `language.resolve()`, which also sees the
script the model is actually writing in and the model's own `language` declaration. When
those disagree, the turn is already speaking.

Check which hint the acknowledgement used:

    grep 'robot-core: turn .*filler' .cache/tmp/core.log

`filler` and `language` in the same turn record are the two decisions. If `asr_language`
differs from `language`, whisper heard one language and the model wrote another -- that
is the mismatch, and it is usually a very short utterance where whisper had little to go
on. `CORE_FILLER=0` turns the acknowledgement off if the mismatch matters more than the
latency it hides.

A **bare greeting** is deliberately exempt: "halo" and "hello" are the same sound, so
both the acknowledgement and the answer are forced to `CORE_DEFAULT_LANGUAGE`.

## The acknowledgement is slow, or arrives after the answer

It is pre-rendered; if it is slow, the cache is not being used. Ask the speaker:

    python - <<'PY'
    import asyncio, sys; sys.path.insert(0, "src")
    from robot.common import ipc
    async def main():
        p = await ipc.connect("run/tts.sock"); m = p.messages()
        await p.send({"type": "ping"}); print(await m.__anext__())
    asyncio.run(main())
    PY

`prerendered` should be 60 and `prerender_hits` should climb with every spoken turn.

* **`prerendered: 0`** -- the pass never ran or never finished. `grep 'pre-render'
  .cache/tmp/tts.log`: `stopping pre-render at ...` means the GPU was full at startup,
  which is a memory problem, not a speech one. It retries on the next robot-tts connect.
* **`prerendered: 60` but `prerender_hits: 0`** -- the phrases are cached under a voice
  or step count that is no longer configured. Changing either clears the cache on
  purpose (`acknowledgement cache cleared; it will refill`); if it did not refill,
  restart robot-core so it re-sends `prerender`.

A cache hit is **123 ms** to first sound on this board, which is `Player.OPEN_SETTLE`
plus spawning `aplay` and nothing else.

## `first_audio_ms` looks better than the robot sounds

Two separate causes, both real, both fixed on 2026-09-16 -- and worth knowing if you are
reading older measurements:

1. **It now measures the acknowledgement.** With `CORE_FILLER=1` the first sound IS the
   acknowledgement, which is honest but is not the answer's latency. `first_chunk_ms` is
   the number to compare against older runs.
2. **It used to be sent after the whole first chunk was written to the device.** `aplay`
   holds about 64 KB -- 0.74 s of 44.1 kHz mono -- so any longer chunk blocked that write
   until the speaker caught up, and the reported figure included however much audio did
   not fit in the pipe. `started` is now sent after the first slice. Measurements from
   before this are pessimistic, by more the longer the first chunk was.

## The acknowledgement cache empties itself at startup

Symptom: `pre-rendered 60 acknowledgement(s)` in the log, but `ping` reports
`prerendered: 23` and `prerender_hits: 0` — fewer than were rendered, and never a hit.

robot-core pushes the current voice, step count and volume to robot-tts on every
connect. That arrives as `configure`, and every key present in the message counted as
"applied" whether or not the value had changed — so a perfectly ordinary startup looked
like a voice change and cleared the cache the pre-render pass was still filling.

Fixed 2026-09-16: only a real change to the voice signature (steps, Supertonic voice,
per-language checkpoints) invalidates. Volume is deliberately NOT in that signature —
gain is applied at playback, after the cache, so changing it cannot make a rendering
stale.

If you see it again, `grep 'acknowledgement cache cleared' .cache/tmp/tts.log` and check
what was reconfigured immediately before it.

## The voice plays slow, or fast, and not the same way twice

Investigated 2026-09-17. Rule out the audio path first — it is the obvious suspect and
it was innocent:

    # does playback keep time, and at the right pitch?
    parecord -d alsa_output.<sink>.monitor --rate=16000 --channels=1 --format=s16le --raw

Send a known tone through `robot.tts.player.Player` and FFT what the monitor hears.
Measured here: 3.000 s of audio played in 3.138 s with a 5 ms spread, and 440 Hz sent
came back as 440 Hz — idle *and* while llama-server was generating. Supertonic is also
bit-deterministic: the same text rendered six times gave identical `audio_s`, at 4, 8
and 16 steps.

Note for anyone reading `pactl list sinks` in alarm: **this speaker is 16 kHz mono only**
(`cat /proc/asound/card0/stream0`) while Supertonic renders at 44.1 kHz, so PulseAudio
resamples 2.76:1 on every sample. It is real CPU work on a full board, but it keeps
perfect time and pitch. It is not the cause.

The two real causes are both about what reaches the synthesiser:

**Fixed padding per call.** Render a phrase one, two, three and four times and fit a
line: the slope is real speech, the intercept is padding.

    "Oke."   x1 1.32  x2 1.46  x3 1.88  x4 2.37   ->  0.35 s each + 0.98 s padding

74% of that chunk is silence, and the first chunk of a reply is short by design, so it
drawls while later chunks sound normal. `TTS_TRIM_SILENCE=1` cuts it, asymmetrically:
`TTS_TRIM_KEEP_MS` at the start (latency), `TTS_TRIM_TAIL_MS` at the end (the sentence
boundary). If sentences start running together, the tail is too short; if the robot
sounds hesitant, it is too long.

**Batching.** `TTS_BATCH_SENTENCES>1` joins queued chunks into one call, and a joined
call keeps the model's own inter-sentence pauses while separate calls do not — 15.0 vs
18.0 chars/s for the same words. Whether it happened depended on how fast the LLM
produced the text, so the same reply came out 19% longer or shorter turn to turn.
`TTS_BATCH_SENTENCES=1` makes it one path always (0.1% apart, measured).

**Check before believing any of these numbers: is the board swapping?** A mid-test
`whats_running.py` showed llm with 1234 MiB paged out, and that run reported 27 s to
first audio and half the expected `audio_s`. Those readings are artefacts, not speech.
`finished` now carries `chars` and `calls` so a short turn can be told from a fast one.
