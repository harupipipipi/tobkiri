//! Actual process termination proves fail-closed restart behavior. It is not
//! evidence for power-loss or filesystem durability beyond the required fsyncs.
use super::*;
use async_trait::async_trait;
use std::{
    os::unix::{fs::PermissionsExt, process::ExitStatusExt},
    process::{Command, Stdio},
    time::Instant,
};
use tobkiri_canon::ids::{PackHash, RequestId};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, CancellationToken, GateEngine, MediatorChain,
    PackPolicy, PermissionSet, Principal, ResourceLimits, StoragePermissions, TrustedPrompt,
};

struct Approve;
#[async_trait]
impl TrustedPrompt for Approve {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        ApprovalAnswer::Approve
    }
}
fn profile() -> ProfileId {
    ProfileId::parse("prof_move_kill").unwrap()
}
fn owner(old: bool) -> PackId {
    PackId::parse(if old {
        "local.move-old"
    } else {
        "local.move-new"
    })
    .unwrap()
}
fn caller(gate: &GateEngine, approvals: &crate::Approvals, old: bool) -> CallCx {
    let pack = owner(old);
    let hash = PackHash::new(Digest::hash(pack.as_str().as_bytes()));
    let limits = ResourceLimits {
        fuel_per_call: 1_000_000,
        memory_mib: 64,
        wall_ms: 30_000,
        table_elements: 4096,
        output_bytes: 4 * 1024 * 1024,
    };
    let permissions = PermissionSet {
        storage: StoragePermissions {
            private: true,
            public_read: true,
            public_write: true,
        },
        wasm: limits.clone(),
        ..Default::default()
    };
    gate.register_pack(
        &profile(),
        &pack,
        &hash,
        PackPolicy {
            declaration: permissions.clone(),
            ceiling: permissions.clone(),
            pack_limits: permissions.clone(),
        },
    )
    .unwrap();
    approvals
        .set_permissions(&profile(), &pack, &hash, &permissions)
        .unwrap();
    CallCx::root(
        profile(),
        Principal::Pack { pack, hash },
        RequestId::parse("req_move_kill").unwrap(),
        CancellationToken::new(),
        Instant::now() + Duration::from_secs(30),
        limits,
    )
}

#[test]
fn kill_child() {
    let Ok(path) = std::env::var("TOBKIRI_TEST_DATA_MOVE_ROOT") else {
        return;
    };
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            let root = DataRoot::open(path).unwrap();
            let approvals = Arc::new(crate::Approvals::open(&root, &profile()).unwrap());
            let audit = Arc::new(
                crate::AuditLog::open(&root, &profile(), crate::AuditQuota::default()).unwrap(),
            );
            let gate = GateEngine::new(
                approvals.clone(),
                audit,
                Arc::new(Approve),
                MediatorChain::default(),
            );
            let old = caller(&gate, &approvals, true);
            let new = caller(&gate, &approvals, false);
            let stores = TieredStore::open(&root, &profile()).unwrap();
            stores
                .register(
                    &owner(true),
                    "notes",
                    StoreTier::Public,
                    "local.data.notes@1.0",
                )
                .unwrap();
            let (request, args) = stores.open_request(&owner(true), "notes").unwrap();
            let handle = stores
                .open_store(
                    &old,
                    &owner(true),
                    "notes",
                    gate.authorize(&old, request, args).await.unwrap(),
                )
                .unwrap();
            let put = StoreCall::KvPut {
                key: "crash-retained".into(),
                value: b"preserved-data".to_vec(),
            };
            let grant = gate
                .authorize(&old, handle.request(&put).unwrap(), put.digest().unwrap())
                .await
                .unwrap();
            handle.execute(&old, put, grant).unwrap();
            drop(handle);
            stores.orphan_pack(&owner(true)).unwrap();
            // A closed empty DELETE journal exercises both admitted rename steps;
            // the pending guard prevents SQLite from opening either side at restart.
            std::fs::write(
                root.path().join(format!(
                    "profiles/{}/orphaned/{}/public/notes.sqlite-journal",
                    profile(),
                    owner(true)
                )),
                [],
            )
            .unwrap();
            let request = EffectRequest::Storage {
                profile: profile(),
                owner: owner(true),
                store: "notes".into(),
                tier: StoreTier::Public,
                operation: StoreOperation::Transfer,
            };
            let args = Digest::hash(
                &canonical(&(&owner(true), "notes", &owner(false), "local.data.notes@1.0"))
                    .unwrap(),
            );
            let grant = gate.authorize(&new, request, args).await.unwrap();
            stores
                .transfer(
                    &new,
                    &owner(true),
                    "notes",
                    &owner(false),
                    "local.data.notes@1.0",
                    grant,
                )
                .unwrap();
        });
}

#[test]
fn process_kill_at_each_move_checkpoint_preserves_bytes_and_blocks_both_endpoints() {
    for stage in [
        "prepared",
        "rename-main",
        "rename-journal",
        "dir-synced",
        "catalog-committed",
        "orphan-prepared",
        "orphan-dir-synced",
        "orphan-catalog-committed",
    ] {
        let temp = tempfile::tempdir().unwrap();
        std::fs::set_permissions(temp.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let path = temp.path().canonicalize().unwrap();
        let ready = path.join("move-ready");
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "tiered::data_move_tests::kill_child",
                "--nocapture",
            ])
            .env("TOBKIRI_TEST_DATA_MOVE_ROOT", &path)
            .env("TOBKIRI_TEST_DATA_MOVE_STAGE", stage)
            .env("TOBKIRI_TEST_DATA_MOVE_READY", &ready)
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(15);
        while !ready.exists() {
            if let Some(status) = child.try_wait().unwrap() {
                panic!("child exited before {stage}: {status}");
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                panic!("child did not reach {stage}");
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(std::fs::read_to_string(&ready).unwrap(), stage);
        child.kill().unwrap();
        assert_eq!(child.wait().unwrap().signal(), Some(libc::SIGKILL));
        let root = DataRoot::open(&path).unwrap();
        let stores = TieredStore::open(&root, &profile()).unwrap();
        let count: u32 = stores
            .catalog
            .connection
            .lock()
            .unwrap()
            .query_row("SELECT count(*) FROM data_moves", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 1, "{stage}");
        let phase: String = stores
            .catalog
            .connection
            .lock()
            .unwrap()
            .query_row("SELECT phase FROM data_moves", [], |row| row.get(0))
            .unwrap();
        assert_eq!(
            phase,
            if matches!(stage, "catalog-committed" | "orphan-catalog-committed") {
                "committed"
            } else {
                "prepared"
            },
            "{stage}"
        );
        for affected in if stage.starts_with("orphan-") {
            vec![owner(true)]
        } else {
            vec![owner(true), owner(false)]
        } {
            assert!(
                matches!(
                    stores.register(
                        &affected,
                        "notes",
                        StoreTier::Public,
                        "local.data.notes@1.0"
                    ),
                    Err(StoreError::PendingDataMove)
                ),
                "{stage}"
            );
            assert!(
                matches!(
                    stores.open_request(&affected, "notes"),
                    Err(StoreError::PendingDataMove)
                ),
                "{stage}"
            );
        }
        let unrelated = PackId::parse("local.move-unrelated").unwrap();
        stores
            .register(
                &unrelated,
                "unrelated",
                StoreTier::Public,
                "local.data.other@1.0",
            )
            .unwrap();
        let locations = [
            format!(
                "profiles/{}/data/{}/public/notes.sqlite",
                profile(),
                owner(true)
            ),
            format!(
                "profiles/{}/orphaned/{}/public/notes.sqlite",
                profile(),
                owner(true)
            ),
            format!(
                "profiles/{}/data/{}/public/notes.sqlite",
                profile(),
                owner(false)
            ),
        ];
        let bodies = locations
            .iter()
            .filter_map(|location| std::fs::read(path.join(location)).ok())
            .collect::<Vec<_>>();
        assert_eq!(bodies.len(), 1, "{stage}");
        assert!(
            bodies[0]
                .windows(b"preserved-data".len())
                .any(|bytes| bytes == b"preserved-data"),
            "{stage}"
        );
        if !stage.starts_with("orphan-") {
            assert_eq!(
                locations
                    .iter()
                    .filter(|location| path.join(format!("{location}-journal")).exists())
                    .count(),
                1,
                "{stage}"
            );
        }
    }
}

#[test]
fn corrupt_move_journal_denies_startup_before_any_data_create() {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
    let stores = TieredStore::open(&root, &profile()).unwrap();
    stores
        .catalog
        .connection
        .lock()
        .unwrap()
        .execute(
            "INSERT INTO data_moves(id,phase,body) VALUES(?1,'prepared',x'7b7d')",
            ["0".repeat(64)],
        )
        .unwrap();
    drop(stores);
    assert!(matches!(
        TieredStore::open(&root, &profile()),
        Err(StoreError::PendingDataMove)
    ));
    assert!(
        !root
            .path()
            .join(format!("profiles/{}/data/local.move-old", profile()))
            .exists()
    );
}
