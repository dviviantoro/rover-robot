//! What robot-core sends, and what the panel sends back.
//!
//! Three rules here, each of which is a bug this crate is not going to have:
//!
//! **`#[serde(other)]` catches everything we do not care about.** robot-core's dashboard
//! socket carries the operator half of the system too -- `health`, `log`, `budget`,
//! `models`, `serial_*`, the fat payload attached to `hello`. The panel shows a
//! conversation and must ignore all of it without erroring.
//!
//! **`#[serde(default)]` on every field the panel does not strictly need**, and never
//! `deny_unknown_fields`. robot-core grows fields (`source` and `language_source` were
//! both added recently); a panel that refuses to parse a message because it learned a new
//! key is a panel that goes blank on the next deploy.
//!
//! **Nothing here is fallible in a way that reaches the screen.** An unparseable line is
//! dropped and logged by the caller, never propagated -- the same reasoning as
//! `robot.core.reply.parse`, which speaks a malformed reply rather than taking the turn
//! down.

use serde::{Deserialize, Serialize};

/// One message from robot-core.
///
/// The `type` field is the tag. Variants cover only the conversational surface; the
/// operator messages fall into `Other`.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type")]
pub enum Incoming {
    /// Pushed unprompted the moment the socket opens. Carries the current turn state, so
    /// a panel that connects mid-sentence does not claim the robot is idle.
    #[serde(rename = "hello")]
    Hello {
        #[serde(default)]
        state: String,
        #[serde(default)]
        languages: Vec<String>,
    },

    /// What the person said (or typed). `source` is "voice" or "text".
    #[serde(rename = "transcript")]
    Transcript {
        #[serde(default)]
        text: String,
        #[serde(default)]
        turn_id: String,
        #[serde(default = "voice")]
        source: String,
    },

    /// Which language the reply will be in, and how that was decided.
    #[serde(rename = "language")]
    Language {
        #[serde(default)]
        turn_id: String,
        #[serde(default)]
        language: String,
        #[serde(default)]
        language_source: String,
    },

    /// One sentence of the answer. These stream: `seq` counts from 0 within a turn and
    /// each one must EXTEND the open reply, not start a new one.
    #[serde(rename = "chunk")]
    Chunk {
        #[serde(default)]
        turn_id: String,
        #[serde(default)]
        seq: u32,
        #[serde(default)]
        text: String,
    },

    /// The whole structured reply, once the model has closed the object. Only `answer` is
    /// ever spoken; `face` is what the body does while it speaks, and is what this panel
    /// draws.
    #[serde(rename = "reply_fields")]
    ReplyFields {
        #[serde(default)]
        turn_id: String,
        #[serde(default)]
        answer: String,
        #[serde(default)]
        language: String,
        #[serde(default)]
        face: String,
        #[serde(default)]
        style: String,
    },

    #[serde(rename = "turn_done")]
    TurnDone {
        #[serde(default)]
        turn_id: String,
        #[serde(default)]
        barged_in: bool,
        #[serde(default)]
        timed_out: bool,
    },

    #[serde(rename = "turn_timeout")]
    TurnTimeout {
        #[serde(default)]
        turn_id: String,
        #[serde(default)]
        after_s: f32,
    },

    /// Someone started talking. `muted` means the microphone was gated shut at the time --
    /// the robot was speaking and this is its own voice coming back.
    #[serde(rename = "speech_start")]
    SpeechStart {
        #[serde(default)]
        muted: bool,
    },

    #[serde(rename = "speech_end")]
    SpeechEnd {
        #[serde(default)]
        audio_s: f32,
    },

    /// ~8 per second while audio flows. Drives the meter and nothing else.
    #[serde(rename = "level")]
    Level {
        #[serde(default = "silence")]
        dbfs: f32,
        #[serde(default = "silence")]
        peak_dbfs: f32,
        #[serde(default = "floor")]
        floor_dbfs: f32,
        #[serde(default)]
        speech: bool,
        #[serde(default = "yes")]
        gate_open: bool,
        /// Digital silence for long enough to mean the wrong capture device.
        #[serde(default)]
        silent: bool,
    },

    /// Heard, transcribed, and deliberately not answered -- wrong language, the robot's
    /// own voice, or no wake word. Worth showing: otherwise the robot looks broken.
    #[serde(rename = "ignored")]
    Ignored {
        #[serde(default)]
        text: String,
        #[serde(default)]
        reason: String,
        #[serde(default)]
        mode: String,
    },

    /// A refusal or failure from the body.
    #[serde(rename = "tool_result")]
    ToolResult {
        #[serde(default)]
        turn_id: String,
        #[serde(default)]
        tool: String,
        #[serde(default)]
        note: String,
    },

    /// Everything else robot-core says. Not an error.
    #[serde(other)]
    Other,
}

fn voice() -> String {
    "voice".to_string()
}

fn silence() -> f32 {
    -90.0
}

fn floor() -> f32 {
    -60.0
}

fn yes() -> bool {
    true
}

/// What the panel can ask robot-core to do.
///
/// Deliberately tiny. This is a face, not a control surface: the browser dashboard keeps
/// the settings, the memory budget and the service supervision.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(tag = "type")]
pub enum Outgoing {
    /// Stop the reply that is playing. The same message the dashboard's Stop sends.
    #[serde(rename = "cancel")]
    Cancel,

    /// Typed input, if a keyboard or touch panel is ever attached.
    ///
    /// `source: "text"` is deliberate and means SILENT -- robot-core does not send a typed
    /// turn to the speaker. A panel on the robot answering out loud because someone poked
    /// the screen would be a surprise, and it would make the robot hear itself.
    #[serde(rename = "say")]
    Say { text: String, source: String },
}

impl Outgoing {
    pub fn say(text: impl Into<String>) -> Self {
        Outgoing::Say {
            text: text.into(),
            source: "text".to_string(),
        }
    }
}

/// Parse one line from the socket. `None` means "not for us, or not readable" -- never an
/// error the caller has to handle, because there is nothing useful it could do with one.
pub fn parse(line: &str) -> Option<Incoming> {
    serde_json::from_str::<Incoming>(line).ok()
}
