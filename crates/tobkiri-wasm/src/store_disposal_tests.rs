//! Exact disposal-owner observations, not native-platform acceptance.
use super::*;
use std::sync::atomic::AtomicUsize;
use tobkiri_gate::{ControlKind, CoreControl, CoreControlLimits};

fn receipt(control: &CoreControl) -> Arc<StoreDisposal> {
    Arc::new(StoreDisposal::new(control, None, Arc::new(AtomicBool::new(false))).unwrap())
}

#[test]
fn shutdown_provenance_requires_the_original_store_generation_and_nonempty_factory_scope() {
    let control = CoreControl::default();
    let original = receipt(&control);
    let replacement = receipt(&control);
    let scope = Digest::hash(b"original-factory-instance");
    let other = Digest::hash(b"other-factory-instance");
    original
        .retirement_cause
        .get_or_init(|| StoreRetirementCause {
            generation: 7,
            shutdown_scope: Some(scope.clone()),
        });
    assert!(original.shutdown_retirement_matches(Some(7), Some(&scope)));
    assert!(!original.shutdown_retirement_matches(Some(8), Some(&scope)));
    assert!(!original.shutdown_retirement_matches(Some(7), Some(&other)));
    assert!(!original.shutdown_retirement_matches(Some(7), None));
    assert!(!replacement.shutdown_retirement_matches(Some(7), Some(&scope)));
    // A later inline observer cannot downgrade or replace the first receipt.
    original
        .retirement_cause
        .get_or_init(|| StoreRetirementCause {
            generation: 8,
            shutdown_scope: None,
        });
    assert!(original.shutdown_retirement_matches(Some(7), Some(&scope)));
    drop(original);
    drop(replacement);
    assert_eq!(control.usage().bytes, 0);
}

#[test]
fn general_retirement_cannot_be_promoted_to_a_later_shutdown() {
    let control = CoreControl::default();
    let original = receipt(&control);
    let scope = Digest::hash(b"later-factory-shutdown");
    original
        .retirement_cause
        .get_or_init(|| StoreRetirementCause {
            generation: 7,
            shutdown_scope: None,
        });
    original
        .retirement_cause
        .get_or_init(|| StoreRetirementCause {
            generation: 7,
            shutdown_scope: Some(scope.clone()),
        });
    assert!(!original.shutdown_retirement_matches(Some(7), Some(&scope)));
    assert!(!original.shutdown_retirement_matches(Some(7), None));
    drop(original);
    assert_eq!(control.usage().bytes, 0);
}

#[tokio::test]
async fn denied_task_funding_cannot_enter_retirement_or_cancel_generation() {
    let control = CoreControl::new(CoreControlLimits {
        max_tasks: 0,
        ..Default::default()
    })
    .unwrap();
    let token = tokio_util::sync::CancellationToken::new();
    let entered = AtomicUsize::new(0);
    let result = PreparedDisposalTask::prepare(&control, || {
        entered.fetch_add(1, Ordering::AcqRel);
        token.cancel();
        Ok(())
    });
    assert!(result.is_err());
    assert_eq!(entered.load(Ordering::Acquire), 0);
    assert!(!token.is_cancelled());
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    assert_eq!(control.usage().bytes, 0);
}

#[test]
fn missing_runtime_rejects_before_retirement_and_refunds_task_funding() {
    let control = CoreControl::default();
    let entered = AtomicUsize::new(0);
    let result = PreparedDisposalTask::prepare(&control, || {
        entered.fetch_add(1, Ordering::AcqRel);
        Ok(())
    });
    assert!(result.is_err());
    assert_eq!(entered.load(Ordering::Acquire), 0);
    assert_eq!(control.usage().bytes, 0);
}

#[tokio::test]
async fn retirement_rejection_refunds_the_already_admitted_task() {
    let control = CoreControl::default();
    let result = PreparedDisposalTask::prepare(&control, || -> Result<()> {
        assert_eq!(control.usage().count(ControlKind::Task), 1);
        bail!("injected retirement rejection before commit")
    });
    assert!(result.is_err());
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    assert_eq!(control.usage().bytes, 0);
}

#[tokio::test]
async fn unowned_receipt_rejects_without_waiting_a_fabricated_worker() {
    let control = CoreControl::default();
    let disposal = receipt(&control);
    {
        let observed = disposal.wait_owned_vm();
        tokio::pin!(observed);
        let std::task::Poll::Ready(Err(error)) = futures_util::poll!(&mut observed) else {
            panic!("unowned receipt must reject on its first poll")
        };
        assert!(error.to_string().contains("no live disposal owner"));
    }
    assert!(!disposal.is_complete());
    drop(disposal);
    assert_eq!(control.usage().bytes, 0);
}

#[tokio::test]
async fn live_owner_is_not_completion_and_another_receipt_cannot_satisfy_it() {
    let control = CoreControl::default();
    let original = receipt(&control);
    let replacement = receipt(&control);
    let owner = original.claim_owner().unwrap();
    assert!(original.claim_owner().is_err());
    let engine = wasmtime::Engine::default();
    let original_store = wasmtime::Store::new(&engine, ());
    let replacement_store = wasmtime::Store::new(&engine, ());
    drop(replacement_store);
    replacement.complete();
    assert!(replacement.wait_owned_vm().await.is_ok());
    {
        let observed = original.wait_owned_vm();
        tokio::pin!(observed);
        assert!(futures_util::poll!(&mut observed).is_pending());
    }
    assert!(!original.is_complete());
    drop(original_store);
    original.complete();
    drop(owner);
    original.wait_owned_vm().await.unwrap();
    drop(original);
    drop(replacement);
    assert_eq!(control.usage().bytes, 0);
}

#[tokio::test]
async fn original_worker_loss_wakes_waiter_without_claiming_store_destruction() {
    let control = CoreControl::default();
    let disposal = receipt(&control);
    let (prepared, owner) =
        PreparedDisposalTask::prepare(&control, || disposal.claim_owner()).unwrap();
    let worker = prepared.runtime.spawn(async move {
        let _control = prepared.control;
        let _owner = owner;
        std::future::pending::<()>().await;
    });
    let observed = disposal.wait_owned_vm();
    tokio::pin!(observed);
    assert!(futures_util::poll!(&mut observed).is_pending());
    worker.abort();
    assert!(worker.await.unwrap_err().is_cancelled());
    let std::task::Poll::Ready(Err(error)) = futures_util::poll!(&mut observed) else {
        panic!("owner loss must resolve the same observer without another grace")
    };
    assert!(error.to_string().contains("no live disposal owner"));
    assert!(!disposal.is_complete());
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test]
async fn abandoned_observer_keeps_original_owner_and_funding_until_real_drop() {
    let control = CoreControl::default();
    let disposal = receipt(&control);
    let (prepared, owner) =
        PreparedDisposalTask::prepare(&control, || disposal.claim_owner()).unwrap();
    let engine = wasmtime::Engine::default();
    let store = wasmtime::Store::new(&engine, ());
    let (release, blocked) = tokio::sync::oneshot::channel();
    let original = disposal.clone();
    let worker = prepared.runtime.spawn(async move {
        let _control = prepared.control;
        let _owner = owner;
        blocked.await.unwrap();
        drop(store);
        original.complete();
    });
    {
        let observed = disposal.wait_owned_vm();
        tokio::pin!(observed);
        assert!(futures_util::poll!(&mut observed).is_pending());
        // Drop only this observer; the independently owned original worker,
        // actual Store and task charge are not owned by the waiting future.
    }
    assert_eq!(disposal.custodians.load(Ordering::Acquire), 1);
    assert!(!disposal.is_complete());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    release.send(()).unwrap();
    let (first, second, joined) =
        tokio::join!(disposal.wait_owned_vm(), disposal.wait_owned_vm(), worker,);
    first.unwrap();
    second.unwrap();
    joined.unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    drop(disposal);
    assert_eq!(control.usage().bytes, 0);
}

struct StoreDropProbe(Arc<AtomicUsize>);
impl Drop for StoreDropProbe {
    fn drop(&mut self) {
        self.0.fetch_add(1, Ordering::AcqRel);
    }
}

#[tokio::test]
async fn inline_store_custody_survives_worker_loss_and_sealing_failure() {
    // This is the same helper used by failed PackInstance initialization. The
    // seal closure is a deterministic interleaving point, not a fake receipt.
    for fail_seal in [false, true] {
        let control = CoreControl::default();
        let disposal = receipt(&control);
        let worker_owner = disposal.claim_owner().unwrap();
        let destroyed = Arc::new(AtomicUsize::new(0));
        let store = wasmtime::Store::new(
            &wasmtime::Engine::default(),
            StoreDropProbe(destroyed.clone()),
        );
        let observed = disposal.wait_owned_vm();
        tokio::pin!(observed);
        assert!(futures_util::poll!(&mut observed).is_pending());
        let result = disposal.dispose_inline(store, || {
            assert_eq!(disposal.custodians.load(Ordering::Acquire), 2);
            drop(worker_owner);
            assert_eq!(disposal.custodians.load(Ordering::Acquire), 1);
            assert_eq!(destroyed.load(Ordering::Acquire), 0);
            assert!(!disposal.is_complete());
            let mut context = std::task::Context::from_waker(futures_util::task::noop_waker_ref());
            assert!(std::future::Future::poll(observed.as_mut(), &mut context).is_pending());
            if fail_seal {
                bail!("injected seal failure after inline custody")
            }
            Ok(())
        });
        assert_eq!(result.is_err(), fail_seal);
        assert_eq!(destroyed.load(Ordering::Acquire), 1);
        assert_eq!(disposal.custodians.load(Ordering::Acquire), 0);
        observed.await.unwrap();
    }
}

#[tokio::test]
async fn inline_store_destruction_does_not_release_other_worker_custody() {
    let control = CoreControl::default();
    let disposal = receipt(&control);
    let worker_owner = disposal.claim_owner().unwrap();
    let destroyed = Arc::new(AtomicUsize::new(0));
    let store = wasmtime::Store::new(
        &wasmtime::Engine::default(),
        StoreDropProbe(destroyed.clone()),
    );
    disposal.dispose_inline(store, || Ok(())).unwrap();
    assert_eq!(destroyed.load(Ordering::Acquire), 1);
    assert_eq!(disposal.custodians.load(Ordering::Acquire), 1);
    disposal.wait_owned_vm().await.unwrap();
    drop(worker_owner);
    assert_eq!(disposal.custodians.load(Ordering::Acquire), 0);
    disposal.wait_owned_vm().await.unwrap();
}

struct NoIo;
#[async_trait]
impl tobkiri_gate::TrustedPrompt for NoIo {
    async fn prompt_approval(
        &self,
        _: tobkiri_gate::ApprovalPrompt,
    ) -> tobkiri_gate::ApprovalAnswer {
        panic!("catalog-only disposal test must never request execution authority")
    }
}
#[async_trait]
impl HostIo for NoIo {
    fn display(&self, _: &str, _: &tobkiri_canon::CanonValue) {
        panic!("catalog-only test performed I/O")
    }
    async fn terminal_read_line(&self, _: &str) -> Result<String, tobkiri_gate::HostError> {
        panic!("catalog-only test performed I/O")
    }
    fn terminal_write(&self, _: &str) {
        panic!("catalog-only test performed I/O")
    }
    fn embed_ui(
        &self,
        _: tobkiri_gate::UiEmbed,
    ) -> Result<tobkiri_canon::ids::UiSessionId, tobkiri_gate::HostError> {
        panic!("catalog-only test performed I/O")
    }
    async fn pick_directory(&self, _: &str) -> Option<std::path::PathBuf> {
        panic!("catalog-only test performed I/O")
    }
}

struct CatalogFixture {
    host: PackHost,
    pack: PackId,
    control: CoreControl,
    _directory: tempfile::TempDir,
}
impl CatalogFixture {
    async fn new(max_tasks: u64) -> Self {
        use tobkiri_canon::{Digest, ids::RequestId};
        use tobkiri_gate::{CoreCancellation, GateEngine, MediatorChain, PermissionSet};
        use tobkiri_manifest::{FileEntry, PackManifest, Ports};
        let directory = tempfile::tempdir().unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(directory.path(), std::fs::Permissions::from_mode(0o700))
                .unwrap();
        }
        let root = Arc::new(DataRoot::open(directory.path().canonicalize().unwrap()).unwrap());
        let control = CoreControl::new(CoreControlLimits {
            max_tasks,
            ..Default::default()
        })
        .unwrap();
        let profile = ProfileId::parse("disposal-catalog").unwrap();
        let pack = PackId::parse("local.disposal-catalog").unwrap();
        let limits = ResourceLimits {
            fuel_per_call: 1000,
            memory_mib: 1,
            wall_ms: 30_000,
            table_elements: 16,
            output_bytes: 1024,
        };
        let bytes = wat::parse_str("(component)").unwrap();
        let manifest = PackManifest {
            format: "tobkiri.pack/1".into(),
            id: pack.clone(),
            version: "1.0.0".into(),
            publisher_key: None,
            runtime: RuntimeSpec::Wasm {
                core_modules: Default::default(),
                component: "component.wasm".into(),
                toolchain: "wat-catalog-test/1".into(),
                config: None,
            },
            shapes: Vec::new(),
            ports: Ports::default(),
            inputs: Vec::new(),
            flows: Vec::new(),
            overlays: Vec::new(),
            stores: Vec::new(),
            permissions: PermissionSet {
                wasm: limits.clone(),
                ..Default::default()
            },
            ui: None,
            files: vec![FileEntry {
                path: "component.wasm".into(),
                sha256: Digest::hash(&bytes),
                size: bytes.len() as u64,
            }],
        };
        let pack_directory = directory.path().join("catalog-pack");
        std::fs::create_dir(&pack_directory).unwrap();
        std::fs::write(pack_directory.join("component.wasm"), bytes).unwrap();
        std::fs::write(
            pack_directory.join("pack.json"),
            tobkiri_manifest::canonical_manifest(&manifest).unwrap(),
        )
        .unwrap();
        let verified =
            tobkiri_manifest::verify_directory(&pack_directory, &BTreeMap::new()).unwrap();
        let effective_limits =
            BTreeMap::from([((pack.clone(), verified.hash.clone()), limits.clone())]);
        let io = Arc::new(NoIo);
        let gate = Arc::new(GateEngine::new_with_control(
            Arc::new(tobkiri_store::Approvals::open(&root, &profile).unwrap()),
            Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap()),
            io.clone(),
            MediatorChain::default(),
            control.clone(),
        ));
        let cx = CallCx::try_root_with_control(
            profile,
            Principal::User,
            RequestId::parse("disposal-catalog").unwrap(),
            CoreCancellation::new_with_control(&control).unwrap(),
            std::time::Instant::now() + std::time::Duration::from_secs(30),
            limits,
            control.clone(),
        )
        .unwrap();
        let host = PackHost::new(
            gate,
            CapacityLedger::new(),
            io,
            root,
            BTreeMap::new(),
            BTreeMap::new(),
            vec![verified],
            effective_limits,
            &cx,
        )
        .await
        .unwrap();
        Self {
            host,
            pack,
            control,
            _directory: directory,
        }
    }
}

#[tokio::test]
async fn actual_host_task_rejection_leaves_generation_and_token_untouched() {
    // Only the real engine's epoch driver fits. This host has loaded and
    // validated component bytes, but no guest entry/Store/source authority.
    let fixture = CatalogFixture::new(1).await;
    let slot = fixture.host.instance(&fixture.pack).unwrap();
    let initial_generation = slot.generation.load(Ordering::Acquire);
    let baseline = fixture.control.usage();
    let error = fixture
        .host
        .discard_generation_mode(&fixture.pack, Some(initial_generation), false, None)
        .await
        .err()
        .unwrap();
    assert_eq!(
        error.downcast_ref::<tobkiri_gate::CoreControlError>(),
        Some(&tobkiri_gate::CoreControlError::Limit)
    );
    assert!(error.to_string().contains("before retirement"));
    let lifetime = slot.retirement.lock().unwrap();
    assert_eq!(slot.generation.load(Ordering::Acquire), initial_generation);
    assert!(!lifetime.retiring);
    assert!(!lifetime.token.is_cancelled());
    assert_eq!(lifetime.disposal.custodians.load(Ordering::Acquire), 0);
    assert!(!lifetime.disposal.is_complete());
    assert!(lifetime.disposal.retirement_cause.get().is_none());
    assert_eq!(fixture.control.usage().bytes, baseline.bytes);
}

#[tokio::test]
async fn failed_inline_generation_seal_freezes_general_provenance_without_completion() {
    let fixture = CatalogFixture::new(1).await;
    let slot = fixture.host.instance(&fixture.pack).unwrap();
    let instance = slot.instance.lock().await;
    // This catalog has no initialized Store/census. The production inline
    // seal must still fix its first retirement cause before that failure;
    // neither the stamp nor the retiring flag proves physical completion.
    assert!(instance.seal_native_generation().is_err());
    let disposal = instance.store_disposal.clone();
    let cause = disposal.retirement_cause.get().unwrap();
    assert_eq!(cause.generation, instance.current_generation);
    assert!(cause.shutdown_scope.is_none());
    assert!(slot.retirement.lock().unwrap().retiring);
    assert!(!disposal.is_complete());
    assert!(
        fixture
            .host
            .retire_generation(
                &slot,
                Some(instance.current_generation),
                Some(Digest::hash(b"later-shutdown")),
            )
            .is_err()
    );
    assert!(
        disposal
            .retirement_cause
            .get()
            .unwrap()
            .shutdown_scope
            .is_none()
    );
}

#[tokio::test]
async fn actual_retirement_owner_is_exclusive_and_loss_is_not_completion() {
    let fixture = CatalogFixture::new(2).await;
    let slot = fixture.host.instance(&fixture.pack).unwrap();
    let generation = slot.generation.load(Ordering::Acquire);
    let retirement = fixture
        .host
        .retire_generation(
            &slot,
            Some(generation),
            Some(Digest::hash(b"original-shutdown")),
        )
        .unwrap();
    let disposal = retirement.disposal.clone();
    assert_eq!(slot.generation.load(Ordering::Acquire), generation + 1);
    assert!(slot.retirement.lock().unwrap().retiring);
    assert_eq!(disposal.custodians.load(Ordering::Acquire), 1);
    assert!(!disposal.is_complete());
    let cause = disposal.retirement_cause.get().unwrap();
    assert_eq!(cause.generation, generation);
    assert_eq!(
        cause.shutdown_scope,
        Some(Digest::hash(b"original-shutdown"))
    );
    // The actual inline seal overlaps an already-admitted retirement. Its
    // later conservative None must preserve the original exact cause.
    assert!(slot.instance.lock().await.seal_native_generation().is_err());
    assert!(
        disposal.shutdown_retirement_matches(
            Some(generation),
            Some(&Digest::hash(b"original-shutdown")),
        )
    );
    let baseline = fixture.control.usage();
    assert!(
        fixture
            .host
            .discard_generation_mode(&fixture.pack, Some(generation), false, None)
            .await
            .is_err()
    );
    assert_eq!(fixture.control.usage().bytes, baseline.bytes);
    let observed = disposal.wait_owned_vm();
    tokio::pin!(observed);
    assert!(futures_util::poll!(&mut observed).is_pending());
    drop(retirement);
    assert!(matches!(
        futures_util::poll!(&mut observed),
        std::task::Poll::Ready(Err(_))
    ));
    assert!(!disposal.is_complete());
}

#[tokio::test]
async fn panicking_seal_keeps_custody_through_store_unwind_without_completion() {
    struct ObserveDrop {
        disposal: Arc<StoreDisposal>,
        owners_at_drop: Arc<AtomicUsize>,
        done_at_drop: Arc<AtomicBool>,
    }
    impl Drop for ObserveDrop {
        fn drop(&mut self) {
            // Record rather than assert inside unwinding: a regression must
            // report normally, not cause a second panic and process abort.
            self.owners_at_drop.store(
                self.disposal.custodians.load(Ordering::Acquire),
                Ordering::Release,
            );
            self.done_at_drop
                .store(self.disposal.is_complete(), Ordering::Release);
        }
    }
    let control = CoreControl::default();
    let disposal = receipt(&control);
    let owners_at_drop = Arc::new(AtomicUsize::new(0));
    let done_at_drop = Arc::new(AtomicBool::new(true));
    let store = wasmtime::Store::new(
        &wasmtime::Engine::default(),
        ObserveDrop {
            disposal: disposal.clone(),
            owners_at_drop: owners_at_drop.clone(),
            done_at_drop: done_at_drop.clone(),
        },
    );
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _ = disposal.dispose_inline(store, || panic!("injected sealing panic"));
    }));
    assert!(result.is_err());
    assert_eq!(owners_at_drop.load(Ordering::Acquire), 1);
    assert!(!done_at_drop.load(Ordering::Acquire));
    assert_eq!(disposal.custodians.load(Ordering::Acquire), 0);
    assert!(!disposal.is_complete());
    let observed = disposal.wait_owned_vm();
    tokio::pin!(observed);
    assert!(matches!(
        futures_util::poll!(&mut observed),
        std::task::Poll::Ready(Err(_))
    ));
}
