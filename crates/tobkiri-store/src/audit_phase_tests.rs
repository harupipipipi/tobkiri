//! Opt-in unit-fixture timing of the real AuditLog append path.
//! No timings or observer state exist in production builds.

#[cfg(unix)]
use super::*;
use std::{
    sync::atomic::{AtomicU64, Ordering},
    time::Instant,
};

const PHASES: usize = 9;

#[derive(Clone, Copy)]
pub(super) enum AuditPhase {
    Total,
    Prepare,
    Lock,
    Begin,
    Verify,
    Queries,
    EncodeInsert,
    Commit,
    FullVerify,
}

#[derive(Default)]
struct Counter {
    calls: AtomicU64,
    total_us: AtomicU64,
    max_us: AtomicU64,
}

#[derive(Default)]
pub(super) struct AuditPhaseTiming {
    phases: [Counter; PHASES],
}

pub(super) struct AuditPhaseSpan<'a> {
    counter: &'a Counter,
    start: Instant,
}

impl Drop for AuditPhaseSpan<'_> {
    fn drop(&mut self) {
        let micros = u64::try_from(self.start.elapsed().as_micros()).unwrap_or(u64::MAX);
        self.counter.calls.fetch_add(1, Ordering::Relaxed);
        self.counter.total_us.fetch_add(micros, Ordering::Relaxed);
        self.counter.max_us.fetch_max(micros, Ordering::Relaxed);
    }
}

impl AuditPhaseTiming {
    pub(super) fn start(&self, phase: AuditPhase) -> AuditPhaseSpan<'_> {
        AuditPhaseSpan {
            counter: &self.phases[phase as usize],
            start: Instant::now(),
        }
    }

    #[cfg(unix)]
    fn calls(&self, phase: AuditPhase) -> u64 {
        self.phases[phase as usize].calls.load(Ordering::Relaxed)
    }

    #[cfg(unix)]
    fn report(&self, batch: &'static str, rows: u64) {
        use std::io::Write as _;
        let mut stderr = std::io::stderr().lock();
        let _ = write!(stderr, "AUDIT_PHASE batch={batch} rows={rows}");
        for (name, counter) in [
            "total",
            "prepare",
            "lock",
            "begin",
            "verify",
            "queries",
            "encode_insert",
            "commit",
            "full_verify",
        ]
        .into_iter()
        .zip(&self.phases)
        {
            let _ = write!(
                stderr,
                " {name}_calls={} {name}_total_us={} {name}_max_us={}",
                counter.calls.load(Ordering::Relaxed),
                counter.total_us.load(Ordering::Relaxed),
                counter.max_us.load(Ordering::Relaxed),
            );
        }
        let _ = writeln!(stderr);
    }
}

#[test]
#[cfg(unix)]
fn audit_phase_costs_preserve_chain_and_idempotency() {
    use std::os::unix::fs::PermissionsExt;
    use tobkiri_canon::{CanonValue, ids::RequestId};
    use tobkiri_gate::{IntegrityFaultCode, Principal};

    // Only this new ephemeral fixture directory gets its required private mode.
    let dir = tempfile::Builder::new()
        .permissions(std::fs::Permissions::from_mode(0o700))
        .tempdir()
        .unwrap();
    let root = DataRoot::open(dir.path().canonicalize().unwrap()).unwrap();
    let profile = ProfileId::parse("prof_audit_phase").unwrap();
    let mut audit = AuditLog::open(&root, &profile, AuditQuota::default()).unwrap();
    let event = |index: u64| AuditEvent {
        profile: profile.clone(),
        principal: Principal::User,
        request: RequestId::parse(&format!("req_phase_{index}")).unwrap(),
        kind: "phase_fixture".into(),
        payload: CanonValue::Obj(
            [
                ("index".into(), CanonValue::Int(index as i64)),
                ("status".into(), CanonValue::Str("cancelled".into())),
            ]
            .into(),
        ),
    };
    for index in 0..256 {
        audit.append(event(index)).unwrap();
    }
    assert!(audit.phase_timing.is_none());

    audit.phase_timing = Some(AuditPhaseTiming::default());
    for index in 256_u64..320 {
        audit
            .append_idempotent(&Digest::hash(&index.to_le_bytes()), event(index))
            .unwrap();
    }
    let timing = audit.phase_timing.take().unwrap();
    assert_eq!(timing.calls(AuditPhase::Total), 64);
    assert_eq!(timing.calls(AuditPhase::Verify), 64);
    assert_eq!(timing.calls(AuditPhase::EncodeInsert), 64);
    assert_eq!(timing.calls(AuditPhase::Commit), 64);
    assert_eq!(timing.calls(AuditPhase::FullVerify), 0);
    timing.report("unique", 320);
    audit.verify().unwrap();

    audit.phase_timing = Some(AuditPhaseTiming::default());
    for index in 256_u64..320 {
        audit
            .append_idempotent(&Digest::hash(&index.to_le_bytes()), event(index))
            .unwrap();
    }
    let timing = audit.phase_timing.take().unwrap();
    assert_eq!(timing.calls(AuditPhase::Total), 64);
    assert_eq!(timing.calls(AuditPhase::Verify), 64);
    assert_eq!(timing.calls(AuditPhase::EncodeInsert), 0);
    assert_eq!(timing.calls(AuditPhase::Commit), 64);
    // Explicit verify cleared the proof, so the first retry fully verifies.
    assert_eq!(timing.calls(AuditPhase::FullVerify), 1);
    timing.report("exact_retry", 320);
    audit.verify().unwrap();

    {
        let connection = audit.connection.lock().unwrap();
        let rows: u64 = connection
            .query_row("SELECT COUNT(*) FROM audit", [], |row| {
                crate::sql_u64(row, 0)
            })
            .unwrap();
        let identified: u64 = connection
            .query_row("SELECT COUNT(DISTINCT event_id) FROM audit", [], |row| {
                crate::sql_u64(row, 0)
            })
            .unwrap();
        assert_eq!(rows, 320);
        assert_eq!(identified, 64);
        for index in 256_u64..320 {
            let body: Vec<u8> = connection
                .query_row(
                    "SELECT body FROM audit WHERE event_id=?1",
                    [Digest::hash(&index.to_le_bytes()).as_str()],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(body, canonical(&event(index)).unwrap());
        }
    }
    assert!(matches!(
        audit.append_idempotent(&Digest::hash(&256_u64.to_le_bytes()), event(999)),
        Err(GateError::Integrity(
            IntegrityFaultCode::AuditIdentityConflict
        ))
    ));
    audit.verify().unwrap();

    // A real second SQLite connection corrupts an OLD row. Even an exact
    // retry must verify the whole chain and reject it, not accept the ID hit.
    let raw =
        rusqlite::Connection::open(root.path().join(format!("profiles/{profile}/audit.sqlite")))
            .unwrap();
    raw.execute_batch("DROP TRIGGER audit_no_update; UPDATE audit SET digest='bad' WHERE seq=1;")
        .unwrap();
    assert!(matches!(
        audit.append_idempotent(&Digest::hash(&256_u64.to_le_bytes()), event(256)),
        Err(GateError::Integrity(IntegrityFaultCode::AuditChain))
    ));
    assert!(matches!(
        audit.verify(),
        Err(StoreError::Integrity(IntegrityFaultCode::AuditChain))
    ));
}
