# PROJECT_BRIEF.md

## What we are building

A physical conversational robot that listens, thinks, ingests new knowledge, and speaks —
running entirely offline on a single NVIDIA Jetson Orin Nano 8GB, with an STM32
microcontroller handling all physical actuation.

The target feel is **natural, light, and fast**: the robot should start replying almost
immediately, should be interruptible mid-sentence like a person, and should never feel like
a walkie-talkie where you wait for a beep.

## Languages

The robot must **listen and speak** in three languages:

- **English**
- **Bahasa Indonesia**
- **Arabic**

Language is detected automatically per utterance; the user can switch languages mid-conversation
without any command or setting change. The reply is always spoken in the language the user
just used.

---

## Hardware

| Part | Role |
|---|---|
| Jetson Orin Nano 8GB (Super) | The "brain" — all AI inference, orchestration, UI |
| STM32 (F4-class or better) | All real-time actuation |
| 4× DC motors + encoders, mecanum wheels | Omnidirectional drive |
| LED matrix | Face / expression display |
| Servos | Simple hand movement / gestures |
| USB microphone | Audio input |
| Speaker | Audio output |
| USB-CDC cable (Jetson ↔ STM32) | Command link |

**Division of labour:** the Jetson decides *what* to do and sends intent. The STM32 decides
*how* to do it — inverse kinematics, PID, PWM timing, LED refresh. See
`dev-docs/architecture/serial-protocol.md`.

---

## Four functional pillars

1. **Listen** — always-on mic, VAD, wake word, streaming multilingual ASR
2. **Think** — local LLM with conversation memory, RAG retrieval, and tool-calling for actions
3. **Ingest** — documents and spoken facts embedded into a local vector store for later recall
4. **Speak** — streaming multilingual TTS with barge-in (user can interrupt playback)

Plus a **local web UI** for chat, service health, memory monitoring, and live configuration.

---

## Non-goals (explicitly out of scope for v1)

- Autonomous navigation, SLAM, obstacle avoidance, path planning
- Camera / vision / VLM
- Inverse kinematics for a multi-DOF arm (hand movement is simple preset gestures only)
- Cloud fallback of any kind — the system must be fully offline-capable
- Multi-robot coordination

---

## Rejected alternatives (do not re-litigate without new information)

### ROS 2 / micro-ROS — rejected
ROS 2 earns its cost when you need pub/sub across many processes, `tf2` coordinate frames,
Nav2 path planning, or `ros2_control` over heterogeneous actuators. This project has **one
process on the Jetson talking to one microcontroller over one wire**, sending occasional
discrete commands. That is a request/response protocol problem, not a middleware problem.
The DDS discovery layer, node lifecycle, and message toolchain would cost RAM and learning
time for zero functional gain.

*Revisit if:* we add LIDAR/depth navigation (Nav2 is worth not reinventing), a real
multi-joint arm needing IK (MoveIt2), or split the brain across multiple compute nodes.

### NanoLLM — rejected as the primary framework
A legitimate, semi-official NVIDIA framework (`dusty-nv/NanoLLM`) that bundles LLM + VLM +
speech + RAG behind one API. Rejected because its container pulls PyTorch, TensorRT,
transformers, MLC, AWQ, DeepSpeed, xformers and flash-attention — exactly the dependency
weight the 8GB budget cannot absorb. Its MLC backend also lags GGUF/llama.cpp on support for
newly released models, and its Whisper-based ASR does not fit our three-language requirement
as cleanly as Qwen3-ASR.

*Still used:* `jetson-containers` prebuilt images for the individual lean components
(llama.cpp, Piper) — same official channel, without the bundle.

### Nemotron-3.5-ASR + MagpieTTS — rejected for v1
NVIDIA's streaming ASR/TTS pair. Both are excellent and both cover English and Arabic well,
but **neither supports Bahasa Indonesia** — Indonesian is absent from Nemotron's 40 locales
entirely, and from MagpieTTS's 12 languages. Ruled out by the language requirement.

*Revisit if:* the Indonesian requirement is dropped, or as an optional higher-quality
EN/AR-only TTS path alongside Piper (see `TECH_STACK.md` § TTS Option B).

### Rust / C++ for the Jetson orchestrator — rejected
All heavy computation already happens inside compiled native engines. The orchestrator's job
is coordinating I/O (mic, ASR partials, LLM tokens, TTS chunks, serial ACKs), which is
I/O-bound, not CPU-bound. Rust's `sherpa-onnx` bindings are genuinely good, so this is not a
capability problem — it is a "extra rigor where rigor isn't the bottleneck" problem.

*Escape hatch:* if profiling ever shows one of **our own** functions is CPU-bound, move that
function alone into a PyO3/pybind11 extension. Do not rewrite the orchestrator.

### JetPack 7.2 — deferred
JetPack 7.2.1 now officially supports the Orin Nano Developer Kit (Ubuntu 24.04, CUDA 13,
kernel 6.8) and has better memory efficiency. But it is new enough that much of the
third-party ecosystem hasn't caught up, and it requires ISO-based flashing (no SD image) plus
JetPack 6.x firmware first. **Start on JetPack 6.2** for ecosystem maturity; revisit once the
pipeline is stable and the wheels/containers we depend on have JP7 builds.

---

## Success criteria for v1

- [ ] Wake word → first audio out in **under ~1.5 s** for a short reply
- [ ] User can interrupt mid-reply and be heard (barge-in works)
- [ ] All three languages work end-to-end, auto-detected, no manual switching
- [ ] Runs 24h without an OOM kill or a stuck motor
- [ ] Robot answers a question using a document ingested after boot
- [ ] Voice command produces correct physical motion via STM32
- [ ] UI reachable from a laptop/phone on the same network
