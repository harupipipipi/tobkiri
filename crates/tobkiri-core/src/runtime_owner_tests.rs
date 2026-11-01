//! Actual physical owner transfer. No source permission or Profile is invented.
use super::*;
use crate::{Core, HostIo};
use tobkiri_canon::{CanonValue, ids::UiSessionId};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, CapacityKind, HostError, NATIVE_SETUP_MEMORY_BYTES,
    TrustedPrompt, UiEmbed,
};

struct UnusedHost;
#[async_trait::async_trait]
impl TrustedPrompt for UnusedHost {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        panic!("runtime ownership transfer must not request an approval")
    }
}
#[async_trait::async_trait]
impl HostIo for UnusedHost {
    fn display(&self, _: &str, _: &CanonValue) {
        panic!("runtime ownership transfer must not display a replacement UI")
    }
    async fn terminal_read_line(&self, _: &str) -> Result<String, HostError> {
        panic!("runtime ownership transfer must not use a terminal")
    }
    fn terminal_write(&self, _: &str) {
        panic!("runtime ownership transfer must not use a terminal")
    }
    fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, HostError> {
        panic!("runtime ownership transfer must not embed a replacement UI")
    }
    async fn pick_directory(&self, _: &str) -> Option<std::path::PathBuf> {
        panic!("runtime ownership transfer must not reselect a directory")
    }
}

#[test]
fn adoption_moves_real_root_control_and_outstanding_native_capacity() {
    let parent = tempfile::tempdir().unwrap();
    let path = parent.path().canonicalize().unwrap().join("runtime");
    let owner = RuntimeOwner::open(&path).unwrap();
    let physical_root = Arc::as_ptr(&owner.root);
    let control = owner.control.clone();
    let original = owner.begin_native_setup().unwrap();
    let held = original
        .reserve(CapacityKind::VfsBuffer, NATIVE_SETUP_MEMORY_BYTES)
        .unwrap();
    let core = Core::from_runtime_owner(owner, Arc::new(UnusedHost)).unwrap();
    assert_eq!(Arc::as_ptr(&core.profiles.root), physical_root);
    assert!(core.control.same_account(&control));
    assert!(core.profile_ids().unwrap().is_empty());
    let next = core.native_artifact_invocation().unwrap();
    assert_eq!(
        next.provenance().account_generation,
        original.provenance().account_generation
    );
    assert!(next.reserve(CapacityKind::AbiMarshal, 1).is_err());
    drop(held);
    assert!(next.reserve(CapacityKind::AbiMarshal, 1).is_ok());
}

#[test]
fn adoption_never_reopens_a_renamed_pinned_root() {
    let parent = tempfile::tempdir().unwrap();
    let path = parent.path().canonicalize().unwrap().join("runtime");
    let moved = path.with_file_name("moved");
    let owner = RuntimeOwner::open(&path).unwrap();
    let original = owner.begin_native_setup().unwrap();
    std::fs::rename(&path, &moved).unwrap();
    let core = Core::from_runtime_owner(owner, Arc::new(UnusedHost)).unwrap();
    assert!(!path.exists());
    assert!(moved.is_dir());
    let next = core.native_artifact_invocation().unwrap();
    assert_eq!(
        next.allocation_domain().unwrap(),
        original.allocation_domain().unwrap()
    );
    assert_eq!(
        next.provenance().account_generation,
        original.provenance().account_generation
    );
    assert!(core.profile_ids().unwrap().is_empty());
}
