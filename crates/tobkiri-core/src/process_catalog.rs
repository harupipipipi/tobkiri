//! Trusted native process descriptor catalog.
//!
//! Registration retains the actual executable and working-directory `File`
//! objects supplied by trusted core. Every native fact is derived from
//! those retained descriptors: stat identity, a SHA-256 streamed from
//! offset zero and matched against a required expected digest, and a
//! before/after stability sample. Program ids and the two labels are
//! trusted non-secret metadata; argv/env are only borrowed by the private
//! commitment helper and are never copied, retained, or rendered.
//!
//! This is a bounded commitment foundation only. Descriptor custody, stat
//! identity, digest equality and a before/after sample do NOT prove
//! immutable bytes, freedom from concurrent writers or aliases, an atomic
//! content snapshot, effective access permission, actual caller/frame
//! liveness, current policy, approval, a jail, launch/reap/cleanup, or
//! executable and interpreter platform support. Mode bits are only an
//! admission fact, not an effective-access or exec-readiness proof.
//! Nothing here authorizes, prepares, or performs a physical launch;
//! `proc`/`pty` stay `Unavailable`.

use std::{fmt, fs::File, sync::Mutex};

#[cfg(unix)]
use sha2::{Digest as _, Sha256};
#[cfg(unix)]
use std::os::unix::fs::{FileExt, MetadataExt};

use tobkiri_canon::Digest;
#[cfg(unix)]
use tobkiri_gate::ControlKind;
use tobkiri_gate::{
    ControlReservation, CoreControl, LaunchBindingError, NativeIdentity, ProcessLaunchBinding,
    ProcessLaunchKey, ProcessLaunchSnapshot, PtyDimensions, ResourceLimits,
};

/// Published catalog entries per Core owner; append-only, never evicted.
const MAX_ENTRIES: usize = 16;
/// Accepted registration attempts per Core owner, including every failure.
const MAX_ATTEMPTS: u64 = 32;
/// Borrowed program id bound in bytes.
const MAX_PROGRAM_ID_BYTES: usize = 128;
/// Per-label bound in bytes for the two trusted display labels.
const MAX_LABEL_BYTES: usize = 128;
/// Maximum executable length admitted to the hash stage.
#[cfg(unix)]
const MAX_EXECUTABLE_BYTES: u64 = 64 * 1024 * 1024;
/// Streaming hash scratch bound; executables are never buffered whole.
const SCRATCH_BYTES: usize = 64 * 1024;
/// Lifetime hash-read work ceiling per Core owner: 1 GiB + 32 bytes.
#[cfg(unix)]
const MAX_HASH_READ_WORK: u64 = 1024 * 1024 * 1024 + 32;

/// Closed failure set for descriptor registration. Variants carry fixed
/// codes and messages only; no input bytes or secret data are ever echoed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProcessRegistrationError {
    /// Process subsystem unavailable or its owner state is poisoned.
    Unavailable,
    /// This platform cannot supply the required descriptor facts.
    UnsupportedPlatform,
    /// Program id is empty, over-long, or holds control characters.
    InvalidProgramId,
    /// A trusted label is empty, over-long, or holds control characters.
    InvalidLabel,
    /// The published catalog holds the maximum number of entries.
    CatalogFull,
    /// Program id is already published; published ids are never rebound.
    DuplicateProgramId,
    /// The per-Core registration attempt budget is exhausted.
    AttemptsExhausted,
    /// Descriptor metadata could not be read.
    MetadataFailed,
    /// Executable descriptor is not a regular file.
    NotRegularFile,
    /// Executable descriptor lacks executable mode bits.
    NotExecutable,
    /// Working-directory descriptor is not a directory.
    NotDirectory,
    /// Executable exceeds the admission length bound.
    ExecutableTooLarge,
    /// The retained descriptor failed a positional read.
    ReadFailed,
    /// Observed facts or visible length changed during the check.
    ContentChanged,
    /// Streamed SHA-256 differs from the required expected digest.
    DigestMismatch,
    /// Native identity payload could not be encoded.
    IdentityEncoding,
    /// Honest control-memory accounting denied a reservation.
    Capacity,
    /// The lifetime hash-read work reservation is exhausted.
    HashWorkExhausted,
    /// Operating system entropy could not be drawn for the owner key.
    Entropy,
}

impl ProcessRegistrationError {
    /// Stable machine-readable code for this failure.
    pub const fn code(self) -> &'static str {
        match self {
            Self::Unavailable => "PROCESS_UNAVAILABLE",
            Self::UnsupportedPlatform => "PROCESS_UNSUPPORTED_PLATFORM",
            Self::InvalidProgramId => "PROCESS_PROGRAM_ID",
            Self::InvalidLabel => "PROCESS_LABEL",
            Self::CatalogFull => "PROCESS_CATALOG_FULL",
            Self::DuplicateProgramId => "PROCESS_DUPLICATE_PROGRAM",
            Self::AttemptsExhausted => "PROCESS_ATTEMPTS_EXHAUSTED",
            Self::MetadataFailed => "PROCESS_METADATA",
            Self::NotRegularFile => "PROCESS_NOT_REGULAR_FILE",
            Self::NotExecutable => "PROCESS_NOT_EXECUTABLE",
            Self::NotDirectory => "PROCESS_NOT_DIRECTORY",
            Self::ExecutableTooLarge => "PROCESS_EXECUTABLE_TOO_LARGE",
            Self::ReadFailed => "PROCESS_READ_FAILED",
            Self::ContentChanged => "PROCESS_CONTENT_CHANGED",
            Self::DigestMismatch => "PROCESS_DIGEST_MISMATCH",
            Self::IdentityEncoding => "PROCESS_IDENTITY_ENCODING",
            Self::Capacity => "PROCESS_CAPACITY",
            Self::HashWorkExhausted => "PROCESS_HASH_WORK_EXHAUSTED",
            Self::Entropy => "PROCESS_ENTROPY",
        }
    }
}

impl fmt::Display for ProcessRegistrationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Unavailable => "process catalog is unavailable",
            Self::UnsupportedPlatform => {
                "process descriptor facts are unsupported on this platform"
            }
            Self::InvalidProgramId => {
                "program id is empty, over-long, or contains control characters"
            }
            Self::InvalidLabel => {
                "display label is empty, over-long, or contains control characters"
            }
            Self::CatalogFull => "process catalog is full",
            Self::DuplicateProgramId => "program id is already registered",
            Self::AttemptsExhausted => "process registration attempts are exhausted",
            Self::MetadataFailed => "descriptor metadata could not be read",
            Self::NotRegularFile => "executable is not a regular file",
            Self::NotExecutable => "executable lacks executable mode bits",
            Self::NotDirectory => "working directory is not a directory",
            Self::ExecutableTooLarge => "executable exceeds the length bound",
            Self::ReadFailed => "descriptor read failed",
            Self::ContentChanged => "descriptor changed during registration",
            Self::DigestMismatch => "executable does not match expected digest",
            Self::IdentityEncoding => "native identity encoding failed",
            Self::Capacity => "control memory capacity denied the reservation",
            Self::HashWorkExhausted => "hash read work budget is exhausted",
            Self::Entropy => "operating system randomness failed",
        })
    }
}
impl std::error::Error for ProcessRegistrationError {}

/// Bounded inline copy of already-validated non-secret text; never heap.
struct BoundedText<const N: usize> {
    bytes: [u8; N],
    len: usize,
}
impl<const N: usize> BoundedText<N> {
    fn parse(
        text: &str,
        error: ProcessRegistrationError,
    ) -> Result<Self, ProcessRegistrationError> {
        if text.is_empty() || text.len() > N || text.chars().any(char::is_control) {
            return Err(error);
        }
        let mut bytes = [0u8; N];
        bytes[..text.len()].copy_from_slice(text.as_bytes());
        Ok(Self {
            bytes,
            len: text.len(),
        })
    }
    fn as_str(&self) -> &str {
        std::str::from_utf8(&self.bytes[..self.len]).unwrap_or("")
    }
}

/// Stability and identity facts derived only from the retained descriptor.
/// Access time is deliberately absent: the hash itself would disturb it.
/// A caller-supplied inode tuple, pathname, or raw fd integer is never
/// accepted as proof; these facts exist only because core read them from
/// its own retained descriptor.
#[cfg(unix)]
#[derive(Clone, Copy, PartialEq, Eq)]
struct ObservedFacts {
    dev: u64,
    ino: u64,
    mode: u32,
    len: u64,
    mtime: i64,
    mtime_nsec: i64,
    ctime: i64,
    ctime_nsec: i64,
    kind: u8,
    created: Option<(i64, u32)>,
}

#[cfg(unix)]
const KIND_REGULAR: u8 = 1;
#[cfg(unix)]
const KIND_DIRECTORY: u8 = 2;
#[cfg(unix)]
const KIND_OTHER: u8 = 3;
#[cfg(unix)]
const ROLE_EXECUTABLE: u8 = 1;
#[cfg(unix)]
const ROLE_CWD: u8 = 2;
/// Versioned, domain-separated identity scheme for the payload encoding.
#[cfg(unix)]
const IDENTITY_METHOD: &str = "tobkiri.unix-descriptor-facts";
#[cfg(unix)]
const IDENTITY_VERSION: u64 = 1;
#[cfg(unix)]
const IDENTITY_MAGIC: [u8; 8] = *b"TKPDFCT1";
/// Fixed payload length; well under the gate's 256-byte identity cap.
#[cfg(unix)]
const IDENTITY_LEN: usize = 83;

/// Read the full fact set from a retained descriptor. Any metadata failure
/// fails closed; a partial fact set is never encoded or compared.
#[cfg(unix)]
fn observe(file: &File) -> Result<ObservedFacts, ProcessRegistrationError> {
    let metadata = file
        .metadata()
        .map_err(|_| ProcessRegistrationError::MetadataFailed)?;
    let kind = if metadata.is_file() {
        KIND_REGULAR
    } else if metadata.is_dir() {
        KIND_DIRECTORY
    } else {
        KIND_OTHER
    };
    let created = metadata
        .created()
        .ok()
        .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|span| {
            (
                i64::try_from(span.as_secs()).unwrap_or(i64::MAX),
                span.subsec_nanos(),
            )
        });
    Ok(ObservedFacts {
        dev: metadata.dev(),
        ino: metadata.ino(),
        mode: metadata.mode(),
        len: metadata.len(),
        mtime: metadata.mtime(),
        mtime_nsec: metadata.mtime_nsec(),
        ctime: metadata.ctime(),
        ctime_nsec: metadata.ctime_nsec(),
        kind,
        created,
    })
}

/// Encode facts as a fixed-width, versioned, domain-separated identity
/// payload: exactly [`IDENTITY_LEN`] inline bytes, no heap formatting.
#[cfg(unix)]
fn encode_identity(
    role: u8,
    facts: &ObservedFacts,
) -> Result<NativeIdentity, ProcessRegistrationError> {
    let mut bytes = [0u8; IDENTITY_LEN];
    let mut at = 0usize;
    let mut push = |chunk: &[u8]| {
        bytes[at..at + chunk.len()].copy_from_slice(chunk);
        at += chunk.len();
    };
    push(&IDENTITY_MAGIC);
    push(&[role]);
    push(&facts.dev.to_le_bytes());
    push(&facts.ino.to_le_bytes());
    push(&facts.mode.to_le_bytes());
    push(&facts.len.to_le_bytes());
    push(&facts.mtime.to_le_bytes());
    push(&facts.mtime_nsec.to_le_bytes());
    push(&facts.ctime.to_le_bytes());
    push(&facts.ctime_nsec.to_le_bytes());
    push(&[facts.kind]);
    match facts.created {
        Some((seconds, nanos)) => {
            push(&[1u8]);
            push(&seconds.to_le_bytes());
            push(&nanos.to_le_bytes());
        }
        None => push(&[0u8; 13]),
    }
    debug_assert_eq!(at, IDENTITY_LEN);
    NativeIdentity::new(IDENTITY_METHOD, IDENTITY_VERSION, &bytes)
        .map_err(|_| ProcessRegistrationError::IdentityEncoding)
}

/// Encode raw SHA-256 output as canonical `sha256:` digest text using a
/// fixed inline lowercase encoding; `Digest::parse` validates the result.
#[cfg(unix)]
fn encode_digest(hash: [u8; 32]) -> Result<Digest, ProcessRegistrationError> {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut text = [0u8; 71];
    text[..7].copy_from_slice(b"sha256:");
    for (index, byte) in hash.iter().enumerate() {
        text[7 + index * 2] = HEX[usize::from(byte >> 4)];
        text[8 + index * 2] = HEX[usize::from(byte & 15)];
    }
    let text =
        std::str::from_utf8(&text).map_err(|_| ProcessRegistrationError::IdentityEncoding)?;
    Digest::parse(text).map_err(|_| ProcessRegistrationError::IdentityEncoding)
}

/// Stream SHA-256 over exactly `initial_len` bytes plus one detection byte
/// from offset zero using `read_at`, so no alias can redirect the stream
/// through the shared cursor. Total exposure never exceeds the reserved
/// `initial_len + 1`. Every read error fails closed, including
/// `Interrupted`: no read is ever retried outside its reservation. A short
/// stream or a surviving extra byte both reject as an observed change.
#[cfg(unix)]
fn stream_sha256(
    executable: &File,
    initial_len: u64,
    scratch: &mut [u8; SCRATCH_BYTES],
    max_step: usize,
    after_step: &mut dyn FnMut() -> Result<(), ProcessRegistrationError>,
) -> Result<Digest, ProcessRegistrationError> {
    let target = initial_len
        .checked_add(1)
        .ok_or(ProcessRegistrationError::ExecutableTooLarge)?;
    let step_cap = max_step.clamp(1, SCRATCH_BYTES);
    let mut hasher = Sha256::new();
    let mut offset = 0u64;
    while offset < target {
        let remaining = usize::try_from(target - offset)
            .map_err(|_| ProcessRegistrationError::ExecutableTooLarge)?;
        let step = remaining.min(scratch.len()).min(step_cap);
        let read = executable
            .read_at(&mut scratch[..step], offset)
            .map_err(|_| ProcessRegistrationError::ReadFailed)?;
        after_step()?;
        if read == 0 {
            break;
        }
        hasher.update(&scratch[..read]);
        offset = offset
            .checked_add(u64::try_from(read).map_err(|_| ProcessRegistrationError::ReadFailed)?)
            .ok_or(ProcessRegistrationError::ReadFailed)?;
    }
    if offset != initial_len {
        return Err(ProcessRegistrationError::ContentChanged);
    }
    encode_digest(hasher.finalize().into())
}

/// One immutable published entry: the retained descriptors plus every
/// Core-derived fact committed on their behalf. Entries are never cloned,
/// rebound, replaced, or evicted before owner teardown.
#[allow(dead_code)]
struct CatalogEntry {
    program_id: BoundedText<MAX_PROGRAM_ID_BYTES>,
    catalog_label: BoundedText<MAX_LABEL_BYTES>,
    cwd_label: BoundedText<MAX_LABEL_BYTES>,
    executable_digest: Digest,
    executable_identity: NativeIdentity,
    cwd_identity: NativeIdentity,
    /// Retained executable descriptor; custody itself is the point of the
    /// field and no accessor ever exposes it.
    executable: File,
    /// Retained working-directory descriptor for the owner's lifetime.
    cwd: File,
    /// Entry storage permit, released only after everything above drops.
    _entry_permit: ControlReservation,
}

struct CatalogState {
    /// Fixed-capacity inline slots; no heap growth or replacement path.
    entries: [Option<CatalogEntry>; MAX_ENTRIES],
    /// Accepted attempts so far, including malformed and failing attempts.
    attempts: u64,
    /// Lifetime non-refundable hash-read bytes reserved so far.
    hash_work_reserved: u64,
    /// Effective work ceiling - the production constant; private tests may
    /// only lower this field in place, never raise it.
    hash_work_limit: u64,
}

/// One bounded process owner per Core: catalog slots, the move-only launch
/// key and every retained descriptor held together. Attempt and hash-work
/// reservations are non-refundable for the owner's whole lifetime.
pub(crate) struct ProcessCatalog {
    state: Mutex<CatalogState>,
    /// Move-only per-Core commitment key, zeroized on drop; only the
    /// private commitment helper ever reads it.
    #[allow(dead_code)]
    key: ProcessLaunchKey,
    /// This Core's shared control account for honest memory permits.
    control: CoreControl,
    /// Owner storage permit, released only after the state, descriptors
    /// and key above have been destroyed.
    _catalog_permit: ControlReservation,
}

impl ProcessCatalog {
    /// Create the bounded owner with precharged honest storage. Entropy or
    /// capacity failure is process-only; unsupported platforms fail closed.
    pub(crate) fn new(control: CoreControl) -> Result<Self, ProcessRegistrationError> {
        #[cfg(not(unix))]
        {
            let _ = control;
            return Err(ProcessRegistrationError::UnsupportedPlatform);
        }
        #[cfg(unix)]
        {
            let key =
                ProcessLaunchKey::generate().map_err(|_| ProcessRegistrationError::Entropy)?;
            let permit = control
                .reserve(
                    ControlKind::Catalog,
                    u64::try_from(std::mem::size_of::<Self>())
                        .map_err(|_| ProcessRegistrationError::Capacity)?,
                )
                .map_err(|_| ProcessRegistrationError::Capacity)?;
            Ok(Self {
                state: Mutex::new(CatalogState {
                    entries: std::array::from_fn(|_| None),
                    attempts: 0,
                    hash_work_reserved: 0,
                    hash_work_limit: MAX_HASH_READ_WORK,
                }),
                key,
                control,
                _catalog_permit: permit,
            })
        }
    }

    /// Register the actual owned executable descriptor and a separate owned
    /// cwd descriptor under `program_id`. One attempt is consumed before
    /// any validation; every failure path preserves all earlier valid
    /// entries and publishes no partial entry.
    pub(crate) fn register(
        &self,
        program_id: &str,
        executable: File,
        expected_executable_digest: &Digest,
        catalog_label: &str,
        cwd: File,
        cwd_label: &str,
    ) -> Result<(), ProcessRegistrationError> {
        self.register_inner(
            program_id,
            executable,
            expected_executable_digest,
            catalog_label,
            cwd,
            cwd_label,
            SCRATCH_BYTES,
            &mut || Ok(()),
        )
    }

    /// Admission entry: consume one attempt under the owner lock, then run
    /// the pipeline. `max_step`/`after_step` are a private deterministic
    /// seam for tests; production always passes the full scratch step and
    /// a no-op observer.
    #[allow(clippy::too_many_arguments)]
    fn register_inner(
        &self,
        program_id: &str,
        executable: File,
        expected_executable_digest: &Digest,
        catalog_label: &str,
        cwd: File,
        cwd_label: &str,
        max_step: usize,
        after_step: &mut dyn FnMut() -> Result<(), ProcessRegistrationError>,
    ) -> Result<(), ProcessRegistrationError> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| ProcessRegistrationError::Unavailable)?;
        if state.attempts >= MAX_ATTEMPTS {
            return Err(ProcessRegistrationError::AttemptsExhausted);
        }
        state.attempts += 1;
        self.register_admitted(
            &self.control,
            &mut state,
            program_id,
            executable,
            expected_executable_digest,
            catalog_label,
            cwd,
            cwd_label,
            max_step,
            after_step,
        )
    }

    /// Unix admission pipeline: validate text and facts, reserve lifetime
    /// hash work and honest memory, stream and verify, then publish into a
    /// free slot. A failure anywhere leaves no partial entry.
    #[cfg(unix)]
    #[allow(clippy::too_many_arguments)]
    fn register_admitted(
        &self,
        control: &CoreControl,
        state: &mut CatalogState,
        program_id: &str,
        executable: File,
        expected_executable_digest: &Digest,
        catalog_label: &str,
        cwd: File,
        cwd_label: &str,
        max_step: usize,
        after_step: &mut dyn FnMut() -> Result<(), ProcessRegistrationError>,
    ) -> Result<(), ProcessRegistrationError> {
        let program_id = BoundedText::<MAX_PROGRAM_ID_BYTES>::parse(
            program_id,
            ProcessRegistrationError::InvalidProgramId,
        )?;
        let catalog_label = BoundedText::<MAX_LABEL_BYTES>::parse(
            catalog_label,
            ProcessRegistrationError::InvalidLabel,
        )?;
        let cwd_label = BoundedText::<MAX_LABEL_BYTES>::parse(
            cwd_label,
            ProcessRegistrationError::InvalidLabel,
        )?;
        if state
            .entries
            .iter()
            .flatten()
            .any(|entry| entry.program_id.as_str() == program_id.as_str())
        {
            return Err(ProcessRegistrationError::DuplicateProgramId);
        }
        if state.entries.iter().all(|slot| slot.is_some()) {
            return Err(ProcessRegistrationError::CatalogFull);
        }
        let executable_facts = observe(&executable)?;
        if executable_facts.kind != KIND_REGULAR {
            return Err(ProcessRegistrationError::NotRegularFile);
        }
        if executable_facts.mode & 0o111 == 0 {
            return Err(ProcessRegistrationError::NotExecutable);
        }
        if executable_facts.len > MAX_EXECUTABLE_BYTES {
            return Err(ProcessRegistrationError::ExecutableTooLarge);
        }
        let cwd_facts = observe(&cwd)?;
        if cwd_facts.kind != KIND_DIRECTORY {
            return Err(ProcessRegistrationError::NotDirectory);
        }
        // Lifetime hash-read work, reserved atomically with owner state
        // BEFORE the first read: initial length plus one detection byte.
        let work = executable_facts
            .len
            .checked_add(1)
            .ok_or(ProcessRegistrationError::ExecutableTooLarge)?;
        state.hash_work_reserved = state
            .hash_work_reserved
            .checked_add(work)
            .filter(|total| *total <= state.hash_work_limit)
            .ok_or(ProcessRegistrationError::HashWorkExhausted)?;
        // Honest memory accounting: entry storage plus hash scratch are
        // precharged before any read; no new account or cap change.
        let entry_permit = control
            .reserve(
                ControlKind::Registration,
                u64::try_from(std::mem::size_of::<CatalogEntry>())
                    .map_err(|_| ProcessRegistrationError::Capacity)?,
            )
            .map_err(|_| ProcessRegistrationError::Capacity)?;
        let _scratch_permit = control
            .reserve(
                ControlKind::Registration,
                u64::try_from(SCRATCH_BYTES).map_err(|_| ProcessRegistrationError::Capacity)?,
            )
            .map_err(|_| ProcessRegistrationError::Capacity)?;
        let mut scratch = [0u8; SCRATCH_BYTES];
        let executable_digest = stream_sha256(
            &executable,
            executable_facts.len,
            &mut scratch,
            max_step,
            after_step,
        )?;
        if executable_digest != *expected_executable_digest {
            return Err(ProcessRegistrationError::DigestMismatch);
        }
        // Re-observe both descriptors after the stream: any drift in the
        // recorded stability facts rejects the attempt. Access time is
        // never a stability condition.
        if observe(&executable)? != executable_facts || observe(&cwd)? != cwd_facts {
            return Err(ProcessRegistrationError::ContentChanged);
        }
        let executable_identity = encode_identity(ROLE_EXECUTABLE, &executable_facts)?;
        let cwd_identity = encode_identity(ROLE_CWD, &cwd_facts)?;
        let slot = state
            .entries
            .iter_mut()
            .find(|slot| slot.is_none())
            .ok_or(ProcessRegistrationError::CatalogFull)?;
        *slot = Some(CatalogEntry {
            program_id,
            catalog_label,
            cwd_label,
            executable_digest,
            executable_identity,
            cwd_identity,
            executable,
            cwd,
            _entry_permit: entry_permit,
        });
        Ok(())
    }

    /// Non-Unix platforms validate input text uniformly, then fail closed;
    /// no facts can be derived and nothing is ever published.
    #[cfg(not(unix))]
    #[allow(clippy::too_many_arguments)]
    fn register_admitted(
        &self,
        _control: &CoreControl,
        state: &mut CatalogState,
        program_id: &str,
        _executable: File,
        _expected_executable_digest: &Digest,
        catalog_label: &str,
        _cwd: File,
        cwd_label: &str,
        _max_step: usize,
        _after_step: &mut dyn FnMut() -> Result<(), ProcessRegistrationError>,
    ) -> Result<(), ProcessRegistrationError> {
        let _ = BoundedText::<MAX_PROGRAM_ID_BYTES>::parse(
            program_id,
            ProcessRegistrationError::InvalidProgramId,
        )?;
        let _ = BoundedText::<MAX_LABEL_BYTES>::parse(
            catalog_label,
            ProcessRegistrationError::InvalidLabel,
        )?;
        let _ = BoundedText::<MAX_LABEL_BYTES>::parse(
            cwd_label,
            ProcessRegistrationError::InvalidLabel,
        )?;
        let _ = (
            state.hash_work_reserved,
            state.hash_work_limit,
            &state.entries,
        );
        Err(ProcessRegistrationError::UnsupportedPlatform)
    }

    /// Internal borrowed commitment helper for a later launch slice. Builds
    /// a snapshot solely from this owner's recorded digest, identities and
    /// labels plus the caller's borrowed argv/env/limits, then commits it
    /// under this owner's key. The result is an inert commitment to
    /// recorded facts only: it proves no caller identity, approval,
    /// permission, content freshness, or launch authority.
    #[allow(dead_code)]
    fn commit_launch(
        &self,
        program_id: &str,
        argv: &[String],
        env: &[(String, String)],
        limits: &ResourceLimits,
        pty: Option<PtyDimensions>,
    ) -> Result<ProcessLaunchBinding, LaunchBindingError> {
        let state = self
            .state
            .lock()
            .map_err(|_| LaunchBindingError::Internal)?;
        let entry = state
            .entries
            .iter()
            .flatten()
            .find(|entry| entry.program_id.as_str() == program_id)
            .ok_or(LaunchBindingError::Mismatch)?;
        let snapshot = launch_snapshot(entry, argv, env, limits, pty);
        ProcessLaunchBinding::commit(&self.key, &snapshot)
    }
}

/// Borrow a commitment view over one registered entry plus caller-supplied
/// argv/env/limits/pty. Nothing is copied or retained by the catalog.
#[allow(dead_code)]
fn launch_snapshot<'a>(
    entry: &'a CatalogEntry,
    argv: &'a [String],
    env: &'a [(String, String)],
    limits: &'a ResourceLimits,
    pty: Option<PtyDimensions>,
) -> ProcessLaunchSnapshot<'a> {
    ProcessLaunchSnapshot {
        program_id: entry.program_id.as_str(),
        executable_digest: &entry.executable_digest,
        executable_identity: &entry.executable_identity,
        argv,
        env,
        cwd_identity: &entry.cwd_identity,
        pty,
        limits,
        catalog_label: entry.catalog_label.as_str(),
        cwd_label: entry.cwd_label.as_str(),
    }
}

#[cfg(all(test, unix))]
#[path = "process_catalog_tests.rs"]
mod tests;
