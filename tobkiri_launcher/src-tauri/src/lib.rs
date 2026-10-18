//! Rumi Viewer — Tauri application library.
//!
//! V2: Full implementation with setup hook, commands, tray menu, and navigation guard.

#[cfg(feature = "core-host")]
mod core_host;
#[cfg(feature = "core-host")]
mod native_startup;

#[cfg(all(feature = "core-host", target_os = "macos"))]
mod native_plan_review;
#[cfg(all(feature = "core-host", windows))]
mod native_plan_review_windows;
#[cfg(all(feature = "core-host", target_os = "macos"))]
mod project_directory_picker;
#[cfg(feature = "core-host")]
mod shutdown_budget;
#[cfg(feature = "core-host")]
mod updater;

#[cfg(feature = "core-host")]
pub fn run() {
    core_host::run();
}

#[cfg(not(feature = "core-host"))]
include!("legacy.rs");
