# The panel on the robot

`robot-face` is a screen attached to the robot. It shows the conversation, the voice state, the
level meter and the robot's expression. It does **not** replace the dashboard's operator pages.
Source: [`face.md`](../../../dev-docs/modules/face.md) and `face/`.

```
./run.sh slint                      # everything + the panel
./scripts/face/build.sh --check     # the fast tests: no Slint, no display, robot untouched
./scripts/face/build.sh             # release build for the panel
```

## How it fits

* It renders **straight to the display hardware** (DRM/KMS) with Slint's software renderer. **No
  desktop, no compositor, no X11, no Wayland.** Constraint 5 holds: "headless" means no desktop
  environment, not no display.
* It is a client of the orchestrator's WebSocket, like the browser, and imports nothing from the
  Python side. `FACE_WS_URL`, `FACE_PING_S=20` and `FACE_DEAD_LINK_S=45` are in `config/face.env`.
* It is **Rust**, the one approved exception (2026-09-09): Slint's Python bindings stop at an alpha
  for this platform and Python 3.10, and Rust was the only way to run a supported release.
* Two crates: `face/protocol` (the whole state machine, no Slint, no sockets, no clock, 27 tests that
  run while the robot is running) and `face/app` (the Slint UI and the WebSocket client).

## What it costs

**11.0 MiB** resident (release) plus **8.4 MiB** of display buffers at 1366×768, which are in the
unified pool but do not show in RSS. The dashboard is 60 MiB, so the panel can replace it on the box
at a net saving. `MemoryMax=200M` in its unit.

## Things that were surprising

| What | Consequence |
|---|---|
| The display did not exist until `modprobe nvidia-drm` | Now persisted in `/etc/modules-load.d/nvidia-drm.conf`. The panel appears as `card1-DP-1`: the HDMI port is a DP++ combo, so a DisplayPort name for an HDMI cable is normal |
| Slint exits when no display is connected | Under `Restart=on-failure` that would be five restarts then permanently failed. So the panel **waits for a connector** instead |
| A Rust build OOM-killed the LLM, twice | Cached build artefacts are as unreclaimable to CUDA as model files. `build.sh` stops the LLM for the duration |
| `.cargo/config.toml` is read from the working directory | `build.sh` exports `CARGO_TARGET_DIR` rather than trusting it |

## Still open ❓

* **Arabic shaping is unverified.** The software renderer's documentation says western scripts only;
  the dependency tree says otherwise. The demo script includes an Arabic line to test it. Fallbacks
  are a vendored Noto Sans Arabic, or the `linuxkms-femtovg` renderer at roughly double the memory.
* **No input device.** Escape and Space are wired to "stop" and will work when a keyboard or touchscreen
  appears; the unit already grants the `input` group.
* **Pin the connector** with `SLINT_DRM_OUTPUT` once the real panel is fitted (`list` prints what exists).
* It has not been seen on the robot's real display in this handbook's work: there is no screenshot.

## Faces: the panel and the body

The face **names** are sent as written to the panel, the dashboard and the body. The panel and the
body each own how `blink`, `talking` and the idle faces are drawn. The face follows the turn by
rule: `blink` for the acknowledgement, `talking` for the answer, a random idle face when nothing is
happening (see [One turn](../01-overview/one-turn.md)). The body's display is a different thing from
this panel: see [The ESP32 as the body](../03-firmware/esp32-conformance.md).
