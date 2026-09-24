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

/// Retention cap for a single inspection stream. Larger outputs keep
/// draining (so the child never wedges on a full pipe) but are truncated —
/// `netstat -ano` on a busy machine can exceed the pipe buffer.
const INSPECTION_OUTPUT_CAP: usize = 4 * 1024 * 1024;

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
/// deadline expires. Both pipes drain on background threads so a child
/// with more than a pipe-buffer of output (an unfiltered `netstat -ano`
/// on a busy machine) still finishes instead of blocking on write until
/// the deadline kills it.
pub fn bounded_output(command: &mut Command, timeout: Duration) -> io::Result<Output> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn()?;
    let stdout_handle = child.stdout.take().map(drain_capped);
    let stderr_handle = child.stderr.take().map(drain_capped);
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
                join_drain(stdout_handle);
                join_drain(stderr_handle);
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "inspection command exceeded its deadline",
                ));
            }
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                join_drain(stdout_handle);
                join_drain(stderr_handle);
                return Err(error);
            }
        }
    };
    Ok(Output {
        status,
        stdout: join_drain(stdout_handle),
        stderr: join_drain(stderr_handle),
    })
}

/// Drain a pipe to EOF retaining at most [`INSPECTION_OUTPUT_CAP`] bytes.
fn drain_capped<R>(mut pipe: R) -> std::thread::JoinHandle<Vec<u8>>
where
    R: Read + Send + 'static,
{
    std::thread::spawn(move || {
        let mut retained = Vec::new();
        let mut buffer = [0u8; 8192];
        loop {
            match pipe.read(&mut buffer) {
                Ok(0) | Err(_) => return retained,
                Ok(count) => {
                    let keep = (INSPECTION_OUTPUT_CAP - retained.len()).min(count);
                    retained.extend_from_slice(&buffer[..keep]);
                }
            }
        }
    })
}

fn join_drain(handle: Option<std::thread::JoinHandle<Vec<u8>>>) -> Vec<u8> {
    handle
        .and_then(|handle| handle.join().ok())
        .unwrap_or_default()
}

/// Kernel start-time token for `pid`, used to detect pid recycling.
///
/// Two different processes that briefly share one pid value never share a
/// start marker, so comparing the recorded marker before signalling a
/// process group keeps a recycled leader's foreign group out of the kill
/// path. Returns `None` when the process is gone or the platform cannot
/// produce a marker — callers must treat `None` as "cannot prove identity".
#[cfg(target_os = "macos")]
pub fn process_start_marker(pid: u32) -> Option<u64> {
    let mut info = std::mem::MaybeUninit::<libc::proc_bsdinfo>::uninit();
    let size = std::mem::size_of::<libc::proc_bsdinfo>() as i32;
    let written = unsafe {
        libc::proc_pidinfo(
            pid as i32,
            libc::PROC_PIDTBSDINFO,
            0,
            info.as_mut_ptr() as *mut libc::c_void,
            size,
        )
    };
    if written != size {
        return None;
    }
    let info = unsafe { info.assume_init() };
    Some(
        u64::from(info.pbi_start_tvsec).saturating_mul(1_000_000)
            + u64::from(info.pbi_start_tvusec),
    )
}

/// Linux marker: field 22 (`starttime`) of `/proc/<pid>/stat`, read past
/// the parenthesized comm so a process name containing spaces or parens
/// cannot shift the field positions.
#[cfg(target_os = "linux")]
pub fn process_start_marker(pid: u32) -> Option<u64> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let after_comm = stat.rfind(')')?;
    let fields: Vec<&str> = stat[after_comm + 1..].split_whitespace().collect();
    // Fields after ')' begin at field 3, so starttime (field 22) is index 19.
    fields.get(19)?.parse::<u64>().ok()
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub fn process_start_marker(_pid: u32) -> Option<u64> {
    None
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
