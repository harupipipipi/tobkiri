use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, CanonValue, CoreValue, Digest, Variant,
    ids::{ProfileId, RequestId},
};
use tobkiri_core::AuditRunJournal;
use tobkiri_flow::{RunEvent, RunEventKind, RunJournal};
use tobkiri_gate::{
    AuditEvent, AuditSink, CallCx, CoreCancellation, GateError, Principal, ResourceLimits,
};

/// A finite owner for this pure journal fixture. Source graphs and temporary
/// serialization use distinct accounts so their actual lifetimes stay visible.
#[derive(Default)]
struct Budget {
    limit: u64,
    used: AtomicU64,
    peak: AtomicU64,
}

impl Budget {
    fn new(limit: u64) -> Self {
        Self {
            limit,
            ..Self::default()
        }
    }

    fn used(&self) -> u64 {
        self.used.load(Ordering::SeqCst)
    }
}

impl AllocationBudget for Budget {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        if request.depth > 64 || request.elements > 4096 || request.work > 1024 * 1024 {
            return Err(AllocationError::Limit);
        }
        let previous = self
            .used
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |used| {
                used.checked_add(request.bytes)
                    .filter(|next| *next <= self.limit)
            })
            .map_err(|_| AllocationError::Limit)?;
        self.peak
            .fetch_max(previous + request.bytes, Ordering::SeqCst);
        Ok(())
    }

    fn release(&self, bytes: u64) {
        self.used
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |used| {
                used.checked_sub(bytes)
            })
            .expect("journal fixture released unreserved storage");
    }
}

#[derive(Default)]
struct Audit(Mutex<Vec<AuditEvent>>);
impl AuditSink for Audit {
    fn append(&self, event: AuditEvent) -> Result<(), GateError> {
        self.0.lock().unwrap().push(event);
        Ok(())
    }
}

#[tokio::test]
async fn persisted_run_journal_contains_digests_and_no_guest_plaintext() {
    let audit = Arc::new(Audit::default());
    let journal = AuditRunJournal(audit.clone());
    let cx = CallCx::root(
        ProfileId::parse("prof_audit").unwrap(),
        Principal::User,
        RequestId::parse("req_audit").unwrap(),
        CoreCancellation::new(),
        Instant::now() + Duration::from_secs(1),
        ResourceLimits {
            fuel_per_call: 100_000,
            memory_mib: 1,
            wall_ms: 1000,
            table_elements: 1024,
            output_bytes: 64 * 1024,
        },
    );
    let literal = CanonValue::Obj(BTreeMap::from([(
        "sensitive".into(),
        CanonValue::Str("secret-plaintext-never-audit".into()),
    )]));
    let expected_value_digest = Digest::hash(
        &tobkiri_canon::encode(&literal, Variant::StrictV1).unwrap(),
    );
    let source = Arc::new(Budget::new(64 * 1024));
    let source_budget: Arc<dyn AllocationBudget> = source.clone();
    let value = CoreValue::try_from_borrowed(&literal, &source_budget).unwrap();
    drop(literal);
    let retained_bytes = source.used();
    assert!(retained_bytes > 0);
    let event = RunEvent {
        request: cx.request().clone(),
        plan_digest: Digest::hash(b"plan"),
        sequence: 1,
        kind: RunEventKind::Completed {
            value,
            cost_units: 2,
        },
    };
    // Journaling must admit its distinct encoding allocation. A denied
    // temporary buffer publishes no partial row and retains the source graph.
    let refused = Arc::new(Budget::new(0));
    let refused_budget: Arc<dyn AllocationBudget> = refused.clone();
    let error = journal
        .append(&cx, &event, &refused_budget)
        .await
        .unwrap_err();
    assert_eq!(error.code, "limit-exceeded");
    assert!(audit.0.lock().unwrap().is_empty());
    assert_eq!(refused.used(), 0);
    assert_eq!(source.used(), retained_bytes);

    let encoding = Arc::new(Budget::new(64 * 1024));
    let encoding_budget: Arc<dyn AllocationBudget> = encoding.clone();
    journal
        .append(&cx, &event, &encoding_budget)
        .await
        .unwrap();
    assert!(encoding.peak.load(Ordering::SeqCst) > 0);
    assert_eq!(encoding.used(), 0);
    assert_eq!(source.used(), retained_bytes);
    drop(event);
    assert_eq!(source.used(), 0);
    journal
        .append(
            &cx,
            &RunEvent {
                request: cx.request().clone(),
                plan_digest: Digest::hash(b"plan"),
                sequence: 2,
                kind: RunEventKind::Failed {
                    error: tobkiri_flow::StepError::new(
                        "secret-plaintext-never-audit",
                        "secret-plaintext-never-audit",
                    ),
                },
            },
            &encoding_budget,
        )
        .await
        .unwrap();
    let rows = audit.0.lock().unwrap();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].kind, "run-completed");
    assert_eq!(
        rows[0].payload.get("value_digest"),
        Some(&CanonValue::Str(expected_value_digest.to_string()))
    );
    assert_eq!(
        rows[0].payload.get("plan_digest"),
        Some(&CanonValue::Str(Digest::hash(b"plan").to_string()))
    );
    assert_eq!(rows[0].payload.get("sequence"), Some(&CanonValue::Int(1)));
    assert_eq!(
        rows[0].payload.get("run_request"),
        Some(&CanonValue::Str(cx.request().to_string()))
    );
    assert_eq!(rows[1].kind, "run-failed");
    assert_eq!(
        rows[1].payload.get("code"),
        Some(&CanonValue::Str("internal".into()))
    );
    let bytes = tobkiri_canon::encode(
        &CanonValue::Arr(rows.iter().map(|row| row.payload.clone()).collect()),
        Variant::StrictV1,
    )
    .unwrap();
    let text = String::from_utf8(bytes).unwrap();
    assert!(!text.contains("secret-plaintext-never-audit"));
    assert!(text.contains("value_digest"));
    assert!(text.contains("plan_digest"));
}
