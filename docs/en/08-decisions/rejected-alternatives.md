# Rejected and deferred alternatives

Each row says what was considered, why it was not chosen, and **the condition under which to look
again**. The project brief's own rule: *do not re-litigate without new information.* Numbers are
from the sources named; the date they were measured is in the source.

## Architecture

| Alternative | Decision | Why | Look again if |
|---|---|---|---|
| **ROS 2 / micro-ROS** | Rejected | ROS 2 earns its cost for pub/sub across many processes, `tf2`, Nav2 or `ros2_control`. Here there is **one process on the Jetson talking to one microcontroller over one wire**, sending occasional commands. That is a protocol problem. DDS discovery, node lifecycle and the message toolchain would cost RAM and learning time for no function | LIDAR or depth navigation is added (Nav2 is worth not reinventing), a multi-joint arm needs inverse kinematics (MoveIt2), or the brain is split across computers |
| **NanoLLM** (NVIDIA's bundled framework) as the primary framework | Rejected | Its container pulls PyTorch, TensorRT, `transformers`, MLC, AWQ, DeepSpeed, xformers and flash-attention: exactly the weight the 8 GB budget cannot absorb. Its backend also lags llama.cpp on new models | Not as a framework. The prebuilt `jetson-containers` images for the individual lean parts are still used |
| **Rust or C++ for the Jetson orchestrator** | Rejected | All heavy work is already in native engines; the orchestrator coordinates I/O (microphone, tokens, audio chunks, serial acknowledgements), which is not CPU-bound. Rust's bindings are good, so this is not a capability problem but "rigor where rigor isn't the bottleneck" | Profiling shows one of **our own** functions is CPU-bound: then move that function alone into an extension. Do not rewrite the orchestrator. (`face/` is the one approved Rust exception, for a different reason) |
| **JetPack 7.2** | Deferred | It supports the Orin Nano and has better memory efficiency, but the third-party ecosystem has not caught up, and it needs ISO flashing and JetPack 6.x firmware first | The pipeline is stable and the wheels and containers it depends on have JetPack 7 builds |
| **A compositor** (`cage`, `weston`) for the panel | Rejected | Measured: the service user can take **and release** DRM master on the display with no root and no seat manager, so a compositor adds a process and 40 to 80 MiB for nothing. The panel measures **11.0 MiB** resident, against 60 MiB for the dashboard | The display stack stops cooperating. `nvidia-l4t-weston` is the fallback, already installed |
| **A charting library** for the dashboard | Rejected | Hundreds of kilobytes of parsing on a board with about 100 MiB of headroom, to draw two lines and a bar per turn. The charts are hand-written SVG | The charts become something a hand-written one cannot do |
| **MicroPython** on the body | Rejected | An interpreted runtime undermines the determinism that is the reason for having a microcontroller | |
| **A binary serial protocol** | Rejected | The rate is low, and being able to read the traffic with `screen` is worth more than the bytes | The rate rises toward a control loop. Then the body should own more, not the protocol grow |

## Speech and language

| Alternative | Decision | Why | Look again if |
|---|---|---|---|
| **Qwen3-ASR** as the speech engine | **Removed 2026-09-14** (human) | It is the better transcriber, but it emitted **no language tag at all**, and this robot routes three languages off that tag. Keeping two engines meant building and measuring every feature twice for one that could not do the job | |
| **Nemotron ASR with MagpieTTS** | Rejected for version 1 | Excellent Arabic and English, but **neither supports Bahasa Indonesia** | The Indonesian requirement is dropped, or as an optional English and Arabic voice |
| **Whisper through PyTorch** (`transformers`) | Rejected | Constraint 4. The same weights run through whisper.cpp as a binary of about 30 MB | |
| **Prefer a smaller model over a quantised one** | Rejected | Measured over nine samples in all three languages: turbo **q5_0 against fp16 was 656 MiB smaller, 223 ms faster and character-for-character identical**. Precision is the cheap axis; parameter count is the expensive one | |
| **Noise suppression on by default** (GTCRN) | Off by default | Against whisper it made the transcript **worse at every SNR tried**: +27% to +314% word error on stationary noise, +58% to +215% on babble. Whisper was trained on noisy audio and a denoiser's artefacts are damage it has never seen. The test was synthetic, so it ships one setting away | `./scripts/measure/denoise_ab.py --noise <30 s of the real room>` says it helps there |
| **all-MiniLM-L6-v2** for embeddings | Rejected | English-centric: it would silently degrade Arabic and Indonesian retrieval. Qwen3-Embedding-0.6B costs about 160 MB more and was verified **8 of 8 retrievals correct, 5 of 5 cross-lingual** | |
| **`k2-fsa/OmniVoice`** for the voice | Rejected | A PyTorch audio language model: loading it needs `torch` and `transformers` in the runtime path (constraint 4). No ONNX export exists. Its voice cloning would be useful in a separate tooling environment, to *produce* a voice a lean engine then speaks | An ONNX export appears |
| **The CPU voice, to save memory** | Not chosen | The GPU voice saves **348 ms on every turn** and does not decay under load. But it is fragile: with part of its memory in swap it took 10.5 s. The CPU voice is the better robot only when the headroom is lost | The memory headroom is lost: set `TTS_USE_GPU=0` |
| **Batching queued sentences** into one voice call | Off (`TTS_BATCH_SENTENCES=1`) | Joined, the model inserts its own pauses (15.0 chars/s); separate, it does not (18.0 chars/s), and which happened depended on timing. The same words came out 12 to 19% apart. Batch size 1 makes it 0.1% | |

## Vision and people

| Alternative | Decision | Why | Look again if |
|---|---|---|---|
| **YOLOv8** (Ultralytics) | Rejected | **AGPL-3.0**, and its package imports PyTorch. YOLOX-Tiny (Apache-2.0) was used: 20 MiB, 145 ms | |
| **InsightFace** | Rejected | Its pretrained models are **non-commercial research only**. SFace (Apache-2.0) and YuNet (MIT) were used | |
| **OpenCV, Pillow** in the runtime | Rejected | A new package for about 100 lines of numpy (`robot/vision/imaging.py`) | |
| **Vision on the GPU** | Not chosen | A fourth CUDA context costs 0.6 to 1.3 GiB. On the CPU the service is about **213 MiB** and about 0.27 of a core while the room is still | `VISION_USE_GPU=1` exists and has not been measured |
| **A vision-language model** | Out of scope | The brief lists it as a non-goal; the small detectors answer "who and what is in front of me" | A reason to read arbitrary images appears |
| **Treating face recognition as security** | Rejected | No liveness check: a photograph of an enrolled person is recognised as them | Never put anything that matters behind it |

## The rule behind the rows

Licences chose some of these models, not only accuracy: **changing a model means re-reading its
licence**, not just its score. And "rejected" in this project nearly always comes with a number.
A row here without one is a gap in the record, not a reason.

Sources: [`PROJECT_BRIEF.md`](../../../planning/PROJECT_BRIEF.md) § Rejected alternatives,
[`TECH_STACK.md`](../../../planning/TECH_STACK.md), [`AGENTS.md`](../../../AGENTS.md),
[`face.md`](../../../dev-docs/modules/face.md), [`vision.md`](../../../dev-docs/modules/vision.md).
