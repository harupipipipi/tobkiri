use async_trait::async_trait;
use std::{
    io::{IsTerminal, Write},
    path::PathBuf,
    sync::Arc,
};
use tobkiri_core::{
    AllocationBudget, AllocationError, CanonValue, CoreBuffer, CoreValue, ids::UiSessionId,
};
use tobkiri_core::{ApprovalAnswer, ApprovalPrompt, HostError, HostIo, TrustedPrompt, UiEmbed};

#[path = "terminal_console.rs"]
mod terminal_console;
use terminal_console::ConsoleOutput;

/// The approval surface belongs to the native terminal host. Piped input and
/// --non-interactive never count as a human approval, and there is no yes flag.
pub struct TerminalHost {
    non_interactive: bool,
    input: tokio::sync::OnceCell<Input>,
    turn: Arc<tokio::sync::Mutex<()>>,
    output: ConsoleOutput,
}
impl TerminalHost {
    pub fn new(non_interactive: bool) -> Result<Self, HostError> {
        Ok(Self {
            non_interactive,
            input: tokio::sync::OnceCell::new(),
            turn: Arc::new(tokio::sync::Mutex::new(())),
            output: ConsoleOutput::Stdout,
        })
    }
    /// Typed native clients reserve stdout for Core return values and frames.
    /// Console text remains available on stderr under the same approval lock.
    pub(crate) fn with_machine_output(mut self) -> Self {
        self.output = ConsoleOutput::Stderr;
        self
    }
    fn write_prompt(&self, prompt: &str) -> Result<(), HostError> {
        self.output
            .write(|out| write_text(out, prompt).and_then(|()| out.flush()))
            .map_err(io_error)
    }
    async fn read_line(&self, turn: tokio::sync::OwnedMutexGuard<()>) -> Result<String, HostError> {
        let input = self
            .input
            .get_or_try_init(|| async { Input::new() })
            .await?;
        #[cfg(windows)]
        {
            input.line(Some(turn)).await
        }
        #[cfg(not(windows))]
        {
            let _turn = turn;
            input.line().await
        }
    }
    fn trusted_tty(&self) -> bool {
        !self.non_interactive && std::io::stdin().is_terminal() && std::io::stderr().is_terminal()
    }
    /// Native User command metadata shares the physical reader and terminal
    /// turn with trusted approval. It never reads ahead into an approval line.
    pub(crate) async fn session_request_line(
        &self,
        maximum: usize,
        cancel: tokio_util::sync::CancellationToken,
    ) -> Result<Option<Vec<u8>>, HostError> {
        if maximum == 0 || maximum > 64 * 1024 || cancel.is_cancelled() {
            return Err(HostError("invalid or cancelled session input bound".into()));
        }
        let turn = tokio::select! {
            held = self.turn.clone().lock_owned() => held,
            _ = cancel.cancelled() => return Err(HostError("session input cancelled".into())),
        };
        let input = self
            .input
            .get_or_try_init(|| async { Input::new() })
            .await?;
        #[cfg(windows)]
        {
            input.metadata_line(maximum, Some(turn), cancel).await
        }
        #[cfg(not(windows))]
        {
            let _turn = turn;
            input.metadata_line(maximum, cancel).await
        }
    }
}

#[async_trait]
impl TrustedPrompt for TerminalHost {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        if !self.trusted_tty() {
            return ApprovalAnswer::Unavailable;
        }
        // A guest terminal read cannot consume the trusted approval response,
        // and guest output cannot interleave counterfeit review lines.
        let turn = self.turn.clone().lock_owned().await;
        let rendered = match render_approval(&prompt) {
            Ok(rendered) => rendered,
            Err(_) => return ApprovalAnswer::Unavailable,
        };
        let written = {
            let mut stderr = std::io::stderr().lock();
            stderr
                .write_all(rendered.as_bytes())
                .and_then(|_| stderr.flush())
        };
        if written.is_err() {
            return ApprovalAnswer::Unavailable;
        }
        match self.read_line(turn).await {
            Ok(answer) if answer.trim() == "approve" => ApprovalAnswer::Approve,
            Ok(_) => ApprovalAnswer::Deny,
            Err(_) => ApprovalAnswer::Unavailable,
        }
    }
}

fn render_approval(prompt: &ApprovalPrompt) -> Result<String, HostError> {
    let dangerous = prompt.risk == tobkiri_core::Risk::Dangerous;
    let title = if dangerous {
        "Tobkiri DANGEROUS approval"
    } else {
        "Tobkiri approval"
    };
    let warning = if dangerous {
        "DANGER: This request may run unsandboxed code or access user-global data. Review the effect before approving.\n"
    } else {
        ""
    };
    let principal = serde_json::to_string(&prompt.principal)
        .map_err(|_| HostError("approval principal cannot be rendered".into()))?;
    let summary = serde_json::to_string(&prompt.summary)
        .map_err(|_| HostError("approval summary cannot be rendered".into()))?;
    Ok(format!(
        "\n{title}\n{warning}Profile: {}\nPrincipal: {principal}\nRisk: {:?}\nReservation: {}\nRequest: {}\nSecurity epoch: {}\nArguments digest: {}\nEffect:\n{}\nSummary: {summary}\nType approve to approve this request; anything else denies it: ",
        prompt.profile,
        prompt.risk,
        prompt.reservation,
        prompt.request,
        prompt.security_epoch,
        prompt.args_digest,
        effect_preview(&prompt.effect)?
    ))
}
fn effect_preview(effect: &tobkiri_core::EffectRequest) -> Result<String, HostError> {
    let mut value = serde_json::to_value(effect)
        .map_err(|_| HostError("approval effect cannot be rendered".into()))?;
    // The exact bound URL remains in the grant's digest, while query/path data
    // may contain credentials. Method and approved origin remain reviewable.
    if matches!(effect, tobkiri_core::EffectRequest::Network { .. }) {
        value
            .as_object_mut()
            .ok_or_else(|| HostError("invalid effect review".into()))?
            .insert(
                "url".into(),
                serde_json::Value::String("[redacted URL; review method and origin]".into()),
            );
    }
    // JSON escaping prevents guest-controlled names/paths from injecting
    // terminal controls or counterfeit approval lines. No argument plaintext.
    serde_json::to_string_pretty(&value)
        .map_err(|_| HostError("approval effect cannot be rendered".into()))
}

#[async_trait]
impl HostIo for TerminalHost {
    fn display(&self, surface: &str, content: &CanonValue) {
        let Ok(_turn) = self.turn.try_lock() else {
            return;
        };
        if let Ok(bytes) = tobkiri_core::encode(content, tobkiri_core::Variant::StrictV1) {
            let _ = self.output.write(|out| {
                write_text(out, surface)?;
                out.write_all(b": ")?;
                out.write_all(&bytes)?;
                out.write_all(b"\n")?;
                out.flush()
            });
        }
    }
    async fn terminal_read_line(&self, prompt: &str) -> Result<String, HostError> {
        let turn = self.turn.clone().lock_owned().await;
        self.write_prompt(prompt)?;
        self.read_line(turn).await
    }
    fn terminal_write(&self, text: &str) {
        let Ok(_turn) = self.turn.try_lock() else {
            return;
        };
        let _ = self
            .output
            .write(|out| write_text(out, text).and_then(|()| out.flush()));
    }
    fn display_budgeted(&self, surface: &str, content: &CoreBuffer) -> Result<(), HostError> {
        let _turn = self
            .turn
            .try_lock()
            .map_err(|_| HostError("trusted terminal review is active".into()))?;
        if surface.len() > 4096 || content.len() > 1024 * 1024 {
            return Err(HostError("bounded display limit exceeded".into()));
        }
        std::str::from_utf8(content.as_bytes())
            .map_err(|_| HostError("display buffer is not UTF-8".into()))?;
        self.output
            .write(|out| {
                write_text(out, surface)?;
                out.write_all(b": ")?;
                out.write_all(content.as_bytes())?;
                out.write_all(b"\n")?;
                out.flush()
            })
            .map_err(io_error)
    }
    async fn terminal_read_line_budgeted(
        &self,
        prompt: &str,
        max_bytes: usize,
        budget: Arc<dyn AllocationBudget>,
    ) -> Result<CoreBuffer, HostError> {
        let turn = self.turn.clone().lock_owned().await;
        self.write_prompt(prompt)?;
        let input = self
            .input
            .get_or_try_init(|| async { Input::new() })
            .await?;
        #[cfg(windows)]
        {
            input.line_budgeted(max_bytes, &budget, Some(turn)).await
        }
        #[cfg(not(windows))]
        {
            let _turn = turn;
            input.line_budgeted(max_bytes, &budget).await
        }
    }
    fn terminal_write_budgeted(&self, text: &CoreBuffer) -> Result<(), HostError> {
        let _turn = self
            .turn
            .try_lock()
            .map_err(|_| HostError("trusted terminal review is active".into()))?;
        let text = text.as_str().map_err(allocation_error)?;
        self.output
            .write(|out| write_text(out, text).and_then(|()| out.flush()))
            .map_err(io_error)
    }
    async fn pick_directory_budgeted(
        &self,
        purpose: &str,
        max_bytes: usize,
        budget: Arc<dyn AllocationBudget>,
    ) -> Result<Option<CoreBuffer>, HostError> {
        if !self.trusted_tty() {
            return Err(HostError("trusted directory picker is unavailable".into()));
        }
        let value = self
            .terminal_read_line_budgeted(purpose, max_bytes, budget)
            .await?;
        let path = value.as_str().map_err(allocation_error)?;
        if path.is_empty() {
            return Ok(None);
        }
        if !std::path::Path::new(path).is_dir() {
            return Err(HostError("selected directory is unavailable".into()));
        }
        Ok(Some(value))
    }
    fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, HostError> {
        Err(HostError(
            "UI embedding is unavailable in the terminal host".into(),
        ))
    }
    async fn pick_directory(&self, purpose: &str) -> Option<PathBuf> {
        if !self.trusted_tty() {
            return None;
        }
        let prompt = format!("{purpose}; enter a directory path or leave blank to cancel: ");
        self.terminal_read_line(&prompt)
            .await
            .ok()
            .filter(|line| !line.trim().is_empty())
            .map(|line| PathBuf::from(line.trim()))
            .filter(|path| path.is_dir())
    }
}

#[cfg(unix)]
struct Input {
    fd: tokio::io::unix::AsyncFd<std::os::fd::OwnedFd>,
    buffer: tokio::sync::Mutex<Vec<u8>>,
    original_flags: i32,
}
#[cfg(unix)]
impl Input {
    fn new() -> Result<Self, HostError> {
        use std::os::fd::FromRawFd;
        // SAFETY: dup returns a fresh owned descriptor; fcntl changes only its
        // nonblocking flag, which is restored when the terminal host is dropped.
        let raw = unsafe { libc::dup(libc::STDIN_FILENO) };
        if raw < 0 {
            return Err(HostError(std::io::Error::last_os_error().to_string()));
        }
        let fd = unsafe { std::os::fd::OwnedFd::from_raw_fd(raw) };
        Self::owned(fd, true)
    }
    fn owned(fd: std::os::fd::OwnedFd, restore_stdin_on_error: bool) -> Result<Self, HostError> {
        use std::os::fd::AsRawFd;
        let original_flags = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_GETFL) };
        if original_flags < 0
            || unsafe {
                libc::fcntl(
                    fd.as_raw_fd(),
                    libc::F_SETFL,
                    original_flags | libc::O_NONBLOCK,
                )
            } < 0
        {
            return Err(HostError(std::io::Error::last_os_error().to_string()));
        }
        let fd = match unsafe { tokio::io::unix::AsyncFd::register(fd) } {
            Ok(fd) => fd,
            Err(error) => {
                // AsyncFd registration consumes the owned descriptor on error.
                // The stdin constructor's shared open-file-description flags
                // are restored there; test/private pipes have no shared stdin.
                if restore_stdin_on_error {
                    unsafe { libc::fcntl(libc::STDIN_FILENO, libc::F_SETFL, original_flags) };
                }
                return Err(HostError(error.to_string()));
            }
        };
        Ok(Self {
            fd,
            buffer: tokio::sync::Mutex::new(Vec::new()),
            original_flags,
        })
    }
    async fn line(&self) -> Result<String, HostError> {
        use std::os::fd::AsRawFd;
        let mut buffer = self.buffer.lock().await;
        loop {
            if let Some(end) = buffer.iter().position(|b| *b == b'\n') {
                let mut line = buffer.drain(..=end).collect::<Vec<_>>();
                line.pop();
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                return String::from_utf8(line)
                    .map_err(|_| HostError("terminal input is not UTF-8".into()));
            }
            if buffer.len() > 1024 * 1024 {
                return Err(HostError("terminal line exceeds the input limit".into()));
            }
            let mut readiness = self
                .fd
                .readable()
                .await
                .map_err(|e| HostError(e.to_string()))?;
            let read = readiness.try_io(|fd| {
                // Human metadata only: do not read ahead into a future guest
                // line outside its established allocation account.
                let mut bytes = [0u8; 1];
                let n = unsafe {
                    libc::read(
                        fd.get_ref().as_raw_fd(),
                        bytes.as_mut_ptr().cast(),
                        bytes.len(),
                    )
                };
                if n < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(bytes[..n as usize].to_vec())
            });
            match read {
                Ok(Ok(bytes)) if bytes.is_empty() => {
                    if buffer.is_empty() {
                        return Err(HostError("end of input".into()));
                    }
                    return String::from_utf8(std::mem::take(&mut *buffer))
                        .map_err(|_| HostError("terminal input is not UTF-8".into()));
                }
                Ok(Ok(bytes)) => buffer.extend(bytes),
                Ok(Err(error)) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Ok(Err(error)) => return Err(HostError(error.to_string())),
                Err(_) => continue,
            }
        }
    }
    async fn metadata_line(
        &self,
        maximum: usize,
        cancel: tokio_util::sync::CancellationToken,
    ) -> Result<Option<Vec<u8>>, HostError> {
        use std::os::fd::AsRawFd;
        let mut buffer = self.buffer.lock().await;
        if !buffer.is_empty() {
            return Err(HostError("terminal metadata has an incomplete line".into()));
        }
        let capacity = maximum
            .checked_add(1)
            .ok_or_else(|| HostError("session metadata bound overflow".into()))?;
        buffer
            .try_reserve_exact(capacity)
            .map_err(|_| HostError("session metadata allocation failed".into()))?;
        loop {
            let mut readiness = tokio::select! {
                _ = cancel.cancelled() => return Err(HostError("session input cancelled".into())),
                ready = self.fd.readable() => ready.map_err(io_error)?,
            };
            let read = readiness.try_io(|fd| {
                let mut byte = 0u8;
                let count = unsafe {
                    libc::read(fd.get_ref().as_raw_fd(), (&mut byte as *mut u8).cast(), 1)
                };
                if count < 0 {
                    Err(std::io::Error::last_os_error())
                } else {
                    Ok((count, byte))
                }
            });
            match read {
                Ok(Ok((0, _))) if buffer.is_empty() => return Ok(None),
                Ok(Ok((0, _))) => {
                    return Err(HostError("session request ended before newline".into()));
                }
                Ok(Ok((_, b'\n'))) => {
                    if buffer.last() == Some(&b'\r') {
                        buffer.pop();
                    }
                    return Ok(Some(std::mem::take(&mut *buffer)));
                }
                Ok(Ok((_, byte))) => {
                    if buffer.len() >= maximum && !(buffer.len() == maximum && byte == b'\r') {
                        return Err(HostError("session request exceeds its input bound".into()));
                    }
                    buffer.push(byte);
                }
                Ok(Err(error)) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Ok(Err(error)) => return Err(io_error(error)),
                Err(_) => continue,
            }
        }
    }
    async fn line_budgeted(
        &self,
        maximum: usize,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<CoreBuffer, HostError> {
        use std::os::fd::AsRawFd;
        let metadata = self.buffer.lock().await;
        if !metadata.is_empty() {
            return Err(HostError("trusted terminal input is pending".into()));
        }
        if maximum == 0 {
            return Err(HostError("terminal input limit is zero".into()));
        }
        let reserved_max = maximum
            .checked_add(1)
            .ok_or_else(|| HostError("terminal input limit overflow".into()))?;
        let mut builder = CoreBuffer::builder(budget, reserved_max);
        loop {
            let mut readiness = self.fd.readable().await.map_err(io_error)?;
            let read = readiness.try_io(|fd| {
                let mut io_failure = None;
                let mut end = false;
                let mut eof = false;
                let length = builder.len();
                // Charge the append storage and growth peak before entering the
                // syscall. The callback sees a fixed borrowed one-byte tail.
                let result = builder.write_chunk(1, |tail| {
                    let count = unsafe {
                        libc::read(fd.get_ref().as_raw_fd(), tail.as_mut_ptr().cast(), 1)
                    };
                    if count < 0 {
                        io_failure = Some(std::io::Error::last_os_error());
                        return Err(AllocationError::Unavailable);
                    }
                    if count == 0 {
                        eof = true;
                        return Ok(0);
                    }
                    if tail[0] == b'\n' {
                        end = true;
                        return Ok(0);
                    }
                    if length >= maximum {
                        return Err(AllocationError::Limit);
                    }
                    Ok(1)
                });
                match io_failure {
                    Some(error) => Err(error),
                    None => Ok((result, end, eof)),
                }
            });
            match read {
                Ok(Ok((result, end, eof))) => {
                    result.map_err(allocation_error)?;
                    if end || eof {
                        if eof && builder.is_empty() {
                            return Err(HostError("end of input".into()));
                        }
                        let value = builder.finish().map_err(allocation_error)?;
                        value.as_str().map_err(allocation_error)?;
                        return Ok(value);
                    }
                }
                Ok(Err(error)) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Ok(Err(error)) => return Err(io_error(error)),
                Err(_) => continue,
            }
        }
    }
}
#[cfg(unix)]
impl Drop for Input {
    fn drop(&mut self) {
        use std::os::fd::AsRawFd;
        unsafe {
            libc::fcntl(
                self.fd.get_ref().as_raw_fd(),
                libc::F_SETFL,
                self.original_flags,
            )
        };
    }
}

#[cfg(windows)]
#[path = "terminal_windows.rs"]
mod windows;
#[cfg(windows)]
use windows::Input;

#[cfg(not(any(unix, windows)))]
struct Input;
#[cfg(not(any(unix, windows)))]
impl Input {
    fn new() -> Result<Self, HostError> {
        Ok(Self)
    }
    async fn line(&self) -> Result<String, HostError> {
        Err(HostError(
            "cancellable terminal input is unavailable on this platform".into(),
        ))
    }
    async fn metadata_line(
        &self,
        _: usize,
        _: tokio_util::sync::CancellationToken,
    ) -> Result<Option<Vec<u8>>, HostError> {
        Err(HostError(
            "cancellable session input is unavailable on this platform".into(),
        ))
    }
    async fn line_budgeted(
        &self,
        _: usize,
        _: &Arc<dyn AllocationBudget>,
    ) -> Result<CoreBuffer, HostError> {
        Err(HostError(
            "budgeted cancellable terminal input is unavailable on this platform".into(),
        ))
    }
}

fn allocation_error(_: AllocationError) -> HostError {
    HostError("terminal value allocation is unavailable or exceeds its approved limit".into())
}
fn io_error(error: std::io::Error) -> HostError {
    HostError(error.to_string())
}

/// Borrowed output only; terminal controls cannot forge the trusted review UI.
fn write_text(out: &mut (impl Write + ?Sized), text: &str) -> std::io::Result<()> {
    for ch in text.chars() {
        if ch.is_control() && !matches!(ch, '\n' | '\r' | '\t') {
            write!(out, "\\u{{{:x}}}", ch as u32)?;
        } else {
            out.write_all(ch.encode_utf8(&mut [0; 4]).as_bytes())?;
        }
    }
    Ok(())
}
fn write_json_string(out: &mut impl Write, text: &str) -> std::io::Result<()> {
    out.write_all(b"\"")?;
    for ch in text.chars() {
        match ch {
            '"' => out.write_all(b"\\\"")?,
            '\\' => out.write_all(b"\\\\")?,
            ch if ch.is_control() => write!(out, "\\u{:04x}", ch as u32)?,
            ch => out.write_all(ch.encode_utf8(&mut [0; 4]).as_bytes())?,
        }
    }
    out.write_all(b"\"")
}
fn write_value(out: &mut impl Write, value: &CoreValue, depth: usize) -> std::io::Result<()> {
    use tobkiri_core::CoreRef;
    if depth > 256 {
        return Err(std::io::Error::other("terminal graph depth exceeded"));
    }
    match value.inspect() {
        CoreRef::Null => out.write_all(b"null"),
        CoreRef::Bool(value) => out.write_all(if value { b"true" } else { b"false" }),
        CoreRef::Int(value) => write!(out, "{value}"),
        CoreRef::BigInt(_) | CoreRef::Float(_) => Err(std::io::Error::other(
            "terminal legacy scalar is unavailable",
        )),
        CoreRef::Str(value) => write_json_string(out, value),
        CoreRef::Arr(values) => {
            out.write_all(b"[")?;
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    out.write_all(b",")?;
                }
                write_value(out, &value, depth + 1)?;
            }
            out.write_all(b"]")
        }
        CoreRef::Obj(fields) => {
            out.write_all(b"{")?;
            for (index, (key, value)) in fields.iter().enumerate() {
                if index > 0 {
                    out.write_all(b",")?;
                }
                write_json_string(out, key)?;
                out.write_all(b":")?;
                write_value(out, &value, depth + 1)?;
            }
            out.write_all(b"}")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Mutex,
        atomic::{AtomicUsize, Ordering},
    };
    #[derive(Default)]
    struct Budget {
        state: Mutex<(u64, u64)>,
        calls: AtomicUsize,
    }
    impl Budget {
        fn set_limit(&self, limit: u64) {
            self.state.lock().unwrap().1 = limit;
        }
        fn used(&self) -> u64 {
            self.state.lock().unwrap().0
        }
    }
    impl AllocationBudget for Budget {
        fn reserve(&self, request: tobkiri_core::AllocationRequest) -> Result<(), AllocationError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let mut state = self.state.lock().unwrap();
            let total = state
                .0
                .checked_add(request.bytes)
                .ok_or(AllocationError::Overflow)?;
            if total > state.1 {
                return Err(AllocationError::Limit);
            }
            state.0 = total;
            Ok(())
        }
        fn release(&self, bytes: u64) {
            let mut state = self.state.lock().unwrap();
            state.0 -= bytes;
        }
    }
    #[test]
    fn console_output_subprocess_fixture() {
        let Ok(mode) = std::env::var("TOBKIRI_CONSOLE_OUTPUT_TEST_MODE") else {
            return;
        };
        let host = TerminalHost::new(true).unwrap();
        let host = match mode.as_str() {
            "machine" => host.with_machine_output(),
            "human" => host,
            _ => panic!("invalid fixture mode"),
        };
        let budget = Arc::new(Budget::default());
        budget.set_limit(8192);
        let funding: Arc<dyn AllocationBudget> = budget;
        host.terminal_write("{\"kind\":\"session_finished\",\"ok\":true,\"guest\":true}\n");
        host.display("legacy-surface", &CanonValue::Str("legacy-value".into()));
        let message =
            CoreBuffer::copy_from(b"{\"kind\":\"value\",\"guest\":true}\n", &funding).unwrap();
        host.terminal_write_budgeted(&message).unwrap();
        let value = CoreBuffer::copy_from(b"\"budget-value\"", &funding).unwrap();
        host.display_budgeted("budget-surface", &value).unwrap();
        host.write_prompt("guest-prompt\u{1b}[2J\n").unwrap();
        {
            let _review = host.turn.try_lock().unwrap();
            host.terminal_write("guest-while-review\n");
            host.display("guest-while-review", &CanonValue::Null);
            assert!(host.terminal_write_budgeted(&message).is_err());
            assert!(host.display_budgeted("guest-while-review", &value).is_err());
        }
        let mut output = std::io::stdout().lock();
        output
            .write_all(b"{\"kind\":\"native-fixture-frame\"}\n")
            .unwrap();
        output.flush().unwrap();
    }
    #[test]
    fn machine_console_cannot_inject_reply_frames_into_stdout() {
        for mode in ["machine", "human"] {
            let captured = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "terminal::tests::console_output_subprocess_fixture",
                    "--nocapture",
                ])
                .env("TOBKIRI_CONSOLE_OUTPUT_TEST_MODE", mode)
                .stdin(std::process::Stdio::null())
                .output()
                .unwrap();
            assert!(
                captured.status.success(),
                "{}",
                String::from_utf8_lossy(&captured.stderr)
            );
            let stdout = String::from_utf8(captured.stdout).unwrap();
            let stderr = String::from_utf8(captured.stderr).unwrap();
            let (console, other) = if mode == "machine" {
                (&stderr, &stdout)
            } else {
                (&stdout, &stderr)
            };
            for text in [
                "{\"kind\":\"session_finished\",\"ok\":true,\"guest\":true}",
                "{\"kind\":\"value\",\"guest\":true}",
                "legacy-surface: \"legacy-value\"",
                "budget-surface: \"budget-value\"",
                "guest-prompt\\u{1b}[2J",
            ] {
                assert!(
                    console.contains(text),
                    "missing {text} in {mode}: {console}"
                );
                assert!(
                    !other.contains(text),
                    "console polluted other channel in {mode}: {other}"
                );
            }
            assert!(stdout.contains("{\"kind\":\"native-fixture-frame\"}"));
            assert!(!stderr.contains("native-fixture-frame"));
            assert!(
                !stdout.contains("guest-while-review") && !stderr.contains("guest-while-review")
            );
            assert!(!stdout.contains('\u{1b}') && !stderr.contains('\u{1b}'));
        }
    }
    #[cfg(unix)]
    fn pipe_input(bytes: &[u8]) -> (Input, std::os::fd::OwnedFd) {
        use std::os::fd::FromRawFd;
        let mut pair = [-1; 2];
        assert_eq!(unsafe { libc::pipe(pair.as_mut_ptr()) }, 0);
        let read = unsafe { std::os::fd::OwnedFd::from_raw_fd(pair[0]) };
        let write = unsafe { std::os::fd::OwnedFd::from_raw_fd(pair[1]) };
        assert_eq!(
            unsafe { libc::write(pair[1], bytes.as_ptr().cast(), bytes.len()) },
            bytes.len() as isize
        );
        (Input::owned(read, false).unwrap(), write)
    }
    #[tokio::test]
    async fn noninteractive_terminal_cannot_approve() {
        let host = TerminalHost::new(true).unwrap();
        assert!(!host.trusted_tty());
    }
    #[tokio::test]
    async fn session_command_cannot_compete_with_a_trusted_review_read() {
        let host = TerminalHost::new(true).unwrap();
        let _review = host.turn.lock().await;
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_millis(20),
                host.session_request_line(32, tokio_util::sync::CancellationToken::new())
            )
            .await
            .is_err()
        );
        assert!(host.input.get().is_none());
    }
    #[cfg(unix)]
    #[tokio::test]
    async fn session_metadata_is_bounded_without_reading_the_next_approval() {
        let (input, writer) = pipe_input(b"cmd\r\napprove\n");
        assert_eq!(
            input
                .metadata_line(3, tokio_util::sync::CancellationToken::new())
                .await
                .unwrap(),
            Some(b"cmd".to_vec())
        );
        assert_eq!(input.line().await.unwrap(), "approve");
        drop(writer);
        assert!(
            input
                .metadata_line(3, tokio_util::sync::CancellationToken::new())
                .await
                .unwrap()
                .is_none()
        );
    }
    #[cfg(unix)]
    #[tokio::test]
    async fn session_metadata_rejects_overflow_and_unterminated_eof() {
        let (overflow, _writer) = pipe_input(b"long\napprove\n");
        assert!(
            overflow
                .metadata_line(2, tokio_util::sync::CancellationToken::new())
                .await
                .is_err()
        );
        let (partial, writer) = pipe_input(b"{}");
        drop(writer);
        assert!(
            partial
                .metadata_line(32, tokio_util::sync::CancellationToken::new())
                .await
                .unwrap_err()
                .0
                .contains("before newline")
        );
    }
    #[cfg(unix)]
    #[tokio::test]
    async fn allocation_denial_happens_before_read_and_next_line_is_never_read_ahead() {
        let (input, _writer) = pipe_input(b"first\nsecond\n");
        let budget = Arc::new(Budget::default());
        let funding: Arc<dyn AllocationBudget> = budget.clone();
        assert!(input.line_budgeted(5, &funding).await.is_err());
        assert_eq!(budget.used(), 0);
        budget.set_limit(4096);
        let first = input.line_budgeted(5, &funding).await.unwrap();
        assert_eq!(first.as_bytes(), b"first");
        let second = input.line_budgeted(6, &funding).await.unwrap();
        assert_eq!(second.as_bytes(), b"second");
        assert!(budget.used() > 0);
        drop((first, second));
        assert_eq!(budget.used(), 0);
    }
    #[cfg(unix)]
    #[tokio::test]
    async fn cancelled_budgeted_read_releases_partial_storage_without_background_thread() {
        let (input, _writer) = pipe_input(b"partial");
        let budget = Arc::new(Budget::default());
        budget.set_limit(4096);
        let funding: Arc<dyn AllocationBudget> = budget.clone();
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_millis(20),
                input.line_budgeted(128, &funding)
            )
            .await
            .is_err()
        );
        assert!(budget.calls.load(Ordering::SeqCst) > 0);
        assert_eq!(budget.used(), 0);
        assert!(input.buffer.lock().await.is_empty());
    }
    #[tokio::test]
    async fn trusted_review_blocks_guest_read_and_guest_output() {
        let host = TerminalHost::new(true).unwrap();
        let budget = Arc::new(Budget::default());
        budget.set_limit(4096);
        let funding: Arc<dyn AllocationBudget> = budget.clone();
        let message = CoreBuffer::copy_from(b"spoof approval", &funding).unwrap();
        let _review = host.turn.lock().await;
        assert!(host.terminal_write_budgeted(&message).is_err());
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_millis(20),
                host.terminal_read_line_budgeted("", 128, funding)
            )
            .await
            .is_err()
        );
        assert!(host.input.get().is_none());
        drop(message);
        assert_eq!(budget.used(), 0);
    }
    #[test]
    fn controlled_display_borrows_graph_and_escapes_terminal_controls() {
        let budget = Arc::new(Budget::default());
        budget.set_limit(8192);
        let funding: Arc<dyn AllocationBudget> = budget.clone();
        let value = CoreValue::try_from_borrowed(
            &CanonValue::Obj(std::collections::BTreeMap::from([(
                "text".into(),
                CanonValue::Str("\u{1b}[2J\nhello".into()),
            )])),
            &funding,
        )
        .unwrap();
        let before = budget.used();
        let mut output = Vec::new();
        write_value(&mut output, &value, 0).unwrap();
        assert_eq!(
            tobkiri_core::parse(&output, tobkiri_core::Variant::StrictV1).unwrap(),
            CanonValue::Obj(std::collections::BTreeMap::from([(
                "text".into(),
                CanonValue::Str("\u{1b}[2J\nhello".into())
            )]))
        );
        assert!(!output.contains(&0x1b));
        assert_eq!(budget.used(), before);
    }
}

#[cfg(test)]
mod approval_tests {
    use super::*;
    fn approval_prompt(effect: serde_json::Value, risk: &str) -> ApprovalPrompt {
        serde_json::from_value(serde_json::json!({"reservation":"grant_fixture","profile":"prof_fixture","principal":{"kind":"user"},"request":"req_fixture","effect":effect,"args_digest":tobkiri_core::Digest::hash(b"arguments"),"security_epoch":7,"risk":risk,"summary":"trusted review"})).unwrap()
    }
    #[test]
    fn dangerous_review_contains_warning_complete_effect_and_context() {
        let prompt = approval_prompt(
            serde_json::json!({"kind":"native","pack":"local.fixture"}),
            "dangerous",
        );
        let text = render_approval(&prompt).unwrap();
        for field in [
            "DANGEROUS approval",
            "DANGER:",
            "unsandboxed",
            "user-global",
            "local.fixture",
            "grant_fixture",
            "req_fixture",
            "Security epoch: 7",
            "Arguments digest:",
        ] {
            assert!(text.contains(field), "{field}");
        }
        let prompt = approval_prompt(
            serde_json::json!({"kind":"user-global-storage","store":"shared","operation":"write"}),
            "dangerous",
        );
        let text = render_approval(&prompt).unwrap();
        assert!(text.contains("DANGER:"));
        assert!(text.contains("shared"));
        assert!(text.contains("write"));
    }
    #[test]
    fn review_redacts_network_credentials_and_escapes_terminal_controls() {
        let mut prompt = approval_prompt(
            serde_json::json!({"kind":"network","origin":"https://example.test","url":"https://example.test/private-secret?token=secret-token","method":"POST"}),
            "normal",
        );
        prompt.summary = "\u{1b}[2J\nDANGER hidden".into();
        let text = render_approval(&prompt).unwrap();
        assert!(text.contains("https://example.test"));
        assert!(text.contains("POST"));
        assert!(!text.contains("secret-token"));
        assert!(!text.contains("private-secret"));
        assert!(!text.contains('\u{1b}'));
        assert!(text.contains("\\u001b"));
        assert!(text.contains("\\nDANGER hidden"));
    }
}
