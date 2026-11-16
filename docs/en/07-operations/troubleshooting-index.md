# Something is wrong: a symptom index

[`TROUBLESHOOTING.md`](../../../dev-docs/ai/TROUBLESHOOTING.md) is the bug log: about 70 entries,
each **symptom, cause, fix**, newest information first, written when the problem was real. It is
long, so this page is a way in. Find your symptom here, then **search `TROUBLESHOOTING.md` for the
words in quotes** (each is the start of an entry's heading).

## First, three things to look at whatever the symptom

1. **`./run.sh status`**, and the **Services** page: is everything running, and is it on the
   GPU or the CPU? A green light only means the service answered, not that it is working: several
   entries below are about every light being green.
2. **`./scripts/measure/whats_running.py`**: look at the `swap` column first. Swap turns a 0.6 s
   voice into a 10 s one, and it makes every other number a lie.
3. **`.cache/tmp/core.log`** (or `journalctl -u robot-core` under systemd). Almost every failure in
   the list below leaves exactly one line there.

## Hearing

| You notice | Search for |
|---|---|
| It hears nothing and nothing reports an error | "The robot hears nothing, and nothing reports an error" |
| It went deaf for good and logs nothing | "The robot goes permanently deaf, and nothing logs anything" |
| You swapped the USB microphone or speaker | "You replaced the USB mic/speaker and the robot went deaf" |
| You chose another whisper size and it is deaf | "Choosing a whisper model in the dashboard leaves the robot deaf" |
| whisper dies at start | "whisper-server dies at startup with a ggml backtrace" |
| whisper says it uses the GPU but is slow | "whisper says `use gpu = 1` and decodes at CPU speed" |
| It answers something nobody said, often in Chinese | "The robot answers something nobody said", "a cough that whisper called Chinese" |
| Good in a quiet room, garbage in a busy one | "Transcription is fine in a quiet room", then "Noise suppression made it worse" |
| It interrupts itself or answers its own voice | "The robot interrupts itself", "still answers its own voice in no_interrupt mode" |
| It stops listening after you switched to `interrupt` | "Switched to "interrupt" mid-reply" |
| A turn ends the instant it starts | "A turn ends the instant it starts, after a barge-in" |

## Speaking

| You notice | Search for |
|---|---|
| It listens but never speaks | "The robot listens but never speaks" |
| It stopped speaking and the Services page is red | "The robot stopped speaking, and the Services page is red" |
| `aplay: audio open error` | "Device or resource busy" |
| The voice says it is on the GPU and is slow | "Supertonic says it is on the GPU and synthesises at CPU speed" |
| `CUDNN_STATUS_INTERNAL_ERROR` | "CUDNN_STATUS_INTERNAL_ERROR from robot-tts" |
| A pause after every sentence | "The robot pauses after every sentence" |
| The voice is slow or fast, differently each time | "The voice plays slow, or fast, and not the same way twice" |
| A voice cannot be found | "The Supertonic voice cannot be found on either page" |
| The "okay, one moment" is late or empty | "The acknowledgement is slow", "The acknowledgement cache empties itself" |
| `first_audio_ms` looks better than it sounds | "`first_audio_ms` looks better than the robot sounds" |

## Thinking and what it says

| You notice | Search for |
|---|---|
| It says "Give me a moment" to everything | "The robot says "Give me a moment" to everything", "Still "Give me a moment"" |
| Answers stop mid-sentence | "Answers stop in the middle of a sentence" |
| A turn never finishes and it goes quiet | "A turn never finishes, and the robot goes quiet" |
| `request (N tokens) exceeds the available context size` | "exceeds the available context size" |
| The stack says the LLM is healthy and there is no LLM | "The stack prints "llama-server healthy" and comes up with no LLM" |
| The model stops producing its action markers | "The model stops emitting [lang:xx] tags" |
| English question, Indonesian answer (or the reverse) | "I spoke English and it answered in Indonesian", "answers in English" |
| A long persona changes the robot | "A long prompt silently changes the persona" |

## Slow, or out of memory

| You notice | Search for |
|---|---|
| Replies are slow and tuning does nothing | "Voice replies feel slow, and tuning the dashboard does not help" |
| It got slow and every gauge is healthy | "The robot got slow, and every gauge says it is healthy" |
| `cudaMalloc failed: out of memory` with GB free | "`cudaMalloc failed: out of memory` while `free` shows GB available" |
| The LLM cannot restart after a crash | "A crashed llama-server cannot restart" |
| KV cache cannot be allocated at start | "fails to allocate its KV cache when other services are starting" |
| A killed service still holds its RAM | "A killed service keeps holding its model in RAM" |
| The Memory page and `free -m` disagree | "The Memory page disagrees with `free -m`" |
| Which processes use the GPU? | "Which processes are on the GPU" |

## Models and the Memory page

| You notice | Search for |
|---|---|
| Your choice on the Memory page does not stick | "Choosing a model on the Memory page does not stick" |
| Which model is it really using? | "Which model am I actually talking to?" |
| A downloaded model is not in the list | "A model I downloaded is not in the list" |
| Two decoders running, or the wrong one | "Two ASR decoders running at once" |

## Knowledge

| You notice | Search for |
|---|---|
| It says it does not know a document it was given | "A document ingested but the robot says it does not know" |
| Right in Indonesian, invented in Arabic | "answers Indonesian questions from a document but invents answers in Arabic" |
| Notes change the reply's language | "Retrieved notes change the reply's language" |
| "Remember that …" does nothing | ""Remember that …" does nothing" |
| The embedding server will not start or wake | "The embedding server fails to start", "The first ingestion after the embedding server wakes", "the embedding server never wakes" |

## Camera and people

| You notice | Search for |
|---|---|
| No picture, or the camera light is amber | "The Conversation page shows no picture" |
| A face is not recognised, or is the wrong person | "A face is not recognised" |
| It never asks a stranger's name | "The robot never asks a stranger's name" |
| "Learn this face" says nothing | ""Learn this face" says nothing" |
| A person's notes come back empty | "The notes about a person come back empty" |
| A conversation has no summary | "A conversation has no summary" |

## The body

| You notice | Search for |
|---|---|
| `/dev/ttyACM0` is not the microcontroller | "`/dev/ttyACM0` exists but is not the microcontroller" |
| "Restart microcontroller" leaves it disconnected | ""Restart microcontroller" leaves the body disconnected" |
| Build error `LL_GPIO_PULL_NO undeclared` | "`LL_GPIO_PULL_NO undeclared` building STM32duino" |
| Build error `typedef struct pid_t pid_t` | "conflicting declaration" |
| The body is an ESP32 and the checks say "no STM32" | not in the log: [Known issue #19](../../../dev-docs/ai/PROGRESS.md) and [The ESP32 as the body](../03-firmware/esp32-conformance.md) |
| The robot strafes or turns the wrong way | not in the log: Known issue #18 and [Motion](../03-firmware/motion.md#settled-which-way-is-vy-and-which-way-is-omega) |

## The dashboard and the supervisors

| You notice | Search for |
|---|---|
| Every dashboard page is blank | "The whole dashboard is blank" |
| The dashboard login never sticks | "The dashboard session never persists" |
| Restart buttons do nothing | "The dashboard's restart buttons say nothing happened" |
| A start is reported as timed out but the service is up | "A service start is reported as timed out" |
| It works under `run.sh` and breaks under systemd | "Everything works under `run.sh` and breaks under systemd" |
| The logs vanish on reboot | "Logs vanish on reboot" |
| `pkill -f` killed your shell | "`pkill -f <pattern>` kills the shell running it" |

## When nothing matches

1. Reproduce it once and **read `.cache/tmp/core.log` around that moment**.
2. Check `./run.sh model`: chosen, configured and running must agree.
3. Check `./run.sh <mode> --check` for a stale pin from the dashboard.
4. Look at the Services page and `whats_running.py` for swap and for CPU where GPU is required.
5. When you find it, **add an entry to `TROUBLESHOOTING.md`** (symptom, cause, fix) **and a
   regression test**, as [`AGENTS.md`](../../../AGENTS.md) rule 5 asks. Then put the symptom in
   this index.
