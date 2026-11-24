//! Real Core registration, committed Profile approval and Session Gate tests.
//! No replacement Gate, frame proof or filesystem grant is manufactured.
use super::*;
use std::fs::{self, File};
use tobkiri_gate::{FsPermission, Gate, Outcome};
use tobkiri_wasm::filesystem::{FsCall, FsError};

async fn fixture(
    approved: bool,
) -> (
    tempfile::TempDir,
    Core,
    Profile,
    Session,
    crate::FilesystemRoot,
    Arc<dyn tobkiri_wasm::FilesystemAccess>,
    Arc<GateEngine>,
    CallCx,
) {
    let temporary = tempfile::tempdir().unwrap();
    let base = temporary.path().canonicalize().unwrap();
    fs::create_dir(base.join("workspace")).unwrap();
    let core = Core::open(
        CoreConfig::for_test_data_root(base.join("data")),
        Arc::new(crate::test_support::RuntimeHost),
    )
    .unwrap();
    let root = core
        .register_filesystem_root(
            File::open(base.join("workspace")).unwrap(),
            crate::FilesystemRights {
                read: true,
                write: false,
            },
        )
        .unwrap();
    let mut permissions = crate::test_support::permissions(&[]);
    if approved {
        permissions.fs.push(FsPermission {
            root: root.id().into(),
            read: true,
            write: false,
        });
    }
    let mut profile = crate::test_support::runtime_profile(&base, &[], permissions.clone(), false);
    profile.user_permissions = Some(permissions);
    core.save_profile(profile.clone(), None).await.unwrap();
    let session = core.start(&profile.id).await.unwrap();
    // Core::authority opens a fresh Gate. Only the actual started Session's
    // Gate has its compiled revision and committed User policy registered.
    let gate = session.gate.clone();
    let cx = core
        .user_context(&profile, &gate)
        .unwrap()
        .with_revision(session.plan_digest().clone());
    let access = core
        .filesystem
        .as_ref()
        .unwrap()
        .for_gate(gate.clone())
        .unwrap();
    (temporary, core, profile, session, root, access, gate, cx)
}

#[tokio::test]
async fn core_filesystem_rejects_unapproved_and_ambient_picker_candidates() {
    let (_temporary, _core, _profile, _session, root, access, _gate, cx) = fixture(false).await;
    assert!(matches!(
        access.issue_open_token(&cx, root.id()),
        Err(FsError::Denied(_))
    ));
    assert!(matches!(
        access.issue_open_token(&cx, "/tmp"),
        Err(FsError::Denied(_))
    ));
    assert!(matches!(
        access.issue_open_token(&cx, "file:../workspace"),
        Err(FsError::Denied(_))
    ));
    assert!(matches!(
        access.issue_open_token(&cx, &format!("fs-root:{}", "0".repeat(64))),
        Err(FsError::Denied(_))
    ));
}

#[tokio::test]
async fn core_filesystem_open_uses_real_gate_and_rejects_root_escape() {
    let (_temporary, _core, _profile, _session, root, access, gate, cx) = fixture(true).await;
    let token = access.issue_open_token(&cx, root.id()).unwrap();
    let prepared = access.registry().prepare_open(&cx, &token).unwrap();
    let grant = gate
        .authorize(
            &cx,
            prepared.request().clone(),
            prepared.args_digest().clone(),
        )
        .await
        .unwrap();
    let id = grant.id().clone();
    let opened = prepared.consume(grant, &cx, gate.clone()).await.unwrap();
    assert_eq!(opened.directory.root_id(), root.id());
    assert_eq!(
        opened.directory.rights(),
        crate::FilesystemRights {
            read: true,
            write: false
        }
    );
    for path in ["../authority", "/etc/passwd", "a/../../authority"] {
        assert!(matches!(
            opened.directory.prepare(
                &cx,
                FsCall::Read {
                    path,
                    offset: 0,
                    len: 1
                }
            ),
            Err(FsError::Invalid(_))
        ));
    }
    assert!(matches!(
        opened.directory.prepare(&cx, FsCall::Mkdir { path: "new" }),
        Err(FsError::Denied(_))
    ));
    assert!(access.registry().prepare_open(&cx, &token).is_err());
    gate.record_outcome(id, Outcome::Succeeded).await.unwrap();
}

#[tokio::test]
async fn core_filesystem_does_not_publish_protected_root_or_descendant() {
    let (temporary, core, _profile, _session, _root, _access, _gate, _cx) = fixture(true).await;
    let base = temporary.path().canonicalize().unwrap();
    let rights = crate::FilesystemRights {
        read: true,
        write: true,
    };
    assert!(
        core.register_filesystem_root(File::open(base.join("data")).unwrap(), rights)
            .is_err()
    );
    std::os::unix::fs::symlink(base.join("data"), base.join("data-alias")).unwrap();
    assert!(
        core.register_filesystem_root(File::open(base.join("data-alias")).unwrap(), rights)
            .is_err()
    );
    fs::create_dir(base.join("data/secret-subtree")).unwrap();
    assert!(
        core.register_filesystem_root(
            File::open(base.join("data/secret-subtree")).unwrap(),
            rights
        )
        .is_err()
    );
    // An enclosing workspace remains usable; operations continue to exclude
    // the live protected descendant by its original descriptor identity.
    assert!(
        core.register_filesystem_root(File::open(&base).unwrap(), rights)
            .is_ok()
    );
}

#[tokio::test]
async fn core_filesystem_picker_checks_current_epoch() {
    let (_temporary, _core, profile, _session, root, access, gate, cx) = fixture(true).await;
    gate.revoke_epoch(&profile.id).unwrap();
    assert!(matches!(
        access.issue_open_token(&cx, root.id()),
        Err(FsError::Gate(tobkiri_gate::GateError::StaleEpoch))
    ));
}

async fn open_registered_directory(
    access: &Arc<dyn tobkiri_wasm::FilesystemAccess>,
    gate: &Arc<GateEngine>,
    cx: &CallCx,
    root: &crate::FilesystemRoot,
) -> tobkiri_wasm::filesystem::FsOpenCompletion {
    gate.check_context(cx).unwrap();
    let token = access.issue_open_token(cx, root.id()).unwrap();
    let prepared = access.registry().prepare_open(cx, &token).unwrap();
    let grant = gate
        .authorize(
            cx,
            prepared.request().clone(),
            prepared.args_digest().clone(),
        )
        .await
        .unwrap();
    let opened = prepared.consume(grant, cx, gate.clone()).await.unwrap();
    gate.record_outcome(opened.permit().id().clone(), Outcome::Succeeded)
        .await
        .unwrap();
    opened
}

async fn consume_registered_read(
    directory: &tobkiri_wasm::filesystem::FsDirectory,
    gate: &Arc<GateEngine>,
    cx: &CallCx,
    path: &str,
) -> tobkiri_wasm::filesystem::FsWork {
    let prepared = directory
        .prepare(
            cx,
            FsCall::Read {
                path,
                offset: 0,
                len: 64,
            },
        )
        .unwrap();
    let grant = gate
        .authorize(
            cx,
            prepared.request().clone(),
            prepared.args_digest().clone(),
        )
        .await
        .unwrap();
    // These registry tests have no guest invocation. Their real filesystem
    // output still uses the existing finite 4MiB fixture allocation owner;
    // this budget supplies no registry identity, revision or Gate authority.
    prepared
        .consume(grant, cx, gate.clone(), crate::test_support::value_budget())
        .await
        .unwrap()
}

async fn execute_registered_read(
    work: tobkiri_wasm::filesystem::FsWork,
    gate: &Arc<GateEngine>,
) -> Result<crate::CoreBuffer, FsError> {
    // Await the actual blocking Join, then retain FsCompletion through its own
    // outcome delivery. No detached work or synthetic physical receipt.
    let mut completion = tokio::task::spawn_blocking(move || work.execute())
        .await
        .expect("actual filesystem work joined");
    assert_eq!(
        completion.disposition(),
        tobkiri_wasm::filesystem::FsDisposition::Clean
    );
    let result = completion.take_result();
    let outcome = if result.is_ok() {
        Outcome::Succeeded
    } else {
        Outcome::Failed {
            code: "filesystem-read-failed".into(),
        }
    };
    gate.record_outcome(completion.permit().id().clone(), outcome)
        .await
        .unwrap();
    match result? {
        tobkiri_wasm::filesystem::FsOutput::Bytes(bytes) => Ok(bytes),
        _ => panic!("read returned a non-byte filesystem output"),
    }
}

#[tokio::test]
async fn registered_descriptor_survives_root_rename_and_path_replacement() {
    let (temporary, _core, _profile, _session, root, access, gate, cx) = fixture(true).await;
    let base = temporary.path().canonicalize().unwrap();
    fs::write(base.join("workspace/value.txt"), b"pinned original").unwrap();
    fs::create_dir(base.join("substitute")).unwrap();
    fs::write(base.join("substitute/value.txt"), b"substitute private").unwrap();
    // Registration already retained the workspace descriptor. Change both its
    // public pathname and what a pathname-based reopen would now select.
    fs::rename(base.join("workspace"), base.join("renamed-workspace")).unwrap();
    std::os::unix::fs::symlink(base.join("substitute"), base.join("workspace")).unwrap();
    assert_eq!(
        fs::read(base.join("workspace/value.txt")).unwrap(),
        b"substitute private"
    );

    let opened = open_registered_directory(&access, &gate, &cx, &root).await;
    let work = consume_registered_read(&opened.directory, &gate, &cx, "value.txt").await;
    let bytes = execute_registered_read(work, &gate).await.unwrap();
    assert_eq!(bytes.as_bytes(), b"pinned original");
    assert_eq!(opened.directory.root_id(), root.id());
    assert_eq!(
        fs::read(base.join("substitute/value.txt")).unwrap(),
        b"substitute private"
    );
}

#[tokio::test]
async fn prepared_read_rejects_file_symlink_substitution_before_native_execution() {
    let (temporary, _core, _profile, _session, root, access, gate, cx) = fixture(true).await;
    let base = temporary.path().canonicalize().unwrap();
    fs::write(base.join("workspace/value.txt"), b"registered data").unwrap();
    fs::write(base.join("outside.txt"), b"outside private data").unwrap();
    let opened = open_registered_directory(&access, &gate, &cx, &root).await;
    // Real exact Read authorization is already consumed, but native execution
    // has not started. Its relative path must be checked against current FDs.
    let work = consume_registered_read(&opened.directory, &gate, &cx, "value.txt").await;
    fs::rename(
        base.join("workspace/value.txt"),
        base.join("workspace/saved.txt"),
    )
    .unwrap();
    std::os::unix::fs::symlink(base.join("outside.txt"), base.join("workspace/value.txt")).unwrap();
    assert!(matches!(
        execute_registered_read(work, &gate).await,
        Err(FsError::Denied(_))
    ));
    assert_eq!(
        fs::read(base.join("outside.txt")).unwrap(),
        b"outside private data"
    );
    // The denial is not a blanket failure of the original root or its policy.
    let fresh = consume_registered_read(&opened.directory, &gate, &cx, "saved.txt").await;
    assert_eq!(
        execute_registered_read(fresh, &gate)
            .await
            .unwrap()
            .as_bytes(),
        b"registered data"
    );
}

#[tokio::test]
async fn tokens_and_open_directories_cannot_cross_real_session_requests() {
    let (temporary, core, profile, _first_session, root, first_access, first_gate, first_cx) =
        fixture(true).await;
    let base = temporary.path().canonicalize().unwrap();
    fs::write(base.join("workspace/value.txt"), b"request scoped").unwrap();
    let old_token = first_access.issue_open_token(&first_cx, root.id()).unwrap();
    let opened = open_registered_directory(&first_access, &first_gate, &first_cx, &root).await;

    let second_session = core.start(&profile.id).await.unwrap();
    let second_gate = second_session.gate.clone();
    let second_cx = core
        .user_context(&profile, &second_gate)
        .unwrap()
        .with_revision(second_session.plan_digest().clone());
    let second_access = core
        .filesystem
        .as_ref()
        .unwrap()
        .for_gate(second_gate.clone())
        .unwrap();
    first_gate.check_context(&first_cx).unwrap();
    second_gate.check_context(&second_cx).unwrap();
    assert_eq!(first_cx.profile(), second_cx.profile());
    assert_eq!(first_cx.principal(), second_cx.principal());
    assert_eq!(first_cx.revision(), second_cx.revision());
    assert_eq!(first_cx.security_epoch(), second_cx.security_epoch());
    assert_ne!(first_cx.request(), second_cx.request());
    assert!(matches!(
        second_access
            .registry()
            .prepare_open(&second_cx, &old_token),
        Err(FsError::Denied(_))
    ));
    // Even a rejected foreign attempt consumes the original one-shot token.
    assert!(matches!(
        first_access.registry().prepare_open(&first_cx, &old_token),
        Err(FsError::Denied(_))
    ));
    assert!(matches!(
        opened.directory.prepare(
            &second_cx,
            FsCall::Read {
                path: "value.txt",
                offset: 0,
                len: 64
            }
        ),
        Err(FsError::Denied(_))
    ));
    let fresh = open_registered_directory(&second_access, &second_gate, &second_cx, &root).await;
    let work =
        consume_registered_read(&fresh.directory, &second_gate, &second_cx, "value.txt").await;
    assert_eq!(
        execute_registered_read(work, &second_gate)
            .await
            .unwrap()
            .as_bytes(),
        b"request scoped"
    );
}

#[tokio::test]
async fn revocation_blocks_preissued_token_and_prepared_read_but_fresh_approval_works() {
    let (temporary, core, profile, _old_session, root, old_access, old_gate, old_cx) =
        fixture(true).await;
    let base = temporary.path().canonicalize().unwrap();
    fs::write(base.join("workspace/value.txt"), b"current approval").unwrap();
    let token = old_access.issue_open_token(&old_cx, root.id()).unwrap();
    let opened = open_registered_directory(&old_access, &old_gate, &old_cx, &root).await;
    let prepared = opened
        .directory
        .prepare(
            &old_cx,
            FsCall::Read {
                path: "value.txt",
                offset: 0,
                len: 64,
            },
        )
        .unwrap();
    let next_epoch = old_gate.revoke_epoch(&profile.id).unwrap();
    assert!(next_epoch > old_cx.security_epoch());
    // This operation was prepared while valid. The actual Gate must refuse a
    // Read grant after persistent revocation, before native work can exist.
    assert!(matches!(
        old_gate
            .authorize(
                &old_cx,
                prepared.request().clone(),
                prepared.args_digest().clone()
            )
            .await,
        Err(tobkiri_gate::GateError::StaleEpoch)
    ));
    core.approve_profile(&profile.id).await.unwrap();
    let fresh_session = core.start(&profile.id).await.unwrap();
    let fresh_gate = fresh_session.gate.clone();
    let fresh_cx = core
        .user_context(&profile, &fresh_gate)
        .unwrap()
        .with_revision(fresh_session.plan_digest().clone());
    fresh_gate.check_context(&fresh_cx).unwrap();
    assert_eq!(fresh_cx.security_epoch(), next_epoch);
    let fresh_access = core
        .filesystem
        .as_ref()
        .unwrap()
        .for_gate(fresh_gate.clone())
        .unwrap();
    assert!(matches!(
        fresh_access.registry().prepare_open(&fresh_cx, &token),
        Err(FsError::Denied(_))
    ));
    assert!(matches!(
        opened.directory.prepare(
            &fresh_cx,
            FsCall::Read {
                path: "value.txt",
                offset: 0,
                len: 64
            }
        ),
        Err(FsError::Denied(_))
    ));
    let fresh = open_registered_directory(&fresh_access, &fresh_gate, &fresh_cx, &root).await;
    let work = consume_registered_read(&fresh.directory, &fresh_gate, &fresh_cx, "value.txt").await;
    assert_eq!(
        execute_registered_read(work, &fresh_gate)
            .await
            .unwrap()
            .as_bytes(),
        b"current approval"
    );
}

#[tokio::test]
async fn reregistering_same_inode_does_not_inherit_approved_root_identity() {
    let (temporary, core, mut profile, _old_session, root, access, gate, cx) = fixture(true).await;
    let base = temporary.path().canonicalize().unwrap();
    fs::write(base.join("workspace/value.txt"), b"explicit root identity").unwrap();
    let new_root = core
        .register_filesystem_root(
            File::open(base.join("workspace")).unwrap(),
            crate::FilesystemRights {
                read: true,
                write: false,
            },
        )
        .unwrap();
    assert_ne!(root.id(), new_root.id());
    gate.check_context(&cx).unwrap();
    assert!(matches!(
        access.issue_open_token(&cx, new_root.id()),
        Err(FsError::Denied(_))
    ));
    let old = open_registered_directory(&access, &gate, &cx, &root).await;
    let work = consume_registered_read(&old.directory, &gate, &cx, "value.txt").await;
    assert_eq!(
        execute_registered_read(work, &gate)
            .await
            .unwrap()
            .as_bytes(),
        b"explicit root identity"
    );

    let permission = FsPermission {
        root: new_root.id().into(),
        read: true,
        write: false,
    };
    profile.ceiling.fs.push(permission.clone());
    profile
        .user_permissions
        .as_mut()
        .unwrap()
        .fs
        .push(permission);
    let (_, old_head) = core.profile(&profile.id).unwrap();
    core.save_profile(profile.clone(), Some(old_head))
        .await
        .unwrap();
    let fresh_session = core.start(&profile.id).await.unwrap();
    let fresh_gate = fresh_session.gate.clone();
    let fresh_cx = core
        .user_context(&profile, &fresh_gate)
        .unwrap()
        .with_revision(fresh_session.plan_digest().clone());
    let fresh_access = core
        .filesystem
        .as_ref()
        .unwrap()
        .for_gate(fresh_gate.clone())
        .unwrap();
    let fresh = open_registered_directory(&fresh_access, &fresh_gate, &fresh_cx, &new_root).await;
    assert_eq!(fresh.directory.root_id(), new_root.id());
    let work = consume_registered_read(&fresh.directory, &fresh_gate, &fresh_cx, "value.txt").await;
    assert_eq!(
        execute_registered_read(work, &fresh_gate)
            .await
            .unwrap()
            .as_bytes(),
        b"explicit root identity"
    );
}

#[tokio::test]
async fn registered_root_moved_into_protected_data_is_rejected_at_actual_read() {
    let (temporary, _core, _profile, _session, root, access, gate, cx) = fixture(true).await;
    let base = temporary.path().canonicalize().unwrap();
    fs::write(base.join("workspace/value.txt"), b"moved under authority").unwrap();
    let opened = open_registered_directory(&access, &gate, &cx, &root).await;
    // Registration was valid outside Core data. Current descriptor ancestry,
    // rather than its old public spelling, must enforce the protected subtree.
    fs::rename(base.join("workspace"), base.join("data/moved-workspace")).unwrap();
    let work = consume_registered_read(&opened.directory, &gate, &cx, "value.txt").await;
    assert!(matches!(
        execute_registered_read(work, &gate).await,
        Err(FsError::Denied(_))
    ));
    assert_eq!(
        fs::read(base.join("data/moved-workspace/value.txt")).unwrap(),
        b"moved under authority"
    );
}
