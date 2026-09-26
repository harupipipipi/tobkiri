use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
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
///
/// The stream keeps draining past the cap so the child never wedges on a
/// full pipe; only the retained buffer is bounded.
pub(crate) fn drain_capped<R>(mut pipe: R) -> std::thread::JoinHandle<Vec<u8>>
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

// ---------------------------------------------------------------------------
// Bounded persistence for supervised-child output
// ---------------------------------------------------------------------------

/// Per-launch persistence cap for one supervised child's log file. Draining
/// always continues past the cap (a wedged child is worse than a truncated
/// log); only durable retention stops.
pub(crate) const CHILD_LOG_MAX_BYTES: u64 = 8 * 1024 * 1024;

/// Cap for append-style diagnostic logs that survive across runs
/// (`python-provision.log`). The previous generation rotates to
/// `<name>.prev` so retained bytes stay strictly bounded.
pub(crate) const PERSISTENT_LOG_MAX_BYTES: u64 = 4 * 1024 * 1024;

/// One bounded log sink shared by a child's stdout/stderr drain threads.
pub(crate) struct ChildLog {
    file: Mutex<Option<fs::File>>,
    remaining: AtomicU64,
    truncation_marked: AtomicBool,
}

impl ChildLog {
    /// Append `text` while budget remains; once it is spent, write one
    /// truncation marker and persist nothing further.
    fn append(&self, stream: &'static str, text: &str) {
        let mut guard = match self.file.lock() {
            Ok(guard) => guard,
            Err(_) => return,
        };
        let Some(file) = guard.as_mut() else { return };
        let bytes = text.as_bytes();
        let mut allowed = 0_u64;
        let _ = self.remaining.fetch_update(
            Ordering::SeqCst,
            Ordering::SeqCst,
            |left| {
                allowed = left.min(bytes.len() as u64);
                (left > 0).then(|| left - allowed)
            },
        );
        if allowed > 0 {
            let _ = file.write_all(&bytes[..allowed as usize]);
        }
        if (allowed as usize) < bytes.len()
            && !self.truncation_marked.swap(true, Ordering::SeqCst)
        {
            let _ = writeln!(
                file,
                "[{stream}] --- output truncated: per-launch log cap reached ---"
            );
        }
    }
}

/// Rotate `path` to `<name>.prev` when it already reached `max_bytes`.
/// Fails closed: a rotation error surfaces instead of silently letting an
/// append-only log grow past its bound.
pub(crate) fn bound_persistent_log(path: &Path, max_bytes: u64) -> io::Result<()> {
    let oversized = match fs::metadata(path) {
        Ok(metadata) => metadata.len() >= max_bytes,
        // No file yet simply means nothing needs rotating.
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        // Fail closed: a stat failure must not silently skip the bound.
        Err(error) => return Err(error),
    };
    if !oversized {
        return Ok(());
    }
    let previous = rotated_log_path(path);
    let _ = fs::remove_file(&previous);
    fs::rename(path, &previous)
}

fn rotated_log_path(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(".prev");
    PathBuf::from(name)
}

/// Open `path` as a fresh, size-bounded per-launch child log. Any existing
/// log rotates to `<name>.prev` first, so at most two bounded generations
/// persist and a prior crash's tail survives the restart.
pub(crate) fn open_child_log(path: &Path, max_bytes: u64) -> io::Result<Arc<ChildLog>> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    if path.exists() {
        let previous = rotated_log_path(path);
        let _ = fs::remove_file(&previous);
        fs::rename(path, &previous)?;
    }
    let file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)?;
    Ok(Arc::new(ChildLog {
        file: Mutex::new(Some(file)),
        remaining: AtomicU64::new(max_bytes),
        truncation_marked: AtomicBool::new(false),
    }))
}

/// Drain one child stream to EOF, mirroring to tracing and persisting into
/// `log`. Persistence is bounded by the shared budget and redacted before it
/// touches durable storage; the pipe itself is always fully drained so the
/// child can never wedge on output.
pub(crate) fn spawn_bounded_log_drain<R>(
    mut reader: R,
    log: Option<Arc<ChildLog>>,
    pid: u32,
    stream: &'static str,
    product: &'static str,
    mirror: bool,
) -> std::thread::JoinHandle<()>
where
    R: Read + Send + 'static,
{
    std::thread::spawn(move || {
        let mut buffer = [0_u8; 8192];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => break,
                Ok(count) => {
                    let output =
                        redact_log_text(&String::from_utf8_lossy(&buffer[..count]));
                    if mirror {
                        if stream == "stderr" {
                            log::warn!("{product} [{stream} pid={pid}]: {}", output.trim_end());
                        } else {
                            log::info!("{product} [{stream} pid={pid}]: {}", output.trim_end());
                        }
                    }
                    if let Some(sink) = &log {
                        sink.append(stream, &format!("[{stream} pid={pid}] {output}"));
                    }
                }
                Err(_) => break,
            }
        }
    })
}

/// Secret-looking key names whose `key=value`, `key: value`, `--key value`,
/// quoted-key JSON (`"key":"v"`), or `Bearer <token>` payloads must not
/// reach durable logs. Compound spellings such as `access_token`,
/// `x-api-key`, or `aws_secret_access_key` still match because `_` and `-`
/// count as separators inside an identifier.
const LOG_SECRET_KEYS: &[&str] = &[
    "api_key",
    "api-key",
    "apikey",
    "authorization",
    "auth_token",
    "auth-token",
    "client_secret",
    "client-secret",
    "access_key",
    "access-key",
    "private_key",
    "private-key",
    "password",
    "passwd",
    "secret",
    "token",
    "bearer",
    "credential",
    "credentials",
];

/// Minimum length for a whitespace-separated bare `Bearer <token>` value so
/// prose like "the bearer of the token" is left untouched.
const BARE_BEARER_MIN_LEN: usize = 8;

fn is_token_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric()
        || matches!(byte, b'.' | b'_' | b'-' | b'~' | b'+' | b'/' | b'=')
}

/// Replace credential-shaped values in free-form text before it is written
/// to a durable log. Handles `key=value`, `key: value`, `--key value`,
/// `Bearer <token>`, and URL userinfo (`scheme://user:pass@host`). A secret
/// split across a read boundary may be only partially redacted, so callers
/// should prefer chunk-aligned secrecy where possible.
pub(crate) fn redact_log_text(text: &str) -> String {
    let with_keys = redact_keyed_values(text);
    redact_url_userinfo(&with_keys)
}

fn redact_url_userinfo(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(pos) = rest.find("://") {
        let (head, tail) = rest.split_at(pos + 3);
        out.push_str(head);
        let seg_end = tail
            .find(|c: char| {
                matches!(c, '/' | '?' | '#' | '"' | '\'') || c.is_whitespace()
            })
            .unwrap_or(tail.len());
        let segment = &tail[..seg_end];
        match segment.rfind('@') {
            Some(at) if segment[..at].contains(':') => {
                out.push_str("[REDACTED]@");
                rest = &tail[at + 1..];
            }
            _ => {
                out.push_str(segment);
                rest = &tail[seg_end..];
            }
        }
    }
    out.push_str(rest);
    out
}

fn redact_keyed_values(text: &str) -> String {
    let lower = text.to_ascii_lowercase();
    let bytes = lower.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut i = 0_usize;
    while i < text.len() {
        // Earliest secret key at or after i. `_` and `-` separate the parts
        // of a compound identifier (`access_token`, `x-api-key`), so only an
        // alphanumeric neighbour disqualifies a match; `tokenizer` still
        // cannot match because `i` is alphanumeric.
        let mut hit: Option<(usize, usize)> = None;
        for key in LOG_SECRET_KEYS {
            let mut from = i;
            while let Some(rel) = lower[from..].find(key) {
                let key_start = from + rel;
                let key_end = key_start + key.len();
                let prev_ok = key_start == 0
                    || !bytes[key_start - 1].is_ascii_alphanumeric();
                let next_ok = key_end >= bytes.len()
                    || !bytes[key_end].is_ascii_alphanumeric();
                if prev_ok && next_ok {
                    if hit.map_or(true, |(start, _)| key_start < start) {
                        hit = Some((key_start, key_end));
                    }
                    break;
                }
                from = key_end;
            }
        }
        let Some((key_start, key_end)) = hit else { break };
        out.push_str(&text[i..key_end]);

        // A quoted key may carry a closing quote before the separator
        // (`{"api_key":"x"}`, `{'password': 'x'}`).
        let mut cursor = key_end;
        if cursor < bytes.len() && matches!(bytes[cursor], b'"' | b'\'') {
            cursor += 1;
        }
        let mut consumed_space = false;
        while cursor < bytes.len() && bytes[cursor] == b' ' {
            cursor += 1;
            consumed_space = true;
        }
        let explicit_sep =
            cursor < bytes.len() && matches!(bytes[cursor], b'=' | b':');
        if explicit_sep {
            cursor += 1;
            while cursor < bytes.len() && bytes[cursor] == b' ' {
                cursor += 1;
            }
        }

        // Whitespace-separated values are accepted only for flag spellings
        // (`--token x`, `--api-key x`) and bare `Bearer <token>`; other prose
        // ("the bearer of the token") must not be mangled.
        let flag_style = {
            let mut back = key_start;
            while back > 0 && bytes[back - 1] == b' ' {
                back -= 1;
            }
            back >= 1 && bytes[back - 1] == b'-'
        };
        let bare_bearer = &lower[key_start..key_end] == "bearer" && !flag_style;
        let whitespace_sep = !explicit_sep && consumed_space;
        if !explicit_sep && !(whitespace_sep && (flag_style || bare_bearer)) {
            i = key_end;
            continue;
        }

        // Locate the value span, honouring an optional surrounding quote.
        // Every index read below is guarded by `cursor < bytes.len()` so a
        // text ending exactly at a separator can never panic.
        let mut value_prefix_end = cursor;
        let mut value_start = cursor;
        let value_end;
        if cursor < bytes.len() && matches!(bytes[cursor], b'"' | b'\'') {
            let mark = bytes[cursor];
            value_start = cursor + 1;
            value_prefix_end = value_start;
            value_end = bytes[value_start..]
                .iter()
                .position(|&b| b == mark)
                .map(|rel| value_start + rel)
                .unwrap_or(text.len());
        } else {
            // Header-style keys carry a scheme token ("Bearer <jwt>"), so
            // their value may span interior spaces; stop only at structural
            // delimiters or end of line.
            let allow_spaces = matches!(
                &lower[key_start..key_end],
                "authorization" | "credential" | "credentials"
            );
            value_end = bytes[cursor..]
                .iter()
                .position(|&b| {
                    if matches!(
                        b,
                        b'"' | b'\'' | b'&' | b',' | b';' | b')' | b']' | b'\n' | b'\r'
                    ) {
                        return true;
                    }
                    !allow_spaces && b.is_ascii_whitespace()
                })
                .map(|rel| cursor + rel)
                .unwrap_or(text.len());
        }
        let valid_value = if bare_bearer {
            value_end.saturating_sub(value_start) >= BARE_BEARER_MIN_LEN
                && bytes[value_start..value_end]
                    .iter()
                    .all(|&b| is_token_byte(b))
        } else {
            value_end > value_start
        };
        if !valid_value {
            i = key_end;
            continue;
        }
        out.push_str(&text[key_end..value_prefix_end]);
        out.push_str("[REDACTED]");
        i = value_end;
    }
    out.push_str(&text[i..]);
    out
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

    #[test]
    fn child_log_stops_persisting_after_budget_and_marks_once() {
        let dir = std::env::temp_dir().join(format!(
            "tobkiri-child-log-{}-{}",
            std::process::id(),
            unix_nanos()
        ));
        let path = dir.join("child.log");
        let log = open_child_log(&path, 32).unwrap();

        log.append("stdout", &"a".repeat(16));
        log.append("stderr", &"b".repeat(16));
        log.append("stdout", &"c".repeat(64));
        log.append("stderr", &"d".repeat(64));
        drop(log);

        let contents = fs::read_to_string(&path).unwrap();
        assert!(contents.len() <= 32 + 96);
        assert!(contents.contains("aaa"));
        assert!(contents.contains("truncated"));
        assert_eq!(contents.matches("truncated").count(), 1);

        // A second open rotates exactly one previous generation.
        let log = open_child_log(&path, 8).unwrap();
        log.append("stdout", "fresh");
        drop(log);
        let prev = rotated_log_path(&path);
        assert!(prev.exists());
        assert!(fs::read_to_string(prev).unwrap().contains("aaa"));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn bound_persistent_log_rotates_only_oversized_files() {
        let dir = std::env::temp_dir().join(format!(
            "tobkiri-bound-log-{}-{}",
            std::process::id(),
            unix_nanos()
        ));
        let path = dir.join("diag.log");
        fs::create_dir_all(&dir).unwrap();
        fs::write(&path, "small").unwrap();
        bound_persistent_log(&path, 16).unwrap();
        assert!(path.exists());
        assert!(!rotated_log_path(&path).exists());

        fs::write(&path, "0123456789abcdef0123").unwrap();
        bound_persistent_log(&path, 16).unwrap();
        assert!(!path.exists());
        assert_eq!(
            fs::read_to_string(rotated_log_path(&path)).unwrap(),
            "0123456789abcdef0123"
        );
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn redact_log_text_strips_secret_shaped_values() {
        let input = concat!(
            "cmd --token abcd1234 --verbose ",
            "API_KEY=sk-xyz789 PASSWORD:hunter2 ",
            "note=plain csrf_token=not-a-secret-word ",
            "curl https://user:p%40ss@example.invalid/x\n",
            "Authorization: Bearer eyJhbGciOiJ.abc.def tail",
        );
        let redacted = redact_log_text(input);
        assert!(!redacted.contains("abcd1234"));
        assert!(!redacted.contains("sk-xyz789"));
        assert!(!redacted.contains("hunter2"));
        assert!(!redacted.contains("p%40ss"));
        assert!(!redacted.contains("eyJhbGciOiJ"));
        assert!(redacted.contains("note=plain"));
        assert!(redacted.contains("[REDACTED]"));
        assert!(redacted.contains("https://[REDACTED]@example.invalid/x"));
        // A header value runs to the end of its line.
        assert!(redacted.ends_with("Authorization: [REDACTED]"));
    }

    #[test]
    fn redact_log_text_handles_text_ending_at_separator_without_panic() {
        // Regression: an eager `then_some(bytes[cursor])` indexed past the
        // end when the text terminated exactly at a separator.
        for input in [
            "x token=",
            "api_key=",
            "Bearer ",
            "password:",
            "--token ",
            "secret='",
            "{\"api_key\":\"",
        ] {
            let redacted = redact_log_text(input);
            assert_eq!(redacted, input, "input {input:?} must not panic or mutate");
        }
    }

    #[test]
    fn redact_log_text_redacts_compound_identifier_keys() {
        let input = concat!(
            "access_token=at-001 refresh_token: rt-002 ",
            "github_token=ghp_003 aws_secret_access_key=AKIA004 ",
            "openai_api_key=sk-005",
        );
        let redacted = redact_log_text(input);
        for leaked in ["at-001", "rt-002", "ghp_003", "AKIA004", "sk-005"] {
            assert!(!redacted.contains(leaked), "leaked {leaked}: {redacted}");
        }
        assert!(redacted.contains("access_token=[REDACTED]"));
        assert!(redacted.contains("refresh_token: [REDACTED]"));
    }

    #[test]
    fn redact_log_text_redacts_hyphen_and_quoted_key_shapes() {
        let input = concat!(
            "--api-key flagval1 api-key: headerval ",
            "x-api-key: xval {\"api_key\":\"jsonval\"} ",
            "{'password':'dictval'}",
        );
        let redacted = redact_log_text(input);
        for leaked in ["flagval1", "headerval", "xval", "jsonval", "dictval"] {
            assert!(!redacted.contains(leaked), "leaked {leaked}: {redacted}");
        }
        assert!(redacted.contains("{\"api_key\":\"[REDACTED]\"}"));
        assert!(redacted.contains("{'password':'[REDACTED]'}"));
    }

    #[test]
    fn redact_log_text_leaves_bare_bearer_prose_alone() {
        let prose = "the bearer of the token is nobody";
        assert_eq!(redact_log_text(prose), prose);
        // A token-shaped bearer value is still redacted.
        let redacted = redact_log_text("Bearer eyJhbGciOiJ9.abc_def-123 rest");
        assert!(redacted.contains("Bearer [REDACTED]"));
        assert!(redacted.ends_with(" rest"));
    }

    #[test]
    fn redact_log_text_keeps_word_boundary_and_plain_values() {
        let redacted = redact_log_text("tokenizer=keep tokenize=x value=keep");
        // `tokenizer`/`tokenize` are not whole-word secret keys.
        assert!(redacted.contains("tokenizer=keep"));
        assert!(redacted.contains("tokenize=x"));
        assert!(redacted.contains("value=keep"));
    }

    #[cfg(unix)]
    #[test]
    fn bound_persistent_log_propagates_metadata_errors() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!(
            "tobkiri-bound-log-deny-{}-{}",
            std::process::id(),
            unix_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o000)).unwrap();
        let result = bound_persistent_log(&dir.join("diag.log"), 16);
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        fs::remove_dir_all(&dir).ok();
        assert!(result.is_err(), "metadata failure must fail closed");
    }

    #[cfg(unix)]
    #[test]
    fn bounded_log_drain_consumes_full_stream_without_wedging_child() {
        // A child emitting far more than the ~64KiB pipe buffer must still
        // terminate: the drain keeps reading even after the persistence
        // budget is spent.
        let dir = std::env::temp_dir().join(format!(
            "tobkiri-drain-{}-{}",
            std::process::id(),
            unix_nanos()
        ));
        let path = dir.join("child.log");
        let log = open_child_log(&path, 128).unwrap();
        let mut child = std::process::Command::new("/bin/sh")
            .arg("-c")
            .arg("head -c 200000 /dev/zero | tr '\\0' 'x'; printf 'secret=tok999'")
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let pid = child.id();
        let handle = spawn_bounded_log_drain(
            child.stdout.take().unwrap(),
            Some(log),
            pid,
            "stdout",
            "Test",
            false,
        );
        handle.join().expect("drain thread must finish");
        assert!(child.wait().unwrap().success(), "child must not wedge");

        let contents = fs::read_to_string(&path).unwrap();
        assert!(contents.contains("truncated"));
        // Persisted bytes were redacted before hitting the file.
        assert!(!contents.contains("tok999"));
        fs::remove_dir_all(&dir).ok();
    }

    fn unix_nanos() -> u128 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    }
}
