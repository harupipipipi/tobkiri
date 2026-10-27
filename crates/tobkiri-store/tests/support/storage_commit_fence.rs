//! Actual Gate and separate durable authority connection at physical writes.
use super::*;
use std::sync::atomic::AtomicBool;

#[derive(Clone, Copy, PartialEq, Eq)]
enum RevocationPoint {
    AfterLeaseWatch,
    BeforeOutcome,
}

/// Every grant/consume uses the real Gate. The observer changes only the
/// position of a revocation through another actual Gate/authority connection.
struct RevokeAtBoundary {
    original: Arc<GateEngine>,
    revoker: GateEngine,
    profile: ProfileId,
    point: RevocationPoint,
    revoked: AtomicBool,
}
impl RevokeAtBoundary {
    fn revoke(&self, profile: &ProfileId) -> Result<(), GateError> {
        assert!(!self.revoked.swap(true, Ordering::SeqCst));
        self.revoker.revoke_epoch(profile)?;
        Ok(())
    }
}
#[async_trait]
impl Gate for RevokeAtBoundary {
    fn check_context(&self, cx: &CallCx) -> Result<(), GateError> {
        self.original.check_context(cx)
    }
    async fn authorize(
        &self,
        cx: &CallCx,
        request: EffectRequest,
        args: Digest,
    ) -> Result<Grant, GateError> {
        self.original.authorize(cx, request, args).await
    }
    fn watch_lease(&self, cx: &CallCx, permit: &DispatchPermit) -> Result<(), GateError> {
        self.original.watch_lease(cx, permit)?;
        if self.point == RevocationPoint::AfterLeaseWatch {
            self.revoke(cx.profile())?;
        }
        Ok(())
    }
    async fn record_outcome(&self, grant: GrantId, outcome: Outcome) -> Result<(), GateError> {
        // Native DATA and authority guards are already gone at this boundary.
        if self.point == RevocationPoint::BeforeOutcome {
            self.revoke(&self.profile)?;
        }
        self.original.record_outcome(grant, outcome).await
    }
    async fn record_denial(
        &self,
        cx: &CallCx,
        kind: &'static str,
        code: &'static str,
    ) -> Result<(), GateError> {
        self.original.record_denial(cx, kind, code).await
    }
}

fn observer(f: &Fixture, point: RevocationPoint) -> Arc<RevokeAtBoundary> {
    Arc::new(RevokeAtBoundary {
        original: f.gate.clone(),
        revoker: GateEngine::new_with_control(
            Arc::new(Approvals::open(&f.root, &f.profile).unwrap()),
            Arc::new(AuditLog::open(&f.root, &f.profile, AuditQuota::default()).unwrap()),
            f.prompt.clone(),
            MediatorChain::default(),
            f.gate.control().clone(),
        ),
        profile: f.profile.clone(),
        point,
        revoked: AtomicBool::new(false),
    })
}

async fn prepared() -> (Fixture, CallCx, StoreHandle) {
    let f = Fixture::new();
    let cx = f.pack("local.activation-fence-test", permissions()).await;
    let stores = TieredStore::open(&f.root, &f.profile).unwrap();
    stores
        .register(
            owner(&cx),
            "activation",
            StoreTier::Private,
            "local.activation-fence-test.data@1.0",
        )
        .unwrap();
    let handle = open(&f, &stores, &cx, owner(&cx), "activation").await;
    native_sql(
        &f,
        &handle,
        &cx,
        "CREATE TABLE activation(value INTEGER NOT NULL)",
        &[],
        false,
    )
    .await
    .unwrap();
    (f, cx, handle)
}

fn insert(budget: &Arc<dyn AllocationBudget>) -> OwnedSqlStatement {
    OwnedSqlStatement::from_value(
        CoreValue::try_from_borrowed(
            &CanonValue::Obj(std::collections::BTreeMap::from([
                (
                    "statement".into(),
                    CanonValue::Str("INSERT INTO activation(value) VALUES(1)".into()),
                ),
                ("params".into(), CanonValue::Arr(vec![])),
                ("max-rows".into(), CanonValue::Int(10)),
            ])),
            budget,
        )
        .unwrap(),
    )
    .unwrap()
}

async fn physical_count(f: &Fixture, handle: &StoreHandle, cx: &CallCx) -> String {
    // Observe actual DATA through a fresh real context after revocation.
    let fresh = f.cx(cx.principal().clone());
    let rows = native_sql(
        f,
        handle,
        &fresh,
        "SELECT count(*) FROM activation",
        &[],
        true,
    )
    .await
    .unwrap();
    rows.value()
        .get_key("rows")
        .unwrap()
        .get_index(0)
        .unwrap()
        .get_index(0)
        .unwrap()
        .get_key("value")
        .unwrap()
        .as_str()
        .unwrap()
        .to_owned()
}

#[tokio::test]
async fn sql_write_rejects_epoch_revoked_after_consumption() {
    let (f, cx, handle) = prepared().await;
    let gate = observer(&f, RevocationPoint::AfterLeaseWatch);
    let budget: Arc<dyn AllocationBudget> = Arc::new(SqlTestValues(AtomicUsize::new(0)));
    let result = handle
        .sql_exec_typed(
            &cx,
            insert(&budget),
            SqlResultPolicy::ForbidRows,
            gate.clone(),
            budget,
        )
        .await;
    assert!(gate.revoked.load(Ordering::SeqCst));
    assert_ne!(f.gate.epoch(&f.profile).unwrap(), cx.security_epoch());
    assert_eq!(
        physical_count(&f, &handle, &cx).await,
        "0",
        "revoked write physically committed activation"
    );
    assert!(matches!(
        result,
        Err(StoreError::Gate(GateError::StaleEpoch))
    ));
}

#[tokio::test]
async fn explicit_transaction_rejects_revocation_at_final_commit_and_rolls_back() {
    let (f, cx, handle) = prepared().await;
    let budget: Arc<dyn AllocationBudget> = Arc::new(SqlTestValues(AtomicUsize::new(0)));
    let transaction = handle
        .sql_begin(&cx, f.gate.clone(), budget.clone())
        .await
        .unwrap();
    transaction
        .exec_typed(
            &cx,
            insert(&budget),
            SqlResultPolicy::ForbidRows,
            f.gate.clone(),
            budget,
        )
        .await
        .unwrap();
    let gate = observer(&f, RevocationPoint::AfterLeaseWatch);
    let result = transaction.commit(&cx, gate.clone()).await;
    assert!(gate.revoked.load(Ordering::SeqCst));
    assert!(matches!(
        result,
        Err(StoreError::Gate(GateError::StaleEpoch))
    ));
    assert_eq!(transaction.state(), SqlTransactionState::RolledBack);
    assert_eq!(
        physical_count(&f, &handle, &cx).await,
        "0",
        "revoked final COMMIT retained the earlier provisional INSERT"
    );
}

#[tokio::test]
async fn physical_commit_before_revocation_remains_one_confirmed_effect() {
    let (f, cx, handle) = prepared().await;
    let gate = observer(&f, RevocationPoint::BeforeOutcome);
    let budget: Arc<dyn AllocationBudget> = Arc::new(SqlTestValues(AtomicUsize::new(0)));
    handle
        .sql_exec_typed(
            &cx,
            insert(&budget),
            SqlResultPolicy::ForbidRows,
            gate.clone(),
            budget,
        )
        .await
        .expect("revocation after physical commit must not turn it into no effect");
    assert!(gate.revoked.load(Ordering::SeqCst));
    assert_ne!(f.gate.epoch(&f.profile).unwrap(), cx.security_epoch());
    assert_eq!(physical_count(&f, &handle, &cx).await, "1");
}
