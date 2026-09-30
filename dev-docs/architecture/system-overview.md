# system-overview.md

## Process layout

Five independently supervised processes. Nothing is a monolith, so one crash cannot take the
robot down.

```
┌─ systemd ──────────────────────────────────────────────────────┐
│                                                                │
│  robot-llm.service      llama.cpp server    :8080 (OpenAI API) │
│  robot-asr.service      sherpa-onnx ASR     local socket/WS    │
│  robot-tts.service      Piper / Supertonic   local socket/WS   │
│  robot-core.service  ◄── the orchestrator (asyncio) ───────────│
│         │                                                      │
│         ├── pyserial ──► /dev/ttyACM0 ──► STM32                │
│         ├── sqlite-vec ─► knowledge.db                          │
│         └── WebSocket ──► robot-ui.service (NiceGUI :8081)     │
└────────────────────────────────────────────────────────────────┘
```

**`robot-core` is the only process that talks to the STM32.** Everything else reaches hardware
through it.

**`robot-ui` is optional by design** — the robot must keep listening and driving with the
dashboard closed. A UI bug must not be able to stop the robot.

> ✅ **Verified in M8 by killing it.** `SIGKILL` on `robot-ui`: the robot kept answering,
> kept executing `DRIVE`, and the heartbeat to the STM32 never stopped. `robot-ui.service`
> is `Requires=` of nothing, imports nothing from `robot.core`, and reconnects by itself.

`./run.sh headless` is that design made into an entry point: the whole voice
stack with no dashboard, which is the normal way to run this robot. It prints the config
it is about to use and starts nothing else. Everything tuned in the browser has already
been persisted -- hot settings to `run/settings.json`, which `robot-core` replays before
the first turn, and restart-scoped choices to `config/*.env`, which the dashboard writes
on Apply -- so headless mode configures itself by reading what the dashboard left behind.
`--check` prints that config without starting anything, which is how you confirm a change
stuck.

The same launcher offers the other two faces: `./run.sh all` adds the browser dashboard,
`./run.sh slint` adds `robot-face`, a native panel on a screen attached to the robot,
rendered straight to DRM/KMS. Both are optional, independent websocket clients of
robot-core; the voice pipeline is byte-for-byte identical in all three modes. See
`dev-docs/modules/face.md`.

### The two conversation pages are two different machines

`/headless` is the robot as it ships: microphone in, speaker out, no keyboard.
`/chat` is a keyboard: typed in, **text out, silent**.

Every turn carries a `source` of `voice` or `text` (`Orchestrator.start_turn`), a typed
turn never reaches robot-tts, and every turn-scoped event is stamped with it so each page
renders its own modality and only its own. The split is not cosmetic: when typing was
answered aloud, a reply could not be read without the room hearing it, and the robot then
heard *itself* -- so the microphone gate had to defend against a turn nobody had spoken.

Under each robot reply both pages show the model's full JSON object, collapsed
(`Robot.replyFields`). Only `answer` is ever spoken; the other six fields are what the
body did while it spoke, and they are the only place the UI answers "why did it turn
left?".
> The link is a WebSocket `robot-core` serves on loopback, gated by a shared token; the
> dashboard carries the session auth for the LAN side.

> It is acceptable to start with `core` and `ui` in one process to move faster, but keep the
> boundary clean (UI talks to core over a WebSocket, not by calling core's functions directly)
> so splitting later is a config change, not a rewrite.
>
> M8 did not take that shortcut: they were separate processes from the start, and the
> WebSocket is real. `robot.common.ws` is a stdlib-only server, hand-rolled for the same
> reason `robot.core.llm` speaks HTTP by hand — `robot-core` holds the STM32 heartbeat and
> is worth keeping at zero dependencies. The dashboard uses aiohttp (already present via
> NiceGUI), which also means the server is tested against a third-party client.

---

## Conversational turn flow

```
1. IDLE
   mic always open → Silero VAD + openWakeWord running continuously (near-zero cost)

2. WAKE
   wake word fires
   → play a short chime IMMEDIATELY  (buys ~200-300 ms of warm-up while the user feels heard)
   → open the ASR stream

3. LISTEN                                    ⚠️ CORRECTED BY MEASUREMENT (M3)
   Silero VAD endpoints on ~650 ms of silence
   → the closed utterance is decoded offline (RTF ~0.4-0.55, so ~1.3 s for 2.4 s
     of speech). There are NO partial transcripts: sherpa-onnx exposes Qwen3-ASR
     only on OfflineRecognizer.
   → emit final transcript. NO language tag: result.lang is always '' for this
     model. See ROADMAP.md M3 for the three options and the recommendation.

4. RETRIEVE                                  ✅ IMPLEMENTED (M7)
   embed the query (Qwen3-Embedding, on robot-embed :8082) → top-k from sqlite-vec
   Measured ~140-220 ms, against a <100 ms budget. Cross-lingual: an Indonesian or
   Arabic question reliably finds an English document (8/8 in testing).
   The retrieved notes go into the prompt BEFORE the language rules -- put after,
   an English note pulls the reply into the wrong language (measured, M7).

5. THINK
   assemble: persona + language instruction + retrieved context + recent history + new turn
   → POST to llama.cpp, stream tokens back

6. SPEAK  (the latency-critical part)          ✅ IMPLEMENTED (M4)
   buffer streamed tokens to the first sentence boundary
   → send that sentence to TTS and start playing it
   → keep generating the next sentence while the first is still playing
   This is what separates "instant reply" from "long silence, then a wall of speech".
   The first chunk is also capped at ~90 chars and may break at a clause: without an
   upper bound, one long opening sentence delays ALL audio (measured, M4).
   Chunks queue in robot-tts and share one aplay stream, so sentence boundaries are
   not audible as gaps.

   Inside robot-tts, RENDERING AND PLAYBACK ARE SEPARATE TASKS. They were one loop
   until M10, and that was wrong in a way only audible on the robot: `aplay` consumes
   in real time behind a ~64 KB pipe, so pushing a rendered sentence into it blocked
   until that sentence had nearly finished playing -- and only then did the next one
   start rendering. Every boundary therefore cost a whole synthesis. At Supertonic's
   RTF of 0.75 that is ~2 s of silence between sentences, which is what it sounded
   like. Now `run_synth` renders ahead into a bounded queue and `run_player` drains it,
   so any RTF below 1 closes the gaps after the first boundary.
   Measured on a five-sentence Indonesian reply: 15.05 s of audio delivered in 15.74 s
   wall, first audio at 599 ms. Before, the same reply took ~26 s.

   Sentences that have ALREADY arrived are joined into one synthesis call
   (`TTS_BATCH_SENTENCES`, default 4) -- never a wait for more text, and never the
   first chunk of a turn, which is what time-to-first-audio measures.

> **Chunking follows the document's own structure.** `rag.split_sections` splits at
> headings (`## X`, `3. X`) when a document has at least three of them, and only falls
> back to cutting every `CHUNK_CHARS` when it does not. A document written as numbered
> facts otherwise becomes chunks of five unrelated facts: the embedding is an average of
> all five and matches none of them well, and every hit drags the other four into a
> prompt with a hard budget. Measured on the Tazakka document (2922 chars, 15 sections):
> size-based chunking had `trim_context` discard 51 of 72 retrieved chunks; heading-based
> discarded none, for a third less context and the same answers.
>
> PDFs are read at ingest time with poppler's `pdftotext` -- a C binary already on the
> board, not a Python parser in the runtime path. A scanned PDF has no text layer and is
> refused with a reason rather than stored as an empty document.

7. ACT                                       ✅ IMPLEMENTED (M6)
   markers parsed out of the reply stream → one serial write each
     [lang:en] Sure, moving forward now. [do:drive vx=0.2 ms=1000]
   Inline markers rather than the OpenAI tools API, because that API returns tool
   calls INSTEAD of content, in their own turn -- decide, then act, then speak. This
   fires the moment the marker is parsed, without awaiting, so the sentence around it
   is still being spoken. Measured: audio at 2952 ms, tool at 3397 ms.
   ERR and motors=stall come back as a note in the next turn's prompt.

8. BARGE-IN  (runs throughout step 6)          ✅ IMPLEMENTED (M4)
   mic stays active during playback
   if the user speaks → stop playback, cancel generation, jump to step 3
   Triggered by the ASR's speech_start event, which fires when VAD opens an
   utterance -- before decoding, so the robot stops within ~250 ms rather than
   waiting ~800 ms for a transcript. Dropping the LLM connection aborts generation
   server-side, and the partial reply is removed from history so the model does not
   believe it said what the user never heard.
   NOTE: echo cancellation is still NOT implemented. It has not been needed here --
   this hardware's speaker and mic barely couple -- but a louder speaker or an
   enclosure will change that. PulseAudio's module-echo-cancel is the intended home
   for it, which is one reason audio now routes through PulseAudio.
```

**Background, off the hot path:** document ingestion and `save_memory()` writes go through
`rag.Ingestor`, which owns a queue and a worker -- both return immediately (M7).
Conversation summarisation is still not implemented; history is bounded by turn count,
which stops the leak but loses the oldest turns outright.

---

## Language routing

> **Resolved in M4.** M3 found that Qwen3-ASR returns no language tag (`result.lang` is
> always `''`), which left this table without a source. Of the three options in
> `ROADMAP.md` M3, **option 1 is now implemented: the LLM declares the language itself.**
> It costs no RAM and no extra model — the one already resident is multilingual and is
> already instructed to reply in the user's language, so it knows the answer.
>
> The system prompt asks it to lead every reply with `[lang:en|id|ar]`;
> `robot.core.language.strip_declaration` removes that before any text reaches TTS.
> Resolution is layered, because a model will eventually not comply:
>
> 1. the declared tag, if present and known;
> 2. otherwise the script of the reply — which identifies Arabic unambiguously;
> 3. otherwise the previous turn's language, then English.
>
> Script also **overrules** a declaration that contradicts it: if the model says
> `[lang:en]` and then writes Arabic, the Arabic wins and gets an Arabic voice.
>
> Measured across a three-turn EN/ID/AR conversation: **3/3 resolved via `declared`.**
> Note the routing now happens *after* the first tokens arrive rather than before the LLM
> call — the standing rule below is what makes that work, since the model follows the
> user's language without being told in advance.

The tag drives everything downstream:

| Detected | LLM instruction | TTS voice |
|---|---|---|
| `en` | "Reply in English" | `en_US-*` |
| `id` | "Reply in Bahasa Indonesia" | `id_ID-news_tts-medium` |
| `ar` | "Reply in Arabic" | `ar_*` |

Plus one standing line in the system prompt: *"Always reply in the same language the user just
spoke."* That, plus this table, is the entire multilingual logic — no mode switching, no user
setting.

---

## Latency budget (target)

| Stage | Target | **Measured (M4, full loop)** |
|---|---|---|
| Wake word detection | < 100 ms | not implemented — blocked, see ROADMAP M3 |
| Chime starts | immediate | not implemented |
| ASR endpoint after user stops | ~600–700 ms | 650 ms (configured) |
| **ASR decode** — *was not in the budget* | — | **805 ms** ⚠️ |
| → first token (prefill) | — | 228 ms |
| LLM first sentence | ~300–500 ms | **943 ms** ⚠️ |
| TTS first audio | ~200 ms | **415 ms** |
| **End of speech → first audio out** | **~1.5 s** | **2393 ms mean** ⚠️ (1722 / 2205 / 3252) |

Per language, end of speech → first audio: **ID 1722 ms, EN 2205 ms, AR 3252 ms.**

> **The 1.5 s target is missed by ~0.9 s on the mean.** Two terms account for almost all
> of it, and neither is a buffering bug:
>
> 1. **ASR decode, 805 ms, was never in the budget.** Qwen3-ASR is offline — nothing can
>    overlap it, because there is no transcript until it finishes.
> 2. **First sentence generation is 943 ms against a 300–500 ms budget** — because
>    generation measures 17.9 tok/s, not the 35–43 tok/s the budget assumed. The chunker
>    is already emitting at the earliest legal point (it caps the first chunk at 90
>    characters rather than waiting for a long opening sentence); the tokens simply are
>    not there sooner.
>
> Arabic is the worst case at 3252 ms: it needs more tokens per character, and Piper runs
> tashkeel diacritisation on it (415 ms mean synthesis, vs 167 ms for English in M2).
>
> The note below about buffering still stands for everything downstream of the ASR — but
> the honest reading of this table is that **the 1.5 s target assumed an ASR with no decode
> cost and an LLM twice as fast as this one.** Closing the gap means a smaller/faster ASR,
> a faster LLM, or revising the target.

If this misses, look at the sentence-chunking in step 6 before blaming model speed —
generation at 35–43 tok/s is far faster than speech, so a slow-feeling robot is almost always
a buffering bug, not an inference bottleneck.

---

## Failure design

| Failure | Required behaviour |
|---|---|
| Orchestrator crashes / OOM | STM32 heartbeat times out → **motors zeroed by the STM32 itself**; systemd restarts core |
| LLM server dies | Core reports unhealthy in UI; speak a canned "give me a moment" line; systemd restarts it |
| TTS fails | Reply still appears in the UI chat; log it; do not hang the turn |
| ASR fails | Wake word still works; robot says it didn't catch that |
| Serial disconnects | Stop issuing motion commands; surface it in the UI health panel; retry reconnect |
| Model won't fit in RAM | Fail loudly at startup with the measured number — never let the OOM killer decide |

**Rule:** every failure path must leave the robot physically safe (no moving motors) and
audibly or visibly honest (the user or the dashboard learns something went wrong).

### The embedding server comes and goes

`robot-embed` is the one service that is not expected to be running. Under
`EMBED_MODE=on-demand` (the default since M9) `robot-core` starts it when the ingestion
queue has work and stops it once that queue drains — 810 MB back for the overwhelming
majority of the time, which on a board with ~1 GB of headroom is the difference between
fitting and not.

```
knowledge queued ──> EmbedServer.acquire()
                       │  not running?  services.control("embed", "start")
                       │  would not fit? stop EMBED_EVICT (the LLM), retry
                       ▼
                     ingest ──> queue empty ──> release()
                       │
                       ▼  idle (EMBED_IDLE_S, or ~5 s if something was evicted)
                     stop embed ──> restart whatever was evicted
```

The eviction ordering is the invariant: the LLM comes back **after** the embedding server
is down, never beside it, so the peak is bounded by the larger of the two rather than
their sum. Every exit path restores — a failed start, a failed retry, and orchestrator
shutdown — because a robot silently left without its LLM is far worse than a failed
ingestion.

A question is a different matter. Retrieval needs the *query* embedded into the same space
as the stored chunks, which only the model that stored them can do; the conversational LLM
cannot substitute and neither can sqlite-vec. So `EMBED_RETRIEVAL` decides what a turn does
when the server is asleep: `warm` (vectors if it happens to be up, FTS5 keyword search
otherwise — a turn never waits for a model to load), `start` (wake it and wait), or `off`.
Keyword search cannot do cross-lingual retrieval, which is what the vector store was built
for, and the health panel and logs say which path each query took rather than leaving it to
be inferred from result quality.

### The reply is an object, not prose

The model answers with a JSON object whose shape is enforced by a grammar
(`robot.core.reply.SCHEMA`), not requested in the prompt:

```json
{"answer": "Baik, saya maju sedikit.", "language": "id",
 "foot_direction": "forward", "foot_step": 2,
 "hand_movement": "wave", "face": "happy", "style": "cheerful"}
```

**Only `answer` is spoken.** The rest is what the body does while it says it:
`foot_direction`/`foot_step` become one bounded `DRIVE`, `hand_movement` a `HAND`,
`face` a `FACE` — through the same `ToolBox` the `[do:...]` markers used, so validation,
refusals and the activity log are unchanged.

Two properties make this work on a robot rather than in a script:

* **The shape cannot come back wrong.** llama.cpp constrains generation to the schema,
  so there is no half-parsed marker, no invented field, and no prose where an object was
  expected. `CORE_REPLY_FORMAT=markers` restores the M6 format for a build with no
  grammar support.
* **`answer` is read while it is still being written.** It is the first property in the
  schema and the grammar follows that order, so `reply.AnswerStream` pulls the text out
  of the token stream character by character. Waiting for the closing brace would add
  the whole generation to time-to-first-audio, which M4 already has at 2393 ms against a
  1500 ms target.

`language` is a field rather than the old `[lang:xx]` prefix. The first chunk still has
to be spoken before that field exists, so the script of what is being written routes it
and the declared value is reconciled at the end — it decides the *next* turn's hint.

### One decoder, and one place to choose it

`robot-asr` and `robot-whisper` are not alternatives, which is easy to misread from
their names. `robot-asr` always runs: it owns the microphone, the Silero endpointing and
the barge-in signal. What `ASR_ENGINE` changes is *who decodes* — Qwen3-ASR loaded inside
that process, or `robot-whisper` over loopback HTTP. One calls the other.

Exactly one decoder is ever resident, because two ASR models is about 2 GB on a board
with roughly one to spare. The rule is enforced in three places rather than trusted:

* choosing an ASR model applies the engine with it, and stops the decoder it does not
  use (`catalog.asr_services_for`, `app._apply_asr_choice`)
* `run_stack.sh spawn_whisper` refuses to start when the engine does not select it, so a
  stale unit, script or button cannot put a second model on the board
* `services.is_expected()` reports a decoder that is running when nothing reads it, and
  one that is missing when the selected model needs it — visible in the terminal and on
  the Services and Memory pages

Choosing itself happens in exactly one place. The Memory page prices a combination
against the ceiling and applies it: every changed value written, each affected component
restarted once, and the result reported from the orchestrator's own view of what is
configured rather than from what the page hoped for. Config offered the same choice
without the budget and without applying it, which is how a model sat chosen-but-never-
applied for a whole session while the robot ran something else.

### The microphone is shut, not ignored, while the robot talks

The speaker and the microphone are one USB device. Under `CORE_LISTEN_MODE=no_interrupt`
or `wake_word` the orchestrator sends `robot-asr` a `gate` message when audio starts and
another `listen_tail_ms` after it stops, and the ASR drops frames in between.

Dropping *frames* rather than *transcripts* is the whole point. An utterance that starts
during playback is still being decoded when playback ends — whisper large-v3-turbo takes
the best part of a second, and 650 ms of endpointing silence comes before that — so a
check made when the text arrives sees a quiet room and lets the echo through as a fresh
question. Closing the gate also resets the endpointer, so half of the robot's sentence
followed by half of yours cannot decode as one confused question.

Three things keep it honest: the gate is cancelled and re-closed if another chunk starts
before the tail elapses; `attach_asr` re-closes it when robot-asr reconnects mid-reply;
and the transcript-side check still refuses an utterance that *began* while muted, so a
lost `gate` message costs a wasted decode rather than a wrong answer.

### Who restarts what

Recovery is the supervisor's job, never the orchestrator's — `robot-core` holds the STM32
heartbeat and stays unprivileged. Which supervisor that is depends on the box: the system
units in `deploy/systemd/` in production, `systemctl --user` copies for testing without
root, or `scripts/run_stack.sh` in a development stack. `robot.common.services` is the one
place that knows the difference, and both the dashboard's Services page and the
orchestrator's scoped restarts go through it.

The Services page is answered by `robot-ui` from `/proc` rather than relayed through
`robot-core`, for the same reason the Monitor page is: the panel has to work when the
orchestrator is the process that is down. `robot-ui` still imports nothing from
`robot.core` — `robot.common` is shared by both, as `ipc` and `ws` already were.

---

## Memory discipline

- One LLM resident at a time. Unload before loading a replacement.
- Cap context and summarize old turns — unbounded history is a silent memory leak.
- Record measured RAM per component in `memory-budget.md` after each milestone.
- Watch thermals under MAXN SUPER: it is uncapped, so sustained load throttles without airflow.
