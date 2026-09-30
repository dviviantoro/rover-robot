# serial-protocol.md — Jetson ↔ STM32 contract

This is the **only** interface between the brain and the body. Both sides implement this doc.

---

## Physical link

| | |
|---|---|
| Transport | USB-CDC (STM32 enumerates as a virtual serial port) |
| Jetson device | **`/dev/robot-stm32`** — a udev symlink, not `/dev/ttyACM0` |
| Baud | 115200 (ignored by USB-CDC, but set it anyway for compatibility) |
| Framing | **Newline-terminated ASCII lines**, `\n` |
| Encoding | UTF-8, but stick to ASCII in practice |

> **Use the symlink, not the raw device.** Two failures in M5/M9 came from this:
> `/dev/ttyACM0` on this board is the USB *soundcard* (a composite device that also
> exposes CDC-ACM), and a device that is replugged or a service that restarts gets a
> different number — after which the orchestrator retries a path that no longer exists
> and never reconnects. `scripts/bringup/setup_udev.sh` installs the rule; the reconnect
> drill only passes against a stable path.

**Why plain text and not a binary protocol:** command rate is low (voice-triggered, not a
100 Hz control loop), so the byte overhead is irrelevant. Being able to open `screen` or
`minicom` and read the traffic with your own eyes is worth far more than the bytes saved.

---

## Jetson → STM32 (commands)

| Command | Arguments | Meaning |
|---|---|---|
| `DRIVE <vx> <vy> <omega> <ms>` | m/s, m/s, rad/s, milliseconds | Move for a bounded duration, then auto-stop |
| `STOP` | — | Immediate halt of all motion |
| `FACE <name>` | e.g. `happy`, `thinking`, `listening`, `speaking`, `sad`, `neutral` | LED matrix expression |
| `HAND <gesture>` | e.g. `wave`, `point`, `home`, `nod` | Preset servo sequence |
| `PING` | — | Heartbeat (see below) |
| `STATUS` | — | Request a full status line |
| `WHEEL <index> <rad_s> <ms>` | 0–3, rad/s, milliseconds | **Bring-up only.** Spin one wheel, bypassing the kinematics |

**`WHEEL` is a diagnostic command, not a motion command.** It exists because composite
mecanum motion cannot be debugged until each wheel is known to turn the correct way on its
own — see § Verifying wheel direction. It bypasses `mecanum_solve()` entirely and drives a
single motor. It obeys the same bounded duration and the same watchdog as `DRIVE`.

**It is deliberately not exposed as an LLM tool** (see § LLM tool → command mapping, which
does not list it). The model has no reason to address individual wheels, and giving it the
ability to do so would let it bypass the kinematics that keep motion sane.

**Examples**

```
DRIVE 0.5 0.0 0.0 1500     # forward at 0.5 m/s for 1.5 s
DRIVE 0.0 0.3 0.0 1000     # strafe right (mecanum) for 1 s
DRIVE 0.0 0.0 1.2 800      # rotate in place
FACE happy
HAND wave
STOP
WHEEL 0 5.0 1000           # bring-up: front-left only, 5 rad/s for 1 s
```

**Design note — bounded duration:** `DRIVE` always carries a duration and the STM32 auto-stops
when it expires. Motion is never open-ended waiting for a `STOP` that might never arrive.

---

## STM32 → Jetson (responses)

| Line | Meaning |
|---|---|
| `OK <command>` | Command accepted and started |
| `ERR <command> <reason>` | Rejected — unknown command, bad args, or unsafe state |
| `DONE <command>` | A timed action finished (e.g. drive duration elapsed, gesture complete) |
| `STAT batt=<V> motors=<ok\|stall> temp=<C> up=<ms>` | Status |

**Every command gets a response.** Without it, the LLM will cheerfully announce "driving
forward" while a wheel is stalled against furniture, and never find out.

The orchestrator should surface `ERR` and `motors=stall` to both the UI health panel and — when
it matters conversationally — to the LLM, so the robot can say "I'm stuck" instead of lying.

---

## Heartbeat watchdog — MANDATORY

**The single most important safety mechanism in this project.**

- Jetson sends `PING` every **200 ms** while the link is up.
- If the STM32 receives no line of any kind for **500 ms**, it **immediately zeroes all motor
  outputs** on its own authority and enters a safe idle state.
- Recovery requires a fresh command after `PING` resumes — motion does not auto-resume.

**Rationale:** the Jetson runs a full Linux userspace on a memory-constrained board with an
OOM-kill risk. It *will* eventually hang or die at a bad moment. A robot whose only stop
mechanism is a `STOP` command from a dead process is a robot that drives into a wall.
Never rely on the Jetson being alive to stop the motors.

---

## Responsibility split

**STM32 owns (real-time, deterministic):**
- Mecanum inverse kinematics: `(vx, vy, omega)` → four wheel velocities
- PID velocity control per wheel, encoder feedback
- Servo PWM generation and gesture sequencing
- LED matrix refresh
- The heartbeat watchdog
- Rejecting unsafe commands (out-of-range velocities, stall detection)

**Jetson owns (soft real-time):**
- Deciding *what* to do and when
- Translating LLM tool calls into command lines
- Sending the heartbeat
- Interpreting `ERR` / `STAT` and reacting

The Jetson **never** generates PWM, runs a PID loop, or computes wheel speeds.

---

## Mecanum kinematics reference (STM32 side)

For wheel radius `r`, half-width `lx`, half-length `ly`:

```
w_front_left  = (vx - vy - (lx + ly) * omega) / r
w_front_right = (vx + vy + (lx + ly) * omega) / r
w_rear_left   = (vx + vy - (lx + ly) * omega) / r
w_rear_right  = (vx - vy + (lx + ly) * omega) / r
```

Sign conventions depend on motor wiring and mounting — **verify each wheel individually**
before trusting composite motion.

---

## Verifying wheel direction

> ### ⚠️ STATUS: NOT YET VERIFIED — no STM32 has been attached to this project
>
> The table below records what the firmware currently *assumes*
> (`mecanum_config_t.direction[]` is `{+1, +1, +1, +1}` in `mecanum.c`). Those are
> defaults, not measurements. **Do not trust composite `DRIVE` motion until this
> procedure has been run and this table filled in.**

Run `scripts/serial/verify_wheels.py`. It drives one wheel at a time with `WHEEL` and asks
what you saw. **Put the robot on blocks first** — every wheel gets driven, and a wrong sign
means it drives the wrong way.

The convention being established: with a positive command, a wheel should turn so that **if
all four were positive, the robot drives forward.** That is what makes the `mecanum_solve()`
sign pattern above hold.

| Wheel | Index | Positive command should turn | Verified | `direction[]` |
|---|---|---|---|---|
| Front left | 0 | forward | ☐ not yet | `+1` (assumed) |
| Front right | 1 | forward | ☐ not yet | `+1` (assumed) |
| Rear left | 2 | forward | ☐ not yet | `+1` (assumed) |
| Rear right | 3 | forward | ☐ not yet | `+1` (assumed) |

Motors mounted mirror-image on the left and right side usually need opposite signs; the two
sides' `direction[]` entries being all `+1` would be unusual, which is another reason not to
trust the defaults.

After the per-wheel check, confirm the composite motions. Each should be unambiguous, and
each fails in a distinctive way if a sign is wrong:

| Command | Expected | If a sign is wrong |
|---|---|---|
| `DRIVE 0.3 0 0 1000` | straight forward | veers or spins |
| `DRIVE -0.3 0 0 1000` | straight back | veers or spins |
| `DRIVE 0 0.3 0 1000` | strafes right, no rotation | rotates, or crabs diagonally |
| `DRIVE 0 0 1.0 1000` | rotates in place, no translation | translates while turning |

A robot that drives forward correctly but strafes diagonally has a **roller** orientation
problem, not a sign problem — mecanum rollers must form an X across the chassis when viewed
from above.

Record the verified values in `mecanum_default_config()` and tick the table above **in the
same change**, so the doc and the firmware cannot drift apart.

---

## LLM tool → command mapping

| Tool call | Serial line |
|---|---|
| `drive(vx, vy, omega, duration_ms)` | `DRIVE <vx> <vy> <omega> <ms>` |
| `stop()` | `STOP` |
| `set_face(emotion)` | `FACE <emotion>` |
| `gesture(name)` | `HAND <name>` |

Clamp all values in the orchestrator **before** writing to serial — never trust raw LLM output
to be within physical limits. The STM32 re-validates as a second line of defence.

---

## Extending the protocol

1. Add the command to the table above **in this doc first**
2. Implement the STM32 parser branch
3. Implement the Jetson-side sender
4. Add the corresponding LLM tool definition only if the model should trigger it

Keep it one line, one action, one response. If a command needs sub-commands or nested
structure, that is a signal the STM32 should own more of the behaviour, not that the protocol
needs to get richer.
