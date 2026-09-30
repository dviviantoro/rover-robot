"""Work out where audio will *actually* go, and say so.

The ALSA device names `pulse` and `default` do not name a device -- they mean
"whatever PulseAudio currently calls its default". On this board that default is
the Tegra onboard analog codec, which is not wired to anything: capture from it
returns digital silence, playback into it is discarded, and both succeed. Every
layer reports success and the robot is deaf, or mute, with nothing in the log.

That has now cost us the same debugging session twice -- once for the microphone
(M8) and once for the speaker. So this module exists to make the resolution
visible at startup: name the endpoint audio is really bound to, and complain when
it is following a default that can silently change on the next reboot or hotplug.
"""
from __future__ import annotations

import os
import subprocess

# ALSA device names that mean "ask PulseAudio", rather than naming hardware.
FOLLOWS_DEFAULT = {"pulse", "default"}

_KIND = {
    "sink":   ("PULSE_SINK", "Default Sink", "sinks"),
    "source": ("PULSE_SOURCE", "Default Source", "sources"),
}


def _pactl(*args: str) -> str:
    try:
        out = subprocess.run(("pactl", *args), capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def resolve(kind: str, device: str) -> tuple[str, str | None]:
    """Describe where `device` leads, plus a warning if it is not pinned.

    Returns (description, warning). The warning is None when the endpoint is
    pinned -- either by naming hardware directly or via PULSE_SINK/PULSE_SOURCE.
    """
    env_var, info_key, list_cmd = _KIND[kind]
    pinned = os.environ.get(env_var, "").strip()

    if device not in FOLLOWS_DEFAULT:
        return f"{device} (ALSA device, not via PulseAudio)", None

    if pinned:
        return f"{device} -> {pinned} (pinned by {env_var})", None

    resolved = ""
    for line in _pactl("info").splitlines():
        if line.startswith(info_key + ":"):
            resolved = line.split(":", 1)[1].strip()
            break

    where = resolved or "unknown"
    return (
        f"{device} -> {where} (PulseAudio default -- NOT pinned)",
        f"audio {kind} is following PulseAudio's default, currently {where!r}. "
        f"If that endpoint is not the hardware you expect, this fails silently: "
        f"it will succeed and produce nothing. Pin it by setting {env_var} "
        f"(list them with `pactl list short {list_cmd}`).",
    )
