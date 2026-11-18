//! Revision-bound model reads from the original saved conversation document.

#[cfg(any(feature = "component", test))]
mod saved_model;

#[cfg(feature = "component")]
mod component;
