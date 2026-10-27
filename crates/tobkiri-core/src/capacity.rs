//! Process-local accounting domains keyed by a securely opened physical root.
//! The weak handle targets the actual ledger inner, retained by allocations.
use crate::CoreError;
use std::{
    collections::BTreeMap,
    sync::{
        Mutex, OnceLock,
        atomic::{AtomicU32, Ordering},
    },
};
use tobkiri_gate::{CapacityLedger, WeakCapacityLedger};
use tobkiri_store::{DataRoot, DataRootIdentity};
const MAX_ROOTS: usize = 4096;
type Roots = BTreeMap<(u32, DataRootIdentity), WeakCapacityLedger>;
static ROOTS: OnceLock<Mutex<Roots>> = OnceLock::new();
static CREATOR_PID: AtomicU32 = AtomicU32::new(0);

#[cfg(test)]
#[path = "native_setup_capacity_tests.rs"]
mod native_setup_tests;

pub(crate) fn shared(root: &DataRoot) -> Result<CapacityLedger, CoreError> {
    let pid = std::process::id();
    // A non-exec fork can inherit a mutex locked by a vanished thread. Reject
    // inherited accounting before touching that mutex or any owner broker.
    if let Err(creator) = CREATOR_PID.compare_exchange(0, pid, Ordering::SeqCst, Ordering::SeqCst)
        && creator != pid
    {
        return Err(CoreError::Gate(tobkiri_gate::GateError::Unavailable));
    }
    let identity = root.identity()?;
    let key = (pid, identity);
    let mut roots = ROOTS
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| CoreError::Runtime("physical root registry poisoned".into()))?;
    // Keys retain the original directory descriptor. Pruning is safe only after
    // every actual reservation/output/IO owner has released the ledger inner.
    roots.retain(|_, ledger| ledger.upgrade().is_some());
    if let Some(ledger) = roots.get(&key).and_then(WeakCapacityLedger::upgrade) {
        return Ok(ledger);
    }
    if roots.len() >= MAX_ROOTS {
        return Err(CoreError::Gate(tobkiri_gate::GateError::Limit(
            "physical root registry capacity".into(),
        )));
    }
    let ledger = CapacityLedger::new();
    roots.insert(key, ledger.downgrade());
    Ok(ledger)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use tobkiri_canon::{
        Digest,
        ids::{PackHash, PackId, ProfileId, RequestId},
    };
    use tobkiri_gate::{CapacityKind, CapacityOwner, CapacityProvenance};
    #[test]
    fn live_allocation_keeps_one_domain_after_core_handles_drop_and_root_renames() {
        let dir = tempfile::tempdir().unwrap();
        let parent = std::fs::canonicalize(dir.path()).unwrap();
        let path = parent.join("original");
        let root = DataRoot::open(&path).unwrap();
        let ledger = shared(&root).unwrap();
        let profile = ProfileId::parse("prof_capacity").unwrap();
        let pack = PackId::parse("local.capacity").unwrap();
        let owner = CapacityOwner::pack_owner(profile.clone(), pack.clone());
        let binding = ledger
            .bind_owner(Arc::new(root.owner_lease(&profile, &pack).unwrap()))
            .unwrap();
        let allocation = ledger
            .reserve(
                owner.clone(),
                CapacityProvenance {
                    hash: Some(PackHash::new(Digest::hash(b"old"))),
                    revision: Digest::hash(b"plan"),
                    security_epoch: 0,
                    request: RequestId::parse("req_capacity").unwrap(),
                    store: None,
                    session: None,
                },
                CapacityKind::AbiMarshal,
                4096,
                16384,
            )
            .unwrap();
        let charged = 4096 + tobkiri_gate::capacity_reservation_bookkeeping_bytes();
        assert_eq!(ledger.snapshot(&owner).unwrap().charged_bytes, charged);
        let retained = allocation.clone();
        drop(binding);
        drop(ledger);
        drop(root);
        let renamed = parent.join("renamed");
        std::fs::rename(&path, &renamed).unwrap();
        let root = DataRoot::open(&renamed).unwrap();
        let reopened = shared(&root).unwrap();
        assert_eq!(reopened.snapshot(&owner).unwrap().charged_bytes, charged);
        drop(allocation);
        assert_eq!(
            reopened.snapshot(&owner).unwrap().charged_bytes,
            charged,
            "the remaining actual shared allocation still owns the original domain"
        );
        drop(retained);
        assert_eq!(reopened.snapshot(&owner).unwrap().charged_bytes, 0);
    }
    #[test]
    fn distinct_physical_roots_never_share_funding() {
        let first = tempfile::tempdir().unwrap();
        let second = tempfile::tempdir().unwrap();
        let first_root =
            DataRoot::open(std::fs::canonicalize(first.path()).unwrap().join("data")).unwrap();
        let second_root =
            DataRoot::open(std::fs::canonicalize(second.path()).unwrap().join("data")).unwrap();
        let first = shared(&first_root).unwrap();
        let second = shared(&second_root).unwrap();
        let profile = ProfileId::parse("prof_separate").unwrap();
        let pack = PackId::parse("local.capacity").unwrap();
        let owner = CapacityOwner::pack_owner(profile.clone(), pack.clone());
        let _first_binding = first
            .bind_owner(Arc::new(first_root.owner_lease(&profile, &pack).unwrap()))
            .unwrap();
        let _second_binding = second
            .bind_owner(Arc::new(second_root.owner_lease(&profile, &pack).unwrap()))
            .unwrap();
        let allocation = first
            .reserve(
                owner.clone(),
                CapacityProvenance {
                    hash: Some(PackHash::new(Digest::hash(b"pack"))),
                    revision: Digest::hash(b"plan"),
                    security_epoch: 0,
                    request: RequestId::parse("req_separate").unwrap(),
                    store: None,
                    session: None,
                },
                CapacityKind::AbiMarshal,
                4096,
                16384,
            )
            .unwrap();
        assert_eq!(
            first.snapshot(&owner).unwrap().charged_bytes,
            4096 + tobkiri_gate::capacity_reservation_bookkeeping_bytes()
        );
        assert_eq!(second.snapshot(&owner).unwrap().charged_bytes, 0);
        drop(allocation);
        assert_eq!(first.snapshot(&owner).unwrap().charged_bytes, 0);
        assert_eq!(second.snapshot(&owner).unwrap().charged_bytes, 0);
    }
}
