# Reply JSON: what the model answers, and what the body is told

The robot answers with a **JSON object**, not with prose that has markers in it. Only one field
is spoken; the others say what the body does *while* it speaks.

```json
{"answer": "Baik, saya maju sedikit.", "language": "id",
 "foot_direction": "forward", "foot_step": 2,
 "hand_movement": "none", "face": "happy", "style": "cheerful"}
```

Source: [`src/robot/core/reply.py`](../../../src/robot/core/reply.py).

## The seven fields

| Field | Type and values | What becomes of it |
|---|---|---|
| `answer` | string | **The only spoken field.** Plain sentences, no markup, no emoji |
| `language` | `id`, `en` or `ar` | The model's own opinion. The language actually used is decided from the person's words (see [Voice pipeline](../04-brain/voice-pipeline.md)) |
| `foot_direction` | `none`, `forward`, `backward`, `left`, `right`, `turn_left`, `turn_right` | Becomes one `DRIVE` |
| `foot_step` | integer 0 to 10 | How long: one step is 600 ms. Clamped to 0 to 10 |
| `hand_movement` | `none`, `wave`, `point`, `home`, `nod` | Becomes one `HAND` |
| `face` | `neutral`, `happy`, `thinking`, `listening`, `speaking`, `sad` | Under the default phase rules the face is set by the phase, not by this field |
| `style` | `neutral`, `cheerful`, `calm`, `excited`, `apologetic` | Carried to the dashboard and the body. **The voice does not act on it** |

A step is `STEP_MS = 600` at `STEP_SPEED = 0.2` m/s, about 12 cm; a turning step is 1.2 rad/s,
about 41°. Ten steps is the most that can be asked, 6 s.

How the direction becomes numbers (a unit step before the 0.2 m/s scaling), from
`_STEP_VECTORS` in `turn.py`:

| `foot_direction` | vx | vy | omega |
|---|---|---|---|
| `forward` / `backward` | +1 / −1 | 0 | 0 |
| `left` / `right` | 0 | +1 / −1 | 0 |
| `turn_left` / `turn_right` | 0 | 0 | +1.2 / −1.2 |

(Whether `+vy` really is *left* on the chassis is [Known issue #18](../03-firmware/motion.md#settled-which-way-is-vy-and-which-way-is-omega).)

## Why it is built this way

* **The shape is enforced, not requested.** llama.cpp is given a JSON schema and cannot produce
  anything else: not prose, not half a JSON object, not an invented field. Asking for markers in
  free text failed roughly one turn in twenty. `SCHEMA` in `reply.py` is the whole contract, and
  adding a field there adds it everywhere.
* **`answer` is the first property, and that is load-bearing.** The grammar generates properties
  in schema order, so the sentence to speak exists before the body fields do, and `AnswerStream`
  reads the answer out of the token stream *as it is written*. Waiting for the closing brace
  would add the whole generation time to the delay before the first word.
* **A vocabulary the body does not know is a command it answers `ERR` to.** The enums in
  `reply.py` are kept in step with the body's faces and gestures for that reason.

## What happens when it goes wrong

| Situation | What the robot does |
|---|---|
| The reply is not readable JSON | It **speaks the raw text** as the answer, with no body actions. Better to say something than nothing |
| A field has a value outside its list | That field falls back to its neutral value (`none`, `neutral`, no language) |
| `foot_step` is not a number or is out of range | It becomes 0, or is clamped to 0 to 10 |
| The reply hits the token limit mid-answer | The object never closes, so `language`, `foot_direction`, `foot_step`, `hand_movement`, `face` and `style` are **never generated**: the robot speaks half a sentence and does not move. It now says so: one WARNING, a `truncated` event on the dashboard, and a `truncated` flag in the turn's numbers |
| Arabic or emoji in `answer` | `\uXXXX` escapes are decoded, and surrogate pairs are joined. A lone half is dropped, because it cannot be encoded and once ended a turn mid-reply |

The reply format can be switched with `CORE_REPLY_FORMAT`: `json` (the default) or `markers`, the
older prose-with-`[do:...]` form, which needs nothing from the server beyond streaming.

## The line the body receives

Once per phase of a turn, `robot-core` also writes the same object to the body as **one compact
ASCII JSON line**, with an added `phase`. It starts with `{`, so it cannot be mistaken for a verb.

| `phase` | When | `answer` | `face` |
|---|---|---|---|
| `acknowledgement` | the spoken "okay, one moment" starts (`CORE_FILLER=1`) | the phrase | `blink` |
| `answering` | the model's reply is complete | what was said | `talking` |
| `idle` | the turn is over, and once at start and on every reconnect | empty | `random` |

```
{"phase":"acknowledgement","answer":"Oke, sebentar ya.","language":"id","foot_direction":"none","foot_step":0,"hand_movement":"none","face":"blink","style":"neutral"}
{"phase":"answering","answer":"Halo! Ada yang bisa saya bantu?","language":"id","foot_direction":"none","foot_step":0,"hand_movement":"none","face":"talking","style":"neutral"}
{"phase":"idle","answer":"","language":"id","foot_direction":"none","foot_step":0,"hand_movement":"none","face":"random","style":"neutral"}
```

* The line is **at most 255 bytes**. The keys other than `answer` take at most 164 of them, so
  `answer` is the only field ever trimmed and every body field survives.
* It is **ASCII only** (`\uXXXX` for anything else), so an Arabic answer cannot crash the write.
* Each phase also sends the matching `FACE <face>` command, for a body that does not read JSON yet.
* **Nothing waits on it, and the body does not answer it.** A body that answers `ERR {"…` is
  ignored. The body must still treat it as a line for the watchdog.
* With `CORE_FILLER=0` there is no acknowledgement and so no such line. Under
  `CORE_FACE_MODE=model` only `answering` is sent.
* `grep 'robot-serial: reply' .cache/tmp/core.log` answers "did the body get it".

**Firmware must be built with `PROTOCOL_MAX_LINE 256`.** An old build (96) cuts the line and
parses its tail as a command of its own. That is exactly how a stale simulator binary once failed.
