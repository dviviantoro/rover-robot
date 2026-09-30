//! Where the panel gets its settings. Environment only -- systemd hands them over via
//! `EnvironmentFile`, exactly as every other service on this robot is configured.

use std::time::Duration;

pub struct Config {
    /// `ws://127.0.0.1:8090/`. robot-core binds loopback on purpose, so in development
    /// this is reached with `ssh -L 8090:127.0.0.1:8090` rather than by opening the bind
    /// address to the LAN.
    pub url: String,
    /// robot-core takes the token as a QUERY PARAMETER on the upgrade request -- not a
    /// header, not a handshake message -- and answers 401 before upgrading if it is
    /// wrong. Empty means the server is running without auth.
    pub token: String,
    /// How many conversation lines to draw. The rest stay in scrollback.
    pub chat_lines: usize,
    /// The server never pings; keepalive is entirely ours.
    pub ping: Duration,
    /// Nothing received at all for this long means the link is dead even though the TCP
    /// socket has not noticed.
    pub dead_link: Duration,
}

fn env_or<T: std::str::FromStr>(key: &str, fallback: T) -> T {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(fallback)
}

impl Config {
    pub fn from_env() -> Config {
        Config {
            url: std::env::var("FACE_WS_URL")
                .unwrap_or_else(|_| "ws://127.0.0.1:8090/".to_string()),
            token: std::env::var("CORE_WS_TOKEN").unwrap_or_default(),
            chat_lines: env_or("FACE_CHAT_LINES", 7usize),
            ping: Duration::from_secs(env_or("FACE_PING_S", 20u64)),
            dead_link: Duration::from_secs(env_or("FACE_DEAD_LINK_S", 45u64)),
        }
    }

    /// The URL actually connected to, token appended.
    pub fn endpoint(&self) -> String {
        if self.token.is_empty() {
            return self.url.clone();
        }
        let join = if self.url.contains('?') { '&' } else { '?' };
        format!("{}{}token={}", self.url, join, self.token)
    }

    /// The endpoint with the token blanked, for logs. A token in a log file is a token
    /// in a backup (AGENTS.md constraint 8).
    pub fn safe_endpoint(&self) -> String {
        if self.token.is_empty() {
            format!("{} (no token)", self.url)
        } else {
            format!("{} (token set)", self.url)
        }
    }
}
