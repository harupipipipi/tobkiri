//! Opt-in timing of the real append-only AuditLog on disposable private DATA.
//! No live Profile, model, network, approvals or quota relaxation is involved.

use std::time::{Duration, Instant};
use tobkiri_canon::{
    CanonValue, Variant,
    ids::{ProfileId, RequestId},
};
use tobkiri_gate::{AuditEvent, AuditSink, Principal};
use tobkiri_store::{AuditLog, AuditQuota, DataRoot};

const TARGETS: [usize; 4] = [64, 128, 256, 512];
const SOFT_WALL_LIMIT: Duration = Duration::from_secs(90);

fn event(profile: &ProfileId, index: usize) -> AuditEvent {
    // Fixed public fixture, shaped like an outcome. Construct outside timings.
    let payload = serde_json::json!({
        "grant": format!("grant_{index:064x}"),
        "effect_digest": format!("sha256:{:064x}", 1),
        "args_digest": format!("sha256:{:064x}", 2),
        "security_epoch": 1,
        "outcome": {"status": "succeeded"},
        "disposition": "completed"
    });
    AuditEvent {
        profile: profile.clone(),
        principal: Principal::User,
        request: RequestId::parse("req_offline_audit_profile").unwrap(),
        kind: "outcome".into(),
        payload: tobkiri_canon::parse(&serde_json::to_vec(&payload).unwrap(), Variant::StrictV1)
            .unwrap(),
    }
}

fn fixture() -> (tempfile::TempDir, DataRoot, ProfileId, AuditLog) {
    let temp = tempfile::tempdir().unwrap();
    // The constructor creates its ordinary protected Windows DACL/private mode.
    let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
    let profile = ProfileId::parse("prof_offline_audit_profile").unwrap();
    let audit = AuditLog::open(&root, &profile, AuditQuota::default()).unwrap();
    (temp, root, profile, audit)
}

fn prior_rows_for_appends(start: usize, end: usize) -> u64 {
    // Each successful append verifies the existing 0..N-1 rows before INSERT.
    (start..end).map(|n| n as u64).sum()
}

#[test]
fn private_fixture_reopens_and_retains_all_immutable_events() {
    let (temp, root, profile, audit) = fixture();
    for index in 0..4 {
        audit.append(event(&profile, index)).unwrap();
    }
    audit.verify().unwrap();
    drop(audit);
    let path = temp
        .path()
        .join("data")
        .join(DataRoot::profile_relative(&profile, "audit.sqlite").unwrap());
    // The live VFS owner is gone before independent read-only fixture inspection.
    let raw =
        rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .unwrap();
    let count: i64 = raw
        .query_row("SELECT COUNT(*) FROM audit", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 4);
    let body: Vec<u8> = raw
        .query_row("SELECT body FROM audit WHERE seq=4", [], |r| r.get(0))
        .unwrap();
    let stored: AuditEvent = serde_json::from_slice(&body).unwrap();
    assert_eq!(stored.profile, profile);
    assert_eq!(stored.kind, "outcome");
    assert_ne!(stored.payload, CanonValue::Null);
    assert_eq!(
        serde_json::to_value(&stored).unwrap(),
        serde_json::to_value(event(&profile, 3)).unwrap()
    );
    raw.close().unwrap();
    let reopened = AuditLog::open(&root, &profile, AuditQuota::default()).unwrap();
    reopened.verify().unwrap();
    assert_eq!(prior_rows_for_appends(0, 4), 6);
    assert_eq!(prior_rows_for_appends(64, 128), 6112);
}

#[test]
#[ignore = "bounded offline profiling; root runs explicitly with --ignored --nocapture --test-threads=1"]
fn profile_real_audit_append_and_verify_at_bounded_sizes() {
    let whole_start = Instant::now();
    let setup_start = Instant::now();
    let (_temp, _root, profile, audit) = fixture();
    let setup_ns = setup_start.elapsed().as_nanos();
    let build_start = Instant::now();
    let events: Vec<_> = (0..*TARGETS.last().unwrap())
        .map(|n| event(&profile, n))
        .collect();
    let fixture_event_build_ns = build_start.elapsed().as_nanos();
    // At most 512 records of this bounded public fixture, below the unchanged
    // default 600 records/minute and 1 MiB/minute quotas for one writer.
    let total_body_bytes: usize = events
        .iter()
        .map(|v| serde_json::to_vec(v).unwrap().len())
        .sum();
    assert!(total_body_bytes < AuditQuota::default().bytes_per_minute as usize);
    println!(
        "{}",
        serde_json::json!({
            "kind": "offline_audit_setup", "format": "tobkiri.audit-cost.v1",
            "setup_ns": setup_ns, "fixture_event_build_ns": fixture_event_build_ns,
            "event_count": events.len(), "fixture_serialized_body_bytes": total_body_bytes,
            "default_quota_unchanged": true, "live_database_used": false,
            "soft_wall_seconds": SOFT_WALL_LIMIT.as_secs(),
            "soft_wall_scope": "checked between synchronous operations; cannot interrupt a single append or verify"
        })
    );
    let mut events = events.into_iter();
    let mut appended = 0;
    for target in TARGETS {
        let before = appended;
        let append_start = Instant::now();
        while appended < target && whole_start.elapsed() < SOFT_WALL_LIMIT {
            audit.append(events.next().unwrap()).unwrap();
            appended += 1;
        }
        let append_ns = append_start.elapsed().as_nanos();
        let verify_ns = if whole_start.elapsed() < SOFT_WALL_LIMIT {
            let start = Instant::now();
            audit.verify().unwrap();
            Some(start.elapsed().as_nanos())
        } else {
            None
        };
        let complete = appended == target && verify_ns.is_some();
        println!(
            "{}",
            serde_json::json!({
                "kind": "offline_audit_batch", "target_rows": target,
                "rows_before": before, "rows_after": appended,
                "appended": appended - before, "append_batch_ns": append_ns,
                "explicit_verify_ns": verify_ns, "explicit_verify_rows": verify_ns.map(|_| appended),
                "theoretical_existing_rows_verified_during_successful_appends": prior_rows_for_appends(before, appended),
                "complete": complete, "whole_elapsed_ns": whole_start.elapsed().as_nanos(),
                "interpretation": "append includes real transaction/chain/quota/sync work; not isolated CPU time or Core/LLM latency"
            })
        );
        if !complete {
            return;
        }
    }
}
