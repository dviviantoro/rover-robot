"""What models are installed, and what each would cost in RAM.

The dashboard offers a choice of models. On a board with ~1 GB of headroom that choice
is only meaningful if you can see what it costs *before* applying it, so every entry
carries an estimate derived from the measurements in memory-budget.md rather than from
the file size alone.

For GGUF models the estimate is not a guess: the KV cache is computed from the model's
own header. Qwen3-4B works out to exactly 144 KiB/token, which is what M1 measured.
"""
from __future__ import annotations

import logging
import time
import struct
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("robot-catalog")

MIB = 1024 * 1024

# ---- overheads, all measured (see memory-budget.md) ---------------------
#
# llama-server resident = weights + KV cache + this. M1: Qwen3-4B Q4_K_M was 3728 MiB at
# 4096 ctx with 2382 MiB of weights and 576 MiB of KV, leaving ~750 MiB of CUDA context
# and compute buffers.
#
# Treated as a constant because most of it is the CUDA context, which does not scale
# with the model. The compute buffers do scale a little, so this is CONSERVATIVE for
# small models -- a 0.6B priced at +750 MiB will probably come in under. Only the 4B has
# actually been measured; re-measure and split this if a small model is ever adopted.
LLM_RUNTIME_OVERHEAD_MIB = 750

# M2: a Piper voice is ~85 MiB resident whatever the 61 MiB file suggests -- the rest is
# the onnxruntime session. A larger "high" voice scales roughly with the file.
PIPER_MIN_VOICE_MIB = 85
PIPER_DISK_TO_RAM = 1.4

# M7: the Q8_0 embedding model is 610 MiB on disk and ~633 MiB of anonymous memory.
EMBED_DISK_TO_RAM = 1.04

# whisper.cpp holds the weights plus compute buffers that scale with the model's
# dimensions rather than with the utterance. Measured on this board, one 2.4 s clip
# decoded, whisper-server built CPU-only:
#
#   tiny    74 MiB on disk -> 151 MiB resident
#   base   141 MiB         -> 243 MiB
#   small  465 MiB         -> 637 MiB
#
# which is disk x 1.24 + 60 to within 8 MiB across a 6x range. `medium` and
# `large-v3-turbo` are extrapolations from that fit, not measurements -- neither fits
# alongside an LLM, so neither has been run here.
WHISPER_DISK_TO_RAM = 1.24
WHISPER_STATE_MIB = 60
# Buffers grow with the longest clip decoded so far: base settled at 286 MiB after a
# run of longer utterances against the 243 MiB above.
WHISPER_DECODE_PEAK_MIB = 50

# On the GPU the weights move to CUDA0 and the process additionally carries a CUDA
# context. Measured: `small` is 637 MiB CPU and 915 MiB on the GPU, so the context is
# ~280 MiB and roughly fixed -- it does not scale with the model. Unified memory means
# the weights are still counted once, not twice; this is the extra, not a doubling.
#
# It buys a great deal. Same board, same clip, whisper `small`: 0.50 s on the GPU
# against 23.5 s on the CPU -- RTF 0.17 versus 8.08. CPU-only whisper is viable at
# `tiny` and `base` and unusable above them.
WHISPER_CUDA_CONTEXT_MIB = 280


# Supertonic-3: four ONNX graphs (380 MiB on disk) plus onnxruntime's sessions.
# Measured on this board: 465 MiB in a bare process, 484 MiB inside robot-tts.
SUPERTONIC_RAM_MIB = 484

# ...on the CPU. On the GPU it additionally carries a CUDA context, cuDNN handles and a
# workspace, exactly as whisper does. M10 measured 1376 MiB on the GPU against 492 on the
# CPU, so the context is ~884 MiB -- nearly twice whisper's, and it was missing from this
# table entirely until the Memory page started showing measured RSS beside the estimate
# and the TTS line read 484 estimated / 1720 measured. The page said the selection fit
# while the board was swapping.
#
# Re-measured 2026-09-15, same model, same board, by restarting robot-tts on each side:
#
#     CPU                              472 MiB
#     GPU, straight after loading     1081 MiB   -> context ~609 MiB
#     GPU, after a few utterances     1738 MiB   -> context ~1266 MiB
#
# **The context GROWS with use**, because cuDNN allocates its workspace lazily, per
# tensor shape, on first use rather than at session creation -- which is also why
# `chunks()` catches CUBLAS_STATUS_ALLOC_FAILED mid-utterance on a board that loaded
# perfectly. One number cannot be right for both ends of that range: 884 sits between
# them and still understates the steady state by ~380 MiB. Budget with it, then believe
# the measured column on the Memory page, which is the only reading that sees the growth.
#
# Note what is NOT the problem: the weights are 383 MiB on disk. Supertonic is not a
# large model, it is a small model behind a large CUDA runtime, and there is no smaller
# variant to download -- `Supertone/supertonic-3` ships exactly one set of ONNX files.
SUPERTONIC_CUDA_CONTEXT_MIB = 884


def tts_engine(config: str = "config/tts.env") -> str:
    """Which speaker is configured. Changes what the TTS line in the budget means.

    Piper is priced per voice and needs three of them for three languages; Supertonic is
    one model that covers all of them, so charging it three times -- or once per voice --
    would be wrong in opposite directions.
    """
    try:
        for line in Path(config).read_text().splitlines():
            if line.strip().startswith("TTS_ENGINE="):
                return line.split("=", 1)[1].strip() or "piper"
    except OSError:
        pass
    return "piper"


def tts_on_gpu(config: str = "config/tts.env") -> bool:
    """Whether synthesis is configured for the GPU, for pricing.

    Same argument as `whisper_on_gpu`, and a bigger number: 884 MiB on a board with about
    a gigabyte spare is the difference between a selection that fits and one that swaps.
    """
    try:
        for line in Path(config).read_text().splitlines():
            if line.strip().startswith("TTS_USE_GPU="):
                return line.split("=", 1)[1].strip() not in ("0", "false", "no", "")
    except OSError:
        pass
    return False


def whisper_on_gpu(config: str = "config/whisper.env") -> bool:
    """Whether the whisper service is configured for the GPU, for pricing.

    Read from the config rather than assumed, because the answer changes the estimate by
    280 MiB on a board with about a gigabyte spare.
    """
    try:
        for line in Path(config).read_text().splitlines():
            if line.strip().startswith("WHISPER_USE_GPU="):
                return line.split("=", 1)[1].strip() not in ("0", "false", "no", "")
    except OSError:
        pass
    return True


# ---- a minimal GGUF metadata reader -------------------------------------
_GGUF_MAGIC = b"GGUF"
_STRING, _ARRAY, _UINT64, _INT64, _FLOAT64 = 8, 9, 10, 11, 12
_FIXED = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?",
          10: "<Q", 11: "<q", 12: "<d"}
_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


# A GGUF header costs ~260 ms to walk on this board, and a full `scan()` used to pay it
# TWICE per model -- once here and once inside `llm_ram_mib`. With eight GGUFs installed
# that was 4.3 s a scan, which is why the model list could only ever be built on connect.
# Keyed by identity, not path: a file replaced in place gets a new size or mtime and is
# re-read, so the cache cannot serve stale metadata for a different model.
_GGUF_CACHE: dict[tuple[str, int, int], dict] = {}


def read_gguf_metadata(path: Path, limit: int = 2000) -> dict:
    """Read a GGUF file's key/value header. Returns {} if it is not readable.

    Only the header is touched -- a few kilobytes, not the multi-gigabyte tensor data.
    Cached by (path, size, mtime) so repeated scans are free.
    """
    try:
        stat = path.stat()
        key = (str(path), stat.st_size, int(stat.st_mtime))
    except OSError:
        return {}
    cached = _GGUF_CACHE.get(key)
    if cached is not None:
        return cached
    meta = _read_gguf_metadata_uncached(path, limit)
    # Bound it: someone with a folder full of quantisations should not accumulate
    # headers for models they deleted three restarts ago.
    if len(_GGUF_CACHE) > 64:
        _GGUF_CACHE.clear()
    _GGUF_CACHE[key] = meta
    return meta


def _read_gguf_metadata_uncached(path: Path, limit: int = 2000) -> dict:
    """The actual read. See `read_gguf_metadata`."""
    try:
        with path.open("rb") as fh:
            if fh.read(4) != _GGUF_MAGIC:
                return {}
            struct.unpack("<I", fh.read(4))[0]              # version
            fh.read(8)                                       # tensor count
            kv_count = struct.unpack("<Q", fh.read(8))[0]

            def read_string() -> str:
                (n,) = struct.unpack("<Q", fh.read(8))
                # A sane cap: a header string is a name or a token, never megabytes. A
                # bogus length here would otherwise try to allocate the whole file.
                if n > 1 << 20:
                    raise ValueError(f"implausible string length {n}")
                return fh.read(n).decode("utf-8", errors="replace")

            def read_value(kind: int):
                if kind == _STRING:
                    return read_string()
                if kind == _ARRAY:
                    (item_kind,) = struct.unpack("<I", fh.read(4))
                    (n,) = struct.unpack("<Q", fh.read(8))
                    if item_kind == _STRING:
                        # Strings are variable length, so a string array can only be
                        # skipped by reading through it. Returning early without doing
                        # so leaves the file position mid-array, and every later key is
                        # then parsed from garbage -- which reads an absurd length and
                        # tries to allocate it. (This is how a vocabulary of 150k tokens
                        # turned into an OOM kill.)
                        kept = []
                        for i in range(n):
                            value = read_string()
                            if i < 8:
                                kept.append(value)
                        return kept if n <= 8 else n
                    size = _SIZES.get(item_kind, 0)
                    if not size:
                        raise ValueError(f"unknown array item type {item_kind}")
                    # Fixed-width arrays can be skipped by seeking.
                    if n > 64:
                        fh.seek(size * n, 1)
                        return n
                    return [struct.unpack(_FIXED[item_kind], fh.read(size))[0]
                            for _ in range(n)]
                fmt = _FIXED.get(kind)
                if fmt is None:
                    raise ValueError(f"unknown value type {kind}")
                return struct.unpack(fmt, fh.read(_SIZES[kind]))[0]

            out: dict = {}
            for _ in range(min(kv_count, limit)):
                key = read_string()
                (kind,) = struct.unpack("<I", fh.read(4))
                out[key] = read_value(kind)
            return out
    except (OSError, struct.error, UnicodeDecodeError, MemoryError, ValueError):
        log.debug("could not read GGUF header from %s", path)
        return {}


def kv_bytes_per_token(meta: dict) -> int | None:
    """KV cache cost per token, from the model's own geometry.

    2 (K and V) x layers x kv_heads x head_dim x 2 bytes (f16). For Qwen3-4B this gives
    exactly the 144 KiB/token measured in M1.
    """
    arch = meta.get("general.architecture")
    if not arch:
        return None

    def get(*names):
        for name in names:
            value = meta.get(f"{arch}.{name}")
            if isinstance(value, list) and value:
                value = value[0]
            if isinstance(value, (int, float)):
                return int(value)
        return None

    layers = get("block_count")
    kv_heads = get("attention.head_count_kv", "attention.head_count")
    head_dim = get("attention.key_length")
    if head_dim is None:
        embedding = get("embedding_length")
        heads = get("attention.head_count")
        head_dim = embedding // heads if embedding and heads else None
    if not (layers and kv_heads and head_dim):
        return None
    return 2 * layers * kv_heads * head_dim * 2


@dataclass
class Model:
    id: str
    kind: str                 # llm | asr | tts | embed
    name: str
    path: str
    size_mib: float
    ram_mib: float            # estimated resident cost
    detail: str = ""
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name, "path": self.path,
                "size_mib": round(self.size_mib, 1), "ram_mib": round(self.ram_mib),
                "detail": self.detail,
                # The dashboard needs this: which engine a model belongs to, and for
                # Supertonic the voices it ships. Without it the Memory page cannot
                # offer the voice picker at all -- which is how it came to be missing.
                "meta": dict(self.meta or {})}


def _dir_size_mib(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / MIB


def llm_ram_mib(path: Path, ctx: int) -> tuple[float, str]:
    """Weights + KV at this context + runtime overhead.

    Raises OSError if the file is gone -- callers decide whether that is fatal.
    """
    size = path.stat().st_size / MIB
    meta = read_gguf_metadata(path)
    per_token = kv_bytes_per_token(meta)
    if per_token:
        kv = per_token * ctx / MIB
        detail = (f"{size:.0f} MiB weights + {kv:.0f} MiB KV at {ctx} ctx "
                  f"({per_token / 1024:.0f} KiB/token) + {LLM_RUNTIME_OVERHEAD_MIB} MiB runtime")
        if size < 1500:
            detail += " — runtime measured on the 4B, so likely conservative here"
    else:
        # No usable header: fall back to the ratio Qwen3-4B showed at 4k.
        kv = size * 0.24
        detail = f"{size:.0f} MiB weights + estimated KV (no readable header)"
    return size + kv + LLM_RUNTIME_OVERHEAD_MIB, detail


def scan(models_dir: str | Path = "models", llm_ctx: int = 2048) -> list[Model]:
    """Find every installed model and price it."""
    root = Path(models_dir)
    found: list[Model] = []
    if not root.is_dir():
        return found

    for gguf in sorted((root / "llm").glob("*.gguf")) if (root / "llm").is_dir() else []:
        if gguf.with_suffix(".gguf.part").exists():
            continue      # still downloading; its size would misprice it
        ram, detail = llm_ram_mib(gguf, llm_ctx)
        meta = read_gguf_metadata(gguf)
        found.append(Model(
            id=f"llm:{gguf.name}", kind="llm", name=gguf.stem, path=str(gguf),
            size_mib=gguf.stat().st_size / MIB, ram_mib=ram, detail=detail,
            meta={"arch": meta.get("general.architecture", "?")},
        ))

    # whisper.cpp checkpoints -- the only ASR models there are. Qwen3-ASR was removed on
    # 2026-09-14 (robot/asr/recognizer.py): it transcribed better and never reported a
    # language, which is the one thing a trilingual robot cannot do without. So the ASR
    # choice is now purely a choice of SIZE, priced here against the ceiling.
    whisper_dir = root / "asr" / "whisper"
    if whisper_dir.is_dir():
        gpu = whisper_on_gpu()
        # By size, not alphabetically: the choice IS the size, so tiny-base-small-medium
        # is the order a person reads it in. Alphabetical put large-v3-turbo second.
        for ggml in sorted(whisper_dir.glob("ggml-*.bin"), key=lambda p: p.stat().st_size):
            if Path(str(ggml) + ".part").exists():
                continue      # still downloading; its size would misprice it
            size = ggml.stat().st_size / MIB
            ram = size * WHISPER_DISK_TO_RAM + WHISPER_STATE_MIB
            if gpu:
                ram += WHISPER_CUDA_CONTEXT_MIB
            found.append(Model(
                id=f"asr:{ggml.name}", kind="asr",
                name=f"whisper {ggml.stem.replace('ggml-', '')}", path=str(ggml),
                size_mib=size, ram_mib=ram,
                detail=(f"{size:.0f} MiB on disk, about {ram:.0f} MiB resident, "
                        f"+{WHISPER_DECODE_PEAK_MIB} MiB while decoding "
                        f"({'GPU' if gpu else 'CPU'})"
                        + ("" if size < 600 else " -- estimated, not measured at this size")),
                meta={"engine": "whisper"},
            ))

    supertonic = root / "tts" / "supertonic"
    tts_gpu = tts_on_gpu()
    if (supertonic / "onnx" / "vocoder.onnx").is_file():
        styles = sorted(p.stem for p in (supertonic / "voice_styles").glob("*.json"))
        size = _dir_size_mib(supertonic)
        found.append(Model(
            id="tts:supertonic", kind="tts", name="supertonic", path=str(supertonic),
            size_mib=size,
            ram_mib=SUPERTONIC_RAM_MIB + (SUPERTONIC_CUDA_CONTEXT_MIB if tts_gpu else 0),
            detail=(f"one model, all three languages, {len(styles)} preset voices "
                    f"({', '.join(styles)}). Replaces all three Piper voices"
                    + (f"; +{SUPERTONIC_CUDA_CONTEXT_MIB} MiB of CUDA context on the GPU"
                       if tts_gpu else "; about three times slower to synthesise on the CPU")),
            meta={"engine": "supertonic", "styles": styles},
        ))

    voices = root / "tts" / "piper"
    if voices.is_dir():
        for onnx in sorted(voices.glob("*.onnx")):
            if not onnx.with_suffix(".onnx.json").is_file():
                continue
            if onnx.with_suffix(".onnx.part").exists():
                continue
            size = onnx.stat().st_size / MIB
            ram = max(PIPER_MIN_VOICE_MIB, size * PIPER_DISK_TO_RAM)
            lang = onnx.stem.split("_")[0]
            found.append(Model(
                id=f"tts:{onnx.stem}", kind="tts", name=onnx.stem, path=str(onnx),
                size_mib=size, ram_mib=ram,
                detail=f"{lang} voice, about {ram:.0f} MiB resident once loaded",
            ))

    for gguf in sorted((root / "embed").glob("*.gguf")) if (root / "embed").is_dir() else []:
        size = gguf.stat().st_size / MIB
        found.append(Model(
            id=f"embed:{gguf.name}", kind="embed", name=gguf.stem, path=str(gguf),
            size_mib=size, ram_mib=size * EMBED_DISK_TO_RAM,
            detail=f"{size:.0f} MiB on disk, CPU-only",
        ))
    return found


def asr_env_updates(path: str | Path) -> list[tuple[str, str, str]]:
    """(env file, key, value) writes that make `path` the ASR model.

    One engine since 2026-09-14, so this is one write. It stays a list because
    `_apply_asr_choice` iterates it and because the shape is the seam where a second
    engine would reappear -- the version of this that had to keep two engines' keys in
    step was where a config saying one thing and a process doing another came from.
    """
    return [("config/whisper.env", "WHISPER_MODEL", str(path))]


def asr_services_for(path: str | Path) -> tuple[list[str], list[str]]:
    """(services to start or restart, services to stop) for this ASR choice.

    whisper-server has to be up before robot-asr reconnects to it, so the order matters
    and the list is ordered.
    """
    return ["whisper", "asr"], []


# ---- the budget ---------------------------------------------------------
#
# All measured. memory-budget.md is the source; the numbers are repeated here because
# this is what the dashboard prices a selection against.
CEILING_MIB = 7620          # what the OS actually sees; the rest is Tegra carveout
TARGET_MIB = 7168           # the ~7 GB steady-state target
OS_BASELINE_MIB = 650       # M0, headless, no dev session
ORCHESTRATOR_MIB = 40       # M4
DASHBOARD_MIB = 60          # M8
VECTOR_STORE_MIB = 10       # M7, negligible until the corpus grows


def system_memory() -> dict:
    """What the board is ACTUALLY using, from /proc/meminfo. No estimates.

    Every other number on the Memory page is a projection -- disk size times a measured
    factor, which is how a model that is not loaded yet can be priced at all. This one
    is the ground truth to check them against, and the reason the page can be trusted:
    if the estimates and this disagree, the estimates are wrong.

    `used` excludes reclaimable page cache, matching `free -m`. On Tegra that distinction
    matters more than usual -- CUDA cannot reclaim page cache, so "available" is
    optimistic for anything that needs the GPU.
    """
    values: dict[str, float] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    values[key] = float(parts[0]) / 1024      # kB -> MiB
    except OSError:
        return {}
    total = values.get("MemTotal", 0.0)
    free = values.get("MemFree", 0.0)
    cached = values.get("Cached", 0.0) + values.get("SReclaimable", 0.0)
    buffers = values.get("Buffers", 0.0)
    swap_total = values.get("SwapTotal", 0.0)
    return {
        "total_mib": round(total),
        "used_mib": round(total - free - buffers - cached),
        "free_mib": round(free),
        "cached_mib": round(cached + buffers),
        "available_mib": round(values.get("MemAvailable", free)),
        "swap_used_mib": round(swap_total - values.get("SwapFree", swap_total)),
    }


def budget(models: list[Model], selection: dict, *, llm_ctx: int = 2048,
           include_dashboard: bool = True, resident: set[str] | None = None,
           measured: dict[str, float] | None = None) -> dict:
    """Price a selection of models against the ceiling.

    `selection` names what would be loaded:
        llm_model    -- path or name of a GGUF, or None for no LLM
        asr_model    -- path or name of an ASR directory, or None
        voices       -- list of Piper voice names that will be resident
        embed_model  -- path or name of an embedding GGUF, or None

    Returns the line items and whether the total fits, so the dashboard can show the
    cost of a change before anything is applied.
    """
    by_path = {m.path: m for m in models}
    by_name = {m.name: m for m in models}

    def find(kind: str, value):
        if not value:
            return None
        model = by_path.get(str(value)) or by_name.get(str(value))
        if model and model.kind == kind:
            return model
        # Accept a bare filename too, which is what the config fields tend to hold.
        stem = Path(str(value)).stem
        model = by_name.get(stem)
        if model and model.kind == kind:
            return model
        # ...and a path whose display name is deliberately not its filename, which is
        # how the whisper checkpoints are listed ("whisper base", not "ggml-base").
        name = Path(str(value)).name
        return next((m for m in models
                     if m.kind == kind and Path(m.path).name == name), None)

    # Which kinds are actually loaded right now. Everything is assumed resident when
    # the caller does not say, which is what a pure projection means -- but the
    # embedding server comes and goes on its own now (embed_server.py), so a page that
    # always charges 810 MiB for it is describing a machine that does not exist.
    def live(kind: str) -> bool:
        return resident is None or kind in resident

    items: list[dict] = [
        {"label": "OS (headless)", "mib": OS_BASELINE_MIB, "kind": "os",
         "resident": True,
         "detail": "measured on a clean boot, no desktop"},
    ]

    llm = find("llm", selection.get("llm_model"))
    if llm:
        try:
            # Re-price at this context rather than reusing the catalogue's, which was
            # computed at whatever the service is configured with.
            ram, detail = llm_ram_mib(Path(llm.path), llm_ctx)
        except OSError:
            # The file went away between the scan and now. Fall back rather than
            # taking the whole page down over it.
            ram, detail = llm.ram_mib, f"{llm.detail} (file no longer readable)"
        items.append({"label": f"LLM — {llm.name}", "mib": round(ram), "kind": "llm",
                      "resident": live("llm"), "detail": detail})
    asr = find("asr", selection.get("asr_model"))
    if asr:
        items.append({"label": f"ASR — {asr.name}", "mib": round(asr.ram_mib), "kind": "asr",
                      "resident": live("asr"), "detail": asr.detail})

    engine = selection.get("tts_engine") or tts_engine()
    if engine == "supertonic":
        # One model, one line. It is not three voices and pricing it as three -- or as
        # one Piper voice -- would misprice the choice the page exists to inform.
        model = next((m for m in models if (m.meta or {}).get("engine") == "supertonic"),
                     None)
        if model:
            items.append({"label": "TTS — Supertonic (one voice, all languages)",
                          "mib": round(model.ram_mib), "kind": "tts",
                          "resident": live("tts"), "detail": model.detail})
    else:
        voice_names = selection.get("voices") or []
        voices = [v for v in (find("tts", name) for name in voice_names) if v]
        if voices:
            total = sum(v.ram_mib for v in voices)
            items.append({"label": f"TTS — {len(voices)} voice{'s' if len(voices) > 1 else ''}",
                          "mib": round(total), "kind": "tts", "resident": live("tts"),
                          "detail": ", ".join(v.name for v in voices)})

    embed = find("embed", selection.get("embed_model"))
    if embed:
        items.append({"label": f"Embeddings — {embed.name}", "mib": round(embed.ram_mib),
                      "kind": "embed", "resident": live("embed"),
                      "detail": embed.detail + ("" if live("embed") else
                                                " — asleep; started only while ingesting")})
        # The store itself is a file the orchestrator holds open, so it is resident
        # whenever robot-core is, regardless of the embedding server.
        items.append({"label": "Vector store", "mib": VECTOR_STORE_MIB, "kind": "embed",
                      "resident": True,
                      "detail": "sqlite-vec, grows with the corpus"})

    items.append({"label": "Orchestrator", "mib": ORCHESTRATOR_MIB, "kind": "core",
                  "resident": live("core"), "detail": "robot-core"})
    if include_dashboard:
        items.append({"label": "Dashboard", "mib": DASHBOARD_MIB, "kind": "ui",
                      "resident": live("ui"),
                      "detail": "robot-ui — optional; close it and the robot is unaffected"})

    # Lay the MEASURED size of each running process over its estimate. `measured` is
    # keyed by budget kind and comes from /proc, so a line that is actually loaded shows
    # what it actually costs rather than what it was predicted to cost. Lines for things
    # that are not running keep the estimate, because there is nothing else to show.
    measured = measured or {}
    for item in items:
        actual = measured.get(item["kind"])
        if actual is None:
            continue
        item["measured_mib"] = round(actual)
        item["drift_mib"] = round(actual - item["mib"])

    steady = sum(i["mib"] for i in items)
    # The decoder climbs while decoding, and that peak overlaps a resident LLM mid-turn.
    peak = steady + (WHISPER_DECODE_PEAK_MIB if asr else 0)

    # What it costs with everything up, and what it costs right now. They differ
    # whenever something is demand-started, and reporting only the first makes the page
    # describe a machine that is not the one in front of you.
    resident_total = sum(i["mib"] for i in items if i.get("resident", True))

    # The sum of what is measured, for the lines that HAVE a measurement. Not the same
    # thing as the system total -- it excludes the OS line and anything not running --
    # so both are reported and neither pretends to be the other.
    measured_total = sum(i["measured_mib"] for i in items if "measured_mib" in i)

    return {
        "ceiling_mib": CEILING_MIB,
        "target_mib": TARGET_MIB,
        "items": items,
        "resident_mib": round(resident_total),
        "asleep_mib": round(steady - resident_total),
        "steady_mib": round(steady),
        "peak_mib": round(peak),
        "headroom_mib": round(CEILING_MIB - peak),
        "fits": peak <= CEILING_MIB,
        "within_target": steady <= TARGET_MIB,
        "llm_ctx": llm_ctx,
        # Ground truth, read at the moment this was built. The page shows it beside the
        # projection so the two can be compared rather than confused.
        "measured_mib": round(measured_total) if measured_total else None,
        "system": system_memory(),
        "measured_at": time.time(),
    }
