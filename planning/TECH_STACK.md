# TECH_STACK.md

Every runtime dependency, why it was chosen, and what it costs.
**Adding anything not listed here requires approval** — see `AGENTS.md`.

---

## Base system

| Item | Choice | Notes |
|---|---|---|
| Board | Jetson Orin Nano 8GB (Super) | Unified CPU/GPU memory — GPU allocations come out of the same 8 GB |
| JetPack | **6.2** (L4T 36.4.3) | Flash with `jetson-orin-nano-devkit-super.conf` |
| Power mode | **MAXN SUPER** — `sudo nvpmodel -m 2` | Unlocks GPU to 1020 MHz and DRAM bandwidth to 102 GB/s. Uncapped mode — verify thermals under sustained load |
| OS profile | **Headless**, `multi-user.target`, no GNOME | A desktop session costs ~600 MB |
| Python | 3.10 (JetPack 6.2 default) | |

**Verify Super Mode is active:** `jetson_release` should report `NV Power Mode: MAXN_SUPER`.
If the board was flashed with the non-super config, that mode will not appear — reflash.

---

## Listen

| Component | Choice | Approx RAM | Why |
|---|---|---|---|
| VAD | **Silero VAD** (ONNX) | negligible | Tiny, continuous, near-zero CPU |
| Wake word | **openWakeWord** (ONNX) ⏸ | ~50 MB claimed | Open source, Apache-2.0, custom words trainable via its Colab pipeline. Acoustic keyword-spotter — language-agnostic, so one wake phrase works regardless of conversation language. ⏸ **On hold (M3):** `pip install openwakeword` pulls `scipy`, `scikit-learn` and `tflite-runtime` — a second ML runtime and well beyond ~50 MB. Alternatives: run its ONNX graphs directly on the `onnxruntime` we already have, or use `sherpa_onnx.KeywordSpotter` (zero new dependencies) |
| ASR | **whisper.cpp** + ggml checkpoints, CUDA | tiny **432 MB** / base **515 MB** / small **917 MB** on the GPU (measured; includes a ~280 MB CUDA context). CPU-only: 152 / 243 / 637 MB | **The only engine since 2026-09-14.** Covers all three languages and reports a **per-utterance language id**, which is what the robot routes on. Chosen by SIZE (`WHISPER_MODEL`); the Memory page lists the installed checkpoints smallest-first and prices each. Runs as its own server process (config/whisper.env, robot-whisper.service) |

**Qwen3-ASR-0.6B was the default from M3 to M9, and was removed.** It is the better
transcriber — whisper `tiny` garbles Arabic where Qwen3 did not — and it was measured in
M3 as emitting **no language tag at all**: `result.lang` is always `''`. This robot routes
three languages off exactly that tag, so the half of its rationale that mattered never
held. `recognizer.script_of()` survives as the stopgap it always was: it separates Arabic
from Latin script and cannot tell English from Indonesian.

Keeping both meant building and measuring every feature twice — the noise gates, the
confidence filter, the budget, the Services page — for an engine that could not do the
job. `ASR_ENGINE`, `ASR_MODEL_DIR`, `ASR_PROVIDER` and `ASR_THREADS` are gone with it, and
with them a whole class of config that could name an engine unable to load the selected
model. sherpa-onnx stays as a dependency: it runs Silero VAD and the GTCRN denoiser.

**Why not Nemotron:** Nemotron-3.5-ASR has excellent Arabic but no Indonesian at all.
whisper covers all three in one checkpoint and tells you which one it heard.

**Built with CUDA, and it must stay on it.** The first build here was CPU-only, reasoning
that a second CUDA allocator competes with `llama-server` for the unified pool. That risk
is real, but it was never weighed against the cost — and the cost is 0.50 s versus
**23.5 s** per utterance on `small` (RTF 0.17 against 8.08, same board, same clips). ASR
decode is on the critical path of every spoken turn and cannot be overlapped.

`WHISPER_USE_GPU=1` is the request; **`WHISPER_REQUIRE_GPU=1` checks the outcome.**
`--no-gpu` is the only thing that stops whisper.cpp trying, and trying is not succeeding:
on a full board it initialises CUDA, fails to allocate, and carries on at RTF ~8 with
nothing in the log at the moment it matters. `run_stack.sh` now greps for
`CUDA0 total size` with no `cudaMalloc failed` before calling the server healthy, and
stops it with an explanation rather than leaving a slow decoder running.

**Dependency cost (AGENTS.md working rule 1):** no new Python package. whisper.cpp is a
lean C++/ggml engine built from source into `vendor/`, exactly like llama.cpp, and is
reached over HTTP on loopback — the same shape as the LLM and the embedding server.
`scripts/models/build_whisper.sh`.

> ⚠️ **Corrected in M3, settled in M9+.** This section used to claim Qwen3-ASR "emits a
> language tag per utterance, which drives TTS voice selection downstream". It does not —
> measured, on all three languages. That correction stood unresolved for six milestones
> with two engines carried side by side; it was resolved on 2026-09-14 by removing the
> engine that could not do it.

Runtime is **offline (segmented), not streaming**: whisper decodes a closed utterance.
Decode costs RTF ~0.17 on the GPU at `small`, ~490 ms for a 3.3 s utterance.

**Wake word — resolved, without a new model.** The plan was an acoustic keyword spotter,
and openWakeWord was put on hold because it pulls `scipy`, `scikit-learn` and
`tflite-runtime`. It is no longer needed: `CORE_LISTEN_MODE=wake_word` matches the name
against the transcript the recogniser already produces. That costs no model, no memory
and no second ML runtime, and it works identically under both ASR engines. What it does
not give is a low-power always-on gate — the robot still decodes every utterance, which
it did anyway.

**What the microphone may do while the robot talks** is now a setting, because the
speaker and the microphone are one USB device and under barge-in the robot hears itself,
cancels its own sentence, and transcribes what it just said as the next question.
`interrupt` (barge-in), `no_interrupt` (deaf while speaking, plus a tail) and
`wake_word`. Config → Listening, live.

**What it will listen to at all:** `ASR_LANGUAGES=id,en,ar`, Bahasa Indonesia first —
it is also the language the robot falls back to. whisper reports a language for
everything it decodes, including a cough or a burst of speaker echo, and it calls most of
those Chinese; without the filter they reach the LLM as if somebody had said them.

**What it does about a busy room.** Measured 2026-09-13, because the pipeline that was
word-perfect in M3 was measured against Piper speech in a quiet one. Three defences,
`src/robot/asr/noise.py`:

| Defence | Setting | Default | Cost |
|---|---|---|---|
| Level gate — drop an utterance too close to the room's own floor, before decoding | `ASR_NOISE_GATE_DB` | `6.0` dB | nothing; it *saves* a decode |
| Confidence gate — drop a transcript whisper itself scored as speechless or incoherent | `ASR_MAX_NO_SPEECH`, `ASR_MIN_LOGPROB` | `0.6`, `-1.0` | nothing; the numbers were already in the response |
| Suppression — **GTCRN** via sherpa-onnx | `ASR_DENOISE` | **`off`** | 523 KiB; +4 MiB streaming (RTF 0.12) or +19 MiB per utterance |

The confidence gate is the one that earns its place, and it needed no new code in the
decoder — `verbose_json` was already carrying `no_speech_prob` and `avg_logprob` and the
pipeline was discarding both. Measured on this board, they separate cleanly: real speech
down to 0 dB SNR stayed under `no_speech` 0.09, while room noise with nothing in it sat
at 0.82–0.91. Twenty seconds of clatter with nobody speaking produced `(knocking)` and
`(thud)` from whisper, tagged English, which `ASR_LANGUAGES` passed straight through.
Both are now rejected, and every rejection is logged with the numbers behind it.

**Suppression is off, and the default is measured rather than assumed.** GTCRN is the
obvious answer to a noisy microphone, costs no new dependency (sherpa-onnx already runs
the VAD and ships the denoiser in the same wheel) and almost no memory. Against whisper
it made the transcript **worse at every SNR tried** — +27% to +314% word error rate on
stationary noise, +58% to +215% on babble — and against Qwen3-ASR it was roughly
neutral. whisper was trained on genuinely noisy audio; a denoiser's artefacts are a kind
of damage it has never seen. It ships anyway, one setting away, because the test was
synthetic and has no reverberation and no far-field speech in it:
`./scripts/measure/denoise_ab.py --noise <30 s of the real room>` settles it where it
matters. `scripts/models/fetch_denoise.sh`.

---

## Think

| Component | Choice | Approx RAM | Why |
|---|---|---|---|
| Engine | **llama.cpp** (server mode, OpenAI-compatible API) | — | Lean C++/CUDA, fastest community support for new model releases, GGUF quantization |
| Model | **Qwen3-4B-Instruct, Q4_K_M GGUF** | ~3.0 GB incl. KV cache | Qwen3 is trained on 119+ languages including Arabic dialects and the Indonesian family — handles all three natively with no swapping |
| Context | **2048–4096 tokens**, rolling summarization | (in the above) | KV cache is the memory cost people forget to budget |

Reference throughput on this board in Super Mode: comparable 3–4B models run ~35–43 tok/s,
several times faster than human speech — generation speed is not the bottleneck.

**Fallbacks if memory is tight:** Qwen3-1.7B or Gemma-3-1B.
**Do not** default to a 7–8B model — those need NVMe swap headroom on this board.

Installed and selectable from the dashboard's Memory page
(`scripts/models/fetch_alternatives.sh`), with the cost of each shown before it is
applied:

| Model | Weights | Resident @ 2048 ctx | Full stack headroom |
|---|---|---|---|
| Qwen3-0.6B Q4_K_M | 378 MiB | ~1150 MiB | ~3.1 GB |
| Qwen3-1.7B Q4_K_M | 1056 MiB | 2030 MiB | ~2.4 GB |
| Gemma-3-1B Q4_K_M | 769 MiB | ~1750 MiB | ~2.6 GB |
| **Qwen3-4B Q4_K_M** (default) | 2382 MiB | 3420 MiB | ~1.0 GB |

Also installed: **Qwen3-ASR-1.7B** (a larger ASR, same sherpa-onnx layout) and extra
Piper voices. Not everything installed fits at once — that is the point of pricing a
selection before applying it.

**Tool calling:** define the physical actions as tools (`drive`, `set_face`, `gesture`, `stop`,
`save_memory`) and parse structured calls from the model output. Each maps to one serial write.

---

## Ingest

| Component | Choice | Approx RAM | Why |
|---|---|---|---|
| Embeddings | **Qwen3-Embedding-0.6B** (Q8_0 GGUF) | ~250 MB est → **~630 MB measured** | Shares Qwen3's multilingual coverage — verified in M7: **8/8 retrievals correct, 5/5 of them cross-lingual**. Served by a second `llama-server --embeddings` on :8082, CPU-only with `CUDA_VISIBLE_DEVICES=` |
| Vector store | **sqlite-vec** 0.1.9 | ~50 MB | Single embedded file, no server process, no telemetry, no network. aarch64 wheel with **zero dependencies** |

**Why not all-MiniLM-L6-v2:** English-centric. Would silently degrade Arabic and Indonesian
retrieval. The extra ~160 MB buys correctness across all three languages.

**Pipeline:** chunk (~300–500 tokens, with overlap) → embed → upsert with metadata.
Ingestion is a **background job** — never on the conversational hot path.

**Resident only while it is working (M9 follow-up).** `EMBED_MODE=on-demand` in
`config/embed.env` is now the default: `robot-core` starts the embedding server when the
ingestion queue has work and stops it once the queue drains, giving 810 MB back for the
~99.9% of the time nothing is being ingested. If it will not start for lack of memory, the
services in `EMBED_EVICT` (the LLM by default) are stopped, the start is retried, and they
are brought back **after** the embedding work finishes and the server is down again — so
the two are never both resident and the peak is bounded by the larger, not the sum.
`EMBED_MODE=always` restores the M7–M9 behaviour.

**What that costs, precisely.** A vector search has to embed the *question* into the same
space as the stored chunks, and only the model that stored them can do that. Neither the
conversational LLM nor sqlite-vec can substitute — the LLM's vectors, if it produced any,
would describe a different space, and the database only compares numbers someone else
computed. So with the server asleep, retrieval falls back to **FTS5 keyword search** over
the same rows: no model, no memory, and no cross-lingual retrieval. An Indonesian question
will not find an English note, which is the one thing M7 was built for. `EMBED_RETRIEVAL`
chooses: `warm` (vectors when it is up, keywords otherwise — never delays a turn), `start`
(wake it and wait), `off` (keywords only).

> **On AGENTS.md constraint 2 ("one LLM resident at a time").** M7 runs a second
> `llama-server` for embeddings. That is not a second generative model: `memory-budget.md`
> lists "Embeddings — Qwen3-Embedding-0.6B" as its own line inside the same steady-state
> total as the LLM, so the budget already assumed both resident. The constraint is about
> not holding two 3–4 GB generative models at once. Flagging it because the reading is a
> judgement call, and it is reversible — set `EMBED_URL=` empty and the robot still
> converses, just without memory or retrieval.

---

## Speak

### Option C — Supertonic-3, one voice for all three (adopted as an option)

| Component | Choice | Approx RAM | Why |
|---|---|---|---|
| TTS | **Supertonic-3** (ONNX) | **492 MB CPU / 1376 MB GPU measured** | One model, 31 languages including all three of ours, 10 preset speaker embeddings. The robot keeps **one voice** whichever language it answers in — Piper's three checkpoints are three different people, and the voice changing mid-conversation is the thing this fixes |

**Dependency cost (AGENTS.md working rule 1):** the `supertonic` SDK needs
**Slint 1.17.1 + Rust** (added M11, approved as the one Rust exception in AGENTS.md) is
the on-robot panel, `face/`. Renders straight to DRM/KMS with the **software renderer** --
no compositor, no X11, no Wayland, no GPU context. Chosen over `cage` and `weston`
because, measured on this board, the service user takes and releases DRM master on
`/dev/dri/card1` with no root and no seatd, so a compositor would have added a process
and 40-80 MiB for nothing. (`nvidia-l4t-weston` stays the fallback if linuxkms ever
fights us; it is vendor-built for this display stack and already installed, unlike cage's
wlroots 0.15.1.) The software renderer also avoids Skia, which needs clang -- not
installed -- and 4-8 GiB of build space on a root filesystem that is 94% full.
**Measured: 11.0 MiB resident**, plus 8.4 MiB of DRM buffers, against 60 MiB for the
NiceGUI dashboard. Feature flags are pinned `=1.17.1`: Slint master has already renamed
them and deprecated `backend-linuxkms-noseat`.

**LLM alternatives, 1B-3B** (added M10, fetched by `scripts/models/fetch_alternatives.sh`):
Llama-3.2-3B-Instruct, Qwen2.5-3B-Instruct, granite-3.3-2b-instruct (IBM) and
Sailor2-1B-Chat, alongside the Qwen3-1.7B and Gemma-3-1B this document already named.
All are Q4_K_M GGUF and run on the same llama-server -- no new runtime dependency.
**Taken, 2026-09-14.** `LLM_MODEL` is now Qwen2.5-3B-Instruct. The reason was not the
accuracy table below but the GPU: with Qwen3-4B, the LLM, whisper `small` and Supertonic
fit on the GPU at boot and then do not hold — Supertonic loses its CUDA allocation under
load and rebuilds on the CPU mid-conversation, taking first-audio from 2.1 s to 8.2 s.
The 841 MiB the 3B gives back is what keeps all three resident. Cost: 17/19 answers
instead of 19/19, and it still refuses to invent a fee.

Measured against an ingested document in `memory-budget.md` § LLM bake-off; the short
version is that **Qwen2.5-3B-Instruct** is the one to reach for, because it keeps almost
all of Qwen3-4B's accuracy for 841 MiB less and does not invent facts it was not given.
Mistral has nothing in this range: Ministral-3B is API-only and the smallest open Mistral
is 7B.

**`onnxruntime-gpu` 1.24.0** (added M10, jetson-ai-lab index, approved by the human)
is optional and opt-in through `TTS_USE_GPU`. It replaces the CPU `onnxruntime` wheel in
`.venv` — same import name, so it shadows it for the services in that venv and nothing
else — and takes Supertonic from RTF 0.75 to **0.06**. It costs **+884 MiB** when the
CUDA provider is actually used and nothing when it is not, so `TTS_USE_GPU=0` is exactly
as cheap as it was before the wheel changed. It is ONNX Runtime, not a training
framework, so AGENTS.md constraint 4 is untouched. On a board where the LLM and a GPU
whisper are already resident there is not 884 MiB of *free* memory for it; robot-tts
detects the cuDNN failure and rebuilds on the CPU rather than going mute.

`onnxruntime`, `numpy`, `soundfile` and `huggingface-hub` — **all four were already
installed** for the ASR and the audio path, so the only new thing on disk is a 52 KB
pure-Python package. No PyTorch, no transformers (constraint 4). Models are fetched by
`scripts/models/fetch_supertonic.sh` into `models/` on the DATA partition, not by the
SDK's own downloader, which would use `~/.cache` on a root filesystem that is 94% full.

**The trade, measured on this board:**

| | Piper ×3 | Supertonic |
|---|---|---|
| Resident | ~255 MB (lazy) / 395 MB preloaded | **484 MB** |
| RTF | 0.24–0.34 | **0.73–0.82** |
| Load | ~1.5 s per voice | 1.5 s once |
| Voice across languages | three different speakers | **one** |

About three times slower, and synthesis sits on the path to first audio, which M4 already
has at 2393 ms against a 1500 ms target. `TTS_ENGINE` in `config/tts.env` chooses;
the Memory page prices the two against each other before anything is loaded.

**Why not `k2-fsa/OmniVoice`,** which was also asked for: it ships `model.safetensors`
plus an audio tokenizer and a chat template — it is a PyTorch audio language model, and
loading it needs `torch` and `transformers` in the runtime path. That is
**AGENTS.md constraint 4**, which exists because those two are hundreds of MB to GB of
RAM on a board with about one GB spare. There is no ONNX export in the repo. It stays
out until there is one; its zero-shot voice cloning would be genuinely useful offline,
in a separate tooling venv, to *produce* a voice that a lean engine then speaks.

### Option A — Piper for all three (start here)

| Language | Voice |
|---|---|
| English | `en_US-lessac-medium` (or preferred) |
| Bahasa Indonesia | `id_ID-news_tts-medium` |
| Arabic | `ar_JO-kareem-medium` (or current Arabic voice) |

~100 MB estimated; **~390 MB measured** with all three resident (M2) — ~85 MiB per voice, since
each carries its own onnxruntime session. Runtime is the `piper-tts` wheel, whose base install
adds only `onnxruntime` and `pathvalidate` (its `torch`/`transformers` extras stay uninstalled,
so constraint 4 holds). VITS-style ~15M-param ONNX models, CPU-capable, measured at RTF
0.24–0.34 here — real-time on far weaker hardware than this. **The only option with genuinely open, locally-runnable voices for all
three languages.** One runtime, one code path.

*Trade-off:* the Indonesian voice is a single community-trained voice — functional, not
polished.

### Option B — MagpieTTS (EN/AR) + Piper (ID) — upgrade path

Adds ~600 MB and a second runtime. MagpieTTS-357M via `magpie-tts.cpp` bundles model, codec,
tokenizer and G2P into one self-contained GGUF with no Python/PyTorch/CUDA at inference, and
its English and Arabic voices are more polished than Piper's.

**Only move to B if Option A's quality actually bothers you in practice.** Do not build B first.

---

## Orchestration & UI

| Component | Choice | Why |
|---|---|---|
| Language | **Python 3.10 + asyncio** | The job is coordinating concurrent I/O streams; heavy compute is already native |
| Serial | **pyserial** | Jetson ↔ STM32 over USB-CDC (`/dev/ttyACM0`) |
| UI | **NiceGUI** 3.16 (on FastAPI/Uvicorn) — **used as the server, not the renderer** | **Measured 60 MiB** (M8), under the 150 MB estimate. Provides FastAPI, uvicorn and the session middleware; the pages themselves are hand-written HTML/CSS/JS in `src/robot/ui/static/`, sharing the design system of the sibling robot project so the two look like one system. Runs as its own process and reaches the orchestrator only over a WebSocket — never by import — so the robot is unaffected when it dies. Brings `aiohttp`, used as the WebSocket client |
| Monitoring | **jetson-stats** (`jtop` Python API) | Reads unified RAM, GPU, CPU, temps, power, per-process memory |
| Supervision | **systemd** (`Restart=on-failure`) | On 8 GB an OOM kill is a "when", not an "if" — services must self-heal |

**UI auth:** the dashboard exposes drive controls on the LAN. NiceGUI session auth is enabled
(M8) and `robot-ui` **refuses to start** without `ROBOT_UI_PASSWORD` — an open page that can
drive a robot is worse than no page. Credentials live in `.env` (gitignored, constraint 8).
The orchestrator's own WebSocket is loopback-only and additionally requires a shared token,
so reaching the port is not enough.

---

## STM32 firmware

| Item | Choice |
|---|---|
| Language | **C** (C++ acceptable for driver classes) |
| Build system | **PlatformIO** (decided 2026-09-02) — `firmware/stm32/`, reproducible CLI builds, no IDE required on the Jetson. Set `PLATFORMIO_CORE_DIR` off the root filesystem |
| Framework | **STM32duino** (`framework = arduino`) — chosen in M5 for working USB-CDC out of the box. The safety-critical logic is portable C that does not touch the framework at all, so this is a low-stakes choice |
| Testing | Portable core built with plain gcc (`run_native_tests.sh`, 628 checks) and for the host by `tools/stm32_sim`, which speaks the real protocol over a PTY |
| RTOS | FreeRTOS optional — only if task scheduling gets complex |
| Responsibilities | Mecanum inverse kinematics, PID motor loops, encoder reads, servo PWM, LED matrix refresh, **heartbeat watchdog** |

No MicroPython or other interpreted runtime — it undermines the determinism that is the entire
reason for having an MCU.

---

## Explicitly forbidden in the runtime path

PyTorch · TensorRT-LLM · NeMo · HuggingFace `transformers` · NanoLLM · ROS 2 / micro-ROS / DDS
· any desktop environment

These are fine in a **separate offline venv** for one-off tooling (e.g. training a custom wake
word). They must never be imported by a service that runs while the robot is live.
