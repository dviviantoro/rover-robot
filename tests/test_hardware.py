"""Picking the microphone, the speaker and the serial port from the dashboard.

These exist because of 2026-10-06. A USB headset was swapped for a stereo one,
PulseAudio renamed the port `...mono-fallback` -> `...analog-stereo`, and the pinned
name in config/asr.env then matched nothing. arecord could not create the stream at all,
so the robot was completely deaf -- and the only sign was one line at startup, buried in
an eighteen-line ALSA hw_params dump, from a service that then exited for a supervisor
that only exists under systemd.

Two defences are tested here: the resolution is checked against what actually exists,
and the choice is a dropdown rather than an env file edited over SSH.
"""
from __future__ import annotations

import pytest

from robot.common import audio
from robot.core.app import _serial_ports
from robot.core.settings import BY_KEY, GROUPS, RESTART


class TestAPinnedEndpointIsVerified:
    """`resolve` used to trust any non-empty PULSE_SOURCE. That is the whole bug."""

    def test_a_pin_that_names_nothing_is_reported(self, monkeypatch):
        monkeypatch.setattr(audio, "endpoints",
                            lambda kind: ["alsa_input.usb-thing.analog-stereo"])
        monkeypatch.setenv("PULSE_SOURCE", "alsa_input.usb-thing.mono-fallback")
        desc, warning = audio.resolve("source", "pulse")
        assert "DOES NOT EXIST" in desc
        assert warning and "deaf" in warning

    def test_the_warning_suggests_the_usb_device_that_is_there(self, monkeypatch):
        monkeypatch.setattr(audio, "endpoints",
                            lambda kind: ["alsa_input.platform-sound.analog-stereo",
                                          "alsa_input.usb-thing.analog-stereo"])
        monkeypatch.setenv("PULSE_SOURCE", "alsa_input.usb-thing.mono-fallback")
        _, warning = audio.resolve("source", "pulse")
        assert "alsa_input.usb-thing.analog-stereo" in warning

    def test_a_pin_that_exists_is_silent(self, monkeypatch):
        monkeypatch.setattr(audio, "endpoints", lambda kind: ["alsa_input.usb.x"])
        monkeypatch.setenv("PULSE_SOURCE", "alsa_input.usb.x")
        desc, warning = audio.resolve("source", "pulse")
        assert warning is None and "pinned" in desc

    def test_a_sink_says_mute_rather_than_deaf(self, monkeypatch):
        monkeypatch.setattr(audio, "endpoints", lambda kind: ["alsa_output.real"])
        monkeypatch.setenv("PULSE_SINK", "alsa_output.gone")
        _, warning = audio.resolve("sink", "pulse")
        assert "mute" in warning

    def test_an_unreadable_pulseaudio_does_not_invent_a_fault(self, monkeypatch):
        """No `pactl` is not evidence that the pin is wrong, so it must not say so."""
        monkeypatch.setattr(audio, "endpoints", lambda kind: [])
        monkeypatch.setenv("PULSE_SOURCE", "alsa_input.usb.x")
        desc, warning = audio.resolve("source", "pulse")
        assert warning is None and "DOES NOT EXIST" not in desc

    def test_naming_hardware_directly_bypasses_all_of_it(self, monkeypatch):
        monkeypatch.setenv("PULSE_SOURCE", "irrelevant")
        desc, warning = audio.resolve("source", "hw:2,0")
        assert warning is None and "not via PulseAudio" in desc


class TestSerialPorts:
    def test_it_returns_paths_that_exist(self):
        import os
        for port in _serial_ports():
            assert os.path.exists(port), port

    def test_the_same_device_is_not_offered_twice(self):
        """A /dev/serial/by-id entry and its ttyACM target are one port."""
        import os
        ports = _serial_ports()
        reals = [os.path.realpath(p) for p in ports]
        assert len(reals) == len(set(reals))

    def test_by_id_paths_come_first(self):
        """They survive a replug; ttyACM0 and ttyACM1 swap with boot order."""
        ports = _serial_ports()
        by_id = [i for i, p in enumerate(ports) if "/by-id/" in p]
        plain = [i for i, p in enumerate(ports) if "/by-id/" not in p]
        if by_id and plain:
            assert max(by_id) < min(plain)


class TestTheHardwareGroup:
    def test_it_is_the_first_tab(self):
        """Nothing else works if these are wrong, so it is not at the bottom."""
        assert GROUPS[0][0] == "hardware"

    @pytest.mark.parametrize("key", ["mic_source", "speaker_sink", "serial_port"])
    def test_each_control_is_filed_under_hardware(self, key):
        assert BY_KEY[key].group == "hardware"

    @pytest.mark.parametrize("key", ["mic_source", "speaker_sink", "serial_port"])
    def test_each_writes_a_real_env_file_and_names_a_service(self, key):
        setting = BY_KEY[key]
        assert setting.env_file and setting.env_key and setting.component
        assert setting in RESTART, "changing hardware cannot be a hot reload"

    def test_every_key_appears_once(self):
        """`serial_port` was defined twice for one commit; BY_KEY silently kept the
        last, so the dashboard showed one control and the registry held another."""
        keys = [s.key for s in RESTART]
        assert len(keys) == len(set(keys))

    def test_the_mic_and_the_asr_model_are_different_changes(self):
        """Both restart robot-asr. Routing the mic through the ASR-model path would
        reload whisper's weights and never write PULSE_SOURCE."""
        assert BY_KEY["mic_source"].component == BY_KEY["asr_model"].component
        assert BY_KEY["mic_source"].env_file != BY_KEY["asr_model"].env_file
        assert BY_KEY["mic_source"].env_key == "PULSE_SOURCE"


class TestTheHelloPayloadStillBuilds:
    """`_settings_with_options()` is in the `hello` message, so if it raises, EVERY page
    is blank -- there is no partial failure here.

    It did raise, for one commit: the new hardware rows read `row["env_file"]`, and
    `describe()` deliberately does not serialise env paths to the browser. The whole
    suite passed while the dashboard was dead, which is the only reason this exists.
    """

    def _app(self):
        from robot.core.app import CoreService
        return CoreService.__new__(CoreService)

    def test_it_returns_one_row_per_setting_and_raises_nothing(self, monkeypatch):
        from robot.core import app as app_mod
        from robot.core.settings import REGISTRY
        app = self._app()
        # No catalogue scan and no pactl: this is about the shape, not the hardware.
        monkeypatch.setattr(app_mod.catalog, "scan", lambda **kw: [])
        monkeypatch.setattr(app_mod.catalog, "tts_engine", lambda: "supertonic")
        monkeypatch.setattr(app_mod.audio_endpoint, "endpoints", lambda kind: [])
        monkeypatch.setattr(app_mod, "_serial_ports", lambda: [])
        monkeypatch.setattr(type(app), "_llm_ctx", lambda self: 2048, raising=False)
        rows = app._settings_with_options()
        assert len(rows) == len(REGISTRY)
        assert {r["key"] for r in rows} == {s.key for s in REGISTRY}

    def test_it_is_json_serialisable(self, monkeypatch):
        """It goes over a websocket. A tuple or a Path in there is a dead dashboard."""
        import json
        from robot.core import app as app_mod
        app = self._app()
        monkeypatch.setattr(app_mod.catalog, "scan", lambda **kw: [])
        monkeypatch.setattr(app_mod.catalog, "tts_engine", lambda: "supertonic")
        monkeypatch.setattr(app_mod.audio_endpoint, "endpoints",
                            lambda kind: ["alsa_input.usb.x", "alsa_input.usb.x.monitor"])
        monkeypatch.setattr(app_mod, "_serial_ports", lambda: ["/dev/ttyACM0"])
        monkeypatch.setattr(type(app), "_llm_ctx", lambda self: 2048, raising=False)
        json.dumps(app._settings_with_options())

    def test_a_monitor_source_is_never_offered_as_a_microphone(self, monkeypatch):
        """Recording a monitor records the robot's own voice back into its own ears."""
        from robot.core import app as app_mod
        app = self._app()
        monkeypatch.setattr(app_mod.catalog, "scan", lambda **kw: [])
        monkeypatch.setattr(app_mod.catalog, "tts_engine", lambda: "supertonic")
        monkeypatch.setattr(app_mod.audio_endpoint, "endpoints",
                            lambda kind: ["alsa_input.usb.x", "alsa_output.usb.x.monitor"])
        monkeypatch.setattr(app_mod, "_serial_ports", lambda: [])
        monkeypatch.setattr(type(app), "_llm_ctx", lambda self: 2048, raising=False)
        rows = {r["key"]: r for r in app._settings_with_options()}
        assert all(".monitor" not in o for o in rows["mic_source"]["options"])

    def test_no_serial_port_stays_a_choice(self, monkeypatch):
        """A robot with no body still talks and listens; that must be selectable."""
        from robot.core import app as app_mod
        app = self._app()
        monkeypatch.setattr(app_mod.catalog, "scan", lambda **kw: [])
        monkeypatch.setattr(app_mod.catalog, "tts_engine", lambda: "supertonic")
        monkeypatch.setattr(app_mod.audio_endpoint, "endpoints", lambda kind: [])
        monkeypatch.setattr(app_mod, "_serial_ports", lambda: ["/dev/ttyACM0"])
        monkeypatch.setattr(type(app), "_llm_ctx", lambda self: 2048, raising=False)
        rows = {r["key"]: r for r in app._settings_with_options()}
        assert "" in rows["serial_port"]["options"]
