"""The supervised-service table, and who gets asked to restart what.

These are the parts that decide which process gets killed and which command line runs,
so they are tested without a supervisor anywhere near them: process matching is a pure
function of a command line and an executable, and the command mapping is a pure function
of the service and the mode.
"""
from __future__ import annotations

import pytest

from robot.common import services


class TestProcessMatching:
    """`pgrep -f`-style matching is how you kill someone's editor. Three parts, always."""

    def test_the_two_llama_servers_are_told_apart(self):
        llm = "/root/vendor/llama.cpp/build/bin/llama-server --model llm.gguf --ctx-size 2048"
        embed = ("/root/vendor/llama.cpp/build/bin/llama-server --model e.gguf "
                 "--embeddings --pooling last")
        exe = "/root/vendor/llama.cpp/build/bin/llama-server"
        assert services.BY_KEY["llm"].matches(llm, exe)
        assert not services.BY_KEY["llm"].matches(embed, exe)
        assert services.BY_KEY["embed"].matches(embed, exe)
        assert not services.BY_KEY["embed"].matches(llm, exe)

    def test_an_editor_holding_the_file_open_is_not_the_service(self):
        """The bug this guards: matching a command line alone kills the wrong process."""
        cmdline = "vim /root/src/robot/asr/service.py robot.asr.service"
        assert not services.BY_KEY["asr"].matches(cmdline, "/usr/bin/vim")

    def test_a_log_tail_that_mentions_a_service_is_not_the_service(self):
        cmdline = "tail -f /root/.cache/tmp/llama-server.log"
        assert not services.BY_KEY["llm"].matches(cmdline, "/usr/bin/tail")

    def test_the_real_python_services_match(self):
        exe = "/root/.venv/bin/python3.10"
        assert services.BY_KEY["core"].matches("/root/.venv/bin/python -m robot.core.app", exe)
        assert services.BY_KEY["tts"].matches("/root/.venv/bin/python -m robot.tts.service", exe)
        assert services.BY_KEY["ui"].matches("/root/.venv/bin/python -m robot.ui.app", exe)
        assert not services.BY_KEY["core"].matches("/root/.venv/bin/python -m robot.ui.app", exe)


class TestCommandMapping:
    """Which supervisor gets asked, and with what."""

    def test_a_dev_stack_is_driven_through_run_stack(self):
        command = services.command_for(services.BY_KEY["llm"], "restart", "dev")
        assert command[-3:] == ["svc", "restart", "llm"]
        assert command[0].endswith("scripts/run_stack.sh")

    def test_system_units_go_through_systemctl(self):
        command = services.command_for(services.BY_KEY["asr"], "stop", "systemd")
        assert command[-3:] == ["systemctl", "stop", "robot-asr"]

    def test_a_user_instance_needs_no_privileges(self):
        command = services.command_for(services.BY_KEY["tts"], "start", "systemd-user")
        assert command == ["systemctl", "--user", "start", "robot-tts"]
        assert "sudo" not in command

    def test_the_simulator_has_no_unit_so_systemd_cannot_help(self):
        """`body` only exists in a dev stack. Saying so beats a confusing systemctl error."""
        assert services.command_for(services.BY_KEY["body"], "start", "systemd") is None
        assert services.command_for(services.BY_KEY["body"], "start", "dev") is not None


class TestRefusals:
    """Nothing from the network reaches a command line without passing the table."""

    async def test_an_unknown_service_is_refused(self):
        result = await services.control("; rm -rf /", "restart")
        assert not result["ok"] and "unknown service" in result["message"]

    async def test_an_unknown_action_is_refused(self):
        result = await services.control("llm", "delete")
        assert not result["ok"] and "unknown action" in result["message"]

    async def test_the_dashboard_will_not_stop_itself(self):
        """robot-ui serves the page the button is on; stopping it strands the browser."""
        result = await services.control("ui", "stop")
        assert not result["ok"]
        assert "cannot be controlled from here" in result["message"]


class TestSnapshot:
    def test_every_service_is_reported_whether_running_or_not(self):
        rows = services.snapshot()
        assert {row["key"] for row in rows} == set(services.BY_KEY)
        for row in rows:
            assert isinstance(row["running"], bool)
            assert row["label"] and row["blurb"]

    def test_a_stopped_service_reports_no_process(self):
        rows = {row["key"]: row for row in services.snapshot()}
        for row in rows.values():
            if not row["running"]:
                assert row["pid"] is None and row["instances"] == []

    def test_a_running_service_reports_how_much_of_it_is_in_swap(self):
        """RSS alone reads healthy for a process the kernel has paged out, and a paged-out
        synthesiser is the difference between a 0.7 s first word and a 10.5 s one. The
        number has to be carried next to RSS or the slowness has no visible cause."""
        for row in services.snapshot():
            if not row["running"]:
                assert row["swap_mib"] is None
                continue
            assert row["swap_mib"] is None or row["swap_mib"] >= 0.0
            for instance in row["instances"]:
                assert "swap_mib" in instance

    def test_this_test_process_is_not_mistaken_for_a_service(self):
        """pytest's own command line mentions `robot`; that must not make it a service."""
        rows = {row["key"]: row for row in services.snapshot()}
        import os
        for row in rows.values():
            assert os.getpid() not in [i["pid"] for i in row["instances"]]


class TestSupervisorDetection:
    def test_the_mode_is_one_we_have_a_label_for(self):
        mode = services.supervisor()
        assert mode in services.SUPERVISOR_LABEL

    def test_the_root_is_the_repository(self):
        assert (services.root() / "scripts" / "run_stack.sh").is_file()


class TestLogTail:
    def test_a_service_with_no_log_configured_returns_nothing(self, monkeypatch):
        monkeypatch.setattr(services, "SERVICES", services.SERVICES)
        assert services.log_tail("nope") == []

    def test_a_missing_log_file_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(services, "LOG_DIR", tmp_path / "nothing-here")
        assert services.log_tail("llm") == []


class TestWhatIsActuallyRunning:
    """Configured is not running, and the difference is what people get wrong.

    A `Kind.RESTART` setting is staged on purpose -- picking a model must not interrupt
    the robot mid-sentence -- so the config file and the live process legitimately
    disagree for a while. Nothing used to say so. Observed on this board: the dashboard
    had Qwen3-4B chosen, `config/llm.env` said gemma-3-1b, and the process was running
    gemma-3-1b. Every layer behaved as designed and the answer to "which model am I
    talking to?" was still wrong.
    """

    def test_the_model_is_read_from_the_command_line(self):
        llm = services.BY_KEY["llm"]
        cmdline = "/root/vendor/llama.cpp/build/bin/llama-server --model models/llm/a.gguf --ctx-size 2048"
        assert services.running_model(llm, 1, cmdline) == "models/llm/a.gguf"

    def test_an_equals_form_flag_is_understood_too(self):
        llm = services.BY_KEY["llm"]
        assert services.running_model(
            llm, 1, "llama-server --model=models/llm/b.gguf") == "models/llm/b.gguf"

    def test_a_service_with_no_model_reports_none(self):
        for key in ("core", "ui", "body"):
            assert services.running_model(services.BY_KEY[key], 1, "whatever") is None

    def test_configured_is_read_from_disk_not_from_the_process(self, tmp_path, monkeypatch):
        """The two answers must come from different places or they agree by construction."""
        (tmp_path / "config").mkdir()
        (tmp_path / "config/llm.env").write_text(
            "# a comment\nLLM_CTX=2048\nLLM_MODEL=models/llm/configured.gguf\n")
        monkeypatch.setattr(services, "root", lambda: tmp_path)
        assert services.configured_model(services.BY_KEY["llm"]) == "models/llm/configured.gguf"

    def test_a_commented_out_key_is_not_the_configured_value(self, tmp_path, monkeypatch):
        (tmp_path / "config").mkdir()
        (tmp_path / "config/llm.env").write_text("#LLM_MODEL=models/llm/old.gguf\n")
        monkeypatch.setattr(services, "root", lambda: tmp_path)
        assert services.configured_model(services.BY_KEY["llm"]) is None

    def test_a_missing_config_file_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(services, "root", lambda: tmp_path)
        assert services.configured_model(services.BY_KEY["llm"]) is None

    def test_the_asr_row_names_the_checkpoint_it_will_post_to(self, tmp_path, monkeypatch):
        """robot-asr holds no model of its own. What it should report is which decoder
        it is talking to, which lives in whisper.env -- the file it now reads."""
        (tmp_path / "config").mkdir()
        (tmp_path / "config/whisper.env").write_text(
            "WHISPER_MODEL=models/asr/whisper/ggml-small.bin\n")
        monkeypatch.setattr(services, "root", lambda: tmp_path)
        assert services.configured_model(services.BY_KEY["asr"]) == "ggml-small.bin"

    def test_every_snapshot_row_carries_the_comparison(self):
        for row in services.snapshot():
            assert "model" in row and "configured" in row and "stale" in row
            assert isinstance(row["stale"], bool)

    def test_a_stopped_service_is_never_stale(self):
        """It cannot be running the wrong model if it is not running."""
        for row in services.snapshot():
            if not row["running"]:
                assert row["stale"] is False


class TestOneDecoderAtATime:
    """robot-asr and robot-whisper are not alternatives -- one calls the other.

    `robot-asr` owns the microphone, the Silero endpointing, the noise gates and the
    barge-in signal, and holds no decoder. `robot-whisper` is the decoder. Both are
    always expected: since Qwen3-ASR was removed on 2026-09-14 there is nothing for the
    engine to be, so neither service is conditional on a setting any more.
    """

    def test_neither_service_is_conditional(self):
        assert services.BY_KEY["asr"].expected_when is None
        assert services.BY_KEY["whisper"].expected_when is None

    def test_a_decoder_nothing_reads_is_reported_as_not_in_use(self, monkeypatch):
        """Not a fault -- it is holding memory nothing is reading, which is worse."""
        monkeypatch.setattr(services, "is_expected", lambda svc: False)
        monkeypatch.setattr(services, "_scan", lambda: [
            (4242, "/root/vendor/whisper.cpp/build/bin/whisper-server --model m.bin",
             "/root/vendor/whisper.cpp/build/bin/whisper-server")])
        row = {r["key"]: r for r in services.snapshot()}["whisper"]
        assert row["running"] and row["unexpected"] and not row["missing"]

    def test_a_decoder_the_engine_needs_but_is_absent_is_reported_as_missing(self, monkeypatch):
        monkeypatch.setattr(services, "is_expected", lambda svc: True)
        monkeypatch.setattr(services, "_scan", lambda: [])
        row = {r["key"]: r for r in services.snapshot()}["whisper"]
        assert row["missing"] and not row["unexpected"]

    def test_services_with_no_condition_are_never_flagged_either_way(self, monkeypatch):
        monkeypatch.setattr(services, "_scan", lambda: [])
        for row in services.snapshot():
            if row["key"] != "whisper":
                assert not row["unexpected"] and not row["missing"]
