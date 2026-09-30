"""The tunables, and which of them can be changed live.

ROADMAP M8 splits configuration into two groups, and the split has to be real:

  * **Hot-reload** -- persona, VAD threshold, wake-word sensitivity, TTS voice mapping,
    LLM temperature/sampling. Applies immediately.
  * **Scoped restart** -- LLM model file, ASR model, serial port. Each gets its own
    button that restarts only that one component.

This registry is the single source of truth for that split. The dashboard builds both
groups from it, and `robot-core` dispatches from the same declaration -- so a control
cannot appear under "hot-reload" while secretly needing a restart. Adding a setting in
the wrong group is a one-line mistake that shows up in both places at once, rather than
a silent lie in the UI.

Values persist to `run/settings.json`, which is merged over the env-file defaults at
startup, so anything tuned from the browser survives a restart.
"""
from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("robot-settings")


class Kind(enum.Enum):
    HOT = "hot"          # applies live
    RESTART = "restart"  # needs its component restarted


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: Kind
    where: str                    # which process applies it: core | asr | tts
    type: str                     # text | textarea | number | select | path
    default: Any
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: tuple[str, ...] = ()
    # RESTART only: the component whose light goes amber while it recovers, and the
    # env file/key that its service reads on the way back up.
    component: str | None = None
    env_file: str | None = None
    env_key: str | None = None
    enabled: bool = True
    disabled_reason: str = ""
    # Which tab the dashboard files this under. Here rather than in the UI so the
    # registry stays the single source of truth for everything about a setting.
    group: str = "behaviour"
    # Which PAGE owns this control. Choosing a model belongs next to the thing that
    # prices it: the Memory page shows what a combination costs against the 7620 MiB
    # ceiling before anything is loaded, and Config offered the same choice with none of
    # that context. Two places to change one value is two places to disagree.
    page: str = "config"
    # Languages this control can be auditioned in. A voice you cannot hear is a name in
    # a dropdown -- and Supertonic's whole point is that ONE voice speaks all three, so
    # hearing it in one of them tells you almost nothing.
    preview_langs: tuple[str, ...] = ()


REGISTRY: tuple[Setting, ...] = (
    # ---- hot-reload -----------------------------------------------------
    Setting("persona", "Persona / system prompt", Kind.HOT, "core", "textarea",
            default=None, group="behaviour",
            help="Prepended to every turn. Takes effect on the next thing you say."),
    Setting("barge_in_grace_ms", "Barge-in grace (ms)", Kind.HOT, "core", "number",
            default=0, minimum=0, maximum=2000, step=50, group="behaviour",
            help="Ignore speech for this long after audio starts. 0 is correct unless "
                 "the robot interrupts itself."),

    Setting("listen_mode", "What the microphone may do while the robot talks",
            Kind.HOT, "core", "select",
            default="no_interrupt", group="listening",
            options=("interrupt", "no_interrupt", "wake_word"),
            help="interrupt — speaking over the robot cuts it off. The most responsive, "
                 "and the only mode that is unsafe when the speaker feeds back into the "
                 "microphone: the robot hears its own voice, cancels itself, and "
                 "transcribes what it just said as if you had said it. "
                 "no_interrupt — the microphone is ignored from the moment audio starts "
                 "until the tail below has passed. It finishes what it is saying. "
                 "wake_word — as no_interrupt, and nothing starts a turn at all unless "
                 "the transcript names the robot."),
    Setting("listen_tail_ms", "Deaf for after speaking (ms)", Kind.HOT, "core", "number",
            default=700, minimum=0, maximum=5000, step=100, group="listening",
            help="How long after the last sample the microphone stays ignored, in the "
                 "two non-interrupt modes. Room echo and the speaker's own decay arrive "
                 "after playback ends, and an utterance that began during playback is "
                 "still being decoded when it stops -- so this, not the grace window, "
                 "is what actually stops the robot answering itself."),
    Setting("wake_word", "Wake word", Kind.HOT, "core", "text",
            default="robot", group="listening",
            help="The name the robot answers to in wake_word mode. Matched loosely "
                 "against the transcript, because an ASR hears \"hey uni\", \"unit\" "
                 "and \"you knit\". Empty means everything wakes it."),
    Setting("vad_min_silence", "End-of-speech silence (s)", Kind.HOT, "asr", "number",
            default=0.65, minimum=0.2, maximum=2.0, step=0.05, group="listening",
            help="How much silence closes an utterance. Lower feels snappier but clips "
                 "people who pause mid-thought."),
    Setting("vad_threshold", "Speech threshold", Kind.HOT, "asr", "number",
            default=0.5, minimum=0.1, maximum=0.9, step=0.05, group="listening",
            help="Higher ignores more background noise, and more quiet speech. "
                 "0.6-0.7 is the first thing to try in a room full of people."),

    # --- what the robot does about a noisy room. robot/asr/noise.py has the numbers.
    Setting("asr_noise_gate_db", "Must beat the room by (dB)", Kind.HOT, "asr", "number",
            default=6.0, minimum=0.0, maximum=30.0, step=1.0, group="listening",
            help="An utterance whose speech level is not this far above the room's own "
                 "floor is dropped before it is decoded -- nothing within a few dB of "
                 "the floor is intelligible anyway, and this saves the decode rather "
                 "than filtering it afterwards. 0 turns the gate off. Watch the meter: "
                 "the floor is the number it settles on between sentences."),
    Setting("asr_max_no_speech", "Reject if whisper says no speech", Kind.HOT, "asr",
            "number", default=0.6, minimum=0.0, maximum=1.0, step=0.05, group="listening",
            help="whisper reports how sure it is that a segment contained no speech at "
                 "all, and then transcribes it anyway. Measured on this board: real "
                 "speech stayed under 0.09 even at 0 dB SNR, room noise sat at "
                 "0.82-0.91. 1.0 accepts everything."),
    Setting("asr_denoise", "Noise suppression", Kind.HOT, "asr", "select",
            default="off", options=("off", "stream", "utterance"), group="listening",
            help="GTCRN suppression before the recogniser. Measured on synthetic "
                 "mixtures it made whisper WORSE at every SNR (+27% to +314% word "
                 "errors), which is why it is off -- "
                 "denoiser artefacts are not the kind of noise whisper was trained on. "
                 "It is here because none of that was measured in your room: record 30 "
                 "seconds of it and run scripts/measure/denoise_ab.py --noise. "
                 "stream cleans every frame before the VAD; utterance cleans only what "
                 "the VAD kept."),
    # `wakeword_sensitivity` lived here, disabled, reserved for an acoustic keyword
    # spotter that was never adopted -- openWakeWord pulls scipy, scikit-learn and
    # tflite-runtime (TECH_STACK.md § Listen). It is gone: `listen_mode=wake_word`
    # matches the name against the transcript the recogniser already produces, which
    # costs no model, no memory and no second ML runtime. A disabled control promising
    # a feature that now exists somewhere else is worse than no control.

    # The two numbers that decide how soon the robot starts talking. They live here
    # rather than in the chunker's defaults because the right value depends on what else
    # is on the GPU, which changes -- see Config.first_max_chars for the measurement.
    # default=None like `max_tokens`: config/core.env is what decides unless somebody
    # sets it here, and a concrete default would be applied over that file on startup.
    Setting("first_max_chars", "Start speaking after at most (characters)",
            Kind.HOT, "core", "number",
            default=None, minimum=20, maximum=240, step=10, group="speaking",
            help="The first chunk breaks at a clause once it passes this length, rather "
                 "than waiting for the sentence to end. Time to the first spoken word is "
                 "roughly proportional to it: measured on this board with the LLM and "
                 "whisper also on the GPU, 44 characters cost 2.3 s and 130 cost 7.6 s. "
                 "Lower is snappier and breaks mid-sentence more often. Listen before "
                 "settling on a value -- the cost is prosody, not a pause."),
    Setting("filler", "Acknowledge before answering",
            Kind.HOT, "core", "select",
            default="on", options=("on", "off"), group="speaking",
            help="Say a short, non-committal phrase -- \"Oke, sebentar ya.\", "
                 "\"Okay, one moment.\" -- the instant the transcript lands, while the "
                 "LLM is still writing. About 2.9 s separates the end of an utterance "
                 "from the first word of an answer and none of it can be removed; this "
                 "removes the SILENCE instead. Twenty phrases per language, drawn at "
                 "random, each about a second long, pre-rendered at startup so the "
                 "acknowledgement itself costs 123 ms. They acknowledge and nothing "
                 "more -- chosen before the LLM has written a token, they cannot assume "
                 "the input was even a question. Turn it off for a robot that waits "
                 "quietly."),
    Setting("first_min_chars", "...but never shorter than (characters)",
            Kind.HOT, "core", "number",
            default=None, minimum=2, maximum=80, step=1, group="speaking",
            help="Floor for that first break, so the robot does not open with one "
                 "syllable. Dropping it to about 4 lets a short opener like \"Baik,\" "
                 "or \"Okay,\" go out on its own, which measured 235 ms to first audio "
                 "against 2258 ms for the whole sentence."),

    Setting("temperature", "LLM temperature", Kind.HOT, "core", "number",
            default=0.7, minimum=0.0, maximum=1.5, step=0.05, group="thinking",
            help="Higher wanders more. Below ~0.3 the robot repeats itself."),
    # default=None for the same reason the restart-scoped settings use it: this is NOT
    # the value in force unless somebody has set it. Left at a concrete 200 it was applied
    # over the derived budget on every startup, which is why CORE_MAX_TOKENS in
    # config/core.env did nothing at all -- the file said 260, the registry said 200, and
    # the registry won silently. Unset means "derive it from LLM_CTX", and the dashboard
    # is shown the derived number so the control still has something to display.
    Setting("max_tokens", "Max reply tokens", Kind.HOT, "core", "number",
            default=None, minimum=32, maximum=2048, step=16, group="thinking",
            help="Caps reply length, and under the JSON format a reply that hits the cap "
                 "is not just short -- the object never closes, so the body fields are "
                 "lost and the last sentence stops mid-word. Left alone it is derived "
                 "from LLM_CTX: whatever the prompt does not need, up to 1024. Set it "
                 "only to make replies deliberately shorter; to make them longer, raise "
                 "LLM_CTX and leave this alone."),

    Setting("rag_k", "Retrieved chunks", Kind.HOT, "core", "number",
            default=4, minimum=0, maximum=10, step=1, group="knowledge",
            help="How many knowledge chunks go into the prompt. 0 disables retrieval."),

    Setting("voice_en", "Voice: English", Kind.HOT, "tts", "text",
            default="en_US-lessac-medium", group="speaking", preview_langs=("en",),
            help="Piper voice name in models/tts/piper."),
    Setting("voice_id", "Voice: Bahasa Indonesia", Kind.HOT, "tts", "text",
            default="id_ID-news_tts-medium", group="speaking", preview_langs=("id",),
            help="Piper ships exactly one Indonesian voice."),
    Setting("voice_ar", "Voice: Arabic", Kind.HOT, "tts", "text",
            default="ar_JO-kareem-medium", group="speaking", preview_langs=("ar",),
            help="Piper voice name in models/tts/piper."),
    Setting("supertonic_voice", "Voice (Supertonic)", Kind.HOT, "tts", "select",
            default="M1", group="speaking", preview_langs=("id", "en", "ar"),
            help="The single voice Supertonic speaks every language in. F1-F5 are "
                 "female, M1-M5 male; the list is read from the styles actually "
                 "installed. Preview each language before adopting one -- a voice that "
                 "suits English is not automatically the one that suits Arabic. Only "
                 "used when TTS_ENGINE=supertonic; the three Piper voices above are "
                 "what speak otherwise."),
    Setting("supertonic_steps", "Synthesis steps (Supertonic)", Kind.HOT, "tts",
            "number", default=4, minimum=1, maximum=16, step=1, group="speaking",
            preview_langs=("id", "en", "ar"),
            help="Flow-matching iterations per utterance. The cheapest speed control "
                 "in the system: cost is very nearly linear in it and it uses no extra "
                 "memory at all. Measured on the CPU -- 8 steps RTF 0.75, 4 steps 0.41, "
                 "2 steps 0.23. Below 3 the artefacts stop being subtle. Preview before "
                 "committing; this is a judgement your ears make, not a number."),
    Setting("tts_volume", "Speaker volume (%)", Kind.HOT, "tts", "number",
            default=100, minimum=0, maximum=200, step=5, group="speaking",
            help="Applied to the audio itself, not a mixer control, so it works the "
                 "same on any sound card. Above 100% is amplification and will clip a "
                 "loud voice."),

    # ---- scoped restart -------------------------------------------------
    Setting("llm_model", "LLM model file", Kind.RESTART, "core", "path",
            default=None, component="llm", group="thinking",
            env_file="config/llm.env", env_key="LLM_MODEL", page="memory",
            help="Path to a GGUF. Chosen on the Memory page, which prices it against "
                 "the ceiling first. Restarting the LLM drops the current reply; the "
                 "robot stays listening and says it needs a moment."),
    Setting("asr_model", "Listening model (whisper size)", Kind.RESTART, "core", "path",
            default=None, component="asr", group="listening",
            env_file="config/whisper.env", env_key="WHISPER_MODEL", page="memory",
            help="Which whisper.cpp checkpoint decodes speech. Since Qwen3-ASR was "
                 "removed this is purely a choice of SIZE, and the list is ordered that "
                 "way: tiny is fastest and garbles Arabic, base is the smallest that is "
                 "honest in all three languages, small is the default. medium and "
                 "large-v3-turbo do not fit beside an LLM on this board -- the line "
                 "above each one is what it costs. The robot is deaf while this "
                 "restarts."),
    # default=None like every other restart-scoped setting, and for the same reason:
    # this process does not apply it, config/tts.env does. A concrete default here would
    # be reported as the current value and disagree with the file that actually governs.
    Setting("tts_use_gpu", "Synthesise on the GPU", Kind.RESTART, "core", "select",
            default=None, component="tts", group="speaking",
            options=("0", "1"), env_file="config/tts.env", env_key="TTS_USE_GPU",
            help="13x faster (RTF 0.75 -> 0.06) and costs about 880 MiB, because the "
                 "CUDA context and cuDNN's workspace come out of the same unified pool "
                 "as the LLM and the ASR decoder. NvMap will not reclaim page cache, so "
                 "what has to be free is `free`, not `available`. If there is not "
                 "enough, cuDNN fails at the first sentence and robot-tts rebuilds "
                 "itself on the CPU rather than going mute -- check the Services page "
                 "for where it actually ended up."),
    Setting("serial_port", "STM32 serial port", Kind.RESTART, "core", "path",
            default=None, component="serial", group="body",
            env_file="config/core.env", env_key="STM32_PORT",
            help="Reconnects the body. Must be a stable path -- /dev/ttyACM0 may be the "
                 "USB soundcard. Use /dev/robot-stm32 (scripts/bringup/setup_udev.sh)."),
)

# Tab order and labels for the dashboard.
GROUPS = (
    ("behaviour", "Behaviour", "Persona and how the robot handles being interrupted."),
    ("listening", "Listening", "Microphone endpointing, sensitivity, what the robot does about a noisy room, and the ASR model."),
    ("thinking", "Thinking", "Sampling, reply length and which LLM is loaded."),
    ("knowledge", "Knowledge", "How much retrieved context reaches the prompt."),
    ("speaking", "Speaking", "Which Piper voice answers in each language."),
    ("body", "Body", "The serial link to the STM32."),
)

BY_KEY = {s.key: s for s in REGISTRY}
HOT = tuple(s for s in REGISTRY if s.kind is Kind.HOT)
RESTART = tuple(s for s in REGISTRY if s.kind is Kind.RESTART)


def describe() -> list[dict[str, Any]]:
    """Serialisable registry for the dashboard. The UI renders only what it is told."""
    return [
        {
            "key": s.key, "label": s.label, "kind": s.kind.value, "where": s.where,
            "type": s.type, "help": s.help, "minimum": s.minimum, "maximum": s.maximum,
            "step": s.step, "options": list(s.options), "component": s.component,
            "enabled": s.enabled, "disabled_reason": s.disabled_reason,
            "group": s.group, "page": s.page,
            "preview_langs": list(s.preview_langs),
        }
        for s in REGISTRY
    ]


class Settings:
    """Current values, persisted to a JSON file next to the runtime sockets."""

    def __init__(self, path: str | Path = "run/settings.json") -> None:
        self.path = Path(path)
        self._values: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            log.warning("could not read %s; starting from defaults", self.path)
            return
        self._values = {k: v for k, v in stored.items() if k in BY_KEY}
        log.info("loaded %d saved settings from %s", len(self._values), self.path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._values, indent=2, ensure_ascii=False))
        tmp.replace(self.path)   # atomic, so a reader never sees a half-written file

    def get(self, key: str, fallback: Any = None) -> Any:
        if key in self._values:
            return self._values[key]
        setting = BY_KEY.get(key)
        if setting and setting.default is not None:
            return setting.default
        return fallback

    def set(self, key: str, value: Any) -> Any:
        """Validate and store. Returns the coerced value."""
        setting = BY_KEY.get(key)
        if setting is None:
            raise KeyError(f"unknown setting {key!r}")
        if not setting.enabled:
            raise ValueError(setting.disabled_reason or "this setting is not available")
        if setting.type == "number":
            value = float(value)
            if setting.minimum is not None:
                value = max(setting.minimum, value)
            if setting.maximum is not None:
                value = min(setting.maximum, value)
            if setting.step and float(setting.step).is_integer():
                value = int(round(value))
        elif setting.options and value not in setting.options:
            raise ValueError(f"{value!r} is not one of {setting.options}")
        else:
            value = str(value)
        self._values[key] = value
        self.save()
        return value

    def all(self) -> dict[str, Any]:
        """Every setting, with registry defaults filled in. For DISPLAY."""
        return {s.key: self.get(s.key) for s in REGISTRY}

    def saved(self) -> dict[str, Any]:
        """Only what somebody actually chose. For APPLYING.

        The difference is not cosmetic. Applying `all()` at startup writes the registry
        default over every setting that has one -- including settings whose real value
        came from an env file this process had already read. That is how
        `CORE_MAX_TOKENS=260` sat in config/core.env doing nothing (the registry said
        200), and then how `CORE_FIRST_MAX_CHARS=40` did nothing either (the registry
        said 90). Both were invisible: the file said one thing, the process did another,
        and nothing compared them.
        """
        return dict(self._values)
