//! robot-face -- the robot's panel.
//!
//! Chat and voice interaction on a screen attached to the robot, rendered straight to
//! DRM/KMS with no desktop, no compositor and no session. The browser dashboard keeps
//! everything else; this is only the conversational surface.
//!
//! The interesting decisions all live in `robot-face-protocol`, which has no Slint and no
//! sockets and is tested with plain `cargo test`. This file is the plumbing: a socket
//! thread, a timer, and a projection of `UiState` onto Slint properties.

#[cfg(not(any(feature = "kms", feature = "desktop", feature = "headless")))]
compile_error!(
    "pick a backend: --features kms (the robot's panel), desktop (a laptop window), \
     or headless (windowless, screenshots over MCP)"
);

mod config;
mod display;
mod replay;
mod ws;

use std::rc::Rc;
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use robot_face_protocol::state::{Face, UiState, Voice};
use robot_face_protocol::Outgoing;
use slint::{ModelRc, VecModel};

slint::include_modules!();

/// How often the UI drains the socket queue and repaints.
///
/// 10 Hz, against ~8 level messages a second. This is the coalescing step: without it
/// every level frame would be its own repaint, and a meter would cost as much as the
/// conversation.
const TICK: Duration = Duration::from_millis(100);

fn main() -> Result<(), slint::PlatformError> {
    env_logger::Builder::from_env(
        env_logger::Env::default().default_filter_or(
            std::env::var("LOG_LEVEL").unwrap_or_else(|_| "info".into()),
        ),
    )
    .format_timestamp_secs()
    .init();

    let cfg = config::Config::from_env();
    let demo = std::env::args().any(|a| a == "--demo");
    let chat_lines = cfg.chat_lines;

    let inbox: ws::Inbox = Arc::new(Mutex::new(Vec::new()));
    let (out_tx, out_rx) = mpsc::channel::<Outgoing>();

    // The socket thread starts FIRST and never needs a display. If the panel is
    // unplugged we still want the link up, the logs honest, and the state current for
    // the moment a screen appears.
    if demo {
        log::info!("--demo: replaying a scripted conversation, no robot needed");
        replay::spawn(Arc::clone(&inbox));
    } else {
        let inbox = Arc::clone(&inbox);
        std::thread::Builder::new()
            .name("robot-core".into())
            .spawn(move || ws::run(cfg, inbox, out_rx))
            .expect("failed to start the link thread");
    }

    // Only now, and only on the real panel, wait for a screen. Slint's linuxkms backend
    // exits during platform init when there is no connected connector, which under
    // Restart=on-failure would burn the start limit and leave the unit permanently
    // failed on a robot whose panel is simply not plugged in yet.
    #[cfg(feature = "kms")]
    display::wait_for_connector(Duration::from_secs(5));
    #[cfg(not(feature = "kms"))]
    let _ = display::any_connected();

    let ui = PanelWindow::new()?;
    let lines: Rc<VecModel<ChatLine>> = Rc::new(VecModel::default());
    ui.set_lines(ModelRc::from(lines.clone()));

    {
        let tx = out_tx.clone();
        ui.on_stop_requested(move || {
            log::info!("stop requested from the panel");
            let _ = tx.send(Outgoing::Cancel);
        });
    }

    let mut state = UiState::new();
    let mut drawn_rev = u64::MAX; // force the first paint
    let started = Instant::now();
    let weak = ui.as_weak();

    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, TICK, move || {
        let Some(ui) = weak.upgrade() else { return };
        let now_ms = started.elapsed().as_millis() as u64;

        // Drain everything the socket thread has queued since the last tick. Applying a
        // burst and painting once is the whole point.
        let batch: Vec<ws::Wire> = match inbox.lock() {
            Ok(mut queue) => std::mem::take(&mut *queue),
            Err(_) => Vec::new(),
        };
        for item in batch {
            match item {
                ws::Wire::Up => state.on_link_up(now_ms),
                ws::Wire::Down => state.on_link_down(now_ms),
                ws::Wire::AuthRejected => state.on_auth_failed(now_ms),
                ws::Wire::Message(msg) => state.apply(msg, now_ms),
            }
        }
        state.tick(now_ms);

        // The conversation is rebuilt only when it actually changed. At 10 Hz against a
        // reply that produces a sentence every couple of seconds, that is the difference
        // between relayouting the chat ten times a second and twice.
        if state.lines_rev != drawn_rev {
            drawn_rev = state.lines_rev;
            let visible: Vec<ChatLine> = state
                .visible(chat_lines)
                .iter()
                .map(|line| ChatLine {
                    robot: line.who == robot_face_protocol::Who::Robot,
                    text: line.text.as_str().into(),
                    language: line.language.as_str().into(),
                    streaming: line.streaming,
                    typed: line.typed,
                })
                .collect();
            lines.set_vec(visible);
        }

        ui.set_voice(match state.voice {
            Voice::Offline => VoiceState::Offline,
            Voice::Idle => VoiceState::Idle,
            Voice::Hearing => VoiceState::Hearing,
            Voice::Transcribing => VoiceState::Transcribing,
            Voice::Thinking => VoiceState::Thinking,
            Voice::Speaking => VoiceState::Speaking,
        });
        ui.set_face(match state.face {
            Face::Neutral => FaceKind::Neutral,
            Face::Happy => FaceKind::Happy,
            Face::Thinking => FaceKind::Thinking,
            Face::Listening => FaceKind::Listening,
            Face::Speaking => FaceKind::Speaking,
            Face::Sad => FaceKind::Sad,
        });
        ui.set_dbfs(state.level.dbfs);
        ui.set_floor_dbfs(state.level.floor_dbfs);
        ui.set_speech(state.level.speech);
        ui.set_gate_open(state.level.gate_open);
        ui.set_linked(state.linked);
        ui.set_auth_failed(state.auth_failed);
        ui.set_language(state.language.as_str().into());
        ui.set_notice(
            state
                .notice
                .as_ref()
                .map(|n| n.text.as_str())
                .unwrap_or("")
                .into(),
        );
        // A face that never blinks is unsettling to stand in front of. Cheap: two small
        // rectangles change height for ~200 ms.
        ui.set_blink(now_ms % 5_000 < 160);
    });

    ui.run()
}
