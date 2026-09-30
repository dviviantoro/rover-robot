# firmware/stm32 — the robot's body

Implements the STM32 half of [`serial-protocol.md`](../../dev-docs/architecture/serial-protocol.md).
The Jetson sends intent; this decides how to execute it (AGENTS.md constraint 6).

> ## ⚠️ Status: builds, tested, **never run on hardware**
>
> No STM32 has been attached to this project yet. The portable core is covered by 628
> native checks and driven end-to-end through a real serial device by the host
> simulator — but `board_stm32.cpp`, the pin map, the PID gains and the wheel sign
> convention have never met a motor. Treat every pin and constant in `robot_pins.h` as
> a proposal, not a measurement.

## Layout

The split is the point: everything that *decides* anything is portable C with no HAL,
so the safety-critical logic runs under test on a normal machine.

| File | Hardware? | What |
|---|---|---|
| `src/protocol.c` | no | Line parsing and response formatting |
| `src/watchdog.c` | no | **The 500 ms heartbeat timeout** |
| `src/mecanum.c` | no | Body velocity → four wheel velocities |
| `src/pid.c` | no | Per-wheel velocity control, anti-windup |
| `src/robot.c` | no | Command state machine; ties the above together |
| `src/board_stm32.cpp` | **yes** | Motors, encoders, servos, LED matrix, ADC |
| `src/main.cpp` | **yes** | USB-CDC read loop; calls `robot_tick` every pass |

`robot_board.h` is the only interface between the two halves.

## Build and test

```bash
./run_native_tests.sh          # 628 checks, needs only gcc — no hardware, no PlatformIO
pio run                        # build for the STM32
pio run -t upload              # flash
pio device monitor             # watch the link
```

PlatformIO's core directory must stay off the root filesystem (it is ~94% full):

```bash
export PLATFORMIO_CORE_DIR=/media/jetson/DATA/robot-py/.cache/platformio
```

Current footprint on an F411CE: **6.1 KB RAM (4.7%), 61 KB flash (11.6%)**.

## Without a microcontroller

`tools/stm32_sim` compiles this exact core for the host and speaks the protocol over a
pseudo-terminal, so the Jetson side and the watchdog can be exercised with no board:

```bash
./tools/stm32_sim/build.sh
python scripts/serial/watchdog_test.py --sim     # kill -9 the brain, watch the wheels stop
python scripts/serial/protocol_test.py --sim     # every documented line, both sides
```

Both scripts take `--port /dev/robot-stm32` instead of `--sim` once hardware exists.

## Bring-up order, when a board arrives

Do these in order. Skipping ahead makes failures much harder to attribute.

1. **Flash with motors disconnected.** Confirm it enumerates as `/dev/ttyACM*` with
   vendor `0483`, and that `scripts/serial/stm32_console.py` gets `OK PING`.
2. **Check the watchdog before connecting motors:**
   `python scripts/serial/watchdog_test.py --port /dev/robot-stm32`.
   Nothing else should be trusted until this passes on real silicon.
3. **Verify each wheel individually, robot on blocks:**
   `python scripts/serial/verify_wheels.py --port /dev/robot-stm32`.
   Write the result into `mecanum_default_config()` **and** the table in
   `serial-protocol.md`, in the same change.
4. **Encoders:** confirm counts rise when a wheel turns the positive way, and that
   `ENCODER_COUNTS_PER_REV` matches reality — turn a wheel exactly one revolution by
   hand and read the count. Getting this wrong scales every velocity by a constant.
5. **Tune the PID gains**, one wheel at a time, with `WHEEL`.
6. **Only then** composite `DRIVE`, using the table in `serial-protocol.md`.

## Two traps already hit

**Do not name a header `board.h` or `pins.h`.** STM32duino has its own
`cores/arduino/board.h`; because `-Iinclude` precedes the framework's path, `Arduino.h`
picked up ours instead and the build failed with `LL_GPIO_PULL_NO undeclared` in a file
a hundred lines away. Project headers are prefixed `robot_` for this reason.

**Do not name a type `pid_t`.** POSIX owns it, and newlib pulls `sys/types.h` in under
the Arduino framework. Ours is `robot_pid_t`.
