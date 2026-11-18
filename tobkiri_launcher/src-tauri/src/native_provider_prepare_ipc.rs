//! One private, synchronous connection to the original Provider PREPARE borrow.

use std::{
    io::{self, Read, Write},
    os::{fd::AsRawFd, unix::net::UnixStream},
    sync::{Arc, Mutex, TryLockError},
    thread,
    time::{Duration, Instant},
};

use anyhow::{anyhow, bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};
use tobkiri_core::{AllocationBudget, AllocationError, CoreBuffer, NativeArtifactError};
use tobkiri_wasm::NativeDataIoError;

use crate::{
    defaultspack_manager::{ApplicationProcessManager, ProviderPrepareListener},
    host_contract::ExecutionProfileIdentity,
    native_harness_bootstrap::{
        start_provider_prepare, PendingProviderPrepare, ProviderPrepareDeadline,
    },
    shell_handoff::{process_identity, LauncherProcessIdentity},
};

const START: u8 = 1;
const CHECK: u8 = 2;
const LIVE: u8 = 3;
const READ: u8 = 4;
const PRESENT: u8 = 5;
const ABSENT: u8 = 6;
const PLAN: u8 = 7;
const FAILURE: u8 = 8;
const CANCEL: u8 = 9;
const INAPPLICABLE: u8 = 10;
const JSON_LIMIT: usize = 16 * 1024;
const DOCUMENT_LIMIT: usize = 1024 * 1024;
const FRAME_TIMEOUT: Duration = Duration::from_secs(3);
const POLL_INTERVAL: Duration = Duration::from_millis(10);
const CLEANUP_WAIT: Duration = Duration::from_secs(10);
const MAX_HORIZON: Duration = Duration::from_secs(300);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Start {
    identity: Identity,
    runtime_instance: String,
    security_epoch: u64,
    deadline_ns: u64,
    request: Request,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    profile_id: String,
    profile_revision: String,
    activation_id: String,
    plan_digest: String,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
struct Request {
    connection_name: String,
    endpoint: String,
    catalog_provider_id: Option<String>,
    display_name: Option<String>,
    capabilities: Option<Vec<String>>,
}

#[derive(Clone, Copy)]
enum Stop {
    Cancelled,
    Deadline,
    Unavailable,
    Broken,
}

impl Stop {
    fn as_error(self) -> anyhow::Error {
        let error = match self {
            Self::Cancelled => NativeDataIoError::Cancelled,
            Self::Deadline => NativeDataIoError::Deadline,
            Self::Unavailable => NativeDataIoError::Unavailable,
            Self::Broken => NativeDataIoError::Io,
        };
        NativeArtifactError::from(error).into()
    }

    fn reply(self) -> Option<&'static str> {
        match self {
            Self::Cancelled | Self::Deadline => Some("cancelled"),
            Self::Unavailable => Some("unavailable"),
            Self::Broken => None,
        }
    }
}

/// Handle one accepted macOS connection for the currently managed Host process.
pub(crate) fn handle_connection(app: &AppHandle, mut stream: UnixStream) -> Result<()> {
    let peer = authenticate_peer(&stream)?;
    suppress_sigpipe(&stream)?;
    stream.set_nonblocking(true)?;
    let start_deadline = Instant::now() + FRAME_TIMEOUT;
    let (tag, size) = read_header(&mut stream, start_deadline)?;
    ensure!(
        tag == START && (1..=JSON_LIMIT).contains(&size),
        "invalid Provider prepare start frame"
    );
    let mut bytes = vec![0; size];
    read_exact(&mut stream, &mut bytes, start_deadline)?;
    let start: Start =
        serde_json::from_slice(&bytes).map_err(|_| anyhow!("invalid Provider prepare start"))?;
    drop(bytes);
    ensure!(
        start.runtime_instance.len() == 32
            && start
                .runtime_instance
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f')),
        "invalid Provider prepare runtime instance"
    );
    ensure!(start.security_epoch != 0, "invalid Provider prepare epoch");
    let deadline = host_deadline(start.deadline_ns)?;
    let identity = ExecutionProfileIdentity::new(
        start.identity.profile_id,
        start.identity.profile_revision,
        start.identity.activation_id,
        start.identity.plan_digest,
    )?;
    let request = serde_json::to_vec(&start.request)?;
    ensure!(
        request.len() <= JSON_LIMIT,
        "Provider prepare request exceeds its envelope"
    );
    let manager = app
        .try_state::<Arc<ApplicationProcessManager>>()
        .context("native Provider preparation Application manager is unavailable")?
        .inner()
        .clone();
    let Some(candidate) = manager.admitted_provider_prepare(&identity)? else {
        return send_frame(
            &mut stream,
            INAPPLICABLE,
            &[],
            deadline.min(Instant::now() + FRAME_TIMEOUT),
        );
    };
    manager.require_provider_prepare_peer(&identity, &peer)?;
    ensure!(
        candidate.matches_process(&peer),
        "Provider prepare peer differs from the admitted launch"
    );
    if candidate.verified_source().security_epoch != start.security_epoch {
        return send_frame(
            &mut stream,
            FAILURE,
            b"unavailable",
            deadline.min(Instant::now() + FRAME_TIMEOUT),
        );
    }
    manager.require_provider_prepare_admission(&candidate)?;
    if !candidate.matches_runtime_instance(&start.runtime_instance) {
        return send_frame(
            &mut stream,
            INAPPLICABLE,
            &[],
            deadline.min(Instant::now() + FRAME_TIMEOUT),
        );
    }

    let deadline = Arc::new(ProviderPrepareDeadline::new(deadline));
    let connection = Arc::new(Connection {
        manager,
        candidate: Arc::clone(&candidate),
        wire: Mutex::new(Wire { stream, stop: None }),
        deadline: Arc::clone(&deadline),
    });
    let check_connection = Arc::clone(&connection);
    let read_connection = Arc::clone(&connection);
    let pending = match start_provider_prepare(
        app,
        candidate,
        request,
        deadline,
        Box::new(move |budget| read_connection.read_registry(budget)),
        Arc::new(move || check_connection.check_current()),
    ) {
        Ok(pending) => pending,
        Err(error) => {
            return connection.send_failure(failure_code(&error), connection.deadline.current())
        }
    };
    connection.finish(pending)
}

struct Connection {
    manager: Arc<ApplicationProcessManager>,
    candidate: Arc<ProviderPrepareListener>,
    // The worker's synchronous borrow and the idle actor share one reader.
    wire: Mutex<Wire>,
    deadline: Arc<ProviderPrepareDeadline>,
}

struct Wire {
    stream: UnixStream,
    stop: Option<Stop>,
}

impl Connection {
    fn checkpoint(&self, wire: &mut Wire) -> Result<()> {
        if let Some(stop) = wire.stop {
            return Err(stop.as_error());
        }
        if Instant::now() >= self.deadline.current() {
            wire.stop = Some(Stop::Deadline);
            return Err(Stop::Deadline.as_error());
        }
        if self
            .manager
            .require_provider_prepare_admission(&self.candidate)
            .is_err()
        {
            wire.stop = Some(Stop::Unavailable);
            return Err(Stop::Unavailable.as_error());
        }
        Ok(())
    }

    fn exchange<T>(
        &self,
        tag: u8,
        receive: impl FnOnce(&mut UnixStream, (u8, usize), Instant) -> Result<T>,
    ) -> Result<T> {
        let mut wire = self.lock_wire()?;
        self.checkpoint(&mut wire)?;
        let deadline = self.deadline.current().min(Instant::now() + FRAME_TIMEOUT);
        let result = (|| {
            wire.check_idle(deadline)?;
            send_frame(&mut wire.stream, tag, &[], deadline)?;
            let header = read_header(&mut wire.stream, deadline)?;
            if header == (CANCEL, 0) {
                wire.stop = Some(Stop::Cancelled);
                return Err(Stop::Cancelled.as_error());
            }
            receive(&mut wire.stream, header, deadline)
        })();
        if result.is_err() && wire.stop.is_none() {
            wire.stop = Some(if Instant::now() >= self.deadline.current() {
                Stop::Deadline
            } else {
                Stop::Broken
            });
        }
        let result = result?;
        self.checkpoint(&mut wire)?;
        Ok(result)
    }

    fn check_current(&self) -> Result<()> {
        self.exchange(CHECK, |_, header, _| {
            ensure!(header == (LIVE, 0), "invalid Provider prepare LIVE frame");
            Ok(())
        })
    }

    fn read_registry(&self, budget: &Arc<dyn AllocationBudget>) -> Result<Option<CoreBuffer>> {
        self.exchange(READ, |stream, (tag, size), deadline| match tag {
            ABSENT if size == 0 => Ok(None),
            PRESENT if size <= DOCUMENT_LIMIT => {
                // The original Core allocation owner funds the receive buffer
                // before any document bytes can cross into the Launcher.
                let mut received = Ok(());
                let buffer = CoreBuffer::write_exact(size, budget, |destination| {
                    received = read_exact(stream, destination, deadline);
                    if received.is_ok() {
                        Ok(())
                    } else {
                        Err(AllocationError::Unavailable)
                    }
                });
                received?;
                let document = buffer?;
                document.as_str()?;
                Ok(Some(document))
            }
            _ => bail!("invalid Provider prepare Registry frame"),
        })
    }

    fn lock_wire(&self) -> Result<std::sync::MutexGuard<'_, Wire>> {
        self.wire
            .lock()
            .map_err(|_| anyhow!("Provider prepare connection is unavailable"))
    }

    fn observe_stop(&self) -> Result<Option<Stop>> {
        match self.wire.try_lock() {
            Ok(mut wire) => {
                if self.checkpoint(&mut wire).is_ok() {
                    let deadline = self.deadline.current().min(Instant::now() + FRAME_TIMEOUT);
                    let _ = wire.check_idle(deadline);
                }
                Ok(wire.stop)
            }
            Err(TryLockError::WouldBlock) => {
                Ok((Instant::now() >= self.deadline.current()).then_some(Stop::Deadline))
            }
            Err(TryLockError::Poisoned(_)) => {
                bail!("Provider prepare connection is unavailable")
            }
        }
    }

    fn finish(&self, pending: PendingProviderPrepare) -> Result<()> {
        loop {
            match self.observe_stop() {
                Ok(Some(stop)) => {
                    pending.cancel()?;
                    pending.join_until(Instant::now() + CLEANUP_WAIT)?;
                    return self.send_stopped(stop);
                }
                Ok(None) => {}
                Err(error) => {
                    pending.cancel()?;
                    pending.join_until(Instant::now() + CLEANUP_WAIT)?;
                    return Err(error);
                }
            }
            if pending.try_join()? {
                break;
            }
            thread::sleep(POLL_INTERVAL);
        }

        // finish_until makes one final CHECK against the issuing Host lease.
        // Only the physically joined worker may now yield a result to send.
        let result = pending.finish_until(self.deadline.current());
        let mut wire = self.lock_wire()?;
        let deadline = self.deadline.current().min(Instant::now() + FRAME_TIMEOUT);
        let ready = self
            .checkpoint(&mut wire)
            .and_then(|()| wire.check_idle(deadline));
        if ready.is_err() {
            let stop = wire.stop.unwrap_or(Stop::Broken);
            drop(wire);
            return self.send_stopped(stop);
        }
        match result {
            Ok(plan) if plan.len() <= JSON_LIMIT => {
                send_frame(&mut wire.stream, PLAN, plan.as_bytes(), deadline)
            }
            Ok(_) => send_frame(&mut wire.stream, FAILURE, b"limit-exceeded", deadline),
            Err(error) => send_frame(
                &mut wire.stream,
                FAILURE,
                failure_code(&error).as_bytes(),
                deadline,
            ),
        }
    }

    fn send_failure(&self, code: &str, deadline: Instant) -> Result<()> {
        let mut wire = self.lock_wire()?;
        send_frame(
            &mut wire.stream,
            FAILURE,
            code.as_bytes(),
            deadline.min(Instant::now() + FRAME_TIMEOUT),
        )
    }

    fn send_stopped(&self, stop: Stop) -> Result<()> {
        if let Some(code) = stop.reply() {
            self.send_failure(code, Instant::now() + FRAME_TIMEOUT)?;
        }
        Ok(())
    }
}

impl Wire {
    fn check_idle(&mut self, deadline: Instant) -> Result<()> {
        if let Some(stop) = self.stop {
            return Err(stop.as_error());
        }
        let outcome = (|| {
            if poll(&self.stream, libc::POLLIN, 0)? {
                let header = read_header(&mut self.stream, deadline)?;
                if header == (CANCEL, 0) {
                    return Ok(Some(Stop::Cancelled));
                }
                bail!("unsolicited Provider prepare frame");
            }
            Ok(None)
        })();
        self.stop = match outcome {
            Ok(stop) => stop,
            Err(_) => Some(Stop::Broken),
        };
        match self.stop {
            Some(stop) => Err(stop.as_error()),
            None => Ok(()),
        }
    }
}

fn failure_code(error: &anyhow::Error) -> &'static str {
    match error.downcast_ref::<NativeArtifactError>() {
        Some(NativeArtifactError::ProviderRejected(error)) => error.code(),
        Some(NativeArtifactError::Execution(
            NativeDataIoError::Cancelled | NativeDataIoError::Deadline,
        )) => "cancelled",
        Some(NativeArtifactError::TooLarge) => "limit-exceeded",
        Some(NativeArtifactError::Denied) => "denied",
        _ => "unavailable",
    }
}

fn authenticate_peer(stream: &UnixStream) -> Result<LauncherProcessIdentity> {
    let descriptor = stream.as_raw_fd();
    let mut uid: libc::uid_t = 0;
    let mut gid: libc::gid_t = 0;
    // Both pointers name writable native IDs and the stream owns the descriptor.
    if unsafe { libc::getpeereid(descriptor, &mut uid, &mut gid) } != 0 {
        return Err(io::Error::last_os_error().into());
    }
    // geteuid takes no pointers and returns the Launcher's effective user ID.
    ensure!(
        uid == unsafe { libc::geteuid() },
        "Provider prepare peer has a different effective user"
    );
    let mut pid: libc::pid_t = 0;
    let expected = std::mem::size_of_val(&pid) as libc::socklen_t;
    let mut length = expected;
    // LOCAL_PEERPID writes the kernel-authenticated pid_t into the fixed buffer.
    if unsafe {
        libc::getsockopt(
            descriptor,
            libc::SOL_LOCAL,
            libc::LOCAL_PEERPID,
            (&mut pid as *mut libc::pid_t).cast(),
            &mut length,
        )
    } != 0
    {
        return Err(io::Error::last_os_error().into());
    }
    ensure!(
        length == expected && pid > 0,
        "Provider prepare peer process is unavailable"
    );
    process_identity(pid as u32)
}

fn suppress_sigpipe(stream: &UnixStream) -> io::Result<()> {
    let enabled: libc::c_int = 1;
    // The kernel copies the fixed socket option from this initialized integer.
    let result = unsafe {
        libc::setsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_NOSIGPIPE,
            (&enabled as *const libc::c_int).cast(),
            std::mem::size_of_val(&enabled) as libc::socklen_t,
        )
    };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn host_deadline(deadline_ns: u64) -> Result<Instant> {
    // Python monotonic_ns uses the same macOS uptime clock. Sample Instant first
    // to avoid accidentally extending the Host deadline by conversion latency.
    let anchor = Instant::now();
    let remaining = deadline_ns
        .checked_sub(uptime_ns()?)
        .filter(|remaining| *remaining != 0)
        .map(Duration::from_nanos)
        .filter(|duration| *duration <= MAX_HORIZON)
        .context("Provider prepare deadline is outside its envelope")?;
    Ok(anchor + remaining)
}

fn uptime_ns() -> Result<u64> {
    let mut now = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // The syscall writes a single timespec into this initialized local.
    if unsafe { libc::clock_gettime(libc::CLOCK_UPTIME_RAW, &mut now) } != 0 {
        return Err(io::Error::last_os_error().into());
    }
    Ok(u64::try_from(
        Duration::new(now.tv_sec.try_into()?, now.tv_nsec.try_into()?).as_nanos(),
    )?)
}

fn read_header(stream: &mut UnixStream, deadline: Instant) -> Result<(u8, usize)> {
    let mut header = [0u8; 5];
    read_exact(stream, &mut header, deadline)?;
    Ok((
        header[0],
        u32::from_be_bytes([header[1], header[2], header[3], header[4]]) as usize,
    ))
}

fn read_exact(stream: &mut UnixStream, mut bytes: &mut [u8], deadline: Instant) -> Result<()> {
    while !bytes.is_empty() {
        wait_ready(stream, libc::POLLIN, deadline)?;
        match stream.read(bytes) {
            Ok(0) => return Err(io::Error::from(io::ErrorKind::UnexpectedEof).into()),
            Ok(read) => bytes = &mut bytes[read..],
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
                ) => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

fn send_frame(stream: &mut UnixStream, tag: u8, bytes: &[u8], deadline: Instant) -> Result<()> {
    let size = u32::try_from(bytes.len())?;
    let [a, b, c, d] = size.to_be_bytes();
    for mut part in [&[tag, a, b, c, d][..], bytes] {
        while !part.is_empty() {
            wait_ready(stream, libc::POLLOUT, deadline)?;
            match stream.write(part) {
                Ok(0) => return Err(io::Error::from(io::ErrorKind::WriteZero).into()),
                Ok(sent) => part = &part[sent..],
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
                    ) => {}
                Err(error) => return Err(error.into()),
            }
        }
    }
    Ok(())
}

fn wait_ready(stream: &UnixStream, events: libc::c_short, deadline: Instant) -> Result<()> {
    loop {
        let duration = deadline.saturating_duration_since(Instant::now());
        if duration.is_zero() {
            return Err(io::Error::from(io::ErrorKind::TimedOut).into());
        }
        let milliseconds = duration.as_micros().div_ceil(1_000).max(1) as libc::c_int;
        match poll(stream, events, milliseconds) {
            Ok(true) if Instant::now() < deadline => return Ok(()),
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) => return Err(error.into()),
        }
    }
}

fn poll(stream: &UnixStream, events: libc::c_short, milliseconds: libc::c_int) -> io::Result<bool> {
    let mut descriptor = libc::pollfd {
        fd: stream.as_raw_fd(),
        events,
        revents: 0,
    };
    // The descriptor array comprises exactly one live borrow of this stream.
    match unsafe { libc::poll(&mut descriptor, 1, milliseconds) } {
        -1 => Err(io::Error::last_os_error()),
        ready => Ok(ready != 0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frames_preserve_utf8_payload_bytes() {
        let (mut sender, mut receiver) = UnixStream::pair().unwrap();
        sender.set_nonblocking(true).unwrap();
        receiver.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + FRAME_TIMEOUT;
        let utf8 = "{\"ä\":null}".as_bytes();
        send_frame(&mut sender, PRESENT, utf8, deadline).unwrap();
        assert_eq!(
            read_header(&mut receiver, deadline).unwrap(),
            (PRESENT, utf8.len())
        );
        let mut received = [0u8; 11];
        read_exact(&mut receiver, &mut received[..utf8.len()], deadline).unwrap();
        assert_eq!(received[..utf8.len()], *utf8);
    }

    #[test]
    fn idle_connection_distinguishes_explicit_cancel_and_disconnect() {
        let (mut sender, receiver) = UnixStream::pair().unwrap();
        sender.set_nonblocking(true).unwrap();
        receiver.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + FRAME_TIMEOUT;
        let mut wire = Wire {
            stream: receiver,
            stop: None,
        };
        wire.check_idle(deadline).unwrap();
        send_frame(&mut sender, CANCEL, &[], deadline).unwrap();
        assert!(wire.check_idle(deadline).is_err());
        assert!(matches!(wire.stop, Some(Stop::Cancelled)));
        let (sender, stream) = UnixStream::pair().unwrap();
        stream.set_nonblocking(true).unwrap();
        let mut wire = Wire { stream, stop: None };
        drop(sender);
        assert!(wire.check_idle(deadline).is_err());
        assert!(matches!(wire.stop, Some(Stop::Broken)));
    }

    #[test]
    fn incomplete_frame_cannot_extend_its_absolute_deadline() {
        let (mut sender, mut receiver) = UnixStream::pair().unwrap();
        receiver.set_nonblocking(true).unwrap();
        sender.write_all(&[PRESENT, 0, 0]).unwrap();
        let error =
            read_header(&mut receiver, Instant::now() + Duration::from_millis(10)).unwrap_err();
        assert_eq!(
            error.downcast_ref::<io::Error>().unwrap().kind(),
            io::ErrorKind::TimedOut
        );
    }

    #[test]
    fn host_deadline_must_be_future_and_bounded() {
        let uptime = uptime_ns().unwrap();
        assert!(host_deadline(uptime).is_err());
        assert!(host_deadline(u64::MAX).is_err());
        let anchor = Instant::now();
        let accepted = host_deadline(uptime + 30_000_000_000).unwrap();
        assert!(accepted > anchor);
        assert!(accepted <= Instant::now() + Duration::from_secs(30));
    }
}
