//! `--demo`: a scripted conversation, so the panel can be built and judged with no robot.
//!
//! This is not a toy. It is how the layout gets designed before the real panel arrives,
//! and it is the only way to exercise the awkward states on demand -- a barge-in, a
//! timeout, an ignored utterance, a dropped link -- which are exactly the ones that are
//! hard to provoke by talking to a robot.

use std::sync::Arc;
use std::thread;
use std::time::Duration;

use crate::ws::{Inbox, Wire};

/// (delay before this line, JSON as robot-core would send it)
const SCRIPT: &[(u64, &str)] = &[
    (300, r#"{"type":"hello","state":"idle","languages":["en","id","ar"]}"#),
    (1200, r#"{"type":"level","dbfs":-52,"floor_dbfs":-60,"speech":false}"#),
    (600, r#"{"type":"speech_start","muted":false}"#),
    (300, r#"{"type":"level","dbfs":-22,"floor_dbfs":-60,"speech":true}"#),
    (400, r#"{"type":"level","dbfs":-16,"floor_dbfs":-60,"speech":true}"#),
    (500, r#"{"type":"speech_end","audio_s":1.6}"#),
    (200, r#"{"type":"transcript","text":"Apa saja Panca Jiwa Pondok Modern Tazakka?","turn_id":"d1","source":"voice"}"#),
    (900, r#"{"type":"language","turn_id":"d1","language":"id","language_source":"declared"}"#),
    (400, r#"{"type":"reply_fields","turn_id":"d1","answer":"","face":"happy","style":"cheerful","language":"id"}"#),
    (300, r#"{"type":"chunk","turn_id":"d1","seq":0,"text":"Panca Jiwa Tazakka ada lima."}"#),
    (1400, r#"{"type":"chunk","turn_id":"d1","seq":1,"text":"Keikhlasan, Kesederhanaan, Berdikari, Ukhuwah Islamiyah, dan Kebebasan."}"#),
    (1600, r#"{"type":"turn_done","turn_id":"d1","audio_s":6.2,"tokens":48}"#),
    // An utterance the robot deliberately did not answer.
    (1500, r#"{"type":"ignored","text":"...","reason":"the robot was speaking (no_interrupt)","mode":"no_interrupt"}"#),
    // A cross-lingual question, which is also the Arabic shaping test.
    (2000, r#"{"type":"transcript","text":"من هم قادة معهد تازاكا الحديث؟","turn_id":"d2","source":"voice"}"#),
    (1000, r#"{"type":"language","turn_id":"d2","language":"ar","language_source":"script"}"#),
    (500, r#"{"type":"chunk","turn_id":"d2","seq":0,"text":"Anang Rikza Masyhadi, Anizar Masyhadi, dan Muhammad Bisri."}"#),
    (1800, r#"{"type":"turn_done","turn_id":"d2"}"#),
    // A typed turn: answered, but silently. The panel must not imply the robot spoke.
    (1500, r#"{"type":"transcript","text":"How many hectares does Tazakka have?","turn_id":"d3","source":"text"}"#),
    (900, r#"{"type":"chunk","turn_id":"d3","seq":0,"text":"About 13 hectares, up from 1.6 in 2011."}"#),
    (1500, r#"{"type":"turn_done","turn_id":"d3","source":"text"}"#),
];

pub fn spawn(inbox: Inbox) {
    thread::Builder::new()
        .name("demo".into())
        .spawn(move || {
            if let Ok(mut queue) = inbox.lock() {
                queue.push(Wire::Up);
            }
            loop {
                for (delay_ms, json) in SCRIPT {
                    thread::sleep(Duration::from_millis(*delay_ms));
                    if let Some(msg) = robot_face_protocol::parse(json) {
                        if let Ok(mut queue) = inbox.lock() {
                            queue.push(Wire::Message(msg));
                        }
                    }
                }
                // Idle noise between runs, so the meter is not frozen while nobody talks.
                for _ in 0..40 {
                    thread::sleep(Duration::from_millis(120));
                    let json = r#"{"type":"level","dbfs":-54,"floor_dbfs":-60,"speech":false}"#;
                    if let Some(msg) = robot_face_protocol::parse(json) {
                        if let Ok(mut queue) = Arc::clone(&inbox).lock() {
                            queue.push(Wire::Message(msg));
                        }
                    }
                }
            }
        })
        .expect("failed to start the demo thread");
}
