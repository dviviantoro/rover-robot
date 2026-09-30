//! The robot panel's brain: what robot-core says, and what the screen should show.
//!
//! Deliberately free of Slint, sockets and clocks, so it builds and tests in seconds on
//! any machine -- including this Jetson while the robot is running, which a Slint build
//! cannot do (the board routinely has under 300 MiB free).
//!
//! See `dev-docs/modules/panel.md` for how the app crate projects this onto a screen.

pub mod events;
pub mod state;

pub use events::{parse, Incoming, Outgoing};
pub use state::{Face, Level, Line, UiState, Voice, Who};
