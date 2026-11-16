# The hard constraints

Eight rules in [`AGENTS.md`](../../../AGENTS.md) can only be broken with a human's sign-off. They
are short because they are expensive: each one was bought with a failure or a measurement. **If a
task seems to need one of them, stop and ask. Do not work around it.**

| # | Constraint | Why, and the evidence | What it costs you |
|---|---|---|---|
| 1 | **Total steady-state RAM at most about 8 GB, aiming at 7 GB** | The Orin Nano has **unified** memory, shared by the CPU and the GPU, and **7620 MiB** of it is usable. Over the ceiling means OOM kills, not slowness. The full stack peaks near **6.5 GB** | Every new dependency has to be priced in `memory-budget.md` first |
| 2 | **One LLM resident at a time** | Two language models do not fit. Unload before load | A model change is a **reload**: whisper and the voice stop, the LLM loads, they come back (about 30 s deaf and mute) |
| 3 | **No ROS 2, micro-ROS or DDS** | One process talking to one microcontroller over one wire is a request/response problem, not a middleware problem | No `tf2`, Nav2 or MoveIt. See [Rejected](rejected-alternatives.md) |
| 4 | **No PyTorch, TensorRT-LLM, NeMo or `transformers` in the runtime path** | They are hundreds of MB to GB for no runtime benefit. `torch` alone is 1.5 GB on disk before a CUDA context. whisper.cpp reaches the identical weights through a binary of about 30 MB | Models must come as GGUF or ONNX. Fine in a separate offline tooling environment |
| 5 | **The Jetson runs headless**: no desktop, no X11, no Wayland, no display manager | A desktop session costs about **600 MB**. "Headless" means no desktop environment, **not** no display: the on-robot panel renders straight to the display hardware | Screens are the browser dashboard or the Slint panel |
| 6 | **The body owns all real-time control** | The Jetson never generates PWM, runs a PID loop or drives a motor. It sends intent; the body executes | All kinematics and control live in the body's firmware |
| 7 | **The body's heartbeat watchdog is mandatory** | If the Jetson goes silent for more than **500 ms** the body zeroes the motors **on its own**. The Jetson will eventually hang or be killed | Never rely on the Jetson to send `STOP`. See [The watchdog](../03-firmware/watchdog.md) |
| 8 | **No secrets in git-tracked files** | Credentials go in `.env`, which is gitignored | Config files hold no passwords or tokens, and neither do these pages |

> **Constraints 6 and 7 say "STM32".** The robot's body is an ESP32 (reported by the owner). The
> *rules* apply to whatever the body controller is; the *word* is out of date in 63 tracked files.
> Renaming touches the constraints table, so it needs sign-off and has not been done. See
> [Waiting for the owner](open-decisions.md).

## The rules that follow from them

### Language and runtime

* **Jetson side: Python 3.10** with `asyncio`, because JetPack 6.2 ships 3.10 and the job is
  coordinating concurrent I/O, not computing. Heavy work is already in native engines (llama.cpp,
  whisper.cpp, ONNX). No Rust or C++ on the Jetson side unless profiling proves a hot loop.
* **One approved exception: `face/`, the on-robot panel, is Rust** (signed off 2026-09-09). Slint's
  Python bindings stop at an alpha for this platform and Python version, and Rust was the only way
  to run a supported release. It is a separate process that imports nothing from the Python side.
* **Body side: C** (C++ acceptable). No MicroPython or any interpreted runtime: it would undermine
  the determinism that is the whole reason to have a microcontroller.

### Working rules for anyone changing the code (human or AI)

1. **Ask before adding a dependency** that is not in `TECH_STACK.md`, with its RAM cost and why an
   existing one cannot do the job.
2. **Follow the roadmap order.** Each milestone runs before the next; do not build the UI before the
   pipeline works headless.
3. **Every long-running component is a supervised service** that survives a crash.
4. **Measure, do not assume.** Record actual RAM in `memory-budget.md` next to the estimate.
5. **Update the docs in the same change**, add a dated entry to `PROGRESS.md`, and for every bug
   fixed add a symptom, cause and fix to `TROUBLESHOOTING.md` plus a regression test. A service
   change is not done until it has been read under **both** supervisors.
6. **A human approves architecture changes.** Implementation details are the implementer's;
   anything touching the table above needs sign-off.

### Definition of done for a feature

- [ ] Runs on the Jetson, headless, without a desktop session
- [ ] Measured RAM delta recorded in `memory-budget.md`
- [ ] Fails safe: a crash cannot leave motors running or the robot mid-speech forever
- [ ] Has a systemd unit if it is long-running
- [ ] Visible in the dashboard's health panel if it is a service
- [ ] Docs updated in the same change

## Two requirements that behave like constraints

They are not in the table but are treated as hard, because the failure they replace was silent:

* **Speech recognition and speech synthesis run on the GPU.** `WHISPER_REQUIRE_GPU=1` and
  `TTS_REQUIRE_GPU=1`. The measured reasons: decoding one utterance with the `small` checkpoint took 0.50 s on the GPU
  against **23.5 s** with a CPU-only build, and the GPU voice answers in a median 626 ms against 974 ms. Set either to 0 only
  deliberately.
* **Face data is biometric and stays on the robot.** `run/faces.db` and `run/people.db` are
  gitignored, nothing is sent anywhere, and there is **no anti-spoofing: never put anything that
  matters behind it.**
