//! Reads and completion transitions for the original saved conversation owner document.

#[cfg(any(feature = "component", test))]
mod legacy_confirmation;

#[cfg(any(feature = "component", test))]
mod legacy_read;

#[cfg(any(feature = "component", test))]
mod saved_model;

#[cfg(feature = "component")]
mod component;
