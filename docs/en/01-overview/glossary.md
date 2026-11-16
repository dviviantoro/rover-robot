# Glossary

Terms in the order you will meet them, grouped by where they live.

## The robot

### Body controller
The small board that owns everything real-time: wheel speeds, timing, the face display and
the safety stop. It receives short text commands from the Jetson over a USB serial cable.

**On the robot it is an ESP32** (reported by the owner on 2026-11-12). In this repository
you will see it called **STM32** almost everywhere (`firmware/stm32`, `tools/stm32_sim`,
the `STM32_PORT` setting, `/dev/robot-stm32`, and hard constraints 6 and 7 in
[`AGENTS.md`](../../../AGENTS.md)), because the project started with an STM32 design. The
reference firmware under `firmware/stm32` is still the executable description of the
protocol both boards speak. Renaming it everywhere touches the hard constraints, so it is a
separate change that needs the owner's sign-off.

### Brain
The Jetson Orin Nano 8 GB and the nine programs on it.

### Watchdog (heartbeat)
The Jetson sends `PING` every 200 ms. If the body hears nothing for 500 ms it stops the
motors by itself. The safety of the robot never depends on the Jetson being alive. Tested
against the reference firmware and the simulator 🧪; not yet checked on the ESP32 📐.

### Mecanum wheels
Wheels with angled rollers. With four of them the robot can move forwards, sideways and
turn, in any combination.

### Encoder
A sensor on a motor that counts how far it has turned, so the body can control speed instead
of just power.

### PID
The control loop that makes a wheel hold the speed it was asked for, using the encoder.

## Listening

### VAD (voice activity detection)
A small model (Silero) that says "someone is speaking now" and "they stopped". It is not the
speech decoder.

### ASR (automatic speech recognition)
Turning audio into text. Here: whisper.cpp, in `robot-whisper`.

### Utterance
One stretch of speech from the voice detector opening to closing.

### Endpointing
Deciding that you have finished. It costs the length of the pause it waits for
(`vad_min_silence`), on every turn.

### Gate
A check that throws an utterance away before it becomes a turn: too quiet, a low
confidence from the decoder, or a language the robot does not listen for.

### Barge-in
Interrupting the robot while it talks.

### Wake word
A word (`robot`) that has to appear in the transcript before a turn starts, in
`wake_word` mode.

## Thinking

### LLM (large language model)
The model that writes the answer. Here a small one in GGUF format, run by llama.cpp.

### GGUF
The file format llama.cpp loads for a model.

### Quantisation (q4, q5, q8)
Storing a model's numbers with fewer bits so it takes less memory. On this board a
quantised model of the same size class is the cheap saving: precision costs less than
parameter count.

### Context window (`LLM_CTX`)
How much text the model can see at once: prompt, history and its own reply together.

### Prompt
Everything sent to the model for a turn: the persona, notes from documents, the language
instruction, who it is talking to, the recent conversation, and what the camera sees.

### Persona
The standing instruction (name, manner, rules for speech) sent with every turn. It lives on
the Knowledge page of the dashboard.

### Grammar (constrained output)
llama.cpp can force the model to produce only text that matches a schema. The robot uses it
so the reply is always the same JSON object and can never come back as a half-written
sentence.

### Reply JSON
The object the model returns: `answer`, `language`, `foot_direction`, `foot_step`,
`hand_movement`, `face`, `style`. Only `answer` is spoken. See
[Voice pipeline § Thinking](../04-brain/voice-pipeline.md#3-thinking).

### Filler (acknowledgement)
The short "okay, one moment" spoken while the answer is prepared. Pre-rendered, so it starts
in about 123 ms.

### RAG (retrieval-augmented generation)
Looking up notes in your documents and giving them to the model with the question. See
[Knowledge](../04-brain/knowledge.md).

### Embedding
A list of numbers that captures the meaning of a piece of text, so "similar meaning" can be
found even across languages.

### Chunk
A piece of a document, split at its headings where possible, stored and searched on its own.

## Speaking

### TTS (text to speech)
Turning text into a voice. Here: Supertonic, one voice for three languages, in `robot-tts`.

### Chunker
Cuts the streaming answer into clauses so the first one can be spoken early.

### First word (time to first word)
The delay from the end of your speech to the robot's first sound. The number the whole
pipeline is tuned for.

## The board

### Unified memory
On the Jetson the CPU and the GPU draw from one pool of RAM. Loading a model on the GPU uses
the same memory as everything else.

### MiB
Mebibyte, 1024 × 1024 bytes. The pool is **7620 MiB**, not 8192: the rest is reserved by the
hardware.

### Swap
Memory pushed out to the disk when RAM runs out. A page that was swapped out has to be read
back before it can be used. Measured here, it turned a 0.6 to 0.9 s first word into 10.5 s.

### OOM (out of memory)
The state where the system kills a process to survive. The design goal is to never see it.

### CUDA context
The fixed memory cost of using the GPU from a process, paid once per process before any
model is loaded.

### GPU-required
STT and TTS must run on the GPU (`WHISPER_REQUIRE_GPU`, `TTS_REQUIRE_GPU`). If a GPU cannot
be had, they report an error instead of silently running slowly on the CPU.

## Running it

### Service
One of the nine long-running programs. Each is restarted if it dies.

### Supervisor
What starts and restarts services. In development: `./run.sh` (via `scripts/run_stack.sh`).
In production: `systemd` units in `deploy/systemd/`.

### Keepalive
The part of `robot-core` that notices a dead engine (LLM, decoder, microphone service,
voice, camera) and restarts it. See [Reliability](../04-brain/reliability.md).

### Dashboard
The web pages on port 8081 for talking to the robot, reading its logs and changing its
settings.

### Panel
The optional screen attached to the robot, drawn by `robot-face`.

### Pin (a dashboard pin)
A value chosen on the dashboard. It beats the value in `config/*.env`, and pins stay until
cleared. `./run.sh <mode> --check` lists them.
