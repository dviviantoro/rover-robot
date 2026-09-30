# START_HERE.md

**Read `../../AGENTS.md` first if you haven't.** This file is the map.

---

## What this project is, in three sentences

A voice-driven robot that listens, thinks, learns, and speaks in **English, Bahasa Indonesia,
and Arabic**, running fully offline on a **Jetson Orin Nano 8GB**, with an **STM32** handling
all physical motion (mecanum drive, LED-matrix face, servo gestures). The Jetson sends intent
over a plain-text serial link; the STM32 does the real-time work. There is deliberately **no
ROS 2**.

---

## Reading order

| Order | File | Read it when |
|---|---|---|
| 1 | `../../AGENTS.md` | Always — hard constraints and working rules |
| 2 | `../../planning/PROJECT_BRIEF.md` | To understand goals and what was already rejected |
| 3 | `../../planning/TECH_STACK.md` | Before touching any dependency |
| 4 | `../architecture/system-overview.md` | Before writing orchestrator code |
| 5 | `../architecture/memory-budget.md` | Before adding anything that loads a model |
| 6 | `../architecture/serial-protocol.md` | Before writing STM32 or serial code |
| 7 | `../../planning/ROADMAP.md` | To know what to build next |

---

## Current state

**Last updated: 2026-09-14. M0–M9 built. M5 has no hardware; M9 needs its 24-hour run.**

> **One STT engine now.** Qwen3-ASR was removed on 2026-09-14. whisper.cpp is the only
> decoder, chosen by SIZE (`WHISPER_MODEL`), and STT and TTS both *require* the GPU
> rather than falling back to the CPU. See AGENTS.md § STT and TTS run on the GPU.

| Milestone | State |
|---|---|
| M0 Board bring-up | ✅ JetPack 6.2.1, MAXN_SUPER, headless, models on the DATA partition |
| M1 Think | ✅ `llama-server` + Qwen3-4B Q4_K_M, 4096 ctx, all three languages answer |
| M2 Speak | ✅ Piper synthesises all three; USB soundcard verified real-time. **Nobody has listened yet** |
| M3 Listen | ⚠️ Transcription word-perfect in all three, mic opens, VAD clean. **No wake word** |
| M4 First full loop | ✅ Three languages, chunked, interruptible. **Latency 2393 ms vs 1500 ms target** |
| M5 Body (STM32) | ⚠️ Firmware + Jetson side done, watchdog test passes against the firmware core. **No microcontroller attached; wheel directions unverified** |
| M6 Voice → motion | ✅ "move forward a little" moves the robot, in all three languages. Speech and motion overlap |
| M7 Ingest | ✅ Answers from a document ingested after boot, 3/3. Cross-lingual retrieval 8/8 |
| M8 UI | ✅ Six pages (Home, Chat, Headless, Knowledge, Monitor, Config) behind session auth. Headless carries the live activity log. Verified the robot survives the dashboard being killed |
| M9 Hardening | ⚠️ **24/24 failure drills pass.** 45 min soak clean apart from `robot-tts` growth. **The 24 h soak still needs a clean-boot run** |
| M9+ Supervision | ✅ Services page: every process, with start/stop/restart through whichever supervisor is actually in charge. Config/Memory restarts go through the same module, so they work in a dev stack |
| M9+ ASR choice | ✅ whisper.cpp selectable with `ASR_ENGINE=whisper`. Reports a language per utterance — the thing M3 measured Qwen3-ASR as not doing. Qwen3-ASR unchanged and still the default |
| M9+ Structured replies | ✅ JSON object constrained by a grammar; only `answer` is spoken, the other fields drive the body. Streams, so first-audio does not regress |
| M9+ Voice activity | ✅ Three listening modes, live on Config → Listening. Fixes the robot interrupting itself. Wake word needs no new model |
| M9+ One voice, three languages | ✅ Supertonic-3 selectable as `TTS_ENGINE`. Pure ONNX, no PyTorch. OmniVoice is safetensors and stays out under constraint 4 |
| M9+ One place to choose | ✅ Models are chosen on the Memory page only, and Apply writes the config and restarts what has to reload. Chosen / configured / running are reported side by side in the terminal and on two pages, from one implementation |
| M9+ Speech that does not stop | ✅ robot-tts renders ahead of playback instead of behind it — the pause after every sentence was a blocked pipe write, not slow synthesis. Five-sentence reply: 15.05 s of audio in 15.74 s wall, first audio 599 ms |
| M9+ TTS on the GPU | ✅ `TTS_USE_GPU=1` via onnxruntime-gpu: RTF 0.75 → 0.06. Costs +884 MiB, which does not fit alongside a GPU whisper — it detects cuDNN failing and rebuilds on the CPU rather than going mute. `SUPERTONIC_STEPS` is the free 1.8× if you have no memory to spend |
| M9+ Two modalities | ✅ `/headless` is voice, `/chat` is text and silent. Every turn carries its `source` and typed turns never reach the speaker. Both pages show the reply JSON collapsed under the answer |
| M9+ One front door | ✅ `./run.sh all\|headless\|slint` — the three modes differ only in which face starts; `--check` prints the config and starts nothing. `scripts/start_headless.sh` is now a shim |
| M11 Panel (Slint) | ✅ `robot-face`: chat, voice state, level meter and expression on a screen attached to the robot, straight to DRM/KMS with no desktop. **11.0 MiB resident.** Rust, approved as the one exception. 27 tests on a pure crate that builds while the robot runs |
| M11 Display path | ✅ `nvidia-drm` was never loaded — the only DRM node was `tegra_drm` with **zero connectors**, so no KMS UI was possible at all. Now loaded, persisted, and `card1-DP-1` runs at 1366x768. DRM master verified without root |
| M9+ Tazakka knowledge | ✅ The briefing PDF ingested as 15 heading-aligned chunks. **19/19 questions answered, 39/39 facts**, including English and Arabic questions against an Indonesian document, and it refuses to invent a fee it was never told. `./scripts/measure/knowledge_qa.py` |
| M9+ PDFs and an ingest CLI | ✅ `./scripts/ingest.sh <file\|folder>`, `--list`, `--forget`, `--rebuild`. PDFs read through poppler's `pdftotext`; a scanned one is refused with a reason rather than stored empty |
| M9+ Chunk on structure | ✅ `rag.split_sections` splits at headings before falling back to character size. A third less context for the same answers, and nothing discarded by the prompt budget |
| M9+ Embeddings actually used | ✅ Fixed `EmbedServer` ignoring a server it had not started itself, which silently made retrieval keyword-only — Indonesian questions still passed, **Arabic retrieved nothing and the model invented names** |
| M9+ 1B-3B alternatives | ✅ Llama-3.2-3B, Qwen2.5-3B, granite-3.3-2b and Sailor2-1B fetched and measured against the Tazakka set: `./scripts/measure/llm_bakeoff.py` |
| M9+ Voice latency | ✅ **8209 ms → 2181 ms median, 1554 ms best**, end of speech to first spoken word — the 1500 ms target in system-overview.md is now in reach. No model changed. Three silent faults: `speech_end` was timestamped when Silero *declared* the end (every number optimistic by `VAD_MIN_SILENCE`); ASR settings were applied at core startup and run_stack starts core **before** robot-asr, so a pinned `vad_min_silence=0.2` had never once taken effect; and `settings.all()` wrote registry defaults over `config/core.env`, which is why `CORE_FIRST_MAX_CHARS` and `CORE_MAX_TOKENS` both did nothing. `./scripts/measure/voice_latency.py` |
| M9+ Answers that finish | ✅ The reply budget was a flat **200 tokens** with no relationship to `LLM_CTX`, so raising the window bought headroom nothing could use — and `finish_reason` came back from llama.cpp on every turn and was **discarded**, so a cut-off reply looked exactly like a finished one. Under `json` that also loses every body field, because the object never closes. Now: `CORE_MAX_TOKENS=auto` derives the budget from the window (4096 → 1024), truncation is logged, broadcast and recorded in the metrics, and the split is printed by `--check` and at startup. Measured: a detailed answer runs to **487 tokens** — 60% of it was being lost silently |
| M9+ One STT engine | ✅ **Qwen3-ASR removed.** It transcribed better and never reported a language -- `result.lang` was always `''` -- and this robot routes three languages off exactly that tag. Keeping it meant building and measuring every feature twice for an engine that could not do the job. whisper.cpp is now the only decoder and the ASR choice is a choice of SIZE, ordered tiny→large on the Memory page with what each costs. `ASR_ENGINE`, `ASR_MODEL_DIR`, `ASR_PROVIDER` and `ASR_THREADS` are gone from `config/asr.env`; `robot-whisper` is no longer conditional |
| M9+ GPU is required, not preferred | ✅ `WHISPER_REQUIRE_GPU=1` and `TTS_REQUIRE_GPU=1`. whisper.cpp's `use gpu = 1` is the REQUEST -- on a full board it initialises CUDA, fails to allocate and carries on at RTF ~8; the outcome is `CUDA0 total size`, and that is what is now checked before the stack calls it healthy. Supertonic no longer rebuilds itself on the CPU: it raises `GpuUnavailable`, the Services page goes red, and the next utterance retries |
| M9+ Memory page tells the truth | ✅ Every line now carries **measured RSS from `/proc`** beside the estimate, plus `/proc/meminfo` as ground truth, pushed every 3 s with the health poll instead of only on connect. It immediately found a real lie: the Supertonic line was **884 MiB light** because nothing priced its CUDA context, so the page reported a selection as fitting while the board swapped 658 MiB |
| M9+ Everything on the GPU | ✅ **It was not.** `wait_llm` trusted `curl`'s exit status, which is 0 for the `503 Loading model` llama-server answers while loading — so the start script declared it healthy 5 s early, whisper took the GPU, and the stack came up **with no LLM at all** after printing "llama-server healthy". Fixed. Supertonic now drops the page cache before asking CUDA for its context (NvMap will not reclaim it) and preloads, so it gets the GPU instead of silently rebuilding on the CPU. With **Qwen2.5-3B** in place of Qwen3-4B — the bake-off's own recommendation — llm, whisper `small` and Supertonic all hold the GPU through 13 turns with zero fallbacks, at 1151 MiB free instead of 209 |
| M9+ A robot that could go deaf | ✅ The capture gate closed on robot-tts's `started` and only robot-tts could reopen it. Killed, restarted or driven by a second client mid-playback, it never sent `finished` — and the robot was **silently, permanently deaf** until robot-core was restarted. Nothing logged it. Now: `forget_speaker()` on connection loss, and a watchdog that will not let the microphone stay shut longer than one turn timeout |
| M9+ Speaking sooner | ✅ `answer` is the first JSON field so it can be spoken while the rest is written — but the chunker was flushed only after the whole object finished, so a one-sentence reply waited for `face` and `style` before a word came out. First chunk 3199-4497 ms → **2085-2416 ms**. `first_max_chars` (90, chosen when Supertonic ran at RTF 0.06) is now a live setting, because on a loaded board it is RTF 0.68-0.86 and those 90 characters cost ~5 s before the first word |
| M9+ Busy rooms | ✅ The room stops becoming utterances. whisper reports `no_speech_prob` and `avg_logprob` per segment and this pipeline was **throwing both away**: measured, real speech down to 0 dB SNR stays under 0.09 while noise with nothing in it sits at 0.82–0.91. Twenty seconds of clatter decoded as `(knocking)` and `(thud)`, **tagged English**, so the language filter passed them to the LLM; both are now rejected, with the numbers, in the log and on the dashboard. Plus a level gate before the decode. **GTCRN suppression exists and is off** — it measured +27% to +314% word errors against whisper. `scripts/measure/denoise_ab.py` |
| M9+ Embeddings on demand | ✅ `EMBED_MODE=on-demand`: resident only while ingesting, evicts the LLM if it will not fit, restores it after. Retrieval falls back to keyword search while asleep |

**What runs:** `robot-llm` (llama.cpp :8080, **Qwen2.5-3B** since 2026-09-14 — see
`memory-budget.md` § LLM bake-off for why, and what it costs), `robot-embed` (embeddings :8082, **only
while ingesting** — see below), `robot-whisper` (whisper.cpp :8083, only under
`ASR_ENGINE=whisper`),
`robot-asr` (Qwen3-ASR + Silero VAD), `robot-tts` (Piper), `robot-core` (the orchestrator,
dashboard WebSocket on :8090), `robot-ui` (the dashboard on :8081). Units are in `deploy/systemd/`; for development use
`scripts/run_stack.sh start|stop|status` instead — it starts them in the right order and,
importantly, stops them completely.

**The reply is a JSON object**, not prose with markers in it (`CORE_REPLY_FORMAT=json`):
`answer`, `language`, `foot_direction`, `foot_step`, `hand_movement`, `face`, `style`.
**Only `answer` is spoken**; the rest drives the body through the same `ToolBox` the
`[do:...]` markers used. The shape is enforced by a llama.cpp grammar rather than asked
for in the prompt, and `answer` is the first field so it can still be read out of the
token stream as it is written — speaking starts before generation finishes.
`robot.core.reply` is the whole contract; `CORE_REPLY_FORMAT=markers` restores M6.

**The robot hearing itself.** The speaker and the microphone are one USB device, so
under barge-in the robot cancels its own sentence and transcribes what it just said.
`CORE_LISTEN_MODE` (Config → Listening, live) decides what the microphone may do while
it talks: `interrupt` (barge-in, the responsive one that hears itself), `no_interrupt`
(deaf from the moment audio starts until `CORE_LISTEN_TAIL_MS` after it stops), or
`wake_word` (as `no_interrupt`, and nothing starts a turn unless the transcript names
`CORE_WAKE_WORD`). The wake word is matched against the transcript the recogniser
already produces — no keyword-spotter model, no new dependency.

In the two non-interrupt modes the microphone is **shut**, not ignored: the orchestrator
sends `robot-asr` a `gate` message and frames are dropped until `CORE_LISTEN_TAIL_MS`
after the last sample. Gating the finished transcript was not enough — an utterance that
starts during playback is still decoding when playback ends, so by the time the text
arrives the room is quiet and the echo looks like a question. `asr.log` says
`microphone open again (N frames dropped while the robot spoke)`.

**What it will listen to at all** is `ASR_LANGUAGES=id,en,ar` in `config/asr.env`,
Bahasa Indonesia first and also the language it falls back to. whisper labels coughs,
fans and speaker echo as Chinese; without the filter those reached the LLM as questions.

**What it does about a busy room** is three gates, all in `config/asr.env` and live on
Config → Listening. The language filter above is not enough on its own: measured here,
twenty seconds of clattering with nobody speaking decoded as `(knocking)` and `(thud)`,
both tagged **English**, so both passed it.

* `ASR_NOISE_GATE_DB=6.0` — an utterance not this far above the room's own floor is
  dropped **before** it is decoded.
* `ASR_MAX_NO_SPEECH=0.6`, `ASR_MIN_LOGPROB=-1.0` — whisper says how much it believed
  itself and the pipeline used to discard it. Real speech down to 0 dB SNR stays under
  0.09; noise with nothing in it sits at 0.82–0.91.
* `ASR_DENOISE=off` — GTCRN suppression, built, measured, and **off because it lost**:
  +27% to +314% word errors against whisper. One setting away, and
  `./scripts/measure/denoise_ab.py --noise <30 s of your room>` is how you decide,
  because the losing measurement was synthetic.

Nothing is dropped quietly — every rejection is an INFO line with the numbers behind it
and an `ignored` event on the dashboard. If the robot has gone deaf instead, that is the
same grep: `grep 'robot-asr: dropped' .cache/tmp/asr.log`.

**One voice for all three languages:** `TTS_ENGINE=supertonic` (`config/tts.env`, after
`scripts/models/fetch_supertonic.sh`). Piper's three checkpoints are three different
speakers; Supertonic-3 is one ONNX model covering all three, 484 MiB, at about a third
of Piper's speed. Both are priced against each other on the Memory page.

Ten preset voices ship with it — `F1`–`F5` female, `M1`–`M5` male — read from the style
files on disk, so a voice added later appears without a code change. Pick one on
**Config → Speaking → Voice (Supertonic)**, which previews each in Bahasa Indonesia,
English and Arabic through the robot's own speaker without adopting it, or on
**Memory → Voice**, which replaces the three per-language Piper pickers when this engine
is selected. To compare all of them at once:

```bash
python scripts/measure/voice_samples.py        # run/voice_samples/<style>_<lang>.wav
```

Which voice suits Arabic is not obvious from how it reads English, which is why every
preview covers all three.

**Which model is actually loaded?** Three layers, and they can still drift after a failed
restart or a hand-edited env file: what is *chosen* in the dashboard
(`run/settings.json`), what is *configured* (`config/*.env`), and what is *running*
(`/proc`). All three are reported side by side in three places, from one implementation
in `robot.common.services` — the Memory page, the Services page, and:

```bash
python scripts/measure/whats_running.py     # side by side; non-zero exit if they differ
                                           # `on` column = GPU or CPU, read from /proc
```

It also reports an ASR decoder running when nothing reads it. **Exactly one decoder is
ever loaded**: `robot-asr` always runs — it owns the microphone, the Silero endpointing
and the barge-in signal — and what changes with the engine is who decodes, Qwen3-ASR
inside that process or `robot-whisper` over HTTP. They are not alternatives to each
other; one calls the other. Choosing a whisper model starts `robot-whisper` and stops
Qwen3-ASR from being loaded at all; choosing a sherpa export stops `robot-whisper`.
Two ASR models resident is about 2 GB on a board with roughly one to spare, so
`run_stack.sh` refuses to start the decoder the selected model does not use, and the
Services page shows the reason instead of a button that would fail.

**Verify anything quickly:**

```bash
./scripts/bringup/check_board.sh              # M0 preconditions, read-only
source scripts/bringup/env.sh                 # ALWAYS do this before pip/model work
python3 scripts/measure/llm_smoke.py          # M1: three languages + tok/s
python  scripts/measure/tts_smoke.py          # M2: needs robot-tts running
python  scripts/measure/asr_smoke.py          # M3: transcribes wavs, reports the tag
python -m pytest tests/ -q                    # 66 hardware-free tests

./scripts/run_stack.sh start --wav .cache/tmp/asr_in/three_turns.wav   # M4: full loop
python  scripts/measure/loop_smoke.py         # M4: watch turns, print timings
python  scripts/measure/peak_ram.py           # M4: peak RAM with everything live
./scripts/run_stack.sh stop

./firmware/stm32/run_native_tests.sh          # M5: 628 checks on the firmware core
./tools/stm32_sim/build.sh                    # M5: host build of that same core
python scripts/serial/watchdog_test.py --sim  # M5: kill -9 the brain, wheels must stop
python scripts/serial/protocol_test.py --sim  # M5: the whole contract, both sides

./scripts/models/fetch_embed.sh                       # M7: embedding model, 610 MB
python scripts/measure/rag_smoke.py \
       --ingest .cache/tmp/docs/robot_facts.txt       # M7: cross-lingual retrieval
./scripts/run_stack.sh start --ctx 2048 --sim --ui    # everything + dashboard
python scripts/measure/ui_smoke.py                    # M8: auth, config groups, restarts
./scripts/run_stack.sh svc restart llm                 # one service, same spawn path
./scripts/run_stack.sh svc stop embed                  # what the Services page presses
./scripts/models/build_whisper.sh                      # the alternative ASR engine
./scripts/run_stack.sh svc start whisper               # only under ASR_ENGINE=whisper
python  scripts/measure/whats_running.py               # chosen vs configured vs running

sudo ./scripts/install_services.sh                    # M9: install the systemd units
sudo ./scripts/bringup/setup_logging.sh               # M9: persistent journal
sudo ./scripts/bringup/setup_sudoers.sh               # M9: dashboard restart buttons
python scripts/hardening/failure_drills.py            # M9: every failure path, 24 checks
python scripts/hardening/soak.py --minutes 1440       # M9: the 24-hour soak
```

**Without root?** `scripts/hardening/user_units.sh install` derives `systemctl --user`
copies of the real units so supervision can be exercised anyway; add `--user` to the
drills. Production uses the system units.

**The dashboard** is at `http://<jetson>:8081`. It needs `ROBOT_UI_PASSWORD` in `.env` and
will refuse to start without one — it can drive the robot and it listens on the LAN. Run
`sudo scripts/bringup/setup_sudoers.sh` once if you want its LLM/ASR restart buttons to
work; without it they say why they cannot.

Pages: **Home** (state, services, recent turns), **Chat** (typed, with the literal serial
line for every action), **Headless** (the physical loop — conversation beside a live
activity log), **Knowledge** (what it has stored, and ingest), **Memory** (pick models and
see what they would cost before applying), **Monitor** (memory, swap, per-core CPU, GPU,
power rails, thermals, per-service RSS), **Services** (every process: running or not, pid,
uptime, RSS, and start/stop/restart for one at a time), **Config** (six tabs, split into
"applies immediately" and "needs a restart").

**Models are chosen on the Memory page, and only there.** That is the page that prices a
combination against the 7620 MiB ceiling, so it is the page that should be able to make
the choice real: Apply writes every changed value, then restarts each affected component
once, and reports what is running when it is done. Config used to offer the same
dropdowns with none of the budget context and without applying anything — two places to
change one value, and two places for it to sit unapplied. It now points at Memory
instead. The serial port stays on Config: it is not a model and has no memory cost.

**Services** is the one page that does not go through the orchestrator. It is answered by
`robot-ui` from `/proc` directly, because the case it exists for is "something is down"
and `robot-core` is one of the things that can be down. It also names the supervisor
actually in charge — the system units, `systemctl --user`, or `scripts/run_stack.sh` — so
a button that cannot work says why before you press it. The same module
(`robot.common.services`) is what the Config and Memory pages' scoped restarts now go
through, which is why those work in a dev stack at all.

**Logs:** the dashboard's **Logs** page streams the orchestrator's records live and
tails every other service (from its file, or the journal under systemd). The level can be
set from there — `DEBUG` makes the orchestrator log the model's raw reply before any
stripping, which is the fastest way to see why a language tag or an action marker went
wrong. Log files otherwise live in `.cache/tmp/<service>.log`.

**If the robot cannot hear you, or cannot be heard:** both are usually the same bug —
`pulse` follows PulseAudio's default endpoint, which on this board is an onboard codec
wired to nothing, and it fails *silently* in both directions. Check the startup lines
(`audio input:` / `audio output:`) and the speaker shown beside `tts` in the health panel;
a `-90 dB` level meter on Config → Listening is the input symptom. `PULSE_SOURCE` in
`config/asr.env` and `PULSE_SINK` in `config/tts.env` pin them. See TROUBLESHOOTING.md.

**Two things are deliberately not always running.**

*The embedding server* is demand-started: `EMBED_MODE=on-demand` in `config/embed.env`
means `robot-core` brings it up when the ingestion queue has work and stops it when that
queue drains, giving 810 MB back the rest of the time. If it will not fit, `EMBED_EVICT`
(the LLM) is stopped, the start retried, and the LLM restarted **after** the embedding
server is down again. While it sleeps, a question falls back to FTS5 keyword search over
the same store — which works, and cannot do cross-lingual retrieval, because embedding the
*question* needs the model that embedded the chunks. `EMBED_MODE=always` restores M7–M9
behaviour; `EMBED_RETRIEVAL=start` keeps vectors at the cost of a stall on the first turn
after an idle period.

*whisper.cpp* is the alternative ASR engine, off by default. `ASR_ENGINE=whisper` in
`config/asr.env` switches `robot-asr` from decoding in-process with Qwen3-ASR to posting
utterances at `robot-whisper`. It exists for the thing M3 measured Qwen3-ASR as not doing:
whisper reports **a language per utterance**, so English and Bahasa Indonesia can be told
apart before the LLM sees the turn (verified on this board, all three languages, with the
weakest `tiny` checkpoint). It is also far smaller — `base` is 235 MiB against Qwen3-ASR's
1243. Qwen3-ASR still transcribes better and remains the default.

Switching is a **model choice**, not a config edit: pick a `whisper …` entry on the
Memory or Config → Listening page and press its button. That writes `ASR_ENGINE`, writes
`WHISPER_MODEL`, starts `robot-whisper` and restarts `robot-asr`, in that order. Doing it
by hand instead:

```bash
./scripts/models/build_whisper.sh    # ~25 min with CUDA; no new Python dependency
./scripts/models/fetch_whisper.sh    # tiny, base, small, medium, large-v3-turbo
sed -i 's/^ASR_ENGINE=.*/ASR_ENGINE=whisper/' config/asr.env
./scripts/run_stack.sh svc start whisper && ./scripts/run_stack.sh svc restart asr
```

It runs on the **GPU** (`WHISPER_USE_GPU=1`). Measured on this board with `small`: 0.50 s
per utterance against 23.5 s on the CPU, for a ~280 MiB CUDA context. Set
`WHISPER_USE_GPU=0` — no rebuild — if `llama-server` ever starts losing the KV cache
allocation race to it.

**Choosing a model:** `scripts/models/fetch_alternatives.sh` installs Qwen3-0.6B/1.7B,
Gemma-3-1B, a larger Qwen3-ASR-1.7B and extra Piper voices. Only Qwen3-4B has been through
the language and tool-marker testing — **Gemma-3-1B answers Indonesian and Arabic
questions in English and repeats the tool-rule examples verbatim**, so treat the smaller
models as a memory escape hatch, not an equivalent. Its chat template is also stricter
than Qwen's about message roles; see TROUBLESHOOTING.md if the robot gets stuck on the
canned unavailable line. The Memory page prices any
combination against the 7620 MiB ceiling first — the KV cache is read from each GGUF's own
header, so the estimate holds for models nobody has measured. Not everything installed
fits at once; that is what the page is for.

They are hand-written HTML/CSS/JS in `src/robot/ui/static/`, sharing the design system of
the sibling project at `/media/jetson/DATA/robot`. NiceGUI is still the server — it
provides FastAPI, uvicorn and the session — but no longer renders the content.

**Do not run the full stack and a dev session at the same time.** As of M7 the stack peaks
at ~6.5 GB on a clean boot; VS Code Server plus Claude Code is another ~1–1.8 GB, which is
more than the remaining margin. Measured at 97% use and 3.2 GB of swap when both ran
together, and the thrashing corrupted the behavioural measurements around it.

**Start order matters.** `llama-server` allocates through CUDA, which on Tegra cannot
reclaim memory another process already holds — start the ASR alongside it and the KV cache
allocation loses the race. `run_stack.sh` waits for the LLM to be healthy first, and drops
the model page cache before it starts (see TROUBLESHOOTING.md).

### What the robot can do now

Hear you in English, Bahasa Indonesia or Arabic; look up what it knows; answer aloud in
your language while it is still generating the rest; move its body mid-sentence; be
interrupted; and remember a fact you tell it. All of it is visible and tunable from a
browser without SSH. The body is simulated — see M5.

### Before you leave it running unattended

Three one-time steps, all needing root, none of them done on this box yet:

```bash
sudo ./scripts/install_services.sh        # system units (these are what production uses)
sudo ./scripts/bringup/setup_logging.sh   # journald is VOLATILE here -- an overnight
                                          # failure would leave no evidence at all
sudo ./scripts/bringup/setup_udev.sh      # stable /dev/robot-stm32
sudo ./scripts/bringup/setup_sudoers.sh   # the dashboard's scoped restart buttons
```

Then `python scripts/hardening/failure_drills.py` (24 checks) and
`python scripts/hardening/soak.py --minutes 1440`.

### Open decisions

1. ~~Where does the language tag come from?~~ **Resolved in M4:** the LLM declares it
   (`[lang:xx]`), cross-checked against the reply's script. 3/3 in testing.
2. **Wake word approach — still open.** `openwakeword` pulls `scipy` + `scikit-learn` +
   `tflite-runtime`. Cheaper: run its ONNX graphs on the `onnxruntime` already installed, or
   use `sherpa_onnx.KeywordSpotter`. Until then the robot is always-listening.
3. **Latency is 2393 ms against a 1500 ms target** (M4). Both overruns are structural — ASR
   decode cannot be overlapped, and the LLM runs at half the assumed tok/s. Cheapest next
   step is a clean-boot re-measure with no dev session attached.
4. **Memory is the binding constraint** (M7/M8). ~6.6 GB of 7.6 GB on a clean boot with
   everything including the dashboard. It fits, with ~1 GB spare — but there is no room to
   develop on the box while it runs.
5. **`robot-tts` grows and has not been shown to plateau.** 342 → 889 MiB over 45 minutes.
   Piper itself is flat (505 MiB from 30 syntheses on), so it is the service's allocator.
   Bounded with `MemoryMax=1200M`; the 24 h soak settles whether that is ever hit.
6. **Arabic is the language that slips first.** Fluent, but under a long prompt (notes +
   tool rules + history) it is the one that answers in the wrong language. 5/6 in the final
   combined run; the miss was Arabic. Watch it as prompts grow.

### The STM32 does not exist yet

`/dev/ttyACM0` **is not the microcontroller** — on this board it is the USB soundcard,
which is a composite device exposing a CDC-ACM interface. An earlier board check reported
"serial device present" and was wrong; it now matches on the STMicroelectronics vendor id
(`0483`) instead. Nothing with that id has ever been plugged in.

So M5 is written, unit-tested and protocol-verified, but has never driven a motor. Before
trusting it near anything breakable, work through the bring-up order in
`firmware/stm32/README.md` — in particular **re-run the watchdog test with `--port`**, and
**verify the wheel directions**, which is the one thing that cannot be checked without
watching a wheel turn.

Run `sudo scripts/bringup/setup_udev.sh` once the board is attached, so it gets a stable
`/dev/robot-stm32` and cannot be confused with the soundcard.

### Audio hardware

A USB soundcard (`cafe:4002 Audio Tools`, ALSA card `Audio`) provides both capture and playback
and was verified during M2/M3: playback is real-time, the mic captures live signal, and the two
run **full-duplex simultaneously** — which barge-in depends on. Both configs point at
`plughw:CARD=Audio,DEV=0`.

Its speaker and microphone barely couple acoustically, so the robot cannot hear itself well
enough for a self-test loop. That is fine — arguably good, since echo is the enemy of barge-in —
but it means **two checks still need a human**: listening to the three voices, and speaking into
the mic. Everything else was verified by round-tripping Piper audio through the real ASR service.

`/dev/ttyACM0` is present, so the STM32 link is ready for M5.

---

## The five things most likely to trip you up

1. **Memory, not speed, is the binding constraint.** The board is fast enough (comparable
   3–4B models run 35–43 tok/s here in Super Mode). It is *not* roomy. Every decision in
   `TECH_STACK.md` was made to protect the 8 GB budget. Check `memory-budget.md` before
   adding anything.

2. **ARM64 wheels are often missing from PyPI.** Use NVIDIA's index first:
   `pip install <pkg> --extra-index-url https://pypi.jetson-ai-lab.io/jp6/cu126`.
   Check `jetson-containers` for a prebuilt image before attempting a source build.

3. **Language support was the deciding factor in model choice.** NVIDIA's Nemotron ASR and
   MagpieTTS are better models in isolation — neither supports Bahasa Indonesia. Do not
   "upgrade" to them without re-reading `PROJECT_BRIEF.md` § Rejected.

4. **Streaming and barge-in are architecture, not polish.** Sentence-boundary chunking and
   mid-reply interruption are what make the robot feel human. They must be designed into the
   orchestrator from the start, not added at the end.

5. **The STM32 watchdog is not optional.** A robot whose only stop mechanism is a command from
   a process that can be OOM-killed is a robot that drives into a wall.

---

## Quick command reference

```bash
# Check power mode (must show MAXN_SUPER)
jetson_release

# Live resource monitor
jtop

# Service health
systemctl status robot-core robot-llm robot-asr robot-tts robot-ui

# Follow orchestrator logs
journalctl -u robot-core -f

# Watch the serial link by hand
screen /dev/ttyACM0 115200

# Check for OOM kills
dmesg | grep -i "killed process"
```

---

## Where to put new docs

| Content | Location |
|---|---|
| A new architectural decision | `../architecture/` + note it in `PROJECT_BRIEF.md` |
| A per-module implementation note | `../modules/<module>.md` |
| A painful build/install procedure | `BUILD_NOTES.md` (this folder) |
| A measured benchmark | `../architecture/memory-budget.md` |
| Something that went wrong and how it was fixed | `TROUBLESHOOTING.md` (this folder) |

Keep files split by concern and short. If one grows past a few hundred lines, split it rather
than letting it become a file nobody reads.
