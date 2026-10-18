//! Opt-in model-free measurements of the real native owner, SQLite and Gate.
//! This tests exact fixed call sequences, not guest frame authority or latency.
#![cfg(all(
    feature = "closed-request-metrics",
    any(
        windows,
        all(target_os = "macos", target_arch = "aarch64"),
        all(target_os = "linux", target_arch = "x86_64", target_env = "gnu")
    )
))]

use async_trait::async_trait;
use std::{
    collections::BTreeMap,
    sync::Arc,
    time::{Duration, Instant},
};
use tobkiri_canon::{CanonValue, Digest, ids::*};
use tobkiri_gate::{
    closed_metrics::{Metric, Observation, enter_sync},
    *,
};
use tobkiri_store::{Approvals, AuditLog, AuditQuota, DataRoot};

struct NoPrompt;
#[async_trait]
impl TrustedPrompt for NoPrompt {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        ApprovalAnswer::Deny
    }
}

#[test]
fn closed_guard_metrics_count_real_selects_and_restore_request_isolation() {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
    let profile = ProfileId::parse("prof_closed_metrics").unwrap();
    let pack = PackId::parse("local.closed-metrics").unwrap();
    let hash = PackHash::new(Digest::hash(b"closed-metrics"));
    let revision = Digest::hash(b"closed-metrics-revision");
    let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
    let audit = Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap());
    let gate = GateEngine::new(
        approvals.clone(),
        audit,
        Arc::new(NoPrompt),
        MediatorChain::default(),
    );
    let limits = ResourceLimits {
        fuel_per_call: 1_000_000,
        memory_mib: 16,
        wall_ms: 60_000,
        table_elements: 32,
        output_bytes: 4096,
    };
    let policy = PermissionSet {
        wasm: limits.clone(),
        ..Default::default()
    };
    approvals
        .set_permissions(&profile, &pack, &hash, &policy)
        .unwrap();
    gate.register_pack(
        &profile,
        &pack,
        &hash,
        PackPolicy {
            declaration: policy.clone(),
            ceiling: policy.clone(),
            pack_limits: policy,
        },
    )
    .unwrap();
    gate.register_revision(
        &profile,
        &revision,
        BTreeMap::from([((pack.clone(), hash.clone()), limits.clone())]),
    )
    .unwrap();
    let observation = Observation::try_new(gate.control()).unwrap();
    let other = Observation::try_new(gate.control()).unwrap();
    let cx = CallCx::try_root_with_control(
        profile.clone(),
        Principal::Pack {
            pack: pack.clone(),
            hash: hash.clone(),
        },
        RequestId::parse("req_closed_metrics").unwrap(),
        CancellationToken::new_with_control(gate.control()).unwrap(),
        Instant::now() + Duration::from_secs(60),
        limits,
        gate.control().clone(),
    )
    .unwrap()
    .with_revision(revision)
    .with_security_epoch(gate.epoch(&profile).unwrap())
    .with_observation(&observation)
    .unwrap();
    // One real check_context = 2 SELECT statements (fault+epoch and Pack).
    // The second lookup adds the funding-owner permission statement: total 5.
    // Every guard/owner check remains; this is not a count of B-tree lookups.
    {
        let _outer = enter_sync(Some(&other));
        gate.check_context(&cx).unwrap();
        gate.code_owner_execution_limits(
            &cx,
            PreparedCodeOwner::Pack {
                pack: &pack,
                hash: &hash,
            },
        )
        .unwrap();
        // The cx scopes above restore this unrelated caller's TLS.
        approvals.security_epoch(&profile).unwrap();
    }
    let snapshot = observation.snapshot();
    assert_eq!(
        snapshot.slots[Metric::GuardFaultEpochSelect as usize].returned,
        2
    );
    assert_eq!(snapshot.slots[Metric::GuardFaultSelect as usize].started, 0);
    assert_eq!(
        snapshot.slots[Metric::GuardPackPermissionsSelect as usize].returned,
        3
    );
    assert_eq!(
        snapshot.slots[Metric::ApprovalDbLockWait as usize].returned,
        5
    );
    assert_eq!(
        other.snapshot().slots[Metric::GuardEpochSelect as usize].returned,
        1
    );
    assert_eq!(snapshot.slots[Metric::GuardEpochSelect as usize].started, 0);
    // The fresh joined read may observe epoch, but fault still precedes active
    // and the epoch result can never override cancellation.
    cx.cancel().cancel();
    assert!(matches!(gate.check_context(&cx), Err(GateError::Cancelled)));
    let stopped = observation.snapshot();
    assert_eq!(
        stopped.slots[Metric::GuardFaultEpochSelect as usize].returned,
        3
    );
    assert_eq!(stopped.slots[Metric::GuardFaultSelect as usize].started, 0);
    assert_eq!(stopped.slots[Metric::GuardEpochSelect as usize].started, 0);
}

#[test]
fn closed_audit_image_metrics_scan_existing_rows_not_insert_candidates() {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
    let profile = ProfileId::parse("prof_closed_audit_metrics").unwrap();
    let audit = AuditLog::open(&root, &profile, AuditQuota::default()).unwrap();
    let event = || AuditEvent {
        profile: profile.clone(),
        principal: Principal::User,
        request: RequestId::parse("req_closed_audit_metrics").unwrap(),
        kind: "fixed-public-fixture".into(),
        payload: CanonValue::Null,
    };
    // One pre-existing event is outside the request's observation.
    audit.append(event()).unwrap();
    let observation = Observation::try_new(&CoreControl::default()).unwrap();
    {
        let _scope = enter_sync(Some(&observation));
        for _ in 0..3 {
            audit.append(event()).unwrap();
        }
    }
    let snapshot = observation.snapshot();
    let scan = snapshot.slots[Metric::AuditImageScan as usize];
    assert_eq!(scan.started, 3);
    assert_eq!(scan.returned, 3);
    assert_eq!(scan.units0, 1 + 2 + 3);
    assert!(scan.units1 > 0); // Existing SQLite TEXT/BLOB payload bytes only.
    assert_eq!(snapshot.slots[Metric::AuditDbLockWait as usize].returned, 3);
    let before = scan.units0;
    audit.append(event()).unwrap();
    assert_eq!(
        observation.snapshot().slots[Metric::AuditImageScan as usize].units0,
        before
    );
    audit.verify().unwrap(); // Same production validation, no observation installed.
}
