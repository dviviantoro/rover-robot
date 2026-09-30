//! Events in, screen contents out. No Slint types, no sockets, no real clock.
//!
//! Everything the panel decides lives here, which is what makes it testable with
//! `cargo test` on any machine with no display and no robot. The UI layer is then a dumb
//! projection of [`UiState`] onto properties.
//!
//! Time is passed in rather than read, so the watchdogs below can be tested without a
//! test that sleeps for a minute.

use std::collections::HashSet;

use crate::events::Incoming;

/// What the robot is doing, as far as the panel can tell.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Voice {
    /// No link to robot-core. A hard override: whatever the last turn was doing, if the
    /// socket is down we do not know and must not pretend.
    Offline,
    Idle,
    Hearing,
    Transcribing,
    Thinking,
    Speaking,
}

/// The six expressions the firmware's LED matrix also knows (serial-protocol.md `FACE`).
/// Keeping the same six means the panel and the LED face can never disagree.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Face {
    Neutral,
    Happy,
    Thinking,
    Listening,
    Speaking,
    Sad,
}

impl Face {
    /// Parse the model's `face` field, falling back to `Neutral`.
    ///
    /// Mirrors `_one_of(..., FACES, "neutral")` in `robot/core/reply.py` on purpose: the
    /// panel should degrade exactly the way the orchestrator does, so a value the grammar
    /// somehow let through produces the same safe expression in both places.
    pub fn parse(value: &str) -> Face {
        match value.trim().to_ascii_lowercase().as_str() {
            "happy" => Face::Happy,
            "thinking" => Face::Thinking,
            "listening" => Face::Listening,
            "speaking" => Face::Speaking,
            "sad" => Face::Sad,
            _ => Face::Neutral,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Who {
    You,
    Robot,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Line {
    pub who: Who,
    pub text: String,
    pub language: String,
    /// Still being written. The UI draws a caret on this one.
    pub streaming: bool,
    /// True when the turn was typed rather than spoken. Shown quietly: a question typed on
    /// the dashboard is still answered, and the panel should not imply the robot spoke.
    pub typed: bool,
}

/// A transient one-liner: an ignored utterance, a timeout, a refusal from the body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Notice {
    pub text: String,
    /// When it should stop being shown.
    pub until_ms: u64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Level {
    pub dbfs: f32,
    pub peak_dbfs: f32,
    pub floor_dbfs: f32,
    pub speech: bool,
    pub gate_open: bool,
    pub silent: bool,
}

impl Default for Level {
    fn default() -> Self {
        Level {
            dbfs: -90.0,
            peak_dbfs: -90.0,
            floor_dbfs: -60.0,
            speech: false,
            gate_open: true,
            silent: false,
        }
    }
}

/// How long the link may be down before the panel stops claiming a state it cannot know.
pub const NOTICE_MS: u64 = 4_000;

/// A turn that produces nothing at all for this long is assumed lost. robot-core has its
/// own 90 s turn deadline, but a robot-core that RESTARTS mid-turn never sends
/// `turn_timeout` -- the panel would sit on "Speaking" for ever with nothing to say so.
pub const TURN_WATCHDOG_MS: u64 = 60_000;

/// Likewise for an utterance that never ends.
pub const HEARING_WATCHDOG_MS: u64 = 30_000;

/// How many lines the panel keeps. Only the last few are ever drawn; the rest exist so a
/// person can see the previous exchange after a reconnect.
pub const SCROLLBACK: usize = 200;

#[derive(Debug, Clone)]
pub struct UiState {
    pub voice: Voice,
    pub face: Face,
    pub lines: Vec<Line>,
    pub level: Level,
    pub language: String,
    pub linked: bool,
    /// The token was rejected. Distinct from "not connected", because the fix is
    /// completely different and a spinner would hide it.
    pub auth_failed: bool,
    pub notice: Option<Notice>,
    /// Bumped whenever `lines` changes. The UI rebuilds its model only when this moves,
    /// so a 10 Hz drain does not relayout the conversation ten times a second.
    pub lines_rev: u64,

    // ---- per-turn bookkeeping, cleared when a turn closes or the link drops ----
    turn: Option<String>,
    /// A face from `reply_fields` wins until the turn ends. Without latching, the derived
    /// face would immediately overwrite the model's choice on the next `chunk`.
    latched_face: Option<Face>,
    /// `(turn_id, seq)` already applied. robot-core is not de-duplicated -- it already
    /// delivers `ingested` twice from two fan-out paths -- so the panel must not assume
    /// a chunk arrives exactly once.
    seen_chunks: HashSet<(String, u32)>,
    /// Index into `lines` of the reply being streamed into.
    open_reply: Option<usize>,
    last_event_ms: u64,
}

impl Default for UiState {
    fn default() -> Self {
        UiState {
            voice: Voice::Offline,
            face: Face::Neutral,
            lines: Vec::new(),
            level: Level::default(),
            language: String::new(),
            linked: false,
            auth_failed: false,
            notice: None,
            lines_rev: 0,
            turn: None,
            latched_face: None,
            seen_chunks: HashSet::new(),
            open_reply: None,
            last_event_ms: 0,
        }
    }
}

impl UiState {
    pub fn new() -> Self {
        Self::default()
    }

    /// The link came up. Clears per-turn state but KEEPS the conversation: someone walking
    /// past should see the last exchange, not a blank screen, when robot-core restarts.
    pub fn on_link_up(&mut self, now_ms: u64) {
        self.linked = true;
        self.auth_failed = false;
        self.voice = Voice::Idle;
        self.close_turn();
        self.last_event_ms = now_ms;
    }

    pub fn on_link_down(&mut self, now_ms: u64) {
        self.linked = false;
        self.voice = Voice::Offline;
        self.face = Face::Neutral;
        self.close_turn();
        self.last_event_ms = now_ms;
    }

    /// The token was refused. robot-core answers 401 before the WebSocket upgrade, so this
    /// is knowable and must be said plainly rather than retried behind a spinner.
    pub fn on_auth_failed(&mut self, now_ms: u64) {
        self.on_link_down(now_ms);
        self.auth_failed = true;
    }

    pub fn apply(&mut self, event: Incoming, now_ms: u64) {
        // `level` is not a turn event and must never touch the conversation or the
        // watchdog -- a quiet room would otherwise look like a lost turn.
        if let Incoming::Level {
            dbfs,
            peak_dbfs,
            floor_dbfs,
            speech,
            gate_open,
            silent,
        } = event
        {
            self.level = Level {
                dbfs,
                peak_dbfs,
                floor_dbfs,
                speech,
                gate_open,
                silent,
            };
            return;
        }

        self.last_event_ms = now_ms;

        match event {
            Incoming::Hello { state, .. } => {
                self.linked = true;
                self.auth_failed = false;
                self.voice = match state.as_str() {
                    "thinking" => Voice::Thinking,
                    "speaking" => Voice::Speaking,
                    _ => Voice::Idle,
                };
                self.refresh_face();
            }

            Incoming::SpeechStart { muted } => {
                if muted {
                    // The microphone is gated shut: this is the robot hearing itself while
                    // it talks, which is exactly what `listen_mode=no_interrupt` is for.
                    // Say so, but do not pretend a turn is starting.
                    self.note("not listening — the robot is speaking", now_ms);
                } else {
                    self.voice = Voice::Hearing;
                    self.refresh_face();
                }
            }

            Incoming::SpeechEnd { .. } => {
                if self.voice == Voice::Hearing {
                    self.voice = Voice::Transcribing;
                    self.refresh_face();
                }
            }

            Incoming::Transcript {
                text,
                turn_id,
                source,
            } => {
                self.open_turn(turn_id);
                let typed = source == "text";
                self.push(Line {
                    who: Who::You,
                    text,
                    language: String::new(),
                    streaming: false,
                    typed,
                });
                self.voice = Voice::Thinking;
                self.refresh_face();
            }

            Incoming::Language {
                language, turn_id, ..
            } => {
                if !language.is_empty() {
                    self.language = language.clone();
                    if self.turn.as_deref() == Some(turn_id.as_str()) {
                        if let Some(i) = self.open_reply {
                            self.lines[i].language = language;
                            self.lines_rev += 1;
                        }
                    }
                }
            }

            Incoming::Chunk { turn_id, seq, text } => {
                if self.turn.as_deref() != Some(turn_id.as_str()) {
                    self.open_turn(turn_id.clone());
                }
                if !self.seen_chunks.insert((turn_id, seq)) {
                    return; // already applied; the server does not de-duplicate
                }
                self.voice = Voice::Speaking;
                match self.open_reply {
                    Some(i) => {
                        if !self.lines[i].text.is_empty() {
                            self.lines[i].text.push(' ');
                        }
                        self.lines[i].text.push_str(&text);
                        self.lines_rev += 1;
                    }
                    None => {
                        let language = self.language.clone();
                        self.push(Line {
                            who: Who::Robot,
                            text,
                            language,
                            streaming: true,
                            typed: false,
                        });
                        self.open_reply = Some(self.lines.len() - 1);
                    }
                }
                self.refresh_face();
            }

            Incoming::ReplyFields {
                turn_id,
                answer,
                face,
                language,
                ..
            } => {
                if self.turn.as_deref() != Some(turn_id.as_str()) {
                    self.open_turn(turn_id);
                }
                self.latched_face = Some(Face::parse(&face));
                if !language.is_empty() {
                    self.language = language;
                }
                // A reply that produced no chunks at all still has an answer worth
                // showing -- a very short one can settle before the chunker emits.
                if self.open_reply.is_none() && !answer.is_empty() {
                    let language = self.language.clone();
                    self.push(Line {
                        who: Who::Robot,
                        text: answer,
                        language,
                        streaming: false,
                        typed: false,
                    });
                    self.open_reply = Some(self.lines.len() - 1);
                }
                self.refresh_face();
            }

            Incoming::TurnDone { barged_in, .. } => {
                self.close_turn();
                // A barge-in means someone is already talking over the robot; robot-asr
                // has sent `speech_start` and the state is Hearing. Do not stomp it.
                if !barged_in {
                    self.voice = Voice::Idle;
                }
                self.refresh_face();
            }

            Incoming::TurnTimeout { after_s, .. } => {
                self.close_turn();
                self.voice = Voice::Idle;
                self.note(&format!("gave up after {after_s:.0}s"), now_ms);
                self.refresh_face();
            }

            Incoming::Ignored { reason, .. } => {
                self.note(&format!("ignored — {reason}"), now_ms);
            }

            Incoming::ToolResult { tool, note, .. } => {
                if !note.is_empty() {
                    self.note(&format!("{tool}: {note}"), now_ms);
                }
            }

            Incoming::Level { .. } | Incoming::Other => {}
        }
    }

    /// Called on a timer. Expires the notice and rescues a turn that stopped reporting.
    ///
    /// Without this the panel can sit on "Speaking" for ever: robot-core restarting
    /// mid-turn never sends `turn_done`, and the socket reconnects perfectly happily.
    pub fn tick(&mut self, now_ms: u64) {
        if let Some(notice) = &self.notice {
            if now_ms >= notice.until_ms {
                self.notice = None;
            }
        }
        if !self.linked {
            return;
        }
        let quiet = now_ms.saturating_sub(self.last_event_ms);
        let stuck = match self.voice {
            Voice::Thinking | Voice::Speaking => quiet >= TURN_WATCHDOG_MS,
            Voice::Hearing | Voice::Transcribing => quiet >= HEARING_WATCHDOG_MS,
            _ => false,
        };
        if stuck {
            self.close_turn();
            self.voice = Voice::Idle;
            self.refresh_face();
            self.note("lost the turn", now_ms);
            self.last_event_ms = now_ms;
        }
    }

    /// The last `n` lines, oldest first -- what the panel actually draws.
    pub fn visible(&self, n: usize) -> &[Line] {
        let start = self.lines.len().saturating_sub(n);
        &self.lines[start..]
    }

    // ---- internals -------------------------------------------------------

    fn open_turn(&mut self, turn_id: String) {
        if self.turn.as_deref() == Some(turn_id.as_str()) {
            return;
        }
        self.close_turn();
        self.turn = Some(turn_id);
    }

    fn close_turn(&mut self) {
        if let Some(i) = self.open_reply {
            self.lines[i].streaming = false;
            self.lines_rev += 1;
        }
        self.turn = None;
        self.latched_face = None;
        self.open_reply = None;
        self.seen_chunks.clear();
    }

    /// The face: a latched one from the model wins, otherwise derive it from what the
    /// robot is doing. One rule, so the expression cannot oscillate between the two.
    fn refresh_face(&mut self) {
        self.face = self.latched_face.unwrap_or(match self.voice {
            Voice::Offline | Voice::Idle => Face::Neutral,
            Voice::Hearing => Face::Listening,
            Voice::Transcribing | Voice::Thinking => Face::Thinking,
            Voice::Speaking => Face::Speaking,
        });
    }

    fn note(&mut self, text: &str, now_ms: u64) {
        self.notice = Some(Notice {
            text: text.to_string(),
            until_ms: now_ms + NOTICE_MS,
        });
    }

    fn push(&mut self, line: Line) {
        self.lines.push(line);
        self.lines_rev += 1;
        if self.lines.len() > SCROLLBACK {
            let drop = self.lines.len() - SCROLLBACK;
            self.lines.drain(0..drop);
            if let Some(i) = self.open_reply {
                self.open_reply = i.checked_sub(drop);
            }
        }
    }
}
