# AGENTS.md — Root AI Contract

**Project:** Multilingual conversational robot on NVIDIA Jetson Orin Nano 8GB
**Scenario:** New project (no code yet)
**Doc convention:** docs-ai v1.4.0 style — `planning/` and `dev-docs/` are output folders

---

## Read this first

1. `dev-docs/ai/START_HERE.md` — reading order and current state
2. `planning/PROJECT_BRIEF.md` — what we are building and why
3. `planning/TECH_STACK.md` — every dependency, with rejected alternatives
4. `dev-docs/architecture/system-overview.md` — process layout and turn flow
5. `dev-docs/architecture/memory-budget.md` — the 8GB ceiling (hard constraint)
6. `dev-docs/architecture/serial-protocol.md` — Jetson ↔ STM32 contract
7. `planning/ROADMAP.md` — build order

---

## Hard constraints — do not violate without asking

| # | Constraint | Why |
|---|---|---|
| 1 | **Total steady-state RAM ≤ ~8 GB**, target ≤ 7 GB with headroom | Jetson Orin Nano 8GB has unified CPU/GPU memory. Exceeding it means OOM kills, not slowdown. Every new dependency must be checked against `memory-budget.md`. |
| 2 | **One LLM resident at a time** | No hot-swapping two models into memory simultaneously. Unload before load. |
| 3 | **Do NOT introduce ROS 2, micro-ROS, or DDS** | Deliberate architectural decision. See `planning/PROJECT_BRIEF.md` § Rejected. If a task seems to need it, stop and ask. |
| 4 | **Do NOT add PyTorch, TensorRT-LLM, NeMo, or transformers** to the runtime path | The runtime uses lean C++/ONNX engines only. These frameworks are hundreds of MB to GB of RAM for zero runtime benefit here. Fine in a separate offline/tooling venv. |
| 5 | **Jetson runs headless** — no desktop environment, no GNOME, no X11, no Wayland, no display manager | A desktop costs ~600 MB of the budget. UI is either browser-based over the network (`robot-ui`) or rendered **straight to DRM/KMS** on a panel attached to the robot (`robot-face`, Slint). Neither needs a desktop session or a compositor: measured, the service user takes DRM master on `/dev/dri/card1` directly. "Headless" means no desktop environment — not no display. |
| 6 | **STM32 owns all real-time control** | Jetson never generates PWM, runs PID loops, or drives motors directly. It sends intent; STM32 executes. |
| 7 | **STM32 heartbeat watchdog is mandatory** | If the Jetson goes silent >500 ms, STM32 zeroes the motors on its own. Never rely on the Jetson to send `STOP`. |
| 8 | **No secrets in files tracked by git** | Config with credentials goes in `.env` (gitignored). Never in `.md` or committed YAML. |

---

## Language & runtime rules

- **Jetson side: Python 3.10** (JetPack 6.2 ships 3.10). `asyncio` for the orchestrator.
- **STM32 side: C (or C++)**. STM32CubeIDE + HAL, or Arduino framework for STM32.
- No Rust, no C++ on the Jetson side unless profiling proves a specific hot loop needs it.
  **One approved exception: `face/`, the on-robot panel, is Rust.** Signed off 2026-09-09. Slint is the UI toolkit and its Python bindings stop at 1.9.2a1 (alpha) for aarch64/cp310, while JetPack ships Python 3.10 and current Slint is 1.17.1 — Rust was the only way to run a supported release. It is a separate process that imports nothing from the Python side and talks to `robot-core` over the same websocket the dashboard uses.
- Prefer calling native engines (llama.cpp, sherpa-onnx, Piper) over reimplementing anything in Python.

## Package installation on Jetson

ARM64 wheels are frequently missing from PyPI. Before assuming a `pip install` works:

```bash
pip install <package> --extra-index-url https://pypi.jetson-ai-lab.io/jp6/cu126
```

If a package still fails, check for a prebuilt container in `dusty-nv/jetson-containers`
before attempting a source build. Document any source build in `dev-docs/ai/BUILD_NOTES.md`.

---

## Working rules for the AI agent

1. **Ask before adding any dependency** that is not already in `planning/TECH_STACK.md`.
   State its RAM cost and why an existing dependency can't do the job.
2. **Follow the roadmap order** in `planning/ROADMAP.md`. Each milestone must run before
   starting the next. Do not build the UI before the pipeline works headless.
3. **Every component is a supervised service.** Anything long-running gets a systemd unit
   with `Restart=on-failure`. The robot must survive a component crash.
4. **Measure, don't assume.** After each milestone, record actual RAM usage (via `jtop`) in
   `dev-docs/architecture/memory-budget.md` next to the estimate.
5. **Update docs as a side-effect of work.** When a decision changes, edit the relevant doc
   in the same change — don't leave the docs describing a system that no longer exists.
6. **Human approves architecture changes.** Implementation details are yours; anything that
   touches the hard constraints table above needs sign-off.

---

## Running it

    ./run.sh all         everything + the browser dashboard on :8081
    ./run.sh headless    everything, no UI -- how the robot ships
    ./run.sh slint       everything + the panel on the robot's own screen
    ./run.sh stop | status | info

The three modes differ ONLY in which face is started; the voice pipeline is identical.
Both UIs are optional, independent websocket clients of `robot-core`, and neither is
depended on by anything. Add `--check` to any mode to print the configuration it would
use and start nothing.

## Knowledge

Source documents live in `knowledge/`; the store is `run/knowledge.db` (`KNOWLEDGE_DB`).

    ./scripts/ingest.sh knowledge/Tazakka_Informasi_Ringkas.pdf
    ./scripts/ingest.sh --list
    ./scripts/ingest.sh --rebuild            # forget everything, re-ingest knowledge/
    ./scripts/measure/knowledge_qa.py        # does it actually know it, and how fast
    ./scripts/measure/knowledge_qa.py --retrieval   # retrieval only, no LLM

Two rules learned the hard way, both in `TROUBLESHOOTING.md`:

* **Check which retrieval mode a turn used.** With `EMBED_MODE=on-demand` a sleeping
  embedding server silently downgrades retrieval to keyword search, which still works
  for questions asked in the document's own language and returns *nothing* for one asked
  in another. `grep 'robot-rag: retrieved' .cache/tmp/core.log` says `by vector` or
  `by keyword` for every turn.
* **Never leave test data in the store.** Chunks from smoke runs compete with real
  documents for the top-k. `ui_smoke.py` now removes what it uploads.

## STT and TTS run on the GPU. That is a requirement, not a preference.

One speech engine (**whisper.cpp**, chosen by SIZE in `config/whisper.env`) and one
speaker (**Supertonic**). Both must have the GPU, and both now say so instead of quietly
running slowly:

    WHISPER_REQUIRE_GPU=1   a whisper-server that came up on the CPU is stopped and
                            reported. `use gpu = 1` in its log is the REQUEST;
                            `CUDA0 total size` is the outcome, and that is what is checked.
    TTS_REQUIRE_GPU=1       a GPU that cannot be had is an error, not a slow day. The
                            sentence is dropped, the Services page goes red, and the NEXT
                            utterance retries -- so freeing memory recovers it with no restart.

Set either to 0 only deliberately. The failure both of them replace was silent: one log
line, scrolled away, and a robot that had simply got slower for no visible reason.

**The ASR model is `openai/whisper-large-v3-turbo`** — the same weights, converted to
GGML by `ggerganov/whisper.cpp`, not the PyTorch repo. The load log proves which
checkpoint it is: 32 encoder layers against **4 decoder layers**, `n_vocab 51866`,
`n_mels 128`. Four decoder layers is the turbo signature; large-v3 has 32.

The HF PyTorch repo is the same model and cannot be used here: it needs `transformers`
and PyTorch in the runtime path, which constraint 4 forbids and the budget could not pay
for anyway (torch alone is 1.5 GB on disk before a CUDA context). whisper.cpp reaches
the identical weights through a ~30 MB C++ binary.

**Prefer a quantised checkpoint over a smaller one.** Measured 2026-09-15 over nine
samples in all three languages, turbo q5_0 against turbo fp16: **656 MiB less resident,
223 ms faster per decode, and character-for-character identical transcripts.** Precision
is the cheap axis here; parameter count is the expensive one.

**Supertonic is a small model behind a large CUDA runtime.** The weights are 383 MiB on
disk; it is resident at 1.1-1.7 GB. Measured 2026-09-15 by restarting robot-tts on each
side: 472 MiB on the CPU, 1081 MiB straight after a GPU load, 1738 MiB after a few
utterances — cuDNN allocates its workspace lazily, so **the GPU cost grows with use.**
There is no lighter build to install: `Supertone/supertonic-3` ships exactly one set of
ONNX files, and all of them are already here. The only lever is the accelerator:

    27 utterances each, identical text, 4 steps, time to first word

    on GPU   1738 MiB   median 626 ms   first half 621 -> second half 633
    on CPU    472 MiB   median 974 ms   first half 974 -> second half 975

**The GPU is worth it: 348 ms on every single turn, and it does not decay under load.**
Both sides are flat across 27 utterances, so the lazily-grown CUDA workspace costs
memory but not time once it has grown.

**That holds only while it fits, and the cliff is not gradual.** The same GPU Supertonic
with 799 MiB of its pages in swap took **10.5 s** to say 21 characters — sixteen times
worse than the CPU it is beating here. CPU mode at 472 MiB would not have been swapped
out in the first place. So the GPU is the fast choice and the fragile one:

    ./scripts/measure/whats_running.py     # a non-zero `swap` on tts means GPU has
                                           # STOPPED being the fast option

Keep the headroom and stay on the GPU. Lose the headroom and `TTS_USE_GPU=0` is the
better robot, which is why the measurement is recorded rather than the conclusion.

**The LLM is the adjustable one.** It is the largest line and the one to shrink when the
other two cannot fit -- `LLM_MODEL` in `config/llm.env`, priced on the Memory page
against everything else before anything is loaded.

## The Memory page is measured, not estimated

Every line shows **Estimated** beside **Measured now**. The estimate is disk size times a
measured factor -- the only thing available for a model that is not loaded. The
measurement is that process's RSS from `/proc`, refreshed every 3 s with the health poll,
plus `/proc/meminfo` as ground truth for the board.

Where the two disagree, the measurement is right. That is not theoretical: it is how the
Supertonic line was found to be 884 MiB light, because nothing had ever priced its CUDA
context -- the page said a selection fit while the board was swapping 658 MiB.

## Installing a model

    ./scripts/models/fetch_hf.sh <repo> <file-in-repo> [kind]
    ./scripts/models/fetch_hf.sh <a huggingface.co URL pasted from the browser>
    ./scripts/models/fetch_hf.sh --dry-run ...     where would it go?
    ./scripts/models/list.sh                       what is installed, and what it costs

The destination is inferred from the filename, and it is the destination that decides
whether the catalogue can see the file at all — a GGUF that lands in `models/asr/` is
invisible however good it is:

    *.gguf       -> models/llm/           pass `embed` for an embedding model
    ggml-*.bin   -> models/asr/whisper/   the ASR choice, picked by size
    *.onnx       -> models/tts/piper/     fetches the .onnx.json too; a voice needs both

**It appears on the Memory page within ~3 s, with no restart and no page reload.**
robot-core rescans on the health poll and broadcasts only when the set has changed; a new
file is logged as `new model installed: <path>`. That is affordable because GGUF headers
are cached by (path, size, mtime) — a rescan was 4.3 s and is now ~2 ms.

Installing does not select. The Memory page prices a model against the ceiling first;
choosing it there is what writes the config and restarts what has to reload.

## How fast it answers, and what to turn

Measured end to end, from the moment speech actually ended: **2181 ms median, 1554 ms
best** (was 8209 ms). `./scripts/measure/voice_latency.py --wav <recording> -n 5` is the
measurement; it spaces turns so a reply finishes before the next starts, which the older
harness did not.

    endpointing   200 ms   vad_min_silence -- dead time on every turn
    ASR decode    510 ms   WHISPER_MODEL size
    first token   +92 ms   prompt size: rag_k, persona
    first clause +926 ms   the LLM writing something speakable
    synthesis    +963 ms   CORE_FIRST_MAX_CHARS -- first word ~ first chunk length

**Three things made tuning it impossible before, all of them silent:**

1. Latency was measured from when Silero *declared* speech over, not when it ended —
   optimistic by `VAD_MIN_SILENCE` on a 1500 ms target. robot-asr now sends `endpoint_s`.
2. ASR settings were applied at core startup, and `run_stack.sh` starts core *before*
   robot-asr — so every one of them was lost, every boot. Now pushed on connect.
3. `settings.all()` fills in registry defaults, and applying those at startup overwrote
   `config/core.env`. Startup now applies `settings.saved()` — only explicit choices.

**A dashboard pin beats both env files, and pins accumulate.** `./run.sh <mode> --check`
lists them. Clearing a stale pin is how you get an env file back.

## The fourth thing, and on a full board it is the only one that matters: swap

Those numbers are what the pipeline does when its pages are in RAM. They describe
nothing at all about a board that is over-committed, and the difference is not a
percentage:

    time to first word, one ~21-character first chunk, Supertonic on the GPU
      pages resident                 609-909 ms
      799 MiB of robot-tts in swap    10.5 s

Same GPU, same model, same steps, health endpoint green in both. Every page the
synthesiser touches has to come back from the swapfile first, and it touches most of
them. **Tuning `CORE_FIRST_MAX_CHARS` or `supertonic_steps` against this is wasted
work** — measured 2026-09-15, steps 4 → 3 → 2 moved the median by less than the
run-to-run noise (577-1726 ms), because none of it was compute-bound.

`./scripts/measure/whats_running.py` now prints RSS and swap per service and says so
out loud. Check it before believing any other latency number:

    service    state     on       RSS    swap
    llm        running   GPU    2816M    382M
    tts        running   GPU    1013M    799M   <- this is the slow reply
    whisper    running   GPU    1954M    232M

**The models have to fit, and "fits" means with the development environment attached.**
`./scripts/models/list.sh` prices them; the ceiling is 7620 MiB and the robot is not the
only thing on the board:

    measured 2026-09-15, stack stopped:  2260 MiB already used
      (JetPack + a VS Code Remote server at ~1400 MiB + an agent session)
    leaving ~5360 MiB, against 6529 MiB of models selected
      Llama-3.2-3B-Q4 2900 + whisper large-v3-turbo 2261 + Supertonic 1368

That deficit is the 2.8 GB of swap, and the swap is the 10.5 s. Headless with no editor
attached the same selection fits with ~700 MiB to spare — which is why a configuration
that demos badly over SSH can still be the right one to ship.

**A model that cannot get its allocation does not degrade, it aborts.** whisper
large-v3-turbo needs 1548 MiB *contiguous* on the GPU; started into a board that has
997 MiB free it dies in `whisper_model_load` with `cudaMalloc failed: out of memory` and
a ggml backtrace, and the robot is simply deaf. Load order is what decides this, so
`./run.sh <mode>` (which starts the LLM, then whisper, then the rest) can succeed where
restarting one service by hand fails.

## It says something while it thinks

~2.9 s separates the end of a question from the first word of an answer -- ASR decode,
the LLM reaching a speakable clause, synthesising it. None of that is removable. The
SILENCE is, and a person filling the same gap says "hmm, let me think".

    CORE_FILLER=1        config/core.env, and Config -> Acknowledge before answering

Twenty phrases per language in `FILLERS` (src/robot/core/language.py), drawn at random,
each about 1.4 s spoken. Voice turns only -- /chat is read, not heard.

**They acknowledge and nothing more.** The phrase is chosen from the transcript alone,
before the LLM has produced a token, so it cannot know what kind of utterance it was --
which rules out every "let me think", "let me check" and "interesting question". All of
those are correct after a question and absurd after "halo". `test_filler.py` holds the
banned vocabulary so this cannot drift back.

**They are pre-rendered to PCM, and that is the whole trick.** robot-core sends
`prerender` on every connect to robot-tts; 60 phrases cost 9.5 MiB held and ~30 s of
background work after startup. Measured on this board, all three languages:

    acknowledgement, cache hit     123-124 ms to first sound
    the same phrase, synthesised   420-620 ms

123 ms *is* `Player.OPEN_SETTLE` plus process spawn -- opening `aplay` is the entire
remaining cost, so there is nothing left in this path to optimise. `pong` reports
`prerendered` and `prerender_hits`, so "it came from the cache" is checkable rather
than believed.

Two things it deliberately does NOT do. It does not set `first_chunk`, which must keep
meaning "the LLM produced something speakable" -- an acknowledgement that improved the
pipeline's own numbers would be measuring itself. And it does not change the state:
the robot stays THINKING, because it is.

**`first_audio_ms` now measures the acknowledgement**, since that is genuinely the
first sound. `first_chunk_ms` is still the answer's own latency.

## A bare greeting is answered in the default language

"halo" and "hello" are the same sound, and so are "hai"/"hi" and "hei"/"hey". Whisper
must pick one and picks on acoustics alone, so a greeting is the one utterance where its
language id is close to a coin toss -- and being wrong does not just mistranslate the
greeting: this turn's answer becomes the next turn's hint, so "hi" can pin an entire
conversation to a language nobody chose.

`language.is_general_greeting()` routes those to `CORE_DEFAULT_LANGUAGE` (id) instead of
guessing. Two limits, both deliberate:

* **Whole utterance only.** "hello, what is the address" carries its own language after
  the comma and is left alone.
* **Latin script only.** No English word looks like مرحبا, so an Arabic greeting is
  answered in Arabic. Only the en/id collision is ambiguous, and only it is overridden.

## Why the voice sounded uneven, and what actually fixed it

Reported as "played slow or fast, not consistent". It was not the audio device and not
the model being random -- both were measured clean first:

    playback, 3.000 s of audio      3.138 s wall, spread 5 ms, idle AND under LLM load
    pitch through the real sink     440 Hz sent, 440 Hz heard, every run
    same text synthesised 6 times   identical to 0.00 s at 4, 8 and 16 steps

**The speaker is 16 kHz mono only** (`/proc/asound/card0/stream0`) and Supertonic renders
44.1 kHz, so every sample is resampled 2.76:1 by PulseAudio. That is real work on a
loaded board, but it keeps perfect time and pitch -- it is not the bug.

Two things were, and both are about what the synthesiser is *given*:

**1. Every synthesis call was padded with a fixed slab of silence.** Measured by
rendering a phrase one to four times and reading the slope:

    "Oke."          0.35 s of speech + 0.98 s of padding    74% silence
    "Sebentar ya."  1.00 s of speech + 0.46 s of padding

Output length is quantised too -- 1.32, 1.46, 1.88, 2.37 s -- so a four-character word
still cost 1.3 s. The first chunk of a reply is short by design
(`CORE_FIRST_MAX_CHARS=40`), so it was mostly padding and drawled, while later, longer
chunks sounded normal. `TTS_TRIM_SILENCE` cuts it. The content slope is unchanged
(2.480 → 2.428 s per repetition, inside the quantisation noise), so this removes silence
and not speech.

**The trim is asymmetric on purpose.** `TTS_TRIM_KEEP_MS=40` at the start, where every
millisecond is latency; `TTS_TRIM_TAIL_MS=280` at the end, because that is what a
sentence boundary sounds like. Trimming both ends equally made sentences run together.

**2. Batching made the same reply two different lengths.** `TTS_BATCH_SENTENCES` joined
already-queued chunks into one call. Joined, the model inserts its own inter-sentence
pauses (15.0 chars/s); separate, it does not (18.0 chars/s) -- and which happened
depended on whether the LLM had produced the text fast enough to still be queued. The
same words, 19% apart, turn to turn, for no visible reason. **`TTS_BATCH_SENTENCES=1`**
removes the choice:

    identical reply, chunks all at once vs 1.1 s apart
      before   12-19% apart
      after     0.1% apart  (9.67 s vs 9.66 s)

Playback stays gapless at batch 1 -- 0.2-1.3% silence across a 9.7 s reply -- so the
per-call overhead batching used to save is not worth paying for in consistency.

`finished` now reports `chars` and `calls` beside `audio_s`. A turn that rendered 159
characters into 5 s of audio has lost half its reply; without those two numbers that is
indistinguishable from a model that simply spoke quickly.

## The context window and the reply budget are one setting, not two

`LLM_CTX` (config/llm.env) is the window. `CORE_MAX_TOKENS` (config/core.env) is what the
reply may have of it, and it defaults to **`auto`** — derived as
`LLM_CTX - prompt budget (66%) - 128 margin`, capped at 1024.

    ./run.sh headless --check      Context  4096 tokens = prompt 2703 + reply 1024
    grep 'context .* tokens =' .cache/tmp/core.log

**To make answers longer, raise `LLM_CTX` and leave `CORE_MAX_TOKENS` alone.** Setting a
number is how you make them deliberately *shorter*; it is taken at face value.

Under `json` a reply that hits the cap is not merely short: the grammar is still inside
the `answer` string, so the object never closes and `language`, `foot_direction`,
`foot_step`, `hand_movement`, `face` and `style` are never generated. The robot speaks
half a sentence and does not move. It now says so — one WARNING naming the split, a
`truncated` event on the dashboard, and `truncated` beside `timed_out` in the turn
metrics. It used to be invisible: `finish_reason` came back from llama.cpp on every turn
and was discarded.

**A dashboard-pinned value beats both files.** That is how `CORE_MAX_TOKENS=260` sat in
config/core.env doing nothing — the registry default was applied over it on every
startup. Restart-scoped settings use `default=None` to avoid exactly this; `max_tokens`
now does too. `--check` lists what the dashboard has pinned.

## Listening in a noisy room

Three gates, all in `config/asr.env` and live on Config → Listening. `src/robot/asr/noise.py`
carries the measurement behind every default.

    ASR_NOISE_GATE_DB=6.0     drop an utterance too close to the room floor, before decoding
    ASR_MAX_NO_SPEECH=0.6     drop a transcript whisper itself scored as speechless
    ASR_DENOISE=off           GTCRN suppression -- measured, and it LOST

Two rules, both in `TROUBLESHOOTING.md`:

* **Read the drop log before changing a threshold.** Every rejection is one INFO line
  with the numbers it was rejected on -- `grep 'robot-asr: dropped' .cache/tmp/asr.log`
  answers both "why did it answer something nobody said" and "why has it gone deaf".
* **Do not turn suppression on without measuring it here.** It is off because denoising
  made whisper *worse* at every SNR tested (+27% to +314% word errors) -- a denoiser's
  artefacts are not the kind of noise whisper was trained on. The losing test was
  synthetic, so the real room gets the deciding vote:
  `./scripts/measure/denoise_ab.py --noise <30 s recorded in it>`.

## Definition of done for any feature

- [ ] Runs on the Jetson, headless, without a desktop session
- [ ] Measured RAM delta recorded in `memory-budget.md`
- [ ] Fails safe (a crash cannot leave motors running or the robot mid-speech forever)
- [ ] Has a systemd unit if it is long-running
- [ ] Visible in the UI health panel if it is a service
- [ ] Docs updated in the same change
