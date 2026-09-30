# BUILD_NOTES.md

Painful build/install procedures, so they never have to be rediscovered.
Anything that was not a clean `pip install` or `apt install` belongs here.

---

## Conventions

- Models live in `models/` at the repo root. The repo is on `/media/jetson/DATA`
  (260 GB free); the root filesystem is nearly full, so **never** let a model,
  build tree, or container image land on `/`.
- Fetch scripts are in `scripts/models/` and are idempotent — re-running one
  verifies rather than re-downloads.

---

<!-- Add entries newest-first, with the date and the milestone they came from. -->

## 2026-09-04 — whisper.cpp, CPU-only, built from source (M9 follow-up)

The alternative ASR engine (`ASR_ENGINE=whisper`). Same shape as llama.cpp: cloned into
`vendor/`, built from source, reached over loopback HTTP. **No new Python dependency.**

```bash
./scripts/models/build_whisper.sh          # clone + configure + build whisper-server
./scripts/models/fetch_whisper.sh          # tiny, base, small, medium, large-v3-turbo
```

which is, in full:

```bash
cd vendor && git clone --depth 1 https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release \
      -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 \
      -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=ON \
      -DWHISPER_BUILD_SERVER=ON -DBUILD_SHARED_LIBS=OFF
cmake --build build -j3 --target whisper-server
```

- **CUDA, after first getting this wrong.** The original build here was CPU-only, on the
  argument that a second CUDA allocator competes with `llama-server` for the unified pool.
  That argument is real but it was not measured against what it costs, and what it costs is
  enormous: same board, same clips, whisper `small` decodes in **0.50 s on the GPU and
  23.5 s on the CPU** — RTF 0.17 against 8.08. ASR decode sits on the critical path of
  every spoken turn and cannot be overlapped (M4: 2393 ms against a 1500 ms target), so
  CPU-only whisper is viable at `tiny`/`base` and unusable above them.
  `WHISPER_USE_GPU=0` in `config/whisper.env` flips one binary back at runtime, so the
  escape hatch survives without a second build.
- `CMAKE_CUDA_ARCHITECTURES=87` is Orin (Ampere GA10B). Never autodetect — same as M1.
- `-j3`, not `-j6`: `nvcc` is memory-hungry and the stack is usually running while this
  builds. Takes about **25 minutes** with CUDA (142 `.cu` translation units) against ~2
  minutes CPU-only, and the binary goes from 3 MB to 197 MB.
- The GPU costs a **~280 MiB CUDA context** on top of the weights, roughly fixed across
  model sizes. `catalog.WHISPER_CUDA_CONTEXT_MIB` prices it so the Memory page is honest.
- `BUILD_SHARED_LIBS=OFF` keeps it to one self-contained binary, so there is no
  `LD_LIBRARY_PATH` to get right in the systemd unit.
- **A server, not a binding.** whisper.cpp's C API takes a ~40-field params struct by
  value; binding that from Python means tracking its layout across whisper.cpp versions
  with a silent-corruption failure mode. The HTTP contract does not move. It also lets the
  ASR model be stopped on its own when the board runs out of room.
- The only endpoint that reports a language is `response_format=verbose_json`. Ask for
  `no_language_probabilities=true` with it — the probabilities are a second decode pass
  over the whole clip, and the decoded language id is already in the response for free.

Models come from `ggerganov/whisper.cpp` on HuggingFace. Multilingual checkpoints only:
the `.en` variants are English-only and this robot speaks three languages.

## 2026-09-02 — llama.cpp with CUDA, built from source (M1)

Built rather than pulled from `jetson-containers`: Docker images land in `/var/lib/docker`
on the root filesystem, which is 94% full. The full toolchain was already present on
JetPack 6.2.1 (cmake 3.28.3, CUDA 12.6, gcc 11.4) — **no `apt install` was needed**.

```bash
cd vendor && git clone --depth 1 https://github.com/ggml-org/llama.cpp.git && cd llama.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release \
      -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 \
      -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF
cmake --build build --config Release -j 4
```

- `CMAKE_CUDA_ARCHITECTURES=87` is Orin (Ampere GA10B). Do not leave it to autodetect.
- `-j 4`, not `-j 6`: `nvcc` is memory-hungry and 6 parallel jobs risks an OOM on 8 GB.
- `LLAMA_CURL=OFF` avoids a `libcurl-dev` dependency; models are fetched by `scripts/models/`.
- Takes roughly 25 minutes. `ccache` is not installed; installing it would help rebuilds.

## 2026-09-02 — model sources (M1–M3)

| Model | Repo | Note |
|---|---|---|
| Qwen3-4B-Instruct Q4_K_M | `unsloth/Qwen3-4B-Instruct-2507-GGUF` | The official `Qwen/...-GGUF` repo is gated and needs an HF token |
| Qwen3-ASR-0.6B int8 | `csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25` | csukuangfj maintains sherpa-onnx; file layout matches `from_qwen3_asr()` exactly |
| Piper voices ×3 | `rhasspy/piper-voices` | Indonesian has exactly one voice in the entire collection |
| Silero VAD | sherpa-onnx `asr-models` GitHub release | 629 KB |

All fetched by idempotent scripts in `scripts/models/`. **Source `scripts/bringup/env.sh`
first** — it pins `PIP_CACHE_DIR`, `TMPDIR` and `HF_HOME` off the nearly-full root filesystem.

## 2026-09-02 — venv needed `--system-site-packages`

`jetson-stats` installs system-wide, so `from jtop import jtop` failed inside the project venv.
Fixed by setting `include-system-site-packages = true` in `.venv/pyvenv.cfg`. The venv's
`setuptools` (59.6) also predated PEP 660 and had to be upgraded before `pip install -e .` worked.

## 2026-09-03 — PlatformIO for the STM32 firmware (M5)

```bash
source scripts/bringup/env.sh
export PLATFORMIO_CORE_DIR="$ROBOT_ROOT/.cache/platformio"   # NOT ~/.platformio
pip install platformio
cd firmware/stm32 && pio run
```

`PLATFORMIO_CORE_DIR` matters: the default `~/.platformio` is on the root filesystem, which
is ~94% full, and the ARM toolchain plus framework is several hundred MB.

Platform `ststm32@20.0.0`, `framework-arduinoststm32@4.30000.0`,
`toolchain-gccarmnoneeabi@1.120301.0`. Build is ~5 s after the first fetch.

**Note:** installing PlatformIO into the project venv downgraded `click` to 8.3.3, which
`huggingface-hub` warns about. Nothing in the runtime path imports either, and the runtime
imports were re-checked afterwards. If this ever bites, move PlatformIO to a separate
tooling venv — AGENTS.md already expects build-only tools to live outside the runtime venv.

## 2026-09-03 — running the firmware without a microcontroller

`tools/stm32_sim/build.sh` compiles the portable firmware core (`protocol.c`, `watchdog.c`,
`mecanum.c`, `pid.c`, `robot.c`) for the host and speaks the protocol over a pseudo-terminal.
It is not a mock of the protocol — it is the same C, so tests driven through it exercise the
code that ships. Only `board_stm32.cpp` is replaced.
