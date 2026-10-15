"""The settings registry and the health model (ROADMAP M8).

The registry is the single source of truth for which settings apply live and which
need a restart -- the dashboard groups from it and the orchestrator dispatches from it.
These tests guard that invariant, because getting it wrong means a control that claims
to apply immediately but silently does not.
"""
from __future__ import annotations

import pytest

from robot.core.health import Health, State
from robot.core.settings import (
    BY_KEY, HOT, REGISTRY, RESTART, Kind, Setting, Settings, describe)


class TestRegistryInvariants:
    def test_every_setting_is_in_exactly_one_group(self):
        assert len(HOT) + len(RESTART) == len(REGISTRY)
        assert not ({s.key for s in HOT} & {s.key for s in RESTART})

    def test_restart_settings_name_the_component_their_button_restarts(self):
        for s in RESTART:
            assert s.component, f"{s.key} has no component"

    def test_hot_settings_claim_no_component(self):
        """A hot-reload control must not secretly belong to something restartable."""
        for s in HOT:
            assert s.component is None, f"{s.key} is hot but names {s.component}"

    def test_the_roadmap_restart_trio_is_present(self):
        """The three the roadmap named, plus `tts` -- which joined them when synthesis
        gained a GPU path, because switching accelerator means rebuilding the ONNX
        sessions and there is no way to do that inside a running utterance."""
        assert {"llm", "asr", "serial"} <= {s.component for s in RESTART}

    def test_every_restart_component_can_actually_be_restarted(self):
        """A button that names a component nothing knows how to bounce is a button that
        reports success and changes nothing."""
        from robot.common.services import SERVICES
        restartable = {s.key for s in SERVICES} | {"serial"}   # serial is in-process
        for s in RESTART:
            assert s.component in restartable, f"{s.key} restarts unknown {s.component}"

    def test_the_roadmap_hot_settings_are_present(self):
        keys = {s.key for s in HOT}
        assert {"persona", "temperature", "vad_threshold"} <= keys
        assert {"voice_en", "voice_id", "voice_ar"} <= keys

    def test_the_listening_modes_are_tunable_without_a_restart(self):
        """The robot hearing itself is something you fix while talking to it, not by
        editing a file and bouncing a service."""
        keys = {s.key for s in HOT}
        assert {"listen_mode", "listen_tail_ms", "wake_word"} <= keys
        assert BY_KEY["listen_mode"].options == ("interrupt", "no_interrupt", "wake_word")
        assert BY_KEY["listen_mode"].group == "listening"

    def test_the_reserved_wake_word_control_is_gone(self):
        """It promised an acoustic keyword spotter that was never adopted. `wake_word`
        does the job against the transcript instead, for no model and no memory."""
        assert "wakeword_sensitivity" not in BY_KEY

    def test_unit_restarts_know_where_to_write_the_new_value(self):
        """A restarted service reads its config from an env file, so it must be named."""
        for s in RESTART:
            if s.component != "serial":     # serial reconnects in-process
                assert s.env_file and s.env_key, f"{s.key} cannot persist its value"

    def test_every_setting_is_applied_by_a_known_process(self):
        for s in REGISTRY:
            assert s.where in ("core", "asr", "tts")

    def test_describe_is_serialisable_and_complete(self):
        rows = describe()
        assert len(rows) == len(REGISTRY)
        for row in rows:
            assert row["kind"] in ("hot", "restart")
            assert isinstance(row["label"], str) and row["label"]

    def test_every_control_offered_is_one_that_works(self):
        """A disabled control promising a feature is worse than no control. If one is
        ever added back it must carry the reason, which is what `set` reports."""
        for s in REGISTRY:
            assert s.enabled or s.disabled_reason, s.key


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "settings.json")


class TestSettings:
    def test_defaults_come_from_the_registry(self, settings):
        assert settings.get("temperature") == 0.7
        assert settings.get("voice_en") == "en_US-lessac-medium"

    def test_numbers_are_clamped_rather_than_rejected(self, settings):
        assert settings.set("temperature", 99) == 1.5
        assert settings.set("temperature", -5) == 0.0

    def test_integer_steps_round_to_integers(self, settings):
        assert settings.set("max_tokens", 200.7) == 201
        assert isinstance(settings.set("rag_k", 3.0), int)

    def test_a_disabled_setting_is_refused_with_its_reason(self, settings, monkeypatch):
        """Tested against a synthetic setting rather than whichever one happens to be
        disabled today -- the mechanism has to keep working when none are."""
        blocked = Setting("blocked", "Blocked", Kind.HOT, "core", "number", default=1,
                          enabled=False, disabled_reason="the hardware is not attached")
        monkeypatch.setitem(BY_KEY, "blocked", blocked)
        with pytest.raises(ValueError, match="hardware is not attached"):
            settings.set("blocked", 0.9)

    def test_unknown_keys_are_refused(self, settings):
        with pytest.raises(KeyError):
            settings.set("launch_missiles", True)

    def test_values_survive_a_restart(self, tmp_path):
        a = Settings(tmp_path / "s.json")
        a.set("persona", "You are a very quiet robot.")
        b = Settings(tmp_path / "s.json")
        assert b.get("persona") == "You are a very quiet robot."

    def test_a_corrupt_file_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("{ this is not json")
        assert Settings(path).get("temperature") == 0.7

    def test_stored_keys_that_no_longer_exist_are_dropped(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text('{"temperature": 0.3, "removed_setting": 1}')
        s = Settings(path)
        assert s.get("temperature") == 0.3
        assert "removed_setting" not in s.all()


class TestHealth:
    def test_every_service_the_roadmap_names_has_a_row(self):
        names = set(Health().services)
        assert {"wakeword", "asr", "llm", "tts", "serial"} <= names

    def test_absent_services_are_not_reported_as_faults(self):
        """No wake word exists yet. That is not a failure, and must not look like one."""
        h = Health()
        assert h.services["wakeword"].state is State.ABSENT
        h.touch("wakeword", State.DOWN)
        assert h.services["wakeword"].state is State.ABSENT

    def test_touch_records_a_heartbeat(self):
        h = Health()
        h.touch("llm", State.UP)
        row = next(r for r in h.snapshot() if r["name"] == "llm")
        assert row["state"] == "up" and row["last_seen_s"] is not None

    def test_errors_are_kept_after_recovery(self):
        """The panel shows 'last error' -- it must outlive the failure."""
        h = Health()
        h.error("tts", "aplay exited early")
        h.touch("tts", State.UP)
        row = next(r for r in h.snapshot() if r["name"] == "tts")
        assert row["state"] == "up" and row["last_error"] == "aplay exited early"

    def test_snapshot_is_json_safe(self):
        import json
        json.dumps(Health().snapshot())



class TestWhichPageOwnsWhat:
    """Choosing a model belongs where the cost of the choice is shown.

    Config offered the same model dropdowns as the Memory page, with none of the budget
    context and without applying the choice -- two places to change one value, and two
    places for it to sit unapplied. The registry decides; both pages render only what
    they own.
    """

    def test_models_are_chosen_on_the_memory_page(self):
        pages = {s["key"]: s["page"] for s in describe()}
        assert pages["llm_model"] == "memory"
        assert pages["asr_model"] == "memory"

    def test_the_serial_port_stays_on_config(self):
        """Not a model, has no memory cost, and nothing on the Memory page prices it."""
        pages = {s["key"]: s["page"] for s in describe()}
        assert pages["serial_port"] == "config"

    # The pages that own controls. A typo here hides a control from every page at once,
    # so the set is written down rather than inferred.
    PAGES = ("config", "memory", "knowledge")

    def test_every_setting_declares_a_page(self):
        for row in describe():
            assert row["page"] in self.PAGES, row["key"]

    def test_a_control_lives_where_its_context_is(self):
        """A setting belongs on the page that can tell you whether the value is right.

        Model choices are on Memory, which prices them against the ceiling. The persona
        is on Knowledge, beside the documents it reasons over -- it is the standing
        instruction the robot carries into every turn, not a tuning dial. Everything that
        needs no such context stays on Config.
        """
        expected = {"llm_model": "memory", "asr_model": "memory",
                    "persona": "knowledge"}
        for row in describe():
            assert row["page"] == expected.get(row["key"], "config"), row["key"]


class TestRestartSettingsReportTheFileThatGovernsThem:
    """A restart-scoped setting is applied by an env file, not by this process.

    So the registry must not carry a concrete default for one: `Settings.all()` returns
    the default for anything unset, the dashboard shows that as the current value, and
    it then disagrees with the file the service actually reads. That is the same class
    of lie the Memory page exists to kill -- three layers, and the one on screen not
    being the one that runs.
    """

    def test_none_of_them_claim_a_default(self):
        for s in RESTART:
            assert s.default is None, (
                f"{s.key} defaults to {s.default!r}; the value in {s.env_file} governs, "
                f"and a default here is reported as current and disagrees with it")

    def test_each_one_names_where_its_value_really_lives(self):
        """Except `serial`, which this process owns and reconnects in place."""
        for s in RESTART:
            if s.component == "serial":
                continue
            assert s.env_file and s.env_key, f"{s.key} names no config file"


class TestEveryTabHasSomethingInIt:
    """Moving the STM32 port into Hardware left `body` as a tab with no controls.

    The Config page renders a tab per group, so an empty group is a tab that opens on
    nothing -- and nothing in the registry notices.
    """

    def test_no_group_is_empty(self):
        from robot.core.settings import GROUPS, REGISTRY
        used = {s.group for s in REGISTRY}
        empty = [key for key, _, _ in GROUPS if key not in used]
        assert not empty, f"tabs with no controls: {empty}"

    def test_no_control_is_filed_under_a_tab_that_does_not_exist(self):
        """The other direction: a typo in `group=` hides the control completely."""
        from robot.core.settings import GROUPS, REGISTRY
        known = {key for key, _, _ in GROUPS}
        orphans = sorted({s.key for s in REGISTRY if s.group not in known})
        assert not orphans, f"controls in no tab: {orphans}"
