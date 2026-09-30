# robot-face — the panel on the robot

Chat and voice interaction on a screen attached to the robot. Rendered straight to
DRM/KMS: **no desktop, no compositor, no X11, no Wayland, no session.** AGENTS.md
constraint 5 holds — "headless" means no desktop environment, not no display.

    ./scripts/face/build.sh --check     the fast tests: no Slint, no display, robot untouched
    ./scripts/face/build.sh             release build for the panel
    ./scripts/face/build.sh desktop     a window on a developer laptop
    ./scripts/run_stack.sh svc start face

## Why Rust, and why not a compositor

Slint's Python bindings stop at **1.9.2a1 (alpha)** for aarch64/cp310, and JetPack 6.2
ships Python 3.10. Current Slint is 1.17.1. Rust was the only way to run a supported
release.

`cage` and `weston` were both considered. Neither is needed: measured on this board,
`jetson` (group `video`) can take **and release DRM master** on `/dev/dri/card1` with no
root and no seatd, so `backend-linuxkms-noseat` works directly. A compositor would have
added a process and 40–80 MiB for nothing. `nvidia-l4t-weston` remains the fallback if
linuxkms ever fights us — it is vendor-built and already installed, unlike cage's
wlroots 0.15.1.

## Two crates, and the split matters

    face/protocol/   the whole state machine. No Slint, no sockets, no clock.
    face/app/        the Slint UI and the WebSocket client.

`protocol` compiles in ~16 s and tests **while the robot is running**. `app` needs the
LLM stopped to build (see below), so keeping every interesting decision out of it is what
makes this workable. 27 tests cover chunk streaming and de-duplication, face latching,
barge-in, the watchdogs and a refused token.

## Things that were true and surprising

**The display did not exist.** Before `modprobe nvidia-drm`, the only DRM node was
`tegra_drm` on host1x with **zero connector nodes** — not a connector reading
`disconnected`. No KMS UI of any kind was possible. Now persisted in
`/etc/modules-load.d/nvidia-drm.conf`. The panel enumerates as `card1-DP-1`: Tegra's
HDMI port is a DP++ combo, so a DisplayPort name for an HDMI cable is normal.

**Slint exits when no display is connected**, during platform init, before the event
loop. Under `Restart=on-failure` with `StartLimitBurst=5` that means five restarts and
then permanently `failed` — the wrong behaviour for a robot whose panel is unplugged. So
`display::wait_for_connector` polls sysfs first and the service waits patiently instead.

**A Rust build OOM-kills the LLM.** Twice. `spawn_llm` dropped page cache for *model
files only*, and 2.4 GB of cached build artefacts are just as unreclaimable to CUDA on
Tegra. Fixed: `drop_model_cache.py --all`, and `build.sh` stops the LLM for the duration.

**`.cargo/config.toml` is read from the working directory, not the manifest.** Running
`cargo --manifest-path face/...` from the repo root silently ignored `face/.cargo/` and
built into a second 1.4 GB `face/target`. `build.sh` exports `CARGO_TARGET_DIR` instead.

## Cost

**11.0 MiB RSS** (release), plus 8.4 MiB of DRM dumb buffers at 1366×768 double-buffered
— which are in the unified pool but do not show in RSS. The NiceGUI dashboard is 60 MiB,
so the panel can replace it on the box at a net saving.

## Open

- **Arabic shaping is unverified.** Slint's docs say the software renderer handles
  western scripts only; 1.17.1's `Cargo.toml` pulls a full Parley/Swash stack, so the
  docs look stale. The board's only Arabic fonts are KACST and DejaVu. The `--demo`
  script includes an Arabic line specifically to test this — look at it. If shaping is
  broken, vendor Noto Sans Arabic and `import` it in `app.slint`; if it is broken at the
  renderer level, `linuxkms-femtovg` is the fallback at roughly double the RSS.
- **No input device.** `/dev/input/*` holds only gpio-keys and HDA jacks. `stop-requested`
  is wired to Escape and Space and will work the moment a keyboard or touchscreen
  appears; the unit already grants `SupplementaryGroups=input` so it will not silently
  fail then.
- **Pin the connector** with `SLINT_DRM_OUTPUT` once the real panel is fitted
  (`SLINT_DRM_OUTPUT=list` prints what is available and exits).
