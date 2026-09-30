"""What the robot does about a busy room, and why each default is what it is.

The complaint this answers came from a real room: the microphone works, the pipeline
works, and in a crowd the transcripts are garbage. `src/robot/asr/noise.py` measured
where that comes from and fitted three defences to it. These tests pin the parts that
can be wrong quietly:

  * a gate that rejects too much makes the robot deaf, which is this project's worst
    failure mode and has cost two debugging sessions already
  * a gate that rejects too little is the bug we started with
  * suppression is off by default *because it was measured and it lost* -- a later
    change flipping that default silently would undo the measurement

No model and no engine is loaded here. The denoiser is exercised through its inactive
path, which is exactly the path a Jetson without `models/denoise/` takes.
"""
from __future__ import annotations

import numpy as np
import pytest

from robot.asr.capture import FRAME
from robot.asr.noise import (NoiseControl, NoiseSettings, dbfs, shape_reason,
                             speech_dbfs)


def tone(seconds: float, amplitude: float = 0.2, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * 16000)) / 16000
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def off() -> NoiseControl:
    return NoiseControl(NoiseSettings(mode="off"))


class TestLevels:
    def test_digital_silence_reads_minus_90_not_minus_infinity(self):
        """A log of zero is -inf, and -inf poisons every average it touches."""
        assert dbfs(np.zeros(1000, dtype=np.float32)) == -90.0
        assert dbfs(np.empty(0, dtype=np.float32)) == -90.0

    def test_full_scale_is_about_minus_3_dbfs_for_a_sine(self):
        assert dbfs(tone(0.1, amplitude=1.0)) == pytest.approx(-3.0, abs=0.2)

    def test_speech_level_ignores_the_pauses_around_the_speech(self):
        """The VAD hands over room tone at both ends of every utterance.

        Plain RMS reads those as part of the voice and under-reports exactly the
        utterances that are mostly pause -- which is how a level gate ends up dropping
        someone who thought before finishing their sentence.
        """
        utterance = np.concatenate([np.zeros(16000, dtype=np.float32),
                                    tone(0.5, amplitude=0.2),
                                    np.zeros(16000, dtype=np.float32)])
        assert speech_dbfs(utterance) > dbfs(utterance) + 6
        assert speech_dbfs(utterance) == pytest.approx(dbfs(tone(0.5, 0.2)), abs=1.0)


class TestTheLevelGate:
    def test_a_voice_well_above_the_room_is_kept(self):
        keep, level = off().keeps(tone(1.0, amplitude=0.2), floor_db=-50.0)
        assert keep and level > -50.0

    def test_something_barely_above_the_room_is_dropped(self):
        """A chair, a door, a cough from across the room: louder than the floor, and
        nowhere near loud enough to be someone talking to the robot."""
        quiet = tone(1.0, amplitude=0.0025)          # about -52 dBFS
        keep, _ = off().keeps(quiet, floor_db=-54.0)
        assert not keep

    def test_a_zero_margin_turns_the_gate_off_completely(self):
        """The escape hatch. Someone with a quiet voice must be able to switch this off
        rather than discover the robot has stopped answering them."""
        control = NoiseControl(NoiseSettings(mode="off", gate_margin_db=0.0))
        keep, _ = control.keeps(np.zeros(16000, dtype=np.float32), floor_db=-90.0)
        assert keep

    def test_the_default_margin_is_six_db(self):
        assert NoiseSettings().gate_margin_db == 6.0


class TestTheConfidenceGate:
    def test_room_noise_that_whisper_transcribed_anyway_is_dropped(self):
        """Measured: two seconds of room noise came back with no_speech 0.82-0.91."""
        why = off().transcript_reason("apa apa", no_speech=0.87, logprob=-0.70)
        assert why and "no speech" in why

    def test_real_speech_at_0_db_snr_is_kept(self):
        """Also measured, on the same board: the worst real speech scored 0.087 /
        -0.63. The gate has to sit above that or it eats the hard cases it exists to
        protect."""
        assert off().transcript_reason("tolong maju sedikit", 0.087, -0.63) is None

    def test_an_incoherent_decode_is_dropped(self):
        why = off().transcript_reason("some words", no_speech=0.01, logprob=-5.03)
        assert why and "incoherent" in why

    def test_qwen3_reports_neither_number_and_is_not_judged_on_them(self):
        """`sherpa` returns None for both. Treating None as 0, or as 1, would either
        disable the gate or mute the default engine entirely."""
        assert off().transcript_reason("tolong maju sedikit", None, None) is None

    def test_the_gates_can_be_opened_all_the_way(self):
        loose = NoiseControl(NoiseSettings(mode="off", max_no_speech=1.0, min_logprob=-90.0))
        assert loose.transcript_reason("anything", no_speech=0.99, logprob=-9.0) is None


class TestTranscriptShape:
    @pytest.mark.parametrize("text", ["(thunder rumbling)", "[Music]", "[ Applause ]",
                                      "♪ ♪", "*coughs*"])
    def test_a_sound_effect_annotation_is_never_a_person(self, text):
        """whisper writes these on audio with no speech in it. `(thunder rumbling)` is
        a real transcript this pipeline produced, from a recording of a quiet room."""
        assert shape_reason(text) is not None

    @pytest.mark.parametrize("text", ["ლლლლლლ", "aaaaaaaa"])
    def test_one_character_to_the_end_of_the_segment_is_a_decoder_loop(self, text):
        assert shape_reason(text) is not None

    def test_the_same_word_over_and_over_is_a_decoder_loop(self):
        assert shape_reason("ya ya ya ya ya") is not None

    @pytest.mark.parametrize("text", [
        "tolong maju sedikit lalu berbelok ke kiri",
        "please move forward a little",
        "من فضلك تقدم قليلا",
        "Terima kasih",          # whisper's favourite hallucination IS a real sentence
        "ya",
        "ya ya",
        "halo halo",
    ])
    def test_things_people_actually_say_survive(self, text):
        assert shape_reason(text) is None

    def test_an_empty_transcript_is_not_reported_as_noise(self):
        """It is already ignored downstream; reporting it would put an empty `ignored`
        card on the dashboard for every pause."""
        assert shape_reason("   ") is None


class TestSuppressionIsOffUntilSomebodyMeasuresIt:
    def test_the_default_mode_is_off(self):
        """Measured on this board, GTCRN in front of whisper made the word error rate
        worse at every SNR tried -- stationary noise and babble alike. If this
        assertion is ever changed, change it with a measurement, not a hunch:
        ./scripts/measure/denoise_ab.py --noise <a recording of the room>.
        """
        assert NoiseSettings().mode == "off"
        assert not off().active

    def test_an_inactive_denoiser_passes_audio_through_untouched(self):
        frame = tone(FRAME / 16000)
        assert off().stream(frame) == [pytest.approx(frame)]
        assert off().utterance(frame) is frame

    def test_a_missing_model_disables_suppression_instead_of_raising(self):
        """The robot must still listen. Refusing to start over an optional 523 KiB file
        would turn a degraded microphone into no microphone."""
        control = NoiseControl(NoiseSettings(mode="stream", model="models/denoise/nope.onnx"))
        assert not control.active
        assert "missing" in control.detail
        assert control.stream(tone(FRAME / 16000))        # still passes audio

    def test_an_unknown_mode_is_refused_rather_than_guessed(self):
        control = NoiseControl(NoiseSettings(mode="denoise-please"))
        assert not control.active and "unknown mode" in control.detail


class TestEnvironment:
    def test_every_knob_can_be_set_from_the_env_file(self, monkeypatch):
        monkeypatch.setenv("ASR_DENOISE", "stream")
        monkeypatch.setenv("ASR_NOISE_GATE_DB", "9")
        monkeypatch.setenv("ASR_MAX_NO_SPEECH", "0.8")
        monkeypatch.setenv("ASR_MIN_LOGPROB", "-2")
        settings = NoiseSettings.from_env()
        assert settings.mode == "stream"
        assert (settings.gate_margin_db, settings.max_no_speech) == (9.0, 0.8)
        assert settings.min_logprob == -2.0
