//! Saved model Registry reads and the existing Settings projection.

pub mod presentation;
pub mod registry;

#[cfg(feature = "component")]
mod component;
#[cfg(test)]
mod oracle_tests;

/// A fixed failure from the original Registry or Settings projection.
#[derive(Debug, PartialEq, Eq)]
pub struct SourceError {
    pub exception_class: &'static str,
    pub message: String,
}

impl SourceError {
    pub(crate) fn new(exception_class: &'static str, message: impl Into<String>) -> Self {
        Self {
            exception_class,
            message: message.into(),
        }
    }
}
