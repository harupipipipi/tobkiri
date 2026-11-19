//! Reads and saved message transactions for the original saved conversation owner document.

#[cfg(any(feature = "component", test))]
mod legacy_confirmation;

#[cfg(any(feature = "component", test))]
mod legacy_append_context;

#[cfg(any(feature = "component", test))]
mod legacy_append_receipt;

#[cfg(any(feature = "component", test))]
mod legacy_message_append;

#[cfg(any(feature = "component", test))]
mod old_message_lifecycle;

#[cfg(any(feature = "component", test))]
mod legacy_read;

#[cfg(any(feature = "component", test))]
mod saved_model;

#[cfg(feature = "component")]
mod component;
