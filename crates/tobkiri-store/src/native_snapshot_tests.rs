//! Actual supported-platform disk reservation/seal tests. Never portable OS proof.
#![cfg(all(target_os = "macos", target_arch = "aarch64"))]
use super::*;
use crate::DataRoot;
use sha2::{Digest as _, Sha256};
use std::{
    io::Write,
    os::{fd::AsRawFd, unix::fs::MetadataExt},
    path::PathBuf,
    sync::Arc,
};
use tobkiri_gate::{CapacityLedger, CoreControl, NativeSetupAccount};

struct Fixture {
    directory: tempfile::TempDir,
    owner: NativeSetupOwnerLease,
    account: NativeSetupAccount,
    invocation: NativeSetupInvocation,
    cancel: CoreCancellation,
    control: CoreControl,
    digest: Digest,
    length: u64,
}
impl Fixture {
    fn new(length: u64) -> Self {
        let directory = tempfile::tempdir().unwrap();
        let mut file = File::create(directory.path().join("artifact.bin")).unwrap();
        let bytes = [0x37; READ_CHUNK];
        let mut hash = Sha256::new();
        let mut remaining = length;
        while remaining != 0 {
            let n = remaining.min(READ_CHUNK as u64) as usize;
            file.write_all(&bytes[..n]).unwrap();
            hash.update(&bytes[..n]);
            remaining -= n as u64;
        }
        drop(file);
        let root = DataRoot::open(directory.path().join("data")).unwrap();
        let control = CoreControl::default();
        let owner = root.native_setup_owner_lease(&control).unwrap();
        let account = CapacityLedger::new()
            .native_setup_account(&control, || Ok(Arc::new(owner.clone())))
            .unwrap();
        let invocation = account.begin().unwrap();
        let cancel = CoreCancellation::new_with_control(&control).unwrap();
        Self {
            directory,
            owner,
            account,
            invocation,
            cancel,
            control,
            digest: Digest::from_sha256(hash.finalize().into()),
            length,
        }
    }
    fn source(&self) -> NativeSnapshotSource<'_> {
        NativeSnapshotSource {
            root: self.directory.path(),
            relative: "artifact.bin",
            length: self.length,
            sha256: &self.digest,
        }
    }
    fn source_path(&self) -> PathBuf {
        self.directory.path().join("artifact.bin")
    }
    fn assert_no_spool_names(&self) {
        assert!(
            std::fs::read_dir(self.directory.path().join("data"))
                .unwrap()
                .all(|entry| !entry
                    .unwrap()
                    .file_name()
                    .to_string_lossy()
                    .starts_with(".native-capture-"))
        );
    }
}

#[test]
fn readonly_unlinked_snapshot_survives_source_mutation_and_final_clone_keeps_funding() {
    let fixture = Fixture::new(READ_CHUNK as u64);
    let before = fixture.control.usage().bytes;
    let snapshot = fixture
        .owner
        .capture_file_snapshot(
            &fixture.invocation,
            fixture.source(),
            &fixture.cancel,
            &mut |_| Ok(()),
        )
        .unwrap();
    assert_eq!(fixture.account.snapshot().unwrap().charged_bytes, 0);
    assert!(fixture.control.usage().bytes > before);
    assert!(snapshot.belongs_to(&fixture.invocation).unwrap());
    assert_eq!(snapshot.sha256(), &fixture.digest);
    let fd = snapshot.0.get()._file.as_raw_fd();
    // SAFETY: an owned live descriptor, queried without transferring ownership.
    assert_eq!(
        unsafe { libc::fcntl(fd, libc::F_GETFL) } & libc::O_ACCMODE,
        libc::O_RDONLY
    );
    assert_eq!(snapshot.0.get()._file.metadata().unwrap().nlink(), 0);
    assert_ne!(
        snapshot.0.get()._file.metadata().unwrap().ino(),
        std::fs::metadata(fixture.source_path()).unwrap().ino()
    );
    fixture.assert_no_spool_names();
    std::fs::write(fixture.source_path(), b"changed after capture").unwrap();
    let alias = snapshot.clone();
    drop(snapshot);
    let memory = fixture
        .invocation
        .reserve(
            tobkiri_gate::CapacityKind::AbiMarshal,
            (READ_CHUNK + 1) as u64,
        )
        .unwrap();
    let mut output = vec![0; READ_CHUNK + 1];
    alias.read_exact_at(0, &mut output[..READ_CHUNK]).unwrap();
    assert!(output[..READ_CHUNK].iter().all(|v| *v == 0x37));
    assert!(alias.read_exact_at(0, &mut output).is_err());
    assert!(alias.read_exact_at(u64::MAX, &mut output[..1]).is_err());
    assert!(alias.read_exact_at(fixture.length, &mut []).is_ok());
    assert!(
        alias
            .read_exact_at(fixture.length, &mut output[..1])
            .is_err()
    );
    drop(output);
    drop(memory);
    assert!(fixture.control.usage().bytes > before);
    drop(alias);
    assert_eq!(fixture.control.usage().bytes, before);
}

#[test]
fn artifact_larger_than_memory_cap_streams_with_real_disk_reservation_and_bounded_scratch() {
    let fixture = Fixture::new(tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES + 123);
    let mut peak_memory = 0;
    let snapshot = fixture
        .owner
        .capture_file_snapshot(
            &fixture.invocation,
            fixture.source(),
            &fixture.cancel,
            &mut |_| {
                peak_memory = peak_memory.max(fixture.account.snapshot().unwrap().charged_bytes);
                Ok(())
            },
        )
        .unwrap();
    assert!(snapshot.len() > tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES);
    assert!(peak_memory >= READ_CHUNK as u64 && peak_memory < 1024 * 1024);
    assert_eq!(fixture.account.snapshot().unwrap().charged_bytes, 0);
    // The completed non-sparse backing still owns its real allocation after the
    // only writer has closed and the name disappeared. No COW estimate is used.
    assert!(snapshot.0.get()._file.metadata().unwrap().blocks() * 512 >= snapshot.len());
    fixture.assert_no_spool_names();
    let memory = fixture
        .invocation
        .reserve(tobkiri_gate::CapacityKind::AbiMarshal, 123)
        .unwrap();
    let mut output = vec![0; 123];
    snapshot
        .read_exact_at(tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES, &mut output)
        .unwrap();
    assert_eq!(output, [0x37; 123]);
    drop(output);
    drop(memory);
}

#[test]
fn cancellation_hash_mismatch_and_source_change_never_publish_and_release_actual_resources() {
    let fixture = Fixture::new(READ_CHUNK as u64);
    let before = fixture.control.usage().bytes;
    let wrong = Digest::hash(b"not the source");
    let bad = NativeSnapshotSource {
        sha256: &wrong,
        ..fixture.source()
    };
    assert!(
        fixture
            .owner
            .capture_file_snapshot(&fixture.invocation, bad, &fixture.cancel, &mut |_| Ok(()))
            .is_err()
    );
    assert_eq!(fixture.account.snapshot().unwrap().charged_bytes, 0);
    assert_eq!(fixture.control.usage().bytes, before);
    fixture.assert_no_spool_names();
    let mut copied = false;
    assert!(
        fixture
            .owner
            .capture_file_snapshot(
                &fixture.invocation,
                fixture.source(),
                &fixture.cancel,
                &mut |work| {
                    if work > 1 {
                        copied = true;
                    } else if copied {
                        std::fs::write(fixture.source_path(), []).unwrap();
                    }
                    Ok(())
                }
            )
            .is_err()
    );
    assert_eq!(fixture.account.snapshot().unwrap().charged_bytes, 0);
    assert_eq!(fixture.control.usage().bytes, before);
    fixture.assert_no_spool_names();
    let cancelled = Fixture::new(READ_CHUNK as u64);
    let before = cancelled.control.usage().bytes;
    assert!(
        cancelled
            .owner
            .capture_file_snapshot(
                &cancelled.invocation,
                cancelled.source(),
                &cancelled.cancel,
                &mut |work| {
                    if work > 1 {
                        cancelled.cancel.cancel();
                        return Err(GateError::Cancelled);
                    }
                    Ok(())
                }
            )
            .is_err()
    );
    assert_eq!(cancelled.account.snapshot().unwrap().charged_bytes, 0);
    assert_eq!(cancelled.control.usage().bytes, before);
    cancelled.assert_no_spool_names();
}

#[test]
fn snapshot_binding_uses_exact_invocation_even_when_same_root_counters_coincide() {
    let fixture = Fixture::new(0);
    let snapshot = fixture
        .owner
        .capture_file_snapshot(
            &fixture.invocation,
            fixture.source(),
            &fixture.cancel,
            &mut |_| Ok(()),
        )
        .unwrap();
    let other = CapacityLedger::new()
        .native_setup_account(&fixture.control, || Ok(Arc::new(fixture.owner.clone())))
        .unwrap()
        .begin()
        .unwrap();
    assert_eq!(fixture.invocation.provenance(), other.provenance());
    assert!(!snapshot.belongs_to(&other).unwrap());
    assert!(snapshot.belongs_to(&fixture.invocation.clone()).unwrap());
}
