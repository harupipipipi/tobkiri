//! Keep Pack console text separate from native machine-readable replies.
use std::io::{self, Write};

#[derive(Clone, Copy, Default)]
pub(super) enum ConsoleOutput {
    #[default]
    Stdout,
    Stderr,
}

impl ConsoleOutput {
    pub(super) fn write(
        self,
        action: impl FnOnce(&mut dyn Write) -> io::Result<()>,
    ) -> io::Result<()> {
        match self {
            Self::Stdout => action(&mut io::stdout().lock()),
            Self::Stderr => action(&mut io::stderr().lock()),
        }
    }
}
