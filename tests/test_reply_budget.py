"""How much room the reply gets, and what happens when it runs out.

The complaint was "incomplete answers". Three separate things caused it, and each one
could cause it again:

  * the reply budget was a flat 200 tokens with no relationship to `LLM_CTX`, so raising
    the context window bought headroom nothing was allowed to use
  * `finish_reason` was read from llama.cpp and thrown away, so a reply cut off at the
    cap was indistinguishable from one that finished
  * the registry default for `max_tokens` was applied over the config on every startup,
    so `CORE_MAX_TOKENS` in config/core.env did nothing at all

Under the JSON reply format a truncated reply is worse than short: the grammar is still
inside the `answer` string, so the object never closes and every body field after it --
language, face, the movement -- is never generated.
"""
from __future__ import annotations

import pytest

from robot.core.app import REPLY_CEILING_TOKENS, CTX_SAFETY_TOKENS, reply_budget
from robot.core.llm import Outcome
from robot.core.settings import BY_KEY, Kind


def prompt_budget(ctx: int) -> int:
    """The same two-thirds split robot-core uses."""
    return max(256, int(ctx * 0.66))


class TestTheBudgetFollowsTheContext:
    def test_a_bigger_window_buys_a_longer_answer(self):
        """The whole point. 2048 -> 4096 used to change nothing about reply length."""
        small = reply_budget("auto", 2048, prompt_budget(2048))
        large = reply_budget("auto", 4096, prompt_budget(4096))
        assert large > small

    def test_it_never_claims_more_than_the_window_has_left(self):
        for ctx in (1024, 2048, 4096, 8192):
            budget = reply_budget("auto", ctx, prompt_budget(ctx))
            assert budget + prompt_budget(ctx) + CTX_SAFETY_TOKENS <= max(ctx, budget + 1)

    def test_a_derived_answer_stays_an_answer(self):
        """A 16k window would otherwise authorise a fifteen-minute monologue."""
        assert reply_budget("auto", 32768, prompt_budget(32768)) == REPLY_CEILING_TOKENS

    def test_a_tiny_window_still_leaves_something_to_say(self):
        """Better a short reply than a budget of nine tokens."""
        assert reply_budget("auto", 512, prompt_budget(512)) >= 128


class TestAnExplicitNumberWins:
    @pytest.mark.parametrize("given", [260, "260", " 260 "])
    def test_a_number_is_taken_at_face_value(self, given):
        """Someone who typed a number meant it -- including a number lower than the
        derived one, which is the way to make replies deliberately shorter."""
        assert reply_budget(given, 4096, prompt_budget(4096)) == 260

    @pytest.mark.parametrize("given", ["auto", "AUTO", "", "  ", "none", "0"])
    def test_the_ways_of_asking_for_derived(self, given):
        assert reply_budget(given, 4096, prompt_budget(4096)) == REPLY_CEILING_TOKENS

    def test_nonsense_falls_back_to_derived_rather_than_crashing(self):
        """robot-core must start. A typo in an env file is not worth a dead robot."""
        assert reply_budget("two hundred", 4096, prompt_budget(4096)) == REPLY_CEILING_TOKENS

    def test_an_absurdly_small_explicit_value_is_floored(self):
        assert reply_budget("1", 4096, prompt_budget(4096)) == 32


class TestTheRegistryDoesNotOverrideTheConfig:
    def test_max_tokens_has_no_concrete_default(self):
        """A concrete default here is applied over the derived value on every startup.

        That is exactly what made `CORE_MAX_TOKENS=260` in config/core.env inert: the
        file said 260, the registry said 200, and the registry won without a word. Every
        restart-scoped setting uses `default=None` for the same reason; this one is
        hot-reloadable but is still not the thing that decides.
        """
        assert BY_KEY["max_tokens"].default is None

    def test_it_is_still_hot_reloadable(self):
        assert BY_KEY["max_tokens"].kind is Kind.HOT

    def test_the_ceiling_leaves_room_to_set_it_higher_by_hand(self):
        assert BY_KEY["max_tokens"].maximum >= REPLY_CEILING_TOKENS


class TestTruncationIsReported:
    def test_length_means_it_ran_out_of_budget(self):
        assert Outcome(finish_reason="length").truncated is True

    def test_stop_means_it_finished(self):
        assert Outcome(finish_reason="stop").truncated is False

    def test_a_server_that_reports_nothing_is_not_called_truncated(self):
        """Absent must not read as broken -- a cancelled stream (barge-in) never sends a
        finish_reason, and calling every interruption a truncation would bury the real
        ones."""
        assert Outcome().truncated is False
        assert Outcome(finish_reason=None).truncated is False


class TestTheRegistryNeverOverwritesTheConfigFiles:
    """Applying registry defaults at startup is how env files quietly stop working.

    It happened twice with the same shape: `CORE_MAX_TOKENS=260` in config/core.env did
    nothing because the registry said 200, and then `CORE_FIRST_MAX_CHARS=40` did nothing
    because the registry said 90. In both cases the file said one thing, the process did
    another, and there was no way to tell from either end.
    """

    def test_saved_returns_only_what_was_chosen(self, tmp_path):
        from robot.core.settings import Settings
        settings = Settings(tmp_path / "settings.json")
        assert settings.saved() == {}
        assert settings.all()          # defaults are still there for display
        settings.set("temperature", 0.5)
        assert settings.saved() == {"temperature": 0.5}

    def test_all_still_fills_in_defaults_for_display(self, tmp_path):
        from robot.core.settings import Settings
        settings = Settings(tmp_path / "settings.json")
        assert settings.all()["temperature"] == BY_KEY["temperature"].default

    @pytest.mark.parametrize("key", ["max_tokens", "first_max_chars", "first_min_chars"])
    def test_env_backed_settings_declare_no_default(self, key):
        """Their real value comes from config/core.env, so the registry must not claim
        one -- `saved()` protects the startup path, and this protects the report."""
        assert BY_KEY[key].default is None


class TestAsrSettingsSurviveTheStartOrder:
    """Settings tuned for robot-asr have to be pushed when it CONNECTS, not at startup.

    `run_stack.sh` starts robot-core before robot-asr. Applying the saved settings once
    during core's startup therefore sent them to a socket nothing was listening on, and
    the failure was swallowed by the `suppress(Exception)` around it. Measured on the
    robot: `vad_min_silence` pinned at 0.2 s was running at its env-file default of
    0.65 -- 450 ms of dead time on the front of every turn, on a 1500 ms target, for as
    long as the pin had existed.
    """

    def test_every_asr_setting_has_a_wire_name(self):
        """A setting the Config page can change but `configure` cannot carry is a
        control that does nothing."""
        from robot.core.app import CoreService
        from robot.core.settings import HOT
        for setting in HOT:
            if setting.where == "asr":
                assert setting.key in CoreService._ASR_KEYS, setting.key

    def test_the_wire_names_are_the_ones_robot_asr_accepts(self):
        """Checked against the handler's own list rather than a copy of it."""
        from robot.core.app import CoreService
        import inspect
        from robot.asr import service as asr_service
        source = inspect.getsource(asr_service.ASRService.handle)
        for wire in CoreService._ASR_KEYS.values():
            assert f'"{wire}"' in source, wire
