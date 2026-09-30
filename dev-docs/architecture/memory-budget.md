# memory-budget.md

**The hardest constraint in this project.** The Jetson Orin Nano 8GB has *unified* memory —
GPU allocations come out of the same 8 GB as everything else. Exceeding it means an OOM kill,
not a slowdown.

---

## Budget

| Component | Estimated | Measured | Notes |
|---|---|---|---|
| OS (headless, no desktop) | ~1.0 GB | **~0.65 GB** | M0. Under estimate. Excludes VS Code Server + Claude Code (~1.8 GB), which are dev tooling and absent on a production boot. `docker`+`containerd`+`snapd` add a further ~0.21 GB and are reclaimable — see Optional trims |
| ASR — Qwen3-ASR-0.6B (sherpa-onnx) | ~0.7 GB | **~1.50 GB** | M3. **~2× over estimate.** 1246 MiB after load, 1538 MiB peak while decoding. int8 ONNX export; the decoder alone is 721 MB on disk. CUDA provider gives no speed gain and no memory saving |
| TTS — Piper × 3 voices | ~0.1 GB | **0.39 GB idle → 0.89 GB after 45 min** ⚠️ | M2. **~4× over estimate.** ~85 MiB per voice resident, not the 61 MB of weights — the rest is onnxruntime session + arena overhead. Estimate counted weights only. Lazy-load one voice at a time to trade ~0.26 GB for a ~1.5 s pause on language switch. **M9: this is the one component that grows.** 342 → 889 MiB over a 45-minute soak while every other service stayed flat. Isolated: **Piper does not leak** — driven directly it plateaus at exactly 505 MiB from 30 syntheses onward. The service reached 606 MiB at 120 turns and 649 MiB after several hundred, with threads and file descriptors flat, so it is allocator growth that is still converging. Capped with `MemoryMax=1200M` so a runaway speaker restarts instead of the robot dying. **The 24 h soak has to settle whether it stays under that** |
| LLM — Qwen3-4B Q4_K_M + 2–4k ctx | ~3.0 GB | **~3.73 GB** | M1, 4096 ctx. Weights 2376 MiB + KV 576 MiB + compute buffers and CUDA context. KV is exactly 144 KiB/token (36 layers × 8 KV heads × 128 dim × 2 × f16), so 2k ctx saves 288 MiB |
| Embeddings — Qwen3-Embedding-0.6B | ~0.25 GB | **~0.63 GB** | M7. Q8_0 is the smallest quant published (610 MiB; the only alternative is f16 at 1.1 GB). RSS reads ~1.3 GB but most of that is the mmapped GGUF — evictable page cache, not pressure. **CPU-only and `CUDA_VISIBLE_DEVICES=`**: with the CUDA backend active it tried to allocate a 761 MiB compute buffer from the same unified pool as the LLM and failed to start |
| Vector store — sqlite-vec | ~0.05 GB | **negligible so far** | M7. In-process, no daemon. 3 chunks is nothing; re-measure with a real corpus |
| Wake word + VAD + orchestrator | ~0.3 GB | **~0.06 GB** | M4. `robot-core` is 36 MiB; Silero VAD +23 MiB, inside the ASR process. Well under estimate. Wake word costs nothing — it matches the transcript rather than running a second model (TECH_STACK § Listen). The noise defences also cost nothing at their defaults; GTCRN suppression is +4 to +19 MiB and is off |
| UI (NiceGUI) | ~0.15 GB | **~0.06 GB** | M8. Under estimate. Its own process, `Requires=` of nothing — killing it changes nothing about the robot |
| **Subtotal** | **~5.6 GB** | **6708 MiB measured** | M9, sampled 270× over a 45-minute soak with LLM (2048 ctx) + ASR + TTS + orchestrator + dashboard live. Not a sum of the rows above — those are each a peak, and the peaks do not coincide |
| **Headroom** | **~2.4 GB** | **~900 MiB measured** | Against the real 7620 MiB ceiling, not 8192. Includes a dev session; a clean boot has ~1 GB more |

**Target: stay at or under ~7 GB steady-state.** Treat anything above that as a bug.

> ### The budget closes — measured in M4, better than the M3 projection
>
> M3 projected ~7.0 GB and about 0.4 GB of headroom. That was **too pessimistic**: it
> summed each component's *peak* as though the peaks coincided, and added estimates for
> M7/M8 components that are not running. Measured with all four services live through a
> real three-turn conversation:
>
> | | 2048 ctx | 4096 ctx |
> |---|---|---|
> | LLM | 3391 MiB | ~3679 MiB |
> | ASR | ~1400 MiB | ~1400 MiB |
> | TTS (3 voices) | ~400 MiB | ~400 MiB |
> | **Embeddings (M7)** | **~633 MiB** | **~633 MiB** |
> | orchestrator | ~35 MiB | ~35 MiB |
> | **Dashboard (M8)** | **~60 MiB** | **~60 MiB** |
> | \+ OS baseline | **~6569 MiB** | **~6857 MiB** |
> | **Headroom against 7620 MiB** | **~1051 MiB** | **~763 MiB** |
>
> It still fits on a production boot. What no longer fits is **the stack plus a
> development session**: measured at M7, peak use hit **7368 MiB (97%) and 3204 MiB of
> swap**, and the thrashing was bad enough to corrupt the behavioural measurements
> around it. VS Code Server plus Claude Code is ~1.0–1.8 GB, and that is now more than
> the whole margin.
>
> **Rule from M7 onward: do not run the full stack and a dev session on the box at the
> same time.** Detach, or expect swap.
>
> **M9 added a second reason to keep headroom: restarts need it.** At 4096 ctx, with the
> rest of the stack up, there was not enough memory left for `llama-server` to come back
> after a crash — `Restart=on-failure` failed five times and gave up. `LLM_CTX` is now
> 2048 for that reason, not for steady-state use. A supervised service you cannot restart
> is not supervised.
>
> The trims below stay ordered and available; the first two are nearly free:
>
> 1. **LLM context 4k → 2k** — frees 288 MiB exactly (KV is 144 KiB/token, measured).
> 2. **Lazy-load TTS voices** — frees ~260 MiB, costs ~1.5 s on a language switch.
> 3. **Disable `docker`/`containerd`/`snapd`** — frees ~210 MiB, costs nothing at runtime.
> 4. **Qwen3-4B → Qwen3-1.7B** — frees ~1 GB, costs reasoning quality. The real fallback.

> Fill in the **Measured** column after each milestone. Estimates are a starting point;
> the measured numbers are the contract.

### Measurement log

Raw snapshots live in `scripts/measure/snapshots.jsonl`. Take one with:

```bash
python3 scripts/measure/ram_snapshot.py --label "M1 llama-server 4k ctx"
```

| Date | Milestone | Moment | RAM used | Notes |
|---|---|---|---|---|
| 2026-09-02 | M0 | Steady-state idle | 2403 MiB raw / **~650 MiB attributable** | Total RAM visible to the OS is 7620 MiB, not 8192 — the rest is carveout. Raw figure includes ~1793 MiB of VS Code Server + Claude Code. Swap 0. |
| 2026-09-02 | M1 | LLM resident, idle | 5750 MiB raw / **3728 MiB for `llama-server`** | Qwen3-4B Q4_K_M, 4096 ctx, all layers on GPU. Free RAM fell to 84 MiB **with the dev session still resident** — see the CUDA OOM note below. |
| 2026-09-02 | M2 | 3 Piper voices loaded + used | **395 MiB** | Measured in-process: 48 MiB interpreter+import, then ~85 MiB per voice. |
| 2026-09-02 | M3 | ASR loaded / decoding | **1246 MiB / 1538 MiB peak** | Qwen3-ASR-0.6B int8, 4 CPU threads. Peak is transient, during decode. |
| 2026-09-02 | M3 | Silero VAD | **23 MiB** | Inside the ASR process, not a separate service. |
| 2026-09-02 | M4 | **All four services, mid-turn peak** | **5274 MiB robot-only** / 6924 MiB raw | LLM 3391 (2048 ctx) + ASR 1387 + TTS 459 + core 36. Sampled 132× over a 3-turn conversation. Raw figure includes ~1000 MiB of dev tooling. |
| 2026-09-03 | M7 | **Everything, including the embedding server** | **7368 MiB used (97%)**, 3204 MiB swap | ⚠️ Thrashing — with a dev session attached the full stack no longer fits. See below. |
| 2026-09-03 | M8 | Dashboard | **60 MiB** | NiceGUI + aiohttp client. Cheapest service in the stack. |
| 2026-09-03 | M9 | **45-minute soak, 313 turns** | **peak 6708 MiB**, swap 310 MiB flat, 0 OOM | LLM 2048 ctx + ASR + TTS + core + UI, embeddings stopped. Peak 71.8 °C, no throttling. |
| 2026-09-04 | M9 follow-up | Services panel (start/stop/restart from the dashboard) | **0 MiB** | No new process and no new dependency: `robot.common.services` reads `/proc` inside `robot-ui`, which already sampled the board for the Monitor page. Its one subprocess is the supervisor command itself, which exits. |
| 2026-09-04 | M9 follow-up | whisper.cpp `tiny` / `base` / `small`, CPU-only, one clip decoded | **151 / 243 / 637 MiB** | Disk is 74 / 141 / 465 MiB, so resident is disk x 1.24 + 60 to within 8 MiB across a 6x range — that fit is what `catalog.WHISPER_DISK_TO_RAM` uses. Buffers grow with the longest clip seen: `base` settled at 286 MiB after a run of longer utterances. `medium` and `large-v3-turbo` are extrapolated, not measured — neither fits beside an LLM. **`base` is 1008 MiB less than Qwen3-ASR** and reports a language per utterance, which Qwen3-ASR does not. |
| 2026-09-04 | M9 follow-up | whisper `small` on the **GPU** vs CPU, same board, same clips | **915 MiB / 0.50 s** vs **637 MiB / 23.5 s** | RTF 0.17 against 8.08 — the GPU is ~46x faster on `small` and costs a ~280 MiB CUDA context, roughly fixed across model sizes. `tiny` is 1.52 s on the CPU, so CPU-only whisper is viable at `tiny`/`base` and unusable above them. In a real turn, GPU `small` decoded Arabic in **743 ms** against Qwen3-ASR's ~1300 ms. `WHISPER_USE_GPU` switches without a rebuild. |
| 2026-09-04 | M9 follow-up | Supertonic-3 TTS, one voice for all three languages | **484 MiB** (465 MiB in a bare process) | 380 MiB of ONNX on disk. Replaces three Piper voices (~255 MiB lazy / 395 MiB preloaded), so it costs roughly +230 MiB — and RTF 0.73-0.82 against Piper's 0.24-0.34, on the path to first audio. Loads in 1.5 s. |
| 2026-09-04 | M9 follow-up | **Qwen3-4B + whisper large-v3-turbo on GPU + Supertonic** | ⚠️ **does not fit** | The LLM failed with `cudaMalloc failed: out of memory` allocating its 288 MiB KV cache. Two CUDA allocators now draw on the unified pool -- llama-server and whisper -- and large-v3-turbo holds ~2 GB of it. Priced at 7330 MiB of a 7620 ceiling BEFORE Supertonic's +230; that is not a margin, it is a coin toss. Levers, cheapest first: `WHISPER_USE_GPU=0`, a smaller whisper checkpoint, `TTS_ENGINE=piper`, or a smaller LLM. |
| 2026-09-04 | M9 follow-up | Embedding server, on demand | **-810 MiB steady state** | `EMBED_MODE=on-demand`: resident only while the ingestion queue has work, then stopped. Wake to first embedding measured at ~6 s (start + model load + readiness). Retrieval falls back to FTS5 keyword search while it sleeps — no memory, but no cross-lingual retrieval. |
| 2026-09-09 | M11 | **robot-face** — Slint panel on DRM/KMS, release build | **11.0 MiB RSS**, 0 swap | Plus 8.4 MiB of DRM dumb buffers (1366x768 x4 bytes, double-buffered) which are in the unified pool but not in RSS. Estimated 40-70 MiB before measuring; the software renderer and a 14 MiB binary are much cheaper than assumed. For comparison the NiceGUI dashboard is 60 MiB — the panel could replace it on the box at a net saving. |
| 2026-09-09 | M11 | Display path enabled (`modprobe nvidia-drm`) | no steady cost | `card1-DP-1` connected at 1366x768. Before this the only DRM node was `tegra_drm` on host1x with **zero connector nodes**, so no KMS UI of any kind was possible. Now persisted via `/etc/modules-load.d/nvidia-drm.conf`. |
| 2026-09-09 | M11 | ⚠️ **A Rust build OOM-kills the LLM** | — | Linking robot-face pulls 211 object files; twice this got `robot-llm` killed and then unable to restart. Cause: `spawn_llm` dropped page cache for MODEL FILES ONLY, and 2.4 GB of cached build artefacts are equally unreclaimable to CUDA. `drop_model_cache.py --all` now drops the whole cache through the NOPASSWD sudoers entry, and `scripts/face/build.sh` stops the LLM for the duration. |
| 2026-09-06 | M10 | **Whole stack with the embedding server resident** | **6976 MiB used, 98 MiB free** | Qwen3-4B + whisper `small` on GPU + Supertonic on GPU + ASR + orchestrator + dashboard + `robot-embed`. It fits, and knowledge answers score 19/19. There is no margin at all: this is the configuration that motivated the 1B-3B bake-off below. |
| 2026-09-06 | M10 | Tazakka document ingested (2922-char PDF, 15 sections) | 15 chunks, 15.5 s including waking `robot-embed` | Re-ingest with the server already up: 10.3 s. Retrieval 18/18 by vector, including English and Arabic questions against an Indonesian document. |
| 2026-09-14 | M9+ | **Voice latency: 8209 ms → 2181 ms median** | no memory change | End of speech to first spoken word, 5 turns, Llama-3.2-3B + whisper `small` + Supertonic all on the GPU. Best turn **1554 ms**, against the 1500 ms target in system-overview.md. Nothing here was a model change: `vad_min_silence` pinned at 0.2 s had never reached robot-asr (core applies settings at startup and run_stack starts core first), registry defaults were being written over `config/core.env` at startup, and every latency number was optimistic by the endpointing silence because `speech_end` was timestamped when Silero declared the end rather than when speech stopped. Split now: endpoint 200 + decode 510 + first token 92 + first clause 926 + synthesis 963. `./scripts/measure/voice_latency.py`. |
| 2026-09-14 | M9+ | **`LLM_CTX` now buys reply length, not just prompt room** | KV cache scales with `LLM_CTX` as before | The reply budget was a flat 200 tokens, unrelated to the window: at `LLM_CTX=4096` the split was prompt 2703 + reply 200, leaving ~1190 tokens of the window that nothing was allowed to use, while answers were cut off mid-sentence. `CORE_MAX_TOKENS=auto` now derives it as `ctx - prompt budget - 128`, capped at 1024. **Raising `LLM_CTX` costs KV cache — price it here before raising it.** Measured: a detailed Indonesian answer ran to 487 tokens, so the old cap was losing ~60% of it silently. |
| 2026-09-14 | M9+ | ⚠️ **The Supertonic line was 884 MiB light, and the page said it fit** | estimate 484 MiB → **1368 MiB** on the GPU | The whisper line has always added `WHISPER_CUDA_CONTEXT_MIB`; the Supertonic line never added anything, so every `TTS_USE_GPU=1` selection was priced at its CPU cost. Found the moment the Memory page started showing measured RSS beside the projection and the row read **484 estimated / 1720 measured**. With the fix the same selection prices at 7041 MiB steady against a 7620 ceiling — and the board it was measured on was at 7244 used with **658 MiB of swap**, which is what the page had been reporting as "fits". `SUPERTONIC_CUDA_CONTEXT_MIB = 884`, from the M10 measurement of 1376 GPU against 492 CPU. |
| 2026-09-14 | M9+ | **Qwen3-ASR removed; whisper is the only decoder** | frees nothing at runtime, ~2 GB of choice | Not a memory change — only one ASR was ever resident. What it removes is the *possibility* of the wrong one: `ASR_ENGINE`, `ASR_MODEL_DIR`, `ASR_PROVIDER` and `ASR_THREADS` are gone, so a config can no longer name an engine that cannot load the selected model. The ASR row of the budget is now always a whisper checkpoint, priced by size, and the Memory page lists them tiny→large in that order. |
| 2026-09-14 | M9+ | **All three on the GPU, whisper `small` + Qwen2.5-3B** | **6184 MiB used / 510 MiB free**, 0 CPU fallbacks | Verified over 3 turns after a cold start: llm GPU, whisper GPU (decode 485 ms), Supertonic GPU, `GpuUnavailable` never raised. This is the configuration the GPU requirements hold in. |
| 2026-09-14 | M9+ | ⚠️ **whisper `large-v3-turbo` + Qwen2.5-3B + Supertonic does not hold** | 7244 MiB used, **658 MiB swap** | All three *load* on the GPU — large-v3-turbo puts 1623 MB on CUDA0 after a page-cache drop — and then Supertonic loses its CUDA workspace mid-utterance and raises `GpuUnavailable`. Before `TTS_REQUIRE_GPU` this was the silent CPU fallback. The lever is the LLM or the checkpoint: `small` leaves 510 MiB free where `large-v3-turbo` leaves none. |
| 2026-09-14 | M9+ | ⚠️ **The LLM was not on the GPU at all** | — | `wait_llm` matched `curl`'s exit status, which is 0 for the `503 {"error":{"message":"Loading model"}}` llama-server answers while loading. Measured: 503 at t=2 s, `{"status":"ok"}` at t=7 s. The start script declared it healthy 5 s early, started whisper on the GPU, and llama-server's 288 MiB KV cache then failed on `cudaMalloc`. The stack came up with **no LLM**, after printing "llama-server healthy". Fixed by matching the body, as `wait_whisper` always did. |
| 2026-09-14 | M9+ | **All three models on the GPU at once** | Qwen3-4B: fits at boot, **does not hold** | 6583 MiB used / 209 MiB free with Qwen3-4B + whisper `small` + Supertonic all on the GPU. It survives startup and then loses the CUDA allocation under load: Supertonic rebuilds on the CPU mid-conversation and first-audio goes 2149 ms → 8213 ms. Swap starts being touched. |
| 2026-09-14 | M9+ | **Same, with Qwen2.5-3B instead of Qwen3-4B** | **5613 MiB used / 1151 MiB free**, holds | The bake-off's own recommendation, taken. 8 turns then 5 more with **zero CPU fallbacks**, 414 MiB free at the tightest. This is the configuration that actually keeps all three on the GPU while keeping whisper `small`. whisper `base` is the other way to buy the same headroom (750 MiB free, ASR decode 487 → 239 ms) and costs transcription accuracy instead of answer quality. |
| 2026-09-14 | M9+ | ⚠️ **What the GPU is worth for TTS once the board is full** | no memory change | The M10 figure of RTF 0.06 was measured **with whisper stopped**. With the LLM and whisper also resident: 5 chars → 235 ms, 44 chars → 2258 ms, 130 chars → 7551 ms (RTF 0.18 / 0.68 / 0.83). The same sentence on the CPU is 3414 ms, so the 884 MiB CUDA context buys about **1.5×, not 13×**. `SUPERTONIC_STEPS` barely moves it (2246 ms at 4 steps, 2219 ms at 2) — it was the cheapest control when Supertonic was CPU-bound and is not any more. The length of the first chunk is what controls time-to-first-word now, which is why `first_max_chars` became a setting. |
| 2026-09-14 | M9+ | **Speaking the answer when its JSON string closes** | no memory change | `answer` is the first field in the schema precisely so it can be spoken while the rest is written, but the chunker was only flushed after the WHOLE object finished. The chunker breaks on a terminator followed by whitespace, and the closing quote of a JSON string is not whitespace — so a one-sentence reply emitted nothing until `language`, `foot_direction`, `foot_step`, `hand_movement`, `face` and `style` had all been generated. Flushing at `AnswerStream.done` took first-chunk from 3199-4497 ms to **2085-2416 ms**, about 1.9 s off every short reply. |
| 2026-09-14 | M9+ | ⚠️ **The microphone could latch shut for ever** | — | The capture gate closes on robot-tts's `started` and only `finished`/`error`/cancel reopen it — all three from robot-tts. A robot-tts killed, restarted or driven by a second client mid-playback never sends one, and the robot was then **silently, permanently deaf**; only a `pong` on the ASR socket reading `gate_open = False` showed it. `turn_timeout_s` already covered the turn; the gate now has the same protection plus a `forget_speaker()` on connection loss. |
| 2026-09-13 | M9+ | **Noise defences on the capture path** | **0 MiB by default**; +3.9 MiB streaming / +18.5 MiB per-utterance if suppression is switched on | The level gate and the confidence gate cost nothing at all — one RMS over the utterance, and two numbers whisper was already returning and the pipeline was discarding. GTCRN suppression is 523 KiB on disk and off by default because it measured WORSE than raw audio (next row), not because of its memory. `ASR_DENOISE=stream` costs RTF 0.122 of one CPU core continuously, worst frame 4.8 ms of a 32 ms budget, 16 ms of added latency; `utterance` costs RTF 0.095 on the critical path of every decode and nothing between utterances. |
| 2026-09-13 | M9+ | ⚠️ **GTCRN suppression makes whisper worse, and was shipped off** | — | Word error rate against the same engine's clean transcript, Piper speech mixed with noise: stationary noise **+27% to +314%**, babble **+58% to +215%**, across 15/10/5/0 dB SNR. Against Qwen3-ASR, roughly neutral (one clear win at 15 dB, one clear loss at 5 dB). The artefact problem — whisper is trained on real noisy audio and a denoiser's output is a kind of damage it has never seen. Kept and documented rather than deleted: the mixtures are synthetic, with no reverberation and no far-field speech, so `./scripts/measure/denoise_ab.py --noise <a recording of the real room>` is what settles it on site. |
| 2026-09-13 | M9+ | whisper's own confidence, on this board | — | The measurement the defences are fitted to. Real speech, clean down to 0 dB SNR: `no_speech_prob` **0.006–0.087**, `avg_logprob` −0.06 to −0.63. Room noise with no speech in it: `no_speech_prob` **0.820–0.911**, `avg_logprob` −0.70 to −0.76. Twenty seconds of clatter with nobody speaking decoded as `(knocking)` and `(thud)`, both tagged **English** — so `ASR_LANGUAGES` passed them through to the LLM. Both are now rejected before they become a turn. |
| 2026-09-06 | M10 | ⚠️ **Retrieval silently degrades to keyword search** | no memory change, large accuracy change | `EMBED_RETRIEVAL=warm` + a sleeping embedding server = FTS5 keyword search. Indonesian questions still pass, because they share words with an Indonesian document; **an Arabic question retrieved 0 chunks and the model invented two names.** Fixed the half of it that was a bug (`EmbedServer.adopt`, which was ignoring a server it had not started itself); the rest is the on-demand trade-off, and the lever is `EMBED_MODE=always` at ~810 MiB. |

### What the embedding server is worth, measured (M10, 2026-09-06)

Same document, same questions, same model. The only difference is whether `robot-embed`
was resident when the question was asked:

| retrieval | answers | the ones that fail |
|---|---|---|
| by vector (embedding server up) | **19/19** | -- |
| by keyword (FTS5 fallback, server asleep) | **16/19** | every cross-lingual question |

The three losses are precisely the questions asked in a language the document is not
written in. "Where is Pondok Modern Tazakka located?" survives only because the proper
noun matches literally; "How many hectares does Tazakka have?" retrieves the wrong
chunks, and the Arabic question retrieves **nothing at all**.

So the ~810 MiB the embedding server costs is not overhead -- it is the multilingual
half of a multilingual robot. On this board that money has to come from somewhere, and
the bake-off below is where: Qwen2.5-3B gives back 841 MiB for two questions of accuracy,
which buys back three.

### LLM bake-off, 1B-3B alternatives (M10, 2026-09-06)

`./scripts/measure/llm_bakeoff.py` -- 19 questions about an ingested document, every
model at the same `CORE_MAX_TOKENS=260`, retrieval by vector, answers scored by whether
the expected facts appear.

| model | answers | facts | median | resident | invented a fee |
|---|---|---|---|---|---|
| Qwen3-4B-Instruct-2507 (current) | **19/19** | **39/39** | 5935 ms | 3402 MiB | no |
| Llama-3.2-3B-Instruct | 18/19 | 38/39 | 4979 ms | 2862 MiB | **yes** |
| **Qwen2.5-3B-Instruct** | 17/19 | 38/39 | 4678 ms | **2560 MiB** | **no** |
| gemma-3-1b-it | 17/19 | 36/39 | **3438 ms** | 1475 MiB | **yes** |
| granite-3.3-2b-instruct (IBM) | 16/19 | 35/39 | 4495 ms | 2076 MiB | no |
| Sailor2-1B-Chat | 16/19 | 33/39 | **2401 ms** | **1117 MiB** | **yes** |
| Qwen3-0.6B | 11/19 | 20/39 | 3496 ms | 1073 MiB | no |
| Qwen3-1.7B | 7/19 | 13/39 | 6178 ms | 1895 MiB | no |

**The last column is the one that decides it.** Asked what the school fees are -- which
the document does not say -- Llama-3.2-3B answered "sekitar empat puluh juta rupiah per
tahun", gemma-3-1b "sekitar 100 ribuan per tahun", and Sailor2-1B produced a fully
itemised "Rp 75.000/bulan atau SPP Total: Rp 11.250.000,- per tahun". All three are
invented, and all three are the kind of answer a parent would act on. Qwen3-4B,
Qwen2.5-3B and granite refused and said to ask the pondok.

That column is sampled at `temperature=0.7`, so it is a tendency rather than a verdict:
Qwen2.5-3B tripped the flag on one run and refused cleanly on another. A model that
refuses *most* of the time is still the safer one.

**Qwen3-1.7B and Qwen3-0.6B are not usable here**, and it is worth knowing why: they are
hybrid-reasoning models, the JSON grammar leaves them nowhere to put their reasoning, and
they spend the token budget on preamble and get cut off mid-object. 37 truncated replies
across the run. `bakeoff` now reports a `cut_off` column so this is visible rather than
inferred.

**Recommendation: Qwen2.5-3B-Instruct.** It keeps 17/19 and 38/39 facts, is 21% faster
than the 4B, refuses to invent, and gives back **841 MiB** -- which is what buys the
embedding server a permanent home, and with it cross-lingual retrieval. Switch on the
Memory page, or `LLM_MODEL=` in `config/llm.env` plus a restart of `robot-llm`.

| 2026-09-06 | M10 | **Supertonic on the GPU** (`TTS_USE_GPU=1`, onnxruntime-gpu 1.24.0) | **1376 MiB** vs 492 MiB on the CPU | +884 MiB for the CUDA context, cuDNN handles and workspace. Buys RTF 0.75 → **0.06**, a 13× speedup. Importing the CUDA wheel costs nothing on its own: the CPU path measures 492 MiB under either wheel, so the fallback is free. |
| 2026-09-06 | M10 | GPU TTS **alongside LLM + whisper on GPU** | ⚠️ **does not fit** | `cudnnCreate` returns `CUDNN_STATUS_INTERNAL_ERROR` at the FIRST synthesis, not at session creation, with `NvMapMemAllocInternalTagged ... error 12` (ENOMEM) in the log. What matters is `free` (121 MiB at the time), not `available` — NvMap cannot reclaim page cache. robot-tts rebuilds itself on the CPU in 1.4 s and keeps talking. With whisper stopped it succeeded and held RTF 0.06 with the LLM resident. |
| 2026-09-06 | M10 | Supertonic `total_steps` ladder, CPU, 54-char Indonesian sentence | no memory change | RTF **0.75** at 8 steps (the SDK default), 0.58 at 6, 0.49 at 5, **0.41 at 4**, 0.32 at 3, 0.23 at 2. Very nearly linear, and free — the cheapest speed control in the system. Default is now 4. |
| 2026-09-04 | M9 follow-up | Gemma-3-1B Q4_K_M, 2048 ctx, all layers on GPU | **~1050 MiB** | For comparison with Qwen3-4B's 3391 MiB at the same context. Fits alongside ASR + embeddings + TTS + core + UI **and** a dev session, which Qwen3-4B does not. See START_HERE on what it costs in answer quality. |

**Caveat on all numbers so far:** they were taken on a developer session, not a clean boot.
VS Code Server + Claude Code hold ~1.8 GB throughout. Re-measure after a reboot with nothing
attached before treating any of these as final.

### CUDA OOM is not the same as "out of RAM" (M1)

`llama-server` failed twice with `cudaMalloc failed: out of memory` while `free` still
reported ~5.3 GB *available*. On Tegra, CUDA allocates through NvMap, which will **not** force
the kernel to reclaim page cache. Having just downloaded ~3.4 GB of models, that cache was all
model data, and CUDA saw only the genuinely-free pages.

`sudo sysctl vm.drop_caches=3` fixes it but needs root. `scripts/measure/drop_model_cache.py`
does the same thing unprivileged, with `posix_fadvise(POSIX_FADV_DONTNEED)` on the model files.
**Run it after fetching a model and before starting a service that loads one.**

This is a real production failure mode, not a developer-only artifact: any service restart that
follows heavy file I/O can hit it. Budget for it rather than being surprised by it.

**Note the real ceiling:** `jtop` reports **7620 MiB** total, not 8192 — ~570 MB is Tegra
carveout and never available. Budget against 7620, which makes the ~7 GB steady-state target
tighter than it looks: it leaves ~450 MiB, not 1 GB.

---

## Estimating before you load

`robot.core.catalog` prices a model without loading it, so the dashboard's **Memory**
page can show what a selection would cost before anything is applied. The estimates are
derived from the measurements in this document, not invented:

| Component | Rule | Basis |
|---|---|---|
| LLM | weights (file size) + KV + **750 MiB** runtime | M1: 3728 MiB at 4096 ctx with 2382 MiB of weights and 576 MiB of KV |
| — its KV | computed from the GGUF header: `2 × layers × kv_heads × head_dim × 2 B` | Qwen3-4B works out to **144.0 KiB/token**, exactly what M1 measured |
| ASR | disk × **1.32** | M3: 941 MiB on disk → 1246 MiB resident |
| — decode peak | **+300 MiB**, transient | M3: 1246 → 1538 MiB while decoding |
| Piper voice | max(**85 MiB**, disk × 1.4) | M2: ~85 MiB per voice regardless of the 61 MiB file |
| Embeddings | disk × **1.04** | M7: 610 MiB on disk → 633 MiB anonymous |

Checked against what was actually measured:

| | Estimated | Measured | Error |
|---|---|---|---|
| Qwen3-4B @ 2048 ctx | 3420 MiB | 3391 MiB (M1) | +0.9% |
| Qwen3-4B @ 4096 ctx | 3708 MiB | 3728 MiB (M1) | −0.5% |
| Qwen3-ASR-0.6B | 1243 MiB | 1246 MiB (M3) | −0.2% |
| Qwen3-Embedding-0.6B | 634 MiB | 633 MiB (M7) | +0.2% |

The KV figure is the reason this is worth trusting: it is read from the model's own layer
and head geometry rather than scaled from the file size, so it stays right for a model
nobody has measured yet.

**What it costs to choose**, full stack with ASR, three voices and embeddings:

| Selection | Steady | Peak | Headroom |
|---|---|---|---|
| Qwen3-0.6B @ 2048 | 4244 MiB | 4544 MiB | 3076 MiB |
| Qwen3-1.7B @ 2048 | 4922 MiB | 5222 MiB | 2398 MiB |
| **Qwen3-4B @ 2048** (current) | 6312 MiB | 6612 MiB | 1008 MiB |
| Qwen3-4B @ 4096 | 6600 MiB | 6900 MiB | 720 MiB |
| Qwen3-4B @ 8192 | 7176 MiB | 7476 MiB | 144 MiB ⚠️ past the target |

And every LLM/ASR pairing installed, at 2048 ctx with three voices and embeddings:

| | ASR 0.6B | ASR 1.7B |
|---|---|---|
| Qwen3-0.6B | 4544 MiB · 3076 free | 6328 MiB · 1292 free |
| Gemma-3-1B | 4763 MiB · 2857 free | 6547 MiB · 1073 free |
| Qwen3-1.7B | 5222 MiB · 2398 free | 7006 MiB · 614 free |
| **Qwen3-4B** | **6612 MiB · 1008 free** | **8396 MiB · ⚠️ over by 776** |

The bottom-right cell is why the page exists: the two best models cannot both be loaded,
and that is worth knowing before a service fails to start rather than after.

## How to measure

```python
from jtop import jtop

with jtop() as jetson:
    while jetson.ok():
        print(jetson.stats)          # RAM, GPU, CPU, temps, power
        print(jetson.memory)         # detailed breakdown incl. per-process
```

Install: `sudo -H pip install -U jetson-stats`, then reboot.
Interactive check: just run `jtop`.

**Measure at three moments, not one:**
1. Steady-state idle (all services up, no conversation)
2. Mid-turn peak (ASR + LLM + TTS all active simultaneously)
3. Model load spike (transient, often the highest point)

---

## Rules

1. **One LLM resident at a time.** If a fallback model is wanted, cold-swap it *after* the
   primary has unloaded and memory is reclaimed. Never both at once.
2. **Cap context, summarize old turns.** KV cache grows with context length and is the
   memory leak people forget to budget for.
3. **No PyTorch / TensorRT-LLM / NeMo / transformers in the runtime path.** Each is hundreds
   of MB to GB for zero runtime benefit here — the lean C++/ONNX engines do the same work.
4. **Headless always.** No desktop session on a production boot.
5. **Fail loudly at startup**, not at runtime. If a model won't fit, refuse to start with the
   measured number in the log — don't let the OOM killer pick a victim mid-conversation.
6. **Check every new dependency against this table before adding it.** Update the table in the
   same change.

---

## Optional trims if the budget gets tight

In rough order of preference:

| Action | Frees | Cost |
|---|---|---|
| Drop LLM context 4k → 2k | ~300–500 MB | Shorter memory of the conversation |
| Qwen3-4B → Qwen3-1.7B (Q8) | ~1.0 GB | Noticeably weaker reasoning |
| Qwen3-ASR → whisper `base` (`ASR_ENGINE=whisper`, GPU) | ~730 MB | Weaker transcription than Qwen3-ASR, especially Arabic. Gains a real per-utterance language id, which Qwen3-ASR does not emit at all. `small` gives most of the accuracy back for ~900 MiB on the GPU. `WHISPER_USE_GPU=0` saves the 280 MiB CUDA context but makes `small` and above unusably slow |
| whisper on the CPU (`WHISPER_USE_GPU=0`) | ~280 MB, and the GPU contention with it | Decode goes from 0.50 s to 23.5 s on `small`. Only sane at `tiny`/`base`. The first thing to try when the LLM cannot allocate its KV cache |
| Supertonic → Piper (`TTS_ENGINE=piper`) | ~230 MB | The robot's voice changes with the language again — three checkpoints are three different speakers |
| Embedding server on demand (`EMBED_MODE=on-demand`, now the default) | ~810 MB | Retrieval is keyword-only while it sleeps: no cross-lingual matching. Ingestion is unaffected — it is what wakes it |
| Stop one service from the dashboard's Services page | its whole RSS | Only that capability, and only until it is started again. The honest way to make room for a model on a board that is already full — the page shows what each process is holding before you press anything |
| Disable the UI process when not developing | ~150 MB | No dashboard |
| Disable `docker` + `containerd` + `snapd` | ~210 MB | Measured M0. Only if `jetson-containers` is not used at runtime — we build llama.cpp from source, so nothing needs Docker while the robot is live |
| Minimal Ubuntu base image instead of the stock NVIDIA rootfs | ~250–400 MB | More setup work; do this only if needed |
| NVMe swap as an OOM shock absorber | — | Latency spikes when touched — a safety net, **not** a capacity plan |

---

## Warning signs

- `jtop` swap usage climbing during normal conversation → context or history is unbounded
- RAM creeping up over hours → a leak in the orchestrator, likely audio buffers or chat history
- OOM kill in `dmesg` → budget is wrong; re-measure before changing anything else
- Thermal throttling under MAXN SUPER → airflow problem, not a memory problem, but it will
  look like "the robot got slow"
