use std::process::Command;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

pub fn command(program: impl AsRef<std::ffi::OsStr>) -> Command {
    let mut command = Command::new(program);
    hide_console_window(&mut command);
    unblock_shutdown_signals(&mut command);
    command
}

/// Build a Python command isolated from ambient import settings with bytecode
/// writes disabled by an interpreter flag rather than an environment variable.
pub fn isolated_python(program: impl AsRef<std::ffi::OsStr>) -> Command {
    let mut command = command(program);
    command.args(["-I", "-B"]);
    command
}

/// Restore TERM/INT/HUP delivery in a spawned child.
///
/// The launcher's shutdown watcher blocks those signals on the main thread so
/// `sigwait` can collect them; every spawned thread — and every forked child —
/// inherits that mask. Managed Python roles install their own signal handlers,
/// so without this their graceful stops always degrade to SIGKILL.
#[cfg(unix)]
fn unblock_shutdown_signals(command: &mut Command) {
    use std::os::unix::process::CommandExt;
    unsafe {
        command.pre_exec(|| {
            let mut set: libc::sigset_t = std::mem::zeroed();
            libc::sigemptyset(&mut set);
            libc::sigaddset(&mut set, libc::SIGTERM);
            libc::sigaddset(&mut set, libc::SIGINT);
            libc::sigaddset(&mut set, libc::SIGHUP);
            // pthread_sigmask returns the errno value directly rather than
            // setting errno; fail the spawn instead of leaving the child with
            // the parent's blocked shutdown mask.
            let error = libc::pthread_sigmask(libc::SIG_UNBLOCK, &set, std::ptr::null_mut());
            if error != 0 {
                return Err(std::io::Error::from_raw_os_error(error));
            }
            Ok(())
        });
    }
}

#[cfg(not(unix))]
fn unblock_shutdown_signals(_: &mut Command) {}

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
