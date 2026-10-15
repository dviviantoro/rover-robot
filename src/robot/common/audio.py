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


def endpoints(kind: str) -> list[str]:
    """Every sink or source PulseAudio currently has, by name."""
    _, _, list_cmd = _KIND[kind]
    names = []
    for line in _pactl("list", "short", list_cmd).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].strip():
            names.append(parts[1].strip())
    return names


def resolve(kind: str, device: str) -> tuple[str, str | None]:
    """Describe where `device` leads, plus a warning if it is not pinned.

    Returns (description, warning). The warning is None when the endpoint is
    pinned to something that exists.
    """
    env_var, info_key, list_cmd = _KIND[kind]
    pinned = os.environ.get(env_var, "").strip()

    if device not in FOLLOWS_DEFAULT:
        return f"{device} (ALSA device, not via PulseAudio)", None

    if pinned:
        # A pin is only a pin if the thing it names is still there. This module was
        # written to catch a silent default; it then let through a different failure one
        # step later. Measured 2026-10-06: the USB headset was replaced with a stereo
        # one, PulseAudio renamed the port `...mono-fallback` -> `...analog-stereo`, and
        # the stale pin matched nothing. This one is not silent -- arecord dies with
        # `PulseAudio: Unable to create stream: No such entity` -- but it is invisible,
        # because that one useful line arrives buried in eighteen lines of ALSA
        # hw_params dump, once, at startup, and robot-asr then exits for a supervisor
        # that only exists under systemd. Under run_stack.sh it simply stays dead.
        available = endpoints(kind)
        if available and pinned not in available:
            usb = [name for name in available
                   if "usb" in name.lower() and not name.endswith(".monitor")]
            suggestion = (f" Did you mean {usb[0]!r}?" if usb else "")
            return (
                f"{device} -> {pinned} (pinned by {env_var}, BUT THAT {kind.upper()} "
                f"DOES NOT EXIST)",
                f"{env_var} is set to {pinned!r} and PulseAudio has no such {kind}. "
                f"Nothing will work: the stream cannot be created, so the robot is "
                f"{'deaf' if kind == 'source' else 'mute'} until this is "
                f"corrected.{suggestion} Available: "
                f"{', '.join(available) or 'none'}.",
            )
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
