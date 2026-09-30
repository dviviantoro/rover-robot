"""The model catalogue and the memory budget.

These numbers drive a page that people will use to decide what to load on a board with
about a gigabyte to spare, so the estimator is checked against what was actually
measured in M1, M3 and M7 rather than against itself.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from robot.core import catalog


def write_gguf(path: Path, arch: str = "qwen3", *, layers: int = 36,
               kv_heads: int = 8, key_length: int = 128, padding: int = 0) -> Path:
    """A minimal but structurally valid GGUF header, including a long token array."""
    out = bytearray(b"GGUF")
    out += struct.pack("<I", 3)          # version
    out += struct.pack("<Q", 0)          # tensor count
    entries: list[tuple[str, int, object]] = [
        ("general.architecture", 8, arch),
        (f"{arch}.block_count", 4, layers),
        (f"{arch}.attention.head_count_kv", 4, kv_heads),
        (f"{arch}.attention.key_length", 4, key_length),
        # A long string array, like a real vocabulary: the reader must skip it by
        # reading through it, not by returning early.
        ("tokenizer.ggml.tokens", 9, [f"tok{i}" for i in range(300)]),
        ("general.name", 8, "test model"),
    ]
    out += struct.pack("<Q", len(entries))
    for key, kind, value in entries:
        out += struct.pack("<Q", len(key)) + key.encode()
        out += struct.pack("<I", kind)
        if kind == 8:
            out += struct.pack("<Q", len(value)) + value.encode()
        elif kind == 4:
            out += struct.pack("<I", value)
        elif kind == 9:
            out += struct.pack("<I", 8) + struct.pack("<Q", len(value))
            for item in value:
                out += struct.pack("<Q", len(item)) + item.encode()
    out += b"\x00" * padding
    path.write_bytes(bytes(out))
    return path


class TestGgufReader:
    def test_reads_the_keys_that_matter(self, tmp_path):
        meta = catalog.read_gguf_metadata(write_gguf(tmp_path / "m.gguf"))
        assert meta["general.architecture"] == "qwen3"
        assert meta["qwen3.block_count"] == 36

    def test_a_long_token_array_does_not_derail_the_parse(self, tmp_path):
        """Returning early from a string array leaves the file position mid-array, and
        every later key is then read from garbage -- which once tried to allocate the
        whole file and was OOM-killed."""
        meta = catalog.read_gguf_metadata(write_gguf(tmp_path / "m.gguf"))
        assert meta["general.name"] == "test model", "keys after the array were lost"

    def test_a_non_gguf_file_is_ignored_not_fatal(self, tmp_path):
        junk = tmp_path / "x.gguf"
        junk.write_bytes(b"not a gguf at all")
        assert catalog.read_gguf_metadata(junk) == {}

    def test_a_truncated_file_is_ignored_not_fatal(self, tmp_path):
        path = write_gguf(tmp_path / "m.gguf")
        path.write_bytes(path.read_bytes()[:40])
        assert catalog.read_gguf_metadata(path) == {}


class TestKvSizing:
    def test_qwen3_4b_matches_the_measurement(self, tmp_path):
        """M1 measured exactly 144 KiB/token for Qwen3-4B (36 layers, 8 kv heads, 128)."""
        meta = catalog.read_gguf_metadata(write_gguf(tmp_path / "m.gguf"))
        assert catalog.kv_bytes_per_token(meta) / 1024 == 144.0

    def test_kv_scales_with_context(self, tmp_path):
        path = write_gguf(tmp_path / "m.gguf", padding=1024)
        small, _ = catalog.llm_ram_mib(path, 2048)
        large, _ = catalog.llm_ram_mib(path, 4096)
        # 144 KiB/token x 2048 extra tokens = 288 MiB, the figure memory-budget.md cites.
        assert 287 < large - small < 289

    def test_an_unreadable_header_still_produces_an_estimate(self, tmp_path):
        junk = tmp_path / "x.gguf"
        junk.write_bytes(b"0" * (4 * 1024 * 1024))
        ram, detail = catalog.llm_ram_mib(junk, 2048)
        assert ram > catalog.LLM_RUNTIME_OVERHEAD_MIB
        assert "no readable header" in detail


@pytest.fixture
def models(tmp_path):
    (tmp_path / "llm").mkdir()
    write_gguf(tmp_path / "llm" / "big.gguf", padding=2000 * 1024 * 1024 // 1000)
    write_gguf(tmp_path / "llm" / "small.gguf", layers=28, kv_heads=8, key_length=128)
    # The only ASR models there are: whisper.cpp checkpoints, which the catalogue has to
    # list smallest-first because the choice IS the size.
    whisper = tmp_path / "asr" / "whisper"
    whisper.mkdir(parents=True)
    for name, mib in (("ggml-tiny.bin", 1), ("ggml-base.bin", 2), ("ggml-small.bin", 6)):
        (whisper / name).write_bytes(b"0" * mib * 1024 * 1024)
    voices = tmp_path / "tts" / "piper"
    voices.mkdir(parents=True)
    for name in ("en_US-a-medium", "id_ID-b-medium", "ar_JO-c-medium"):
        (voices / f"{name}.onnx").write_bytes(b"0" * 1024 * 1024)
        (voices / f"{name}.onnx.json").write_text("{}")
    # The other speaker: one model covering all three languages, so the catalogue has
    # to price it as one line rather than per voice.
    supertonic = tmp_path / "tts" / "supertonic"
    (supertonic / "onnx").mkdir(parents=True)
    (supertonic / "voice_styles").mkdir(parents=True)
    (supertonic / "onnx" / "vocoder.onnx").write_bytes(b"0" * 1024 * 1024)
    for style in ("F1", "M1"):
        (supertonic / "voice_styles" / f"{style}.json").write_text("{}")
    return catalog.scan(tmp_path, llm_ctx=2048)


class TestScan:
    def test_finds_every_kind(self, models):
        kinds = {m.kind for m in models}
        assert {"llm", "asr", "tts"} <= kinds

    def test_every_entry_is_priced(self, models):
        assert all(m.ram_mib > 0 for m in models)

    def test_a_partial_download_is_not_offered(self, tmp_path):
        (tmp_path / "llm").mkdir()
        write_gguf(tmp_path / "llm" / "half.gguf")
        (tmp_path / "llm" / "half.gguf.part").write_bytes(b"")
        # A file still downloading would be priced from its partial size.
        assert not [m for m in catalog.scan(tmp_path) if m.name == "half"]

    def test_a_voice_without_its_config_is_skipped(self, tmp_path):
        voices = tmp_path / "tts" / "piper"
        voices.mkdir(parents=True)
        (voices / "orphan.onnx").write_bytes(b"0" * 1024)
        assert not catalog.scan(tmp_path)

    def test_a_missing_models_directory_is_empty_not_an_error(self, tmp_path):
        assert catalog.scan(tmp_path / "nope") == []


class TestBudget:
    def test_ceiling_is_the_real_one(self, models):
        assert catalog.budget(models, {})["ceiling_mib"] == 7620

    def test_the_os_is_always_counted(self, models):
        b = catalog.budget(models, {})
        assert any(i["kind"] == "os" for i in b["items"])

    def test_a_bigger_model_costs_more(self, models):
        big = next(m for m in models if m.name == "big")
        small = next(m for m in models if m.name == "small")
        cheap = catalog.budget(models, {"llm_model": small.path})
        dear = catalog.budget(models, {"llm_model": big.path})
        assert dear["steady_mib"] > cheap["steady_mib"]

    def test_context_size_changes_the_total(self, models):
        big = next(m for m in models if m.name == "big")
        narrow = catalog.budget(models, {"llm_model": big.path}, llm_ctx=1024)
        wide = catalog.budget(models, {"llm_model": big.path}, llm_ctx=8192)
        assert wide["steady_mib"] > narrow["steady_mib"]

    def test_peak_exceeds_steady_when_an_asr_is_selected(self, models):
        asr = next(m for m in models if m.kind == "asr")   # whisper, decoding
        b = catalog.budget(models, {"asr_model": asr.path})
        assert b["peak_mib"] == b["steady_mib"] + catalog.WHISPER_DECODE_PEAK_MIB

    def test_voices_add_up(self, models):
        """Piper is priced per voice, so three languages cost three checkpoints.

        The engine is pinned in the selection rather than read from `config/tts.env`:
        this asserts a property of Piper pricing, and it must not change its meaning
        because the machine running the tests happens to be configured for Supertonic.
        """
        names = [m.name for m in models if m.kind == "tts" and m.name != "supertonic"]
        one = catalog.budget(models, {"voices": names[:1], "tts_engine": "piper"})
        three = catalog.budget(models, {"voices": names, "tts_engine": "piper"})
        assert three["steady_mib"] > one["steady_mib"]

    def test_supertonic_is_one_line_however_many_languages(self, models):
        """One model covering all three, so pricing it per voice would be wrong twice:
        three times too much, or once too little."""
        if not any(m.name == "supertonic" for m in models):
            pytest.skip("Supertonic not installed")
        names = [m.name for m in models if m.kind == "tts"]
        b = catalog.budget(models, {"voices": names, "tts_engine": "supertonic"})
        tts_lines = [i for i in b["items"] if i["kind"] == "tts"]
        assert len(tts_lines) == 1
        supertonic = next(m for m in models if m.name == "supertonic")
        assert tts_lines[0]["mib"] == round(supertonic.ram_mib)

    def test_the_gpu_context_is_priced_into_the_supertonic_line(self, tmp_path, monkeypatch):
        """It was not, and the Memory page said a selection fit while the board swapped.

        884 MiB is nearly twice whisper's CUDA context. Leaving it out of the estimate
        made every GPU synthesis selection look ~900 MiB cheaper than it is -- caught
        only once the page started showing measured RSS beside the projection, where the
        TTS line read 484 estimated against 1720 measured.
        """
        config = tmp_path / "tts.env"
        config.write_text("TTS_USE_GPU=1\n")
        assert catalog.tts_on_gpu(str(config)) is True
        config.write_text("TTS_USE_GPU=0\n")
        assert catalog.tts_on_gpu(str(config)) is False
        assert catalog.tts_on_gpu(str(tmp_path / "absent.env")) is False

    def test_the_engine_choice_changes_the_tts_price(self, models):
        if not any(m.name == "supertonic" for m in models):
            pytest.skip("Supertonic not installed")
        piper_names = [m.name for m in models
                       if m.kind == "tts" and m.name != "supertonic"][:1]
        piper = catalog.budget(models, {"voices": piper_names, "tts_engine": "piper"})
        single = catalog.budget(models, {"voices": piper_names,
                                         "tts_engine": "supertonic"})
        assert single["steady_mib"] != piper["steady_mib"]

    def test_an_impossible_selection_is_reported_as_not_fitting(self, models):
        b = catalog.budget(models, {})
        b_over = dict(b)
        # Price something far past the ceiling by hand to check the verdict logic.
        huge = catalog.Model(id="x", kind="llm", name="huge", path="/x",
                             size_mib=20000, ram_mib=20000)
        over = catalog.budget([huge], {"llm_model": "/x"})
        assert not over["fits"] and over["headroom_mib"] < 0
        assert b["fits"]

    def test_an_unknown_model_is_simply_absent(self, models):
        b = catalog.budget(models, {"llm_model": "/does/not/exist.gguf"})
        assert not any(i["kind"] == "llm" for i in b["items"])

    def test_dashboard_can_be_excluded(self, models):
        with_ui = catalog.budget(models, {})
        without = catalog.budget(models, {}, include_dashboard=False)
        assert with_ui["steady_mib"] - without["steady_mib"] == catalog.DASHBOARD_MIB


class TestChoosingAListeningModel:
    """Picking an ASR model is now purely picking a whisper checkpoint SIZE.

    It used to pick an engine as well -- sherpa exports and whisper checkpoints were
    listed under one `asr` kind because they competed for the same slot in the budget,
    and writing the chosen path without also switching ASR_ENGINE left the engine
    pointing at a model it could not read. Qwen3-ASR was removed on 2026-09-14 and that
    whole class of disagreement went with it. What remains is that the write lands in
    the file whisper-server actually reads, and that the two services restart in order.
    """

    def test_the_choice_is_written_where_whisper_server_reads_it(self):
        updates = dict(((f, k), v) for f, k, v in
                       catalog.asr_env_updates("models/asr/whisper/ggml-small.bin"))
        assert updates[("config/whisper.env", "WHISPER_MODEL")].endswith("ggml-small.bin")

    def test_nothing_is_written_to_the_old_engine_keys(self):
        """ASR_ENGINE and ASR_MODEL_DIR are gone from config/asr.env. Writing them back
        would resurrect a variable nothing reads, which is how a config starts lying."""
        keys = [(f, k) for f, k, _ in catalog.asr_env_updates("models/asr/whisper/ggml-tiny.bin")]
        assert ("config/asr.env", "ASR_ENGINE") not in keys
        assert ("config/asr.env", "ASR_MODEL_DIR") not in keys

    def test_one_write_per_choice(self):
        assert len(catalog.asr_env_updates("models/asr/whisper/ggml-base.bin")) == 1

    def test_both_services_restart_and_none_is_stopped(self):
        start, stop = catalog.asr_services_for("models/asr/whisper/ggml-base.bin")
        assert start == ["whisper", "asr"] and stop == []

    def test_whisper_comes_up_before_the_listener_reconnects_to_it(self):
        start, _ = catalog.asr_services_for("models/asr/whisper/ggml-base.bin")
        assert start.index("whisper") < start.index("asr")
