use std::io::{self, Read};
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

/// Deadline for short-lived inspection subprocesses (`lsof`, `ps`,
/// `netstat`, `tasklist`) that run while lifecycle locks are held.
pub const INSPECTION_COMMAND_TIMEOUT: Duration = Duration::from_secs(10);

/// Poll interval used while waiting out an inspection subprocess deadline.
const INSPECTION_COMMAND_POLL_INTERVAL: Duration = Duration::from_millis(10);

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

/// Run `command` with piped output under a hard deadline.
///
/// Inspection helpers execute while lifecycle mutexes are held, so an
/// unbounded `Command::output()` could wedge every supervised process behind
/// one hung diagnostic tool. The child is killed and reaped when the
/// deadline expires. Output is drained only after the process exits — a
/// process cannot exit while a pipe write is still blocked — which keeps
/// this single-threaded drain safe for bounded diagnostic output.
pub fn bounded_output(command: &mut Command, timeout: Duration) -> io::Result<Output> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn()?;
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(INSPECTION_COMMAND_POLL_INTERVAL);
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "inspection command exceeded its deadline",
                ));
            }
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        }
    };
    let mut stdout = Vec::new();
    if let Some(mut pipe) = child.stdout.take() {
        pipe.read_to_end(&mut stdout)?;
    }
    let mut stderr = Vec::new();
    if let Some(mut pipe) = child.stderr.take() {
        pipe.read_to_end(&mut stderr)?;
    }
    Ok(Output {
        status,
        stdout,
        stderr,
    })
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
