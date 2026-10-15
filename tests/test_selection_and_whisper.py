"""One answer to "which model", a watched decoder, and small talk that retrieves nothing."""
import json

import pytest

from robot.core import keepalive as ka
from robot.core import selection
from robot.core.health import Health, State, probe_whisper
from robot.core.rag import content_tokens


class TestDecoderIsWatched:
    def test_whisper_has_its_own_row_and_is_watched(self):
        assert "whisper" in Health().services and "whisper" in ka.WATCHED

    def test_decoder_is_revived_before_the_client(self):
        assert ka.WATCHED.index("whisper") < ka.WATCHED.index("asr")

    async def test_a_dead_decoder_is_restarted_while_asr_stays_up(self, monkeypatch):
        h = Health()
        for name in ka.WATCHED:
            h.services[name].state = State.UP
        calls = []

        async def fake_restart(component, timeout=150.0):
            calls.append(component)
            return True, "ok"

        monkeypatch.setattr(ka, "restart_service", fake_restart)
        k = ka.Keepalive(h)
        for _ in range(3):
            h.services["whisper"].state = State.DOWN     # robot-asr's row stays UP
            await k.poll()
        assert calls == ["whisper"]

    def test_probe_reports_silence_for_a_closed_port(self):
        assert probe_whisper("127.0.0.1", 1, timeout=0.3) == "silent"


class TestOneAnswerForTheModel:
    @pytest.fixture
    def tree(self, tmp_path, monkeypatch):
        (tmp_path / "config").mkdir()
        (tmp_path / "run").mkdir()
        (tmp_path / "models").mkdir()
        for name in ("Llama.gguf", "Qwen.gguf"):
            (tmp_path / "models" / name).write_bytes(b"x")
        (tmp_path / "config/llm.env").write_text(
            f"LLM_MODEL={tmp_path}/models/Qwen.gguf\nLLM_CTX=2048\n")
        (tmp_path / "config/whisper.env").write_text("WHISPER_MODEL=none\n")
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_the_pin_beats_the_env_file(self, tree):
        (tree / "run/settings.json").write_text(
            json.dumps({"llm_model": f"{tree}/models/Llama.gguf"}))
        assert selection.effective("llm").endswith("Llama.gguf")

    def test_sync_makes_the_file_say_what_the_page_says(self, tree):
        (tree / "run/settings.json").write_text(
            json.dumps({"llm_model": f"{tree}/models/Llama.gguf"}))
        notes = selection.sync()
        assert notes and "Llama.gguf" in notes[0]
        assert f"LLM_MODEL={tree}/models/Llama.gguf" in (tree / "config/llm.env").read_text()
        assert "LLM_CTX=2048" in (tree / "config/llm.env").read_text()   # nothing else touched
        assert selection.sync() == []                                    # idempotent

    def test_a_pin_to_a_missing_file_is_not_applied(self, tree):
        (tree / "run/settings.json").write_text(
            json.dumps({"llm_model": f"{tree}/models/Gone.gguf"}))
        before = (tree / "config/llm.env").read_text()
        notes = selection.sync()
        assert (tree / "config/llm.env").read_text() == before
        assert notes and "not installed" in notes[0]
        assert selection.effective("llm").endswith("Qwen.gguf")


class TestSmallTalkRetrievesNothing:
    @pytest.mark.parametrize("q", ["Halo, apa kabar?", "Apa yang bisa kamu lakukan?",
                                   "Ceritakan tentang dirimu secara singkat.",
                                   "Hello, how are you?", "Terima kasih", "Yeah."])
    def test_chit_chat_has_no_content_words(self, q):
        assert content_tokens(q) == []

    @pytest.mark.parametrize("q,expected", [
        ("Siapa pendiri pondok modern Tazakka?", {"pendiri", "pondok", "modern", "tazakka"}),
        ("Apa itu Tazakka?", {"tazakka"}),
        ("What is Tazakka?", {"tazakka"}),
        ("Di mana lokasi Tazakka?", {"lokasi", "tazakka"}),
    ])
    def test_real_questions_keep_their_subject(self, q, expected):
        assert expected <= set(content_tokens(q))


class TestAFailedRestartIsRetried:
    async def test_state_returns_to_down_so_the_backoff_can_run(self, monkeypatch):
        h = Health()
        for name in ka.WATCHED:
            h.services[name].state = State.UP
        calls = []

        async def failing(component, timeout=150.0):
            calls.append(component)
            return False, "failed to allocate CUDA0 buffer"

        monkeypatch.setattr(ka, "restart_service", failing)
        k = ka.Keepalive(h)
        k.BACKOFF_S = (0.0,)           # no waiting in a test
        h.services["whisper"].state = State.DOWN
        for _ in range(8):
            await k.poll()
            assert h.services["whisper"].state is State.DOWN, \
                "left at RESTARTING, so keepalive would never look again"
            h.services["whisper"].state = State.DOWN
        assert len(calls) >= 3, f"only {len(calls)} attempt(s)"


class TestAClientThatNeverConnectedIsNotInvisible:
    def _svc(self, uptime, running, monkeypatch):
        import time as _t
        from robot.core import app as app_mod
        svc = app_mod.CoreService.__new__(app_mod.CoreService)
        svc.health = Health()
        svc._started_at = _t.monotonic() - uptime
        monkeypatch.setattr(app_mod.services, "is_running", lambda name: running)
        return svc

    def test_unknown_past_the_grace_and_not_running_is_down(self, monkeypatch):
        svc = self._svc(120, False, monkeypatch)
        svc._poll_never_connected()
        assert svc.health.services["asr"].state is State.DOWN

    def test_still_inside_the_start_up_grace_it_is_left_alone(self, monkeypatch):
        svc = self._svc(5, False, monkeypatch)
        svc._poll_never_connected()
        assert svc.health.services["asr"].state is State.UNKNOWN

    def test_a_running_process_that_has_not_connected_is_slow_not_dead(self, monkeypatch):
        svc = self._svc(120, True, monkeypatch)
        svc._poll_never_connected()
        assert svc.health.services["asr"].state is State.UNKNOWN
