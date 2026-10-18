//! Descriptor-confined native filesystem effects.
//!
//! Only trusted Core code registers already-open directory descriptors and
//! issues picker tokens. Neither a guest pathname nor a persisted inode tuple
//! can manufacture a root. Each open and each later operation still consumes
//! its own exact Gate grant. A directory resource is only a bound ceiling.
//!
//! The threat boundary is a malicious Wasm guest. All Core-mediated namespace
//! mutations, including overlapping registered roots, use one process-wide
//! lock. An independent editor running as the same OS user is not serialized:
//! post-validation namespace/hardlink/mount changes by that editor remain a
//! platform limitation, rather than a claim of an OS sandbox or VM boundary.
//!
//! `FsWork` must be moved into Core's supervised blocking task. Cancellation of
//! an awaiting guest must NOT drop that task's real input/FD/control custody.
//! `execute` consumes the work once and returns a retained `FsCompletion`;
//! Core keeps the completion through conversion and immutable terminal delivery.
//! Nothing in this module retries an effect or writes a terminal from Drop.

use std::{
    fmt,
    fs::File,
    io,
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{AllocationBudget, AllocationError, CoreBuffer, CoreValue, Digest};
use tobkiri_gate::{
    CallCx, ControlKind, ControlReservation, CoreControl, DispatchPermit, EffectRequest,
    FsOperation, Gate, GateError, Grant,
};

#[path = "filesystem_runtime.rs"]
pub(crate) mod runtime;

const MAX_ROOTS: usize = 64;
const MAX_PROTECTED: usize = 64;
const MAX_TOKENS: usize = 64;
const MAX_PATH: usize = 4096;
const MAX_COMPONENTS: usize = 128;
const MAX_LIST: usize = 1024;
const MAX_LIST_BYTES: usize = 64 * 1024;
const MAX_IO: usize = 32 * 1024 * 1024;
const CHUNK: usize = 64 * 1024;
const PRIVATE_PREFIX: &str = ".tobkiri-core-fs-";
const MAX_TOKEN_AGE: Duration = Duration::from_secs(60);
const CALL_CONTROL_BYTES: u64 = 64 * 1024;
static NEXT_ID: AtomicU64 = AtomicU64::new(1);
static NAMESPACE_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

#[derive(Debug)]
pub enum FsError {
    Invalid(&'static str),
    Denied(&'static str),
    Unavailable,
    Io(io::Error),
    Gate(GateError),
    Allocation(AllocationError),
    /// A mutation may have happened, or a committed result was not delivered.
    /// The consumed grant cannot be replayed in either case.
    CommitUnknown,
}
impl fmt::Display for FsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(message) | Self::Denied(message) => f.write_str(message),
            Self::Unavailable => f.write_str("descriptor-confined filesystem unavailable"),
            Self::Io(error) => error.fmt(f),
            Self::Gate(error) => error.fmt(f),
            Self::Allocation(error) => error.fmt(f),
            Self::CommitUnknown => f.write_str("filesystem commit outcome unknown; do not replay"),
        }
    }
}
impl std::error::Error for FsError {}
impl From<io::Error> for FsError {
    fn from(value: io::Error) -> Self {
        Self::Io(value)
    }
}
impl From<GateError> for FsError {
    fn from(value: GateError) -> Self {
        Self::Gate(value)
    }
}
impl From<AllocationError> for FsError {
    fn from(value: AllocationError) -> Self {
        Self::Allocation(value)
    }
}
type Result<T> = std::result::Result<T, FsError>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FsRights {
    pub read: bool,
    pub write: bool,
}
impl FsRights {
    fn subset_of(self, ceiling: Self) -> bool {
        (self.read || self.write) && (!self.read || ceiling.read) && (!self.write || ceiling.write)
    }
    fn allows(self, operation: FsOperation) -> bool {
        match operation {
            FsOperation::Open => self.read || self.write,
            FsOperation::Read | FsOperation::List | FsOperation::Stat => self.read,
            FsOperation::Write | FsOperation::Rename | FsOperation::Remove | FsOperation::Mkdir => {
                self.write
            }
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct ObjectId {
    device: u64,
    inode: u64,
}

struct RegisteredRoot {
    id: String,
    file: File,
    object: ObjectId,
    rights: FsRights,
    _reservation: ControlReservation,
}

/// Opaque Core-issued identity retaining the original descriptor. Its displayed
/// name is the exact `FsPermission.root` value for policy and hash approval.
#[derive(Clone)]
pub struct FsRootIdentity {
    root: Arc<RegisteredRoot>,
}
impl FsRootIdentity {
    pub fn id(&self) -> &str {
        &self.root.id
    }
}
impl fmt::Debug for FsRootIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_tuple("FsRootIdentity").field(&self.id()).finish()
    }
}
struct ProtectedRoot {
    file: File,
    object: ObjectId,
    _reservation: ControlReservation,
}
struct OpenToken {
    token: String,
    root: Arc<RegisteredRoot>,
    rights: FsRights,
    cx: CallCx,
    expiry: Instant,
    _reservation: ControlReservation,
}
struct RegistryState {
    roots: Vec<Arc<RegisteredRoot>>,
    protected: Vec<ProtectedRoot>,
    tokens: Vec<OpenToken>,
}
struct RegistryInner {
    state: Mutex<RegistryState>,
    control: CoreControl,
    _reservation: ControlReservation,
}

/// Private bounded root/token registry. Keep one per live Core and register
/// the actual live authority/approval/audit/catalog/secret directory descriptors
/// (including Core staging directories) as protected before issuing any token.
#[derive(Clone)]
pub struct FsRegistry {
    inner: Arc<RegistryInner>,
}
impl FsRegistry {
    pub fn new(control: CoreControl) -> Result<Self> {
        if !cfg!(unix) {
            return Err(FsError::Unavailable);
        }
        let size = std::mem::size_of::<RegistryInner>()
            + MAX_ROOTS * std::mem::size_of::<Arc<RegisteredRoot>>()
            + MAX_PROTECTED * std::mem::size_of::<ProtectedRoot>()
            + MAX_TOKENS * std::mem::size_of::<OpenToken>();
        let reservation = control
            .reserve(ControlKind::Registration, size as u64)
            .map_err(|_| FsError::Unavailable)?;
        let mut state = RegistryState {
            roots: Vec::new(),
            protected: Vec::new(),
            tokens: Vec::new(),
        };
        state
            .roots
            .try_reserve_exact(MAX_ROOTS)
            .map_err(|_| FsError::Unavailable)?;
        state
            .protected
            .try_reserve_exact(MAX_PROTECTED)
            .map_err(|_| FsError::Unavailable)?;
        state
            .tokens
            .try_reserve_exact(MAX_TOKENS)
            .map_err(|_| FsError::Unavailable)?;
        Ok(Self {
            inner: Arc::new(RegistryInner {
                state: Mutex::new(state),
                control,
                _reservation: reservation,
            }),
        })
    }

    /// Trusted registration only: `directory` must come from Core's explicit
    /// persisted root configuration ceremony, never from guest supplied paths.
    /// Root IDs are newly issued and never reconstructed from dev/inode alone.
    /// Adding/changing configured roots requires fresh policy/hash approval.
    pub fn register_root(&self, directory: File, rights: FsRights) -> Result<FsRootIdentity> {
        if !rights.read && !rights.write {
            return Err(FsError::Invalid("empty root rights"));
        }
        let _namespace = namespace_lock()?;
        let object = native::directory_id(&directory)?;
        let mut state = self.inner.state.lock().map_err(|_| FsError::Unavailable)?;
        if state.roots.len() == MAX_ROOTS {
            return Err(FsError::Unavailable);
        }
        let reservation = self
            .inner
            .control
            .reserve(
                ControlKind::Registration,
                (std::mem::size_of::<RegisteredRoot>() + 256) as u64,
            )
            .map_err(|_| FsError::Unavailable)?;
        let id = opaque_id("fs-root:")?;
        let root = Arc::new(RegisteredRoot {
            id,
            file: directory,
            object,
            rights,
            _reservation: reservation,
        });
        state.roots.push(root.clone());
        Ok(FsRootIdentity { root })
    }

    /// Protect a live Core subtree by actual open descriptor, including against
    /// rename/remove of any ancestor and replacement at either rename endpoint.
    pub fn protect_root(&self, directory: File) -> Result<()> {
        let _namespace = namespace_lock()?;
        let object = native::directory_id(&directory)?;
        let mut state = self.inner.state.lock().map_err(|_| FsError::Unavailable)?;
        if state.protected.iter().any(|item| item.object == object) {
            return Ok(());
        }
        if state.protected.len() == MAX_PROTECTED {
            return Err(FsError::Unavailable);
        }
        let reservation = self
            .inner
            .control
            .reserve(
                ControlKind::Registration,
                std::mem::size_of::<ProtectedRoot>() as u64,
            )
            .map_err(|_| FsError::Unavailable)?;
        state.protected.push(ProtectedRoot {
            file: directory,
            object,
            _reservation: reservation,
        });
        Ok(())
    }

    /// Core's trusted picker/configuration issuer. This token carries no Gate
    /// approval and cannot widen the registered ceiling or a later policy.
    pub fn issue_open_token(
        &self,
        cx: &CallCx,
        root: &FsRootIdentity,
        rights: FsRights,
        ttl: Duration,
    ) -> Result<String> {
        cx.check_active()?;
        if ttl.is_zero() || ttl > MAX_TOKEN_AGE || !rights.subset_of(root.root.rights) {
            return Err(FsError::Denied(
                "invalid filesystem token ceiling or expiry",
            ));
        }
        let expiry = Instant::now()
            .checked_add(ttl)
            .ok_or(FsError::Unavailable)?
            .min(cx.deadline());
        let mut state = self.inner.state.lock().map_err(|_| FsError::Unavailable)?;
        state.tokens.retain(|token| token.expiry > Instant::now());
        if !state.roots.iter().any(|item| Arc::ptr_eq(item, &root.root)) {
            return Err(FsError::Denied(
                "filesystem root belongs to another Core registry",
            ));
        }
        if state.tokens.len() == MAX_TOKENS {
            return Err(FsError::Unavailable);
        }
        let reservation = self
            .inner
            .control
            .reserve(
                ControlKind::Registration,
                (std::mem::size_of::<OpenToken>() + 256) as u64,
            )
            .map_err(|_| FsError::Unavailable)?;
        let token = opaque_id("fs-open:")?;
        let returned = copy_string(&token)?;
        state.tokens.push(OpenToken {
            token,
            root: root.root.clone(),
            rights,
            cx: cx.clone(),
            expiry,
            _reservation: reservation,
        });
        Ok(returned)
    }

    /// A token is removed before any binding/expiry check, so even a rejected
    /// attempt cannot reuse it. Original scope includes exact request identity.
    pub fn prepare_open(&self, cx: &CallCx, token: &str) -> Result<PreparedFsOpen> {
        if token.len() != 72 || !token.starts_with("fs-open:") {
            return Err(FsError::Denied("unknown filesystem open token"));
        }
        let reservation = cx
            .control()
            .reserve(ControlKind::Frame, CALL_CONTROL_BYTES)
            .map_err(|_| FsError::Unavailable)?;
        let mut state = self.inner.state.lock().map_err(|_| FsError::Unavailable)?;
        let index = state
            .tokens
            .iter()
            .position(|entry| entry.token == token)
            .ok_or(FsError::Denied("unknown or consumed filesystem open token"))?;
        let token = state.tokens.swap_remove(index);
        cx.check_active()?;
        if Instant::now() >= token.expiry || !same_scope(&token.cx, cx) {
            return Err(FsError::Denied("expired or foreign filesystem open token"));
        }
        let request = EffectRequest::Fs {
            root: copy_string(&token.root.id)?,
            path: String::new(),
            operation: FsOperation::Open,
        };
        let args = argument_digest(&token.root.id, token.rights, &OwnedCall::Open);
        Ok(PreparedFsOpen {
            registry: self.clone(),
            token,
            request,
            args,
            _reservation: reservation,
        })
    }
}

pub struct PreparedFsOpen {
    registry: FsRegistry,
    token: OpenToken,
    request: EffectRequest,
    args: Digest,
    _reservation: ControlReservation,
}
impl PreparedFsOpen {
    pub fn request(&self) -> &EffectRequest {
        &self.request
    }
    pub fn args_digest(&self) -> &Digest {
        &self.args
    }
    pub async fn consume(
        self,
        grant: Grant,
        cx: &CallCx,
        gate: Arc<dyn Gate>,
    ) -> Result<FsOpenCompletion> {
        if Instant::now() >= self.token.expiry || !same_scope(&self.token.cx, cx) {
            return Err(FsError::Denied("expired or foreign filesystem open token"));
        }
        let (permit, cx) = consume(grant, cx, gate.as_ref(), &self.request, &self.args).await?;
        // Opening grants no future I/O; the pinned root and its rights are only
        // a ceiling for the fresh grants required by Directory::prepare.
        let reservation = Arc::new(self._reservation);
        let directory = FsDirectory {
            registry: self.registry,
            root: self.token.root,
            rights: self.token.rights,
            cx: cx.clone(),
            _reservation: reservation.clone(),
        };
        Ok(FsOpenCompletion {
            directory,
            permit,
            cx,
            _token_reservation: self.token._reservation,
            _reservation: reservation,
        })
    }
}

/// Retain this receipt until `open-grant` resource conversion and terminal
/// delivery have completed. Its permit is the real consumed Open lease.
pub struct FsOpenCompletion {
    pub directory: FsDirectory,
    permit: DispatchPermit,
    cx: CallCx,
    _token_reservation: ControlReservation,
    // Also retain Open's enclosing control if the directory is moved/dropped
    // during conversion before the native Open receipt is delivered.
    _reservation: Arc<ControlReservation>,
}
impl FsOpenCompletion {
    pub fn permit(&self) -> &DispatchPermit {
        &self.permit
    }
    pub fn context(&self) -> &CallCx {
        &self.cx
    }
}

#[derive(Clone)]
pub struct FsDirectory {
    registry: FsRegistry,
    root: Arc<RegisteredRoot>,
    rights: FsRights,
    cx: CallCx,
    _reservation: Arc<ControlReservation>,
}
impl FsDirectory {
    pub fn root_id(&self) -> &str {
        &self.root.id
    }
    pub fn rights(&self) -> FsRights {
        self.rights
    }
    pub fn prepare(&self, cx: &CallCx, call: FsCall<'_>) -> Result<PreparedFsCall> {
        cx.check_active()?;
        self.cx.check_active()?;
        if !same_scope(&self.cx, cx) {
            return Err(FsError::Denied(
                "filesystem directory has foreign or stale scope",
            ));
        }
        let reservation = cx
            .control()
            .reserve(ControlKind::Task, CALL_CONTROL_BYTES)
            .map_err(|_| FsError::Unavailable)?;
        let call = OwnedCall::from_borrowed(call)?;
        let operation = call.operation();
        if !self.rights.allows(operation) {
            return Err(FsError::Denied("filesystem directory rights denied"));
        }
        let request = EffectRequest::Fs {
            root: copy_string(&self.root.id)?,
            path: copy_string(call.path())?,
            operation,
        };
        let args = argument_digest(&self.root.id, self.rights, &call);
        Ok(PreparedFsCall {
            directory: self.clone(),
            call,
            request,
            args,
            _reservation: Some(reservation),
        })
    }
}

/// Paths are borrowed until bounded Core control admission has succeeded.
/// Write data must already have its actual immutable Core allocation owner.
pub enum FsCall<'a> {
    Read {
        path: &'a str,
        offset: u64,
        len: u32,
    },
    Write {
        path: &'a str,
        data: CoreBuffer,
        append: bool,
    },
    List {
        path: &'a str,
    },
    Stat {
        path: &'a str,
    },
    Rename {
        from: &'a str,
        to: &'a str,
    },
    Remove {
        path: &'a str,
    },
    Mkdir {
        path: &'a str,
    },
}
enum OwnedCall {
    Open,
    Read {
        path: String,
        offset: u64,
        len: u32,
    },
    Write {
        path: String,
        data: CoreBuffer,
        append: bool,
    },
    List {
        path: String,
    },
    Stat {
        path: String,
    },
    Rename {
        from: String,
        to: String,
    },
    Remove {
        path: String,
    },
    Mkdir {
        path: String,
    },
}
impl OwnedCall {
    fn from_borrowed(call: FsCall<'_>) -> Result<Self> {
        let path = |path: &str, root: bool| {
            validate_path(path, root)?;
            copy_string(path)
        };
        Ok(match call {
            FsCall::Read {
                path: value,
                offset,
                len,
            } => {
                if offset > i64::MAX as u64
                    || offset
                        .checked_add(u64::from(len))
                        .is_none_or(|n| n > i64::MAX as u64)
                    || len as usize > MAX_IO
                {
                    return Err(FsError::Invalid("filesystem read range exceeds bound"));
                }
                Self::Read {
                    path: path(value, false)?,
                    offset,
                    len,
                }
            }
            FsCall::Write {
                path: value,
                data,
                append,
            } => {
                if data.len() > MAX_IO {
                    return Err(FsError::Invalid("filesystem write exceeds bound"));
                }
                Self::Write {
                    path: path(value, false)?,
                    data,
                    append,
                }
            }
            FsCall::List { path: value } => Self::List {
                path: path(value, true)?,
            },
            FsCall::Stat { path: value } => Self::Stat {
                path: path(value, true)?,
            },
            FsCall::Rename { from, to } => Self::Rename {
                from: path(from, false)?,
                to: path(to, false)?,
            },
            FsCall::Remove { path: value } => Self::Remove {
                path: path(value, false)?,
            },
            FsCall::Mkdir { path: value } => Self::Mkdir {
                path: path(value, false)?,
            },
        })
    }
    fn operation(&self) -> FsOperation {
        match self {
            Self::Open => FsOperation::Open,
            Self::Read { .. } => FsOperation::Read,
            Self::Write { .. } => FsOperation::Write,
            Self::List { .. } => FsOperation::List,
            Self::Stat { .. } => FsOperation::Stat,
            Self::Rename { .. } => FsOperation::Rename,
            Self::Remove { .. } => FsOperation::Remove,
            Self::Mkdir { .. } => FsOperation::Mkdir,
        }
    }
    fn path(&self) -> &str {
        match self {
            Self::Open => "",
            Self::Read { path, .. }
            | Self::Write { path, .. }
            | Self::List { path }
            | Self::Stat { path }
            | Self::Remove { path }
            | Self::Mkdir { path } => path,
            Self::Rename { from, .. } => from,
        }
    }
}

pub struct PreparedFsCall {
    directory: FsDirectory,
    call: OwnedCall,
    request: EffectRequest,
    args: Digest,
    _reservation: Option<ControlReservation>,
}
impl PreparedFsCall {
    pub fn request(&self) -> &EffectRequest {
        &self.request
    }
    pub fn args_digest(&self) -> &Digest {
        &self.args
    }
    pub async fn consume(
        mut self,
        grant: Grant,
        cx: &CallCx,
        gate: Arc<dyn Gate>,
        budget: Arc<dyn AllocationBudget>,
    ) -> Result<FsWork> {
        if !same_scope(&self.directory.cx, cx) {
            return Err(FsError::Denied(
                "filesystem directory has foreign or stale scope",
            ));
        }
        let (permit, cx) = consume(grant, cx, gate.as_ref(), &self.request, &self.args).await?;
        let count = match &self.call {
            OwnedCall::Read { len, .. } => u64::from(*len),
            OwnedCall::Write { data, .. } => data.len() as u64,
            _ => 0,
        };
        if count > cx.limits().output_bytes {
            return Err(FsError::Denied(
                "filesystem operation exceeds lease byte limit",
            ));
        }
        let reservation = self._reservation.take().ok_or(FsError::Unavailable)?;
        Ok(FsWork {
            prepared: self,
            permit,
            cx,
            gate,
            budget,
            #[cfg(test)]
            panic_before_io: false,
            _reservation: reservation,
        })
    }
}
async fn consume(
    grant: Grant,
    cx: &CallCx,
    gate: &dyn Gate,
    request: &EffectRequest,
    args: &Digest,
) -> Result<(DispatchPermit, CallCx)> {
    gate.check_context(cx)?;
    let mut permit = grant.consume(cx, request, args)?;
    let cx = cx.clone().narrow_limits(permit.limits());
    gate.narrow_lease(&mut permit, &cx).await?;
    gate.watch_lease(&cx, &permit)?;
    gate.check_context(&cx)?;
    cx.check_active()?;
    if Instant::now() >= permit.deadline() {
        return Err(FsError::Gate(GateError::Deadline));
    }
    Ok((permit, cx))
}

/// Move-only custody of one genuinely consumed operation. The supervisor owns
/// this until the blocking OS operation has actually stopped, even if delivery
/// was cancelled. No API permits extracting input or dispatching it twice.
pub struct FsWork {
    prepared: PreparedFsCall,
    permit: DispatchPermit,
    cx: CallCx,
    gate: Arc<dyn Gate>,
    budget: Arc<dyn AllocationBudget>,
    #[cfg(test)]
    panic_before_io: bool,
    // Last: all actual inputs, permit/cx metadata, FDs and outputs in the
    // enclosing completion are destroyed before native control is released.
    _reservation: ControlReservation,
}
impl FsWork {
    pub(crate) fn is_mutation(&self) -> bool {
        matches!(
            self.prepared.call,
            OwnedCall::Write { .. }
                | OwnedCall::Rename { .. }
                | OwnedCall::Remove { .. }
                | OwnedCall::Mkdir { .. }
        )
    }
    pub fn permit(&self) -> &DispatchPermit {
        &self.permit
    }
    pub fn context(&self) -> &CallCx {
        &self.cx
    }
    pub fn execute(self) -> FsCompletion {
        let mut disposition = FsDisposition::Clean;
        // Keep `self` outside the unwinding scope. A native panic must return
        // an uncertain completion retaining the actual permit, FDs and input
        // until the existing supervisor has delivered its terminal record.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.check_active()?;
            #[cfg(test)]
            if self.panic_before_io {
                panic!("injected filesystem worker panic");
            }
            let _namespace = namespace_lock()?;
            self.check_active()?;
            let state = self
                .prepared
                .directory
                .registry
                .inner
                .state
                .lock()
                .map_err(|_| FsError::Unavailable)?;
            let access = NativeAccess {
                root: &self.prepared.directory.root,
                protected: &state.protected,
            };
            native::execute(
                &access,
                &self.prepared.call,
                &self.budget,
                self.cx.limits().output_bytes,
                &|| self.check_active(),
                &mut disposition,
            )
        }));
        let result = match result {
            Ok(result) => result,
            Err(_) => {
                disposition = FsDisposition::PartialUnknown;
                Err(FsError::CommitUnknown)
            }
        };
        let result = match result {
            Err(_) if disposition != FsDisposition::Clean => Err(FsError::CommitUnknown),
            result => result,
        };
        FsCompletion {
            work: self,
            result: Some(result),
            disposition,
        }
    }
    fn check_active(&self) -> Result<()> {
        self.cx.check_active()?;
        self.prepared.directory.cx.check_active()?;
        if Instant::now() >= self.permit.deadline() {
            return Err(FsError::Gate(GateError::Deadline));
        }
        self.gate.check_context(&self.cx)?;
        if !self.permit.matches_context(&self.cx) {
            return Err(FsError::Denied("filesystem permit scope mismatch"));
        }
        Ok(())
    }
}

/// Preserve physical-effect replay evidence through guest cancellation, traps,
/// output conversion and terminal persistence failures in the enclosing tree.
pub(crate) fn preserve_effect_error(cx: &CallCx, error: anyhow::Error) -> anyhow::Error {
    if cx.filesystem_mutation_possible() {
        FsError::CommitUnknown.into()
    } else {
        error
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FsDisposition {
    /// No namespace/content effect happened; a failed call is a clean failure.
    Clean,
    /// A syscall/append may already have changed data. Never replay the grant.
    PartialUnknown,
    /// The requested mutation completed. Delivery failure becomes unknown to
    /// the guest, while Gate's existing immutable winner remains unchanged.
    Committed,
}
pub enum FsOutput {
    Unit,
    Bytes(CoreBuffer),
    /// Array of {"name": string, "is-dir": bool, "size": decimal-string}.
    /// Decimal storage preserves the complete WIT u64 range without an
    /// unowned native payload or JSON's safe-integer restriction.
    Entries(CoreValue),
    Stat(FsStat),
}
pub struct FsStat {
    pub name: CoreValue,
    pub size: u64,
    pub is_directory: bool,
    pub modified_unix_ms: Option<u64>,
}
/// Keep this receipt through result conversion and terminal delivery. Taking
/// the result does not release actual work/input/control/descriptor custody.
pub struct FsCompletion {
    result: Option<Result<FsOutput>>,
    disposition: FsDisposition,
    work: FsWork,
}
impl FsCompletion {
    pub fn permit(&self) -> &DispatchPermit {
        &self.work.permit
    }
    pub fn context(&self) -> &CallCx {
        &self.work.cx
    }
    pub fn disposition(&self) -> FsDisposition {
        self.disposition
    }
    pub fn take_result(&mut self) -> Result<FsOutput> {
        self.result
            .take()
            .ok_or(FsError::Denied("filesystem completion already taken"))?
    }
    /// Use when conversion, cancellation, audit or terminal delivery fails.
    /// This never retries native work or proposes a new immutable winner.
    pub fn delivery_error(&self, error: FsError) -> FsError {
        if self.disposition != FsDisposition::Clean {
            FsError::CommitUnknown
        } else {
            error
        }
    }
}

fn same_scope(left: &CallCx, right: &CallCx) -> bool {
    left.profile() == right.profile()
        && left.principal() == right.principal()
        && left.request() == right.request()
        && left.revision() == right.revision()
        && left.security_epoch() == right.security_epoch()
}
fn namespace_lock() -> Result<std::sync::MutexGuard<'static, ()>> {
    NAMESPACE_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .map_err(|_| FsError::Unavailable)
}
fn copy_string(value: &str) -> Result<String> {
    let mut result = String::new();
    result
        .try_reserve_exact(value.len())
        .map_err(|_| FsError::Unavailable)?;
    result.push_str(value);
    Ok(result)
}
fn opaque_id(prefix: &str) -> Result<String> {
    let sequence = NEXT_ID
        .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| n.checked_add(1))
        .map_err(|_| FsError::Unavailable)?;
    let mut random = [0u8; 32];
    getrandom::fill(&mut random).map_err(|_| FsError::Unavailable)?;
    random[24..].copy_from_slice(&sequence.to_be_bytes());
    let mut id = String::new();
    id.try_reserve_exact(prefix.len() + 64)
        .map_err(|_| FsError::Unavailable)?;
    id.push_str(prefix);
    const HEX: &[u8; 16] = b"0123456789abcdef";
    for value in random {
        id.push(HEX[(value >> 4) as usize] as char);
        id.push(HEX[(value & 15) as usize] as char);
    }
    Ok(id)
}
fn validate_path(path: &str, allow_root: bool) -> Result<()> {
    if path.is_empty() {
        return if allow_root {
            Ok(())
        } else {
            Err(FsError::Invalid("empty filesystem path"))
        };
    }
    if path.len() > MAX_PATH || path.starts_with('/') || path.contains(['\\', '\0']) {
        return Err(FsError::Invalid(
            "filesystem path must be bounded and relative",
        ));
    }
    let mut components = 0;
    for part in path.split('/') {
        components += 1;
        if part.is_empty()
            || part == "."
            || part == ".."
            || part.len() > 255
            || part.starts_with(PRIVATE_PREFIX)
            || components > MAX_COMPONENTS
        {
            return Err(FsError::Invalid(
                "unsafe or private filesystem path component",
            ));
        }
    }
    Ok(())
}
/// Fixed length-prefixed binary canonical argument form. The actual pinned
/// root ID/rights and ordered endpoints are bound; guest table reps never are.
fn argument_digest(root: &str, rights: FsRights, call: &OwnedCall) -> Digest {
    let mut storage = [0u8; 2 * MAX_PATH + 512];
    let mut length = 0;
    let mut add = |bytes: &[u8]| {
        storage[length..length + 8].copy_from_slice(&(bytes.len() as u64).to_be_bytes());
        length += 8;
        storage[length..length + bytes.len()].copy_from_slice(bytes);
        length += bytes.len();
    };
    add(b"tobkiri-fs-args-v1");
    add(root.as_bytes());
    add(&[u8::from(rights.read), u8::from(rights.write)]);
    let operation = match call.operation() {
        FsOperation::Open => 0,
        FsOperation::Read => 1,
        FsOperation::Write => 2,
        FsOperation::List => 3,
        FsOperation::Stat => 4,
        FsOperation::Rename => 5,
        FsOperation::Remove => 6,
        FsOperation::Mkdir => 7,
    };
    add(&[operation]);
    add(call.path().as_bytes());
    match call {
        OwnedCall::Read { offset, len, .. } => {
            add(&offset.to_be_bytes());
            add(&len.to_be_bytes());
        }
        OwnedCall::Write { data, append, .. } => {
            add(&[u8::from(*append)]);
            add(&(data.len() as u64).to_be_bytes());
            add(Digest::hash(data.as_bytes()).as_str().as_bytes());
        }
        OwnedCall::Rename { to, .. } => add(to.as_bytes()),
        _ => {}
    }
    Digest::hash(&storage[..length])
}
struct NativeAccess<'a> {
    root: &'a RegisteredRoot,
    protected: &'a [ProtectedRoot],
}

#[cfg(not(unix))]
mod native {
    use super::*;
    pub(super) fn directory_id(_: &File) -> Result<ObjectId> {
        Err(FsError::Unavailable)
    }
    pub(super) fn execute(
        _: &NativeAccess<'_>,
        _: &OwnedCall,
        _: &Arc<dyn AllocationBudget>,
        _: u64,
        _: &dyn Fn() -> Result<()>,
        _: &mut FsDisposition,
    ) -> Result<FsOutput> {
        Err(FsError::Unavailable)
    }
}

#[cfg(unix)]
mod native {
    use super::*;
    use std::{
        ffi::{CStr, CString},
        io::Write,
        os::{
            fd::{AsRawFd, FromRawFd, IntoRawFd},
            unix::fs::{FileExt, MetadataExt},
        },
    };

    pub(super) fn directory_id(file: &File) -> Result<ObjectId> {
        let metadata = file.metadata()?;
        if !metadata.is_dir() || metadata.nlink() == 0 {
            return Err(FsError::Denied("filesystem root is not a live directory"));
        }
        Ok(id(&metadata))
    }
    fn id(metadata: &std::fs::Metadata) -> ObjectId {
        ObjectId {
            device: metadata.dev(),
            inode: metadata.ino(),
        }
    }
    fn name(value: &str) -> Result<CString> {
        CString::new(value).map_err(|_| FsError::Invalid("NUL filesystem component"))
    }
    fn open_at(parent: &File, value: &str, flags: i32, mode: libc::mode_t) -> Result<File> {
        let value = name(value)?;
        let fd = unsafe {
            libc::openat(
                parent.as_raw_fd(),
                value.as_ptr(),
                flags | libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK,
                mode as libc::c_uint,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error().into());
        }
        Ok(unsafe { File::from_raw_fd(fd) })
    }
    fn open_dir(parent: &File, value: &str) -> Result<File> {
        open_at(parent, value, libc::O_RDONLY | libc::O_DIRECTORY, 0)
    }
    /// Inspect without opening a device/FIFO/socket. The descriptor opened
    /// afterwards is checked again; post-validation external editor races are
    /// outside the malicious-guest boundary documented above.
    fn open_child(parent: &File, leaf: &CStr) -> Result<Option<File>> {
        let mut status = std::mem::MaybeUninit::<libc::stat>::uninit();
        let result = unsafe {
            libc::fstatat(
                parent.as_raw_fd(),
                leaf.as_ptr(),
                status.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        if result != 0 {
            let error = io::Error::last_os_error();
            return if error.kind() == io::ErrorKind::NotFound {
                Ok(None)
            } else {
                Err(error.into())
            };
        }
        let status = unsafe { status.assume_init() };
        let kind = status.st_mode & libc::S_IFMT;
        if kind != libc::S_IFDIR && (kind != libc::S_IFREG || status.st_nlink != 1) {
            return Err(FsError::Denied(
                "filesystem object must be a directory or regular file without aliases",
            ));
        }
        let file = open_at(
            parent,
            leaf.to_str()
                .map_err(|_| FsError::Invalid("non-UTF8 filesystem component"))?,
            libc::O_RDONLY,
            0,
        )?;
        let metadata = file.metadata()?;
        if metadata.dev() != status.st_dev as u64 || metadata.ino() != status.st_ino as u64 {
            return Err(FsError::Denied(
                "filesystem endpoint changed during validation",
            ));
        }
        Ok(Some(file))
    }

    /// Ascend actual live descriptors; dev/inode compare supports an identity
    /// only while the original trusted descriptor remains pinned in registry.
    fn has_ancestor(directory: &File, wanted: ObjectId) -> Result<bool> {
        let mut current = open_dir(directory, ".")?;
        for _ in 0..=MAX_COMPONENTS * 2 {
            let object = directory_id(&current)?;
            if object == wanted {
                return Ok(true);
            }
            let parent = open_dir(&current, "..")?;
            let parent_id = directory_id(&parent)?;
            if object == parent_id {
                return Ok(false);
            }
            current = parent;
        }
        Err(FsError::Denied(
            "filesystem ancestry exceeds supported bound",
        ))
    }
    fn check_directory(
        access: &NativeAccess<'_>,
        directory: &File,
        changes_identity: bool,
    ) -> Result<()> {
        let object = directory_id(directory)?;
        if object.device != access.root.object.device
            || !has_ancestor(directory, access.root.object)?
        {
            return Err(FsError::Denied(
                "filesystem directory escaped pinned root or crossed a mount",
            ));
        }
        for protected in access.protected {
            // Walking a trusted protected descriptor is necessary even after a
            // workspace editor renamed it: a spelling prefix is not authority.
            if has_ancestor(directory, protected.object)?
                || (changes_identity && has_ancestor(&protected.file, object)?)
            {
                return Err(FsError::Denied(
                    "filesystem operation intersects a live Core subtree",
                ));
            }
        }
        Ok(())
    }
    fn check_object(
        access: &NativeAccess<'_>,
        file: &File,
        changes_identity: bool,
    ) -> Result<std::fs::Metadata> {
        let metadata = file.metadata()?;
        if metadata.dev() != access.root.object.device {
            return Err(FsError::Denied("filesystem mount crossing denied"));
        }
        if metadata.is_dir() {
            check_directory(access, file, changes_identity)?;
        } else if !metadata.is_file() || metadata.nlink() != 1 {
            return Err(FsError::Denied(
                "filesystem object must be a regular file without hardlink aliases",
            ));
        }
        Ok(metadata)
    }
    fn parent(access: &NativeAccess<'_>, path: &str) -> Result<(File, CString)> {
        let (prefix, leaf) = path.rsplit_once('/').unwrap_or(("", path));
        let directory = directory(access, prefix)?;
        Ok((directory, name(leaf)?))
    }
    fn directory(access: &NativeAccess<'_>, path: &str) -> Result<File> {
        let mut directory = open_dir(&access.root.file, ".")?;
        // The pinned root can legitimately be an ancestor of a protected Core
        // directory. Traversing that ancestor is allowed; final targets below
        // it and every destructive ancestor endpoint are checked separately.
        check_directory(access, &directory, false)?;
        if !path.is_empty() {
            for component in path.split('/') {
                directory = open_dir(&directory, component)?;
                check_directory(access, &directory, false)?;
            }
        }
        Ok(directory)
    }
    fn existing(access: &NativeAccess<'_>, path: &str, mutation: bool) -> Result<File> {
        if path.is_empty() {
            let directory = directory(access, "")?;
            check_object(access, &directory, mutation)?;
            return Ok(directory);
        }
        let (parent, leaf) = parent(access, path)?;
        let file = open_child(&parent, &leaf)?
            .ok_or_else(|| FsError::Io(io::Error::from(io::ErrorKind::NotFound)))?;
        check_object(access, &file, mutation)?;
        Ok(file)
    }
    fn optional_child(
        access: &NativeAccess<'_>,
        parent: &File,
        leaf: &CStr,
        mutation: bool,
    ) -> Result<Option<File>> {
        let file = open_child(parent, leaf)?;
        if let Some(file) = &file {
            check_object(access, file, mutation)?;
        }
        Ok(file)
    }

    pub(super) fn execute(
        access: &NativeAccess<'_>,
        call: &OwnedCall,
        budget: &Arc<dyn AllocationBudget>,
        output_limit: u64,
        active: &dyn Fn() -> Result<()>,
        disposition: &mut FsDisposition,
    ) -> Result<FsOutput> {
        active()?;
        match call {
            OwnedCall::Open => Err(FsError::Invalid("Open is not a native I/O operation")),
            OwnedCall::Read { path, offset, len } => {
                let file = existing(access, path, false)?;
                if !file.metadata()?.is_file() {
                    return Err(FsError::Denied("filesystem read requires a regular file"));
                }
                let mut output = CoreBuffer::builder(budget, *len as usize);
                while output.len() < *len as usize {
                    active()?;
                    let offset = offset
                        .checked_add(output.len() as u64)
                        .ok_or(FsError::Invalid("filesystem read offset overflow"))?;
                    let mut native_error = None;
                    let count =
                        output.write_chunk(CHUNK.min(*len as usize - output.len()), |target| {
                            match file.read_at(target, offset) {
                                Ok(count) => Ok(count),
                                Err(error) => {
                                    native_error = Some(error);
                                    Err(AllocationError::Unavailable)
                                }
                            }
                        });
                    if let Some(error) = native_error {
                        return Err(error.into());
                    }
                    if count? == 0 {
                        break;
                    }
                }
                active()?;
                Ok(FsOutput::Bytes(output.finish()?))
            }
            OwnedCall::Write { path, data, append } => {
                if *append {
                    append_file(access, path, data.as_bytes(), active, disposition)?;
                } else {
                    atomic_write(access, path, data.as_bytes(), active, disposition)?;
                }
                Ok(FsOutput::Unit)
            }
            OwnedCall::List { path } => list(access, path, budget, output_limit, active),
            OwnedCall::Stat { path } => {
                let file = existing(access, path, false)?;
                let metadata = file.metadata()?;
                active()?;
                let modified_unix_ms = metadata
                    .modified()
                    .ok()
                    .and_then(|n| n.duration_since(std::time::UNIX_EPOCH).ok())
                    .and_then(|n| u64::try_from(n.as_millis()).ok());
                let name = CoreValue::string(path.rsplit('/').next().unwrap_or(""), budget)?;
                Ok(FsOutput::Stat(FsStat {
                    name,
                    size: metadata.len(),
                    is_directory: metadata.is_dir(),
                    modified_unix_ms,
                }))
            }
            OwnedCall::Rename { from, to } => {
                let (source, source_name) = parent(access, from)?;
                let (destination, destination_name) = parent(access, to)?;
                let _source = optional_child(access, &source, &source_name, true)?
                    .ok_or_else(|| FsError::Io(io::Error::from(io::ErrorKind::NotFound)))?;
                let _replacement = optional_child(access, &destination, &destination_name, true)?;
                active()?;
                *disposition = FsDisposition::PartialUnknown;
                let result = unsafe {
                    libc::renameat(
                        source.as_raw_fd(),
                        source_name.as_ptr(),
                        destination.as_raw_fd(),
                        destination_name.as_ptr(),
                    )
                };
                if result != 0 {
                    return Err(FsError::CommitUnknown);
                }
                *disposition = FsDisposition::Committed;
                Ok(FsOutput::Unit)
            }
            OwnedCall::Remove { path } => {
                let (parent, leaf) = parent(access, path)?;
                let file = optional_child(access, &parent, &leaf, true)?
                    .ok_or_else(|| FsError::Io(io::Error::from(io::ErrorKind::NotFound)))?;
                let flags = if file.metadata()?.is_dir() {
                    libc::AT_REMOVEDIR
                } else {
                    0
                };
                active()?;
                *disposition = FsDisposition::PartialUnknown;
                let result = unsafe { libc::unlinkat(parent.as_raw_fd(), leaf.as_ptr(), flags) };
                if result != 0 {
                    return Err(FsError::CommitUnknown);
                }
                *disposition = FsDisposition::Committed;
                Ok(FsOutput::Unit)
            }
            OwnedCall::Mkdir { path } => {
                let (parent, leaf) = parent(access, path)?;
                if optional_child(access, &parent, &leaf, true)?.is_some() {
                    return Err(FsError::Denied("filesystem mkdir destination exists"));
                }
                active()?;
                *disposition = FsDisposition::PartialUnknown;
                let result = unsafe { libc::mkdirat(parent.as_raw_fd(), leaf.as_ptr(), 0o700) };
                if result != 0 {
                    return Err(FsError::CommitUnknown);
                }
                *disposition = FsDisposition::Committed;
                Ok(FsOutput::Unit)
            }
        }
    }

    fn list(
        access: &NativeAccess<'_>,
        path: &str,
        budget: &Arc<dyn AllocationBudget>,
        output_limit: u64,
        active: &dyn Fn() -> Result<()>,
    ) -> Result<FsOutput> {
        let directory = directory(access, path)?;
        // A fresh open description, not dup(): repeated list calls must not
        // share an advanced readdir offset with the pinned root descriptor.
        let scan = open_dir(&directory, ".")?;
        let fd = scan.into_raw_fd();
        let pointer = unsafe { libc::fdopendir(fd) };
        if pointer.is_null() {
            unsafe {
                libc::close(fd);
            }
            return Err(io::Error::last_os_error().into());
        }
        struct Stream(*mut libc::DIR);
        impl Drop for Stream {
            fn drop(&mut self) {
                unsafe {
                    libc::closedir(self.0);
                }
            }
        }
        let stream = Stream(pointer);
        let mut values = CoreValue::array_builder(budget, MAX_LIST)?;
        let mut bytes = 0usize;
        loop {
            active()?;
            // errno must distinguish the normal end of stream from an OS
            // error. The supported Unix targets expose different accessors.
            set_errno(0);
            let entry = unsafe { libc::readdir(stream.0) };
            if entry.is_null() {
                let error = io::Error::last_os_error();
                if error.raw_os_error().unwrap_or(0) != 0 {
                    return Err(error.into());
                }
                break;
            }
            let value = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }
                .to_str()
                .map_err(|_| FsError::Invalid("non-UTF8 filesystem entry"))?;
            if value == "." || value == ".." || value.starts_with(PRIVATE_PREFIX) {
                continue;
            }
            validate_path(value, false)?;
            // Protected children are omitted; accessing their names through a
            // guessed path is independently denied using the actual object.
            let child = open_child(&directory, &name(value)?)?
                .ok_or_else(|| FsError::Io(io::Error::from(io::ErrorKind::NotFound)))?;
            let metadata = match check_object(access, &child, false) {
                Err(FsError::Denied("filesystem operation intersects a live Core subtree")) => {
                    continue;
                }
                result => result?,
            };
            bytes = bytes
                .checked_add(value.len() + 32)
                .ok_or(FsError::Invalid("filesystem list overflow"))?;
            if bytes > MAX_LIST_BYTES || bytes as u64 > output_limit || values.len() == MAX_LIST {
                return Err(FsError::Denied("filesystem list exceeds bound"));
            }
            let mut digits = [0u8; 20];
            let mut number = metadata.len();
            let mut start = digits.len();
            loop {
                start -= 1;
                digits[start] = b'0' + (number % 10) as u8;
                number /= 10;
                if number == 0 {
                    break;
                }
            }
            let size = std::str::from_utf8(&digits[start..]).map_err(|_| FsError::Unavailable)?;
            values.push(CoreValue::object_shared(
                [
                    Ok(("name", CoreValue::string(value, budget)?)),
                    Ok(("is-dir", CoreValue::bool(metadata.is_dir(), budget)?)),
                    Ok(("size", CoreValue::string(size, budget)?)),
                ]
                .into_iter(),
                budget,
            )?)?;
        }
        active()?;
        Ok(FsOutput::Entries(values.finish()?))
    }
    #[cfg(any(target_os = "macos", target_os = "ios", target_os = "freebsd"))]
    fn set_errno(value: i32) {
        unsafe {
            *libc::__error() = value;
        }
    }
    #[cfg(any(target_os = "linux", target_os = "android"))]
    fn set_errno(value: i32) {
        unsafe {
            *libc::__errno_location() = value;
        }
    }
    #[cfg(not(any(
        target_os = "macos",
        target_os = "ios",
        target_os = "freebsd",
        target_os = "linux",
        target_os = "android"
    )))]
    fn set_errno(_: i32) {}

    fn append_file(
        access: &NativeAccess<'_>,
        path: &str,
        bytes: &[u8],
        active: &dyn Fn() -> Result<()>,
        disposition: &mut FsDisposition,
    ) -> Result<()> {
        let (parent, leaf) = parent(access, path)?;
        let prior = optional_child(access, &parent, &leaf, false)?;
        if prior
            .as_ref()
            .is_some_and(|file| file.metadata().map_or(true, |m| !m.is_file()))
        {
            return Err(FsError::Denied("filesystem append requires a regular file"));
        }
        active()?;
        // O_EXCL distinguishes creation from opening an existing file; a
        // dangling symlink or replaced endpoint never falls back to following.
        let flags = libc::O_WRONLY
            | libc::O_APPEND
            | if prior.is_some() {
                0
            } else {
                libc::O_CREAT | libc::O_EXCL
            };
        if prior.is_none() {
            *disposition = FsDisposition::PartialUnknown;
        }
        let file = open_at(
            &parent,
            leaf.to_str()
                .map_err(|_| FsError::Invalid("filesystem component encoding"))?,
            flags,
            0o600,
        );
        let mut file = file.map_err(|error| {
            if *disposition == FsDisposition::PartialUnknown {
                FsError::CommitUnknown
            } else {
                error
            }
        })?;
        let metadata = check_object(access, &file, false)?;
        if !metadata.is_file()
            || prior
                .as_ref()
                .is_some_and(|old| old.metadata().map_or(true, |m| id(&m) != id(&metadata)))
        {
            return Err(if *disposition == FsDisposition::PartialUnknown {
                FsError::CommitUnknown
            } else {
                FsError::Denied("filesystem append endpoint changed")
            });
        }
        for chunk in bytes.chunks(CHUNK) {
            if let Err(error) = active() {
                return Err(if *disposition == FsDisposition::Clean {
                    error
                } else {
                    FsError::CommitUnknown
                });
            }
            // write_all may partially write before returning Err. Set the
            // disposition before entering the syscall, never after its result.
            *disposition = FsDisposition::PartialUnknown;
            if file.write_all(chunk).is_err() {
                return Err(FsError::CommitUnknown);
            }
        }
        *disposition = FsDisposition::Committed;
        Ok(())
    }

    struct TempFile<'a> {
        parent: &'a File,
        name: CString,
        file: File,
        installed: bool,
    }
    impl TempFile<'_> {
        fn cleanup(&mut self) -> Result<()> {
            if !self.installed {
                let result =
                    unsafe { libc::unlinkat(self.parent.as_raw_fd(), self.name.as_ptr(), 0) };
                if result != 0 {
                    return Err(io::Error::last_os_error().into());
                }
                self.installed = true;
            }
            Ok(())
        }
    }
    impl Drop for TempFile<'_> {
        fn drop(&mut self) {
            if !self.installed {
                unsafe {
                    libc::unlinkat(self.parent.as_raw_fd(), self.name.as_ptr(), 0);
                }
            }
        }
    }
    fn atomic_write(
        access: &NativeAccess<'_>,
        path: &str,
        bytes: &[u8],
        active: &dyn Fn() -> Result<()>,
        disposition: &mut FsDisposition,
    ) -> Result<()> {
        let (parent, leaf) = parent(access, path)?;
        let prior = optional_child(access, &parent, &leaf, true)?;
        if prior
            .as_ref()
            .is_some_and(|file| file.metadata().map_or(true, |m| !m.is_file()))
        {
            return Err(FsError::Denied("filesystem write requires a regular file"));
        }
        active()?;
        // Private names are globally rejected in guest paths and omitted from
        // list. The namespace lock prevents other Core calls seeing this temp.
        let temp_name = opaque_id(PRIVATE_PREFIX)?;
        let file = open_at(
            &parent,
            &temp_name,
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
            0o600,
        )?;
        let mut temp = TempFile {
            parent: &parent,
            name: name(&temp_name)?,
            file,
            installed: false,
        };
        let result = (|| {
            check_object(access, &temp.file, false)?;
            for chunk in bytes.chunks(CHUNK) {
                active()?;
                temp.file.write_all(chunk)?;
            }
            temp.file.sync_all()?;
            // Revalidate both actual parent and replacement after temp production.
            // No Core operation can move them while holding this same lock.
            check_directory(access, &parent, false)?;
            let _replacement = optional_child(access, &parent, &leaf, true)?;
            active()?;
            *disposition = FsDisposition::PartialUnknown;
            let result = unsafe {
                libc::renameat(
                    parent.as_raw_fd(),
                    temp.name.as_ptr(),
                    parent.as_raw_fd(),
                    leaf.as_ptr(),
                )
            };
            if result != 0 {
                return Err(FsError::CommitUnknown);
            }
            temp.installed = true;
            *disposition = FsDisposition::Committed;
            Ok(())
        })();
        if result.is_err() && temp.cleanup().is_err() {
            *disposition = FsDisposition::PartialUnknown;
            return Err(FsError::CommitUnknown);
        }
        result
    }
}

#[cfg(test)]
#[path = "filesystem_tests.rs"]
mod tests;
