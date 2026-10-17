//! Real descriptor/temporary-directory tests. Authority-path tests use the
//! production GateEngine and durable Store Approvals; the trusted approval
//! UI and an explicit audit-failure switch are test implementations. The low-level append fault
//! test injects a native cancellation checkpoint and makes no authority claim.

#![cfg(unix)]

use super::*;
use async_trait::async_trait;
use std::{
    fs,
    os::unix::fs::symlink,
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, AtomicUsize, Ordering as AtomicOrdering},
};
use tobkiri_canon::{
    AllocationRequest,
    ids::{PackHash, PackId, ProfileId, RequestId},
};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, AuditEvent, AuditSink, CoreCancellation,
    FsPermission, GateEngine, MediatorChain, PackPolicy, PermissionSet, Principal, ResourceLimits,
    TrustedPrompt,
};
use tobkiri_store::{Approvals, AuditLog, AuditQuota, DataRoot};

struct Approve;
#[async_trait]
impl TrustedPrompt for Approve {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        ApprovalAnswer::Approve
    }
}

struct TestAudit {
    inner: AuditLog,
    reject: AtomicBool,
}
impl AuditSink for TestAudit {
    fn append(&self, event: AuditEvent) -> std::result::Result<(), GateError> {
        if self.reject.load(Ordering::Acquire) {
            return Err(GateError::Persistence("injected audit failure".into()));
        }
        self.inner.append(event)
    }
    fn append_idempotent(
        &self,
        id: &Digest,
        event: AuditEvent,
    ) -> std::result::Result<(), GateError> {
        if self.reject.load(Ordering::Acquire) {
            return Err(GateError::Persistence("injected audit failure".into()));
        }
        self.inner.append_idempotent(id, event)
    }
}

#[derive(Default)]
struct Owner {
    live: AtomicU64,
    maximum: AtomicU64,
}
impl AllocationBudget for Owner {
    fn reserve(&self, request: AllocationRequest) -> std::result::Result<(), AllocationError> {
        let live = self.live.fetch_add(request.bytes, Ordering::AcqRel) + request.bytes;
        self.maximum.fetch_max(live, Ordering::AcqRel);
        Ok(())
    }
    fn release(&self, bytes: u64) {
        self.live.fetch_sub(bytes, Ordering::AcqRel);
    }
}

struct Fixture {
    _temporary: tempfile::TempDir,
    workspace: PathBuf,
    control: CoreControl,
    approvals: Arc<Approvals>,
    audit: Arc<TestAudit>,
    gate: Arc<GateEngine>,
    registry: FsRegistry,
    root: FsRootIdentity,
    cx: CallCx,
    owner: Arc<Owner>,
    budget: Arc<dyn AllocationBudget>,
}
impl Fixture {
    async fn new(rights: FsRights) -> Self {
        let temporary = tempfile::tempdir().unwrap();
        let base = temporary.path().canonicalize().unwrap();
        let workspace = base.join("workspace");
        fs::create_dir(&workspace).unwrap();
        let authority = base.join("authority");
        let data = DataRoot::open(&authority).unwrap();
        let control = CoreControl::default();
        let registry = FsRegistry::new(control.clone()).unwrap();
        registry
            .protect_root(File::open(&authority).unwrap())
            .unwrap();
        let root = registry
            .register_root(File::open(&workspace).unwrap(), rights)
            .unwrap();
        let profile = ProfileId::parse("prof_filesystem").unwrap();
        let approvals = Arc::new(Approvals::open(&data, &profile).unwrap());
        let audit = Arc::new(TestAudit {
            inner: AuditLog::open(&data, &profile, AuditQuota::default()).unwrap(),
            reject: AtomicBool::new(false),
        });
        let gate = Arc::new(GateEngine::new_with_control(
            approvals.clone(),
            audit.clone(),
            Arc::new(Approve),
            MediatorChain::default(),
            control.clone(),
        ));
        let pack = PackId::parse("local.filesystem-test").unwrap();
        let hash = PackHash::new(Digest::hash(b"filesystem-test-pack"));
        let limits = ResourceLimits {
            fuel_per_call: 1_000_000,
            memory_mib: 64,
            wall_ms: 30_000,
            table_elements: 16384,
            output_bytes: MAX_IO as u64,
        };
        let permissions = PermissionSet {
            fs: vec![FsPermission {
                root: root.id().into(),
                read: rights.read,
                write: rights.write,
            }],
            wasm: limits.clone(),
            ..PermissionSet::default()
        };
        gate.register_pack(
            &profile,
            &pack,
            &hash,
            PackPolicy {
                declaration: permissions.clone(),
                ceiling: permissions.clone(),
                pack_limits: permissions,
            },
        )
        .unwrap();
        let revision = Digest::hash(b"filesystem-test-compiled-revision");
        gate.register_revision(
            &profile,
            &revision,
            std::collections::BTreeMap::from([((pack.clone(), hash.clone()), limits.clone())]),
        )
        .unwrap();
        let context = |principal| {
            CallCx::try_root_with_control(
                profile.clone(),
                principal,
                RequestId::parse("req_filesystem").unwrap(),
                CoreCancellation::new_with_control(&control).unwrap(),
                Instant::now() + Duration::from_secs(30),
                limits.clone(),
                control.clone(),
            )
            .unwrap()
            .with_revision(revision.clone())
        };
        gate.approve_pack_permissions(&context(Principal::User), &pack, None, &hash)
            .await
            .unwrap();
        let cx = context(Principal::Pack { pack, hash });
        let owner = Arc::new(Owner::default());
        let budget = owner.clone();
        Self {
            _temporary: temporary,
            workspace,
            control,
            approvals,
            audit,
            gate,
            registry,
            root,
            cx,
            owner,
            budget,
        }
    }
    async fn open(&self, rights: FsRights) -> FsDirectory {
        let token = self
            .registry
            .issue_open_token(&self.cx, &self.root, rights, Duration::from_secs(5))
            .unwrap();
        let prepared = self.registry.prepare_open(&self.cx, &token).unwrap();
        assert!(matches!(
            prepared.request(),
            EffectRequest::Fs {
                operation: FsOperation::Open,
                ..
            }
        ));
        let grant = self
            .gate
            .authorize(
                &self.cx,
                prepared.request().clone(),
                prepared.args_digest().clone(),
            )
            .await
            .unwrap();
        let completion = prepared
            .consume(grant, &self.cx, self.gate.clone())
            .await
            .unwrap();
        assert!(completion.permit().matches_context(&self.cx));
        completion.directory
    }
    async fn work(&self, directory: &FsDirectory, call: FsCall<'_>) -> FsWork {
        let prepared = directory.prepare(&self.cx, call).unwrap();
        let grant = self
            .gate
            .authorize(
                &self.cx,
                prepared.request().clone(),
                prepared.args_digest().clone(),
            )
            .await
            .unwrap();
        prepared
            .consume(grant, &self.cx, self.gate.clone(), self.budget.clone())
            .await
            .unwrap()
    }
    async fn run(&self, directory: &FsDirectory, call: FsCall<'_>) -> FsCompletion {
        self.work(directory, call).await.execute()
    }
    fn bytes(&self, data: &[u8]) -> CoreBuffer {
        CoreBuffer::copy_from(data, &self.budget).unwrap()
    }
    fn repeated(&self, byte: u8, length: usize) -> CoreBuffer {
        CoreBuffer::write_exact(length, &self.budget, |target| {
            target.fill(byte);
            Ok(())
        })
        .unwrap()
    }
    fn protect(&self, relative: &str) {
        self.registry
            .protect_root(File::open(self.workspace.join(relative)).unwrap())
            .unwrap();
    }
}
fn rw() -> FsRights {
    FsRights {
        read: true,
        write: true,
    }
}
fn read_bytes(completion: &mut FsCompletion) -> CoreBuffer {
    match completion.take_result().unwrap() {
        FsOutput::Bytes(bytes) => bytes,
        _ => panic!("expected filesystem bytes"),
    }
}

#[tokio::test]
async fn supervised_write_records_success_only_after_output_conversion() {
    let fixture = Fixture::new(rw()).await;
    let directory = fixture.open(rw()).await;
    let work = fixture
        .work(
            &directory,
            FsCall::Write {
                path: "supervised",
                data: fixture.bytes(b"one committed write"),
                append: false,
            },
        )
        .await;
    let id = work.permit().id().clone();
    let delivery = work.dispatch().await.unwrap();
    assert!(fixture.approvals.terminal_delivery(&id).unwrap().is_none());
    let result = delivery.convert(|output| {
        assert!(matches!(output, FsOutput::Unit));
        Ok(())
    });
    delivery.finish(result).await.unwrap();
    let terminal = fixture.approvals.terminal_delivery(&id).unwrap().unwrap();
    assert!(matches!(
        terminal.decision.outcome,
        tobkiri_gate::Outcome::Succeeded
    ));
    assert_eq!(
        fs::read(fixture.workspace.join("supervised")).unwrap(),
        b"one committed write"
    );
}

#[tokio::test]
async fn supervised_mutation_conversion_failure_is_durable_unknown() {
    let fixture = Fixture::new(rw()).await;
    let directory = fixture.open(rw()).await;
    let work = fixture
        .work(
            &directory,
            FsCall::Write {
                path: "conversion-failure",
                data: fixture.bytes(b"still committed"),
                append: false,
            },
        )
        .await;
    let id = work.permit().id().clone();
    let delivery = work.dispatch().await.unwrap();
    let result = delivery.convert::<()>(|_| Err(AllocationError::Limit.into()));
    let error = delivery.finish(result).await.unwrap_err();
    assert!(matches!(
        error.downcast_ref::<FsError>(),
        Some(FsError::CommitUnknown)
    ));
    let terminal = fixture.approvals.terminal_delivery(&id).unwrap().unwrap();
    assert!(
        matches!(terminal.decision.outcome, tobkiri_gate::Outcome::Failed { code }
        if code == "filesystem-commit-unknown")
    );
    assert_eq!(
        fs::read(fixture.workspace.join("conversion-failure")).unwrap(),
        b"still committed"
    );
}

#[tokio::test]
async fn supervised_committed_write_with_audit_failure_never_delivers_success() {
    let fixture = Fixture::new(rw()).await;
    let directory = fixture.open(rw()).await;
    let work = fixture
        .work(
            &directory,
            FsCall::Write {
                path: "audit-failure",
                data: fixture.bytes(b"committed once"),
                append: true,
            },
        )
        .await;
    let id = work.permit().id().clone();
    let delivery = work.dispatch().await.unwrap();
    fixture.audit.reject.store(true, Ordering::Release);
    let result = delivery.convert(|_| Ok(()));
    let error = delivery.finish(result).await.unwrap_err();
    assert!(matches!(
        error.downcast_ref::<FsError>(),
        Some(FsError::CommitUnknown)
    ));
    let terminal = fixture.approvals.terminal_delivery(&id).unwrap().unwrap();
    assert!(!terminal.acknowledged);
    assert_eq!(
        fs::read(fixture.workspace.join("audit-failure")).unwrap(),
        b"committed once"
    );
}

#[tokio::test]
async fn supervised_mutation_evidence_outlives_successful_import_delivery() {
    let fixture = Fixture::new(rw()).await;
    let directory = fixture.open(rw()).await;
    let work = fixture
        .work(
            &directory,
            FsCall::Read {
                path: "missing",
                offset: 0,
                len: 1,
            },
        )
        .await;
    let delivery = work.dispatch().await.unwrap();
    let result = delivery.convert(|_| Ok(()));
    let error = delivery.finish(result).await.unwrap_err();
    assert!(!matches!(
        error.downcast_ref::<FsError>(),
        Some(FsError::CommitUnknown)
    ));
    assert!(!fixture.cx.filesystem_mutation_possible());
    let work = fixture
        .work(
            &directory,
            FsCall::Write {
                path: "later-trap",
                data: fixture.bytes(b"one write"),
                append: false,
            },
        )
        .await;
    let delivery = work.dispatch().await.unwrap();
    let result = delivery.convert(|_| Ok(()));
    delivery.finish(result).await.unwrap();
    let later_export_error = preserve_effect_error(&fixture.cx, anyhow::anyhow!("guest trap"));
    assert!(matches!(
        later_export_error.downcast_ref::<FsError>(),
        Some(FsError::CommitUnknown)
    ));
    assert_eq!(
        fs::read(fixture.workspace.join("later-trap")).unwrap(),
        b"one write"
    );
}

#[tokio::test]
async fn supervised_worker_panic_preserves_actual_custody_until_terminal_delivery() {
    let fixture = Fixture::new(rw()).await;
    let directory = fixture.open(rw()).await;
    let mut work = fixture
        .work(
            &directory,
            FsCall::Write {
                path: "panic-before-io",
                data: fixture.repeated(b'p', CHUNK),
                append: false,
            },
        )
        .await;
    let id = work.permit().id().clone();
    work.panic_before_io = true;
    let delivery = work.dispatch().await.unwrap();
    assert!(fixture.owner.live.load(Ordering::Acquire) >= CHUNK as u64);
    assert!(fixture.approvals.terminal_delivery(&id).unwrap().is_none());
    let result = delivery.convert::<()>(|_| panic!("uncertain work has no value to encode"));
    let error = delivery.finish(result).await.unwrap_err();
    assert!(matches!(
        error.downcast_ref::<FsError>(),
        Some(FsError::CommitUnknown)
    ));
    assert!(!fixture.workspace.join("panic-before-io").exists());
    let terminal = fixture.approvals.terminal_delivery(&id).unwrap().unwrap();
    assert!(
        matches!(terminal.decision.outcome, tobkiri_gate::Outcome::Failed { code }
        if code == "filesystem-commit-unknown")
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn supervised_abandoned_wait_retains_real_worker_and_records_unknown_after_completion() {
    let fixture = Fixture::new(rw()).await;
    let directory = fixture.open(rw()).await;
    let work = fixture
        .work(
            &directory,
            FsCall::Write {
                path: "abandoned",
                data: fixture.repeated(b'z', CHUNK),
                append: false,
            },
        )
        .await;
    let id = work.permit().id().clone();
    // Hold the actual OS namespace gate so the blocking worker cannot finish.
    let held_namespace = namespace_lock().unwrap();
    let before = fixture
        .control
        .usage()
        .count(tobkiri_gate::ControlKind::Task);
    let waiter = tokio::spawn(async move { work.dispatch().await });
    tokio::time::timeout(Duration::from_secs(5), async {
        while fixture
            .control
            .usage()
            .count(tobkiri_gate::ControlKind::Task)
            <= before
        {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    waiter.abort();
    assert!(matches!(waiter.await, Err(error) if error.is_cancelled()));
    assert!(fixture.owner.live.load(Ordering::Acquire) >= CHUNK as u64);
    assert!(fixture.approvals.terminal_delivery(&id).unwrap().is_none());
    drop(held_namespace);
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if fixture.approvals.terminal_delivery(&id).unwrap().is_some()
                && fixture.owner.live.load(Ordering::Acquire) == 0
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    let terminal = fixture.approvals.terminal_delivery(&id).unwrap().unwrap();
    assert!(
        matches!(terminal.decision.outcome, tobkiri_gate::Outcome::Failed { code }
        if code == "filesystem-commit-unknown")
    );
    assert_eq!(
        fs::read(fixture.workspace.join("abandoned")).unwrap(),
        vec![b'z'; CHUNK]
    );
}

#[tokio::test]
async fn real_gate_atomic_write_bounded_read_and_eof() {
    let fixture = Fixture::new(rw()).await;
    let directory = fixture.open(rw()).await;
    fs::write(fixture.workspace.join("file"), b"long original contents").unwrap();
    let mut write = fixture
        .run(
            &directory,
            FsCall::Write {
                path: "file",
                data: fixture.bytes(b"new"),
                append: false,
            },
        )
        .await;
    assert!(matches!(write.take_result(), Ok(FsOutput::Unit)));
    assert_eq!(write.disposition(), FsDisposition::Committed);
    assert_eq!(fs::read(fixture.workspace.join("file")).unwrap(), b"new");
    assert!(matches!(
        write.delivery_error(FsError::Unavailable),
        FsError::CommitUnknown
    ));
    assert!(write.take_result().is_err());
    let mut read = fixture
        .run(
            &directory,
            FsCall::Read {
                path: "file",
                offset: 1,
                len: 1,
            },
        )
        .await;
    let bytes = read_bytes(&mut read);
    assert_eq!(bytes.as_bytes(), b"e");
    assert!(fixture.owner.live.load(Ordering::Acquire) > 0);
    let mut eof = fixture
        .run(
            &directory,
            FsCall::Read {
                path: "file",
                offset: 100,
                len: 8,
            },
        )
        .await;
    assert!(read_bytes(&mut eof).is_empty());
    assert!(
        directory
            .prepare(
                &fixture.cx,
                FsCall::Read {
                    path: "file",
                    offset: i64::MAX as u64,
                    len: 2
                }
            )
            .is_err()
    );
    assert!(fs::read_dir(&fixture.workspace).unwrap().all(|entry| {
        !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(PRIVATE_PREFIX)
    }));
    drop((bytes, write, read, eof));
    assert_eq!(fixture.owner.live.load(Ordering::Acquire), 0);
}

#[tokio::test]
async fn traversal_symlink_hardlink_and_special_files_are_rejected() {
    let fixture = Fixture::new(rw()).await;
    let directory = fixture.open(rw()).await;
    for path in [
        "../escape",
        "/absolute",
        "a/../b",
        "a//b",
        "a\0b",
        "a\\b",
        ".",
        ".tobkiri-core-fs-hidden",
    ] {
        assert!(
            directory
                .prepare(&fixture.cx, FsCall::Stat { path })
                .is_err(),
            "{path:?}"
        );
    }
    fs::write(fixture.workspace.join("ordinary"), b"private").unwrap();
    symlink("ordinary", fixture.workspace.join("link")).unwrap();
    fs::hard_link(
        fixture.workspace.join("ordinary"),
        fixture.workspace.join("alias"),
    )
    .unwrap();
    let fifo = std::ffi::CString::new(fixture.workspace.join("fifo").to_str().unwrap()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o600) }, 0);
    for path in ["link", "ordinary", "alias", "fifo"] {
        let mut result = fixture
            .run(
                &directory,
                FsCall::Read {
                    path,
                    offset: 0,
                    len: 8,
                },
            )
            .await;
        assert!(result.take_result().is_err(), "{path}");
        assert_eq!(result.disposition(), FsDisposition::Clean);
    }
}

#[tokio::test]
async fn pinned_root_survives_rename_and_old_path_replacement() {
    let fixture = Fixture::new(rw()).await;
    let directory = fixture.open(rw()).await;
    fs::write(fixture.workspace.join("file"), b"pinned").unwrap();
    let moved = fixture.workspace.with_file_name("moved-workspace");
    fs::rename(&fixture.workspace, &moved).unwrap();
    fs::create_dir(&fixture.workspace).unwrap();
    fs::write(fixture.workspace.join("file"), b"replacement").unwrap();
    let mut read = fixture
        .run(
            &directory,
            FsCall::Read {
                path: "file",
                offset: 0,
                len: 64,
            },
        )
        .await;
    assert_eq!(read_bytes(&mut read).as_bytes(), b"pinned");
    let mut write = fixture
        .run(
            &directory,
            FsCall::Write {
                path: "file",
                data: fixture.bytes(b"updated"),
                append: false,
            },
        )
        .await;
    write.take_result().unwrap();
    assert_eq!(fs::read(moved.join("file")).unwrap(), b"updated");
    assert_eq!(
        fs::read(fixture.workspace.join("file")).unwrap(),
        b"replacement"
    );
}

#[tokio::test]
async fn tokens_are_single_use_and_bound_to_exact_scope_and_expiry() {
    let fixture = Fixture::new(rw()).await;
    let token = fixture
        .registry
        .issue_open_token(&fixture.cx, &fixture.root, rw(), Duration::from_secs(5))
        .unwrap();
    let foreign = fixture.cx.clone().with_security_epoch(1);
    assert!(fixture.registry.prepare_open(&foreign, &token).is_err());
    assert!(fixture.registry.prepare_open(&fixture.cx, &token).is_err());
    let token = fixture
        .registry
        .issue_open_token(&fixture.cx, &fixture.root, rw(), Duration::from_nanos(1))
        .unwrap();
    std::thread::yield_now();
    assert!(fixture.registry.prepare_open(&fixture.cx, &token).is_err());
    let token = fixture
        .registry
        .issue_open_token(&fixture.cx, &fixture.root, rw(), Duration::from_secs(5))
        .unwrap();
    let _prepared = fixture.registry.prepare_open(&fixture.cx, &token).unwrap();
    assert!(fixture.registry.prepare_open(&fixture.cx, &token).is_err());
    let other = FsRegistry::new(fixture.control.clone()).unwrap();
    assert!(
        other
            .issue_open_token(&fixture.cx, &fixture.root, rw(), Duration::from_secs(5))
            .is_err()
    );
}

#[tokio::test]
async fn read_only_ceiling_and_wrong_argument_grant_have_zero_effect() {
    let fixture = Fixture::new(rw()).await;
    let directory = fixture
        .open(FsRights {
            read: true,
            write: false,
        })
        .await;
    assert!(
        directory
            .prepare(
                &fixture.cx,
                FsCall::Write {
                    path: "file",
                    data: fixture.bytes(b"denied"),
                    append: false
                }
            )
            .is_err()
    );
    assert!(!fixture.workspace.join("file").exists());
    let directory = fixture.open(rw()).await;
    let prepared = directory
        .prepare(
            &fixture.cx,
            FsCall::Write {
                path: "file",
                data: fixture.bytes(b"wrong"),
                append: false,
            },
        )
        .unwrap();
    let wrong = fixture
        .gate
        .authorize(
            &fixture.cx,
            prepared.request().clone(),
            Digest::hash(b"different actual write bytes"),
        )
        .await
        .unwrap();
    assert!(
        prepared
            .consume(
                wrong,
                &fixture.cx,
                fixture.gate.clone(),
                fixture.budget.clone()
            )
            .await
            .is_err()
    );
    assert!(!fixture.workspace.join("file").exists());
    let prepared = directory
        .prepare(
            &fixture.cx,
            FsCall::Write {
                path: "file",
                data: fixture.bytes(b"stale"),
                append: false,
            },
        )
        .unwrap();
    let grant = fixture
        .gate
        .authorize(
            &fixture.cx,
            prepared.request().clone(),
            prepared.args_digest().clone(),
        )
        .await
        .unwrap();
    fixture
        .approvals
        .advance_security_epoch(fixture.cx.profile())
        .unwrap();
    assert!(
        prepared
            .consume(
                grant,
                &fixture.cx,
                fixture.gate.clone(),
                fixture.budget.clone()
            )
            .await
            .is_err()
    );
    assert!(!fixture.workspace.join("file").exists());
}

#[tokio::test]
async fn cancel_before_effect_retains_actual_input_and_completion_custody() {
    let fixture = Fixture::new(rw()).await;
    let directory = fixture.open(rw()).await;
    let work = fixture
        .work(
            &directory,
            FsCall::Write {
                path: "file",
                data: fixture.bytes(b"retained input"),
                append: false,
            },
        )
        .await;
    let held = fixture.owner.live.load(Ordering::Acquire);
    assert!(held > 0);
    fixture.cx.cancel().cancel();
    assert_eq!(fixture.owner.live.load(Ordering::Acquire), held);
    let mut completion = work.execute();
    assert!(completion.take_result().is_err());
    assert_eq!(completion.disposition(), FsDisposition::Clean);
    assert_eq!(fixture.owner.live.load(Ordering::Acquire), held);
    assert!(!fixture.workspace.join("file").exists());
    drop(completion);
    assert_eq!(fixture.owner.live.load(Ordering::Acquire), 0);
}

#[tokio::test]
async fn live_protected_roots_deny_both_rename_endpoints_and_ancestors() {
    let fixture = Fixture::new(rw()).await;
    fs::create_dir_all(fixture.workspace.join("container/core")).unwrap();
    fs::write(
        fixture.workspace.join("container/core/authority"),
        b"secret",
    )
    .unwrap();
    fs::create_dir(fixture.workspace.join("other")).unwrap();
    fixture.protect("container/core");
    let directory = fixture.open(rw()).await;
    let calls = [
        FsCall::Read {
            path: "container/core/authority",
            offset: 0,
            len: 64,
        },
        FsCall::Write {
            path: "container/core/authority",
            data: fixture.bytes(b"overwrite"),
            append: false,
        },
        FsCall::Rename {
            from: "container",
            to: "moved",
        },
        FsCall::Rename {
            from: "other",
            to: "container",
        },
        FsCall::Rename {
            from: "container/core/authority",
            to: "stolen",
        },
        FsCall::Remove { path: "container" },
        FsCall::Remove {
            path: "container/core",
        },
        FsCall::Mkdir {
            path: "container/core/new",
        },
    ];
    for call in calls {
        let mut result = fixture.run(&directory, call).await;
        assert!(result.take_result().is_err());
        assert_eq!(result.disposition(), FsDisposition::Clean);
    }
    // An external editor may rename the protected subtree. Descriptor checks
    // still protect the actual live directory at its new spelling.
    fs::rename(
        fixture.workspace.join("container/core"),
        fixture.workspace.join("renamed-core"),
    )
    .unwrap();
    let mut denied = fixture
        .run(
            &directory,
            FsCall::Read {
                path: "renamed-core/authority",
                offset: 0,
                len: 64,
            },
        )
        .await;
    assert!(denied.take_result().is_err());
    assert_eq!(
        fs::read(fixture.workspace.join("renamed-core/authority")).unwrap(),
        b"secret"
    );
}

#[tokio::test]
async fn list_and_stat_return_owned_wit_entries_and_repeatable_scans() {
    let fixture = Fixture::new(rw()).await;
    fs::write(fixture.workspace.join("file"), b"1234").unwrap();
    fs::create_dir(fixture.workspace.join("directory")).unwrap();
    fs::create_dir(fixture.workspace.join("core")).unwrap();
    fixture.protect("core");
    let directory = fixture.open(rw()).await;
    for _ in 0..2 {
        let mut completion = fixture.run(&directory, FsCall::List { path: "" }).await;
        let FsOutput::Entries(entries) = completion.take_result().unwrap() else {
            panic!("entries")
        };
        let entries = entries.array().unwrap();
        assert_eq!(entries.len(), 2);
        let mut found = false;
        for entry in entries.iter() {
            let entry_name = entry.get_key("name").unwrap();
            assert_ne!(entry_name.as_str(), Some("core"));
            if entry_name.as_str() == Some("file") {
                found = true;
                assert_eq!(entry.get_key("size").unwrap().as_str(), Some("4"));
                assert_eq!(entry.get_key("is-dir").unwrap().as_bool(), Some(false));
            }
        }
        assert!(found);
    }
    let mut completion = fixture.run(&directory, FsCall::Stat { path: "file" }).await;
    let FsOutput::Stat(stat) = completion.take_result().unwrap() else {
        panic!("stat")
    };
    assert_eq!(stat.name.as_str(), Some("file"));
    assert_eq!(stat.size, 4);
    assert!(!stat.is_directory);
}

#[tokio::test]
async fn separate_consumed_core_mutations_complete_without_torn_files() {
    let fixture = Fixture::new(rw()).await;
    let first = fixture.open(rw()).await;
    let second = fixture.open(rw()).await;
    let first = fixture
        .work(
            &first,
            FsCall::Write {
                path: "file",
                data: fixture.repeated(b'a', CHUNK * 4),
                append: false,
            },
        )
        .await;
    let second = fixture
        .work(
            &second,
            FsCall::Write {
                path: "file",
                data: fixture.repeated(b'b', CHUNK * 4),
                append: false,
            },
        )
        .await;
    let first = std::thread::spawn(move || first.execute());
    let second = std::thread::spawn(move || second.execute());
    let mut first = first.join().unwrap();
    let mut second = second.join().unwrap();
    first.take_result().unwrap();
    second.take_result().unwrap();
    assert_eq!(first.disposition(), FsDisposition::Committed);
    assert_eq!(second.disposition(), FsDisposition::Committed);
    let data = fs::read(fixture.workspace.join("file")).unwrap();
    assert_eq!(data.len(), CHUNK * 4);
    assert!(data.iter().all(|byte| *byte == data[0]));
    assert!(data[0] == b'a' || data[0] == b'b');
}

#[tokio::test]
async fn native_partial_append_checkpoint_is_unknown_without_replay() {
    let fixture = Fixture::new(rw()).await;
    fs::write(fixture.workspace.join("file"), b"before").unwrap();
    let call = OwnedCall::Write {
        path: "file".into(),
        data: fixture.repeated(b'x', CHUNK * 2),
        append: true,
    };
    let _lock = namespace_lock().unwrap();
    let state = fixture.registry.inner.state.lock().unwrap();
    let access = NativeAccess {
        root: &fixture.root.root,
        protected: &state.protected,
    };
    let checks = AtomicUsize::new(0);
    let active = || {
        if checks.fetch_add(1, AtomicOrdering::AcqRel) >= 3 {
            Err(FsError::Gate(GateError::Cancelled))
        } else {
            Ok(())
        }
    };
    let mut disposition = FsDisposition::Clean;
    let result = native::execute(
        &access,
        &call,
        &fixture.budget,
        MAX_IO as u64,
        &active,
        &mut disposition,
    );
    assert!(matches!(result, Err(FsError::CommitUnknown)));
    assert_eq!(disposition, FsDisposition::PartialUnknown);
    let bytes = fs::read(fixture.workspace.join("file")).unwrap();
    assert_eq!(bytes.len(), b"before".len() + CHUNK);
    assert_eq!(&bytes[..b"before".len()], b"before");
}

#[test]
fn argument_digest_binds_rights_ranges_order_append_and_actual_bytes() {
    let owner: Arc<dyn AllocationBudget> = Arc::new(Owner::default());
    let read = |offset| OwnedCall::Read {
        path: "file".into(),
        offset,
        len: 3,
    };
    assert_ne!(
        argument_digest("root", rw(), &read(0)),
        argument_digest("root", rw(), &read(1))
    );
    assert_ne!(
        argument_digest("root", rw(), &read(0)),
        argument_digest("other", rw(), &read(0))
    );
    assert_ne!(
        argument_digest("root", rw(), &read(0)),
        argument_digest(
            "root",
            FsRights {
                read: true,
                write: false
            },
            &read(0)
        )
    );
    let write = |data, append| OwnedCall::Write {
        path: "file".into(),
        data: CoreBuffer::copy_from(data, &owner).unwrap(),
        append,
    };
    assert_ne!(
        argument_digest("root", rw(), &write(b"a", false)),
        argument_digest("root", rw(), &write(b"b", false))
    );
    assert_ne!(
        argument_digest("root", rw(), &write(b"a", false)),
        argument_digest("root", rw(), &write(b"a", true))
    );
    assert_ne!(
        argument_digest(
            "root",
            rw(),
            &OwnedCall::Rename {
                from: "a".into(),
                to: "b".into()
            }
        ),
        argument_digest(
            "root",
            rw(),
            &OwnedCall::Rename {
                from: "b".into(),
                to: "a".into()
            }
        )
    );
    assert!(validate_path(Path::new("../outside").to_str().unwrap(), false).is_err());
}
