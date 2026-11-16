# What the robot is

You talk to it. It listens, thinks, answers out loud and, if you ask, moves. It speaks
**English, Bahasa Indonesia and Arabic**, works out which one you used from your own words,
and answers in that language, with no setting to change. All of it runs on the robot:
there is no cloud service anywhere in the loop.

## What it does today

| Ability | In one line | State |
|---|---|---|
| **Listen** | Always-on microphone, a voice detector, then the open speech decoder whisper.cpp on the GPU | ✅ |
| **Think** | A language model that runs on the Jetson, constrained to answer in one fixed JSON shape | ✅ |
| **Speak** | One synthetic voice for all three languages. It starts talking **before the answer is finished** | ✅ |
| **Be interrupted** | Can be set to stop talking when you speak over it (`interrupt` mode). The default is `no_interrupt`, because the speaker and the microphone are one USB device and the robot otherwise hears itself | ✅ |
| **Learn from documents** | Drop in a PDF or type a fact. It is searchable in any of the three languages | ✅ |
| **See** | A camera finds objects and faces. A one-sentence description of the scene is given to the model | ✅ (not yet tried with a live person) |
| **Know who is talking** | Asks a stranger their name once, then recognises them, greets them and carries the conversation on | ✅ (not yet tried with a live person) |
| **Move** | The model's answer carries "walk forward 2 steps" or "wave"; the robot turns that into short, bounded serial commands | 🧪 (the body has not been attached; see below) |

Sources: [`PROGRESS.md`](../../../dev-docs/ai/PROGRESS.md) status snapshot,
[`PROJECT_BRIEF.md`](../../../planning/PROJECT_BRIEF.md).

## What it deliberately does not do

* **No cloud, ever.** There is no fallback to an online service. This is a hard rule.
* **No navigation.** No mapping, no obstacle avoidance, no path planning. (A 360-degree lidar
  has been reported on the body, but nothing in this repository reads it. See the hardware
  table below.)
* **No vision-language model.** The camera feeds small detectors, not a model that reads
  pictures.
* **Face recognition is not security.** It exists to greet people and continue a
  conversation. There is no check that a face is a live person, so nothing that matters may
  ever sit behind it.
* **No ROS 2.** One program talks to one microcontroller over one wire. See
  [`PROJECT_BRIEF.md`](../../../planning/PROJECT_BRIEF.md) § Rejected alternatives.

## The hardware at a glance

The picture is on the [front page](../README.md). This table says how far each part is
proven.

| Part | What it does | State | Evidence |
|---|---|---|---|
| Jetson Orin Nano 8 GB (Super) | All AI and orchestration | ✅ | JetPack 6.2.1, MAXN_SUPER power mode, no desktop |
| USB sound card, microphone | Audio in | ✅ | Transcription checked in all three languages |
| USB sound card, speaker | Audio out | ✅ | Real-time playback checked; the speaker is pinned by name in `config/tts.env` |
| CSI camera, IMX219 | Sight | ✅ | Found and tested at 3 frames a second, clean shutdown |
| Screen on HDMI/DisplayPort (optional) | The on-robot panel | ✅ | The display path works at 1366×768; no touch or keyboard is attached |
| **ESP32 body controller** | Real-time control and safety | 📐 | Reported by the owner. Its firmware is not in this repository and it is not attached to this Jetson today |
| 4 DC motors with encoders, mecanum wheels | Drive in any direction | 📐 | In the design ([`robot_pins.h`](../../../firmware/stm32/include/robot_pins.h)); wheel directions have never been checked |
| Face display | Expressions | 📐 | Owner-reported; the `FACE` command is defined and simulated |
| Battery voltage and other sensors | The `STAT` status line | 📐 | Defined in the protocol; the reference firmware reads a battery divider |
| Servos for hand gestures | The `HAND` command | ❓ | The protocol has `HAND`; the owner did not list servos |
| 360-degree lidar, SPI camera | Not used by anything yet | ❓ | Owner-reported; no code or protocol for them exists here |

**What "the body" means in this repository.** The project started with an STM32 design, so
the reference firmware and the host simulator are called `firmware/stm32` and
`tools/stm32_sim`. The robot's real controller is an ESP32 that speaks the same text
protocol. The reference firmware is still the executable description of that protocol, and
it is what the tests run against. See the [glossary](glossary.md#body-controller).

## Four rules that explain most decisions

1. **One 8 GB pool.** CPU and GPU share it, so going over means a process is killed, not
   that things slow down. Every model is priced against the pool before it loads. See
   [Models and memory](../04-brain/models-and-memory.md).
2. **The body owns everything real-time.** The Jetson says *what* ("go forward a little");
   the microcontroller decides *how* (wheel speeds, timing). If the Jetson goes quiet for
   half a second, the body stops the motors by itself.
3. **Everything is a supervised service.** Nine small programs, each restarted if it dies,
   so one crash cannot take the robot down. See [The brain](../04-brain/README.md).
4. **The screens are optional.** The dashboard and the on-robot panel are visitors: closing
   or crashing them changes nothing about the robot.

The full list, with the reasons, is in [`AGENTS.md`](../../../AGENTS.md) § Hard constraints.

## Next

* [One turn, end to end](one-turn.md): what happens between "hello" and the first spoken word.
* [The brain](../04-brain/README.md): the nine programs and how they talk to each other.
