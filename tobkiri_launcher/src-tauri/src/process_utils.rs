use std::process::Command;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

pub fn command(program: impl AsRef<std::ffi::OsStr>) -> Command {
    let mut command = Command::new(program);
    hide_console_window(&mut command);
    command
}

/// Build a Python command isolated from ambient import settings with bytecode
/// writes disabled by an interpreter flag rather than an environment variable.
pub fn isolated_python(program: impl AsRef<std::ffi::OsStr>) -> Command {
    let mut command = command(program);
    command.args(["-I", "-B"]);
    command
}

pub fn hide_console_window(command: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    #[cfg(not(windows))]
    {
        let _ = command;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn isolated_python_policy_is_explicit_and_environment_independent() {
        let command = isolated_python("python3");
        let args = command
            .get_args()
            .map(|value| value.to_string_lossy().into_owned())
            .collect::<Vec<_>>();

        assert_eq!(args, ["-I", "-B"]);
    }
}
