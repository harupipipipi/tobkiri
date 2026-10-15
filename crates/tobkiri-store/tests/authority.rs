use async_trait::async_trait;
use std::{
    collections::BTreeSet,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{Digest, ids::*};
use tobkiri_gate::*;
use tobkiri_store::*;
struct Prompt {
    count: AtomicUsize,
    allow: bool,
}
#[async_trait]
impl TrustedPrompt for Prompt {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        self.count.fetch_add(1, Ordering::SeqCst);
        if self.allow {
            ApprovalAnswer::Approve
        } else {
            ApprovalAnswer::Unavailable
        }
    }
}
struct Widen;
#[async_trait]
impl Mediator for Widen {
    async fn review(&self, _: &ApprovalPrompt) -> Verdict {
        Verdict::Constrain(PermissionSet {
            native: true,
            ..Default::default()
        })
    }
}
struct RemoveWrite;
#[async_trait]
impl Mediator for RemoveWrite {
    async fn review(&self, _: &ApprovalPrompt) -> Verdict {
        Verdict::Constrain(PermissionSet::default())
    }
}
struct Fixture {
    _temp: tempfile::TempDir,
    root: DataRoot,
    profile: ProfileId,
    approvals: Arc<Approvals>,
    audit: Arc<AuditLog>,
    prompt: Arc<Prompt>,
}
impl Fixture {
    fn new(allow: bool) -> Self {
        let temp = tempfile::tempdir().unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(temp.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse("prof_authority").unwrap();
        let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
        let audit = Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap());
        Self {
            _temp: temp,
            root,
            profile,
            approvals,
            audit,
            prompt: Arc::new(Prompt {
                count: AtomicUsize::new(0),
                allow,
            }),
        }
    }
    fn engine(&self, chain: MediatorChain) -> GateEngine {
        GateEngine::new(
            self.approvals.clone(),
            self.audit.clone(),
            self.prompt.clone(),
            chain,
        )
    }
    fn cx(&self, p: Principal) -> CallCx {
        CallCx::root(
            self.profile.clone(),
            p,
            RequestId::parse("req_test").unwrap(),
            CancellationToken::new(),
            Instant::now() + Duration::from_secs(30),
            limits(),
        )
    }
    fn setup(&self, g: &GateEngine, id: &str, p: PermissionSet) -> CallCx {
        let pack = PackId::parse(id).unwrap();
        let hash = PackHash::new(Digest::hash(id.as_bytes()));
        g.register_pack(
            &self.profile,
            &pack,
            &hash,
            PackPolicy {
                declaration: p.clone(),
                ceiling: p.clone(),
                pack_limits: p.clone(),
            },
        )
        .unwrap();
        self.approvals
            .set_permissions(&self.profile, &pack, &hash, &p)
            .unwrap();
        self.cx(Principal::Pack { pack, hash })
    }
}
fn strict_digest<T: serde::Serialize>(value: &T) -> Digest {
    let bytes = serde_json::to_vec(value).unwrap();
    let value = tobkiri_canon::parse(&bytes, tobkiri_canon::Variant::StrictV1).unwrap();
    Digest::hash(&tobkiri_canon::encode(&value, tobkiri_canon::Variant::StrictV1).unwrap())
}
fn limits() -> ResourceLimits {
    ResourceLimits {
        fuel_per_call: 1_000_000,
        memory_mib: 64,
        wall_ms: 30_000,
        table_elements: 10_000,
        output_bytes: 4 * 1024 * 1024,
    }
}
fn perms() -> PermissionSet {
    PermissionSet {
        storage: StoragePermissions {
            private: true,
            public_read: true,
            public_write: true,
        },
        fs: vec![FsPermission {
            root: "project".into(),
            read: true,
            write: true,
        }],
        audit_read: true,
        wasm: limits(),
        loops: LoopLimits {
            max_iterations: 8,
            max_wall_ms: 30_000,
            max_cost_units: 1000,
        },
        ..Default::default()
    }
}
#[tokio::test]
async fn mediators_cannot_widen_or_restore_removed_permissions() {
    for mediator in [
        Arc::new(Widen) as Arc<dyn Mediator>,
        Arc::new(RemoveWrite) as Arc<dyn Mediator>,
    ] {
        let f = Fixture::new(true);
        let gate = f.engine(MediatorChain::new(vec![mediator]));
        let cx = f.setup(&gate, "local.owner", perms());
        let req = EffectRequest::Fs {
            root: "project".into(),
            path: "a.txt".into(),
            operation: FsOperation::Write,
        };
        assert!(
            gate.authorize(&cx, req, Digest::hash(b"args"))
                .await
                .is_err()
        );
        assert_eq!(f.prompt.count.load(Ordering::SeqCst), 0);
    }
}
#[tokio::test]
async fn noninteractive_and_cancelled_or_expired_calls_never_dispatch() {
    let f = Fixture::new(false);
    let gate = f.engine(MediatorChain::default());
    let cx = f.setup(&gate, "local.owner", perms());
    let req = EffectRequest::Fs {
        root: "project".into(),
        path: "a".into(),
        operation: FsOperation::Write,
    };
    assert!(matches!(
        gate.authorize(&cx, req, Digest::hash(b"a")).await,
        Err(GateError::ApprovalRequired)
    ));
    cx.cancel().cancel();
    assert!(matches!(
        gate.authorize(&cx, EffectRequest::AuditRead, Digest::hash(b"a"))
            .await,
        Err(GateError::Cancelled)
    ));
    let expired = CallCx::root(
        f.profile.clone(),
        cx.principal().clone(),
        RequestId::parse("req_expired").unwrap(),
        CancellationToken::new(),
        Instant::now() - Duration::from_secs(1),
        limits(),
    );
    assert!(matches!(
        gate.authorize(&expired, EffectRequest::AuditRead, Digest::hash(b"a"))
            .await,
        Err(GateError::Deadline)
    ));
}
#[tokio::test]
async fn a_pack_hash_update_prompts_only_for_additions_and_other_profiles_have_no_approval() {
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let pack = PackId::parse("local.owner").unwrap();
    let first = PackHash::new(Digest::hash(b"first"));
    let second = PackHash::new(Digest::hash(b"second"));
    let third = PackHash::new(Digest::hash(b"third"));
    let p = perms();
    for hash in [&first, &second] {
        gate.register_pack(
            &f.profile,
            &pack,
            hash,
            PackPolicy {
                declaration: p.clone(),
                ceiling: p.clone(),
                pack_limits: p.clone(),
            },
        )
        .unwrap();
    }
    let user = f.cx(Principal::User);
    gate.approve_pack_permissions(&user, &pack, None, &first)
        .await
        .unwrap();
    let baseline = f.prompt.count.load(Ordering::SeqCst);
    gate.approve_pack_permissions(&user, &pack, Some(&first), &second)
        .await
        .unwrap();
    assert_eq!(f.prompt.count.load(Ordering::SeqCst), baseline);
    let mut added = p;
    added.network.origins = BTreeSet::from(["https://new.example".into()]);
    gate.register_pack(
        &f.profile,
        &pack,
        &third,
        PackPolicy {
            declaration: added.clone(),
            ceiling: added.clone(),
            pack_limits: added,
        },
    )
    .unwrap();
    gate.approve_pack_permissions(&user, &pack, Some(&second), &third)
        .await
        .unwrap();
    assert_eq!(f.prompt.count.load(Ordering::SeqCst), baseline + 1);
    let other = ProfileId::parse("prof_other").unwrap();
    let other_store = Approvals::open(&f.root, &other).unwrap();
    assert!(
        other_store
            .permissions(&other, &pack, &third)
            .unwrap()
            .is_none()
    );
    f.audit.verify().unwrap();
}
#[tokio::test]
async fn user_overlay_direct_effect_always_reaches_trusted_prompt() {
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let profile_hash = Digest::hash(b"explicit User Profile document");
    let revision = Digest::hash(b"explicit User compiled revision");
    gate.register_user_policy_for_revision(
        &f.profile,
        &profile_hash,
        &revision,
        0,
        perms(),
        perms(),
    )
    .unwrap();
    gate.approve_user_permissions(
        &f.cx(Principal::User),
        &profile_hash,
        &revision,
        0,
        &perms(),
    )
    .await
    .unwrap();
    let baseline = f.prompt.count.load(Ordering::SeqCst);
    let cx = f.cx(Principal::User).with_revision(revision);
    let req = EffectRequest::Fs {
        root: "project".into(),
        path: "chosen.txt".into(),
        operation: FsOperation::Write,
    };
    let args = Digest::hash(b"write-data");
    let g = gate
        .authorize(&cx, req.clone(), args.clone())
        .await
        .unwrap();
    assert_eq!(f.prompt.count.load(Ordering::SeqCst), baseline + 1);
    g.consume(&cx, &req, &args).unwrap();
}
#[tokio::test]
async fn orphan_takeover_requires_identical_shape_and_one_trusted_approval() {
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let old = f.setup(&gate, "local.old", perms());
    let new = f.setup(&gate, "local.new", perms());
    let old_pack = old.principal().pack().unwrap().0;
    let new_pack = new.principal().pack().unwrap().0;
    let stores = TieredStore::open(&f.root, &f.profile).unwrap();
    stores
        .register(old_pack, "notes", StoreTier::Public, "local.data.notes@1.0")
        .unwrap();
    let (req, args) = stores.open_request(old_pack, "notes").unwrap();
    let grant = gate.authorize(&old, req, args).await.unwrap();
    let h = stores.open_store(&old, old_pack, "notes", grant).unwrap();
    let call = StoreCall::KvPut {
        key: "retained".into(),
        value: b"saved".to_vec(),
    };
    let req = h.request(&call).unwrap();
    let g = gate
        .authorize(&old, req, call.digest().unwrap())
        .await
        .unwrap();
    h.execute(&old, call, g).unwrap();
    drop(h);
    let orphans = stores.orphan_pack(old_pack).unwrap();
    assert!(orphans[0].orphaned);
    let req = EffectRequest::Storage {
        profile: f.profile.clone(),
        owner: old_pack.clone(),
        store: "notes".into(),
        tier: StoreTier::Public,
        operation: StoreOperation::Transfer,
    };
    let args = Digest::hash(&canonical(&(
        old_pack,
        "notes",
        new_pack,
        "other.shape@1.0",
    )));
    let g = gate.authorize(&new, req.clone(), args).await.unwrap();
    assert!(
        stores
            .transfer(&new, old_pack, "notes", new_pack, "other.shape@1.0", g)
            .is_err()
    );
    let baseline = f.prompt.count.load(Ordering::SeqCst);
    let args = Digest::hash(&canonical(&(
        old_pack,
        "notes",
        new_pack,
        "local.data.notes@1.0",
    )));
    let g = gate.authorize(&new, req, args).await.unwrap();
    stores
        .transfer(&new, old_pack, "notes", new_pack, "local.data.notes@1.0", g)
        .unwrap();
    assert_eq!(f.prompt.count.load(Ordering::SeqCst), baseline + 1);
    let (req, args) = stores.open_request(new_pack, "notes").unwrap();
    let g = gate.authorize(&new, req, args).await.unwrap();
    let h = stores.open_store(&new, new_pack, "notes", g).unwrap();
    let call = StoreCall::KvGet {
        key: "retained".into(),
    };
    let g = gate
        .authorize(&new, h.request(&call).unwrap(), call.digest().unwrap())
        .await
        .unwrap();
    assert!(matches!(h.execute(&new,call,g).unwrap(),StoreResult::Value(Some(v))if v==b"saved"));
}

#[tokio::test]
async fn private_takeover_moves_every_shape_and_preserves_both_stores() {
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let old = f.setup(&gate, "local.private-old", perms());
    let new = f.setup(&gate, "local.private-new", perms());
    let old_pack = old.principal().pack().unwrap().0;
    let new_pack = new.principal().pack().unwrap().0;
    let stores = TieredStore::open(&f.root, &f.profile).unwrap();
    let shapes = std::collections::BTreeMap::from([
        ("first".to_owned(), "local.data.first@1.0".to_owned()),
        ("second".to_owned(), "local.data.second@1.0".to_owned()),
    ]);
    for (name, shape) in &shapes {
        stores
            .register(old_pack, name, StoreTier::Private, shape)
            .unwrap();
        let (request, args) = stores.open_request(old_pack, name).unwrap();
        let handle = stores
            .open_store(
                &old,
                old_pack,
                name,
                gate.authorize(&old, request, args).await.unwrap(),
            )
            .unwrap();
        let put = StoreCall::KvPut {
            key: "key".into(),
            value: name.as_bytes().to_vec(),
        };
        let grant = gate
            .authorize(&old, handle.request(&put).unwrap(), put.digest().unwrap())
            .await
            .unwrap();
        handle.execute(&old, put, grant).unwrap();
    }
    stores.orphan_pack(old_pack).unwrap();
    let request = EffectRequest::Storage {
        profile: f.profile.clone(),
        owner: old_pack.clone(),
        store: "*".into(),
        tier: StoreTier::Private,
        operation: StoreOperation::Transfer,
    };
    let args = strict_digest(&(old_pack, new_pack, &shapes));
    let baseline = f.prompt.count.load(Ordering::SeqCst);
    let grant = gate.authorize(&new, request, args).await.unwrap();
    stores
        .transfer_private_database(&new, old_pack, new_pack, &shapes, grant)
        .unwrap();
    assert_eq!(f.prompt.count.load(Ordering::SeqCst), baseline + 1);
    assert!(
        !f.root
            .path()
            .join(format!(
                "profiles/{}/orphaned/{old_pack}/private.sqlite",
                f.profile
            ))
            .exists()
    );
    for name in shapes.keys() {
        assert!(matches!(
            stores.describe(old_pack, name),
            Err(StoreError::NotFound)
        ));
        let descriptor = stores.describe(new_pack, name).unwrap();
        assert_eq!(descriptor.tier, StoreTier::Private);
        assert!(!descriptor.orphaned);
        let (request, args) = stores.open_request(new_pack, name).unwrap();
        let handle = stores
            .open_store(
                &new,
                new_pack,
                name,
                gate.authorize(&new, request, args).await.unwrap(),
            )
            .unwrap();
        let get = StoreCall::KvGet { key: "key".into() };
        let grant = gate
            .authorize(&new, handle.request(&get).unwrap(), get.digest().unwrap())
            .await
            .unwrap();
        assert!(
            matches!(handle.execute(&new, get, grant).unwrap(), StoreResult::Value(Some(value)) if value == name.as_bytes())
        );
    }
}

#[tokio::test]
async fn takeover_refuses_a_live_old_handle_then_succeeds_after_confirmed_close() {
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let old = f.setup(&gate, "local.live-old", perms());
    let new = f.setup(&gate, "local.live-new", perms());
    let old_pack = old.principal().pack().unwrap().0;
    let new_pack = new.principal().pack().unwrap().0;
    let stores = TieredStore::open(&f.root, &f.profile).unwrap();
    stores
        .register(old_pack, "notes", StoreTier::Public, "local.data.notes@1.0")
        .unwrap();
    let (request, args) = stores.open_request(old_pack, "notes").unwrap();
    let handle = stores
        .open_store(
            &old,
            old_pack,
            "notes",
            gate.authorize(&old, request, args).await.unwrap(),
        )
        .unwrap();
    stores.orphan_pack(old_pack).unwrap();
    let request = EffectRequest::Storage {
        profile: f.profile.clone(),
        owner: old_pack.clone(),
        store: "notes".into(),
        tier: StoreTier::Public,
        operation: StoreOperation::Transfer,
    };
    let args = strict_digest(&(old_pack, "notes", new_pack, "local.data.notes@1.0"));
    let grant = gate
        .authorize(&new, request.clone(), args.clone())
        .await
        .unwrap();
    assert!(matches!(
        stores.transfer(
            &new,
            old_pack,
            "notes",
            new_pack,
            "local.data.notes@1.0",
            grant
        ),
        Err(StoreError::Conflict(_))
    ));
    assert!(stores.describe(old_pack, "notes").unwrap().orphaned);
    assert!(matches!(
        stores.describe(new_pack, "notes"),
        Err(StoreError::NotFound)
    ));
    drop(handle);
    let grant = gate.authorize(&new, request, args).await.unwrap();
    stores
        .transfer(
            &new,
            old_pack,
            "notes",
            new_pack,
            "local.data.notes@1.0",
            grant,
        )
        .unwrap();
    assert!(!stores.describe(new_pack, "notes").unwrap().orphaned);
}

#[tokio::test]
async fn failed_takeover_catalog_update_rolls_back_the_exact_database_move() {
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let old = f.setup(&gate, "local.rollback-old", perms());
    let new = f.setup(&gate, "local.rollback-new", perms());
    let old_pack = old.principal().pack().unwrap().0;
    let new_pack = new.principal().pack().unwrap().0;
    let stores = TieredStore::open(&f.root, &f.profile).unwrap();
    stores
        .register(old_pack, "notes", StoreTier::Public, "local.data.notes@1.0")
        .unwrap();
    let (request, args) = stores.open_request(old_pack, "notes").unwrap();
    let handle = stores
        .open_store(
            &old,
            old_pack,
            "notes",
            gate.authorize(&old, request, args).await.unwrap(),
        )
        .unwrap();
    let put = StoreCall::KvPut {
        key: "retained".into(),
        value: b"rollback-kept".to_vec(),
    };
    let grant = gate
        .authorize(&old, handle.request(&put).unwrap(), put.digest().unwrap())
        .await
        .unwrap();
    handle.execute(&old, put, grant).unwrap();
    drop(handle);
    stores.orphan_pack(old_pack).unwrap();
    let catalog = rusqlite::Connection::open(
        f.root
            .path()
            .join(format!("profiles/{}/data/stores.meta", f.profile)),
    )
    .unwrap();
    catalog.execute_batch("CREATE TRIGGER fail_takeover BEFORE UPDATE OF owner ON stores BEGIN SELECT RAISE(ABORT,'injected takeover metadata failure'); END;").unwrap();
    let request = EffectRequest::Storage {
        profile: f.profile.clone(),
        owner: old_pack.clone(),
        store: "notes".into(),
        tier: StoreTier::Public,
        operation: StoreOperation::Transfer,
    };
    let args = strict_digest(&(old_pack, "notes", new_pack, "local.data.notes@1.0"));
    let grant = gate
        .authorize(&new, request.clone(), args.clone())
        .await
        .unwrap();
    assert!(matches!(
        stores.transfer(
            &new,
            old_pack,
            "notes",
            new_pack,
            "local.data.notes@1.0",
            grant
        ),
        Err(StoreError::Sql(_))
    ));
    assert!(stores.describe(old_pack, "notes").unwrap().orphaned);
    assert!(matches!(
        stores.describe(new_pack, "notes"),
        Err(StoreError::NotFound)
    ));
    assert!(
        f.root
            .path()
            .join(format!(
                "profiles/{}/orphaned/{old_pack}/public/notes.sqlite",
                f.profile
            ))
            .is_file()
    );
    assert!(
        !f.root
            .path()
            .join(format!(
                "profiles/{}/data/{new_pack}/public/notes.sqlite",
                f.profile
            ))
            .exists()
    );
    catalog
        .execute_batch("DROP TRIGGER fail_takeover;")
        .unwrap();
    drop(catalog);
    let grant = gate.authorize(&new, request, args).await.unwrap();
    stores
        .transfer(
            &new,
            old_pack,
            "notes",
            new_pack,
            "local.data.notes@1.0",
            grant,
        )
        .unwrap();
    let (request, args) = stores.open_request(new_pack, "notes").unwrap();
    let handle = stores
        .open_store(
            &new,
            new_pack,
            "notes",
            gate.authorize(&new, request, args).await.unwrap(),
        )
        .unwrap();
    let get = StoreCall::KvGet {
        key: "retained".into(),
    };
    let grant = gate
        .authorize(&new, handle.request(&get).unwrap(), get.digest().unwrap())
        .await
        .unwrap();
    assert!(
        matches!(handle.execute(&new, get, grant).unwrap(), StoreResult::Value(Some(value)) if value == b"rollback-kept")
    );
}
fn canonical<T: serde::Serialize>(v: &T) -> Vec<u8> {
    let raw = serde_json::to_vec(v).unwrap();
    let value = tobkiri_canon::parse(&raw, tobkiri_canon::Variant::StrictV1).unwrap();
    tobkiri_canon::encode(&value, tobkiri_canon::Variant::StrictV1).unwrap()
}
#[tokio::test]
async fn init_rejects_interactive_effect_before_prompt_and_inherits_callee_limits() {
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let cx = f.setup(&gate, "local.owner", perms());
    let mut own = limits();
    own.memory_mib = 256;
    own.fuel_per_call = 9_000_000;
    own.wall_ms = 1000;
    let init = cx
        .callee(cx.principal().clone(), own.clone())
        .without_interactive_approval();
    assert_eq!(init.limits(), &own);
    assert!(init.deadline() <= cx.deadline());
    assert!(!init.child(cx.principal().clone()).interactive_approval());
    assert!(matches!(
        gate.authorize(
            &init,
            EffectRequest::Fs {
                root: "project".into(),
                path: "init-write".into(),
                operation: FsOperation::Write
            },
            Digest::hash(b"data")
        )
        .await,
        Err(GateError::ApprovalRequired)
    ));
    assert_eq!(f.prompt.count.load(Ordering::SeqCst), 0);
    cx.cancel().cancel();
    assert!(matches!(init.check_active(), Err(GateError::Cancelled)));
}
#[tokio::test]
async fn network_grant_requires_actual_url_origin_to_match_the_exact_approved_origin() {
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let mut p = perms();
    p.network.origins.insert("https://api.example".into());
    let cx = f.setup(&gate, "local.owner", p);
    for url in [
        "http://127.0.0.1:18080/v1",
        "https://api.example@127.0.0.1/v1",
        "https://user:password@api.example/v1",
        "file:///etc/passwd",
    ] {
        assert!(
            gate.authorize(
                &cx,
                EffectRequest::Network {
                    origin: "https://api.example".into(),
                    method: "GET".into(),
                    url: url.into()
                },
                Digest::hash(b"args")
            )
            .await
            .is_err(),
            "accepted {url}"
        );
    }
    assert!(
        gate.authorize(
            &cx,
            EffectRequest::Network {
                origin: "https://api.example".into(),
                method: "GET".into(),
                url: "https://api.example/v1".into()
            },
            Digest::hash(b"args")
        )
        .await
        .is_ok()
    );
}
#[tokio::test]
async fn verified_seeds_replace_readonly_defaults_but_preserve_private_user_values() {
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let cx = f.setup(&gate, "local.owner", perms());
    let pack = cx.principal().pack().unwrap().0;
    let stores = TieredStore::open(&f.root, &f.profile).unwrap();
    stores
        .register(
            pack,
            "builtins",
            StoreTier::ReadOnly,
            "local.owner.defaults@1.0",
        )
        .unwrap();
    stores
        .register(
            pack,
            "settings",
            StoreTier::Private,
            "local.owner.defaults@1.0",
        )
        .unwrap();
    let first = PackHash::new(Digest::hash(b"first"));
    let second = PackHash::new(Digest::hash(b"second"));
    let seed =
        tobkiri_canon::parse(br#"{"language":"one"}"#, tobkiri_canon::Variant::StrictV1).unwrap();
    for name in ["builtins", "settings"] {
        stores.apply_seed(pack, name, &first, &seed).unwrap();
    }
    let (req, args) = stores.open_request(pack, "settings").unwrap();
    let g = gate.authorize(&cx, req, args).await.unwrap();
    let private = stores.open_store(&cx, pack, "settings", g).unwrap();
    let put = StoreCall::KvPut {
        key: "language".into(),
        value: br#""user""#.to_vec(),
    };
    let g = gate
        .authorize(&cx, private.request(&put).unwrap(), put.digest().unwrap())
        .await
        .unwrap();
    private.execute(&cx, put, g).unwrap();
    let updated = tobkiri_canon::parse(
        br#"{"language":"two","added":"new"}"#,
        tobkiri_canon::Variant::StrictV1,
    )
    .unwrap();
    for name in ["builtins", "settings"] {
        stores.apply_seed(pack, name, &second, &updated).unwrap();
    }
    for (name, expected) in [
        ("builtins", br#""two""#.as_slice()),
        ("settings", br#""user""#.as_slice()),
    ] {
        let (req, args) = stores.open_request(pack, name).unwrap();
        let g = gate.authorize(&cx, req, args).await.unwrap();
        let handle = stores.open_store(&cx, pack, name, g).unwrap();
        let call = StoreCall::KvGet {
            key: "language".into(),
        };
        let g = gate
            .authorize(&cx, handle.request(&call).unwrap(), call.digest().unwrap())
            .await
            .unwrap();
        assert!(
            matches!(handle.execute(&cx,call,g).unwrap(),StoreResult::Value(Some(v))if v==expected)
        );
    }
    let call = StoreCall::KvGet {
        key: "added".into(),
    };
    let g = gate
        .authorize(&cx, private.request(&call).unwrap(), call.digest().unwrap())
        .await
        .unwrap();
    assert!(
        matches!(private.execute(&cx,call,g).unwrap(),StoreResult::Value(Some(v))if v==br#""new""#)
    );
}
#[tokio::test]
async fn dangerous_global_storage_can_share_data_only_after_explicit_permissions_and_prompt() {
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let mut p = perms();
    p.cross_profile_storage = true;
    let cx = f.setup(&gate, "local.owner", p);
    let (req, args) = UserGlobalStore::open_request("shared").unwrap();
    let g = gate.authorize(&cx, req, args).await.unwrap();
    let global = UserGlobalStore::open(&f.root, &cx, "shared", g).unwrap();
    assert!(f.root.path().join("user-global/shared.sqlite").is_file());
    assert!(
        !f.root
            .path()
            .join(format!("profiles/{}/user-global/shared.sqlite", f.profile))
            .exists()
    );
    let put = StoreCall::KvPut {
        key: "shared-key".into(),
        value: b"global".to_vec(),
    };
    let g = gate
        .authorize(&cx, global.request(&put), put.digest().unwrap())
        .await
        .unwrap();
    global.execute(&cx, put, g).unwrap();
    let get = StoreCall::KvGet {
        key: "shared-key".into(),
    };
    let g = gate
        .authorize(&cx, global.request(&get), get.digest().unwrap())
        .await
        .unwrap();
    assert!(
        matches!(global.execute(&cx,get,g).unwrap(),StoreResult::Value(Some(v))if v==b"global")
    );
    assert_eq!(f.prompt.count.load(Ordering::SeqCst), 3);

    let second_profile = ProfileId::parse("prof_global_second").unwrap();
    let second_approvals = Arc::new(Approvals::open(&f.root, &second_profile).unwrap());
    let second_audit =
        Arc::new(AuditLog::open(&f.root, &second_profile, AuditQuota::default()).unwrap());
    let second_prompt = Arc::new(Prompt {
        count: AtomicUsize::new(0),
        allow: true,
    });
    let second_gate = GateEngine::new(
        second_approvals.clone(),
        second_audit,
        second_prompt,
        MediatorChain::default(),
    );
    let second_pack = PackId::parse("local.second-owner").unwrap();
    let second_hash = PackHash::new(Digest::hash(b"second owner"));
    let mut second_permissions = perms();
    second_permissions.cross_profile_storage = true;
    second_gate
        .register_pack(
            &second_profile,
            &second_pack,
            &second_hash,
            PackPolicy {
                declaration: second_permissions.clone(),
                ceiling: second_permissions.clone(),
                pack_limits: second_permissions.clone(),
            },
        )
        .unwrap();
    second_approvals
        .set_permissions(
            &second_profile,
            &second_pack,
            &second_hash,
            &second_permissions,
        )
        .unwrap();
    let second_cx = CallCx::root(
        second_profile.clone(),
        Principal::Pack {
            pack: second_pack,
            hash: second_hash,
        },
        RequestId::parse("req_global_second").unwrap(),
        CancellationToken::new(),
        Instant::now() + Duration::from_secs(30),
        limits(),
    )
    .with_security_epoch(second_gate.epoch(&second_profile).unwrap());
    let (request, arguments) = UserGlobalStore::open_request("shared").unwrap();
    let grant = second_gate
        .authorize(&second_cx, request, arguments)
        .await
        .unwrap();
    let second_global = UserGlobalStore::open(&f.root, &second_cx, "shared", grant).unwrap();
    let get = StoreCall::KvGet {
        key: "shared-key".into(),
    };
    let grant = second_gate
        .authorize(
            &second_cx,
            second_global.request(&get),
            get.digest().unwrap(),
        )
        .await
        .unwrap();
    assert!(matches!(
        second_global.execute(&second_cx, get, grant).unwrap(),
        StoreResult::Value(Some(value)) if value == b"global"
    ));
}

#[tokio::test]
async fn frozen_entries_and_live_handles_require_exact_revision_approval_and_budgets() {
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let mut p = perms();
    let shape = ShapeRef::parse("local.owner.flow@1.0").unwrap();
    p.ports.insert(shape.to_string());
    let cx = f.setup(&gate, "local.owner", p);
    let (pack, hash) = cx.principal().pack().unwrap();
    let revision = Digest::hash(b"frozen-revision");
    let entry = EffectRequest::WasmEntry {
        pack: pack.clone(),
        hash: hash.clone(),
        entry: WasmEntryKind::Init,
    };
    assert!(
        gate.authorize(
            &cx.clone().non_interactive(),
            entry.clone(),
            Digest::hash(b"init")
        )
        .await
        .is_err()
    );
    let frozen = cx.clone().with_revision(revision.clone());
    assert!(gate.check_context(&frozen).is_err());
    gate.register_revision(
        &f.profile,
        &revision,
        std::collections::BTreeMap::from([((pack.clone(), hash.clone()), limits())]),
    )
    .unwrap();
    let input_view = ShapeRef::parse("local.owner.input@1.0").unwrap();
    gate.register_operation_views(
        &f.profile,
        &revision,
        std::collections::BTreeMap::from([
            (shape.clone(), std::collections::BTreeSet::new()),
            (
                input_view.clone(),
                std::collections::BTreeSet::from([OpName::parse("read").unwrap()]),
            ),
        ]),
    )
    .unwrap();
    gate.check_context(&frozen).unwrap();
    assert!(
        gate.authorize(&frozen, entry.clone(), Digest::hash(b"init"))
            .await
            .is_err()
    );
    let grant = gate
        .authorize(
            &frozen.clone().non_interactive(),
            entry.clone(),
            Digest::hash(b"init"),
        )
        .await
        .unwrap();
    let id = grant.id().clone();
    grant
        .consume(
            &frozen.clone().non_interactive(),
            &entry,
            &Digest::hash(b"init"),
        )
        .unwrap();
    gate.record_outcome(id, Outcome::Succeeded).await.unwrap();
    assert!(
        gate.authorize(
            &frozen,
            EffectRequest::WasmEntry {
                pack: pack.clone(),
                hash: hash.clone(),
                entry: WasmEntryKind::MediatorReview
            },
            Digest::hash(b"review")
        )
        .await
        .is_err()
    );
    // This Gate-membership fixture supplies a concrete canonical Flow document
    // and its exact input WIT. The AST and test-compiled plan identities derive
    // from that document/context; compiler validation is tested by Profile.
    let document = serde_json::json!({
        "format": "tobkiri.flow/1",
        "id": "frozen-entry",
        "implements": shape.to_string(),
        "trigger": { "input": input_view.to_string() },
        "loop_limits": "profile-default",
        "steps": [{ "overlay": "open", "return": "$input" }],
        "on_error": { "default": "fail", "steps": {} }
    });
    let input_wit = "package local:owner@1.0.0; interface input { read: func() -> string; }";
    let target = FrozenFlowTarget {
        profile: f.profile.clone(),
        revision: revision.clone(),
        owner: pack.clone(),
        owner_hash: hash.clone(),
        flow_key: format!("{pack}/frozen-entry"),
        flow_ast: strict_digest(&document),
        plan_digest: strict_digest(&(
            "tobkiri-store-test-compiled-flow-plan-v1",
            &f.profile,
            &revision,
            pack,
            hash,
            &document,
            input_wit,
        )),
        implements: shape.clone(),
        input_view: input_view.clone(),
        entry_signature: strict_digest(&(
            "tobkiri-flow-entry-signature-v1",
            &input_view,
            input_wit,
        )),
    };
    let flow = EffectRequest::FlowRun {
        implements: shape.clone(),
        flow_owner: (pack.clone(), hash.clone()),
        flow_key: target.flow_key.clone(),
        target_digest: target.digest().unwrap(),
    };
    assert!(
        gate.authorize(&frozen, flow.clone(), Digest::hash(b"flow"))
            .await
            .is_err()
    );
    let _endpoints = gate
        .register_flow_endpoints(&f.profile, &revision, vec![target.clone()])
        .unwrap();
    assert!(
        gate.authorize(&frozen, flow, Digest::hash(b"flow"))
            .await
            .is_ok()
    );
    let mut changed_document = document;
    changed_document["steps"][0]["return"] = serde_json::json!("changed");
    let mut changed_target = target;
    changed_target.flow_ast = strict_digest(&changed_document);
    let changed_flow = EffectRequest::FlowRun {
        implements: shape,
        flow_owner: (pack.clone(), hash.clone()),
        flow_key: changed_target.flow_key.clone(),
        target_digest: changed_target.digest().unwrap(),
    };
    assert!(
        gate.authorize(&frozen, changed_flow, Digest::hash(b"flow"))
            .await
            .is_err()
    );
    let oversized = frozen.callee(
        cx.principal().clone(),
        ResourceLimits {
            memory_mib: 65,
            ..limits()
        },
    );
    assert!(gate.check_context(&oversized).is_err());
    gate.revoke_epoch(&f.profile).unwrap();
    assert!(matches!(
        gate.check_context(&frozen),
        Err(GateError::StaleEpoch)
    ));
}

#[tokio::test]
async fn exact_operation_views_and_original_authority_layers_cannot_be_masked_by_intersection() {
    let view = ShapeRef::parse("local.provider.iface@1.0").unwrap();
    let newer = ShapeRef::parse("local.provider.iface@1.1").unwrap();
    let op = OpName::parse("old-op").unwrap();
    for omitted in 0..4 {
        let f = Fixture::new(true);
        let gate = f.engine(MediatorChain::default());
        let pack = PackId::parse("local.caller").unwrap();
        let hash = PackHash::new(Digest::hash(b"caller"));
        let mut good = perms();
        good.ports.insert(view.to_string());
        let mut layers = [good.clone(), good.clone(), good.clone(), good.clone()];
        layers[omitted].ports.clear();
        gate.register_pack(
            &f.profile,
            &pack,
            &hash,
            PackPolicy {
                declaration: layers[0].clone(),
                ceiling: layers[1].clone(),
                pack_limits: layers[2].clone(),
            },
        )
        .unwrap();
        f.approvals
            .set_permissions(&f.profile, &pack, &hash, &layers[3])
            .unwrap();
        let revision = Digest::hash(b"exact views");
        gate.register_operation_views(
            &f.profile,
            &revision,
            std::collections::BTreeMap::from([
                (view.clone(), [op.clone()].into_iter().collect()),
                (
                    newer.clone(),
                    [op.clone(), OpName::parse("new-op").unwrap()]
                        .into_iter()
                        .collect(),
                ),
            ]),
        )
        .unwrap();
        let cx = f.cx(Principal::Pack { pack, hash }).with_revision(revision);
        assert!(
            gate.authorize(
                &cx,
                EffectRequest::PortCall {
                    shape: view.clone(),
                    op: op.clone()
                },
                Digest::hash(b"args")
            )
            .await
            .is_err()
        );
        assert_eq!(f.prompt.count.load(Ordering::SeqCst), 0);
    }
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let mut p = perms();
    p.ports.insert(view.to_string());
    let cx = f.setup(&gate, "local.caller", p.clone());
    let revision = Digest::hash(b"old exact view");
    gate.register_operation_views(
        &f.profile,
        &revision,
        std::collections::BTreeMap::from([
            (view.clone(), [op.clone()].into_iter().collect()),
            (
                newer.clone(),
                [op.clone(), OpName::parse("new-op").unwrap()]
                    .into_iter()
                    .collect(),
            ),
        ]),
    )
    .unwrap();
    let cx = cx.with_revision(revision);
    assert!(
        gate.authorize(
            &cx,
            EffectRequest::PortCall {
                shape: view.clone(),
                op: OpName::parse("new-op").unwrap()
            },
            Digest::hash(b"args")
        )
        .await
        .is_err()
    );
    assert!(
        gate.authorize(
            &cx,
            EffectRequest::PortCall {
                shape: newer,
                op: op.clone()
            },
            Digest::hash(b"args")
        )
        .await
        .is_err()
    );
    assert!(
        gate.authorize(
            &cx,
            EffectRequest::PortCall { shape: view, op },
            Digest::hash(b"args")
        )
        .await
        .is_ok()
    );
    let (pack, hash) = cx.principal().pack().unwrap();
    p.ports.insert("local.missing.iface@1.0".into());
    f.approvals
        .set_permissions(&f.profile, pack, hash, &p)
        .unwrap();
    assert!(
        gate.authorize(&cx, EffectRequest::AuditRead, Digest::hash(b"args"))
            .await
            .is_err(),
        "unknown approval view must not disappear in meet"
    );
}
#[tokio::test]
async fn user_authority_requires_fresh_document_revision_epoch_bound_approval_and_per_effect_prompt()
 {
    let f = Fixture::new(true);
    let gate = f.engine(MediatorChain::default());
    let revision = Digest::hash(b"user revision");
    let hash = Digest::hash(b"user profile document");
    let cx = f.cx(Principal::User).with_revision(revision.clone());
    assert!(matches!(
        gate.register_user_policy(&f.profile, perms()),
        Err(GateError::Unavailable)
    ));
    assert!(
        gate.authorize(&cx, EffectRequest::AuditRead, Digest::hash(b"args"))
            .await
            .is_err()
    );
    assert_eq!(f.prompt.count.load(Ordering::SeqCst), 0);
    gate.register_user_policy_for_revision(&f.profile, &hash, &revision, 0, perms(), perms())
        .unwrap();
    assert!(matches!(
        gate.authorize(&cx, EffectRequest::AuditRead, Digest::hash(b"args"))
            .await,
        Err(GateError::ApprovalRequired)
    ));
    gate.approve_user_permissions(&f.cx(Principal::User), &hash, &revision, 0, &perms())
        .await
        .unwrap();
    let count = f.prompt.count.load(Ordering::SeqCst);
    assert!(
        gate.authorize(&cx, EffectRequest::AuditRead, Digest::hash(b"args"))
            .await
            .is_ok()
    );
    assert_eq!(f.prompt.count.load(Ordering::SeqCst), count + 1);
    assert!(
        f.approvals
            .user_permissions(&f.profile, &Digest::hash(b"changed document"), &revision, 0)
            .unwrap()
            .is_none()
    );
    assert!(
        f.approvals
            .user_permissions(&f.profile, &hash, &Digest::hash(b"changed revision"), 0)
            .unwrap()
            .is_none()
    );
    gate.revoke_epoch(&f.profile).unwrap();
    assert!(matches!(
        gate.authorize(&cx, EffectRequest::AuditRead, Digest::hash(b"args"))
            .await,
        Err(GateError::StaleEpoch)
    ));
    let fresh = cx.with_security_epoch(1);
    gate.register_user_policy_for_revision(&f.profile, &hash, &revision, 1, perms(), perms())
        .unwrap();
    assert!(matches!(
        gate.authorize(&fresh, EffectRequest::AuditRead, Digest::hash(b"args"))
            .await,
        Err(GateError::ApprovalRequired)
    ));
    assert!(
        f.approvals
            .user_permissions(&f.profile, &hash, &revision, 1)
            .unwrap()
            .is_none()
    );
}
