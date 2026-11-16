# PROGRESS.md — what is done, what changed, what is next

**This is the tracker.** `START_HERE.md` is the map, `AGENTS.md` is the contract,
`TROUBLESHOOTING.md` is the bug log, `ROADMAP.md` is the original build order. This file is
the one that answers *"where are we right now?"* — keep it current in the same change as
the work (AGENTS.md working rule 5).

> **Last updated: 2026-11-16** · Python tests **1156 passing** (`python -m pytest tests/ -q`; counted 2026-11-11, only docs have changed since)
> · firmware core **634 checks, 0 failures** (`./firmware/stm32/run_native_tests.sh`; re-run 2026-11-16).

## How to keep this file honest

| When you… | …do this here |
|---|---|
| finish a piece of work | add a dated entry to **Changelog** (newest first): what, why, files, how it was verified |
| find a bug you are not fixing now | add it to **Known issues** with the symptom and why it was left |
| are asked for / think of a feature | add a row to **Backlog**; move it to the Changelog when it ships |
| change a hard constraint, dependency or the architecture | **stop and ask** (AGENTS.md), then record the decision under **Decisions** |
| fix a bug | also add the symptom → cause → fix to `TROUBLESHOOTING.md`, and a regression test |

Prefer *measured* over *believed*: say how a claim was checked ("verified with
`systemd-run -p EnvironmentFile=`", "fails on the old code, passes on the new").

---

## 1. Status snapshot

| Area | State | Notes |
|---|---|---|
| Board bring-up (M0) | ✅ | JetPack 6.2.1, MAXN_SUPER, headless, models on the DATA partition |
| Think — LLM (M1) | ✅ | llama.cpp, grammar-constrained JSON reply, `CORE_MAX_TOKENS=auto` |
| Listen — whisper.cpp on the GPU (M3) | ✅ | one engine, chosen by SIZE; level + confidence gates; language filter (fixed 2026-10-22) |
| Speak — Supertonic on the GPU (M2) | ✅ | one voice for three languages; pre-rendered acknowledgements |
| Full loop (M4) | ✅ | median **2181 ms** end-of-speech → first word (target 1500 ms), best 1554 ms |
| Body / STM32 (M5) | ⚠️ | firmware + Jetson side done and sim-tested; **no microcontroller attached; wheel directions unverified** |
| Voice → motion (M6) | ✅ | reply fields → bounded `DRIVE`/`HAND`/`FACE` |
| Knowledge (M7) | ✅ | sqlite-vec + FTS5; `save_memory` **repaired 2026-10-22** (was silently dead) |
| Dashboard (M8) | ✅ | nine pages, session auth, hand-written HTML/JS |
| Hardening (M9) | ⚠️ | 24/24 failure drills, 45-min soak clean; **the 24 h soak has not been run** |
| On-robot panel (M11) | ✅ | Rust/Slint to DRM/KMS, 11 MiB; no input device attached yet |
| See — camera, objects, faces (M12) | ✅ (not yet with a live person) | IMX219 on CSI; YOLOX-Tiny + YuNet + SFace on the CPU; **~213 MiB, ~0.27 core idle**; recognises across different photographs; reaches the LLM as one sentence; Vision page. `dev-docs/modules/vision.md` |
| People — ask a stranger's name, remember the conversation, greet and resume (M13) | ✅ (not yet with a live person) | one **Talk** page replaces Chat/Voice/Vision; conversations filed per recognised face in `run/people.db`; 0 MiB, no new process or dependency; verified through the real orchestrator and against the real LLM with a scripted camera. `dev-docs/modules/people.md` |
| Production (systemd) path | ⚠️ | units **never installed on this box**; four faults found and fixed by reading them against the dev path (2026-10-22) — still needs a real boot, see Known issues |
| Wake word | ✅ (by transcript) | `CORE_LISTEN_MODE=wake_word`; no keyword-spotter model by design |

Currently configured on this robot (read from `config/` and `run/settings.json`, not
assumed): LLM **Qwen3-0.6B Q4_K_M** · ASR **whisper large-v3-turbo q5_0** (GPU) · TTS
**Supertonic F1** (GPU, 4 steps) · listen mode **no_interrupt**, tail 100 ms · filler **on**.
See Known issues #1 about that LLM.

---

## 2. Changelog (newest first)

### 2026-11-16 — Handbook chapters 03, 05, 07 and 08

Continued the illustrated handbook (see 2026-11-12). Written, in English, in `docs/en/`:

* **03 Firmware** (`03-firmware/`): what the body is and the portable-core / board split, the
  watchdog, motion (mecanum formulas, limits, the wheel-sign check), trying the body without a
  board, and **what an ESP32 firmware must do** (a 17-row conformance checklist). It documents the
  reference firmware: the ESP32's is not in the repo.
* **05 Interfaces** (`05-interfaces/`): a map of every wire, the serial protocol, the reply JSON,
  the WebSocket and local sockets, and a configuration reference whose table of the 32 live settings
  was **generated from `settings.py`**.
* **07 Operations** (`07-operations/`): installing, running day to day, the systemd units, and a
  symptom index into `TROUBLESHOOTING.md` (74 quoted search phrases, each checked to exist there).
* **08 Decisions** (`08-decisions/`): the hard constraints with their evidence, rejected and deferred
  alternatives, and everything that is waiting for the owner.
* New hand-editable picture `docs/assets/diagrams/mecanum.svg`; 14 more Mermaid blocks. The two
  front doors (`docs/en/README.md`, `docs/id/README.md`) list the new chapters.
* **A site build for the handbook.** `mkdocs.yml`, `requirements-docs.txt`, `scripts/docs/mkdocs_hooks.py` (rewrites
  links that leave `docs/` into GitHub URLs, so the Markdown stays unchanged), `.github/workflows/docs.yml` (GitHub Pages) and
  `.readthedocs.yaml`. `mkdocs build` is strict and passes: 31 pages, 24 Mermaid blocks, 75 links rewritten, no warnings. Committed as `84c82d8` and pushed to `origin master` at the owner's request, together with the three files the handbook links to that
  were untracked (`dev-docs/ai/PROGRESS.md`, `dev-docs/modules/people.md`, `vision.md`). On GitHub the CI steps `pip install` and
  `mkdocs build --strict` **pass**; the deploy fails at `configure-pages` only because **GitHub Pages is not enabled yet**: Settings -> Pages ->
  Source: GitHub Actions, then re-run the `Handbook` workflow. The repository is **public**, so the handbook and those three dev docs are public.

**Found while writing, not fixed** (all recorded under Known issues): #18 widened (the hand console
and one tool alias use the same opposite convention), **#20** (nothing ever sends `STATUS`) and
**#21** (`watchdog_test.py --port` cannot observe a real board). Also noticed, in the old docs and
not edited: Known issue #1 and the "Currently configured" line still say Qwen3-0.6B, whisper q5_0 and
voice F1 (the board runs LFM2.5-2.6B, q8_0 and M1); 628 firmware checks in three docs (it is 634);
the root filesystem is at 96%, not 94%. `docs/en/08-decisions/open-decisions.md` § D lists them.

**How it was checked (2026-11-16).** `run_native_tests.sh` 634 checks, 0 failures; `protocol_test.py
--sim` 20 passed, 0 failed; `watchdog_test.py --sim` PASS (wheels stopped 499 ms after `SIGKILL`);
`check_board.sh` 11 passed, 0 failed (warnings: no USB audio, no body attached, root disk 96%, volatile
journal); `./run.sh model` and `headless --check` read for the configuration in force (including the
dashboard's pins). All 24 Mermaid blocks parse with Mermaid 10.9.1's own parser (a deliberately
broken block is rejected); 220 relative links and anchors in `docs/` resolve and every SVG is
referenced; the new SVG was rendered with `rsvg-convert` and looked at. The mecanum sign pattern was
derived from the roller geometry, not copied from the doc. **No code or config changed and the Python
tests were not re-run.** `tools/stm32_sim/stm32_sim` was not rebuilt.

### 2026-11-12 — The illustrated handbook (`docs/`), phases 0 to 2

Asked: *analyse the project and plan docs covering the Jetson, the microcontroller, sensors and
motors, with schemas or pictures.* Chosen by the owner: a **layered handbook built beside the
existing docs** (nothing moved), **English master plus an Indonesian version**, for **team and
students**. The owner also said the body controller is an **ESP32** (see Known issues #19).

* **New `docs/`.** `docs/README.md` (language chooser); `docs/en/README.md` (front door: the
  system picture, four status badges ✅ measured / 🧪 tested without hardware / 📐 designed or
  reported / ❓ unknown, who-reads-what, chapter map, quick facts); `docs/en/01-overview/`
  (what it is, one turn end to end, glossary); `docs/en/04-brain/` (processes and ports,
  voice pipeline, models and memory, knowledge, vision and people, reliability);
  `docs/id/README.md` (the front door in Indonesian).
* **Pictures.** Three hand-editable SVGs in `docs/assets/diagrams/` (system context, process map,
  the GPU-voice swap cliff) and nine Mermaid diagrams inline (turn sequence, face phases, boot
  order, listening gates, language decision, speaking path, knowledge flow, vision and people
  flow, keepalive). No new dependency in the repo.
* **Stale spots fixed in existing docs.** `README.md` listed the old dashboard pages (Home, Chat,
  Headless) and now lists the current ones; `START_HERE.md` rows that still said Qwen3-ASR is
  the default and that there are six pages are marked historical; `USART/README.md` says what that
  folder is (an unreferenced vendor sample for a different serial motor board); `AGENTS.md`
  "Read this first" and `START_HERE.md` point at the handbook; `README.md` explains that
  `firmware/stm32` is the reference implementation of the body protocol.
* **Found while writing, not fixed:** Known issues #18 (the sign of `+vy` is documented two
  ways) and #19 (the body is an ESP32 and the repo is written for an STM32).
* **How it was checked.** Every Mermaid block parsed with Mermaid 10.9.1's own parser (a
  deliberately broken block fails, so the check is not a no-op); 107 relative links and anchors
  resolve; each SVG was rendered with `rsvg-convert` and looked at. Every figure on the pages was
  traced to `AGENTS.md`, `PROGRESS.md`, `memory-budget.md`, a module doc or `config/*.env`, and
  carries its date. Two claims were corrected when a source disagreed (the swap cliff is 17× the
  GPU voice's median and 11× the CPU voice's, not "16× the CPU"). **No code changed and the tests
  were not re-run.**
* **Not done yet** (Backlog): chapters 02 hardware, 03 firmware, 05 interfaces, 06 dashboard and
  panel, 07 operations, 08 decisions; the Indonesian pages beyond the front door; a test that
  keeps the handbook's links and the pin table honest.

### 2026-11-11 — Every conversation is stored, listed per person on Logs, and summarised when it ends

Asked: *remove Stop and Clear from the conversation log; the conversation log is always saved and
listed on the Logs menu for every person; it is saved in the database and a summary is always made
at the end of a conversation.*

* **Stop and Clear are gone from the Conversation thread.** Clear was the one thing that could make
  the page disagree with the database; Stop is not on the page any more (a person talking over the
  robot still interrupts it; the `cancel` message is still understood by core). The thread is capped
  at 400 bubbles in the browser. The **activity** log's own Clear is kept: it is a view of the
  pipeline, not the record.
* **Logs → Conversations** (new source, second in the list): every stored conversation, grouped per
  person, newest first, each folded to *when · how many turns · its summary* and expandable to the
  turns. A conversation still going on says `live`; one whose summary is not written yet says so; a
  summary the model could not write says `plain summary`. Polled every 5 s while open.
* **A conversation is a visit** (arrival → gone for 2 min). New table `conversations(person_id, visit,
  summary, kind, summary_at)` in `run/people.db`, created in place on an existing database (a test
  builds an old one). The turns already carried their visit number, so nothing is migrated.
* **A summary at the end of every conversation, not only the running notes.** Two writes now: that
  conversation's own summary (`kind="model"`), then the person's running notes the model is shown next
  time (as before). *Always* is made true by a sweep every 15 s: anything owed a summary -- cancelled
  because somebody started talking (it used to be dropped until the next time that person left),
  a restart mid-visit, a model that was down -- is written when the robot is next idle. Two unusable
  model answers and the **plain** summary is written instead ("N turns. It began with ... and ended
  with ..."), marked `auto`; with no model at all it is written at once. The conversation in progress
  is never summarised. A one-turn conversation now gets one (the old rule skipped fewer than two).
* **History is no longer cut at 400 turns** (`MAX_TURNS` 400 → 5000 per person; recall reads only the
  last few, so the prompt is unaffected).
* **A "no" is forgotten (added the same day).** A person who declines to give their name stays unknown:
  nothing is stored (it never was for strangers) and now the in-memory history of that exchange is
  emptied too (`Scripted.forget`), so the model does not carry it into the next thing they say. It never
  clears a recognised person's conversation. +2 tests.
* **Still true, and not changed:** only turns attributed to a recognised face are stored -- a typed
  line with nobody in front of the camera is not, and a stranger's talk goes under nobody. If *every*
  turn should be kept regardless, that is a privacy decision (it would store people who never agreed
  to be remembered) and is the user's to make.
* **Files:** `core/people.py` (table, `conversations`, `conversation_turns`,
  `unsummarised_conversations`, `set_conversation_summary`, `plain_summary`), `core/social.py` (`_sweep`,
  `_summarise`, `_summarise_conversation`, `_notes`, `conversations`), `core/app.py` (`conversations`,
  `conversation_turns` messages), `ui/static/logs.{html,js}`, `talk.{html,js}`.
* **Tests: +20** (`tests/test_people.py`): per-visit conversations and summaries, forgetting, an old
  database, leaving -> summary, the live one skipped, junk -> retried -> plain, no model -> plain, a
  cancelled summary written later, a restart mid-visit, one summary per visit, core's two messages, the
  Logs source, and no Stop/Clear on the thread. The JS harness feeds both new messages to every page.
* **Not verified in a browser or with a person in front of the camera.** The summaries were
  exercised against a scripted model; how good they are depends on the resident LLM (see people.md,
  Limits).

### 2026-11-10 — Conversation page tidied; a model switch now sticks; `./run.sh model`

Six comments on M13, all done.

| # | Asked | Done |
|---|---|---|
| 1 | rename Talk to Conversation | the sidebar entry, the page title and heading (the address is still `/talk`) |
| 2 | an unknown face should be asked its name again | a stranger is asked on arrival and **again** if still there when the question lapses unanswered (60 s); the least between two questions is 30 s (was 300), a "no" buys 5 min (was 30). Somebody who leaves and returns is a new stranger. `CORE_PEOPLE_ASK_COOLDOWN_S`. A known face already bound its stored conversation; that is unchanged |
| 3 | JSON response out of the chat | each reply's JSON is in the **activity log**, under the turn that produced it; the thread is only what was said |
| 4 | drop *People* and *What the model is told*; activity in their place; one page, no scrolling | the right column is the camera over the activity log; the page is `100vh` with `overflow:hidden` and only the thread and the log scroll (stacked, scrolling, on a phone). *What the model is told* is gone |
| 5 | People to Config | **Config → People**: the list, stored conversations and notes, Forget (face + conversation), Forget the conversation only, Introduce someone, and the two switches |
| 6 | changing the model selection did not change it; add `./run.sh model` | below |

**#6, what was wrong — three things, found by reproducing it on the board.**
1. *The health poll painted the LLM DOWN while it was loading.* A 3B model takes 20-40 s to load; the
   poll overwrote `RESTARTING` with `DOWN`, keepalive restarted it again (killing the load), and after two
   such failures it **fell back to a smaller model and re-pinned it**. `_probe_llm` now leaves a
   restarting service alone.
2. *The model could not load beside whisper and Supertonic.* Llama-3.2-3B restarted in place died on
   `cudaMalloc failed: out of memory` -- the Memory page said it fit (6607 MiB, 963 free); Supertonic's
   cuDNN workspace had grown to 1.8 GB since boot. A cold start works because the LLM loads first. A swap
   now stops whisper and TTS, loads the LLM, and starts them again (`_reload_llm`, ~30 s deaf and mute).
3. *Pumps repainted a service that was being restarted on purpose* (the TTS pump's own `touch(UP)`), so
   keepalive restarted Supertonic in the middle of the LLM load. `Health.hold` / `release`.
Also: restarts are now serialised (`_restart_lock`), and a failure that is out-of-memory says so in words.

**Verified live, on this board** (full stack, Llama-3.2-3B + whisper large-v3-turbo q8_0 + Supertonic GPU +
camera): switching Llama → Qwen2.5-3B → Llama through the dashboard websocket succeeded both ways in
31-34 s with `ok: true`, the LLM row went `up → restarting → unknown → up`, whisper and TTS were reloaded,
keepalive never touched the LLM, and `./run.sh model` showed chosen = configured = running each time.
Before the fix the same switch failed twice and downgraded.

**`./run.sh model`** prints the LLM, the listening model and the voice as *chosen* (dashboard), *configured*
(`config/*.env`) and *running* (read from the process), and says plainly when they differ.

**Found on the way.** This combination barely fits: with the editor attached the board had ~100-300 MiB
free after the switch. It boots and runs, but the Memory page's estimate for Supertonic (1368 MiB) is below
what it grows to (1.8 GB), so "fits, 963 MiB free" was optimistic by about that much. Not changed here; if
the margin worries you, `whisper large-v3-turbo q5_0` (the previous choice) gives back ~350 MiB.
The microphone is still absent (`PULSE_SOURCE` matches nothing), so `robot-asr` keeps failing its restarts;
that is the sound card, not this change.

**Tests:** +22 (`tests/test_model_switch.py`, `tests/test_people.py`, the page layout); the JS harness runs the new page and
the People tab. **1134 passing.**

**To apply:** `./run.sh stop && ./run.sh all`.

### 2026-10-29 — M13: one Talk page, and the robot learns who it is talking to

Asked: merge Chat, Voice and Vision into one menu and view, typed and spoken together, with the
camera as the eye. When it sees an unknown face it asks the name, saves it, and logs the
conversation under it; when it recognises a face it recalls the conversation and resumes it.

**What changed for a person using it**

| | |
|---|---|
| `/talk` | one thread (typed and spoken turns together, each marked), the camera with boxes beside it, the people it knows (unfold one to read the stored conversation and notes), "what the model is told" (camera line + memory line), the pipeline log folded away. `/chat`, `/headless`, `/vision` redirect. **Read answers aloud** decides whether a *typed* turn is spoken (off by default); a spoken one always is |
| an unknown face | alone, close, clear and steady for ~4 s -> spoken and shown: "what's your name? I'll remember you, unless you'd rather I didn't." Asked once, then 5 min of peace (30 after a "no") |
| the answer | by voice or keyboard; accepted only if it is plainly a name (`parse_name`, rules, three languages) — otherwise an ordinary turn and the question stays open |
| a name | robot-vision learns the face (3 frames), the person is filed, welcomed in the language they used. A name that is **already known is refused** to an unrecognised face |
| a known face | every turn is filed under them; the model is shown who they are, the last turns with them (as chat turns) and notes it wrote when they left |
| they come back | greeted by name after 2 min away; "Forget" removes face **and** conversation |
| a stranger sits down | the previous person's conversation leaves the history and the prompt at once |

**Built:** `core/people.py` (store, name parser, spoken lines, memory block), `core/presence.py`
(frames -> events), `core/social.py` (what the events do), `Orchestrator` hooks (`person`,
`Scripted` replies, `kind="cue"|"robot"` turns the robot starts itself, recording, barge-in takes the
stored reply back too), `prompt.build_system_prompt(memory=)`, the `talk_state` / `people_history` /
`people_forget_conversation` messages, `presence` and `initiated` events, People settings
(`people_ask`, `people_greet`), `config/core.env` `CORE_PEOPLE_*`, a People line in the `run.sh`
banner. Removed `chat.*`, `headless.*`, `vision.*` (merged into `talk.*`).

**Found by testing against the real model, and fixed.** The notes written when someone leaves came back
as the empty string on Qwen3-0.6B: it is a hybrid-reasoning model, thinks first, the thinking goes to a
field the client does not read, and 160 tokens were gone before the answer began. The request now carries
Qwen3's `/no_think` switch (other models see one odd token) — TROUBLESHOOTING § notes come back empty.
Found by the tests before it could matter: `Presence` kept reporting the last scene's faces as "in view"
after the camera stopped, so a visit never ended.

**Verified.** +128 tests (`tests/test_people.py`): the parser on 40+ phrasings, the store, every presence
rule, and the whole story through the real orchestrator with a scripted LLM and a fake camera (ask,
answer, refuse, retry, lookalike, taken name, greet, recall, isolation, leaving, summary, forgetting).
Against the **real Qwen3-0.6B** on :8080 with a fake speaker and camera: asked, welcomed, filed, notes
written, a stranger shown nothing of Deny's, Deny greeted an hour later with 6 turns recalled. The JS
harness runs the new page against every event it receives; robot-core's handling of the page's requests is tested too. **1112 passing.**

**Not verified — please do:** (1) a *live person* in front of the camera: stand alone, wait ~5 s, answer
when asked, then leave for 2+ minutes and return; (2) the *voice* half end to end — the USB sound card is
absent on this board, so the answer was exercised through the typed path, which takes the same route;
(3) the *words* on a model that can use them — with Qwen3-0.6B the plumbing is right and the greeting is
poor (two languages in one sentence, invented topics). Judge it on Qwen2.5-3B.

**Not changed:** any hard constraint, any dependency, RAM (a sqlite file), the voice pipeline, the
firmware, robot-vision.

**To apply:** the running `robot-ui` serves the old route table and `app.js` now points at `/talk`, so
the dashboard needs a restart: `./run.sh stop && ./run.sh all` (or `headless`).

### 2026-10-22 (later) — M12: the camera. Objects and faces, on the CPU, inside the budget

Asked: find the camera, test it, and if it works add object detection and face recognition,
with every model fitting on the Orin. Done in that order, measuring each step.

**Found and tested.** An **IMX219 on CSI sensor 0** (`/dev/video0`, behind Argus). 50 frames in
~3 s; the picture is a real, sharp, grainy desk scene (dim room, high gain, a pink cast);
`nvarguscamerasrc` is the only way in -- the board's OpenCV has no GStreamer -- and it shuts
down cleanly, leaving the sensor free. Raw RGB channel order verified against the JPEG
(means identical). Under the proposed systemd sandbox the sensor still opens; under a private
`/tmp` it would not (Argus uses `/tmp/argus_socket`).

**Built** (`src/robot/vision/`, `config/vision.env`, `deploy/systemd/robot-vision.service`,
`scripts/models/fetch_vision.sh`, `scripts/measure/vision_smoke.py`, the Vision page):

| piece | what |
|---|---|
| `camera.py` | one `gst-launch-1.0` child; frames on a private pipe (argus prints to stdout); newest frame only; restarts on a stall; SIGINT shutdown; a JPEG side-output for the dashboard |
| `imaging.py` | resize, letterbox, similarity transform, affine warp, NMS in **numpy** -- no OpenCV |
| `detect.py` / `faces.py` | YOLOX-Tiny (Apache-2.0), YuNet (MIT), SFace (Apache-2.0) on onnxruntime; decoding pinned by hand-worked tests |
| `service.py` | socket protocol, enrolment from several frames with refusals that say why, **motion gate** |
| `core/scene.py` + prompt | the scene as one sentence, **last** in the system prompt, only while fresh, a name only for a matched face |
| `core/app.py` | `_pump_vision`, health row (UP / UNKNOWN / DOWN), dashboard commands |
| dashboard | `/vision` page, `GET /api/camera.jpg` (auth, torn-read retry, refuses a stale frame), Memory-page line, Services row |
| ops | unit, `run_stack.sh` (skips with a reason when there is no sensor or no models), sudoers, `--no-vision`, launcher banner |

**Measured** (all on this board, live camera):

* **Fits:** whole service **213 MiB PSS** (Python 166-176 + gst child 41); the project's own
  pricer, embeddings asleep: Qwen3-0.6B **4714** steady, **Qwen2.5-3B 6024 (1546 free)**,
  Qwen3-4B 6782 (788 free) -- all inside the 7168 MiB target.
* **Speed:** YOLOX-Tiny 145 ms, YuNet 25 ms, SFace 29 ms per face (3 CPU threads).
* **CPU:** 1.05 cores with every frame analysed; **0.27 idle** with the motion gate.
* **Recognition:** enrolled one photograph, then a **different photograph of the same person**
  scored **0.756**; three other people 0.097-0.135 (threshold 0.363). RGB beat BGR (0.756 vs 0.655).
* **End to end against the running LLM:** known person -> *"I see Barack in front of me"*
  (3/3 questions); empty live camera -> *"I don't see anything right now."*

**Two things I got wrong first, and the tests/measurements that caught them**
* *The motion gate skipped nothing.* I guessed the sensor's grain at 0.5; measured, two frames of
  a still scene differ by **2.1** per 8 px block -- above any usable threshold. 16 px blocks
  bring noise to 1.1-1.3 and a 60x140 px person is 2.9, so 2.0 sits between. (Then a second
  bug: the first frame never recorded its reference blocks, so the second was analysed too.)
* *A "sharper" scene sentence was worse* (0/3 vs 3/3 on the same model). Not adopted.
* Also: `resize` returned float64 despite its docstring (a test caught it), and I killed my own
  shell with a pattern-matching cleanup loop (the `pkill -f` trap) -- twice.

**Tests:** +101 in `tests/test_vision.py` (pixels, decoders against hand-computed boxes, the
face store, the camera against a **fake gst-launch** that streams, crashes and stalls, the
socket protocol, enrolment refusals, the motion gate, the prompt, robot-core's pump, config
invariants) plus the Vision page in the JS harness. Four use the real models on real images and
skip when absent. **990 passing.**

**Not changed:** any hard constraint, any dependency (zero new Python packages), the voice
pipeline, the firmware.

**To apply on the running robot:** it was started before this work, so `robot-core` has no
camera pump yet. `./scripts/models/fetch_vision.sh` is done; then `./run.sh stop && ./run.sh all`
(or `headless`). Then Vision page -> enrol yourself -- the one thing not exercised here is a
live person in the frame.

### 2026-10-22 — Full audit: docs, code and process re-read against each other

Method: read every doc and every source file; compared what the docs promise with what the
code does; reproduced each suspicion before changing anything (several by experiment on
this board); wrote a regression test per fix and confirmed it **fails on the old code**
(44 of the 76 new tests fail there; all pass now).

**Bugs fixed — behaviour**

| # | Symptom | Cause | Fix (file) |
|---|---|---|---|
| 1 | *"Remember that the gate code is 4417"* did nothing, in every mode | the `remember` closure had been pasted into the tail of `_paint_embed_health`; `run()` built the `ToolBox` with the still-`None` `self.remember` | moved into `enable_knowledge` (`core/app.py`); background task now held and its failure logged (`core/tools.py`) |
| 2 | A barge-in near the end of a reply could end the **next** turn instantly, with the old turn's `audio_s` | `TTSService._finish` swallowed the drain error a cancel causes and sent `finished` with `id: None`; core's `on_tts` never looked at ids | TTS no longer reports a cancelled turn as finished (`tts/service.py`); core ignores events for other turns and keeps the microphone gate tied to the audio that is really playing (`core/turn.py`) |
| 3 | The language filter (`ASR_LANGUAGES`) never dropped a cough tagged *Chinese* | the recogniser mapped every unknown language to `None`, which the filter lets through on purpose | unknown names pass through as named (`asr/recognizer.py`); `_reject` drops them unless the *words* are English/Indonesian/Arabic — the same "words beat whisper's id" rule as `language.heard` (`core/turn.py`) |
| 4 | Dashboard "restart the microcontroller" left the body disconnected for good | `SerialLink.close()` cancelled the heartbeat that `run()` was awaiting, which ended `run()` itself; nothing reconnected. Reproduced against `tools/stm32_sim` | `close()` marks a requested reconnect; `run()` loops (`core/serial_link.py`) |
| 5 | After one oversized prompt the persona moved into the user turn for the rest of the session | every HTTP 400 set `system_role=False` | only a role/template error folds, and only a successful retry is remembered (`core/llm.py`) |
| 6 | Switching `listen_mode` → `interrupt` mid-reply left the robot deaf until restart | `_set_gate` refused every call in `interrupt`, including reopening | `interrupt` may open, never shut (`core/turn.py`) |
| 7 | A typed `/chat` question with the LLM down hung for 90 s and was recorded `timed_out` | `_speak_unavailable` waited for a speaker that typed turns do not have | completes at once (`core/turn.py`) |
| 8 | Pressing Cancel on an idle robot (or a cancel during retrieval) erased the previous, fully-heard reply from history | `drop_last_assistant()` pops whatever is last | only if this turn put one there (`core/turn.py`) |
| 9 | Level gate effectively off for minutes after each boot | noise floor started at −90 dBFS and converged with a ~53 s time constant | seeded from the first real frame (`asr/service.py`) |
| 10 | An emoji in an answer could end the turn mid-sentence | `😀` decoded to two lone surrogates; UTF-8 encoding raised | surrogate pairs joined, orphans dropped (`core/reply.py`) |
| 11 | A browser could ingest any file the robot can read (then read it back) | `ingest` took an arbitrary `path` from the relay | dashboard ingest restricted to `run/uploads/` and `knowledge/` (`core/app.py`); the local CLI socket is unchanged |
| 12 | TTS cancel counters leaked into the next turn's `finished.chars/calls`; embed pool said "stopped elsewhere" for its own crash | state not reset / flag cleared before it was read | `tts/service.py`, `core/embed_server.py` |

**Bugs fixed — production (systemd) path.** None of these can show under `run_stack.sh`,
which is why they survived: the units had never been installed on this board.

| # | Fault | Verified by | Fix |
|---|---|---|---|
| P1 | `NoNewPrivileges=true` on core, ui and tts blocks `sudo -n systemctl …` / `tee drop_caches` — on-demand embedding, keepalive, Apply/Restart buttons and the GPU page-cache drop would all fail | `systemd-run -p NoNewPrivileges=yes sudo -n true` → *"The 'no new privileges' flag is set, which prevents sudo from running as root"* | flag removed from those three units, with the reason in a comment |
| P2 | no `XDG_RUNTIME_DIR`/`PULSE_SERVER` → robot deaf **and** mute under systemd with every light green | `env -u XDG_RUNTIME_DIR pactl info` → *Connection refused*; with `PULSE_SERVER=…` it connects | `Environment=` lines on asr, tts, core |
| P3 | `config/tts.env` had `TTS_TRIM_KEEP_MS=40   # comment`; systemd keeps the comment in the value, so `float()` crashes robot-tts at start | `systemd-run -p EnvironmentFile=` printed `A=40        # trailing comment` | comment moved to its own line; a test now forbids trailing comments in `config/*.env` |
| P4 | `robot-core.service` never read `.env`, so `CORE_WS_TOKEN` was empty and the websocket's second lock was open | read the unit against `.env` and `core.env` | `EnvironmentFile=-…/.env` after `core.env` |
| P5 | `ProtectSystem=strict` + `ReadWritePaths=run` made `config/` read-only for the process that writes `config/*.env` on every Apply | read the unit against `_write_env` callers | `config` added to core's `ReadWritePaths` |
| P6 | `./run.sh stop` could leave `robot-face` running | read `stop_all` against `pids_for` | `face` added to the reaper list; a test compares the two lists |
| P7 | `./run.sh <mode>` banner printed `supertonic: M1` while the dashboard's pin (and the voice actually spoken) was `F1` | compared the banner with `run/settings.json` | banner prefers the dashboard's pin (`run.sh`) |

**Docs repaired:** `TECH_STACK.md` (a Slint paragraph had overwritten Supertonic's
dependency note), `system-overview.md` (process diagram, LISTEN step, decoder section,
language-routing banner), stale help strings in `core/settings.py` and `core/health.py`,
`START_HERE.md`, `README.md`, `AGENTS.md` header, `BUILD_NOTES.md`; new bug-log entries in
`TROUBLESHOOTING.md`; this file.

**Tests:** +76 (`tests/test_audit_fixes.py`, `tests/test_config_files.py`); one superseded
assertion in `tests/test_recognizer.py` rewritten to the new contract.

**Not changed:** firmware, models, memory budget, any hard constraint, any dependency.

### 2026-10-15 — `3e37e34` Serial: every phase's JSON to the microcontroller, discovery at start
Acknowledgement / answer / idle each write a JSON line (with `phase`); at start robot-core
pings every serial port with `{PING_robot}`, adopts the one that answers and saves it to
`config/core.env`. Firmware core + simulator answer the discovery ping. TTS keeps the
speaker fed with silence and resumes on a fresh `aplay` if it dies mid-reply.
Left open by that commit: a live microcontroller status card on the Config page.

### 2026-09-30 — `0801264` "ready to use"
The baseline: M0–M11 as described in the milestone table in `START_HERE.md`, plus
keepalive, the dashboard overview, the face phases, language-by-words routing and the
latency work recorded in `memory-budget.md`.

### Before that
Per-milestone history lives in `START_HERE.md` (table), `ROADMAP.md` (M0–M9 detail) and
`memory-budget.md` (measurement log).

---

## 3. Known issues

Ordered by what I would look at first. "Left" means found in the audit and consciously not
changed — the reason is given so the next person does not have to rediscover it.

1. **The configured LLM is the one the bake-off rejected.** `config/llm.env` and
   `run/settings.json` both pin `Qwen3-0.6B-Q4_K_M`. `memory-budget.md` § LLM bake-off:
   11/19 answers and *37 truncated replies* — a hybrid-reasoning model under the JSON
   grammar runs out of budget mid-object. Fine for developing on a box with an editor
   attached; **confirm before this is how the robot ships.** The recommendation on file is
   Qwen2.5-3B-Instruct. Also pinned: `tts_volume: 11`, `mic_gain: 52` — presumably
   deliberate, noted here so they are not mistaken for defaults.
2. **The systemd path has been read, linted (`systemd-analyze verify`) and its faults
   reproduced individually — but never booted.** Before leaving the robot unattended:
   `sudo ./scripts/install_services.sh && sudo ./scripts/bringup/setup_sudoers.sh && sudo
   loginctl enable-linger jetson`, then `python scripts/hardening/failure_drills.py`.
   `enable-linger` is new and required: the units now point at the user's PulseAudio.
3. **Wheel directions are unverified** (`serial-protocol.md` § Verifying wheel direction)
   and the watchdog has never been run against real hardware (`--port`).
4. **The 24-hour soak has not been run**, and `robot-tts` growth (342 → 889 MiB in 45 min)
   has not been shown to plateau (`MemoryMax=1200M` bounds it meanwhile).
5. *Left —* **discovery writes `{PING_robot}` to every `ttyACM*/ttyUSB*/ttyTHS*`.** That
   includes the USB sound card's CDC interface (harmless) and the Jetson's header UARTs
   (`ttyTHS*`), which could be wired to something that dislikes unexpected text. A
   vendor-id allow-list (STM32 is `0483`) or an opt-in would be safer. It was a deliberate
   choice in `3e37e34`, so it is a decision, not a bug.
6. *Left —* **the serial link reports UP while the board is silent.** `SerialLink.last_rx`
   is tracked and never read; the health row only checks that the port opened. The STM32
   answers every 200 ms `PING`, so "no line for 2–3 s" is a cheap, reliable "body hung".
7. *Left —* `robot-asr` decodes inside its frame loop: while a long utterance is being
   transcribed nothing is read from `arecord`, whose pipe holds ~2 s. A decode slower than
   that overruns the capture buffer. Not seen at current decode times (~0.5 s); a bounded
   queue between capture and decode is the fix when the model grows.
8. *Left —* `monitor.snapshot()` shells out to `ps` on the dashboard's event loop every 2 s;
   `Store.upsert` commits per chunk on the orchestrator's loop (a long document stalls it in
   short bursts); `ws.py` unmasks frames byte-by-byte in Python (≤ 1 MiB). All measured as
   small; none is on the voice path.
9. **Arabic on the panel is unverified** (`dev-docs/modules/face.md`), and the panel has no
   input device.
10. Arabic is the language that slips first under a long prompt (5/6 in the last combined
    run). Watch it as personas and notes grow.
11. **Not seen with a live person.** Nobody was in front of the camera during M12, so
    recognition was proven on photographs (through the service's own code path, same
    letterboxing as the sensor) and the live camera only on an empty room. Enrol yourself on the
    Vision page and check the similarity shown beside your name; if it hovers near 0.363,
    enrol again in the room's real light.
12. **The configured LLM uses the scene line poorly** (Known issue #1 again): with Qwen3-0.6B a
    known person was named 3/3 on a plain persona and missed on the long Indonesian one. A
    3B model is the real fix; the sentence itself was tested and should not be reworded blind.
13. *Left —* the camera is tuned by Argus's defaults: dim, grainy, pink. A lamp helps more than
    any software. White balance / exposure ceiling are untried; `VISION_USE_GPU=1` is unmeasured.
14. **The Conversation page has not met a live person** (M13). Scenes were scripted in the shape robot-vision
    sends. Stand alone in front of the camera for ~5 s and answer; if the robot never asks, check
    `grep 'presence\|asked a stranger' .cache/tmp/core.log` and that the face is >= 60 px wide
    (`CORE_PEOPLE_MIN_FACE`) and scores >= 0.75.
15. **A name is whatever whisper heard** and is the only key between a face and a conversation.
    Two people who give the same name are refused (the second is told the name is taken); a
    person whose face stops matching (glasses, light) can be re-introduced on Config -> People.
16. *Left —* a **typed** line is filed under whoever the camera sees, not whoever typed it.
17. *Left —* the robot asks its first question in `CORE_DEFAULT_LANGUAGE`; it cannot know the
    stranger's language before they speak. The welcome follows what they say.
18. **The sign of `+vy` is documented two ways.** The firmware's wheel formulas
    (`firmware/stm32/src/mecanum.c`: FL = vx − vy − kω, FR = vx + vy + kω, RL = vx + vy − kω,
    RR = vx − vy + kω) make `+vy` a strafe to the **left** on an X-roller chassis (FL and RR
    backward, FR and RL forward), `+ω` a turn to the left, and the Jetson side agrees
    (`turn.py` `_STEP_VECTORS`: `left` is `+vy`, `turn_left` is `+ω`). But
    `serial-protocol.md` (the `DRIVE 0.0 0.3 0.0 1000` example and the composite-motion table)
    and `scripts/serial/verify_wheels.py` both say `+vy` "strafes right". Found while writing
    `docs/en/04-brain/voice-pipeline.md`; **not changed here because the protocol doc is the
    contract.** Settle it before the first wheel check on a real chassis, or a correctly wired
    robot will look wrong and a sign that is right may get "fixed". The firmware test
    (`test_main.c`, "strafe: FL-,FR+,RL+,RR-") already encodes the formulas.
    **Widened 2026-11-16:** `scripts/serial/stm32_console.py` (`r` = `vy +0.30`, `cw` = `omega +1.00`) and
    the `drive` tool's `right` alias for `vy` (`tools.py`) use the same opposite convention, so
    `+omega` is "clockwise" in the console and `turn_left` in the Jetson. The smallest fix keeps the
    formulas and the Jetson and corrects those five places. Evidence table:
    `docs/en/03-firmware/motion.md`.
19. **The body is an ESP32; the repository is written for an STM32.** Reported by the owner on
    2026-11-12 (not verified: it was not attached): it speaks this repo's line protocol over a USB
    cable through a USB-UART chip, and drives 4 motors with encoders, the face display, battery
    and other sensors, plus a 360-degree lidar and an SPI camera (servos were not listed). Its
    firmware is not in this repository. Consequences found by reading:
    * `scripts/bringup/setup_udev.sh` matches `idVendor 0483` (STMicroelectronics) only, so a
      CP2102 (`10c4`) or CH340 (`1a86`) board gets **no `/dev/robot-stm32`**, and
      `scripts/bringup/check_board.sh` only looks at `ttyACM*` with vendors `0483/2341/2e8a`.
      Discovery by `{PING_robot}` (`ttyACM*`, `ttyUSB*`, `ttyTHS*`) would still find it;
      related to #5.
    * The 500 ms watchdog, the wheel signs, the pin map and conformance to
      `serial-protocol.md` have been checked only against the reference firmware and the
      simulator (#3), never against the ESP32.
    * 63 tracked files and hard constraints 6 and 7 say STM32. Renaming touches the constraints
      table, so it **needs sign-off** and was not done; the handbook says "body controller" and
      explains the naming in its glossary.
    * The lidar and the SPI camera have no code or protocol here, and `PROJECT_BRIEF.md` lists
      navigation as a non-goal.

20. **`STATUS` is never requested, so battery, temperature and the stall flag never arrive.**
    Found 2026-11-16. `serial_link.py` parses `STAT`, and `app.py` turns `motors=stall` into the
    hardware note "a wheel is stalled -- you are not actually moving", but nothing sends `STATUS` in
    normal running: the only caller of `request_status()` in `src/`, `scripts/` and `tests/` is
    `scripts/serial/protocol_test.py`. Whatever the body does, `STAT` never reaches the dashboard
    or the model. Not fixed: polling, a body-volunteered `STAT`, or piggybacking on `OK PING` is a
    protocol decision. Whichever is chosen, the reference firmware's stall test (`|measured| < 0.2 rad/s`
    while `|target| > 1.0 rad/s`, no waiting time) needs a debounce first or every start reports a stall.
21. **`watchdog_test.py --port` cannot observe a real board.** Found 2026-11-16. Pass or fail is read
    from `run/stm32_state.json`, which only `tools/stm32_sim` writes; against a real port it prints a
    note and then fails at step 3 (`wheels never started turning`, after 10 s). Bring-up step 2 in
    `firmware/stm32/README.md` reads as if it were an automated check on silicon. A real check needs another
    observable: a scope or LED on the PWM or driver-standby pin, or the wheels off the ground. Not fixed: the
    choice depends on the ESP32's wiring. Related to #3.

## 4. Decisions

| Date | Decision | By | Notes |
|---|---|---|---|
| 2026-09-09 | `face/` may be Rust (Slint) | human | the one exception to "Python on the Jetson" |
| 2026-09-14 | whisper.cpp is the only ASR; Qwen3-ASR removed | human | routing needs a real language id |
| 2026-10-22 | `NoNewPrivileges` dropped on core/ui/tts units | **proposed — please confirm** | the sudoers rule (5 units × 3 verbs) is the privilege boundary; the flag made that boundary unusable. Revert if you would rather give up the dashboard's restart buttons and on-demand embedding than allow `sudo` there |
| 2026-10-22 | Dashboard may ingest only from `run/uploads/` and `knowledge/` | proposed | add a root to `INGEST_ROOTS` in `core/app.py` if you need another |
| 2026-10-22 | Camera, object detection and face recognition are **in scope** | **owner** | `PROJECT_BRIEF.md` listed "Camera / vision / VLM" as a non-goal; it now lists a VLM only |
| 2026-10-22 | Vision on the **CPU**, not the GPU | proposed | a fourth CUDA context is 0.6-1.3 GiB; models are small enough. `VISION_USE_GPU=1` exists, unmeasured |
| 2026-10-22 | **No OpenCV, no new package**; `ultralytics`/`insightface`/`torch` (in the user site) not used | proposed | constraint 4 and licences (AGPL, non-commercial). Pillow only in scripts/tests |
| 2026-10-22 | Face recognition is **context only, not security** | proposed | no liveness; a photo of an enrolled person matches |
| 2026-10-29 | Chat, Voice and Vision are **one Talk page** | **owner** | the old addresses redirect |
| 2026-10-29 | The robot **keeps conversations per recognised face**, on the robot, and speaks first to ask a name | **owner** (asked for) | a new kind of personal data: see `people.md`. Off with `CORE_PEOPLE=0`; each person removable. Recall is by prompt (last turns + notes), not by embedding search, so it costs no memory |
| 2026-11-12 | Documentation: a layered handbook in `docs/`, English master plus Indonesian, for team and students; `dev-docs/` and `planning/` untouched | **owner** | Diagrams are hand-written SVG plus inline Mermaid; no docs-site generator (a new tool needs approval under working rule 1) |
| 2026-11-16 | The handbook is published as a **MkDocs + Material** site, on **GitHub Pages or Read the Docs** | **owner** (asked for it) | Build-time tooling only, in `.cache/docs-venv`, never on the robot. A docs generator is a new tool under working rule 1, so this is the approval. Recorded in `TECH_STACK.md`. Pushed in `84c82d8`; Pages still has to be enabled in the repository settings |
| 2026-11-12 | The body controller is an **ESP32** | **owner** (reported, unverified) | the repo still says STM32 in 63 files and in constraints 6-7; renaming is a separate change that needs sign-off. See Known issues #19 |
| 2026-10-29 | A **typed** line is filed under whoever the camera sees | proposed | the person at the keyboard may not be the one in front of the camera; the page names who it is filed under. Say if you would rather typed lines were never filed |

## 5. Backlog / future features

Add yours here. One row per feature; say *why* and *what it costs* (RAM first —
`memory-budget.md` rule 6).

| Feature | Why | RAM / constraint check | Status |
|---|---|---|---|
| *(your features go here — you said you would add some)* | | | |
| Microcontroller status card on Config → Hardware | the data is already broadcast (`microcontroller`, `ping_result`); `robot-ui` neither caches nor relays it in `hello` | 0 MiB | ready to build — add the key to `client.py` and `ui/app.py`'s hello **and** to `test_ui_scripts.py`'s list |
| Link-silence → health DOWN | see Known issue #6 | 0 MiB | small |
| Vendor-id allow-list for discovery | see Known issue #5 | 0 MiB | needs a decision |
| Conversation summarisation | history is bounded by turn count, so the oldest turns are lost outright | LLM time, no RAM | **per person: done (M13)**; the anonymous history is still just bounded |
| Acoustic echo cancellation (`module-echo-cancel`) | would let `interrupt` mode stop hearing itself | small | not started; the listen modes are the workaround |
| Panel input (keyboard/touch) | `stop-requested` is wired to Esc/Space but nothing is attached | 0 | hardware |
| Verify Arabic shaping on the panel | `face.md` § Open | 0 | needs a look at the screen |
| MagpieTTS for EN/AR (Option B) | only if Supertonic's quality bothers you | +~600 MiB | deferred (TECH_STACK) |
| JetPack 7.x | ecosystem maturity | — | deferred (PROJECT_BRIEF) |
| Navigation, SLAM | non-goals for v1 | — | out of scope |
| Voice enrolment: "remember my face as Deny" | asked-for introductions are done (M13); an unprompted "remember me" is not | 0 | needs a tool or a reply-schema field |
| ~~Greet a known person when they arrive~~ | | | **done (M13)** |
| ~~Track people across frames~~ | `Presence` confirms a name over 3 scenes and keeps a visit open for 2 min | | **done (M13)**, by name not by box |
| Rename a person; merge two | whisper may spell a name oddly | 0 | not started (Forget + introduce again works today) |
| Retrieve old conversations by meaning | "what did we say about the volcano in March?" | +810 MiB while embedding | not started; the knowledge store is the place |
| Per-message "don't remember this" | privacy for a single typed line | 0 | not started |
| Notice a new face when people are talking | asking only fires for one lone face | 0 | by design; two faces cannot be asked |
| Camera tuning: white balance, exposure ceiling, IR | grain and pink cast | 0 | try `nvarguscamerasrc` properties |
| `VISION_USE_GPU=1`, measured | maybe worth it with a smaller LLM | **+0.6-1.3 GiB** | unmeasured |
| A second camera / a USB camera | CSI only today | ~40 MiB each | not started |
| Handbook chapters 02 hardware and 06 dashboard and panel (03, 05, 07 and 08 are written, 2026-11-16) | phases 3 to 5 of the docs plan; 02 needs the real body's parts, power and wiring | 0 | 02 waits for the owner (the ESP32 was not at hand on 2026-11-12) |
| Indonesian pages beyond the front door (operations first; hardware waits on chapter 02) | the owner chose English + Indonesian | 0 | `docs/id/README.md` exists; every other Indonesian link goes to the English page |
| Request `STATUS` on a timer, or let the body volunteer `STAT`; debounce the stall flag | Known issue #20 | 0 | needs a decision |
| A way to see the watchdog trip on real hardware (LED or pin) and a `--port` mode that reads it | Known issue #21 | 0 | needs the ESP32's wiring |
| `tests/test_docs.py`: handbook links resolve, every figure is referenced, the pin table is generated from `robot_pins.h` | keeps the handbook from drifting | 0 | not started |
| Settle the `+vy` sign, then fix whichever of `serial-protocol.md` / `verify_wheels.py` / the firmware is wrong | Known issue #18 | 0 | needs a decision |
| ESP32 body firmware in the repo (or a pointer to where it lives), and a udev rule / `check_board.sh` that fits a USB-UART chip | Known issue #19 | 0 | needs the owner |
| Rename STM32 to "body controller" in the code, scripts and constraints | the robot's controller is an ESP32 | 0 | **needs sign-off**: touches AGENTS.md constraints 6-7 |

## 6. Adding a feature — the short checklist

1. Read AGENTS.md § Hard constraints. Anything touching one of them: **ask first.**
2. A new dependency? State its RAM cost and why an existing one cannot do it (rule 1).
3. Build it headless first; UI after (rule 2). Long-running → a unit in `deploy/systemd/`
   **and** a branch in `scripts/run_stack.sh`, and check it under *both* (this audit's
   lesson: the two supervisors read the same files differently).
4. Add a test that fails without the change. Run `python -m pytest tests/ -q`.
5. Measure RAM with `jtop`/`whats_running.py` and add a row to `memory-budget.md`.
6. Update the doc that describes it **in the same change**, then add the Changelog entry
   above and, if something broke on the way, a `TROUBLESHOOTING.md` entry.
