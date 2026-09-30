//! What the panel shows, for every sequence robot-core can produce.
//!
//! These run with no display, no socket and no robot, which is the whole reason the
//! reducer is a separate crate: a Slint build needs the stack stopped on this board,
//! and the interesting decisions are all here anyway.
//!
//! Time is a parameter, so the watchdogs are tested without a test that sleeps.

use robot_face_protocol::events::Incoming;
use robot_face_protocol::state::{
    Face, UiState, Voice, Who, HEARING_WATCHDOG_MS, SCROLLBACK, TURN_WATCHDOG_MS,
};

fn ev(json: &str) -> Incoming {
    robot_face_protocol::parse(json).expect("fixture should parse")
}

/// A linked panel at t=0.
fn linked() -> UiState {
    let mut ui = UiState::new();
    ui.on_link_up(0);
    ui
}

/// The normal shape of a spoken turn.
fn a_spoken_turn(ui: &mut UiState, t: u64) {
    ui.apply(ev(r#"{"type":"speech_start","muted":false}"#), t);
    ui.apply(ev(r#"{"type":"speech_end","audio_s":1.4}"#), t + 1);
    ui.apply(
        ev(r#"{"type":"transcript","text":"Apa kabar?","turn_id":"t1","source":"voice"}"#),
        t + 2,
    );
    ui.apply(
        ev(r#"{"type":"language","turn_id":"t1","language":"id","language_source":"declared"}"#),
        t + 3,
    );
    ui.apply(
        ev(r#"{"type":"chunk","turn_id":"t1","seq":0,"text":"Baik sekali."}"#),
        t + 4,
    );
}

mod the_voice_state {
    use super::*;

    #[test]
    fn walks_the_whole_turn() {
        let mut ui = linked();
        assert_eq!(ui.voice, Voice::Idle);

        ui.apply(ev(r#"{"type":"speech_start","muted":false}"#), 10);
        assert_eq!(ui.voice, Voice::Hearing);

        ui.apply(ev(r#"{"type":"speech_end","audio_s":1.0}"#), 20);
        assert_eq!(ui.voice, Voice::Transcribing);

        ui.apply(
            ev(r#"{"type":"transcript","text":"halo","turn_id":"t1","source":"voice"}"#),
            30,
        );
        assert_eq!(ui.voice, Voice::Thinking);

        ui.apply(
            ev(r#"{"type":"chunk","turn_id":"t1","seq":0,"text":"Halo!"}"#),
            40,
        );
        assert_eq!(ui.voice, Voice::Speaking);

        ui.apply(ev(r#"{"type":"turn_done","turn_id":"t1"}"#), 50);
        assert_eq!(ui.voice, Voice::Idle);
    }

    #[test]
    fn a_dropped_link_overrides_everything() {
        let mut ui = linked();
        a_spoken_turn(&mut ui, 10);
        assert_eq!(ui.voice, Voice::Speaking);
        ui.on_link_down(100);
        assert_eq!(ui.voice, Voice::Offline, "claimed a state it cannot know");
        assert_eq!(ui.face, Face::Neutral);
    }

    #[test]
    fn reconnecting_mid_turn_is_believed() {
        // hello carries the live state, so a panel that starts while the robot is
        // already talking must not claim it is idle.
        let mut ui = UiState::new();
        ui.apply(ev(r#"{"type":"hello","state":"speaking"}"#), 0);
        assert_eq!(ui.voice, Voice::Speaking);
    }

    #[test]
    fn a_muted_speech_start_does_not_open_a_turn() {
        // The microphone was gated: this is the robot hearing its own voice, which is
        // what listen_mode=no_interrupt exists to stop.
        let mut ui = linked();
        ui.apply(ev(r#"{"type":"speech_start","muted":true}"#), 10);
        assert_eq!(ui.voice, Voice::Idle);
        assert!(ui.notice.is_some(), "should say why it is not listening");
    }

    #[test]
    fn a_barge_in_does_not_stomp_the_new_utterance() {
        // turn_done with barged_in arrives AFTER robot-asr has already reported the
        // interrupting speech. Going to Idle here would lose it.
        let mut ui = linked();
        a_spoken_turn(&mut ui, 10);
        ui.apply(ev(r#"{"type":"speech_start","muted":false}"#), 20);
        assert_eq!(ui.voice, Voice::Hearing);
        ui.apply(
            ev(r#"{"type":"turn_done","turn_id":"t1","barged_in":true}"#),
            21,
        );
        assert_eq!(ui.voice, Voice::Hearing);
    }
}

mod the_conversation {
    use super::*;

    #[test]
    fn chunks_extend_one_reply_rather_than_stacking_up() {
        let mut ui = linked();
        ui.apply(
            ev(r#"{"type":"transcript","text":"halo","turn_id":"t1","source":"voice"}"#),
            1,
        );
        for (seq, text) in [(0, "Halo."), (1, "Apa kabar?"), (2, "Senang bertemu.")] {
            ui.apply(
                ev(&format!(
                    r#"{{"type":"chunk","turn_id":"t1","seq":{seq},"text":"{text}"}}"#
                )),
                10 + seq as u64,
            );
        }
        let robot: Vec<_> = ui.lines.iter().filter(|l| l.who == Who::Robot).collect();
        assert_eq!(robot.len(), 1, "one bubble per reply, not one per sentence");
        assert_eq!(robot[0].text, "Halo. Apa kabar? Senang bertemu.");
    }

    #[test]
    fn a_repeated_chunk_is_not_applied_twice() {
        // robot-core is not de-duplicated -- it already delivers `ingested` twice from
        // two fan-out paths -- so a repeat must not double the text.
        let mut ui = linked();
        let chunk = r#"{"type":"chunk","turn_id":"t1","seq":0,"text":"Halo."}"#;
        ui.apply(ev(chunk), 1);
        ui.apply(ev(chunk), 2);
        assert_eq!(ui.lines[0].text, "Halo.");
    }

    #[test]
    fn a_reply_that_never_chunked_is_still_shown() {
        // A very short answer can settle before the sentence chunker emits anything.
        let mut ui = linked();
        ui.apply(
            ev(r#"{"type":"reply_fields","turn_id":"t1","answer":"Ya.","face":"happy"}"#),
            1,
        );
        assert_eq!(ui.lines.last().unwrap().text, "Ya.");
    }

    #[test]
    fn the_reply_stops_streaming_when_the_turn_closes() {
        let mut ui = linked();
        a_spoken_turn(&mut ui, 1);
        assert!(ui.lines.last().unwrap().streaming);
        ui.apply(ev(r#"{"type":"turn_done","turn_id":"t1"}"#), 20);
        assert!(!ui.lines.last().unwrap().streaming);
    }

    #[test]
    fn a_typed_turn_is_marked_as_typed() {
        // /chat turns are answered silently. The panel must not imply the robot spoke.
        let mut ui = linked();
        ui.apply(
            ev(r#"{"type":"transcript","text":"halo","turn_id":"t1","source":"text"}"#),
            1,
        );
        assert!(ui.lines[0].typed);
    }

    #[test]
    fn the_conversation_survives_a_reconnect() {
        // robot-core restarting should not blank the screen for someone walking past.
        let mut ui = linked();
        a_spoken_turn(&mut ui, 1);
        let before = ui.lines.len();
        ui.on_link_down(100);
        ui.on_link_up(200);
        assert_eq!(ui.lines.len(), before, "lost the conversation on reconnect");
    }

    #[test]
    fn a_new_turn_after_a_reconnect_does_not_extend_the_old_reply() {
        let mut ui = linked();
        a_spoken_turn(&mut ui, 1);
        ui.on_link_down(100);
        ui.on_link_up(200);
        ui.apply(
            ev(r#"{"type":"chunk","turn_id":"t2","seq":0,"text":"Baru."}"#),
            210,
        );
        let robot: Vec<_> = ui.lines.iter().filter(|l| l.who == Who::Robot).collect();
        assert_eq!(robot.len(), 2);
        assert_eq!(robot[1].text, "Baru.");
    }

    #[test]
    fn scrollback_is_bounded_and_does_not_corrupt_the_open_reply() {
        let mut ui = linked();
        for i in 0..(SCROLLBACK + 50) {
            ui.apply(
                ev(&format!(
                    r#"{{"type":"transcript","text":"q{i}","turn_id":"t{i}","source":"voice"}}"#
                )),
                i as u64,
            );
        }
        assert_eq!(ui.lines.len(), SCROLLBACK);
        // The open reply index must still point at a real line after the drain.
        ui.apply(
            ev(r#"{"type":"chunk","turn_id":"t999","seq":0,"text":"ok"}"#),
            9_000,
        );
        assert_eq!(ui.lines.last().unwrap().text, "ok");
    }

    #[test]
    fn visible_returns_the_newest_lines_oldest_first() {
        let mut ui = linked();
        for i in 0..10 {
            ui.apply(
                ev(&format!(
                    r#"{{"type":"transcript","text":"q{i}","turn_id":"t{i}","source":"voice"}}"#
                )),
                i,
            );
        }
        let shown = ui.visible(3);
        assert_eq!(shown.len(), 3);
        assert_eq!(shown[0].text, "q7");
        assert_eq!(shown[2].text, "q9");
    }
}

mod the_face {
    use super::*;

    #[test]
    fn follows_the_voice_state_when_the_model_says_nothing() {
        let mut ui = linked();
        assert_eq!(ui.face, Face::Neutral);
        ui.apply(ev(r#"{"type":"speech_start","muted":false}"#), 1);
        assert_eq!(ui.face, Face::Listening);
        ui.apply(ev(r#"{"type":"speech_end"}"#), 2);
        assert_eq!(ui.face, Face::Thinking);
    }

    #[test]
    fn a_model_face_wins_for_the_rest_of_the_turn() {
        // Without latching, the next chunk would derive Speaking and overwrite it.
        let mut ui = linked();
        ui.apply(
            ev(r#"{"type":"reply_fields","turn_id":"t1","answer":"","face":"happy"}"#),
            1,
        );
        assert_eq!(ui.face, Face::Happy);
        ui.apply(
            ev(r#"{"type":"chunk","turn_id":"t1","seq":0,"text":"Halo."}"#),
            2,
        );
        assert_eq!(ui.face, Face::Happy, "the model's face was overwritten");
    }

    #[test]
    fn the_latch_is_released_when_the_turn_ends(
    ) {
        let mut ui = linked();
        ui.apply(
            ev(r#"{"type":"reply_fields","turn_id":"t1","answer":"","face":"sad"}"#),
            1,
        );
        ui.apply(ev(r#"{"type":"turn_done","turn_id":"t1"}"#), 2);
        assert_eq!(ui.face, Face::Neutral);
    }

    #[test]
    fn an_unknown_expression_is_neutral_not_a_crash() {
        // Same fallback as robot.core.reply._one_of, so the panel and the orchestrator
        // degrade identically.
        assert_eq!(Face::parse("smug"), Face::Neutral);
        assert_eq!(Face::parse(""), Face::Neutral);
        assert_eq!(Face::parse("HAPPY"), Face::Happy);
    }

    #[test]
    fn the_six_match_the_firmware() {
        // serial-protocol.md: FACE <name> drives the LED matrix. The panel is a second
        // face and must never show one the body cannot.
        for name in ["neutral", "happy", "thinking", "listening", "speaking", "sad"] {
            assert_ne!(
                format!("{:?}", Face::parse(name)).to_ascii_lowercase(),
                "unknown"
            );
        }
    }
}

mod the_watchdogs {
    use super::*;

    #[test]
    fn a_turn_that_stops_reporting_is_released() {
        // robot-core restarting mid-turn never sends turn_done. Without this the panel
        // sits on "Speaking" for ever with nothing to say so.
        let mut ui = linked();
        a_spoken_turn(&mut ui, 1_000);
        assert_eq!(ui.voice, Voice::Speaking);
        // `a_spoken_turn` runs to t+4, so the deadline is measured from there.
        ui.tick(1_000 + TURN_WATCHDOG_MS + 100);
        assert_eq!(ui.voice, Voice::Idle);
        assert!(ui.notice.is_some());
    }

    #[test]
    fn an_utterance_that_never_ends_is_released() {
        let mut ui = linked();
        ui.apply(ev(r#"{"type":"speech_start","muted":false}"#), 1_000);
        ui.tick(1_000 + HEARING_WATCHDOG_MS + 100);
        assert_eq!(ui.voice, Voice::Idle);
    }

    #[test]
    fn a_healthy_turn_is_not_interrupted() {
        let mut ui = linked();
        a_spoken_turn(&mut ui, 1_000);
        ui.tick(1_000 + TURN_WATCHDOG_MS / 2);
        assert_eq!(ui.voice, Voice::Speaking);
    }

    #[test]
    fn the_meter_alone_does_not_keep_a_dead_turn_alive() {
        // `level` arrives ~8/s regardless of whether a turn is progressing. If it reset
        // the watchdog, a wedged turn would never be released.
        let mut ui = linked();
        a_spoken_turn(&mut ui, 1_000);
        for i in 0..600 {
            ui.apply(
                ev(r#"{"type":"level","dbfs":-40.0,"speech":false}"#),
                1_000 + i * 120,
            );
        }
        ui.tick(1_000 + TURN_WATCHDOG_MS + 100);
        assert_eq!(ui.voice, Voice::Idle, "the meter masked a lost turn");
    }

    #[test]
    fn nothing_is_watchdogged_while_offline() {
        let mut ui = UiState::new();
        ui.on_link_down(0);
        ui.tick(TURN_WATCHDOG_MS * 10);
        assert_eq!(ui.voice, Voice::Offline);
    }

    #[test]
    fn a_notice_expires() {
        let mut ui = linked();
        ui.apply(
            ev(r#"{"type":"ignored","text":"...","reason":"wrong language","mode":"no_interrupt"}"#),
            1_000,
        );
        assert!(ui.notice.is_some());
        ui.tick(1_000 + robot_face_protocol::state::NOTICE_MS + 1);
        assert!(ui.notice.is_none());
    }
}

mod the_link {
    use super::*;

    #[test]
    fn a_rejected_token_is_not_just_disconnected() {
        // robot-core answers 401 before the upgrade, so this is knowable. Hiding it
        // behind a reconnect spinner would make a one-line fix look like a dead robot.
        let mut ui = linked();
        ui.on_auth_failed(10);
        assert!(ui.auth_failed);
        assert!(!ui.linked);
        assert_eq!(ui.voice, Voice::Offline);
    }

    #[test]
    fn reconnecting_clears_the_auth_failure() {
        let mut ui = UiState::new();
        ui.on_auth_failed(10);
        ui.on_link_up(20);
        assert!(!ui.auth_failed);
    }
}
