"""The endpoint resolver must be loud about unpinned defaults.

Both halves of the robot's audio have now failed this exact way: `pulse` follows
PulseAudio's default, the default is a codec with nothing attached, and capture or
playback succeeds while producing nothing. Nothing else in the stack can detect it,
so the warning here is the only thing standing between us and a silent failure.
"""
import pytest

from robot.common import audio


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("PULSE_SINK", raising=False)
    monkeypatch.delenv("PULSE_SOURCE", raising=False)


def _fake_pactl(monkeypatch, default="alsa_output.platform-sound.analog-stereo"):
    monkeypatch.setattr(audio, "_pactl",
                        lambda *a: f"Default Sink: {default}\nDefault Source: {default}\n")


@pytest.mark.parametrize("kind,var", [("sink", "PULSE_SINK"), ("source", "PULSE_SOURCE")])
def test_unpinned_default_warns_and_names_the_endpoint(kind, var, monkeypatch):
    _fake_pactl(monkeypatch)
    where, warning = audio.resolve(kind, "pulse")
    assert "NOT pinned" in where
    assert "analog-stereo" in where          # says which device, not just "a default"
    assert warning and var in warning        # and how to fix it
    assert "pactl list short" in warning


@pytest.mark.parametrize("kind,var", [("sink", "PULSE_SINK"), ("source", "PULSE_SOURCE")])
def test_pinned_endpoint_is_quiet(kind, var, monkeypatch):
    _fake_pactl(monkeypatch)
    monkeypatch.setenv(var, "alsa_output.usb-Thing.mono-fallback")
    where, warning = audio.resolve(kind, "pulse")
    assert warning is None
    assert "usb-Thing" in where and "pinned" in where


def test_default_device_name_also_follows_pulse(monkeypatch):
    """`default` is the same trap as `pulse`, and used to be the shipped value."""
    _fake_pactl(monkeypatch)
    _, warning = audio.resolve("sink", "default")
    assert warning is not None


def test_naming_hardware_directly_needs_no_warning(monkeypatch):
    _fake_pactl(monkeypatch)
    where, warning = audio.resolve("sink", "plughw:CARD=Audio,DEV=0")
    assert warning is None
    assert "not via PulseAudio" in where


def test_missing_pactl_is_not_fatal(monkeypatch):
    """No PulseAudio (or no pactl) must still warn, not raise."""
    monkeypatch.setattr(audio, "_pactl", lambda *a: "")
    where, warning = audio.resolve("sink", "pulse")
    assert "unknown" in where
    assert warning is not None


def test_shipped_config_pins_both_endpoints():
    """The committed config must not ship an unpinned endpoint again."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for env_file, device_var, pin in (("tts.env", "TTS_ALSA_DEVICE", "PULSE_SINK"),
                                      ("asr.env", "ASR_ALSA_DEVICE", "PULSE_SOURCE")):
        text = (root / "config" / env_file).read_text()
        device = next((l.split("=", 1)[1].strip() for l in text.splitlines()
                       if l.startswith(device_var + "=")), "")
        if device in audio.FOLLOWS_DEFAULT:
            assert f"\n{pin}=" in text, (
                f"config/{env_file} sets {device_var}={device}, which follows "
                f"PulseAudio's default -- it must pin {pin} or audio fails silently")
