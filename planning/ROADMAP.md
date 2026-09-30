# ROADMAP.md

Build in this order. **Each milestone must actually run before starting the next.**
The ordering is deliberate: it front-loads the risky, memory-critical pieces and leaves the
comfortable ones (UI) for last, so a nasty surprise shows up on day 3 and not week 6.

---

## M0 — Board bring-up ✅

- [x] Flash JetPack 6.2 with `jetson-orin-nano-devkit-super.conf` — JetPack **6.2.1**, L4T 36.4.7
- [x] `sudo nvpmodel -m 2` → verify `jetson_release` shows `MAXN_SUPER` — active (`jetson_clocks` on)
- [x] Disable desktop: `sudo systemctl set-default multi-user.target`, reboot — already headless
- [x] Install `jetson-stats`, record **baseline idle RAM** in `memory-budget.md` — **~0.65 GB**
- [x] NVMe storage set up (models are several GB) — `models/` on `/dev/nvme0n1p16`, 260 GB free

**Exit:** headless board in Super Mode with a measured idle RAM number written down. ✅

Re-verify any time with `scripts/bringup/check_board.sh` (read-only, exits non-zero on a
regression). Source `scripts/bringup/env.sh` before any install — the **root filesystem is 94%
full**, so pip cache, `TMPDIR` and HuggingFace downloads are pinned to the DATA partition.

---

## M1 — Think (LLM alone) ✅

- [x] llama.cpp built or pulled from `jetson-containers` — **built from source**, CUDA, `sm_87`,
      in `vendor/llama.cpp`. Not a container: Docker images land on the 94%-full root fs
- [x] Qwen3-4B-Instruct Q4_K_M downloaded — `unsloth/Qwen3-4B-Instruct-2507-GGUF` (the official
      Qwen GGUF repo is gated). `scripts/models/fetch_llm.sh`
- [x] `llama-server` running with 2–4k context, OpenAI-compatible endpoint on :8080 — 4096 ctx,
      all layers on GPU, `robot-llm.service`
- [x] `curl` a prompt in **English, Bahasa Indonesia, and Arabic** — verify all three reply well
      — `scripts/measure/llm_smoke.py`. EN and ID good; **AR fluent but factually weaker**, see below
- [x] Measure tokens/sec and RAM; record both — **17.9 tok/s** generation, 334 tok/s prompt eval,
      **3728 MiB** resident

**Exit:** a working local LLM answering in all three languages. ✅

**Two findings worth knowing before M4:**

*Throughput is ~half the documented reference.* 17.9 tok/s against TECH_STACK's "35–43 tok/s
for a comparable 3–4B model". At 2.4 GB of weights per token that is ~43 GB/s of the 102 GB/s
DRAM bandwidth, so it is plausibly bandwidth contention with the dev session. It does **not**
threaten the latency budget — 17.9 tok/s is still ~5× faster than speech — but re-measure on a
clean boot before drawing conclusions.

*Arabic is the weak language.* All three are fluent, but on a technical question Arabic
returned a confidently wrong answer (mecanum wheels described as being for balance on uneven
ground) where English and Indonesian were correct. Quality, not capability — worth a persona/
prompt pass in M4 and worth re-checking if Arabic answers seem off later.

---

## M2 — Speak ✅

- [x] Piper installed, all three voices downloaded — `piper-tts` 1.7.0 (base install pulls only
      `onnxruntime` + `pathvalidate`; `torch`/`transformers` stay behind extras we don't install,
      so constraint 4 holds). `scripts/models/fetch_tts.sh`
- [x] Synthesize a test sentence in each language — all three produce valid 22050 Hz mono audio at
      **RTF 0.24–0.34** on CPU. ⚠️ **Still not listened to by a human** — needs your ears
- [x] Confirm audio output device works (USB soundcard config, sample rate) — a USB soundcard
      (`cafe:4002 Audio Tools`, card `Audio`) was attached mid-session. 2.91 s of audio plays in
      3.03 s wall-clock, `aplay` exit 0 → real-time to real hardware. Mixer healthy (playback 80%,
      mic 100%, both unmuted). `TTS_ALSA_DEVICE=plughw:CARD=Audio,DEV=0`
- [x] Wrap as a service with a simple text-in → audio-out interface — `robot-tts.service`,
      NDJSON over a Unix socket, `scripts/measure/tts_smoke.py`

**Exit:** the box talks, intelligibly, in three languages. ✅ **Met**, with one human step left:
nobody has *listened* yet. Run `python scripts/measure/tts_smoke.py` with `robot-tts` running,
judge the Indonesian voice quality by ear, and decide whether Option B (MagpieTTS) is ever needed.

Measured first-audio latency (synthesis only, `null` sink): **EN 167 ms, ID 209 ms, AR 407 ms**
against the ~200 ms target in `system-overview.md`. Arabic is slower because Piper runs tashkeel
diacritisation on it. Real playback will add ALSA buffer latency on top.

---

## M3 — Listen ⚠️ (one decision, one dependency question)

- [x] sherpa-onnx + Qwen3-ASR-0.6B running — `sherpa-onnx` 1.13.7 has
      `OfflineRecognizer.from_qwen3_asr`; model from `csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25`
      (csukuangfj is the sherpa-onnx maintainer). `scripts/models/fetch_asr.sh`
- [x] ~~Streaming~~ **segmented** transcription from the USB mic — the service opens the USB
      capture device and runs. Recognition itself was verified by replaying Piper-synthesised
      audio through the real service (word-perfect in all three languages); **nobody has spoken
      into the mic yet**. **There is no streaming path** — see below
- [ ] **Verify the language tag is correct** for all three languages — ❌ **there is no language
      tag.** `result.lang` is `''` for every utterance. See below — this needs your decision
- [x] Silero VAD endpointing tuned (~600–700 ms silence) — 650 ms, segments a 3-utterance
      recording correctly, +23 MiB. **30 s on the live mic in a quiet room: zero false accepts**
- [ ] openWakeWord with a custom wake phrase — ⏸ **stopped to ask.** `pip install openwakeword`
      pulls `scipy`, `scikit-learn` and `tflite-runtime` (a second ML runtime), far beyond the
      "~50 MB" in TECH_STACK. Options in the note below

**Exit:** speak in any of the three languages, get correct text plus the right language tag.
**Text: met.** Transcription is word-perfect in all three languages, including Arabic and
Indonesian — the model choice is validated. **Tag: not met, and not achievable as designed.**

### ❌ Qwen3-ASR does not emit a language tag

`system-overview.md` § Language routing is built on "the ASR emits a language tag per utterance
… that tag drives everything downstream", and `TECH_STACK.md` cites it as the reason Qwen3-ASR
was chosen. Measured: `result.lang` is `''` on all three languages, as are `emotion` and
`event`. Those fields are populated by SenseVoice, not by Qwen3-ASR. The decoded tokens are
plain text with no language marker.

`robot.asr.recognizer.script_of()` is an interim stopgap: it separates Arabic from Latin script
reliably, and **cannot separate English from Indonesian at all** — they share a script.

Three ways forward, none of them free (this is an architecture change, so it is your call):

1. **Let the LLM declare the language.** It is already resident and multilingual, and the system
   prompt already says "reply in the language the user just spoke". Have it prefix its reply with
   a tag, strip that before TTS. Costs no RAM and no extra model; the voice can only be chosen
   once the first tokens exist, which the streaming design already waits for anyway.
2. **Add a small text language-ID step.** Accurate and immediate, but it is a new dependency
   against a budget that no longer has room.
3. **Switch the ASR model.** Whisper does return a language, but it is heavier, its language ID
   is per-window, and re-opening the model choice re-opens the Indonesian question that ruled
   out Nemotron in the first place.

**Recommendation: option 1.** It fits the existing design, adds nothing to the budget, and the
standing "reply in the user's language" instruction already does the hard part.

### ⚠️ It is offline, not streaming — and decode is not free

sherpa-onnx exposes Qwen3-ASR only on `OfflineRecognizer`; there is no `from_qwen3_asr` on
`OnlineRecognizer`. So `system-overview.md` step 3 ("streams partial transcripts") cannot happen:
VAD closes an utterance, then the whole thing decodes. There are no partial transcripts to show
in the UI.

### ⚠️ It hallucinates confidently on noise

Fed 6 s of near-silence (a failed acoustic loopback — the speaker and mic barely couple), the
recogniser returned fluent Chinese: `为什么他把刀子扔了？他去杀人了。` Not a transcription of
anything; an invention.

VAD normally prevents this by only handing over real speech, and it did not false-trigger in
30 s of room tone. But a cough, a door, or a burst of music **will** open a segment, and the ASR
will then invent words the robot answers as if they were said. **M4 needs a gate before the
transcript reaches the LLM** — an RMS floor on the segment, a check on `result.ys_log_probs`,
or both.

### Decode cost

Measured decode: **RTF 0.40–0.55** — 1.38 s for 2.4 s of speech, 2.01 s for 4.5 s. CUDA gives no
speedup (int8 ops fall back to CPU) and neither do more threads. The latency budget never
allotted anything for decode, so end-of-speech → first audio is realistically
**~1.3 s (ASR) + 0.1 (retrieval) + 0.4 (LLM) + 0.2 (TTS) ≈ 2.0 s**, against a 1.5 s target.
Confirm this against a real microphone in M4 before optimising.

---

## M4 — First full loop ✅ (works; latency target missed)

- [x] asyncio orchestrator wiring M1–M3 together — `robot-core`, `src/robot/core/`.
      Reconnects to `robot-asr`/`robot-tts` rather than dying with them
- [x] Language routing table implemented — **the M3 blocker is resolved**: the LLM declares
      its own language via a `[lang:xx]` prefix, cross-checked against the reply's script.
      **3/3 turns resolved via `declared`** across an EN/ID/AR conversation
- [x] **Sentence-boundary chunking** — first sentence goes to TTS while the rest generates;
      2 chunks per typical reply. The first chunk is capped at 90 characters so a long
      opening sentence cannot delay all audio
- [x] **Barge-in** — verified: robot speaking at 3480 ms, user speaks, playback and
      generation both stop, the interrupting utterance gets its own complete reply
- [x] Measure ~~wake →~~ **end-of-speech →** first-audio latency — **2393 ms mean**
      (ID 1722 / EN 2205 / AR 3252) against a 1500 ms target. ⚠️ **Missed by ~0.9 s**
- [x] Measure peak RAM with all three models loaded simultaneously — **5274 MiB robot-only**,
      sampled 132× through a live conversation. Budget closes; see `memory-budget.md`

**Exit:** a real conversation, in three languages, interruptible. ✅ **Met.**

There is no wake word (M3, blocked), so the loop is currently always-listening: any
endpointed utterance starts a turn. Once a wake word exists it gates the IDLE→LISTEN edge
and nothing else changes.

### ⚠️ The latency target is missed, and not for a fixable reason

| Stage | Budget | Measured |
|---|---|---|
| ASR decode | *not budgeted* | **805 ms** |
| → first token | — | 228 ms |
| → first sentence | 300–500 ms | **943 ms** |
| → first audio | ~200 ms | 415 ms |
| **Total** | **1500 ms** | **2393 ms** |

Both overruns are structural, not buffering bugs:

- **ASR decode cannot be overlapped.** Qwen3-ASR is offline; there is no transcript until
  decoding finishes, so nothing downstream can start earlier.
- **Generation runs at 17.9 tok/s, not the 35–43 tok/s the budget assumed.** The chunker
  already emits at the earliest legal point. The tokens are not there any sooner.

Three honest options: accept ~2.4 s, move to a smaller/faster ASR (which reopens the
Indonesian-coverage question that ruled out Nemotron), or re-measure on a clean boot without
the dev session — every run above touched swap because of it. **Recommend the clean-boot
re-measure first**, since it is free and the numbers may improve.

### Two real bugs found by running it

**PulseAudio owns the USB card.** Opening `plughw:` directly worked for the first utterance
and then failed with `Device or resource busy` on every reopen after a barge-in — PulseAudio
re-acquires the PCM the moment `aplay` releases it. Both configs now route through `pulse`,
which also makes full-duplex PulseAudio's problem rather than ours, and is where
`module-echo-cancel` would sit if barge-in ever needs AEC.

**Cancelling playback looked like a device failure.** Killing `aplay` breaks the pipe the
synthesiser is writing into, which was reported to the orchestrator as an audio error on
*every* interruption. Barge-in now stops the player without that false signal.

---

## M5 — Body (STM32) ⚠️ (both sides written and tested; **no STM32 exists to run it on**)

- [x] STM32 firmware: serial line parser per `serial-protocol.md` — `firmware/stm32/`,
      PlatformIO. Every documented command and response implemented
- [x] Mecanum inverse kinematics + per-wheel PID — `mecanum.c`, `pid.c`, both portable and
      unit-tested (anti-windup included). ❌ **Wheel directions NOT verified** — see below
- [x] LED matrix face expressions — six faces over I2C. ⚠️ never run on hardware
- [x] Servo gesture presets — `wave`, `point`, `home`, `nod`. ⚠️ never run on hardware
- [x] **Heartbeat watchdog: 500 ms timeout → motors zeroed by the STM32 itself** —
      `watchdog.c`, portable and unit-tested
- [x] Test the watchdog by killing the Jetson process mid-drive — **PASSES**, see below
- [x] Jetson side: `pyserial` writer + 200 ms `PING` + response handling —
      `src/robot/core/serial_link.py`, wired into `robot-core`

**Exit:** commands typed by hand into a serial terminal move the robot correctly, and pulling
the plug on the brain stops the wheels. **Half-met.** The second half passes against the
firmware core. The first half cannot be met without a microcontroller and motors.

### ✅ The watchdog test passes

```
[1] firmware core running, serial port /dev/pts/0
[2] Jetson-side process started, pid 260944 (heartbeat every 200 ms)
[3] wheels TURNING after 152 ms: FL=10.42 FR=10.42 RL=10.42 RR=10.42
[4] SIGKILL -> pid 260944  (no cleanup, no STOP command)
    confirmed dead, exit -9 (-9 = killed)
[5] wheels STOPPED 509 ms after the kill
    FL=0.00 FR=0.00 RL=0.00 RR=0.00
[6] still stopped 1 s later; motion did not auto-resume

PASS: the STM32 stopped the motors by itself.
```

`SIGKILL` cannot be caught, so nothing on the Jetson participated in stopping the robot.
Reproduce with `python scripts/serial/watchdog_test.py --sim`.

**What this does and does not prove.** The firmware core — `protocol.c`, `watchdog.c`,
`mecanum.c`, `robot.c` — is compiled for the host by `tools/stm32_sim` and driven over a
real serial device (a PTY) by the real `SerialLink`. So the code that stopped those wheels
is the code that will ship. What it does not prove is that PWM actually reaches zero on a
real driver: that is `board_stm32.cpp`, and it has never run. **Re-run this on hardware
with `--port` before trusting the robot near anything breakable.**

### ❌ Wheel directions are NOT verified — no hardware

The protocol doc asks for each wheel to be checked individually before composite motion is
trusted. That cannot be faked: it requires watching a wheel turn.

What exists instead:

- **A new `WHEEL <index> <rad_s> <ms>` command**, added to `serial-protocol.md` first per
  that doc's own extension procedure. It bypasses the kinematics to spin one motor —
  necessary because mecanum maps 3 body velocities onto 4 wheels, so no `DRIVE` can isolate
  one. It is deliberately **not** exposed as an LLM tool.
- **`scripts/serial/verify_wheels.py`** — drives each wheel in turn, asks what you saw, and
  prints both the `direction[]` values and the doc table to fill in.
- **A marked-unverified table** in `serial-protocol.md` § Verifying wheel direction. The
  firmware currently assumes `{+1, +1, +1, +1}`, which for mirror-mounted motors is
  *unlikely to be right* — that is recorded as an assumption, not written up as a result.

### Also verified without hardware

- **628 native checks** on the portable core (`firmware/stm32/run_native_tests.sh`) — plain
  gcc, no PlatformIO, no board. Covers watchdog timing to the millisecond, counter wrap,
  "no auto-resume", every documented command and rejection, and PID anti-windup.
- **20/20 protocol conformance** across a real serial device
  (`scripts/serial/protocol_test.py --sim`), driving the Python client against the C
  firmware: both implementations of the contract agree.
- **The firmware builds for ARM** — 6.1 KB RAM (4.7%), 61 KB flash (11.6%) on an F411CE.

---

## M6 — Voice → motion ✅

- [x] LLM tool definitions: `drive`, `stop`, `set_face`, `gesture`, `save_memory` —
      `src/robot/core/tools.py`
- [x] Tool-call parsing → clamped values → serial write — clamped in three places
      (orchestrator, `SerialLink`, STM32), because the first input is an LLM
- [x] Speech and motion happen **in parallel**, not sequentially — measured: audio at
      2952 ms, tool at 3397 ms, with 1.71 s of speech playing. The robot talks while it moves
- [x] `ERR` / stall responses surfaced back into the conversation — a refusal, a stall or a
      dropped link becomes a note in the next turn's prompt, so the robot can say it did not
      move instead of narrating a drive that never happened

**Exit:** "move forward a little" makes the robot move forward a little, in all three
languages. ✅ **Met** — all three sent `DRIVE 0.200 0.000 0.000 1000` to the body.

### Inline markers, not the OpenAI tools API

llama.cpp supports function calling, but it returns tool calls *instead of* content, in
their own turn — which would make the robot decide, then act, then speak. The checklist
above requires the opposite. So the model writes markers inside its reply:

    [lang:en] Sure, moving forward now. [do:drive vx=0.2 ms=1000]

`ToolParser` strips them before a character reaches TTS and fires each one immediately,
without awaiting — awaiting would stall the token stream behind a serial round trip.

### Two prompt bugs found by running it

**The model moved in silence.** Asked to move, Qwen3-4B emitted `[do:drive vx=0.35 ms=1500]`
and nothing else — a marker is silent, so the robot moved without a word. Fixed by making
"a marker is not a reply" explicit, with worked examples.

**`[lang:in]`, not `[lang:id]`.** The model used the deprecated ISO 639-1 code for
Indonesian (still what the JDK emits, so it is all over the training data). Asking more
firmly helps; the actual fix is an alias table in `robot.core.language`.

---

## M7 — Ingest ✅ (works; memory is now the constraint)

- [x] Qwen3-Embedding-0.6B + sqlite-vec — `robot-embed.service` (a second `llama-server`,
      CPU-only) and `src/robot/core/rag.py`
- [x] Chunk → embed → upsert pipeline (background job, off the hot path) — `Ingestor` owns
      a queue and a worker; `save_memory()` and file ingestion both return immediately
- [x] Retrieval wired into the prompt assembly — top-k chunks injected before the language
      rules, see below for why the ordering matters
- [x] `save_memory()` tool so spoken facts get stored — `[do:remember ...]` → the store
- [x] **Test cross-lingual retrieval**: ingest an English doc, ask about it in Indonesian —
      **8/8 queries retrieved the right chunk, 5/5 of them cross-lingual**

**Exit:** the robot answers using a document ingested after boot. ✅ **Met — 3/3**, spoken
aloud, in the language of the question, from a document ingested into the running robot.

### Two prompt-ordering bugs, both found by measurement

**English notes dragged the reply into the wrong language.** With an English chunk in the
prompt, an Arabic question came back in Indonesian. Retrieved notes sit close to the user's
turn and act like an example of how to answer. Fixed by putting the notes *before* the
language rules and saying explicitly that their language means nothing. Verified 9/9.

**The declaration rule had to move last.** Buried mid-prompt alongside the notes and the
tool rules, the model dropped the `[lang:xx]` tag on turns where it was calling a tool —
the reply was in the right language but nothing downstream knew, so it would have got the
wrong voice. Moving it to the end of the system prompt: 12/12.

### And one real bug in the orchestrator

**History was storing the stripped reply.** The model saw its own past turns with no
`[lang:xx]` tag and no `[do:...]` markers, concluded that was the format, and started
copying it — a movement request in Indonesian came back verbatim in English with no marker
and no movement. History now stores the raw reply. Movement requests went from **1/3 to
3/3** on that fix alone.

### Residual: Arabic

Across the final combined run: **language 5/6, grounded 3/3, movement 3/3.** The one miss
was an Arabic movement request answered in English. This is the same Arabic weakness
recorded in M1 — all three languages are fluent, but Arabic is the one that slips first
under a long prompt.

### ⚠️ The stack no longer fits alongside a dev session

Adding the embedding server pushed peak use to **7368 MiB (97%) with 3204 MiB of swap**
while VS Code Server and Claude Code were attached. It still fits on a production boot
(~6.5 GB of 7.6 GB), but there is no longer room to develop on the box while the full
stack runs. See `memory-budget.md`.

---

## M8 — UI ✅ (rebuilt as hand-written pages)

- [x] **Chat** — typed input, live transcript, detected language (and *how* it was decided),
      streaming reply, and the **literal line sent to the STM32** (`→ DRIVE 0.200 0.000
      0.000 1000`). The orchestrator reports that line itself rather than the dashboard
      rebuilding it from the tool call, so clamping and rounding are visible as they
      really happened. Refusals and stalls appear inline
- [x] **Health** — one row per service (wake word, ASR, LLM, TTS, embeddings, serial):
      state, last heartbeat, last error, on the home page and beside the restart buttons.
      Wake word shows as *absent*, not *failed*
- [x] **Headless** — the physical loop: conversation on the left, a live **activity log**
      on the right. One timestamped, colour-coded entry per pipeline stage (hearing,
      transcribing, looking through what it knows, thinking, speaking), plus every serial
      line, every refusal, and every service state *change*. Consecutive identical states
      collapse, so a lagging link does not fill the log with repeats
- [x] **Memory monitor** — live feed, per-component bars, swap warning. Drawn against the
      **real 7620 MiB ceiling, not 8192** — the difference is Tegra carveout, and drawing
      against 8 GB would flatter every number by ~7%
- [x] **Config** — two groups, driven by one registry (`robot.core.settings`)
- [x] Session auth enabled — the service **refuses to start** without `ROBOT_UI_PASSWORD`
      rather than serving an open page that can drive a robot

**Exit:** everything tunable from a browser without SSH. ✅ **Met.**

### Rebuilt to match the sibling project

The first version used NiceGUI's own components, which look like Quasar rather than like
the rest of the system. The pages are now hand-written HTML/CSS/JS sharing the design
system of `/media/jetson/DATA/robot` — same tokens, same nav, same card and badge
language — served from `src/robot/ui/static/`.

NiceGUI is still the server, as TECH_STACK.md specifies: it brings FastAPI, uvicorn and
the session middleware. What changed is that it no longer renders the content.

The pages are **Home, Chat, Headless, Knowledge, Memory, Monitor, Logs, Config**.

**Logs** streams the orchestrator's own records over the same websocket and tails every
other service from its file or the journal, with level/text filters and a live level
switch — `DEBUG` surfaces the model's raw reply. **Config → Listening** carries a live
microphone meter (level, peak, noise floor, and whether the VAD is triggering) so the
threshold can be tuned in the room it will actually run in. **Config → Speaking** has a
per-voice preview button and a speaker volume applied to the audio itself rather than a
mixer control. **Knowledge** accepts dropped documents, and reports whether ingestion
actually succeeded rather than leaving "ingesting" on screen for ever. The browser never
reaches `robot-core`: it holds a session cookie to `robot-ui`, which holds one token-
authenticated websocket to the orchestrator and fans events out to every open tab. Ten
dashboards therefore cost the robot one connection.

### Memory: price a selection before applying it

The board has ~1 GB of headroom, so "which model should I load" is a question with a
real answer. The **Memory** page answers it: pick an LLM, an ASR, a context size and the
voices, and it shows the projected steady state and mid-turn peak against the **real
7620 MiB ceiling** — as a stacked allocation bar, a line-item table, and a comparison
with what is actually in use right now. Nothing is applied until you press Apply.

`robot.core.catalog` scans what is installed and prices each entry from the measurements
in `memory-budget.md`. For GGUF models the KV cache is **read from the model's own header**
(`2 × layers × kv_heads × head_dim × 2 B`) rather than scaled from the file size, which
reproduces the 144.0 KiB/token measured for Qwen3-4B in M1 and stays right for a model
nobody has measured. Checked against the milestones: Qwen3-4B **3420 est / 3391 measured**,
ASR **1243 / 1246**, embeddings **634 / 633**.

What the choice costs, full stack:

| Selection | Peak | Headroom |
|---|---|---|
| Qwen3-0.6B @ 2048 | 4544 MiB | 3076 MiB |
| Qwen3-1.7B @ 2048 | 5222 MiB | 2398 MiB |
| Qwen3-4B @ 2048 | 6612 MiB | 1008 MiB |
| Qwen3-4B @ 8192 | 7476 MiB | 144 MiB ⚠️ |
| Qwen3-4B + the larger ASR-1.7B | 8396 MiB | **over by 776 MiB** |

That last row is the point: the two best models installed cannot both be loaded, and the
page says so before a service fails to start rather than after.

Model fields in Config became real dropdowns, filled from the same catalogue — so the
registry stays the single source of truth for a setting's group and kind, while what is
*installed* is discovered rather than hard-coded.

### The two config groups are structurally honest

The brief asks that hot-reload settings apply live and that scoped-restart settings get
their own button — and that a hot-reload control never silently need a restart. That is
enforced by construction rather than by care: `robot.core.settings.REGISTRY` declares each
setting's group once, the dashboard *renders* both groups from that declaration, and the
orchestrator *dispatches* from the same one. Putting a control in the wrong group would
have to be done in the registry, where it changes both behaviours together.

Hot-reload genuinely reaches the owning process: VAD threshold and endpointing go to
`robot-asr` (which rebuilds its endpointer live), the voice map goes to `robot-tts` (which
refuses a voice whose files are missing rather than failing mid-utterance). Editing a
scoped-restart field **stages** the value and says so; only its button applies it.

Each restart button restarts exactly one thing and flips that one light amber while it
recovers. `serial` is handled in-process — reconnecting the link *is* the scoped restart,
where restarting `robot-core` would drop the conversation and the heartbeat. The other
buttons shell out to `systemctl restart` via a scoped sudoers rule
(`scripts/bringup/setup_sudoers.sh`); without it they report exactly why they could not,
rather than pretending.

### The robot does not depend on the dashboard

Verified by killing it: `SIGKILL` on `robot-ui`, then the robot still answered, still
executed `DRIVE 0.200 0.000 0.000 1000`, and the heartbeat to the body never stopped
(223 → 275 pings across the kill). `robot-ui.service` is deliberately not `Requires=` of
anything, imports nothing from `robot.core`, and reconnects on its own.

The link is a WebSocket that `robot-core` serves itself, hand-rolled on the stdlib so the
process holding the STM32 heartbeat gains no dependency. It is tested against aiohttp's
client rather than a matching hand-rolled one, so protocol mistakes actually surface.
Loopback-only and token-gated, so reaching the port is not enough to drive the robot.

### Measured

`robot-ui` costs **60 MiB** against a ~150 MB estimate. **35/35 dashboard checks pass**
(`scripts/measure/ui_smoke.py`), driving it the way a browser does — form login, then the
same session cookie for the pages, the metrics endpoint and the websocket.

The Monitor page samples the board itself (`/api/monitor`: memory, swap, per-core CPU,
GPU load and clock, INA3221 power rails, thermal zones, per-service RSS with a trend
column) rather than asking the orchestrator, so a page left open costs the robot nothing.
Memory is drawn against the **real 7620 MiB ceiling**.

---

## M9 — Hardening ⚠️ (all drills pass; the 24-hour soak still needs a clean-boot run)

- [x] systemd units for every service, `Restart=on-failure` — six units plus `robot.target`;
      `check_board.sh` now verifies the declaration so it cannot quietly regress
- [x] All failure paths from `system-overview.md` § Failure design tested by hand —
      **24/24**, `scripts/hardening/failure_drills.py`
- [~] 24-hour soak — **45 minutes run clean** after three fixes; the full 24 h has not
      been run, and needs a box with no dev session attached
- [x] Thermals verified under sustained MAXN SUPER load — **peak 71.8 °C**, no throttling
- [x] `memory-budget.md` **Measured** column fully filled in

**Exit:** you can leave it running and walk away. **Not yet** — see the soak caveat below.

### Failure drills: 24/24

Every row of the failure-design table, exercised by killing something for real:

| Failure | Result |
|---|---|
| Orchestrator killed mid-drive | Motors zeroed by the STM32 in ~500 ms, on its own authority. systemd restarted core. Motion did **not** auto-resume |
| LLM killed | Health went down, the canned "give me a moment" line was spoken, the turn completed, systemd restarted it and it served again |
| TTS killed | The reply still reached the dashboard with no speaker, and the turn completed |
| ASR killed | The orchestrator survived, typed input kept working, and it reconnected by itself |
| Serial disconnected | Reported in health, the turn completed rather than hanging, and the link came back on its own |
| Model too large | Refused to start, logged as an error, with the size it could not allocate |

Reproduce: `python scripts/hardening/failure_drills.py` (add `--user` where there is no root).

### Four real bugs the drills found

**`Restart=on-failure` did not work for the LLM.** A killed `llama-server` failed to
restart *five times* and then gave up: the model it had just died with was still in page
cache, and CUDA on Tegra will not force reclaim. The restart policy was worthless exactly
when it was needed. Fixed with `ExecStartPre=` dropping that model's cache — recovery is
now ~45 s.

**A dead speaker lost the reply.** A send to `robot-tts` raised, killing the turn task, so
the reply was never broadcast and `turn_done` never fired. The doc requires the reply to
appear anyway. Fixed.

**`robot-tts` kept a single peer reference.** The dashboard's hot-reload opens a
short-lived connection to change the voice map; it replaced the orchestrator's connection
and then closed, so every later `started`/`finished` went nowhere and **every turn hung**.
Now it broadcasts to all peers.

**Nothing bounded a turn.** Playback, synthesis and writes are each bounded now, but a turn
ends when the speaker reports back — so any unreported failure downstream hung it silently
(observed: 4.5 minutes). `Orchestrator._turn_deadline` force-completes at 90 s and says so.

### Soak: 45 minutes, 313 turns, 0 errors

| | |
|---|---|
| Peak memory | 6708 MiB of 7620 |
| Peak swap | 310 MiB (pre-existing, flat) |
| Peak temperature | **71.8 °C** — trip points are passive@99 °C, critical@104 °C |
| OOM kills | 0 |
| Turns | 313, median 7.4 s, **0 timed out**, 0 link errors |
| Language routing | **313/313 correct** |

**Two of the three initial FAIL flags were my own measurement bugs**, both fixed:
"thermally throttled in 134/270 samples" counted a *low* passive trip at 70 °C that merely
asks the cooling governor to engage — real throttling is 27 °C away; and "stuck motors in 5
samples" counted isolated samples that caught the soak's own `DRIVE` commands mid-flight,
never consecutive. The check now requires continuous motion beyond the protocol's 10 s cap.

### ⚠️ Open: robot-tts grows

The one real finding. Over 45 minutes `robot-tts` went **342 → 889 MiB** while every other
service stayed flat (LLM −11, ASR −5, core −1, UI ±0). The growth decelerates sharply
(1193 → 385 → 217 MiB/hour across thirds). Isolating it:

* **Piper itself does not leak.** Driven directly, 150 syntheses across all three voices:
  301 MiB with the voices loaded, 503 MiB after 30, and then **exactly 505 MiB at 60, 90,
  120 and 150** — onnxruntime's arena reaches its high-water mark and stops.
* **The remainder is in `robot-tts`.** Driven turn-by-turn through its socket it reached
  606 MiB at 120 turns and 649 MiB after several hundred, with thread and file-descriptor
  counts flat — so it is allocator growth, not leaked handles, and it is still converging
  rather than climbing linearly.

So the bulk (~505 MiB) is a bounded arena, and roughly 100–150 MiB on top had not settled
within the runs done here. The budget only has ~1 GB spare.

Mitigated, not solved: `robot-tts.service` now carries `MemoryMax=1200M` / `MemoryHigh=1000M`,
so a runaway speaker is restarted instead of the robot being OOM-killed. Restarting TTS
costs ~1 s and drill 3 proves the reply survives a dead speaker.

**This is what the 24-hour run has to settle.** If TTS plateaus below 1 GB, nothing more is
needed; if it keeps climbing, the arena has to be capped at the onnxruntime session level.

### Before leaving it running

```bash
sudo ./scripts/install_services.sh
sudo ./scripts/bringup/setup_logging.sh   # journald is VOLATILE by default here --
                                          # an overnight failure would leave no evidence
sudo ./scripts/bringup/setup_sudoers.sh   # the dashboard's restart buttons
python scripts/hardening/failure_drills.py
python scripts/hardening/soak.py --minutes 1440
```

---

## Deliberately deferred

- MagpieTTS upgrade for EN/AR (only if Piper's quality proves insufficient)
- JetPack 7.2 migration (once the ecosystem catches up)
- Vision / VLM
- Navigation, SLAM (would also be the point to reconsider ROS 2)
