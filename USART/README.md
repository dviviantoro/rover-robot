# USART/ — a vendor sample, not part of the robot

`USART.py` is an example program supplied with a **serial motor-driver board** (comments in
Chinese and English; "520 motor", "TT motor", `$spd:m1,m2,m3,m4#`, `/dev/ttyUSB0`, 115200
baud, messages ending in `#`).

**Nothing in this repository imports, runs or tests it.** The robot's Jetson-to-body protocol
is a different one: newline-terminated text lines (`DRIVE`, `STOP`, `FACE`, `HAND`, `PING`),
defined in [`dev-docs/architecture/serial-protocol.md`](../dev-docs/architecture/serial-protocol.md).

It is kept as a reference for what such a board can do (set motor type, dead zone, encoder
lines, gear ratio, wheel diameter, closed-loop speed, raw PWM, encoder upload). If the body
ever uses a board like this one, that is a hardware chapter question: see
[`docs/en/README.md`](../docs/en/README.md) § The handbook map.

It also opens `/dev/ttyUSB0` at import time, so do not run it on the robot while
`robot-core` holds the body's serial port.
