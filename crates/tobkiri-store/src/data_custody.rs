//! Actual lifetime custody shared by native DATA handles and their VFS files.
use crate::{DataRoot, OwnerLease, StoreError};
use std::sync::Arc;
use tobkiri_gate::ControlReservation;

/// This owns the guards themselves, not descriptive owner/account metadata.
/// Every SQLite file receives the same Arc before xOpen, so an unconfirmed
/// xClose retains the original OS owner lock and Core registration together.
pub(crate) struct DataCustody {
    owner: Option<OwnerLease>,
    global_root: Option<DataRoot>,
    _registration: Option<ControlReservation>,
}
impl DataCustody {
    pub(crate) fn registered(
        owner: Option<OwnerLease>,
        registration: ControlReservation,
    ) -> Arc<Self> {
        Arc::new(Self {
            owner,
            global_root: None,
            _registration: Some(registration),
        })
    }
    /// Global DATA needs the same private native filesystem proof while its
    /// actual account-root descriptor remains pinned. It gains no owner lock.
    pub(crate) fn registered_global(
        root: &DataRoot,
        registration: ControlReservation,
    ) -> Result<Arc<Self>, StoreError> {
        root.validate_native_data_root()?;
        Ok(Arc::new(Self {
            owner: None,
            global_root: Some(root.clone()),
            _registration: Some(registration),
        }))
    }
    /// Only the trusted, bounded seed-installation API lacks a CallCx account.
    /// Its real physical owner proof must still outlive all SQLite descriptors.
    pub(crate) fn trusted_seed(owner: OwnerLease) -> Arc<Self> {
        Arc::new(Self {
            owner: Some(owner),
            global_root: None,
            _registration: None,
        })
    }
    pub(crate) fn check_owner_live(&self) -> Result<(), StoreError> {
        if let Some(owner) = &self.owner {
            owner.check_live()?;
        }
        if let Some(root) = &self.global_root {
            root.validate_native_data_root()?;
        }
        Ok(())
    }
}

/// Field order closes SQLite before removing its pathname routes or dropping
/// lifetime custody, including setup/seed error paths before NativeDatabase is
/// constructed. No Drop implementation prevents moving these fields into it.
pub(crate) struct DataConnection {
    pub(crate) connection: rusqlite::Connection,
    pub(crate) directory: crate::vfs::DatabaseGuard,
    pub(crate) custody: Arc<DataCustody>,
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use tobkiri_gate::{ControlKind, CoreControl};

    #[test]
    fn global_custody_rejects_a_permissive_actual_root_and_refunds_admission() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::set_permissions(temp.path(), std::fs::Permissions::from_mode(0o755)).unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let control = CoreControl::default();
        let registration = control
            .reserve(ControlKind::Registration, 64 * 1024)
            .unwrap();
        assert!(matches!(
            DataCustody::registered_global(&root, registration),
            Err(StoreError::Denied(_))
        ));
        assert_eq!(control.usage().count(ControlKind::Registration), 0);
        assert!(!root.path().join("user-global").exists());
    }
}
