# people — the robot knows who it is talking to (M13)

`robot-vision` knows a **face**. This is the other half: what was **said** to whoever that
face belongs to, so a conversation can stop when they walk away and start again, by name,
when they come back. It lives in `robot-core` because that is where the conversation is.
It adds **no process, no model and no dependency**: 0 MiB of resident memory beyond a
sqlite file.

    src/robot/core/people.py     PeopleStore (sqlite), parse_name, the spoken lines, memory_block
    src/robot/core/presence.py   frames -> events (current / arrived / left / stranger)
    src/robot/core/social.py     what the events DO: ask, learn, file, greet, recall, summarise
    src/robot/core/turn.py       Orchestrator hooks: person, scripted turns, cues, recording
    src/robot/ui/static/talk.*   the one page that shows all of it
    config/core.env              CORE_PEOPLE_*
    run/people.db                the conversations (gitignored; faces are in run/faces.db)

## The story

    1  a face nobody knows stands alone in front of the camera for ~4 s
       -> "Hello! I don't think we've met. What's your name? I'll remember you, unless you'd
          rather I didn't."                       (spoken in the default language, and shown)
    2  the NEXT thing said, by voice or typed, is read by rules (`parse_name`)
         "My name is Deny" / "Saya Budi" / "اسمي محمد"   -> a name
         "no thanks" / "tidak mau" / "لا شكرا"          -> a refusal: stop asking for 5 min
         anything else                                  -> an ordinary turn; the question stays open
    3  a name -> robot-vision learns the face (3 frames) -> the person is filed -> welcomed
       "Nice to meet you, Deny! I'll remember you from now on."   (in the language they used)
    4  from then on, every turn while that face is in view is filed under them, and the model
       is shown who it is talking to and what was said last time
    5  they leave (out of the picture for 2 minutes): the visit -- the conversation -- ends. The
       resident LLM writes a summary of THAT conversation, and updates a few lines of running notes
       about the person. Logs -> Conversations lists every conversation, per person
    6  they come back: greeted by name in their own language, with the last of the talk already
       in the conversation -- "resuming" is the model seeing its last turns with them as
       ordinary chat turns, plus the notes in the system prompt

## What is stored, and what is not

    people         name (unique, case-blind) · first/last seen · visits · last language · running notes
    turns          who said it (user/assistant) · text · the raw model reply · language · typed/spoken · visit
    conversations  one per (person, visit): its summary, who wrote it (model / auto), when

* **Only turns attributed to a recognised face are written down.** A typed question with nobody
  in front of the camera is never stored. Turns by someone the robot does not know are filed
  under nobody.
* 5000 turns per person, the oldest fall off; 1200 characters a turn. The thread on the Conversation
  page has no Clear: the record is the database, and Logs -> Conversations shows all of it.
* The **raw** model reply is kept beside the spoken text, because the history the model is shown
  must have the shape of what it wrote -- a plain sentence among JSON objects teaches it that
  plain sentences are fine (measured in M7; `turn.py`).
* The link to the face is the **name**, nothing else. `Forget` (Config → People) removes the face
  samples (robot-vision) and the person and everything said to them (`turns` cascade).
  "Forget the conversation only" keeps the face.
* Everything is on the robot. Nothing is sent anywhere. **This is personal data**: the same
  rules as `vision.md` § biometric, plus conversation text. Do not copy `run/people.db` off.

## The rules that keep it safe to leave running in a room

| Rule | Why |
|---|---|
| Ask when a stranger arrives, again if they are still there when the question lapses unanswered (60 s), never twice within 30 s; 5 min of peace after a "no" | a stranger who is not asked is a person the robot never learns; one asked every few seconds is a nuisance. Somebody who leaves and comes back is a new stranger and is asked again |
| Ask only when **one** face is in view, close (≥ 60 px), clear (score ≥ 0.75), steady (4 s) | two faces and it cannot know who it is asking; the same bar enrolment applies |
| A "no" is forgotten: nothing stored, and the history of the exchange is emptied | a person who will not be remembered is not remembered; strangers' talk is never written down anyway |
| An answer must be a name, by rules, or it is an ordinary turn | a wrong name is written under somebody's face; a missed one costs a second try |
| A name already known is **not** given to an unrecognised face | otherwise "I'm Deny" hands anyone Deny's face model and conversation |
| A name must be recognised in 3 scenes running (~1.5 s) | one misread frame must not greet someone or swap whose past is in the prompt |
| Leaving takes 2 min out of the picture | turning round or bending down is not leaving, and must not cause a second greeting |
| A stranger alone in view **detaches** the previous person at once | what was said to Deny is not for whoever sat down next; history and memory are cleared |
| The robot never interrupts: its own turns are dropped if anything is running | and a summary being written is cancelled the instant any turn starts (one LLM slot) |
| No wake word needed for the answer | otherwise a `wake_word` robot could not be told its visitor's name |

## The prompt

`prompt.build_system_prompt(..., memory=)`: persona … language rules … language line …
**memory** … **scene** (last). Memory changes when the person does, the scene every frame, so
the scene stays last and the cached prefix survives (see `vision.md`). Memory is:

    You are talking with Deny, whom you recognise by their face. You have met them 3 times;
    you last spoke 2 days ago. What you remember of your earlier talks: <notes>. The most
    recent exchange is in the conversation above, so carry on from where it stopped. Bring up
    the past only when it helps, never recite it, and if you are not sure what was said, do
    not invent it.

Stored text is collapsed to one line and square brackets are replaced before it goes in:
it is untrusted, and `[do:drive ...]` in a note must not be able to become a marker.

## Conversations and their summaries

A **conversation is a visit**: from the person arriving to them having been gone for `away_s`
(2 min). Turns already carry their visit number, so a conversation is not stored as a thing of its own
-- only its **summary** is (`conversations`), because that is written afterwards.

* **Every conversation gets one.** At the end of a visit the resident model is asked for three
  sentences about *that* talk. `Social._sweep` runs every 15 s and writes whatever is owed when the
  robot is idle, so a summary cancelled by somebody starting to talk, a restart mid-visit, or a model
  that was down all end up written. The conversation still going on is never summarised.
* **`kind` says who wrote it.** `model`, or `auto` -- the plain "N turns. It began with ... and ended
  with ..." written after `SUMMARY_TRIES` (2) unusable answers, or at once when there is no model
  (`CORE_PEOPLE_SUMMARISE=0`). The Logs page marks `plain summary`.
* **The running notes are separate and unchanged**: what the model is *shown* next time. Two writes
  at the end of a visit, in that order, one model slot, cancelled by any turn.
* **Logs -> Conversations** (`conversations`, `conversation_turns` on the websocket): per person,
  newest first, folded to the summary, expandable to the turns; polled every 5 s; filter by name or
  summary. Forgetting a person (or just their conversation) removes these too.

## Turns the robot starts itself

`Orchestrator.start_turn(kind=...)`:

* `"user"` -- a person speaking or typing (default). May carry a `Scripted` reply when robot-core
  can answer for itself (the name it just asked for).
* `"robot"` -- a scripted line with no model call (the question).
* `"cue"` -- a hidden instruction answered by the model (the greeting), with a `fallback` line if
  the model is down. Not retrieved for, not shown as something the person said, not stored.

Every event of a turn is stamped `source` (spoken or text reply), `via` (`mic`, `keyboard`,
`robot`), `kind` and `person`. The Conversation page marks each bubble from those.

## The Conversation page (`/talk`) and Config → People

Replaces `/chat`, `/headless` and `/vision` (they redirect). **One screen; the page does not
scroll.** Left: one thread, typed and spoken turns together, each marked, with a chip saying who
the robot is talking with. Right: the camera with the boxes drawn in the browser, and under it the
**activity log** -- the pipeline turn by turn, *and each reply's JSON*, which is the model's output
and not conversation, so it lives here and not in the thread. **Read answers aloud** (off by
default, remembered in the browser) decides whether a *typed* turn is answered out loud; a spoken
turn always is.

**The people the robot knows are on Config → People**, not here: the list, each person's stored
conversation and notes, *Forget* (face and conversation), *Forget the conversation only*, and
*Introduce someone* (the person faces the camera -- picture on the Conversation page). *Ask
strangers their name* and *Greet people who come back* are the two settings on the same tab.

A caution that is easy to miss: **the person at the keyboard is not necessarily the person in
front of the camera.** A message typed from another room is filed under whoever the robot
sees, and the chip at the top of the thread says who that is. If that matters, turn the eye
off (Faces toggle) before typing.

## Dashboard messages

    -> say {text, speak?}            typed turn; speak=true answers aloud
    -> talk_state                    <- {presence, conversation, state}   (on open / reconnect)
    <- presence {person, in_view, faces, unknown, awaiting_name, ask, greet}   on change
    <- initiated {turn_id, reason}   the robot spoke first ("ask" | "greet")
    -> vision_enroll / vision_forget (forget now removes the conversation too)
    -> people_history {name}         <- {turns, summary, visits}
    -> people_forget_conversation {name}
    <- vision_people {people:[{name, samples, visits, turns, last_seen, summary, lang}]}
    -> conversations {name?}         <- {conversations:[{name, visit, started, ended, turns, summary, kind, live}]}
    -> conversation_turns {name, visit}   <- {turns}

## Measured and verified (2026-10-29)

* 128 tests (`tests/test_people.py`): the name parser on 40+ real phrasings in three languages;
  the store; every presence rule; the whole story through the **real** orchestrator with a
  scripted LLM and a fake camera (ask, answer, refuse, retry, lookalike, taken name, greet,
  recall, isolation between people, leaving, summary, forgetting).
* Against the **real LLM** (Qwen3-0.6B on :8080) with a fake speaker and camera: asked, welcomed,
  two turns filed, notes written, a stranger shown **nothing** of Deny's, Deny greeted an hour
  later with 6 earlier turns recalled, and the talk resumed on the robot arm.
* **Not verified: a live person in front of the camera.** The scenes were scripted in the shape
  robot-vision sends; recognition itself is the M12 result (photographs, 0.756 vs ≤ 0.35). Do it
  once: stand alone in front of the camera and wait ~5 s; answer when it asks.
* **Not verified: by voice, end to end.** The USB sound card is absent on this board, so the
  microphone half (answering the question aloud) was exercised through the typed path, which
  takes the same `before_turn` route. `expecting()` lets the answer through the wake-word gate;
  that too is a unit test, not a room.

## Limits, honestly

* **Qwen3-0.6B uses all of this poorly** (Known issue #1). It greets in two languages in one
  sentence and invents what the previous talk was about. The plumbing is right; a 3B model is
  what makes the *words* right. Judge the feature on Qwen2.5-3B.
* The notes are written by that same small model and are only as good as it is. They are bounded
  (600 characters), best effort, cancelled by any turn, and the turns themselves are always kept
  verbatim -- a bad note costs a little prompt, never a lost conversation.
* **Qwen3 thinks** unless told `/no_think`; the notes request says it. Other models see one odd
  token. Found against the real model: every note came back empty (TROUBLESHOOTING).
* There is **no anti-spoofing** (see `vision.md`). A photograph of Deny is Deny, and gets Deny's
  past recalled to it. Never put anything behind this that matters.
* A name is whatever the speech recogniser heard. Whisper will spell an unusual name its own
  way; it is the dashboard's job to introduce someone by hand when that matters.
* Two known people in frame: the **larger face** is who the robot talks to.
* Multiple visitors who share a name share a record. Names are the key.
