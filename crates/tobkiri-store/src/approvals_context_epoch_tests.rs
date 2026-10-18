//! Actual disposable Approvals connections; no cached authority or fake SQLite.
#![cfg(any(
    windows,
    all(target_os = "macos", target_arch = "aarch64"),
    all(target_os = "linux", target_arch = "x86_64", target_env = "gnu")
))]

use super::*;
use rusqlite::hooks::{AuthAction, AuthContext, Authorization};
use std::{
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::ids::RequestId;
use tobkiri_gate::{CallCx, CancellationToken, Principal, ResourceLimits};

struct Fixture {
    reader: Approvals,
    writer: Approvals,
    _root: DataRoot,
    profile: ProfileId,
    _temp: tempfile::TempDir,
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
        let profile = ProfileId::parse("prof_context_epoch").unwrap();
        let reader = Approvals::open(&root, &profile).unwrap();
        let writer = Approvals::open(&root, &profile).unwrap();
        Self {
            reader,
            writer,
            _root: root,
            profile,
            _temp: temp,
        }
    }
    fn cx(&self, epoch: u64) -> CallCx {
        context(
            &self.profile,
            epoch,
            Instant::now() + Duration::from_secs(60),
        )
    }
    fn sql(&self, sql: &str) {
        // Deliberately corrupt only this disposable unit fixture, never a live DB.
        self.writer
            .connection
            .lock()
            .unwrap()
            .execute_batch(sql)
            .unwrap();
    }
}
fn context(profile: &ProfileId, epoch: u64, deadline: Instant) -> CallCx {
    CallCx::root(
        profile.clone(),
        Principal::User,
        RequestId::parse("req_context_epoch").unwrap(),
        CancellationToken::new(),
        deadline,
        ResourceLimits::default(),
    )
    .with_security_epoch(epoch)
}

#[derive(Debug, PartialEq)]
enum Kind {
    Allowed,
    Cancelled,
    Deadline,
    Stale,
    Denied,
    Persistence,
    Integrity(IntegrityFaultCode),
}
fn kind(value: Result<(), GateError>) -> Kind {
    match value {
        Ok(()) => Kind::Allowed,
        Err(GateError::Cancelled) => Kind::Cancelled,
        Err(GateError::Deadline) => Kind::Deadline,
        Err(GateError::StaleEpoch) => Kind::Stale,
        Err(GateError::Denied(_)) => Kind::Denied,
        Err(GateError::Persistence(_)) => Kind::Persistence,
        Err(GateError::Integrity(code)) => Kind::Integrity(code),
        other => panic!("unexpected fixture result: {other:?}"),
    }
}
fn separate(approvals: &Approvals, cx: &CallCx) -> Result<(), GateError> {
    // The original production sequence remains the independent comparison path.
    match approvals.admission_status(cx.profile())? {
        AdmissionStatus::Allowed => {}
        AdmissionStatus::IntegrityFault { code } => return Err(GateError::Integrity(code)),
    }
    cx.check_active()?;
    if approvals.security_epoch(cx.profile())? != cx.security_epoch() {
        return Err(GateError::StaleEpoch);
    }
    Ok(())
}
fn assert_both(f: &Fixture, cx: &CallCx, expected: Kind) {
    assert_eq!(kind(separate(&f.reader, cx)), expected);
    assert_eq!(kind(f.reader.check_context_epoch(cx)), expected);
}
fn deny_reads(approvals: &Approvals, table: &'static str) {
    approvals
        .connection
        .lock()
        .unwrap()
        .authorizer(Some(move |cx: AuthContext<'_>| match cx.action {
            AuthAction::Read { table_name, .. } if table_name == table => Authorization::Deny,
            _ => Authorization::Allow,
        }))
        .unwrap();
}
fn restore_reads(approvals: &Approvals) {
    approvals
        .connection
        .lock()
        .unwrap()
        .authorizer(None::<fn(AuthContext<'_>) -> Authorization>)
        .unwrap();
}

#[test]
fn context_epoch_reads_fresh_values_from_a_second_connection() {
    let f = Fixture::new();
    let original = f.cx(0);
    for _ in 0..2 {
        assert_both(&f, &original, Kind::Allowed);
    }
    assert_eq!(f.writer.advance_security_epoch(&f.profile).unwrap(), 1);
    assert_both(&f, &original, Kind::Stale);
    assert_both(&f, &f.cx(1), Kind::Allowed);
    f.writer
        .latch_integrity(&f.profile, IntegrityFaultCode::AuditChain)
        .unwrap();
    assert_both(
        &f,
        &f.cx(1),
        Kind::Integrity(IntegrityFaultCode::AuditChain),
    );
    original.cancel().cancel();
    assert_both(
        &f,
        &original,
        Kind::Integrity(IntegrityFaultCode::AuditChain),
    );
}

#[test]
fn context_epoch_keeps_fault_before_active_and_active_before_epoch() {
    let f = Fixture::new();
    let cancelled = f.cx(0);
    cancelled.cancel().cancel();
    f.writer.advance_security_epoch(&f.profile).unwrap();
    assert_both(&f, &cancelled, Kind::Cancelled);
    let expired = context(&f.profile, 0, Instant::now());
    assert_both(&f, &expired, Kind::Deadline);
    f.writer
        .latch_integrity(&f.profile, IntegrityFaultCode::AuditChain)
        .unwrap();
    assert_both(
        &f,
        &cancelled,
        Kind::Integrity(IntegrityFaultCode::AuditChain),
    );
    assert_both(
        &f,
        &expired,
        Kind::Integrity(IntegrityFaultCode::AuditChain),
    );
}

#[test]
fn context_epoch_missing_row_is_not_zero_and_can_be_reinserted() {
    let f = Fixture::new();
    let cx = f.cx(0);
    assert_both(&f, &cx, Kind::Allowed);
    f.sql("DELETE FROM security_epoch WHERE singleton=1");
    assert_both(&f, &cx, Kind::Persistence);
    let cancelled = f.cx(0);
    cancelled.cancel().cancel();
    assert_both(&f, &cancelled, Kind::Cancelled);
    f.sql("INSERT INTO security_epoch(singleton,epoch) VALUES(1,7)");
    assert_both(&f, &cx, Kind::Stale);
    assert_both(&f, &f.cx(7), Kind::Allowed);
}

#[test]
fn context_epoch_null_negative_text_and_blob_are_not_filled_or_coerced() {
    for value in ["NULL", "-1", "'not-an-epoch'", "X'00'"] {
        let f = Fixture::new();
        assert_both(&f, &f.cx(0), Kind::Allowed);
        f.sql("DROP TABLE security_epoch; CREATE TABLE security_epoch(singleton INTEGER PRIMARY KEY, epoch)");
        f.sql(&format!(
            "INSERT INTO security_epoch(singleton,epoch) VALUES(1,{value})"
        ));
        assert_both(&f, &f.cx(0), Kind::Persistence);
        let cancelled = f.cx(0);
        cancelled.cancel().cancel();
        assert_both(&f, &cancelled, Kind::Cancelled);
        f.writer
            .latch_integrity(&f.profile, IntegrityFaultCode::AuditChain)
            .unwrap();
        assert_both(
            &f,
            &f.cx(0),
            Kind::Integrity(IntegrityFaultCode::AuditChain),
        );
        assert_both(
            &f,
            &cancelled,
            Kind::Integrity(IntegrityFaultCode::AuditChain),
        );
    }
}

#[test]
fn context_epoch_present_null_or_bad_fault_is_not_absent_authority() {
    for (value, expected) in [
        ("NULL", Kind::Persistence),
        ("'not-a-blob'", Kind::Persistence),
        (
            "X'7b'",
            Kind::Integrity(IntegrityFaultCode::AuthorityDatabase),
        ),
        (
            "X'6e756c6c'",
            Kind::Integrity(IntegrityFaultCode::AuthorityDatabase),
        ),
    ] {
        let f = Fixture::new();
        assert_both(&f, &f.cx(0), Kind::Allowed);
        f.sql("DROP TABLE authority_fault; CREATE TABLE authority_fault(singleton INTEGER PRIMARY KEY,code)");
        f.sql(&format!(
            "INSERT INTO authority_fault(singleton,code) VALUES(1,{value})"
        ));
        let cancelled = f.cx(0);
        cancelled.cancel().cancel();
        assert_both(&f, &cancelled, expected);
    }
}

#[test]
fn context_epoch_combined_prepare_failure_uses_the_old_read_order() {
    let f = Fixture::new();
    assert_both(&f, &f.cx(0), Kind::Allowed);
    f.sql("DROP TABLE security_epoch");
    // The JOIN cannot prepare, yet the original sequence never reaches epoch
    // when cancellation or the immutable authority fault already decides it.
    let cancelled = f.cx(0);
    cancelled.cancel().cancel();
    assert_both(&f, &cancelled, Kind::Cancelled);
    assert_both(&f, &f.cx(0), Kind::Persistence);
    let encoded = canonical(&IntegrityFaultCode::AuditChain).unwrap();
    f.writer
        .connection
        .lock()
        .unwrap()
        .execute(
            "INSERT INTO authority_fault(singleton,code) VALUES(1,?1)",
            [encoded],
        )
        .unwrap();
    assert_both(
        &f,
        &cancelled,
        Kind::Integrity(IntegrityFaultCode::AuditChain),
    );
    assert_both(
        &f,
        &f.cx(0),
        Kind::Integrity(IntegrityFaultCode::AuditChain),
    );
}

#[test]
fn context_epoch_authorizer_deny_restore_and_priority_survive_cached_statements() {
    let f = Fixture::new();
    assert_both(&f, &f.cx(0), Kind::Allowed);
    deny_reads(&f.reader, "security_epoch");
    assert_both(&f, &f.cx(0), Kind::Persistence);
    let cancelled = f.cx(0);
    cancelled.cancel().cancel();
    assert_both(&f, &cancelled, Kind::Cancelled);
    restore_reads(&f.reader);
    assert_both(&f, &f.cx(0), Kind::Allowed);
    deny_reads(&f.reader, "authority_fault");
    assert_both(&f, &cancelled, Kind::Persistence);
    restore_reads(&f.reader);
    assert_both(&f, &cancelled, Kind::Cancelled);
    f.writer
        .latch_integrity(&f.profile, IntegrityFaultCode::AuditChain)
        .unwrap();
    deny_reads(&f.reader, "security_epoch");
    assert_both(
        &f,
        &cancelled,
        Kind::Integrity(IntegrityFaultCode::AuditChain),
    );
}

#[test]
fn context_epoch_rechecks_the_actual_token_after_the_fresh_read() {
    let f = Fixture::new();
    let cx = f.cx(0);
    assert_both(&f, &cx, Kind::Allowed);
    let token = cx.cancel().clone();
    f.reader
        .connection
        .lock()
        .unwrap()
        .authorizer(Some(move |auth: AuthContext<'_>| {
            if matches!(
                auth.action,
                AuthAction::Read {
                    table_name: "security_epoch",
                    ..
                }
            ) {
                token.cancel();
            }
            Authorization::Allow
        }))
        .unwrap();
    assert_eq!(kind(f.reader.check_context_epoch(&cx)), Kind::Cancelled);
    restore_reads(&f.reader);
    assert_both(&f, &cx, Kind::Cancelled);
}

#[test]
fn context_epoch_other_profile_is_denied_before_any_sql_even_when_cancelled() {
    let f = Fixture::new();
    assert_both(&f, &f.cx(0), Kind::Allowed);
    let reads = Arc::new(AtomicUsize::new(0));
    let counted = reads.clone();
    f.reader
        .connection
        .lock()
        .unwrap()
        .authorizer(Some(move |auth: AuthContext<'_>| {
            if matches!(auth.action, AuthAction::Read { .. }) {
                counted.fetch_add(1, Ordering::SeqCst);
            }
            Authorization::Allow
        }))
        .unwrap();
    let other = ProfileId::parse("prof_context_epoch_other").unwrap();
    let cx = context(&other, 0, Instant::now() + Duration::from_secs(60));
    assert_both(&f, &cx, Kind::Denied);
    cx.cancel().cancel();
    assert_both(&f, &cx, Kind::Denied);
    assert_eq!(reads.load(Ordering::SeqCst), 0);
}

#[test]
fn context_epoch_i64_edge_is_exact_and_reads_do_not_write() {
    let f = Fixture::new();
    f.sql("UPDATE security_epoch SET epoch=9223372036854775807 WHERE singleton=1");
    let before = f.reader.connection.lock().unwrap().total_changes();
    assert_both(&f, &f.cx(i64::MAX as u64), Kind::Allowed);
    assert_both(&f, &f.cx(u64::MAX), Kind::Stale);
    deny_reads(&f.reader, "security_epoch");
    assert_both(&f, &f.cx(i64::MAX as u64), Kind::Persistence);
    restore_reads(&f.reader);
    assert_eq!(f.reader.connection.lock().unwrap().total_changes(), before);
}

#[cfg(feature = "closed-request-metrics")]
#[test]
fn context_epoch_metrics_count_one_statement_and_report_fallback_separately() {
    use tobkiri_gate::closed_metrics::{Metric, Observation, enter_sync};
    let f = Fixture::new();
    let cx = f.cx(0);
    let observation = Observation::try_new(cx.control()).unwrap();
    let _scope = enter_sync(Some(&observation));
    f.reader.check_context_epoch(&cx).unwrap();
    let first = observation.snapshot();
    assert_eq!(
        first.slots[Metric::GuardFaultEpochSelect as usize].started,
        1
    );
    assert_eq!(
        first.slots[Metric::GuardFaultEpochSelect as usize].returned,
        1
    );
    assert_eq!(first.slots[Metric::GuardFaultSelect as usize].started, 0);
    assert_eq!(first.slots[Metric::GuardEpochSelect as usize].started, 0);
    assert_eq!(first.slots[Metric::ApprovalDbLockWait as usize].returned, 1);
    deny_reads(&f.reader, "security_epoch");
    assert_eq!(kind(f.reader.check_context_epoch(&cx)), Kind::Persistence);
    let fallback = observation.snapshot();
    assert_eq!(
        fallback.slots[Metric::GuardFaultEpochSelect as usize].returned,
        2
    );
    assert_eq!(
        fallback.slots[Metric::GuardFaultSelect as usize].returned,
        1
    );
    assert_eq!(
        fallback.slots[Metric::GuardEpochSelect as usize].returned,
        1
    );
    assert_eq!(
        fallback.slots[Metric::ApprovalDbLockWait as usize].returned,
        4
    );
    assert!(!fallback.overflow);
}
