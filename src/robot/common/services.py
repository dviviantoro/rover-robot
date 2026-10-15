"""What is actually running, and how to start, stop or restart one piece of it.

AGENTS.md working rule 3: every long-running component is a supervised service. In
production that supervisor is systemd -- the units in `deploy/systemd/`. During
development it is `scripts/run_stack.sh`, which is what is in charge on this board
today: the units have never been installed. Both are real supervisors, and the
dashboard has to drive whichever one is actually holding the processes, or its buttons
lie about what they did. That is not hypothetical -- it is exactly how a dead
`llama-server` stayed dead while the Config page offered a restart that could only ever
have called `systemctl`.

This module is the single place that knows:

  * which processes make up the stack, and how to recognise one in `/proc`
  * which supervisor is managing them right now
  * how to ask that supervisor to start, stop or restart exactly one of them

It is imported by `robot-ui` (the Services panel, which must keep working when the
orchestrator is the thing that is down) and by `robot-core` (the scoped restart behind
a staged setting), so the two cannot disagree about what a service is called or about
what restarting it means.

Nothing here takes a command from its caller. A request names a *key*; the key is looked
up in `SERVICES` and the command line is built from that table. There is no path from
the network to an arbitrary process.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("robot-services")

ACTIONS = ("start", "stop", "restart")

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
MIB = 1024.0 * 1024.0

LOG_DIR = Path(".cache/tmp")


@dataclass(frozen=True)
class Managed:
    """One supervised process, and enough to find it without guessing."""

    key: str
    label: str
    unit: str | None            # systemd unit name, when there is one
    blurb: str

    # Recognising the process takes three parts, not one. `llama-server` runs twice --
    # once for the LLM, once for embeddings -- so matching the binary alone silently
    # folds them into a single row, and killing "the" llama-server takes the wrong one.
    needle: str                 # must appear in the command line
    exe: str                    # must appear in the resolved /proc/<pid>/exe
    excludes: str = ""          # must NOT appear in the command line
    requires: str = ""          # must also appear in the command line

    log: str | None = None      # .cache/tmp/<log>, shown when a start fails
    # Where to read what this process ACTUALLY loaded, and what the config says it
    # should have. The two are read from different places on purpose: one from the live
    # process, one from disk. Deriving both from the same source would make them agree
    # by construction and report nothing.
    model_flag: str = ""                        # a command-line flag, e.g. --model
    model_env: str = ""                         # or an environment variable
    config_at: tuple[str, str] | None = None    # (env file, key)
    # (env file, key, value) that has to hold for this service to belong on the board at
    # all. A service running when this does not hold is not a fault, but it is holding
    # memory nothing is reading -- which on 8 GB is the whole game.
    expected_when: tuple[str, str, str] | None = None
    controllable: bool = True
    why_not: str = ""
    critical: bool = False      # the UI asks before stopping this one
    warning: str = ""           # what stopping it costs

    def matches(self, cmdline: str, exe: str) -> bool:
        """Is this process mine?

        The executable is checked as well as the command line because `pgrep -f`-style
        matching also hits an editor, a shell or a log tail that merely *mentions* the
        service -- which is a fine way to kill someone's editor.
        """
        if self.needle not in cmdline or self.exe not in exe:
            return False
        if self.excludes and self.excludes in cmdline:
            return False
        if self.requires and self.requires not in cmdline:
            return False
        return True


# Order is start order, which is also the order the panel reads best in.
SERVICES: tuple[Managed, ...] = (
    Managed(
        "llm", "Thinking (LLM)", "robot-llm",
        "llama.cpp serving the conversational model on :8080. Every turn needs it: "
        "without it the robot speaks its canned unavailable line.",
        needle="bin/llama-server", exe="llama-server", excludes="--embeddings",
        log="llama-server.log",
        model_flag="--model", config_at=("config/llm.env", "LLM_MODEL"),
        warning="Turns will fall back to the canned unavailable line until it is back."),
    Managed(
        "embed", "Knowledge (embeddings)", "robot-embed",
        "The second llama.cpp server, CPU-only, on :8082. Retrieval and save_memory "
        "need it; conversation does not.",
        needle="bin/llama-server", exe="llama-server", requires="--embeddings",
        log="embed.log",
        model_flag="--model", config_at=("config/embed.env", "EMBED_MODEL"),
        warning="The robot keeps talking, but stops remembering and retrieving."),
    Managed(
        "tts", "Speaking (TTS)", "robot-tts",
        "Piper or Supertonic. Holds the voices and owns the speaker.",
        needle="robot.tts.service", exe="python", log="tts.log",
        model_env="TTS_ENGINE", config_at=("config/tts.env", "TTS_ENGINE"),
        warning="Replies still appear in the dashboard; nothing is spoken aloud."),
    Managed(
        "core", "Orchestrator", "robot-core",
        "The turn state machine, the knowledge store and -- when a board is attached -- "
        "the STM32 heartbeat.",
        needle="robot.core.app", exe="python", log="core.log", critical=True,
        warning="The conversation and the STM32 heartbeat both drop. The firmware "
                "watchdog zeroes the motors within 500 ms (that is its job), but the "
                "robot is deaf and mute until this is back."),
    Managed(
        "asr", "Listening (microphone + VAD)", "robot-asr",
        "Owns the microphone, the Silero endpointing and the noise gates, and is what "
        "emits the barge-in signal. It holds no decoder of its own: it posts closed "
        "utterances at robot-whisper. It is not an alternative to that service -- it is "
        "the thing that calls it.",
        needle="robot.asr.service", exe="python", log="asr.log",
        # Read from the process environment, not its command line: the checkpoint comes
        # from WHISPER_MODEL, and /proc/<pid>/environ is that value as the RUNNING
        # process received it -- which is the whole question being asked.
        model_env="WHISPER_MODEL", config_at=("config/whisper.env", "WHISPER_MODEL"),
        warning="The robot stops hearing you. Typed chat still works."),
    Managed(
        "whisper", "ASR decoder (whisper.cpp)", "robot-whisper",
        "The decoder, on :8083. The only one there is since Qwen3-ASR was removed on "
        "2026-09-14, so the ASR choice is a choice of SIZE -- tiny, base, small -- made "
        "on the Memory page, which prices each against the ceiling. Always expected.",
        needle="bin/whisper-server", exe="whisper-server", log="whisper.log",
        model_flag="--model", config_at=("config/whisper.env", "WHISPER_MODEL"),
        warning="The robot goes deaf: robot-asr keeps the microphone and the barge-in "
                "signal, but every utterance is lost until this is back."),
    Managed(
        "body", "Body (STM32 simulator)", None,
        "The firmware core running on a PTY, for when no microcontroller is attached. "
        "Started by run_stack.sh --sim; there is no systemd unit for it.",
        needle="stm32_sim", exe="stm32_sim", log="stm32_sim.log",
        warning="Motion commands stop being acknowledged."),
    Managed(
        "face", "Face (panel)", "robot-face",
        "The Slint panel on the robot itself: what it heard, what it answered, and the "
        "voice meter. Renders straight to DRM/KMS -- no desktop, no compositor. A second "
        "websocket client of robot-core, and as optional as the dashboard.",
        needle="robot-face", exe="robot-face", log="face.log",
        warning="The robot's screen goes dark. It keeps listening and answering."),
    Managed(
        "ui", "Dashboard", "robot-ui",
        "This page. Deliberately not controllable from itself.",
        needle="robot.ui.app", exe="python", log="ui.log",
        controllable=False,
        why_not="this is the process serving the page you are reading -- stop it from a "
                "terminal (scripts/run_stack.sh) if you really want it down"),
)

BY_KEY: dict[str, Managed] = {s.key: s for s in SERVICES}


# ---------------------------------------------------------------- where we are

def root() -> Path:
    """The repository root, which is every service's working directory."""
    override = os.environ.get("ROBOT_ROOT")
    if override:
        return Path(override)
    here = Path(__file__).resolve().parents[3]
    if (here / "scripts" / "run_stack.sh").is_file():
        return here
    return Path.cwd()


def supervisor() -> str:
    """Which supervisor is in charge: `systemd`, `systemd-user` or `dev`.

    Decided by which unit files exist rather than by what is running, so the answer is
    the same whether the stack is up or down -- a Start button has to know where to send
    a service that is not there yet.
    """
    if shutil.which("systemctl"):
        if (Path("/etc/systemd/system") / "robot-core.service").is_file():
            return "systemd"
        if (Path.home() / ".config/systemd/user/robot-core.service").is_file():
            return "systemd-user"
    return "dev"


SUPERVISOR_LABEL = {
    "systemd": "systemd (system units — production)",
    "systemd-user": "systemd --user (scripts/hardening/user_units.sh)",
    "dev": "scripts/run_stack.sh (development stack)",
}


# ------------------------------------------------------------------ discovery

def _scan() -> list[tuple[int, str, str]]:
    """Every process, as (pid, command line, resolved executable).

    Read straight out of /proc rather than shelled out to `ps`: it costs no subprocess,
    and a `ps | grep` pipeline can match the grep's own command line, which is how
    process-matching bugs usually start.
    """
    found: list[tuple[int, str, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue                      # the process exited between listing and reading
        if not raw:
            continue                      # kernel thread
        cmdline = raw.replace(b"\0", b" ").decode(errors="replace").strip()
        try:
            exe = os.readlink(entry / "exe")
        except OSError:
            exe = ""                      # not ours to look at; matching will reject it
        found.append((int(entry.name), cmdline, exe))
    return found


# ---- what is ACTUALLY loaded, versus what the config says ------------------
#
# These two answers come from deliberately different places. The running one is read out
# of the live process -- its command line, or the environment it was handed at exec. The
# configured one is read off disk, now. Deriving both from the same source would make
# them agree by construction and report nothing, and "they agreed" is precisely the
# claim that turned out to be false: `run/settings.json` said Qwen3-4B, `config/llm.env`
# said gemma-3-1b, and the process was running gemma-3-1b, with nothing anywhere saying
# the three disagreed.


def _proc_env(pid: int) -> dict[str, str]:
    """The environment a process was actually started with.

    Not `os.environ` and not the config file: a service started before an edit is still
    running with the old values, and that is the thing worth knowing.
    """
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if b"=" in entry:
            key, _, value = entry.partition(b"=")
            out[key.decode(errors="replace")] = value.decode(errors="replace")
    return out


# A CUDA process on Tegra holds file descriptors on the GPU nodes. This is what it is
# ACTUALLY using, not what its flags asked for -- and the two come apart: llama-server
# with --n-gpu-layers 99 still falls back to the CPU if the allocation fails, and the
# embedding server is given -ngl 0 AND CUDA_VISIBLE_DEVICES= precisely so it cannot.
_GPU_FD_MARKERS = ("nvgpu", "nvhost", "nvmap", "nvidia")


def accelerator(pid: int) -> str:
    """`GPU` or `CPU`, decided by the file descriptors the process is holding.

    On a board where both draw from one unified pool, which processes are on the GPU is
    the difference between the LLM getting its KV cache and not -- so it is worth
    knowing from the process rather than inferring from a config file that may have been
    edited since it started.
    """
    try:
        entries = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return "?"
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if any(marker in target for marker in _GPU_FD_MARKERS):
            return "GPU"
    return "CPU"


def _flag_value(cmdline: str, flag: str) -> str | None:
    parts = cmdline.split()
    for i, part in enumerate(parts):
        if part == flag and i + 1 < len(parts):
            return parts[i + 1]
        if part.startswith(f"{flag}="):
            return part.split("=", 1)[1]
    return None


def env_value(path: str, key: str) -> str | None:
    """One KEY=value out of an env file, as it is on disk right now."""
    try:
        for line in Path(root() / path).read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


def running_model(service: Managed, pid: int, cmdline: str) -> str | None:
    """What this process has loaded, according to the process."""
    if service.model_flag:
        return _flag_value(cmdline, service.model_flag)
    if service.model_env:
        value = _proc_env(pid).get(service.model_env)
        if service.key == "asr" and value:
            # One engine, so the answer is just which checkpoint it was handed.
            return Path(value).name
        if service.key == "tts" and value == "supertonic":
            # Supertonic is one model and many speaker embeddings, so the voice is the
            # only part of the choice worth reporting -- and it is the part someone
            # changes and then wonders whether it took.
            voice = _proc_env(pid).get("SUPERTONIC_VOICE")
            return f"{value}: {voice}" if voice else value
        return value
    return None


# Which dashboard setting lands on which service, so the third layer -- what someone
# picked, before it was written anywhere -- can be lined up with the other two.
STAGED_KEYS = {"llm_model": "llm", "asr_model": "asr"}


def chosen_models() -> dict[str, str]:
    """What the dashboard has selected, per service key.

    Read from `run/settings.json`, which is where a selection lives before it reaches
    any env file. With the Memory page applying immediately this should now match the
    configured layer -- but "should" is exactly the assumption that was wrong before,
    so it is still reported rather than trusted.
    """
    try:
        saved = json.loads((root() / "run/settings.json").read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for setting, key in STAGED_KEYS.items():
        value = saved.get(setting)
        if value:
            out[key] = str(value)
    return out


def is_expected(service: Managed) -> bool | None:
    """Should this service be running at all, given the config? None = always."""
    if service.expected_when is None:
        return None
    path, key, wanted = service.expected_when
    return env_value(path, key) == wanted


def configured_model(service: Managed) -> str | None:
    """What the config on disk says it should be loading."""
    if service.config_at is None:
        return None
    value = env_value(*service.config_at)
    if service.key == "asr" and value:
        return Path(value).name
    if service.key == "tts" and value == "supertonic":
        # Composed the same way `running_model` composes it. Both sides of the
        # comparison have to be built from the same parts, or every row is permanently
        # "stale" and the one that genuinely is gets lost among them.
        voice = env_value("config/tts.env", "SUPERTONIC_VOICE")
        return f"{value}: {voice}" if voice else value
    return value


def _short(value: str | None) -> str | None:
    """Paths are long and the interesting part is the end."""
    if not value:
        return value
    return Path(value).name if "/" in value else value


def _uptime_now() -> float:
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError):
        return 0.0


def _process_detail(pid: int, boot_uptime: float) -> dict:
    detail: dict = {"pid": pid, "rss_mib": None, "swap_mib": None, "uptime_s": None,
                    "accelerator": accelerator(pid)}
    try:
        resident = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
        detail["rss_mib"] = round(resident * PAGE_SIZE / MIB, 1)
    except (OSError, IndexError, ValueError):
        pass
    # RSS alone says a service is small when what actually happened is that the kernel
    # took its pages away. Measured 2026-09-15: robot-tts showed 1042 MiB resident with
    # 801 MiB in swap, and Supertonic synthesised 40 characters in 10.5 s -- RTF ~4 on
    # a GPU that reports itself healthy. Nothing in the stack reported the swap, so the
    # slowness had no visible cause. VmSwap is that cause, and it belongs next to RSS.
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmSwap:"):
                detail["swap_mib"] = round(int(line.split()[1]) / 1024, 1)
                break
    except (OSError, IndexError, ValueError):
        pass
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # The comm field can contain spaces and brackets, so everything is counted
        # from the closing parenthesis. starttime is field 22 overall.
        fields = stat[stat.rindex(")") + 1:].split()
        started = float(fields[19]) / CLOCK_TICKS
        detail["uptime_s"] = round(max(0.0, boot_uptime - started), 1)
    except (OSError, ValueError, IndexError):
        pass
    return detail


def snapshot() -> list[dict]:
    """One row per managed service: is it up, since when, and how big."""
    processes = _scan()
    boot_uptime = _uptime_now()
    rows = []
    chosen = chosen_models()
    for service in SERVICES:
        mine = [(pid, cmdline) for pid, cmdline, exe in processes
                if service.matches(cmdline, exe)]
        instances = [_process_detail(pid, boot_uptime) for pid, _ in mine]

        running = running_model(service, *mine[0]) if mine else None
        configured = configured_model(service)
        picked = chosen.get(service.key)
        expected = is_expected(service)
        # Both sides need the same shape before they can be compared: the asr row reads
        # "engine: model" and only the model half is comparable, and the other rows hold
        # a full path where `chosen` holds a basename.
        unapplied = bool(picked and configured
                         and _short(picked) != _short(configured.split(": ")[-1]))
        # Only a real disagreement counts. A service that is not running cannot be
        # stale, and one with nothing configured has nothing to disagree with.
        stale = bool(mine and configured and running
                     and _short(running) != _short(configured))

        rows.append({
            "key": service.key,
            "label": service.label,
            "blurb": service.blurb,
            "unit": service.unit,
            "running": bool(instances),
            "instances": instances,
            "pid": instances[0]["pid"] if instances else None,
            # GPU if any of this service's processes is on it. Only llama-server ever
            # runs more than one, and they are separate services here anyway.
            "accelerator": (next((i["accelerator"] for i in instances
                                  if i["accelerator"] == "GPU"),
                                 instances[0]["accelerator"]) if instances else None),
            "rss_mib": round(sum(i["rss_mib"] or 0.0 for i in instances), 1) if instances else None,
            "swap_mib": round(sum(i["swap_mib"] or 0.0 for i in instances), 1) if instances else None,
            "uptime_s": instances[0]["uptime_s"] if instances else None,
            "controllable": service.controllable,
            "why_not": service.why_not,
            "critical": service.critical,
            "warning": service.warning,
            # What it loaded, what the config says, and whether those differ. A restart
            # is what closes the gap, which is why this sits next to the restart button.
            "model": _short(running),
            "model_path": running,
            "configured": _short(configured),
            "configured_path": configured,
            "stale": stale,
            # The third layer: picked in the dashboard, possibly not written anywhere
            # yet. Reported next to the other two so all three can be compared at once,
            # in the terminal and in the UI, from the same source.
            "chosen": _short(picked),
            "chosen_path": picked,
            "unapplied": unapplied,
            # Whether the config wants this service at all, and whether reality agrees.
            "expected": expected,
            "unexpected": bool(expected is False and instances),
            "missing": bool(expected is True and not instances),
        })
    # robot-asr holds no model and never touches CUDA, so reading its own file
    # descriptors says "CPU" -- next to the checkpoint name it now reports, which
    # decodes on the GPU in robot-whisper. That reads as "your STT is on the CPU" and
    # it is not true. One capability, one answer: take it from the decoder.
    by_key = {row["key"]: row for row in rows}
    decoder, listener = by_key.get("whisper"), by_key.get("asr")
    if decoder and listener and listener["running"]:
        listener["accelerator"] = decoder["accelerator"] if decoder["running"] else None

    return rows


def pids_for(key: str) -> list[int]:
    service = BY_KEY.get(key)
    if service is None:
        return []
    return [pid for pid, cmdline, exe in _scan() if service.matches(cmdline, exe)]


def is_running(key: str) -> bool:
    """Is this service's process alive right now, whoever started it.

    Reads /proc, so it answers for a service started by systemd, by run_stack.sh, or by
    a process that has since exited -- not just for one this program started itself.
    """
    return bool(pids_for(key))


def log_tail(key: str, lines: int = 12) -> list[str]:
    """The last few lines of a service's log -- the reason a start failed.

    A start that fails leaves nothing useful in the supervisor's own output: llama.cpp
    exits 1 and the actual sentence you need (`cudaMalloc failed: out of memory`) is in
    its log. Reporting the command's exit status without this makes the panel useless
    exactly when something went wrong.
    """
    service = BY_KEY.get(key)
    if service is None or service.log is None:
        return []
    path = root() / LOG_DIR / service.log
    try:
        return [line for line in path.read_text(errors="replace").splitlines()[-lines:]
                if line.strip()]
    except OSError:
        return []


# -------------------------------------------------------------------- control

def command_for(service: Managed, action: str, mode: str) -> list[str] | None:
    """The exact command line for one action, or None when this mode cannot do it.

    Kept separate from running it so the mapping is testable without a supervisor.
    """
    if mode == "dev":
        return [str(root() / "scripts" / "run_stack.sh"), "svc", action, service.key]
    if service.unit is None:
        return None
    if mode == "systemd-user":
        return ["systemctl", "--user", action, service.unit]
    command = ["systemctl", action, service.unit]
    # robot-core runs unprivileged and must stay that way -- it owns the heartbeat.
    # scripts/bringup/setup_sudoers.sh grants exactly these restarts and nothing else.
    return ["sudo", "-n", *command] if shutil.which("sudo") else command


def _run(command: list[str], timeout: float) -> tuple[int | None, str]:
    """Run a supervisor command and collect its output. Blocking; called in a thread.

    Two things here are deliberate and both were bought the hard way.

    Output goes to a temporary FILE, never a pipe. The command being run is a supervisor:
    it spawns daemons that outlive it, and a pipe stays open for as long as *any*
    descendant holds the write end -- so `communicate()` can sit there long after the
    script itself has exited. Measured from the dashboard: `run_stack.sh svc restart llm`
    finished in 7 s, llama-server was answering /health at 8 s, and the read blocked until
    the 120 s timeout. A file has no such lifetime.

    And it is `subprocess` in a worker thread rather than `asyncio.create_subprocess_exec`,
    so waiting for the child is a plain `waitpid` in that thread and does not depend on the
    event loop's child watcher -- the dashboard runs under uvicorn's uvloop, and this must
    behave the same there as under the default loop.
    """
    with tempfile.TemporaryFile("w+b") as sink:
        proc = subprocess.Popen(
            command, cwd=str(root()), stdin=subprocess.DEVNULL,
            stdout=sink, stderr=subprocess.STDOUT,
            # Detach: a service started from here must not die when the process that
            # asked for it (the dashboard) is itself restarted.
            start_new_session=True)
        try:
            code: int | None = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kills the supervisor command only. Anything it already started is in its
            # own session and keeps running -- which is what you want: a slow start is
            # not a reason to tear down a service that is halfway up.
            proc.kill()
            proc.wait()
            code = None
        sink.seek(0)
        return code, sink.read().decode(errors="replace").strip()


async def control(key: str, action: str, timeout: float = 150.0) -> dict:
    """Start, stop or restart exactly one service, through whoever supervises it."""
    if action not in ACTIONS:
        return {"ok": False, "key": key, "action": action,
                "message": f"unknown action {action!r}"}
    service = BY_KEY.get(key)
    if service is None:
        return {"ok": False, "key": key, "action": action,
                "message": f"unknown service {key!r}"}
    if not service.controllable:
        return {"ok": False, "key": key, "action": action,
                "message": f"{service.label} cannot be controlled from here: {service.why_not}"}

    mode = supervisor()
    command = command_for(service, action, mode)
    if command is None:
        return {"ok": False, "key": key, "action": action, "supervisor": mode,
                "message": f"{service.label} has no systemd unit -- it only exists in a "
                           f"run_stack.sh development stack"}

    log.info("%s %s via %s", action, key, mode)
    try:
        code, output = await asyncio.to_thread(_run, command, timeout)
    except OSError as exc:
        return {"ok": False, "key": key, "action": action, "supervisor": mode,
                "message": str(exc)}
    if code is None:
        return {"ok": False, "key": key, "action": action, "supervisor": mode,
                "message": f"{service.label} did not {action} within {timeout:.0f}s"}

    ok = code == 0
    if ok:
        message = output.splitlines()[-1] if output else f"{service.label} {action}ed"
    else:
        message = output.splitlines()[-1] if output else f"exit {code}"
        if "password" in message.lower() or "not allowed" in message.lower():
            message += "  (run: sudo scripts/bringup/setup_sudoers.sh)"

    result = {"ok": ok, "key": key, "action": action, "supervisor": mode,
              "message": message, "output": output.splitlines()[-20:]}
    if not ok and action in ("start", "restart"):
        result["log"] = log_tail(key)
    return result
