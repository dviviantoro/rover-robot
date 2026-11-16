# Waiting for the owner

Things found while writing the handbook, or recorded as "please confirm" in the tracker, that **a
person has to decide**. Nothing here was changed without that decision. Numbers like `#18` are
**Known issues** in [`PROGRESS.md`](../../../dev-docs/ai/PROGRESS.md) § 3. Dated 2026-11-16.

## A. Before the first wheel test

| # | Question | Why it blocks | Suggested answer |
|---|---|---|---|
| **#18** | Which way is `+vy`, and which way is `+omega`? The formulas and the Jetson say **left** and **counter-clockwise**; the protocol document's examples, `verify_wheels.py`, the `stm32_console.py` shorthands and the `drive` tool's `right` alias say **right** and **clockwise** ([evidence](../03-firmware/motion.md#open-question-which-way-is-vy-and-which-way-is-omega)) | A correct robot would look wrong, and a right sign might be "fixed" | Keep the formulas and the Jetson (no firmware or behaviour change) and correct the five places that disagree. **The protocol document is the contract**, so this is the owner's call |
| **#21** | How is the watchdog proved **on the real board**? `watchdog_test.py --port` waits for a state file that only the simulator writes, so on a real port it fails at step 3 | The firmware README says nothing else should be trusted until this passes on real silicon | Something visible: a scope or LED on the motor driver's standby or PWM pin, then the wheels off the ground. Optionally teach the script to read an LED or a pin state through an agreed `STATUS` field |

## B. Before the ESP32 is a first-class body

| # | Question | Notes |
|---|---|---|
| **#19** | **The name.** 63 tracked files and hard constraints 6 and 7 say STM32. Rename to "body controller"? | Touches the constraints table, so it needs sign-off. The handbook says "body controller" and explains the name in the [glossary](../01-overview/glossary.md#body-controller) |
| **#19** | **The USB-UART chip.** `setup_udev.sh` matches STMicroelectronics only, and `check_board.sh` looks at `ttyACM*` with three vendors. A CP2102 or CH340 board gets no `/dev/robot-stm32` and is reported as "no STM32" | Options: extend both to the chip's vendor and product ids; or rely on discovery by ping and the stable `/dev/serial/by-id` name it already saves. The second needs no change |
| **#20** | **`STATUS` is never requested.** Battery, temperature and the stall flag cannot reach the dashboard or the model, because nothing sends `STATUS` outside the protocol test | Poll it every few seconds beside the heartbeat; or let the body volunteer a `STAT` line on change; or piggyback it on `OK PING`. Either way the stall test needs a debounce first |
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

## D. Records that look out of date

Not decisions, but the next person will trust them. Each was checked against the system on
2026-11-16 and **none was edited**.

| Where | Says | Today |
|---|---|---|
| `PROGRESS.md` Known issue #1 and the "Currently configured" line | the LLM is **Qwen3-0.6B Q4_K_M**, whisper **q5_0**, voice **F1** | `./run.sh model` and `--check`: **LFM2.5-2.6B-Q5_K_M**, whisper **q8_0**, Supertonic **M1**. Known issues #1 and #12 (about that model) may no longer apply. The "tail 100 ms", `tts_volume` 11 and `mic_gain` 52 in those lines are still true: they are pins |
| `TECH_STACK.md` § Think | Qwen3-4B Q4_K_M is the default | the same: LFM2.5-2.6B |
| `firmware/stm32/README.md`, `README.md`, `TECH_STACK.md` | **628** native checks | **634** (run today; `PROGRESS.md` already says 634) |
| `README.md` | the root filesystem is about 94% full | **96%**, 4 GB free |
| `setup_sudoers.sh` header, `robot-core.service` comment | five units | six units are listed |
| `.env.example` | lists the dashboard's three settings | `CORE_WS_TOKEN`, which the orchestrator and both screens read, is not listed |

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
