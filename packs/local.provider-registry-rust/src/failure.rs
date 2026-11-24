//! Original source exceptions retain raw arguments separately from their text.

use ordered_python_compat::{PyValue, pyrepr, pystr};
use provider_model_access::registry_support::SourceFailure;

#[derive(Clone, Debug, PartialEq)]
pub struct SourceError {
    pub exception_class: &'static str,
    pub message: String,
    pub args: Vec<PyValue>,
}

pub type SourceResult<T> = Result<T, SourceError>;

impl SourceError {
    /// Construct the original exception with one string argument.
    pub fn new(exception_class: &'static str, argument: impl Into<String>) -> Self {
        Self::from_args(exception_class, vec![PyValue::String(argument.into())])
    }

    /// Apply BaseException/KeyError string rules to the actual argument tuple.
    pub fn from_args(exception_class: &'static str, args: Vec<PyValue>) -> Self {
        let message = match args.as_slice() {
            [] => String::new(),
            [value] if exception_class == "KeyError" => pyrepr(value),
            [value] => pystr(value),
            values => tuple_repr(values),
        };
        Self {
            exception_class,
            message,
            args,
        }
    }

    pub fn value(argument: impl Into<String>) -> Self {
        Self::new("ValueError", argument)
    }

    pub fn type_error(argument: impl Into<String>) -> Self {
        Self::new("TypeError", argument)
    }

    pub fn permission(argument: impl Into<String>) -> Self {
        Self::new("PermissionError", argument)
    }

    pub fn conflict(argument: impl Into<String>) -> Self {
        Self::new("ProviderRegistryConflict", argument)
    }

    pub fn key_error(argument: impl Into<String>) -> Self {
        Self::new("KeyError", argument)
    }

    pub fn args_repr(&self) -> String {
        tuple_repr(&self.args)
    }
}

fn tuple_repr(values: &[PyValue]) -> String {
    let body = values.iter().map(pyrepr).collect::<Vec<_>>().join(", ");
    format!("({body}{})", if values.len() == 1 { "," } else { "" })
}

impl From<SourceFailure> for SourceError {
    fn from(value: SourceFailure) -> Self {
        Self::new(value.exception_class, value.message)
    }
}

impl std::fmt::Display for SourceError {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        output.write_str(&self.message)
    }
}

impl std::error::Error for SourceError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_error_keeps_the_raw_argument_and_python_quoted_message() {
        let error = SourceError::key_error("provider instance is unknown");
        assert_eq!(error.message, "'provider instance is unknown'");
        assert_eq!(error.args_repr(), "('provider instance is unknown',)");
        assert_eq!(
            error.args,
            vec![PyValue::String("provider instance is unknown".into())]
        );
    }

    #[test]
    fn exception_argument_tuples_keep_empty_single_and_multiple_shapes() {
        assert_eq!(
            SourceError::from_args("ValueError", vec![]).args_repr(),
            "()"
        );
        let error = SourceError::from_args("ValueError", vec![PyValue::Bool(true), PyValue::Null]);
        assert_eq!(error.message, "(True, None)");
        assert_eq!(error.args_repr(), "(True, None)");
    }
}
