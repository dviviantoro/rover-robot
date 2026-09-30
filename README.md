# robot-py

Multilingual (English / Bahasa Indonesia / Arabic) offline conversational robot.
Jetson Orin Nano 8GB for the brain, STM32 for the body.

**Start with [`AGENTS.md`](AGENTS.md), then [`dev-docs/ai/START_HERE.md`](dev-docs/ai/START_HERE.md).**
Those two describe the hard constraints and the reading order; this file is only a map of the tree.

## Layout

| Path | What lives here |
|---|---|
| `planning/` | Brief, tech stack, roadmap — the *why* and the build order |
| `dev-docs/` | Architecture, memory budget, serial protocol, build notes |
| `src/robot/` | Python: `core` (orchestrator), `asr`, `tts`, `ui` (dashboard + `static/` pages), `common` |
| `config/` | Per-service config. No secrets — those go in `.env` |
| `firmware/stm32/` | PlatformIO project for the body — portable core + STM32 board layer |
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

## Testing the body without a microcontroller

```bash
./firmware/stm32/run_native_tests.sh           # 628 checks, gcc only
./tools/stm32_sim/build.sh
python scripts/serial/watchdog_test.py --sim   # kill -9 the brain; the wheels must stop
python scripts/serial/protocol_test.py --sim   # every documented line, both sides
```

## Running the whole thing

```bash
cp .env.example .env        # then set ROBOT_UI_PASSWORD -- the dashboard needs it
./scripts/run_stack.sh start --ctx 2048 --sim --ui
./scripts/run_stack.sh status
./scripts/run_stack.sh stop
```

The dashboard is then at `http://<jetson>:8081` — Home, Chat, Headless (conversation plus
a live activity log), Knowledge (drag documents in), Memory (price a model choice before
applying it), Monitor, Logs (live records and debug) and Config. It is a separate process that reaches
the orchestrator only over a WebSocket, so closing or crashing it does not affect the
robot; one connection serves every open tab.

**Do not run the full stack and a dev session at the same time.** The stack peaks around
6.5 GB of the 7.6 GB available; a VS Code Remote session plus Claude Code is another
1–1.8 GB. Measured at 97% use and 3.2 GB of swap when both ran together.

## Hardening checks

```bash
python scripts/hardening/failure_drills.py     # every failure path in the architecture doc
python scripts/hardening/soak.py --minutes 60  # RAM creep, thermals, stuck motors, OOM
```

The drills kill each service for real and assert what the docs require: the STM32 stops
the motors when the orchestrator dies, the robot speaks a canned line when the LLM is
gone, a dead speaker does not lose the reply, and a turn never hangs.

## Two things that will bite you

**The root filesystem is ~94% full.** Models, build trees, pip cache and Docker images must
stay on the DATA partition. Always `source scripts/bringup/env.sh` first.

**`/dev/ttyACM0` is the USB soundcard, not the STM32.** It is a composite device that
also exposes CDC-ACM. Match on vendor id `0483`, and run `sudo scripts/bringup/setup_udev.sh`
to get a stable `/dev/robot-stm32`.

**Usable RAM is 7620 MiB, not 8192.** The rest is Tegra carveout. Budget against the real
number — see [`dev-docs/architecture/memory-budget.md`](dev-docs/architecture/memory-budget.md).
