//! Generic in-process Launcher adapter. Every runtime dispatch uses Core.

#[path = "../../src-tauri/src/core_host_commands.rs"]
mod commands;
mod isolation;
mod native_contract;
mod runtime;
mod transport;

pub use commands::CORE_HOST_COMMANDS;
pub use isolation::{
    HostCaller, HostError, PACK_UI_CSP, PackAssetPath, PackAssetRequest, trusted_host_origin,
};
pub use native_contract::{
    ShutdownCompletion, ShutdownObservation, TrustedReview, observe_shutdown,
    preserve_primary_close, restore_primary, startup_ui_embedding, trusted_review,
};
pub use runtime::{
    LauncherCore, UiFrame, UiOperationRegistration, UiTransportResponse, UiTransportTicket,
};
pub use transport::{
    TRUSTED_HOST_RECEIVER, TicketReservation, TransportTickets, copy_final_host_payload,
};
// All host-visible runtime types cross the single Core facade dependency.
pub use tobkiri_core as core;
