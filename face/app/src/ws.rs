//! The link to robot-core: one blocking thread, reconnecting for ever.
//!
//! Blocking `tungstenite` rather than tokio, deliberately. robot-core's server is a
//! hand-rolled RFC 6455 with no subprotocols, no extensions and no permessage-deflate; it
//! never sends Ping, though it answers ours. One thread with a read timeout does
//! everything an async runtime would here, and leaves a couple of MB and a lot of binary
//! out of a board that measures every MiB.
//!
//! Everything this thread learns goes into a shared queue. The UI drains it on a timer,
//! which is what coalesces the ~8/s level messages into at most one repaint per tick.

use std::sync::mpsc::{Receiver, TryRecvError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use robot_face_protocol::{Incoming, Outgoing};
use tungstenite::stream::MaybeTlsStream;

use crate::config::Config;

/// What the UI thread needs to know, beyond the messages themselves.
pub enum Wire {
    Up,
    Down,
    /// The token was refused. Reported separately because the fix is completely
    /// different from "not connected yet" and must not hide behind a spinner.
    AuthRejected,
    Message(Incoming),
}

pub type Inbox = Arc<Mutex<Vec<Wire>>>;

/// How long a single read may block. Small enough that keepalive and the outbox stay
/// responsive; large enough that the thread is not a spin loop.
const READ_TIMEOUT: Duration = Duration::from_millis(200);

/// A refused token is not going to fix itself. Retrying every half second would fill the
/// journal and hammer the server for nothing.
const AUTH_BACKOFF: Duration = Duration::from_secs(30);

pub fn run(cfg: Config, inbox: Inbox, outbox: Receiver<Outgoing>) -> ! {
    let mut backoff = Duration::from_millis(500);
    let max_backoff = Duration::from_secs(5);

    loop {
        log::info!("connecting to {}", cfg.safe_endpoint());
        match tungstenite::connect(cfg.endpoint()) {
            Ok((mut socket, _)) => {
                backoff = Duration::from_millis(500);
                push(&inbox, Wire::Up);
                log::info!("linked");
                pump(&mut socket, &cfg, &inbox, &outbox);
                push(&inbox, Wire::Down);
                log::warn!("link dropped");
            }
            // 401 arrives before the upgrade, so this is knowable rather than a mystery.
            Err(tungstenite::Error::Http(response)) if response.status() == 401 => {
                log::error!(
                    "robot-core refused the token (401). Check CORE_WS_TOKEN in .env \
                     matches the one robot-core was started with."
                );
                push(&inbox, Wire::AuthRejected);
                std::thread::sleep(AUTH_BACKOFF);
            }
            Err(err) => {
                log::warn!("cannot reach robot-core ({err}); retrying");
                push(&inbox, Wire::Down);
                std::thread::sleep(backoff);
                backoff = (backoff * 2).min(max_backoff);
            }
        }
    }
}

fn pump(
    socket: &mut tungstenite::WebSocket<MaybeTlsStream<std::net::TcpStream>>,
    cfg: &Config,
    inbox: &Inbox,
    outbox: &Receiver<Outgoing>,
) {
    // The read timeout is what turns a blocking socket into a poll loop. Without it this
    // thread would sit in `read()` and never send a keepalive or a cancel.
    if let MaybeTlsStream::Plain(stream) = socket.get_ref() {
        let _ = stream.set_read_timeout(Some(READ_TIMEOUT));
        let _ = stream.set_nodelay(true);
    }

    let mut last_ping = Instant::now();
    let mut last_seen = Instant::now();

    loop {
        match socket.read() {
            Ok(tungstenite::Message::Text(text)) => {
                last_seen = Instant::now();
                match robot_face_protocol::parse(&text) {
                    Some(msg) => push(inbox, Wire::Message(msg)),
                    // Unparseable is not fatal: robot-core grows fields, and a panel that
                    // drops its link over one is a panel that goes blank on deploy day.
                    None => log::debug!("ignored an unreadable message"),
                }
            }
            Ok(tungstenite::Message::Close(_)) => return,
            Ok(_) => last_seen = Instant::now(), // pong, binary, ping -- all signs of life
            Err(tungstenite::Error::Io(e))
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                // Nothing to read this tick. Not an error; tungstenite keeps any partial
                // frame buffered for the next call.
            }
            Err(err) => {
                log::warn!("read failed: {err}");
                return;
            }
        }

        // Anything the UI wants to say.
        loop {
            match outbox.try_recv() {
                Ok(message) => {
                    let json = serde_json::to_string(&message).unwrap_or_default();
                    if let Err(err) = socket.send(tungstenite::Message::Text(json.into())) {
                        log::warn!("send failed: {err}");
                        return;
                    }
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => break,
            }
        }

        if last_ping.elapsed() >= cfg.ping {
            last_ping = Instant::now();
            if let Err(err) = socket.send(tungstenite::Message::Ping(Vec::new().into())) {
                log::warn!("keepalive failed: {err}");
                return;
            }
        }
        if let Err(err) = socket.flush() {
            log::warn!("flush failed: {err}");
            return;
        }

        // A TCP socket can stay open long after the peer has stopped caring. Since the
        // server never initiates traffic when idle... it does: `level` and `health` keep
        // arriving. Silence this long means the link is dead whatever TCP thinks.
        if last_seen.elapsed() >= cfg.dead_link {
            log::warn!(
                "nothing from robot-core for {}s; reconnecting",
                cfg.dead_link.as_secs()
            );
            return;
        }
    }
}

fn push(inbox: &Inbox, item: Wire) {
    if let Ok(mut queue) = inbox.lock() {
        // Bounded, so a UI thread that stalls cannot make this grow without limit. The
        // oldest go first: on a face, the newest state is the one that matters.
        if queue.len() > 4096 {
            queue.drain(0..2048);
        }
        queue.push(item);
    }
}
