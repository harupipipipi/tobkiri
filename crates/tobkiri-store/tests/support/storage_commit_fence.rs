//! Actual Gate and separate durable authority connection at a consumed write.
use super::*;
use std::sync::atomic::AtomicBool;

/// All decisions and permits come from the real Gate. The observer revokes
/// through another real Gate/connection after the normal lease-watch boundary.
struct RevokeAfterWatch {
    original: Arc<GateEngine>,
    revoker: GateEngine,
    revoked: AtomicBool,
}

#[async_trait]
impl Gate for RevokeAfterWatch {
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
        assert!(
            !self.revoked.swap(true, Ordering::SeqCst),
            "one physical write must have one original consumed lease"
        );
        self.revoker.revoke_epoch(cx.profile())?;
        Ok(())
    }

    async fn record_outcome(&self, grant: GrantId, outcome: Outcome) -> Result<(), GateError> {
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

#[tokio::test]
async fn sql_write_rejects_epoch_revoked_after_consumption() {
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

    let second_approvals = Arc::new(Approvals::open(&f.root, &f.profile).unwrap());
    let second_audit =
        Arc::new(AuditLog::open(&f.root, &f.profile, AuditQuota::default()).unwrap());
    let gate = Arc::new(RevokeAfterWatch {
        original: f.gate.clone(),
        revoker: GateEngine::new_with_control(
            second_approvals,
            second_audit,
            f.prompt.clone(),
            MediatorChain::default(),
            f.gate.control().clone(),
        ),
        revoked: AtomicBool::new(false),
    });
    let budget: Arc<dyn AllocationBudget> = Arc::new(SqlTestValues(AtomicUsize::new(0)));
    let statement = OwnedSqlStatement::from_value(
        CoreValue::try_from_borrowed(
            &CanonValue::Obj(std::collections::BTreeMap::from([
                (
                    "statement".into(),
                    CanonValue::Str("INSERT INTO activation(value) VALUES(1)".into()),
                ),
                ("params".into(), CanonValue::Arr(vec![])),
                ("max-rows".into(), CanonValue::Int(10)),
            ])),
            &budget,
        )
        .unwrap(),
    )
    .unwrap();
    let result = handle
        .sql_exec_typed(
            &cx,
            statement,
            SqlResultPolicy::ForbidRows,
            gate.clone(),
            budget,
        )
        .await;
    assert!(gate.revoked.load(Ordering::SeqCst));
    assert_ne!(f.gate.epoch(&f.profile).unwrap(), cx.security_epoch());

    // Observe the real physical DATA with a fresh context after revocation.
    // The rejected old operation must not leave an activation row behind.
    let fresh = f.cx(cx.principal().clone());
    let rows = native_sql(
        &f,
        &handle,
        &fresh,
        "SELECT count(*) FROM activation",
        &[],
        true,
    )
    .await
    .unwrap();
    let count = rows
        .value()
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
        .to_owned();
    assert_eq!(count, "0", "revoked write physically committed activation");
    assert!(matches!(
        result,
        Err(StoreError::Gate(GateError::StaleEpoch))
    ));
}
