//! The actual storage/accounting owner shared by native bootstrap and Core.
//!
//! Opening a physical data root does not grant access to application sources,
//! Profile data or effects. Source policies and the ordinary Gate remain separate.

use crate::CoreError;
use std::{path::Path, sync::Arc};
use tobkiri_gate::{CapacityLedger, CoreControl, NativeSetupInvocation, NativeSetupLease};
use tobkiri_store::DataRoot;

/// Move-only runtime storage and accounting, before a Host is needed.
///
/// A composition selects its fixed internal storage path once, opens this owner
/// at the original operation reachpoint and later transfers it to Core. Adopting
/// it must not reopen the root, replace its control account or reset live native
/// capacity. This object contains no Pack identity, Host or source permission.
pub struct RuntimeOwner {
    pub(crate) root: Arc<DataRoot>,
    pub(crate) capacity: CapacityLedger,
    pub(crate) control: CoreControl,
}

impl RuntimeOwner {
    /// Borrow this same bounded metadata account for trusted artifact loading.
    /// It cannot change its limits or authorize source/effect operations.
    pub fn control(&self) -> &CoreControl {
        &self.control
    }

    /// Open one actual descriptor-pinned root selected by trusted composition.
    /// This is not a guest/IPC path selector or a filesystem permission grant.
    pub fn open(path: impl AsRef<Path>) -> Result<Self, CoreError> {
        let root = Arc::new(DataRoot::open(path)?);
        let capacity = crate::capacity::shared(&root)?;
        Ok(Self {
            root,
            capacity,
            control: CoreControl::default(),
        })
    }

    /// Begin an operation from the real physical-root lease and shared ledger.
    ///
    /// The native caller owns operation/retry policy. This does not reset an
    /// existing invocation or its single Store claim. Original source admission
    /// and its finite reachable operation policy must be bound independently.
    pub fn begin_native_setup(&self) -> Result<NativeSetupInvocation, CoreError> {
        begin(&self.root, &self.capacity, &self.control)
    }
}

pub(crate) fn begin(
    root: &DataRoot,
    capacity: &CapacityLedger,
    control: &CoreControl,
) -> Result<NativeSetupInvocation, CoreError> {
    // Preserve the physical broker's original failure instead of translating
    // an unsupported filesystem/OS into an unrelated approval-surface error.
    let lease = Arc::new(root.native_setup_owner_lease(control)?) as Arc<dyn NativeSetupLease>;
    Ok(capacity
        .native_setup_account(control, || Ok(lease))?
        .begin()?)
}
