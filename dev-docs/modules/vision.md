# robot-vision — the camera, object detection and face recognition

The robot can see. A CSI camera feeds two small ONNX pipelines on the CPU; what they find
reaches the conversation as one sentence in the system prompt, and reaches the dashboard as
boxes over a live picture. Added 2026-10-22 (M12). Camera, objects and faces are all
optional: with `VISION_ENABLED=0`, or no camera, the robot is exactly what it was.

    ./scripts/models/fetch_vision.sh                     the three models, ~57 MiB
    ./run.sh all                                         starts it (when a camera and the models exist)
    ./scripts/run_stack.sh svc restart vision            one service
    ./scripts/measure/vision_smoke.py image|faces|camera numbers, on a photo or the live sensor
    http://<jetson>:8081/talk                            the picture, who it knows, enrol / forget (the Vision page, merged into Talk in M13)

## What is plugged in

An **IMX219** on CSI sensor 0 (`/dev/video0`, `imx219 10-0010`), reached through NVIDIA's
Argus (`nvargus-daemon`, GStreamer's `nvarguscamerasrc`). `v4l2-ctl` lists it but cannot read
frames the way a USB camera allows: the sensor sits behind the ISP. Modes: 3280x2464@21,
3280x1848@28, 1920x1080@30, 1640x1232@30, **1280x720@60/30** (the one used).

Argus allows **one client at a time**. Stop `robot-vision` before running anything else that
opens the sensor, including `vision_smoke.py camera`.

## How it is built

```
nvarguscamerasrc 1280x720@30    the ISP: exposure, white balance, demosaic
  -> nvvidconv                  the hardware scaler makes 640x360 -- no CPU resize
  -> tee
       -> RGB frames, `VISION_FPS` a second, on a private pipe      -> the models
       -> a JPEG, `VISION_JPEG_FPS` a second, into ONE file         -> the dashboard
```

One `gst-launch-1.0` child per `Camera` (`src/robot/vision/camera.py`), driven the way
`robot.asr.capture` drives `arecord`: the platform tool does the device work, a pipe carries
the result, **no Python binding enters the runtime path**. That is also the only way in — the
board's OpenCV (cv2 5.0, installed twice alongside a 4.10 headless build, both in the user
site) was built **without GStreamer**.

* Frames go down a **private descriptor** (`fdsink fd=N` + `pass_fds`), not stdout. Argus
  prints `GST_ARGUS:` / `CONSUMER:` lines to stdout; one inside a 691 KB frame would be a
  corrupt frame that looks fine until a model reads it.
* The reader keeps **only the newest frame**. Inference is slower than capture; a queue would
  analyse the past.
* **No frame for `stall_s` (10) → stop and restart**, backing off to 30 s. Stopping is
  `SIGINT` (the pipeline has `-e`, so it becomes EOS and Argus closes the sensor); a SIGKILL
  leaves the camera "busy" for the next client. The service never exits over a camera that
  is not there.
* The JPEG is **one file rewritten in place**, validated by its JPEG start/end markers when
  read (a torn read is retried once) and refused when older than 15 s — the last frame of a
  dead camera is the most misleading picture there could be.

## The models

| | model | licence | size | cost on this board (CPU, 3 threads) |
|---|---|---|---|---|
| objects | **YOLOX-Tiny**, 416x416, COCO-80 | Apache-2.0 | 20 MiB | **145 ms** |
| face detection | **YuNet 2023mar**, 640x640, 5 landmarks | MIT | 0.2 MiB | **25 ms** (6 faces, 640x360) |
| face recognition | **SFace 2021dec**, 112x112 → 128-d | Apache-2.0 | 37 MiB | **29 ms per face** |

**Why not YOLOv8 and InsightFace**, which are the obvious names (and are installed in the
user site): Ultralytics' weights are AGPL-3.0 and its package imports PyTorch, which
AGENTS.md constraint 4 forbids in the runtime path; InsightFace's pretrained models are
non-commercial research only, and this robot may end up in a school. Both of those packages
are therefore **not used**; only their sample images appear in the tests, and only when
present. The three models above are plain ONNX and run on the `onnxruntime` already
installed for Supertonic. `yolox_nano` (3.6 MiB, weaker) and `yolox_s` (34 MiB, ~4x slower)
load without a code change (`VISION_DETECT_MODEL`).

**Why the CPU.** A CUDA session in a fourth process pays its own context out of the same
unified pool as the LLM, whisper and Supertonic — 0.6-1.3 GiB as measured for Supertonic —
for models that already run at the rate the robot looks around. `VISION_USE_GPU=1` exists
and is **not measured here**: the stack was resident with <1 GiB free.

**No OpenCV.** `robot/vision/imaging.py` is the whole of it in numpy: bilinear resize,
letterbox, a least-squares similarity transform (Umeyama), an inverse-mapped affine warp
for the 112x112 face crop, NMS. GStreamer already did the one expensive resize. This keeps a
fragile dependency (two conflicting wheels, 60+ MiB resident) out of the runtime and costs
~100 lines that have tests.

### Face recognition: what it does and does not mean

Each face becomes 128 numbers; two photographs of the same person point the same way. A face
**matches** someone enrolled when the cosine similarity is ≥ **0.363** (OpenCV's own figure
for SFace; `VISION_FACE_THRESHOLD`). A person is the best of their stored samples, so
enrolling again in different light only helps.

Measured here (`vision_smoke.py faces`, real photographs, through the service's own code):

| | similarity |
|---|---|
| enrolled one photo; a **different photo of the same person** | **0.756** → recognised |
| three other people against that enrolment | 0.097 – 0.135 → unknown |
| six people in one photograph, worst impostor pair | 0.349 (threshold 0.363) |
| the same six, darker / mirrored / zoomed | genuine ≥ 0.62 (mirror), ≥ 0.79 (dark, zoom) |

The channel order was settled by the real pair above, not by memory of the reference code:
RGB 0.756 against BGR 0.655.

**This is for context and greeting, not for security.** There is no liveness or
anti-spoofing: a photograph of an enrolled person will be recognised as them. Never put a
door, a purchase or a permission behind it.

**It is biometric data.** Enrolled faces live in `run/faces.db` — local, gitignored with
every other database, never sent anywhere, 128 floats a sample (no photograph is stored).
`Forget` on Config -> People (or `{"type":"forget","name":...}` on the socket) removes a
person and every sample completely -- and, from M13, what was said to them
(`dev-docs/modules/people.md`). Asked directly of robot-vision, `forget` still removes only the face. Enrolment refuses a face that already matches someone
else, unless forced.

## What it costs

Measured 2026-10-22 on the live camera, objects and faces on, 2 fps, a still room
(`PSS`, because the GStreamer child shows 152 MiB resident of which only 41 are its own):

| | python service | gst child | **total** | CPU |
|---|---|---|---|---|
| YOLOX-Tiny, **motion gate on**, nothing moving | 166-176 MiB | 41 MiB | **~213 MiB** | **~0.27 core** |
| YOLOX-Tiny, gate off (every frame analysed) | 174 MiB | 42 MiB | 216 MiB | ~1.05 core |
| YOLOX-Nano, gate off | 140 MiB | 39 MiB | 179 MiB | ~0.56 core |

`catalog.VISION_RAM_MIB = 215` prices it on the Memory page. With it included, the project's
own pricer puts every selection inside the 7168 MiB target — Qwen2.5-3B + whisper
large-v3-turbo q5_0 + Supertonic on the GPU + camera: **6024 MiB steady, 1546 MiB headroom**;
the largest LLM tested, Qwen3-4B: 6782 MiB, 788 headroom. (Those prices exclude a development
session, ~1.4-1.8 GiB — do not run the two together.)

### The motion gate

Re-reading an unchanged picture was ~0.9 of a core. So frames are compared first, in
16x16 blocks, and one where nothing moved **reuses the last scene** (freshly stamped, marked
`"still": true`) instead of running the models. Measured in this room, dim, sensor at high
gain, mean change per block between two frames:

| | 8 px blocks | **16 px** | 32 px |
|---|---|---|---|
| a still scene | 2.1 | **1.1 – 1.3** | 0.6 – 0.9 |
| a 60x140 px person arrives | 3.7 | **2.9** | 2.8 |
| a 100x220 px person | 7.1 | **6.7** | 6.7 |
| a hand, 40x40 px | 2.4 | 1.5 | 1.0 |

At 8 px the noise sits above any usable threshold — the first version of the gate skipped
**nothing** for exactly that reason, and the threshold I had guessed (1.2) was inside the
noise. 16 px with `VISION_MOTION=2.0` sits between noise (≤ 1.3) and the weakest realistic
person (2.9). Three things stop it being a blind spot: the comparison is against the last
*analysed* frame, so a slow movement accumulates instead of creeping under the threshold;
brightness is judged relative to the whole frame, so the lights coming on or auto-exposure
drifting is not somebody arriving; and a still scene is re-analysed every `VISION_REFRESH_S`
(3 s) regardless. A hand-sized movement does not clear 2.0 and is picked up by that refresh.
Enrolment bypasses the gate — three identical frames are not three samples. A brighter room
has less grain and could use a lower threshold.

## In the conversation

`robot-core` holds one subscription to `run/vision.sock` (`_pump_vision`) and keeps the
newest scene. When a turn builds its prompt, `scene.summarize` turns it into one sentence,
added **last** in the system prompt (so llama.cpp's cached prefix — the persona — survives;
the scene is the one line that changes turn to turn):

> Right now your camera shows: Barack (someone you know); 2 people you do not recognise;
> objects: tie x2. This is live from your camera. Use it only when it helps answer what was
> asked -- never describe the scene unprompted, never claim to see anything that is not
> listed, and call someone by name only if they are listed as someone you know.

* **Only a fresh scene (≤ 6 s) is said at all.** A stalled camera or a dead service reads as
  "cannot see", never as the last thing it saw.
* **A name appears only when the face matched.** Everyone else is "a person you do not
  recognise".
* **"Nothing it recognises" is said too** when the camera works and sees nothing, so the
  model is not left free to invent a scene. Live, in the empty room: *"I don't see anything
  right now."*
* ~40-70 tokens a turn on a window that is already the scarcest thing here.

Verified end to end against the running `llama-server` (Qwen3-0.6B): with a known person the
reply was *"I see Barack in front of me"* / *"Barack is in front of me"* (3/3 questions); with
the empty live camera, *"I don't see anything right now."* A "sharper", more imperative
wording was **worse** (0/3) on the same model and was not adopted — keep the sentence as it
is unless you re-measure. Answer quality depends on the LLM: the 0.6B is the weakest, which
is Known Issue #1 in `PROGRESS.md` and not a vision problem.

## Health

One row, **Seeing (camera)**, with three states because "connected" is not "seeing":
**UP** while scenes arrive; **UNKNOWN** while the process is up and the camera is not (a stalled
Argus, a loose ribbon — a restart would change nothing, so keepalive leaves it); **DOWN** when
the process has gone (keepalive restarts it, bounded, like the others). `pong` carries the
camera state and the last reason (`camera busy`, `no cameras available`...) read from what
GStreamer and Argus printed.

## Settings

All in `config/vision.env`; none is a dashboard setting except the two switches on the Conversation
page (objects, faces), which are live. `VISION_FPS` is the knob for how much CPU the robot
spends looking (each analysis costs ~0.15 s of three cores). `VISION_FLIP` for an inverted
mount (0, 2, 4, 6 only — the 90° ones would swap the frame size).

## Gotchas found building it

* **`systemd`: no `PrivateTmp`.** Argus reaches the daemon through `/tmp/argus_socket`; a
  private `/tmp` is empty and the sensor never opens. Every other robot unit has
  `PrivateTmp=true`, which is why `robot-vision.service` says so in a comment. Verified the
  camera still opens under `ProtectSystem=strict` + `NoNewPrivileges` with the real `/tmp`.
* **The first frame is dark.** Auto-exposure needs ~1 s; frame 1 had a mean of 14/255.
* **Grain.** This room is dim and the sensor runs at high gain: ~1.1 mean change per 16 px
  block between two frames of a still scene, and a 250 KB JPEG at 640x360.
* **Pink cast** in the dashboard picture (R/G/B means 114/105/113). The ISP's white balance
  is Argus's; not tuned. It does not affect detection noticeably.
* **`pkill -f` / `grep` on a command line kills your own shell** (TROUBLESHOOTING.md) — twice
  during this work, once more in a cleanup loop. Match argv elements or `/proc/PID/exe`.
* Pillow reads images in `vision_smoke.py` and the tests; **the service never imports it**.

## Not done

* Voice enrolment ("remember my face as Deny") — needs a body field in the reply schema or a
  tool; the dashboard and the socket enrol today.
* Proactive greeting when a known person arrives. The data is there (`scene.faces[].name`).
* Tracking, so a name follows a person who turns away. Each frame is judged alone.
* `VISION_USE_GPU=1` is unmeasured; a USB camera is not supported (CSI only).
* Camera tuning (white balance, exposure ceiling for the low-light grain).
