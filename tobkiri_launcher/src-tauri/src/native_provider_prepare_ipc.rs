//! Private Registry requests and live authorization checks for the managed Host.

use std::{
    io::{self, Read, Write},
    os::{fd::AsRawFd, unix::net::UnixStream},
    sync::{Arc, Mutex, TryLockError},
    thread,
    time::{Duration, Instant},
};

use anyhow::{anyhow, bail, ensure, Context, Result};
use serde::{de, Deserialize, Deserializer, Serialize};
use serde_json::value::RawValue;
use tauri::{AppHandle, Manager};
use tobkiri_core::NativeArtifactError;
use tobkiri_wasm::NativeDataIoError;

use crate::{
    defaultspack_manager::{ApplicationProcessManager, ProviderPrepareListener},
    host_contract::ExecutionProfileIdentity,
    native_harness_bootstrap::{
        start_provider_prepare, ConversationReadCaller, ConversationRegistryRequiresLegacyRead,
        ModelRegistryRequiresLegacyRead, NativeRegistryRequest, PendingProviderPrepare,
        ProviderPrepareDeadline, CONVERSATION_GET_REPLY_LIMIT,
    },
    shell_handoff::{process_identity, LauncherProcessIdentity},
};

const START: u8 = 1;
const CHECK: u8 = 2;
const LIVE: u8 = 3;
const PLAN: u8 = 7;
const FAILURE: u8 = 8;
const CANCEL: u8 = 9;
const INAPPLICABLE: u8 = 10;
const PUBLISH_CHECK: u8 = 11;
const PUBLISH_PERMIT: u8 = 12;
const JSON_LIMIT: usize = 16 * 1024;
const MODEL_RESULT_LIMIT: usize = 1024 * 1024;
const FRAME_TIMEOUT: Duration = Duration::from_secs(3);
const POLL_INTERVAL: Duration = Duration::from_millis(10);
const CLEANUP_WAIT: Duration = Duration::from_secs(10);
const MAX_HORIZON: Duration = Duration::from_secs(300);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Start<'a> {
    identity: Identity,
    runtime_instance: String,
    security_epoch: u64,
    deadline_ns: u64,
    #[serde(default, deserialize_with = "deserialize_present")]
    request: Option<Request>,
    #[serde(borrow, default, deserialize_with = "deserialize_optional_object")]
    apply: Option<&'a RawValue>,
    #[serde(default, deserialize_with = "deserialize_present")]
    model_list: Option<ModelList>,
    #[serde(borrow, default, deserialize_with = "deserialize_present")]
    model_save: Option<ModelSave<'a>>,
    #[serde(default, deserialize_with = "deserialize_conversation_get")]
    conversation_get: Option<ConversationGet>,
}

impl Start<'_> {
    fn operation(&self) -> Result<NativeRegistryRequest> {
        match (
            &self.request,
            &self.model_list,
            &self.model_save,
            &self.conversation_get,
            self.apply,
        ) {
            (Some(request), None, None, None, apply) => {
                let request = serde_json::to_vec(request)?;
                ensure!(
                    request.len() <= JSON_LIMIT,
                    "Provider request exceeds its envelope"
                );
                // Preserve the frozen plan's exact JSON for the Core parser.
                let apply = apply.map(|plan| plan.get().as_bytes().to_vec());
                Ok(NativeRegistryRequest::Provider { request, apply })
            }
            (None, Some(_), None, None, None) => Ok(NativeRegistryRequest::ModelList),
            (None, None, Some(save), None, None) => Ok(NativeRegistryRequest::ModelSave {
                request: save.request.get().as_bytes().to_vec(),
                provider: save.provider.get().as_bytes().to_vec(),
            }),
            (None, None, None, Some(get), None) => Ok(NativeRegistryRequest::ConversationGet {
                caller: get.caller,
                conversation_id: get.conversation_id.clone(),
            }),
            _ => bail!("native Registry start must name exactly one operation"),
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelList {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelSave<'a> {
    #[serde(borrow, deserialize_with = "deserialize_object")]
    request: &'a RawValue,
    #[serde(borrow, deserialize_with = "deserialize_object")]
    provider: &'a RawValue,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConversationGet {
    #[serde(deserialize_with = "deserialize_conversation_caller")]
    caller: ConversationReadCaller,
    conversation_id: String,
}

fn deserialize_conversation_get<'de, D>(
    deserializer: D,
) -> std::result::Result<Option<ConversationGet>, D::Error>
where
    D: Deserializer<'de>,
{
    let object = deserialize_object(deserializer)?;
    serde_json::from_str(object.get())
        .map(Some)
        .map_err(de::Error::custom)
}

fn deserialize_conversation_caller<'de, D>(
    deserializer: D,
) -> std::result::Result<ConversationReadCaller, D::Error>
where
    D: Deserializer<'de>,
{
    match String::deserialize(deserializer)?.as_str() {
        "shell" => Ok(ConversationReadCaller::Shell),
        "saved-turn" => Ok(ConversationReadCaller::SavedTurn),
        "saved-orchestration" => Ok(ConversationReadCaller::SavedOrchestration),
        _ => Err(de::Error::custom("unknown Conversation caller")),
    }
}

fn deserialize_present<'de, T, D>(deserializer: D) -> std::result::Result<Option<T>, D::Error>
where
    T: Deserialize<'de>,
    D: Deserializer<'de>,
{
    T::deserialize(deserializer).map(Some)
}

fn deserialize_object<'de, D>(deserializer: D) -> std::result::Result<&'de RawValue, D::Error>
where
    D: Deserializer<'de>,
{
    let plan = <&RawValue>::deserialize(deserializer)?;
    if !plan.get().starts_with('{') {
        return Err(de::Error::custom("Registry input must be an object"));
    }
    Ok(plan)
}

fn deserialize_optional_object<'de, D>(
    deserializer: D,
) -> std::result::Result<Option<&'de RawValue>, D::Error>
where
    D: Deserializer<'de>,
{
    deserialize_object(deserializer).map(Some)
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
    let start: Start<'_> =
        serde_json::from_slice(&bytes).map_err(|_| anyhow!("invalid Provider prepare start"))?;
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
    let operation = start.operation()?;
    let identity = ExecutionProfileIdentity::new(
        start.identity.profile_id,
        start.identity.profile_revision,
        start.identity.activation_id,
        start.identity.plan_digest,
    )?;
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
    if !candidate.matches_runtime_instance(&start.runtime_instance)
        || !operation.has_launch_proof(candidate.verified_source())
    {
        return send_frame(
            &mut stream,
            INAPPLICABLE,
            &[],
            deadline.min(Instant::now() + FRAME_TIMEOUT),
        );
    }

    let reply = match &operation {
        NativeRegistryRequest::Provider { .. } => ReplyRole::Provider,
        NativeRegistryRequest::ModelList => ReplyRole::ModelList,
        NativeRegistryRequest::ModelSave { .. } => ReplyRole::ModelSave,
        NativeRegistryRequest::ConversationGet { .. } => ReplyRole::ConversationGet,
    };
    drop(bytes);
    let deadline = Arc::new(ProviderPrepareDeadline::new(deadline));
    let connection = Arc::new(Connection {
        manager,
        candidate: Arc::clone(&candidate),
        wire: Mutex::new(Wire::new(stream)),
        deadline: Arc::clone(&deadline),
        reply,
    });
    let check_connection = Arc::clone(&connection);
    let publish_permit = (reply == ReplyRole::ModelSave).then(|| {
        let connection = Arc::clone(&connection);
        Arc::new(move || connection.authorize_publication())
            as Arc<dyn Fn() -> Result<()> + Send + Sync>
    });
    let pending = match start_provider_prepare(
        app,
        candidate,
        operation,
        deadline,
        Arc::new(move || check_connection.check_current()),
        publish_permit,
    ) {
        Ok(pending) => pending,
        Err(error) => {
            return connection.send_failure(failure_code(&error), connection.deadline.current())
        }
    };
    connection.finish(pending)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ReplyRole {
    Provider,
    ModelList,
    ModelSave,
    ConversationGet,
}

struct Connection {
    manager: Arc<ApplicationProcessManager>,
    candidate: Arc<ProviderPrepareListener>,
    // The worker's authorization checks and the idle actor share one reader.
    wire: Mutex<Wire>,
    deadline: Arc<ProviderPrepareDeadline>,
    reply: ReplyRole,
}

struct Wire {
    stream: UnixStream,
    stop: Option<Stop>,
    authority: WireAuthority,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum WireAuthority {
    AwaitingLive,
    Live,
    PublicationPermitted,
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

    fn check_current(&self) -> Result<()> {
        self.authorize(false)
    }

    fn authorize_publication(&self) -> Result<()> {
        ensure!(
            self.reply == ReplyRole::ModelSave,
            "Registry exchange does not admit Model publication"
        );
        self.authorize(true)
    }

    fn authorize(&self, publish: bool) -> Result<()> {
        let mut wire = self.lock_wire()?;
        self.checkpoint(&mut wire)?;
        let deadline = self.deadline.current().min(Instant::now() + FRAME_TIMEOUT);
        let result = wire.exchange_authority(publish, deadline);
        if result.is_err() && wire.stop.is_none() {
            wire.stop = Some(if Instant::now() >= self.deadline.current() {
                Stop::Deadline
            } else {
                Stop::Broken
            });
        }
        result?;
        self.checkpoint(&mut wire)
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
        let (limit, overflow_code) = match self.reply {
            ReplyRole::Provider => (JSON_LIMIT, "limit-exceeded"),
            ReplyRole::ModelList => (MODEL_RESULT_LIMIT, "legacy-read-required"),
            ReplyRole::ModelSave => (MODEL_RESULT_LIMIT, "limit-exceeded"),
            ReplyRole::ConversationGet => (CONVERSATION_GET_REPLY_LIMIT, "legacy-read-required"),
        };
        match result {
            Ok(output) if output.len() <= limit => {
                send_frame(&mut wire.stream, PLAN, output.as_bytes(), deadline)
            }
            Ok(_) => send_frame(
                &mut wire.stream,
                FAILURE,
                overflow_code.as_bytes(),
                deadline,
            ),
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
    fn new(stream: UnixStream) -> Self {
        Self {
            stream,
            stop: None,
            authority: WireAuthority::AwaitingLive,
        }
    }

    fn exchange_authority(&mut self, publish: bool, deadline: Instant) -> Result<()> {
        self.check_idle(deadline)?;
        let (challenge, expected) = if publish {
            ensure!(
                self.authority == WireAuthority::Live,
                "Model Registry publication is outside its live phase"
            );
            (PUBLISH_CHECK, PUBLISH_PERMIT)
        } else {
            (CHECK, LIVE)
        };
        send_frame(&mut self.stream, challenge, &[], deadline)?;
        let header = read_header(&mut self.stream, deadline)?;
        if header == (CANCEL, 0) {
            self.stop = Some(Stop::Cancelled);
            return Err(Stop::Cancelled.as_error());
        }
        ensure!(
            header == (expected, 0),
            "invalid Registry authorization frame"
        );
        if publish {
            self.authority = WireAuthority::PublicationPermitted;
        } else if self.authority == WireAuthority::AwaitingLive {
            self.authority = WireAuthority::Live;
        }
        Ok(())
    }

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
    if error.is::<ModelRegistryRequiresLegacyRead>()
        || error.is::<ConversationRegistryRequiresLegacyRead>()
    {
        return "legacy-read-required";
    }
    match error.downcast_ref::<NativeArtifactError>() {
        Some(
            NativeArtifactError::ProviderRegistryLimitBeforePublish
            | NativeArtifactError::ModelRegistryLimitBeforePublish,
        ) => "legacy-read-required",
        Some(NativeArtifactError::ModelRegistryPublicationUnconfirmed(_)) => "unavailable",
        Some(NativeArtifactError::ProviderRejected(error)) => error.code(),
        Some(NativeArtifactError::ModelRegistryRejected(error)) => error.code(),
        Some(NativeArtifactError::ConversationRegistryRejected(error)) => error.code(),
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

    const PROVIDER_REQUEST: &str =
        r#", "request":{"connection-name":"local","endpoint":"http://localhost"}"#;

    fn start_frame(operation: &str) -> String {
        format!(
            r#"{{"identity":{{"profile_id":"profile","profile_revision":"revision","activation_id":"activation","plan_digest":"digest"}},"runtime_instance":"0123456789abcdef0123456789abcdef","security_epoch":1,"deadline_ns":1{operation}}}"#
        )
    }

    #[test]
    fn start_distinguishes_prepare_and_preserves_frozen_apply_bytes() {
        let prepare = start_frame(PROVIDER_REQUEST);
        let prepare: Start<'_> = serde_json::from_str(&prepare).unwrap();
        assert!(prepare.apply.is_none());
        assert_eq!(prepare.request.as_ref().unwrap().connection_name, "local");
        assert!(matches!(
            prepare.operation().unwrap(),
            NativeRegistryRequest::Provider { apply: None, .. }
        ));

        // Leave semantic checks, including duplicate object keys, to the Core.
        let plan = r#"{"record": {"name":"ä", "name":"same"}}"#;
        let apply = start_frame(&format!(r#"{PROVIDER_REQUEST}, "apply": {plan}"#));
        let apply: Start<'_> = serde_json::from_str(&apply).unwrap();
        assert_eq!(apply.apply.unwrap().get(), plan);
        let list = start_frame(r#", "model_list": {}"#);
        let list: Start<'_> = serde_json::from_str(&list).unwrap();
        assert!(matches!(
            list.operation().unwrap(),
            NativeRegistryRequest::ModelList
        ));
    }

    #[test]
    fn start_rejects_nonobject_plans_and_extra_envelope_fields() {
        for extra in [
            r#","plan":{}"#,
            r#","plan":{},"apply":{}"#,
            r#","apply":"{}""#,
            r#","apply":42"#,
            r#","apply":{},"apply":{}"#,
            r#","apply":{},"approved":true"#,
            r#","apply":null"#,
            r#","apply":[] "#,
        ] {
            let payload = start_frame(&format!("{PROVIDER_REQUEST}{extra}"));
            assert!(
                serde_json::from_str::<Start<'_>>(&payload).is_err(),
                "{extra}"
            );
        }
        for extra in [
            r#", "request":null, "model_list":{}"#,
            r#", "model_list":null"#,
            r#", "model_list":true"#,
            r#", "model_list":{"profile_id":"elsewhere"}"#,
            r#", "model_list":{}, "model_list":{}"#,
        ] {
            let payload = start_frame(extra);
            assert!(
                serde_json::from_str::<Start<'_>>(&payload).is_err(),
                "{extra}"
            );
        }
        for extra in [
            String::new(),
            format!(r#"{PROVIDER_REQUEST}, "model_list":{{}}"#),
            r#", "apply":{}, "model_list":{}"#.to_owned(),
        ] {
            let payload = start_frame(&extra);
            let parsed: Start<'_> = serde_json::from_str(&payload).unwrap();
            assert!(parsed.operation().is_err(), "{extra}");
        }
    }

    #[test]
    fn frames_preserve_utf8_payload_bytes() {
        let (mut sender, mut receiver) = UnixStream::pair().unwrap();
        sender.set_nonblocking(true).unwrap();
        receiver.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + FRAME_TIMEOUT;
        let utf8 = "{\"ä\":null}".as_bytes();
        send_frame(&mut sender, PLAN, utf8, deadline).unwrap();
        assert_eq!(
            read_header(&mut receiver, deadline).unwrap(),
            (PLAN, utf8.len())
        );
        let mut received = [0u8; 11];
        read_exact(&mut receiver, &mut received[..utf8.len()], deadline).unwrap();
        assert_eq!(received[..utf8.len()], *utf8);
    }

    #[test]
    fn save_start_borrows_only_two_object_inputs() {
        let request = r#"{"model_id": "gemma-3-1b"}"#;
        let provider = r#"{"revision":1,"providers":[]}"#;
        let save = format!(r#", "model_save":{{"request":{request},"provider":{provider}}}"#);
        let frame = start_frame(&save);
        let start: Start<'_> = serde_json::from_str(&frame).unwrap();
        let NativeRegistryRequest::ModelSave {
            request: raw,
            provider: snapshot,
        } = start.operation().unwrap()
        else {
            panic!("expected Model Save");
        };
        assert_eq!(raw, request.as_bytes());
        assert_eq!(snapshot, provider.as_bytes());
        for extra in [
            PROVIDER_REQUEST,
            r#", "model_list": {}"#,
            r#", "apply": {}"#,
        ] {
            let frame = start_frame(&format!("{save}{extra}"));
            assert!(serde_json::from_str::<Start<'_>>(&frame)
                .unwrap()
                .operation()
                .is_err());
        }
        for body in [
            r#"null"#,
            r#"{}"#,
            r#"{"request":[],"provider":{}}"#,
            r#"{"request":{},"provider":{},"permit":true}"#,
        ] {
            let frame = start_frame(&format!(r#", "model_save":{body}"#));
            assert!(serde_json::from_str::<Start<'_>>(&frame).is_err());
        }
    }

    #[test]
    fn conversation_get_accepts_only_its_fixed_caller_and_conversation_id() {
        for (wire_caller, expected_caller) in [
            ("shell", ConversationReadCaller::Shell),
            ("saved-turn", ConversationReadCaller::SavedTurn),
            (
                "saved-orchestration",
                ConversationReadCaller::SavedOrchestration,
            ),
        ] {
            let frame = start_frame(&format!(
                r#", "conversation_get":{{"caller":"{wire_caller}","conversation_id":"会話"}}"#
            ));
            let start: Start<'_> = serde_json::from_str(&frame).unwrap();
            let NativeRegistryRequest::ConversationGet {
                caller,
                conversation_id,
            } = start.operation().unwrap()
            else {
                panic!("expected Conversation Get");
            };
            assert_eq!(caller, expected_caller);
            assert_eq!(conversation_id, "会話");
        }

        for body in [
            r#"null"#,
            r#"{}"#,
            r#"["shell","stored"]"#,
            r#"{"caller":"shell"}"#,
            r#"{"conversation_id":"stored"}"#,
            r#"{"caller":"unknown","conversation_id":"stored"}"#,
            r#"{"caller":"saved_turn","conversation_id":"stored"}"#,
            r#"{"caller":{"shell":null},"conversation_id":"stored"}"#,
            r#"{"caller":"shell","conversation_id":123}"#,
            r#"{"caller":"shell","conversation_id":"stored","profile_id":"other"}"#,
            r#"{"caller":"shell","caller":"saved-turn","conversation_id":"stored"}"#,
        ] {
            let frame = start_frame(&format!(r#", "conversation_get":{body}"#));
            assert!(serde_json::from_str::<Start<'_>>(&frame).is_err(), "{body}");
        }
    }

    #[test]
    fn conversation_get_cannot_share_a_start_with_another_operation() {
        let get = r#", "conversation_get":{"caller":"shell","conversation_id":"stored"}"#;
        for extra in [
            PROVIDER_REQUEST,
            r#", "apply": {}"#,
            r#", "model_list": {}"#,
            r#", "model_save": {"request":{},"provider":{}}"#,
        ] {
            let frame = start_frame(&format!("{get}{extra}"));
            assert!(serde_json::from_str::<Start<'_>>(&frame)
                .unwrap()
                .operation()
                .is_err());
        }
        assert!(serde_json::from_str::<Start<'_>>(&start_frame(&format!("{get}{get}"))).is_err());
        assert_eq!(
            failure_code(&ConversationRegistryRequiresLegacyRead.into()),
            "legacy-read-required"
        );
    }

    #[test]
    fn publication_requires_live_and_uses_one_permit() {
        let (mut client, server) = UnixStream::pair().unwrap();
        client.set_nonblocking(true).unwrap();
        server.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + FRAME_TIMEOUT;
        let mut wire = Wire::new(server);
        assert!(wire.exchange_authority(true, deadline).is_err());
        let peer = thread::spawn(move || {
            assert_eq!(read_header(&mut client, deadline).unwrap(), (CHECK, 0));
            send_frame(&mut client, LIVE, &[], deadline).unwrap();
            assert_eq!(
                read_header(&mut client, deadline).unwrap(),
                (PUBLISH_CHECK, 0)
            );
            send_frame(&mut client, PUBLISH_PERMIT, &[], deadline).unwrap();
            assert_eq!(read_header(&mut client, deadline).unwrap(), (CHECK, 0));
            send_frame(&mut client, LIVE, &[], deadline).unwrap();
            client
        });
        wire.exchange_authority(false, deadline).unwrap();
        wire.exchange_authority(true, deadline).unwrap();
        wire.exchange_authority(false, deadline).unwrap();
        let _peer = peer.join().unwrap();
        assert!(wire.exchange_authority(true, deadline).is_err());
    }

    #[test]
    fn idle_connection_distinguishes_explicit_cancel_and_disconnect() {
        let (mut sender, receiver) = UnixStream::pair().unwrap();
        sender.set_nonblocking(true).unwrap();
        receiver.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + FRAME_TIMEOUT;
        let mut wire = Wire::new(receiver);
        wire.check_idle(deadline).unwrap();
        send_frame(&mut sender, CANCEL, &[], deadline).unwrap();
        assert!(wire.check_idle(deadline).is_err());
        assert!(matches!(wire.stop, Some(Stop::Cancelled)));
        let (sender, stream) = UnixStream::pair().unwrap();
        stream.set_nonblocking(true).unwrap();
        let mut wire = Wire::new(stream);
        drop(sender);
        assert!(wire.check_idle(deadline).is_err());
        assert!(matches!(wire.stop, Some(Stop::Broken)));
    }

    #[test]
    fn incomplete_frame_cannot_extend_its_absolute_deadline() {
        let (mut sender, mut receiver) = UnixStream::pair().unwrap();
        receiver.set_nonblocking(true).unwrap();
        sender.write_all(&[CHECK, 0, 0]).unwrap();
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
