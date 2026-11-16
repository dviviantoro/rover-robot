# The dashboard, page by page

Sidebar order: **Dashboard, Conversation, Knowledge**, then a **System** group (**Memory, Monitor,
Services**), then **Logs** and **Config**. A dot at the bottom of the sidebar shows whether the
browser is connected to the orchestrator. Sources: `src/robot/ui/static/*.html` and
[`AGENTS.md`](../../../AGENTS.md) § The dashboard.

## Before the pages

| Page | Address | What |
|---|---|---|
| **Landing** | `/` | **Public.** What the robot is, for someone who has just been given the address: three languages, runs entirely on the robot, no cloud. An "Open dashboard" button |
| **Sign in** | `/login` | A plain form and a signed cookie. The user is `ROBOT_UI_USER` (default `admin`) |
| (old addresses) | `/chat`, `/headless`, `/vision` | Redirect to Conversation, which replaced them |

## Dashboard (`/dashboard`)

*The summary.* Four headline numbers: **memory in use** out of 7620 MiB, **chunks of knowledge**
stored, **tokens in** (prompt) and **tokens out** (generated). Below: **board activity** (a graph
sampled every 2 s, so the minute before you opened the page is there), **tokens per turn**, a
small **memory** card (*price a change* and *measured now*), a **system** card (what is configured and what is running) and the **recent
conversation**.

Token counts are **llama.cpp's own**, not an estimate. The "cached" part matters: a persona is sent
every turn and almost entirely reused, so without it the prompt looks far more expensive than it is.
The lifetime total resets when `robot-core` restarts; there is no store behind it.

## Conversation (`/talk`)

*The page to open when a turn felt slow.* One screen with no page scroll: the **thread** on the
left, typed and spoken together; on the right **the robot's eye** (the camera picture, with a legend
for "someone it knows", "a face it does not" and "an object", and lists of **Objects** and **Faces**) and the
**Activity** log, the pipeline turn by turn with each reply's JSON.

* A **Send** box and a **Read answers aloud** switch: typed turns are silent unless it is on.
* **Nobody recognised** shows who the camera sees. When the robot asks a stranger their name, the
  page says so and offers *No thanks*.
* The pipeline strip shows **hearing, transcribing, retrieving, thinking, speaking, serial**.
* Every spoken turn has a collapsible **Timing** breakdown: per-stage durations with the slowest
  marked and the setting that governs it named. The durations **sum exactly to the total**, which is
  what makes the table trustworthy: nothing is double-counted. The acknowledgement is an absolute
  marker, because it starts before the answer's first clause exists.
* There is **no Stop or Clear** for conversations.

See [One turn, end to end](../01-overview/one-turn.md) for what each stage is.

## Knowledge (`/knowledge`)

The standing **System prompt** (the persona) and the documents it reasons over, on one page.

* **System prompt:** *Save* and *Revert*. It is knowledge, not a tuning dial: the page shows what it
  costs, roughly a quarter of its character count in tokens, re-sent each turn.
* **Documents:** source and chunk count; click one for three views: **Document**, **Chunks (JSON)**
  and **About** (key terms, and on request a summary and entities from the resident LLM).
* **Add knowledge:** drop a file (`.txt .md .json .csv .log .rst .yaml`, up to 8 MB of UTF-8 text),
  type a fact, or give a file path on the robot (only under `run/uploads/` or `knowledge/`).
* Cards show **chunks retrievable**, **documents ingested** and **queued, waiting to embed**.

Details and the retrieval modes are in [Knowledge](../04-brain/knowledge.md).

## Memory (`/memory`)

*Prices a model change before it is applied.* Every line shows **Estimated** beside **Measured
now**: the estimate is disk size times a measured factor, the measurement is the process's RSS
from `/proc`, refreshed every 3 s. **Where they disagree the measurement is right.**

* Headline: memory in use of 7620 MiB at peak, **steady state** (everything loaded, idle), **loaded
  now**, **mid-turn peak** (ASR decoding while the LLM is resident), **headroom**.
* **Model selection** (LLM, listening model, voice, GPU switch) reprices as you change it.
  Applying it is a reload, not a restart. **Installed models** lists what is on disk; a new file
  appears within about 3 s.

## Monitor (`/monitor`)

The board as it is now: memory in use, **swap** ("swap on this board means thrashing: it wrecks
latency"), power and temperature, and a per-service table with a trend. RSS overstates a model,
because it counts memory-mapped weights the kernel can evict; **watch for one row climbing on its
own, which is what a leak looks like before it becomes an OOM kill.**

## Services (`/services`)

Every process the robot is made of, what it weighs, and **start, stop, restart** buttons that work
through whichever supervisor is in charge (`run_stack.sh` or systemd). Stopping one is how you free
memory for another. A start can fail with memory free: a model-loading service needs *contiguous*
memory that CUDA on Tegra cannot take from another process, so stop something first. Each action is
logged with who asked.

## Logs (`/logs`)

* **Orchestrator (live)** and one tab per service log (a dimmed tab is a service that is not
  running; its log is still where the reason will be).
* **Conversations**: every conversation the robot had with someone it recognises, per person, with
  its summary. This is personal data and stays on the robot.
* Level buttons (Debug, Info, Warn, Error), Pause, Clear, and the orchestrator's **log level**.
  Debug logs the model's raw reply before any stripping.

## Config (`/config`)

*Everything the robot exposes, tunable without SSH.* Live settings apply at once; the rest restart
one component and say so. Tabs follow the groups in the registry: **Hardware** (microphone,
speaker, volume, voice-detector threshold, noise gate, noise cancelling, the **microcontroller
port** and *Ping all ports*), **Listening**, **Thinking**, **Knowledge** (how many chunks are
retrieved), **Speaking** and **People**. The model and
persona controls live on Memory and Knowledge instead.

* **A device that moved stays in the dropdown, selected, with the reason beside it**, so the page
  shows what is wrong and does not redraw as though nothing were configured.
* Monitor sources are never offered as a microphone: recording one records the robot's own voice.
* The noise-cancelling switch carries the measurement that made it default to off.

The full list of 32 settings, with ranges and defaults, is in
[Configuration](../05-interfaces/configuration.md#the-32-live-settings).
