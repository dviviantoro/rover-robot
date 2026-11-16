# Running it day to day

## The commands

```bash
./run.sh all          # everything + the browser dashboard on :8081
./run.sh headless     # everything, no UI: how the robot ships
./run.sh slint        # everything + the panel on the robot's own screen
./run.sh status       # what is running, and what it has loaded
./run.sh model        # which LLM / listening model / voice is chosen, configured, running
./run.sh info         # the long version
./run.sh stop
```

The three modes differ **only in which face is started**. The voice pipeline is identical, because
both screens are optional, independent clients of `robot-core` and nothing depends on them:
"a UI bug must not be able to stop the robot".

| Mode | Adds | Costs | Use it when |
|---|---|---|---|
| `all` | `robot-ui`, the dashboard | about 60 MiB | setting up or debugging |
| `headless` | nothing | 0 | how the robot is meant to run: speak to it. Configure it once from `all`, then settings persist |
| `slint` | `robot-face`, the panel on a screen attached to the robot | about 11 MiB, plus 8.4 MiB of display buffers | a display is connected |

`all` and `slint` do not run together by default, because on a 7620 MiB board the memory is usually
better spent on a larger model. `./scripts/run_stack.sh start --ui --face` does both.

### Flags that pass through

| Flag | Effect |
|---|---|
| `--check` | Print the configuration this run would use, **including dashboard pins**, and start nothing. Works on every mode |
| `--sim` | Also start the **simulated body** (`tools/stm32_sim`), so the heartbeat and the watchdog have something to talk to |
| `--no-asr` | Skip the microphone |
| `--no-embed` | Skip the embedding server |
| `--no-vision` | Skip the camera once. `VISION_ENABLED=0` in `config/vision.env` removes it for good |
| `--ctx N` | Override the LLM context size for this run |
| `--wav FILE` | Feed a recording instead of the microphone |

One service at a time, which is what the dashboard's Services page drives through the same spawn
functions the full start uses:

```bash
./scripts/run_stack.sh svc start|stop|restart llm|embed|tts|core|asr|body|ui|face
```

### What a start looks like

The order is the boot order from [the brain](../04-brain/README.md#the-order-they-start-in): LLM,
embeddings if not on-demand, whisper (checked to be on the GPU), voice, camera, core, microphone,
then the screens. Starting by hand beside the others can fail where a cold start works: whisper
wants a 1.3 GB contiguous block of GPU memory while the LLM wants about 2.8 GB.

## Where to look

| What | Where |
|---|---|
| Logs of every service under `./run.sh` | `.cache/tmp/`: `core.log`, `llama-server.log`, `asr.log`, `tts.log`, `embed.log`, `vision.log`, `ui.log`, `stm32_sim.log` |
| Logs under systemd | `journalctl -u robot-core -f` (needs the persistent journal: `setup_logging.sh`) |
| The conversation as it happens | `tail -f .cache/tmp/core.log` |
| What is loaded and on GPU or CPU | `./scripts/measure/whats_running.py` |
| Live, in a browser | the **Services**, **Monitor** and **Logs** pages |
| Everything the dashboard saved | `run/settings.json`, and `./run.sh <mode> --check` |
| Local sockets, data | `run/` (`asr.sock`, `tts.sock`, `vision.sock`, `core.sock`, `knowledge.db`, `people.db`, `faces.db`) |

`people.db` and `faces.db` are **personal data** (conversations and faces) and stay on the robot.
Do not copy them off, and do not commit them (`run/` is gitignored).

### One-line answers to "what happened?"

Each of these is a log line the code writes on purpose.

| Question | Command |
|---|---|
| Which retrieval mode did a turn use? (keyword search silently misses a question in another language) | `grep 'robot-rag: retrieved' .cache/tmp/core.log` says `by vector` or `by keyword` |
| Why was an utterance dropped, or why has it gone deaf? | `grep 'robot-asr: dropped' .cache/tmp/asr.log` |
| What language was decided, and why? | `grep 'recogniser said' .cache/tmp/core.log` |
| What context and reply budget are in force? | `grep 'context .* tokens =' .cache/tmp/core.log` |
| Which port is the body, and did discovery find it? | `grep 'robot-core: microcontroller' .cache/tmp/core.log` |
| Did the body get the phase's JSON? | `grep 'robot-serial: reply' .cache/tmp/core.log` |
| Did the keepalive restart something? | `grep 'is down; restarting' .cache/tmp/core.log` |

## Looking after it

| Task | Command | What it tells you |
|---|---|---|
| Is memory healthy? | `./scripts/measure/whats_running.py` | RSS and **swap** per service. A non-zero swap on `tts` means the GPU voice has stopped being the fast option |
| How fast does it answer? | `./scripts/measure/voice_latency.py --wav <recording> -n 5` | End of speech to first word. Median 2181 ms, best 1554 ms measured; target 1500 ms |
| Does the knowledge work? | `./scripts/measure/knowledge_qa.py` (add `--retrieval` for no LLM) | Whether it knows what was ingested, and how fast |
| Do the failure paths hold? | `python scripts/hardening/failure_drills.py` | Kills each service for real. 24 of 24 passed |
| Does it survive time? | `python scripts/hardening/soak.py --minutes 60` | RAM creep, thermals, stuck motors, OOM. A 45-minute soak (313 turns) was clean; **24 hours has not been run** |
| Is the body there? | Config → Hardware → *Ping all ports* | Which ports answered `{PING_robot}`, and how fast |

## Teaching it documents

```bash
./scripts/ingest.sh knowledge/Tazakka_Informasi_Ringkas.pdf
./scripts/ingest.sh --list
./scripts/ingest.sh --rebuild        # forget everything, re-ingest knowledge/
```

Or drag files onto the **Knowledge** page. **Never leave test data in the store**: chunks from smoke
runs compete with real documents for the top results. See [Knowledge](../04-brain/knowledge.md).

## Rules of thumb

* **Check swap before believing any latency number.** A board that is over-committed makes every
  other measurement meaningless.
* **Configure from `all`, ship `headless`.** Settings persist, so there is no need to leave a
  screen running.
* **Prefer `/dev/serial/by-id/...` to `ttyACM0`.** Discovery saves the stable name for you.
* **Do not run `pkill -f <pattern>` from a shell whose own command line contains the pattern**: it
  kills the shell. This happened twice ([`TROUBLESHOOTING.md`](../../../dev-docs/ai/TROUBLESHOOTING.md)).
* **After changing a service, read the change under both supervisors**, `run_stack.sh` and the
  units.
