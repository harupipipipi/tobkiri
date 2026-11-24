//! Native-only frozen disk DATA. This carries no Profile or storage grant.
use crate::{NativeSetupOwnerLease, StoreError};
use std::{fs::File, path::Path, time::Instant};
use tobkiri_canon::{Digest, LeasedShared};
use tobkiri_gate::{ControlReservation, CoreCancellation, GateError, NativeSetupInvocation};

#[cfg(target_os = "macos")]
#[path = "native_snapshot_macos.rs"]
mod platform;
#[cfg(test)]
#[path = "native_snapshot_tests.rs"]
mod tests;

pub(crate) const READ_CHUNK: usize = 64 * 1024;
#[cfg(target_os = "macos")]
const SNAPSHOT_METADATA_BYTES: u64 = 4096;

/// Native-selected expected source. No guest/IPC may select a filesystem path
/// through this Rust-only bridge. Whole-capture/source verification remains the
/// trusted caller's responsibility; a file hash grants no configuration power.
pub struct NativeSnapshotSource<'a> {
    pub root: &'a Path,
    pub relative: &'a str,
    pub length: u64,
    pub sha256: &'a Digest,
}

struct HeldSnapshot {
    // Close the actual final FD BEFORE its native root, invocation and control.
    _file: File,
    length: u64,
    sha256: Digest,
    invocation: NativeSetupInvocation,
    cancel: CoreCancellation,
    _root: NativeSetupOwnerLease,
}

/// Independent, fully written, readonly/unlinked disk backing. Clones retain
/// the SAME FD/guard; neither a writer, pathname nor raw descriptor is exposed.
#[derive(Clone)]
pub struct NativeFileSnapshot(LeasedShared<HeldSnapshot, ControlReservation>);

fn check_active(
    invocation: &NativeSetupInvocation,
    cancel: &CoreCancellation,
) -> Result<(), StoreError> {
    // Original process/root validation precedes any native local state access.
    let deadline = invocation.deadline()?;
    if cancel.is_cancelled() {
        return Err(GateError::Cancelled.into());
    }
    if Instant::now() >= deadline {
        return Err(GateError::Deadline.into());
    }
    Ok(())
}

impl NativeSetupOwnerLease {
    /// Synchronous native capture primitive, intended for a funded managed IO
    /// worker. OS calls may remain in flight after cancellation: callers must
    /// retain this operation until actual return, never refund on a timeout.
    /// `check_work` charges the original invocation's host work/retirement state.
    pub fn capture_file_snapshot(
        &self,
        invocation: &NativeSetupInvocation,
        source: NativeSnapshotSource<'_>,
        cancel: &CoreCancellation,
        check_work: &mut dyn FnMut(u64) -> Result<(), GateError>,
    ) -> Result<NativeFileSnapshot, StoreError> {
        self.check_live()?;
        if invocation.allocation_domain()? != *self.allocation_domain() {
            return Err(StoreError::Denied(
                "snapshot belongs to another native root".into(),
            ));
        }
        check_active(invocation, cancel)?;
        check_work(1)?;
        #[cfg(target_os = "macos")]
        {
            platform::capture(self, invocation, source, cancel, check_work)
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = source;
            Err(StoreError::Unavailable(
                "native snapshot disk reservation is unproven on this platform".into(),
            ))
        }
    }
}

impl NativeFileSnapshot {
    pub fn len(&self) -> u64 {
        self.0.get().length
    }
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
    pub fn sha256(&self) -> &Digest {
        &self.0.get().sha256
    }
    /// Validate the native binding without revealing a mutable backing or FD.
    pub fn belongs_to(&self, invocation: &NativeSetupInvocation) -> Result<bool, StoreError> {
        check_active(invocation, &self.0.get().cancel)?;
        Ok(self.0.get().invocation.same_invocation(invocation))
    }
    /// Read only completed backing DATA. The caller prepays `output` in the same
    /// invocation before allocation; a WIT callback must not supply another owner.
    /// No path lookup, source re-read, native verification or new copy occurs here.
    pub fn read_exact_at(&self, offset: u64, output: &mut [u8]) -> Result<(), StoreError> {
        let held = self.0.get();
        check_active(&held.invocation, &held.cancel)?;
        if output.len() > READ_CHUNK
            || offset
                .checked_add(output.len() as u64)
                .is_none_or(|end| end > held.length)
        {
            return Err(StoreError::Invalid(
                "captured input range exceeds its fixed backing".into(),
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::FileExt;
            let mut filled = 0;
            while filled < output.len() {
                check_active(&held.invocation, &held.cancel)?;
                match held
                    ._file
                    .read_at(&mut output[filled..], offset + filled as u64)
                {
                    Ok(0) => {
                        return Err(StoreError::Invalid(
                            "sealed captured input ended early".into(),
                        ));
                    }
                    Ok(count) => filled += count,
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(error) => return Err(error.into()),
                }
            }
            check_active(&held.invocation, &held.cancel)
        }
        #[cfg(not(unix))]
        {
            Err(StoreError::Unavailable(
                "native captured reads are unproven on this platform".into(),
            ))
        }
    }
}
