//! The optional in-process host exposes only these generic IPC commands.

pub const CORE_HOST_COMMANDS: &[&str] = &[
    "core_start_profile",
    "core_stop_session",
    "core_host_output_ack",
    "pack_ui_invoke",
    "pack_ui_register",
    "pack_ui_cancel",
    "pack_ui_ack",
    "pack_ui_detach",
    "pack_ui_stream_pull",
    "pack_ui_stream_cancel",
    "core_terminal_reply",
];
