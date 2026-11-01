//! Core's bounded read-only action at a reached native resolution operation.
//!
//! A trusted native adapter moves its actual resolver result here. Its original
//! allocations are retained, not rebuilt from client paths, pins or booleans.
//! Core selects a member of that result's declaration and owns the operation's
//! active state, invocation, jailed open, verification and physical cleanup.
//! This is separate from ordinary Profile permissions and early Setup reads.

use crate::{Core, CoreError};
use sha2::{Digest as _, Sha256};
use std::{
    collections::BTreeMap,
    io::{self, Read},
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU8, Ordering},
    },
};
use tobkiri_canon::{ControlHandle, CoreBuffer, Digest, MAX_BYTES};
use tobkiri_gate::{
    CapacityKind, ControlKind, ControlReservation, NativeSetupInvocation, NativeSetupReservation,
};
use tobkiri_wasm::{NativeDataIo, NativeDataIoError, NativeInspectionOutput, NativeSetupStore};

#[path = "native_artifact_open_unix.rs"]
mod opening;
use opening::{OpenFailure, OpenedArtifact};

/// Borrowed declaration facts from the actual native resolver result.
/// A digest alone never authorizes access to a path supplied independently.
pub trait NativeArtifactDeclaration {
    /// Borrow the canonical SHA-256 identity recorded by the native resolver.
    fn digest(&self) -> &str;
}

/// Trusted native adapter over an already reached, owned resolver result.
///
/// Implementations must expose that same result's root and declaration map;
/// they must not reconstruct a selection from guest/client input. Construction
/// and invocation belong to the native composition, never a guest import.
pub trait NativeDeclaredArtifactSource: Send + Sync + 'static {
    /// The actual resolver's declaration record, without a copied catalog.
    type Declaration: NativeArtifactDeclaration;
    /// Borrow the selected root; its concrete FD binding is established at open.
    fn root(&self) -> &Path;
    /// Borrow the same resolved owner's map from declared names to identities.
    fn declarations(&self) -> &BTreeMap<String, Self::Declaration>;
}

/// No source bytes or host path are disclosed by these error descriptions.
#[derive(Debug, thiserror::Error)]
pub enum NativeArtifactError {
    #[error("native declared-artifact execution: {0}")]
    Execution(#[from] NativeDataIoError),
    #[error("native declared-artifact admission: {0}")]
    Core(#[from] CoreError),
    #[error("native artifact source IO failed")]
    SourceIo(#[source] io::Error),
    #[error("native artifact is not an eligible declared target")]
    Denied,
    #[error("native artifact changed or failed its declaration verification")]
    Integrity,
    #[error("native artifact exceeds the original DATA envelope")]
    TooLarge,
}

const READY: u8 = 0;
const READING: u8 = 1;
const CLOSED: u8 = 2;

struct Owned<S> {
    source: Option<S>,
    files: Option<OpenedArtifact>,
    failure: Option<NativeArtifactError>,
}
struct Work<S> {
    // Actual native source and descriptors outlive an abandoned read future.
    owned: Mutex<Owned<S>>,
    phase: AtomicU8,
    invocation: NativeSetupInvocation,
    _core: Arc<Core>,
}
struct Funding {
    _native: NativeSetupReservation,
    _control: ControlReservation,
}
type Held<S> = ControlHandle<Work<S>>;

/// Move-only native action. Only Core constructs its active operation owner.
///
/// One invocation, Store, deadline, 64MiB account and physical IO slot serve all
/// reads. This is not a transferable filesystem capability or an FD registry.
pub struct NativeArtifactInspection<S: NativeDeclaredArtifactSource> {
    work: Held<S>,
    store: Option<NativeSetupStore<()>>,
    io: NativeDataIo,
}

impl Core {
    /// Admit the actual resolved native source for declared-artifact inspection.
    /// Requires the host's entered async runtime, not a new runtime per read.
    pub fn inspect_declared_artifacts<S: NativeDeclaredArtifactSource>(
        self: &Arc<Self>,
        source: S,
    ) -> Result<NativeArtifactInspection<S>, NativeArtifactError> {
        let invocation = self.native_artifact_invocation()?;
        let bytes = u64::try_from(Held::<S>::storage_bytes::<Funding>())
            .ok()
            // std's pinned Darwin Mutex lazily allocates one pthread_mutex_t.
            // Cover that requested storage before either allocation/first lock.
            .and_then(|n| n.checked_add(size_of::<libc::pthread_mutex_t>() as u64))
            .ok_or(NativeDataIoError::Capacity)?;
        let native = invocation
            .reserve(CapacityKind::VfsBuffer, bytes)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let control = invocation
            .control()
            .reserve(ControlKind::Frame, bytes)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let work = Held::try_new_precharged(
            Work {
                owned: Mutex::new(Owned {
                    source: Some(source),
                    files: None,
                    failure: None,
                }),
                phase: AtomicU8::new(READY),
                invocation: invocation.clone(),
                _core: self.clone(),
            },
            Funding {
                _native: native,
                _control: control,
            },
        )
        .map_err(|_| NativeDataIoError::Capacity)?;
        let engine = tobkiri_wasm::WasmEngine::new_with_control(invocation.control())
            .map_err(|_| NativeDataIoError::Unavailable)?;
        let mut store = engine
            .native_setup_store(invocation, ())
            .map_err(|_| NativeDataIoError::Unavailable)?;
        let io = store.bind_native_data_io().map_err(|error| {
            error
                .downcast_ref::<NativeDataIoError>()
                .copied()
                .unwrap_or(NativeDataIoError::Unavailable)
        })?;
        Ok(NativeArtifactInspection {
            work,
            store: Some(store),
            io,
        })
    }
}

impl<S: NativeDeclaredArtifactSource> NativeArtifactInspection<S> {
    /// Pure borrowed facts for the original native domain validation. No IO or
    /// permission decision is delegated to this nonescaping callback.
    pub fn with_source<T>(
        &self,
        view: impl for<'source> FnOnce(&'source S) -> T,
    ) -> Result<T, NativeArtifactError> {
        self.require_ready()?;
        let owned = self
            .work
            .get()
            .owned
            .lock()
            .map_err(|_| NativeDataIoError::Unavailable)?;
        let source = owned.source.as_ref().ok_or(NativeArtifactError::Denied)?;
        let result = view(source);
        drop(owned);
        self.require_ready()?;
        Ok(result)
    }

    fn require_ready(&self) -> Result<(), NativeArtifactError> {
        let deadline = self
            .work
            .get()
            .invocation
            .deadline()
            .map_err(|_| NativeDataIoError::Unavailable)?;
        if std::time::Instant::now() >= deadline {
            return Err(NativeDataIoError::Deadline.into());
        }
        if self
            .store
            .as_ref()
            .is_some_and(|store| store.cancellation().is_cancelled())
        {
            return Err(NativeDataIoError::Cancelled.into());
        }
        match self.work.get().phase.load(Ordering::Acquire) {
            READY => Ok(()),
            READING => Err(NativeDataIoError::Busy.into()),
            _ => Err(NativeDataIoError::Retired.into()),
        }
    }

    /// Read a name selected FROM this actual source's declaration, confined to
    /// its root as opened at this reachpoint. No independent path/digest pair.
    pub async fn read(&mut self, relative: &str) -> Result<CoreBuffer, NativeArtifactError> {
        self.require_ready()?;
        if !self.with_source(|source| source.declarations().contains_key(relative))? {
            return Err(NativeArtifactError::Denied);
        }
        // New queued key storage is prepaid under the original graph owner.
        let store = self.store.as_ref().ok_or(NativeDataIoError::Retired)?;
        let key = CoreBuffer::copy_from(relative.as_bytes(), store.allocation_owners().graph())
            .map_err(|_| NativeDataIoError::Capacity)?;
        self.work
            .get()
            .phase
            .compare_exchange(READY, READING, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| NativeDataIoError::Busy)?;
        let held = self.work.clone();
        // On cancellation/error the original worker retains source/files and
        // phase stays non-admitting. finish() must drive physical completion.
        let bytes = self
            .io
            .inspect_retaining(
                held.project(|work| work as &(dyn std::any::Any + Send + Sync)),
                move |output| inspect_selected(&held, &key, output),
            )
            .await?;
        let mut owned = self
            .work
            .get()
            .owned
            .lock()
            .map_err(|_| NativeDataIoError::Unavailable)?;
        // inspect() returns successful DATA only AFTER its physical Join.
        drop(owned.files.take());
        let failure = owned.failure.take();
        self.work.get().phase.store(READY, Ordering::Release);
        match failure {
            Some(error) => Err(error),
            None => Ok(bytes),
        }
    }

    /// Stop admission, await the original physical Joins, then recover the same
    /// moved resolver result. Never recreate it by rereading a mutable source.
    pub async fn finish(mut self) -> Result<S, NativeArtifactError> {
        let deadline = self
            .work
            .get()
            .invocation
            .deadline()
            .map_err(|_| NativeDataIoError::Unavailable);
        let cancelled = self
            .store
            .as_ref()
            .is_some_and(|store| store.cancellation().is_cancelled());
        self.work.get().phase.store(CLOSED, Ordering::Release);
        if let Some(store) = self.store.take() {
            store.retire();
            drop(store);
        }
        self.io.retire_and_wait().await?;
        // Cleanup still waits after expiry/cancellation. Its successful Join
        // does not renew the action's deadline or authorize a late result.
        if cancelled {
            return Err(NativeDataIoError::Cancelled.into());
        }
        if std::time::Instant::now() >= deadline? {
            return Err(NativeDataIoError::Deadline.into());
        }
        let mut owned = self
            .work
            .get()
            .owned
            .lock()
            .map_err(|_| NativeDataIoError::Unavailable)?;
        drop(owned.files.take());
        owned.source.take().ok_or(NativeArtifactError::Denied)
    }
}
impl<S: NativeDeclaredArtifactSource> Drop for NativeArtifactInspection<S> {
    fn drop(&mut self) {
        self.work.get().phase.store(CLOSED, Ordering::Release);
        if let Some(store) = self.store.take() {
            store.retire();
            drop(store);
        }
        self.io.retire();
        // Dropping an observer cannot release a worker's source/file/funding.
        // Its clone of work remains owned by the original execution kernel.
    }
}

fn inspect_selected<S: NativeDeclaredArtifactSource>(
    held: &Held<S>,
    key: &CoreBuffer,
    output: &mut NativeInspectionOutput<'_>,
) -> Result<(), NativeDataIoError> {
    if !held.get().invocation.same_invocation(output.invocation())
        || held.get().phase.load(Ordering::Acquire) != READING
    {
        return Err(output.reject_execution(NativeDataIoError::Retired));
    }
    output.charge_work(1)?;
    let mut owned = held
        .get()
        .owned
        .lock()
        .map_err(|_| output.reject_execution(NativeDataIoError::Unavailable))?;
    let Owned {
        source,
        files,
        failure,
    } = &mut *owned;
    let source = source
        .as_ref()
        .ok_or_else(|| output.reject_execution(NativeDataIoError::Retired))?;
    let relative = key
        .as_str()
        .map_err(|_| output.reject_execution(NativeDataIoError::Unavailable))?;
    let Some((selected, declaration)) = source.declarations().get_key_value(relative) else {
        *failure = Some(NativeArtifactError::Denied);
        return Ok(());
    };
    let digest = declaration.digest();
    if !valid_digest(digest) {
        *failure = Some(NativeArtifactError::Integrity);
        return Ok(());
    }
    use std::os::unix::ffi::OsStrExt;
    let relative = Path::new(selected);
    let maximum = relative
        .components()
        .map(|c| c.as_os_str().as_bytes().len())
        .fold(source.root().as_os_str().as_bytes().len(), usize::max)
        .checked_add(1)
        .ok_or_else(|| output.reject_execution(NativeDataIoError::Capacity))?;
    // Scratch is privately staged and rewound; no pathname bytes are published.
    let opened = output.write_chunk_checked(maximum, |scratch, checkpoint| {
        *files = Some(
            opening::open_selected_file(source.root(), relative, scratch, &checkpoint)
                .map_err(map_open_failure)?,
        );
        Ok::<_, NativeArtifactError>(0)
    })?;
    if let Err(error) = opened {
        return record_failure(output, failure, error);
    }
    let opened = files
        .as_ref()
        .ok_or_else(|| output.reject_execution(NativeDataIoError::CompletionUnknown))?;
    let length = match usize::try_from(opened.before.len()) {
        Ok(length) if length <= MAX_BYTES => length,
        _ => return record_failure(output, failure, NativeArtifactError::TooLarge),
    };
    let read = output.write_chunk_checked(length, |tail, checkpoint| {
        read_verified(opened, tail, digest, &checkpoint)
    })?;
    match read {
        Ok(_) => Ok(()),
        Err(error) => record_failure(output, failure, error),
    }
}

fn record_failure(
    output: &mut NativeInspectionOutput<'_>,
    failure: &mut Option<NativeArtifactError>,
    error: NativeArtifactError,
) -> Result<(), NativeDataIoError> {
    match error {
        NativeArtifactError::Execution(error) => Err(output.reject_execution(error)),
        error => {
            *failure = Some(error);
            Ok(())
        }
    }
}
fn map_open_failure(error: OpenFailure) -> NativeArtifactError {
    match error {
        OpenFailure::InvalidTarget => NativeArtifactError::Denied,
        OpenFailure::SourceIo(error) => NativeArtifactError::SourceIo(error),
        OpenFailure::Execution(error) => NativeArtifactError::Execution(error),
    }
}
fn valid_digest(digest: &str) -> bool {
    digest.len() == 71
        && digest.starts_with("sha256:")
        && digest.as_bytes()[7..]
            .iter()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
}

fn read_verified(
    opened: &OpenedArtifact,
    tail: &mut [u8],
    declared: &str,
    checkpoint: &tobkiri_wasm::NativeInspectionCheckpoint<'_>,
) -> Result<usize, NativeArtifactError> {
    use std::os::unix::fs::MetadataExt;
    // Retain and use this actual root/File, never a second pathname open.
    let _root_custody = &opened.root;
    let mut file = &opened.file;
    let mut read = 0;
    let mut hash = Sha256::new();
    loop {
        checkpoint.charge_work(1)?;
        // A one-byte stack probe verifies EOF even for an empty or full tail.
        let mut probe = [0_u8; 1];
        let result = if read == tail.len() {
            file.read(&mut probe)
        } else {
            file.read(&mut tail[read..])
        };
        checkpoint.charge_work(0)?;
        let count = match result {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            result => result.map_err(NativeArtifactError::SourceIo)?,
        };
        if count == 0 {
            break;
        }
        if read == tail.len() {
            return Err(NativeArtifactError::Integrity);
        }
        hash.update(&tail[read..read + count]);
        read += count;
    }
    checkpoint.charge_work(1)?;
    let after = file.metadata();
    checkpoint.charge_work(0)?;
    let after = after.map_err(NativeArtifactError::SourceIo)?;
    let before = &opened.before;
    if !after.is_file()
        || after.dev() != before.dev()
        || after.ino() != before.ino()
        || after.len() != before.len()
        || after.nlink() != before.nlink()
        || after.mtime() != before.mtime()
        || after.mtime_nsec() != before.mtime_nsec()
        || read != tail.len()
        || Digest::from_sha256(hash.finalize().into()).as_str() != declared
    {
        return Err(NativeArtifactError::Integrity);
    }
    Ok(read)
}

#[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
#[path = "native_declared_artifact_tests.rs"]
mod tests;
