"""Two speakers, one interface.

**Piper** is the default and the engine M2 measured: one checkpoint per language,
RTF 0.24-0.34, ~85 MiB resident per voice. Its cost is that the three languages are
three different checkpoints and therefore three different people -- the robot's voice
changes when the conversation does.

**Supertonic-3** is the alternative: one ONNX model covering 31 languages including all
three of ours, with preset speaker embeddings, so the robot keeps ONE voice whichever
language it answers in. It is pure ONNX Runtime -- no PyTorch, no transformers
(AGENTS.md constraint 4) -- and every dependency it needs was already installed for the
ASR.

Its speed has two knobs, both measured on this board with the M1 voice and a
54-character Indonesian sentence:

    where   steps   RTF     resident
    CPU       8     0.75     492 MiB     the original default -- three times slower
    CPU       4     0.41     492 MiB     than Piper, on the path to first audio
    GPU       8     0.06    1376 MiB
    GPU       4     0.03    1376 MiB

`steps` is the flow-matching iteration count, and cost is very nearly linear in it: 4
steps is 1.8x faster than 8 for a difference most listeners have to be told to look for.
`SUPERTONIC_STEPS` sets it; render the ladder with scripts/measure/voice_samples.py
--steps and pick with your own ears rather than taking this paragraph's word for it.

The GPU is worth 13x, and costs ~880 MiB of a 7620 MiB board because the CUDA context
and cuDNN's workspace are charged to the same unified pool as everything else. It is
opt-in through `TTS_USE_GPU` for that reason, and it FALLS BACK rather than failing:
NvMap cannot reclaim page cache, so on a full board `cudnnCreate` returns
CUDNN_STATUS_INTERNAL_ERROR -- at the first `run()`, not at session creation, which is
why the fallback is armed around synthesis and not just around loading. Importing the
CUDA build costs nothing until the CUDA provider is actually used: measured, the CPU
path is 492 MiB under either wheel.

Both engines present the same three calls to `robot.tts.service`, and both hand back a
BLOCKING iterator of int16 PCM. The service steps that iterator one item at a time in a
worker thread so a cancel stays serviceable mid-utterance -- which is what barge-in is.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

from robot.tts import voices as voicemap

log = logging.getLogger(__name__)

ENGINES = ("piper", "supertonic")

# Supertonic's flow-matching iteration count. The SDK defaults to 8; cost is very nearly
# linear in it (measured: RTF 0.75 at 8, 0.41 at 4, 0.23 at 2 on the CPU), so this is the
# cheapest speed knob there is and it costs no memory at all. 4 is the default here
# because it halves synthesis for a quality difference you have to be listening for;
# below 3 the artefacts stop being subtle.
DEFAULT_STEPS = 4
MIN_STEPS = 1
MAX_STEPS = 16

# What a CUDA/cuDNN failure looks like coming back through ONNX Runtime, which reports
# all of them as a generic `Fail` with the detail only in the message.
_CUDA_TROUBLE = ("cudnn", "cublas", "cuda failure", "cudamalloc", "cuda error",
                 "out of memory", "nvmap", "cuda_error", "cudaerror")


def _brief(exc: Exception) -> str:
    """One readable line out of an ONNX Runtime error, which arrive as paragraphs of
    build paths and template signatures."""
    text = " ".join(str(exc).split())
    return text[:160] + ("..." if len(text) > 160 else "")


def _cuda_trouble(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _CUDA_TROUBLE)


def trim_silence(samples, rate: int, keep_s: float = 0.04,
                 floor_ratio: float = 0.01, tail_s: float | None = None):
    """Cut the silence Supertonic pads every utterance with.

    Measured on this board 2026-09-17, by rendering a phrase 1-4 times and reading the
    slope: every synthesis call carries a FIXED 0.46-0.98 s of padding regardless of how
    much speech is in it.

        "Oke."          0.35 s of speech + 0.98 s of padding   -- 74% silence
        "Sebentar ya."  1.00 s of speech + 0.46 s of padding

    Output length is also quantised -- 1.32, 1.46, 1.88, 2.37 s -- which is the vector
    estimator working in fixed latent blocks, so a four-character word still costs 1.3 s.

    That is why a streamed reply sounds uneven. The first chunk is short by design
    (CORE_FIRST_MAX_CHARS=40), so it is mostly padding and drawls; later chunks are
    longer and sound normal; and whether chunks got batched together varies with how
    fast the LLM produced them, so the same reply is padded differently from one turn to
    the next. Trimming makes the rate a property of the words rather than of the
    chunking.

    `keep_s` of margin stays at the START: a hard cut on the first phoneme clips
    plosives, and every millisecond before the first one is latency.

    `tail_s` is the margin at the END, and it is NOT the same thing. Trimming both ends
    equally fixed each chunk and broke the reply: rendered separately the sentences ran
    into one another at 18.0 chars/s, while the same four sentences batched into one
    call kept the model's own inter-sentence pauses and came out at 15.0 -- the same
    words, 19% apart, decided by nothing but whether the LLM had produced them fast
    enough to be batched. The tail is what a sentence boundary sounds like, so it is
    kept deliberately and identically whichever path rendered it.
    """
    if tail_s is None:
        tail_s = keep_s
    if samples.size == 0:
        return samples
    peak = float(np.abs(samples).max())
    if peak <= 0.0:
        return samples
    # Frame RMS rather than per-sample, so one stray sample cannot defeat the trim.
    win = max(1, rate // 200)                       # 5 ms
    usable = (samples.size // win) * win
    if usable < win:
        return samples
    rms = np.sqrt((samples[:usable].reshape(-1, win) ** 2).mean(axis=1))
    loud = np.flatnonzero(rms > peak * floor_ratio)
    if loud.size == 0:
        return samples
    start = max(0, loud[0] * win - int(keep_s * rate))
    end = (loud[-1] + 1) * win + int(tail_s * rate)
    if end <= samples.size:
        return samples[start:end]
    # The model did not leave enough tail to keep. Pad with silence rather than return
    # a short chunk: the gap between sentences has to be the same length however much
    # padding this particular call happened to come with.
    out = np.zeros(end - start, dtype=samples.dtype)
    out[: samples.size - start] = samples[start:]
    return out


class GpuUnavailable(RuntimeError):
    """The GPU was required for synthesis and could not be had.

    Raised instead of quietly rebuilding on the CPU. robot-tts turns it into an `error`
    event, the orchestrator completes the turn without audio, and the Services page goes
    red -- so "the robot went quiet" points at the real cause instead of "the robot got
    slow" pointing at nothing. The engine is left unloaded, so the NEXT utterance tries
    the GPU again: free some memory and it recovers without a restart.
    """


@dataclass(frozen=True)
class Selection:
    """What will actually speak this chunk, and how to say so in a log line."""
    key: str                 # cache key: a Piper voice name, or a Supertonic style
    label: str               # for logs and the health panel
    lang: str                # the language tag the engine was asked for
    fell_back: bool          # no voice for the requested language


class PiperEngine:
    """One checkpoint per language, chosen by the reply's language tag."""

    name = "piper"

    def __init__(self, voices: dict, preload: bool = False) -> None:
        self.voices = voices
        self._loaded: dict = {}
        if preload:
            for voice in voices.values():
                self.load(Selection(voice.name, voice.name, voice.lang, False))

    def resolve(self, lang: str | None, override=None) -> Selection:
        voice, fell_back = ((override, False) if override is not None
                            else voicemap.resolve(self.voices, lang))
        return Selection(key=voice.name, label=voice.name,
                         lang=voice.lang, fell_back=fell_back)

    def load(self, selection: Selection):
        """Load and cache one voice. ~85 MiB resident each (measured, M2).

        Keyed by voice NAME, not language, so previewing an alternative does not evict
        the configured one -- and switching back is instant.
        """
        if selection.key not in self._loaded:
            from piper import PiperVoice      # imported late: ~1.5 s and 45 MiB

            voice = next((v for v in self.voices.values() if v.name == selection.key), None)
            if voice is None:
                raise RuntimeError(f"no Piper voice named {selection.key!r}")
            t0 = time.monotonic()
            self._loaded[selection.key] = PiperVoice.load(
                str(voice.onnx), config_path=str(voice.config))
            log.info("loaded voice %s (%s) in %.2fs", voice.name, voice.lang,
                     time.monotonic() - t0)
        return self._loaded[selection.key]

    def sample_rate(self, handle) -> int:
        return handle.config.sample_rate

    def chunks(self, handle, text: str, lang: str, voice: str | None = None) -> Iterator[bytes]:
        """Piper streams its own audio chunks; pass them straight through.

        `voice` is unused: which Piper checkpoint speaks was already decided by
        `resolve`, because for Piper the voice IS the model.
        """
        return (audio.audio_int16_bytes for audio in handle.synthesize(text))

    def available(self) -> bool:
        return bool(self.voices)

    def styles(self) -> list[str]:
        """Nothing to choose beyond the per-language voice map, which has its own
        settings. Returning nothing is what tells the dashboard not to offer a picker."""
        return []

    def set_voice(self, name: str) -> bool:
        return False

    def describe(self) -> str:
        return f"piper: {', '.join(sorted(v.name for v in self.voices.values()))}"

    def accelerator(self) -> str:
        """Piper is CPU-only here. `PiperVoice.load(use_cuda=True)` exists, but its
        checkpoints are small and its RTF is already 0.24-0.34 -- a CUDA context would
        cost more of the budget than the time it saved."""
        return "CPU"


class SupertonicEngine:
    """One model, one voice, every language.

    The whole utterance is synthesised in a single call, so this yields one item rather
    than a stream. The orchestrator already splits a reply into sentences before it gets
    here (`core.chunker`), so a cancel still lands within a sentence -- just not within
    a clause, as it can with Piper.
    """

    name = "supertonic"

    # 31 languages, of which these are the ones this robot speaks. Anything else is
    # asked for as the fallback rather than passed through: an unknown tag makes the
    # model guess, and guessing wrong on Arabic produces confident nonsense.
    SUPPORTED = ("en", "id", "ar")

    def __init__(self, model_dir: str | Path, voice: str = "M1",
                 threads: int = 4, steps: int = DEFAULT_STEPS,
                 use_gpu: bool = False, preload: bool = False,
                 require_gpu: bool = True, trim_silence: bool = True,
                 trim_keep_s: float = 0.04, trim_tail_s: float = 0.28) -> None:
        self.model_dir = Path(model_dir)
        self.voice = voice
        self.threads = threads
        # See trim_silence(): every call is padded by 0.46-0.98 s of silence, which is
        # most of a short chunk and is why a streamed reply sounds uneven.
        self.trim_silence = trim_silence
        self.trim_keep_s = trim_keep_s
        self.trim_tail_s = trim_tail_s
        self.steps = max(MIN_STEPS, min(MAX_STEPS, int(steps)))
        self.want_gpu = bool(use_gpu)
        # Whether a GPU that cannot be had is an ERROR or a slow day. Default: an error.
        # Synthesis on the CPU measured RTF ~1.0 on a loaded board against ~0.7 on the
        # GPU, and the fallback was silent -- the robot just got slower and the log line
        # that said why had scrolled away hours earlier. `TTS_REQUIRE_GPU=0` restores
        # the old behaviour for a board where the CPU is genuinely the plan.
        self.require_gpu = bool(require_gpu) and bool(use_gpu)
        self.on_gpu = False              # what the loaded sessions ACTUALLY are
        self._tts = None
        self._styles: dict = {}
        if preload:
            # Load NOW rather than on the first sentence of the first turn. It matters
            # more than it looks: `_engine` drops the page cache to win the GPU
            # allocation, and doing that mid-turn spends the saving on a cold read of
            # the very models it is about to load. At startup nobody is waiting.
            try:
                self._engine()
            except GpuUnavailable:
                # Already logged, with the remedy. Do NOT take robot-tts down over it:
                # a service that exits here restart-loops against a board that is full,
                # burns its StartLimitBurst and is then gone for good. Staying up means
                # the Services page can say why, and the next utterance retries.
                log.error("robot-tts is up but cannot speak until the GPU has room")

    def _load(self, gpu: bool):
        """Build the four ONNX sessions on one accelerator.

        The provider list is a module global in the SDK (`DEFAULT_ONNX_PROVIDERS`,
        whose own comment reads "GPU support can be added by extending this list"), and
        `load_onnx_modules` reads it through a from-import -- so it has to be set on
        `supertonic.loader`, not on `supertonic.config`. Patching the latter is a silent
        no-op, which is worth knowing before someone "tidies" this.
        """
        import supertonic.loader as loader
        from supertonic import TTS      # imported late, like Piper: ~1.5 s

        loader.DEFAULT_ONNX_PROVIDERS = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"] if gpu
            else ["CPUExecutionProvider"])
        t0 = time.monotonic()
        # auto_download=False on purpose: models live under models/ on the DATA
        # partition, fetched by scripts/models/fetch_supertonic.sh like everything
        # else. The SDK's own downloader would put them in ~/.cache on the root
        # filesystem, which is 94% full.
        tts = TTS(model_dir=str(self.model_dir), auto_download=False,
                  intra_op_num_threads=self.threads)
        self.on_gpu = gpu
        log.info("loaded Supertonic from %s on %s in %.2fs (%d steps)",
                 self.model_dir, "GPU" if gpu else "CPU", time.monotonic() - t0,
                 self.steps)
        return tts

    def _engine(self):
        if self._tts is None:
            gpu = self.want_gpu and self._cuda_present()
            if gpu:
                _drop_page_cache()
            try:
                self._tts = self._load(gpu)
            except Exception as exc:
                if not gpu or not _cuda_trouble(exc):
                    raise
                if self.require_gpu:
                    # Leave self._tts None on purpose: the next utterance retries the
                    # GPU, so freeing memory is enough to recover.
                    log.error("Supertonic could not load on the GPU (%s). TTS_REQUIRE_GPU "
                              "is set, so it will NOT fall back to the CPU -- the robot "
                              "stays silent until the GPU has room. Free some: a smaller "
                              "LLM (config/llm.env), a smaller whisper checkpoint "
                              "(config/whisper.env), or TTS_REQUIRE_GPU=0 to accept the "
                              "CPU at roughly RTF 1.0.", _brief(exc))
                    raise GpuUnavailable(
                        f"Supertonic needs the GPU and could not get it: {_brief(exc)}"
                    ) from exc
                log.error("Supertonic could not load on the GPU (%s); using the CPU",
                          _brief(exc))
                self._tts = self._load(False)
        return self._tts

    @staticmethod
    def _cuda_present() -> bool:
        """Is there a CUDA provider at all, before trying to use one.

        A CPU-only `onnxruntime` wheel is the normal case on a board that never wanted
        the GPU; asking for CUDA there should say so once, not fail per utterance.
        """
        try:
            import onnxruntime as ort
        except ImportError:
            return False
        if "CUDAExecutionProvider" in ort.get_available_providers():
            return True
        log.warning("TTS_USE_GPU=1 but this onnxruntime has no CUDA provider "
                    "(install onnxruntime-gpu); speaking on the CPU")
        return False

    def _fall_back_to_cpu(self, exc: Exception) -> None:
        """Rebuild on the CPU after CUDA failed mid-utterance.

        The board is unified memory and NvMap will not reclaim page cache, so whether
        cuDNN gets its workspace depends on what the LLM and the ASR decoder happen to
        be holding at that moment. Losing that race must cost one slow sentence, not a
        mute robot -- so the sessions are rebuilt here and the caller retries once.
        """
        log.error("Supertonic failed on the GPU (%s); rebuilding on the CPU. "
                  "Set TTS_USE_GPU=0 to stop trying, or free GPU memory "
                  "(WHISPER_USE_GPU=0 is the cheapest ~280 MiB).", _brief(exc))
        self._tts = None
        self._styles.clear()
        self.want_gpu = False            # one attempt per process, not per sentence
        self._tts = self._load(False)

    def resolve(self, lang: str | None, override=None) -> Selection:
        name = override if isinstance(override, str) else self.voice
        tag = (lang or "").split("-")[0].split("_")[0].lower()
        fell_back = tag not in self.SUPPORTED
        if fell_back:
            log.warning("no Supertonic language for %r, speaking as %r",
                        lang, voicemap.FALLBACK)
            tag = voicemap.FALLBACK
        return Selection(key=name, label=f"{name} ({tag})", lang=tag, fell_back=fell_back)

    def load(self, selection: Selection):
        tts = self._engine()
        if selection.key not in self._styles:
            self._styles[selection.key] = tts.get_voice_style(voice_name=selection.key)
        return tts

    def sample_rate(self, handle) -> int:
        # The SDK reports it; fall back to the model's native rate if it ever stops.
        return int(getattr(handle, "sample_rate", 44100) or 44100)

    def _synth(self, handle, text: str, lang: str, name: str):
        if name not in self._styles:
            self._styles[name] = handle.get_voice_style(voice_name=name)
        wav, _ = handle.synthesize(text, voice_style=self._styles[name], lang=lang,
                                   total_steps=self.steps)
        return wav

    def chunks(self, handle, text: str, lang: str, voice: str | None = None) -> Iterator[bytes]:
        # `voice` is the preview path: hear a style without adopting it, so the
        # configured one is untouched and switching back needs no second call.
        name = voice or self.voice
        try:
            wav = self._synth(handle, text, lang, name)
        except Exception as exc:
            # cuDNN allocates its workspace on the first run, not at session creation,
            # so this is where a full board actually says no. Rebuild on the CPU and
            # say the sentence rather than dropping it.
            if not self.on_gpu or not _cuda_trouble(exc):
                raise
            if self.require_gpu:
                # cuDNN allocates its workspace on the first run, so a board that was
                # fine at load time can still say no here. Drop the sessions and let the
                # next utterance rebuild on the GPU -- after a page-cache drop, which is
                # what usually makes the difference on Tegra.
                self._tts, self.on_gpu = None, False
                log.error("Supertonic lost the GPU mid-utterance (%s). TTS_REQUIRE_GPU "
                          "is set: this sentence is dropped rather than spoken slowly, "
                          "and the next one retries the GPU.", _brief(exc))
                raise GpuUnavailable(
                    f"Supertonic lost the GPU mid-utterance: {_brief(exc)}") from exc
            self._fall_back_to_cpu(exc)
            wav = self._synth(self._tts, text, lang, name)
        samples = np.asarray(wav).reshape(-1)
        if self.trim_silence:
            samples = trim_silence(
                samples, int(getattr(handle, "sample_rate", 44100) or 44100),
                keep_s=self.trim_keep_s, tail_s=self.trim_tail_s)
        # The SDK hands back float32 in [-1, 1]; the player speaks int16. Clipping
        # rather than wrapping, for the same reason as the whisper WAV encoder: an
        # int16 overflow turns a loud syllable into the opposite-signed one.
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        return iter((pcm,))

    def available(self) -> bool:
        return (self.model_dir / "onnx" / "vocoder.onnx").is_file()

    def styles(self) -> list[str]:
        """Every preset speaker embedding on disk.

        Read from the files rather than hard-coded: the open-weight release ships ten,
        and Supertone's Voice Builder produces more as JSON in the same directory. A
        list baked into the code would stop being true the moment someone adds one.
        """
        return sorted(p.stem for p in (self.model_dir / "voice_styles").glob("*.json"))

    def set_voice(self, name: str) -> bool:
        """Adopt a style live. Cheap: the model is already loaded, this is an embedding."""
        if name not in self.styles():
            return False
        self.voice = name
        return True

    def describe(self) -> str:
        return (f"supertonic: {self.voice} on {self.accelerator()}, {self.steps} steps "
                f"(one voice, {'/'.join(self.SUPPORTED)})")

    def accelerator(self) -> str:
        """Where synthesis actually runs -- after any fallback, not what was asked for.

        The model loads lazily, so until the first utterance there is no answer yet, only
        an intention. Saying "CPU" then would be a lie the moment it loads on the GPU,
        and the Services page would show it for as long as the robot stayed quiet.
        """
        if self._tts is None:
            if self.require_gpu:
                return "GPU required, not loaded"
            return "GPU?" if self.want_gpu else "CPU"
        return "GPU" if self.on_gpu else "CPU"


def _drop_page_cache() -> None:
    """Give CUDA a chance at the ~880 MiB it needs, before asking for it.

    Measured on this board, 2026-09-13. With llama-server and whisper-server already
    holding the GPU, Supertonic's CUDA session failed on
    `CUBLAS_STATUS_ALLOC_FAILED` and fell back to the CPU -- where it runs at RTF 0.75
    instead of 0.06, which showed up as **4.8 seconds** between the reply being written
    and the first audible word. `free` reported 1.4 GB available at the time.

    The available memory was real; it was page cache, and NvMap on Tegra will not force
    the kernel to reclaim page cache for a CUDA allocation (TROUBLESHOOTING.md). Dropping
    it first turned the same allocation into a success, with all three models on the GPU.

    `spawn_llm` and the robot-llm/robot-embed units already do this before loading. The
    reason it has to happen HERE too, rather than once at startup, is that Supertonic
    loads lazily: by the time the first sentence needs speaking the cache has refilled.
    """
    script = Path(__file__).resolve().parents[3] / "scripts" / "measure" / "drop_model_cache.py"
    if not script.is_file():
        return
    try:
        out = subprocess.run([sys.executable, str(script), "--all"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not drop the page cache before loading on the GPU: %s", exc)
        return
    log.info("page cache dropped before the GPU load: %s",
             " / ".join(line for line in out.stdout.splitlines() if line.strip()) or "no output")


def build(engine: str, *, voices: dict, preload: bool,
          supertonic_dir: str | Path, supertonic_voice: str, threads: int = 4,
          steps: int = DEFAULT_STEPS, use_gpu: bool = False, require_gpu: bool = True,
          trim_silence: bool = True, trim_keep_s: float = 0.04,
          trim_tail_s: float = 0.28):
    """Pick an engine by name. Unknown names fail loudly rather than falling back."""
    if engine == "piper":
        return PiperEngine(voices, preload=preload)
    if engine == "supertonic":
        return SupertonicEngine(supertonic_dir, voice=supertonic_voice, threads=threads,
                                steps=steps, use_gpu=use_gpu, preload=preload,
                                require_gpu=require_gpu, trim_silence=trim_silence,
                                trim_keep_s=trim_keep_s, trim_tail_s=trim_tail_s)
    raise ValueError(f"unknown TTS engine {engine!r}; expected one of {ENGINES}")


def from_env(voices: dict, preload: bool):
    return build(
        os.environ.get("TTS_ENGINE", "piper"),
        voices=voices, preload=preload,
        supertonic_dir=os.environ.get("SUPERTONIC_DIR", "models/tts/supertonic"),
        supertonic_voice=os.environ.get("SUPERTONIC_VOICE", "M1"),
        threads=int(os.environ.get("TTS_THREADS", "4")),
        steps=int(os.environ.get("SUPERTONIC_STEPS", str(DEFAULT_STEPS))),
        use_gpu=os.environ.get("TTS_USE_GPU", "0") == "1",
        require_gpu=os.environ.get("TTS_REQUIRE_GPU", "1") == "1",
        trim_silence=os.environ.get("TTS_TRIM_SILENCE", "1") == "1",
        trim_keep_s=float(os.environ.get("TTS_TRIM_KEEP_MS", "40")) / 1000.0,
        trim_tail_s=float(os.environ.get("TTS_TRIM_TAIL_MS", "280")) / 1000.0,
    )
