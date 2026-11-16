# robot-py

Multilingual (English / Bahasa Indonesia / Arabic) offline conversational robot.
Jetson Orin Nano 8GB for the brain, STM32 for the body.

**Start with [`AGENTS.md`](AGENTS.md), then [`dev-docs/ai/START_HERE.md`](dev-docs/ai/START_HERE.md).**
Those two describe the hard constraints and the reading order; this file is only a map of the tree.

**New here, or want pictures first?** The illustrated handbook is [`docs/en/README.md`](docs/en/README.md)
([Bahasa Indonesia](docs/id/README.md)): what the robot is, how a conversation works, which program does
what, with diagrams and a badge on every claim saying how far it is proven.

**Where are we, what changed, what is next?** → [`dev-docs/ai/PROGRESS.md`](dev-docs/ai/PROGRESS.md)
(status, changelog, known issues, backlog). **Something broke?** →
[`dev-docs/ai/TROUBLESHOOTING.md`](dev-docs/ai/TROUBLESHOOTING.md), searchable by symptom.

## Layout

| Path | What lives here |
|---|---|
| `planning/` | Brief, tech stack, roadmap — the *why* and the build order |
| `dev-docs/` | Architecture, memory budget, serial protocol, build notes |
| `src/robot/` | Python: `core` (orchestrator), `asr`, `tts`, `vision` (camera: objects + faces), `ui` (dashboard + `static/` pages), `common` |
| `config/` | Per-service config. No secrets — those go in `.env` |
| `docs/` | The illustrated handbook for people (English + Indonesian): pictures, status badges, how the parts fit. Starts at `docs/en/README.md` |
| `firmware/stm32/` | PlatformIO project for the body — portable core + STM32 board layer. It is the reference implementation of the body protocol and what the tests run against; the robot's own controller is an **ESP32** speaking the same protocol (`docs/en/01-overview/glossary.md`) |
| `tools/stm32_sim/` | The firmware core built for the host, so the body can be tested without one |
| `deploy/systemd/` | One unit per service, all `Restart=on-failure` |
| `scripts/` | Bring-up checks, model fetch, RAM measurement, serial console |
| `models/` | Fetched model files — gitignored, ~4 GB (LLM, ASR, TTS, VAD, embeddings) |
| `vendor/` | Built third-party engines (llama.cpp) — gitignored |

## First steps on a fresh board

```bash
./scripts/bringup/check_board.sh        # verifies every M0 precondition, read-only
source scripts/bringup/env.sh           # pins pip/tmp/HF caches off the full root fs
./scripts/models/fetch_llm.sh           # ~2.4 GB
./scripts/models/fetch_whisper.sh       # ~470 MB, the speech decoder
./scripts/models/fetch_tts.sh           # the voices
sudo ./scripts/install_services.sh
```

## Adding a model later

```bash
./scripts/models/fetch_hf.sh <repo> <file>     # or paste a huggingface.co URL
./scripts/models/fetch_hf.sh --dry-run ...     # where would it go?
./scripts/models/list.sh                       # what is installed, and what it costs
```

It appears on the dashboard's Memory page within ~3 s — no restart, no page reload.
Installing does not select it: the Memory page prices it against the 7620 MiB ceiling,
and choosing it there is what applies it.

## The camera

An IMX219 on CSI gives the robot eyes: object detection and face recognition on the CPU, no
new dependency.

```bash
./scripts/models/fetch_vision.sh                       # ~57 MiB, once
./run.sh all                                           # starts robot-vision when a camera + the models exist
./scripts/measure/vision_smoke.py faces --enroll me.jpg=Deny --test other.jpg   # recognition, without a person
```

Open `/talk` (**Conversation**) on the dashboard: the thread (typed and spoken together), the picture and the pipeline log. The people the robot knows are on **Config → People**; what the model is
told. `VISION_ENABLED=0` in `config/vision.env` removes it entirely; `./run.sh <mode>
--no-vision` skips it once. Costs ~213 MiB. Details, measurements and limits (**not a
security feature**): [`dev-docs/modules/vision.md`](dev-docs/modules/vision.md).

## Testing the body without a microcontroller

```bash
./firmware/stm32/run_native_tests.sh           # 634 checks, gcc only
./tools/stm32_sim/build.sh
python scripts/serial/watchdog_test.py --sim   # kill -9 the brain; the wheels must stop
python scripts/serial/protocol_test.py --sim   # every documented line, both sides
```

## Running the whole thing

```bash
cp .env.example .env        # then set ROBOT_UI_PASSWORD -- the dashboard needs it
./run.sh all                # everything + the browser dashboard on :8081
./run.sh headless           # everything, no UI -- how the robot ships
./run.sh slint              # everything + the panel on the robot's own screen
./run.sh status | stop | info
./run.sh headless --check   # print the configuration it would use, start nothing
```

`./run.sh` drives `scripts/run_stack.sh` (add `--sim` for a simulated STM32). The three
modes differ only in which face is started; the voice pipeline is identical.

In `all` mode the dashboard is at `http://<jetson>:8081`. `/` is a public landing page; behind the
login are Dashboard, Conversation (`/talk`: typed and spoken turns, the camera and the pipeline's
activity log on one page), Knowledge (drag documents in), Memory (price a model choice before
applying it), Monitor, Services, Logs (live records and debug, and every conversation per person
with its summary) and Config. It is a separate process that reaches
the orchestrator only over a WebSocket, so closing or crashing it does not affect the
robot; one connection serves every open tab.

**Do not run the full stack and a dev session at the same time.** The stack peaks around
6.5 GB of the 7.6 GB available; a VS Code Remote session plus Claude Code is another
1–1.8 GB. Measured at 97% use and 3.2 GB of swap when both ran together.

## Production: systemd instead of `run.sh`

`./run.sh` is the development stack. A robot that is left running uses the units in
`deploy/systemd/`, which read the same `config/*.env` but run sandboxed -- and **have never
been booted on this board yet** (see `dev-docs/ai/PROGRESS.md` § Known issues #2):

```bash
sudo ./scripts/install_services.sh
sudo ./scripts/bringup/setup_sudoers.sh     # scoped start/stop/restart; REQUIRED for the
                                            # dashboard's buttons and on-demand embeddings
sudo ./scripts/bringup/setup_logging.sh     # journald is volatile on this image
sudo ./scripts/bringup/setup_udev.sh        # stable /dev/robot-stm32
sudo loginctl enable-linger jetson          # the units use the user's PulseAudio at
                                            # /run/user/1000, which must exist at boot
python scripts/hardening/failure_drills.py
```

## Hardening checks

```bash
python scripts/hardening/failure_drills.py     # every failure path in the architecture doc
python scripts/hardening/soak.py --minutes 60  # RAM creep, thermals, stuck motors, OOM
```

The drills kill each service for real and assert what the docs require: the STM32 stops
the motors when the orchestrator dies, the robot speaks a canned line when the LLM is
gone, a dead speaker does not lose the reply, and a turn never hangs.

## Two things that will bite you

**The root filesystem is ~96% full** (4 GB free on 2026-11-16). Models, build trees, pip cache and Docker images must
stay on the DATA partition. Always `source scripts/bringup/env.sh` first.

**`/dev/ttyACM0` is the USB soundcard, not the STM32.** It is a composite device that
also exposes CDC-ACM. Match on vendor id `0483`, and run `sudo scripts/bringup/setup_udev.sh`
to get a stable `/dev/robot-stm32`.

**Usable RAM is 7620 MiB, not 8192.** The rest is Tegra carveout. Budget against the real
number — see [`dev-docs/architecture/memory-budget.md`](dev-docs/architecture/memory-budget.md).
