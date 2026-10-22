use super::{SecretRef, Vault};
use crate::{DataRoot, StoreError};
use std::{
    sync::{Arc, Mutex},
    time::Instant,
};
use tobkiri_canon::ids::ProfileId;
use tobkiri_gate::{CallCx, DispatchPermit, EffectRequest, GateError, GrantId};
use zeroize::Zeroizing;

/// Linear consumed-Secret work: construction only moves already-prepared
/// trusted inputs; every permit/context check precedes key or vault IO.
pub struct SecretWork {
    root: DataRoot,
    cx: CallCx,
    permit: DispatchPermit,
    /// Test-only per-call arrival signal; compiled out of production.
    #[cfg(test)]
    pre_lock: Option<Box<dyn Fn() + Send + Sync>>,
}
impl SecretWork {
    /// Move-only preparation; the original permit stays in custody. No IO.
    pub fn new(root: DataRoot, cx: CallCx, permit: DispatchPermit) -> Self {
        Self {
            root,
            cx,
            permit,
            #[cfg(test)]
            pre_lock: None,
        }
    }
    /// Test-only signal before the cache lock, after validation succeeds.
    #[cfg(test)]
    fn observe_pre_lock(mut self, signal: impl Fn() + Send + Sync + 'static) -> Self {
        self.pre_lock = Some(Box::new(signal));
        self
    }
    pub fn id(&self) -> &GrantId {
        self.permit.id()
    }
    pub fn permit(&self) -> &DispatchPermit {
        &self.permit
    }
    pub fn context(&self) -> &CallCx {
        &self.cx
    }
    /// Synchronous body of the downstream managed blocking job. `cache` is
    /// that job's coherent (root, Profile) slot; foreign identity is refused.
    pub fn execute(self, cache: &Mutex<Option<Arc<Vault>>>) -> SecretCompletion {
        self.execute_with(cache, Vault::open_default)
    }
    fn execute_with(
        self,
        cache: &Mutex<Option<Arc<Vault>>>,
        open: impl FnOnce(&DataRoot, &ProfileId) -> Result<Vault, StoreError>,
    ) -> SecretCompletion {
        let result = self.run(cache, open);
        SecretCompletion {
            result: Some(result),
            work: self,
        }
    }
    fn check_live(&self) -> Result<(), StoreError> {
        self.cx.check_active()?;
        if Instant::now() >= self.permit.deadline() {
            return Err(GateError::Deadline.into());
        }
        Ok(())
    }
    /// Pure validation: name/mode derive from the permit's bound effect/args.
    fn validated(&self) -> Result<(&str, bool), StoreError> {
        if !self.permit.matches_context(&self.cx) {
            return Err(StoreError::Denied("secret permit context mismatch".into()));
        }
        self.check_live()?;
        match self.permit.effect() {
            EffectRequest::Secret { name, reveal } => {
                crate::secure::name(name)?;
                if self.permit.args_digest() != &Vault::secret_digest(name)? {
                    return Err(StoreError::Denied("secret args digest mismatch".into()));
                }
                Ok((name.as_str(), *reveal))
            }
            _ => Err(StoreError::Denied("permit is not a secret effect".into())),
        }
    }
    fn run(
        &self,
        cache: &Mutex<Option<Arc<Vault>>>,
        open: impl FnOnce(&DataRoot, &ProfileId) -> Result<Vault, StoreError>,
    ) -> Result<SecretWorkResult, StoreError> {
        let (name, reveal) = self.validated()?;
        let profile = self.cx.profile().clone();
        // Per-call test signal after successful validation, immediately
        // before the real cache lock attempt; it only reports arrival.
        #[cfg(test)]
        if let Some(signal) = &self.pre_lock {
            signal();
        }
        let vault = {
            let mut slot = cache
                .lock()
                .map_err(|_| StoreError::Unavailable("vault cache lock poisoned".into()))?;
            // A lock wait may span the original cancel/deadline; re-check the
            // same liveness under the guard, before cold key/vault IO.
            self.check_live()?;
            if let Some(vault) = slot.as_ref() {
                if vault.profile != profile || vault.root.path() != self.root.path() {
                    return Err(StoreError::Denied("vault cache identity mismatch".into()));
                }
            } else {
                *slot = Some(Arc::new(open(&self.root, &profile)?));
            }
            slot.as_ref().expect("vault cache slot populated").clone()
        };
        // A cold opener (or the wait) may consume the original deadline or
        // observe cancellation; re-check before vault IO on hit and cold
        // paths alike. Entered synchronous IO is not interruptible by it.
        self.check_live()?;
        let mut values = vault.load()?;
        // A late cancel/deadline suppresses delivery after the read as well.
        self.check_live()?;
        let mut secret = values.remove(name).ok_or(StoreError::NotFound)?;
        Ok(if reveal {
            SecretWorkResult::Revealed(RevealedSecret(Zeroizing::new(std::mem::take(
                &mut secret.0,
            ))))
        } else {
            SecretWorkResult::Opaque(SecretRef {
                profile,
                principal: self.cx.principal().clone(),
                name: name.into(),
                bytes: secret,
            })
        })
    }
}
/// Revealed secret bytes; zeroizing ownership and no content Debug/Display.
pub struct RevealedSecret(Zeroizing<Vec<u8>>);
impl RevealedSecret {
    /// Borrowed bytes for the unchanged trusted host conversion only.
    pub fn bytes(&self) -> &[u8] {
        &self.0
    }
}
impl std::fmt::Debug for RevealedSecret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RevealedSecret").finish_non_exhaustive()
    }
}
/// The single outcome of an executed `SecretWork`.
pub enum SecretWorkResult {
    Opaque(SecretRef),
    Revealed(RevealedSecret),
}
impl std::fmt::Debug for SecretWorkResult {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Opaque(_) => "SecretWorkResult::Opaque(..)",
            Self::Revealed(_) => "SecretWorkResult::Revealed(..)",
        })
    }
}
/// Retained completion: keeps the identical original work, permit and context
/// for Stage B same-ID terminal handling; never settles or records outcomes.
/// Declaration order drops an untaken sensitive result before the original
/// work, permit, context and captures are released.
pub struct SecretCompletion {
    result: Option<Result<SecretWorkResult, StoreError>>,
    work: SecretWork,
}
impl SecretCompletion {
    pub fn id(&self) -> &GrantId {
        self.work.permit.id()
    }
    pub fn work(&self) -> &SecretWork {
        &self.work
    }
    pub fn permit(&self) -> &DispatchPermit {
        &self.work.permit
    }
    pub fn context(&self) -> &CallCx {
        &self.work.cx
    }
    /// Take the outcome exactly once; custody is never released by taking.
    pub fn take(&mut self) -> Result<SecretWorkResult, StoreError> {
        self.result
            .take()
            .unwrap_or_else(|| Err(StoreError::Denied("secret result already taken".into())))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Approvals, AuditLog, AuditQuota, vault::VaultKeyProvider};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;
    use tobkiri_canon::{
        AllocationBudget, AllocationError, AllocationRequest, Digest,
        ids::{PackHash, PackId, RequestId},
    };
    use tobkiri_gate::{
        ApprovalAnswer, ApprovalPrompt, CancellationToken, ConsumptionAttempt, Gate, GateEngine,
        LoopLimits, MediatorChain, Outcome, PackPolicy, PermissionSet, Principal, ResourceLimits,
        SecretPermission, StoragePermissions, TrustedPrompt,
    };
    struct TestKeys;
    struct HeaderBudget {
        used: AtomicUsize,
        calls: AtomicUsize,
        limit: usize,
    }
    impl HeaderBudget {
        fn new(limit: usize) -> Self {
            Self {
                used: AtomicUsize::new(0),
                calls: AtomicUsize::new(0),
                limit,
            }
        }
    }
    impl AllocationBudget for HeaderBudget {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let bytes = usize::try_from(request.bytes).map_err(|_| AllocationError::Overflow)?;
            self.used
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |used| {
                    used.checked_add(bytes).filter(|used| *used <= self.limit)
                })
                .map_err(|_| AllocationError::Limit)?;
            Ok(())
        }
        fn release(&self, bytes: u64) {
            self.used.fetch_sub(bytes as usize, Ordering::SeqCst);
        }
    }
    impl VaultKeyProvider for TestKeys {
        fn key(&self, _: &ProfileId) -> Result<[u8; 32], StoreError> {
            Ok([7; 32])
        }
    }
    struct Prompt;
    #[async_trait::async_trait]
    impl TrustedPrompt for Prompt {
        async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
            ApprovalAnswer::Approve
        }
    }
    struct Fixture {
        _temp: tempfile::TempDir,
        root: DataRoot,
        profile: ProfileId,
        gate: Arc<GateEngine>,
    }
    fn live() -> Instant {
        Instant::now() + Duration::from_secs(30)
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
    fn permissions() -> PermissionSet {
        let mut permissions = PermissionSet {
            storage: StoragePermissions {
                private: true,
                public_read: true,
                public_write: true,
            },
            audit_append: true,
            audit_read: true,
            wasm: limits(),
            loops: LoopLimits {
                max_iterations: 16,
                max_wall_ms: 30_000,
                max_cost_units: 1000,
            },
            ..Default::default()
        };
        permissions.secrets.push(SecretPermission {
            name: "api-key".into(),
            reveal: true,
            optional: false,
        });
        permissions
            .network
            .origins
            .insert("https://api.example".into());
        permissions
    }
    impl Fixture {
        fn new() -> Self {
            let temp = tempfile::tempdir().unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(temp.path(), std::fs::Permissions::from_mode(0o700))
                    .unwrap();
            }
            let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
            let profile = ProfileId::parse("prof_test").unwrap();
            let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
            let audit = Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap());
            let gate = Arc::new(GateEngine::new(
                approvals,
                audit,
                Arc::new(Prompt),
                MediatorChain::default(),
            ));
            Self {
                _temp: temp,
                root,
                profile,
                gate,
            }
        }
        fn cx(&self, principal: Principal) -> CallCx {
            let control = self.gate.control();
            CallCx::try_root_with_control(
                self.profile.clone(),
                principal,
                RequestId::parse("req_test").unwrap(),
                CancellationToken::new_with_control(control).unwrap(),
                live(),
                limits(),
                control.clone(),
            )
            .unwrap()
            .with_security_epoch(self.gate.epoch(&self.profile).unwrap())
        }
        async fn pack(&self, id: &str) -> CallCx {
            let pack = PackId::parse(id).unwrap();
            let hash = PackHash::new(Digest::hash(id.as_bytes()));
            let permissions = permissions();
            self.gate
                .register_pack(
                    &self.profile,
                    &pack,
                    &hash,
                    PackPolicy {
                        declaration: permissions.clone(),
                        ceiling: permissions.clone(),
                        pack_limits: permissions,
                    },
                )
                .unwrap();
            self.gate
                .approve_pack_permissions(&self.cx(Principal::User), &pack, None, &hash)
                .await
                .unwrap();
            self.cx(Principal::Pack { pack, hash })
        }
        async fn permit(
            &self,
            cx: &CallCx,
            request: EffectRequest,
            args: Digest,
        ) -> (GrantId, DispatchPermit) {
            let grant = self
                .gate
                .authorize(cx, request.clone(), args.clone())
                .await
                .unwrap();
            let id = grant.id().clone();
            match grant.consume_owned(cx, &request, &args) {
                ConsumptionAttempt::Consumed(consumed) => (id, consumed.into_dispatch().unwrap()),
                _ => panic!("grant consumption was not executable"),
            }
        }
    }
    fn secret_request(reveal: bool) -> (EffectRequest, Digest) {
        (
            EffectRequest::Secret {
                name: "api-key".into(),
                reveal,
            },
            Vault::secret_digest("api-key").unwrap(),
        )
    }
    async fn network_reference(f: &Fixture, cx: &CallCx, bytes: &[u8]) -> SecretRef {
        Vault::open(&f.root, &f.profile, Arc::new(TestKeys))
            .unwrap()
            .set("api-key", bytes)
            .unwrap();
        let (request, args) = secret_request(false);
        let (_, permit) = f.permit(cx, request, args).await;
        let mut completion = SecretWork::new(f.root.clone(), cx.clone(), permit)
            .execute_with(&Mutex::new(None), |root, profile| {
                Vault::open(root, profile, Arc::new(TestKeys))
            });
        match completion.take().unwrap() {
            SecretWorkResult::Opaque(value) => value,
            _ => panic!("opaque secret required"),
        }
    }
    fn network_request() -> EffectRequest {
        EffectRequest::Network {
            origin: "https://api.example".into(),
            method: "POST".into(),
            url: "https://api.example/v1".into(),
        }
    }
    #[tokio::test]
    async fn network_secret_scratch_is_precharged_retained_and_refunded_on_success_or_unwind() {
        let f = Fixture::new();
        let cx = f.pack("local.owner").await;
        let secret = network_reference(&f, &cx, b"token").await;
        let (_, permit) = f
            .permit(&cx, network_request(), Digest::hash(b"request"))
            .await;
        let accounting = Arc::new(HeaderBudget::new(12));
        let budget: Arc<dyn AllocationBudget> = accounting.clone();
        let length = secret
            .with_network_value(&cx, &permit, "Bearer ", &budget, |value| {
                assert_eq!(accounting.used.load(Ordering::SeqCst), 12);
                assert_eq!(value, b"Bearer token");
                value.len()
            })
            .unwrap();
        assert_eq!(length, 12);
        assert_eq!(accounting.used.load(Ordering::SeqCst), 0);
        let unwind = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = secret.with_network_value(&cx, &permit, "Bearer ", &budget, |_| {
                assert_eq!(accounting.used.load(Ordering::SeqCst), 12);
                panic!("injected conversion unwind");
            });
        }));
        assert!(unwind.is_err());
        assert_eq!(accounting.used.load(Ordering::SeqCst), 0);
        assert_eq!(accounting.calls.load(Ordering::SeqCst), 2);
    }
    #[tokio::test]
    async fn network_secret_header_limits_and_owner_denial_precede_material_conversion() {
        let f = Fixture::new();
        let cx = f.pack("local.owner").await;
        let secret = network_reference(&f, &cx, b"x").await;
        let (_, permit) = f
            .permit(&cx, network_request(), Digest::hash(b"request"))
            .await;
        let accounting = Arc::new(HeaderBudget::new(SecretRef::MAX_NETWORK_HEADER_BYTES));
        let budget: Arc<dyn AllocationBudget> = accounting.clone();
        let prefix = "p".repeat(SecretRef::MAX_NETWORK_HEADER_BYTES - 1);
        secret
            .with_network_value(&cx, &permit, &prefix, &budget, |value| {
                assert_eq!(value.len(), SecretRef::MAX_NETWORK_HEADER_BYTES);
                assert_eq!(value.last(), Some(&b'x'));
            })
            .unwrap();
        let calls = accounting.calls.load(Ordering::SeqCst);
        assert!(matches!(
            secret.with_network_value(&cx, &permit, &(prefix + "p"), &budget, |_| panic!(
                "oversized header copied"
            )),
            Err(StoreError::Quota)
        ));
        assert_eq!(accounting.calls.load(Ordering::SeqCst), calls);
        let denied = Arc::new(HeaderBudget::new(0));
        let denied_budget: Arc<dyn AllocationBudget> = denied.clone();
        assert!(matches!(
            secret.with_network_value(&cx, &permit, "", &denied_budget, |_| panic!(
                "denied owner copied secret"
            )),
            Err(StoreError::Quota)
        ));
        assert_eq!(denied.used.load(Ordering::SeqCst), 0);
        assert_eq!(accounting.used.load(Ordering::SeqCst), 0);
    }
    #[tokio::test]
    async fn network_secret_rejects_foreign_cancelled_or_expired_context_before_funding() {
        let f = Fixture::new();
        let cx = f.pack("local.owner").await;
        let secret = network_reference(&f, &cx, b"token").await;
        let (_, permit) = f
            .permit(&cx, network_request(), Digest::hash(b"request"))
            .await;
        let accounting = Arc::new(HeaderBudget::new(16384));
        let budget: Arc<dyn AllocationBudget> = accounting.clone();
        assert!(matches!(
            secret.with_network_value(&f.cx(Principal::User), &permit, "", &budget, |_| panic!(
                "foreign conversion"
            )),
            Err(StoreError::Denied(_))
        ));
        let expired = cx
            .clone()
            .cap_deadline(Instant::now() - Duration::from_millis(1));
        assert!(matches!(
            secret.with_network_value(&expired, &permit, "", &budget, |_| panic!(
                "expired conversion"
            )),
            Err(StoreError::Gate(GateError::Deadline))
        ));
        cx.cancel().cancel();
        assert!(matches!(
            secret.with_network_value(&cx, &permit, "", &budget, |_| panic!(
                "cancelled conversion"
            )),
            Err(StoreError::Gate(GateError::Cancelled))
        ));
        assert_eq!(accounting.calls.load(Ordering::SeqCst), 0);
    }
    #[tokio::test]
    async fn network_secret_honors_the_consumed_permits_earlier_deadline() {
        let f = Fixture::new();
        let cx = f.pack("local.owner").await;
        let secret = network_reference(&f, &cx, b"token").await;
        let narrowed = cx
            .clone()
            .cap_deadline(Instant::now() + Duration::from_secs(1));
        let (_, permit) = f
            .permit(&narrowed, network_request(), Digest::hash(b"request"))
            .await;
        tokio::time::sleep_until((permit.deadline() + Duration::from_millis(1)).into()).await;
        cx.check_active().unwrap();
        let accounting = Arc::new(HeaderBudget::new(16384));
        let budget: Arc<dyn AllocationBudget> = accounting.clone();
        assert!(matches!(
            secret.with_network_value(&cx, &permit, "", &budget, |_| panic!(
                "expired permit conversion"
            )),
            Err(StoreError::Gate(GateError::Deadline))
        ));
        assert_eq!(accounting.calls.load(Ordering::SeqCst), 0);
    }
    #[tokio::test]
    async fn rejected_permits_return_retained_failures_without_key_io() {
        let f = Fixture::new();
        let cx = f.pack("local.owner").await;
        let calls = AtomicUsize::new(0);
        let open = |root: &DataRoot, profile: &ProfileId| {
            calls.fetch_add(1, Ordering::SeqCst);
            Vault::open(root, profile, Arc::new(TestKeys))
        };
        let cache = Mutex::new(None);
        let request = EffectRequest::Network {
            origin: "https://api.example".into(),
            method: "POST".into(),
            url: "https://api.example/v1".into(),
        };
        let (id, permit) = f
            .permit(&cx, request, Digest::hash(b"network request"))
            .await;
        let mut completion =
            SecretWork::new(f.root.clone(), cx.clone(), permit).execute_with(&cache, open);
        assert!(completion.take().is_err());
        assert!(completion.permit().id() == &id);
        let (request, args) = secret_request(false);
        let (_, permit) = f.permit(&cx, request, args).await;
        let other_cx = f.cx(Principal::User);
        let mut completion =
            SecretWork::new(f.root.clone(), other_cx, permit).execute_with(&cache, open);
        assert!(completion.take().is_err());
        let (request, args) = secret_request(false);
        let (_, permit) = f.permit(&cx, request, args).await;
        let expired = cx.clone().cap_deadline(Instant::now());
        let mut completion =
            SecretWork::new(f.root.clone(), expired, permit).execute_with(&cache, open);
        assert!(completion.take().is_err());
        let other = ProfileId::parse("prof_other").unwrap();
        let foreign = Vault::open(&f.root, &other, Arc::new(TestKeys)).unwrap();
        *cache.lock().unwrap() = Some(Arc::new(foreign));
        let (request, args) = secret_request(false);
        let (_, permit) = f.permit(&cx, request, args).await;
        let mut completion =
            SecretWork::new(f.root.clone(), cx.clone(), permit).execute_with(&cache, open);
        assert!(completion.take().is_err());
        assert_eq!(calls.load(Ordering::SeqCst), 0);
    }
    #[tokio::test]
    async fn get_and_reveal_execute_on_one_cold_vault_and_stay_opaque() {
        let f = Fixture::new();
        let cx = f.pack("local.owner").await;
        let secret = b"highly-sensitive-token-only-in-vault";
        Vault::open(&f.root, &f.profile, Arc::new(TestKeys))
            .unwrap()
            .set("api-key", secret)
            .unwrap();
        let calls = AtomicUsize::new(0);
        let open = |root: &DataRoot, profile: &ProfileId| {
            calls.fetch_add(1, Ordering::SeqCst);
            Vault::open(root, profile, Arc::new(TestKeys))
        };
        let cache = Mutex::new(None);
        let (request, args) = secret_request(false);
        let (_, permit) = f.permit(&cx, request, args).await;
        let mut completion =
            SecretWork::new(f.root.clone(), cx.clone(), permit).execute_with(&cache, open);
        let opaque = match completion.take().unwrap() {
            SecretWorkResult::Opaque(opaque) => opaque,
            _ => panic!("secret get did not produce an opaque reference"),
        };
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        let text = std::str::from_utf8(secret).unwrap();
        assert!(!format!("{opaque:?}").contains(text));
        let request = EffectRequest::Network {
            origin: "https://api.example".into(),
            method: "POST".into(),
            url: "https://api.example/v1".into(),
        };
        let (_, permit) = f
            .permit(&cx, request, Digest::hash(b"opaque ref request"))
            .await;
        let budget: Arc<dyn AllocationBudget> = Arc::new(HeaderBudget::new(16384));
        assert_eq!(
            opaque
                .with_network_value(&cx, &permit, "Bearer ", &budget, |v| v.to_vec())
                .unwrap(),
            [b"Bearer ".as_slice(), secret].concat()
        );
        let (request, args) = secret_request(true);
        let (_, permit) = f.permit(&cx, request, args).await;
        let mut completion =
            SecretWork::new(f.root.clone(), cx.clone(), permit).execute_with(&cache, open);
        let revealed = match completion.take().unwrap() {
            SecretWorkResult::Revealed(revealed) => revealed,
            _ => panic!("secret reveal did not produce bytes"),
        };
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert_eq!(revealed.bytes(), secret.as_slice());
        assert!(!format!("{revealed:?}").contains(text));
    }
    #[tokio::test]
    async fn result_takes_once_while_completion_retains_terminal_custody() {
        let f = Fixture::new();
        let cx = f.pack("local.owner").await;
        Vault::open(&f.root, &f.profile, Arc::new(TestKeys))
            .unwrap()
            .set("api-key", b"token")
            .unwrap();
        let (request, args) = secret_request(false);
        let (id, permit) = f.permit(&cx, request, args).await;
        let cache = Mutex::new(None);
        let mut completion = SecretWork::new(f.root.clone(), cx.clone(), permit)
            .execute_with(&cache, |r, p| Vault::open(r, p, Arc::new(TestKeys)));
        assert!(completion.take().is_ok());
        assert!(completion.take().is_err());
        assert!(completion.id() == &id);
        assert!(completion.permit().id() == &id);
        assert!(completion.context().request() == cx.request());
        Gate::record_outcome(
            f.gate.as_ref(),
            id.clone(),
            Outcome::Failed {
                code: "test-terminal".into(),
            },
        )
        .await
        .unwrap();
    }
    #[tokio::test]
    async fn post_validation_cache_wait_still_fails_closed_on_cancel() {
        let f = Fixture::new();
        let cx = f.pack("local.owner").await;
        let calls = AtomicUsize::new(0);
        let cache: Mutex<Option<Arc<Vault>>> = Mutex::new(None);
        let (request, args) = secret_request(false);
        let (id, permit) = f.permit(&cx, request, args).await;
        let deadline = permit.deadline();
        let work_cx = cx.clone();
        let root = f.root.clone();
        let cache_ref = &cache;
        let calls_ref = &calls;
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        let guard = cache.lock().unwrap();
        let mut completion = std::thread::scope(|scope| {
            let worker = scope.spawn(move || {
                SecretWork::new(root, work_cx, permit)
                    .observe_pre_lock(move || {
                        let _ = tx.send(());
                    })
                    .execute_with(cache_ref, move |r, p| {
                        calls_ref.fetch_add(1, Ordering::SeqCst);
                        Vault::open(r, p, Arc::new(TestKeys))
                    })
            });
            // Arrival is observed while the cache stays locked; cancel the
            // same original token first, then release the lock and join.
            let arrived = rx.recv_timeout(Duration::from_secs(30));
            cx.cancel().cancel();
            drop(guard);
            let completion = worker.join().unwrap();
            arrived.expect("worker never reached the pre-lock signal");
            completion
        });
        assert!(cache.lock().unwrap().is_none());
        assert_eq!(calls.load(Ordering::SeqCst), 0);
        match completion.take() {
            Err(StoreError::Gate(GateError::Cancelled)) => {}
            other => panic!("expected Gate(Cancelled); got {other:?}"),
        }
        assert!(completion.take().is_err());
        assert_eq!(completion.id(), &id);
        assert_eq!(completion.permit().id(), &id);
        assert_eq!(completion.permit().deadline(), deadline);
        assert!(completion.context().request() == cx.request());
        assert!(completion.permit().matches_context(&cx));
    }
}
