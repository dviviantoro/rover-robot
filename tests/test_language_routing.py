"""Language routing: the part of the multilingual logic that needs no hardware.

dev-docs/architecture/system-overview.md § Language routing.
"""
from __future__ import annotations

import pytest

from robot.asr.recognizer import script_of
from robot.tts import voices as voicemap


class TestScriptDetection:
    """`script_of` is the M3 stopgap for a missing ASR language tag.

    It must reliably pick out Arabic, and must be honest that it cannot tell
    English from Indonesian -- both are Latin script.
    """

    def test_arabic(self):
        assert script_of("من فضلك تقدم قليلا ثم استدر إلى اليسار.") == "arab"

    def test_english(self):
        assert script_of("Please move forward a little.") == "latn"

    def test_indonesian_is_indistinguishable_from_english(self):
        assert script_of("Tolong maju sedikit lalu berbelok ke kiri.") == "latn"

    def test_empty_and_punctuation_only(self):
        assert script_of("") is None
        assert script_of("... !? 123") is None

    def test_mixed_script_takes_the_majority(self):
        assert script_of("OK من فضلك تقدم قليلا ثم استدر إلى اليسار") == "arab"


class TestVoiceResolution:
    @pytest.fixture
    def voices(self):
        return {
            lang: voicemap.Voice(lang=lang, name=f"{lang}-voice", onnx=None, config=None)
            for lang in voicemap.SUPPORTED
        }

    @pytest.mark.parametrize("tag", ["en", "id", "ar"])
    def test_supported_tags_resolve_exactly(self, voices, tag):
        voice, fell_back = voicemap.resolve(voices, tag)
        assert voice.lang == tag
        assert not fell_back

    @pytest.mark.parametrize("tag", ["en-US", "EN", "id_ID", "ar-JO"])
    def test_region_and_case_variants_normalise(self, voices, tag):
        voice, fell_back = voicemap.resolve(voices, tag)
        assert voice.lang == tag.split("-")[0].split("_")[0].lower()
        assert not fell_back

    @pytest.mark.parametrize("tag", ["de", "zh", "fr", "", None])
    def test_unknown_tag_falls_back_rather_than_raising(self, voices, tag):
        """Qwen3-ASR knows far more languages than we have voices for."""
        voice, fell_back = voicemap.resolve(voices, tag)
        assert voice.lang == voicemap.FALLBACK
        assert fell_back

    def test_no_voices_at_all_is_an_error(self):
        with pytest.raises(RuntimeError, match="fetch_tts"):
            voicemap.resolve({}, "en")
