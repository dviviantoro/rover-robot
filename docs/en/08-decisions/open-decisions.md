# Waiting for the owner

Things found while writing the handbook, or recorded as "please confirm" in the tracker, that **a
person has to decide**. Nothing here was changed without that decision. Numbers like `#18` are
**Known issues** in [`PROGRESS.md`](../../../dev-docs/ai/PROGRESS.md) § 3. Dated 2026-11-16.

## A. Before the first wheel test

| # | Question | Why it blocks | Suggested answer |
|---|---|---|---|
| ~~#18~~ | ~~Which way is `+vy`, and which way is `+omega`?~~ | | **Settled 2026-11-16**: `+vy` left, `+omega` counter-clockwise; six places corrected ([Motion](../03-firmware/motion.md#settled-which-way-is-vy-and-which-way-is-omega)) |
| ~~#21~~ | ~~How is the watchdog proved on the real board?~~ | | **Settled 2026-11-16**: `watchdog_test.py --port` is a guided check ([how](../03-firmware/watchdog.md#proving-it-on-a-real-board)). It has never been run on a board |

## B. Before the ESP32 is a first-class body

| # | Question | Notes |
|---|---|---|
| **#19** | **The name.** 63 tracked files and hard constraints 6 and 7 say STM32. Rename to "body controller"? | Touches the constraints table, so it needs sign-off. The handbook says "body controller" and explains the name in the [glossary](../01-overview/glossary.md#body-controller) |
| **#19** | **The USB-UART chip.** `setup_udev.sh` matches STMicroelectronics only, and `check_board.sh` looks at `ttyACM*` with three vendors. A CP2102 or CH340 board gets no `/dev/robot-stm32` and is reported as "no STM32" | Options: extend both to the chip's vendor and product ids; or rely on discovery by ping and the stable `/dev/serial/by-id` name it already saves. The second needs no change |
| ~~#20~~ | ~~`STATUS` is never requested~~ | **Settled 2026-11-16**: polled every 2 s, stall confirmed over two samples ([how](../05-interfaces/serial-protocol.md#status-and-the-stall-flag)) |
| **HAND** | **No servos listed.** What should the body do with `HAND wave`? | Accept and do nothing (`OK HAND`, then `DONE HAND`) is the least surprising. `ERR HAND unknown` makes the Jetson tell the model the gesture was refused |
| **Lidar, SPI camera** | What are they for? **No code, no protocol and no unit exists for either**, and navigation is a non-goal in `PROJECT_BRIEF.md` | A scope decision. Navigation would also reopen the ROS 2 question ([Rejected](rejected-alternatives.md)) |
| **#5** | Discovery sends `{PING_robot}` to **every** `ttyACM*`, `ttyUSB*` and `ttyTHS*`. A lidar on a USB-UART adapter, or anything on the Jetson's header UARTs, gets it too | A vendor-id allow-list or an opt-in would be safer. It was deliberate in `3e37e34`, so it is a decision, not a bug |
| **#6** | The serial link reports **UP while the board is silent**: `last_rx` is tracked and never read. A hung body still shows green | "No line for 2 to 3 s" is a cheap, reliable "body hung". Matters more with a body that may reset when its port opens |

## C. Recorded as "proposed, please confirm"

From the decision log in `PROGRESS.md`:

| Date | Proposal | What to say yes to |
|---|---|---|
| 2026-10-22 | `NoNewPrivileges` dropped on `robot-core`, `robot-ui` and `robot-tts` | The sudoers rule (six units, three verbs) is the privilege boundary. Revert if you would rather give up the dashboard's restart buttons and on-demand embeddings than allow `sudo` there |
| 2026-10-22 | The dashboard may ingest only from `run/uploads/` and `knowledge/` | Add a root to `INGEST_ROOTS` in `core/app.py` if you need another |
| 2026-10-22 | Vision runs on the **CPU** | A fourth CUDA context costs 0.6 to 1.3 GiB. `VISION_USE_GPU=1` exists and is unmeasured |
| 2026-10-22 | No OpenCV and no new package | Constraint 4 and the licences |
| 2026-10-22 | Face recognition is **context only, not security** | No liveness: a photograph matches |
| 2026-10-29 | A **typed** line is filed under whoever the camera sees | The person at the keyboard may not be the one in front of the camera. Say if typed lines should never be filed |

## D. Records that looked out of date: corrected 2026-11-16

Not decisions. Each was checked against the system and then fixed, so nobody trusts a stale line.

| Where | It said | Now |
|---|---|---|
| `PROGRESS.md` "Currently configured" line and Known issues #1, #12 | the LLM is Qwen3-0.6B, whisper q5_0, voice F1 | updated to LFM2.5-2.6B-Q5_K_M, whisper q8_0, Supertonic M1; #1 and #12 marked superseded (the "tail 100 ms" is a dashboard pin; `config/core.env` says 700) |
| `TECH_STACK.md` § Think | Qwen3-4B is the default | a note at the top says the configured model is LFM2.5-2.6B |
| `firmware/stm32/README.md`, `README.md`, `TECH_STACK.md` | 628 native checks | 634 |
| `README.md` | the root filesystem is about 94% full | about 96%, 4 GB free |
| `robot-core.service` comment | the sudoers rule names five units | six |
| `.env.example` | lacked `CORE_WS_TOKEN` | now lists it, empty |

## E. What the handbook still needs from you

The hardware chapter (02) is the one that cannot be written without the owner and the robot. It
needs:

| Needed | For |
|---|---|
| The exact **ESP32 module** and the **USB-UART chip**, and the baud rate its firmware uses | the link, the udev rule, the conformance page |
| The **firmware source** (or where it lives) | the firmware chapter, which today documents only the reference |
| The **motor driver**, motors, gearing and **encoder counts per revolution**; wheel diameter, wheelbase and track | the kinematics constants and the PID |
| The **power tree**: battery type and voltage, what feeds the Jetson, the ESP32, the motors, the sensors | a power diagram, and the battery sense scaling |
| The **face display** type and how it is wired | what `FACE` really draws |
| The **lidar** (model, how it connects) and the **SPI camera** (model, which processor reads it) | a sensors page, and the scope question above |
| Whether there are **servos** | the `HAND` question |
| **Photographs** of the chassis and the wiring, and a wiring list | the pictures and the pin table |
| Whether the watchdog was **ever tested on the ESP32** | the reliability table |
