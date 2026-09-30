//! Wait for a screen before asking Slint for a window.
//!
//! This exists because of exactly how Slint's linuxkms backend fails. It scans
//! `/dev/dri/` with no filtering, tries each node, and if none has a connected connector
//! it returns `No connected display connector found` during platform *initialisation* --
//! before the event loop starts. The process exits non-zero.
//!
//! With the project's standard `Restart=on-failure` plus `StartLimitBurst=5`, that means
//! five restarts and then `failed`, permanently, until someone notices. On a robot whose
//! panel is unplugged that is precisely the wrong behaviour: the service should sit
//! patiently and light up when a screen appears.
//!
//! So: check sysfs first, which needs no libdrm call and takes no DRM master. Measured on
//! this board, the connected panel shows up as `card1-DP-1` -- Tegra's HDMI port
//! enumerates as DisplayPort, which is normal and not a sign of anything wrong.

use std::time::Duration;

/// Every connector the kernel knows about, and whether something is plugged into it.
pub fn connectors() -> Vec<(String, String)> {
    let mut found = Vec::new();
    let Ok(entries) = std::fs::read_dir("/sys/class/drm") else {
        return found;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        // `card1-DP-1` is a connector; `card1` and `renderD129` are not.
        if !name.contains('-') {
            continue;
        }
        if let Ok(status) = std::fs::read_to_string(entry.path().join("status")) {
            found.push((name, status.trim().to_string()));
        }
    }
    found.sort();
    found
}

pub fn any_connected() -> bool {
    connectors().iter().any(|(_, status)| status == "connected")
}

/// Block until a display is plugged in. Never gives up.
///
/// Logs once at the start and then once a minute, so an unplugged robot leaves an honest
/// trail without filling the journal.
///
/// Only the `kms` build calls this -- a windowed or headless build has no connector to
/// wait for -- so it is dead code elsewhere by design, not by accident.
#[cfg_attr(not(feature = "kms"), allow(dead_code))]
pub fn wait_for_connector(poll: Duration) {
    if any_connected() {
        for (name, status) in connectors() {
            if status == "connected" {
                log::info!("display on {name}");
            }
        }
        return;
    }

    log::warn!(
        "no display connected ({}); waiting. The panel will start by itself when a \
         screen is plugged in.",
        describe()
    );
    let mut waited = Duration::ZERO;
    loop {
        std::thread::sleep(poll);
        waited += poll;
        if any_connected() {
            log::info!("display appeared after {}s", waited.as_secs());
            return;
        }
        if waited.as_secs() % 60 < poll.as_secs() {
            log::info!("still waiting for a display ({})", describe());
        }
    }
}

#[cfg_attr(not(feature = "kms"), allow(dead_code))]
fn describe() -> String {
    let found = connectors();
    if found.is_empty() {
        // This is the state the board was in before `modprobe nvidia-drm`: the only DRM
        // node was tegra_drm on host1x, which exposes no connectors at all. Worth naming,
        // because "no connectors" and "nothing plugged in" look identical from here.
        return "no connectors at all -- is nvidia-drm loaded?".to_string();
    }
    found
        .iter()
        .map(|(name, status)| format!("{name}={status}"))
        .collect::<Vec<_>>()
        .join(", ")
}
