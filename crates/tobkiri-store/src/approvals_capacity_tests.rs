//! Real SQLite/Approvals unit fixtures. These are not Core or provider acceptance.
use super::*;
use crate::{AuditLog, AuditQuota, DataRoot};
use std::sync::Arc;
use tobkiri_canon::{Digest, ids::*};
use tobkiri_gate::{
    AdmissionStatus, ApprovalStore, AuditSink, EffectRequest, GrantId, Outcome, PermissionEffect,
    PermissionSet, Principal, Reservation,
};

struct Fixture {
    _temp: tempfile::TempDir,
    root: DataRoot,
    profile: ProfileId,
    approvals: Arc<super::super::Approvals>,
    audit: AuditLog,
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
        let profile = ProfileId::parse("prof_capacity").unwrap();
        let approvals = Arc::new(super::super::Approvals::open(&root, &profile).unwrap());
        let audit = AuditLog::open(&root, &profile, AuditQuota::default()).unwrap();
        Self {
            _temp: temp,
            root,
            profile,
            approvals,
            audit,
        }
    }
    fn reservation(&self, label: &str) -> Reservation {
        Reservation {
            id: serde_json::from_value(serde_json::Value::String(format!(
                "grant_{}",
                Digest::hash(label.as_bytes()).hex()
            )))
            .unwrap(),
            profile: self.profile.clone(),
            principal: Principal::User,
            request: RequestId::parse("req_capacity").unwrap(),
            effect: EffectRequest::AuditRead.digest().unwrap(),
            args: Digest::hash(label.as_bytes()),
            epoch: 0,
            revision: None,
            expires_unix_ms: super::super::now_ms().unwrap() + 3_600_000,
            original_expires_unix_ms: None,
            authority: None,
        }
    }
    fn raw(&self) -> Connection {
        Connection::open(
            self.root
                .path()
                .join(format!("profiles/{}/approvals.sqlite", self.profile)),
        )
        .unwrap()
    }
    fn info(&self) -> Snapshot {
        snapshot(&self.approvals.connection.lock().unwrap()).unwrap()
    }
    fn amount(&self, id: &GrantId) -> u64 {
        let value: Vec<u8> = self
            .raw()
            .query_row(
                "SELECT remaining FROM terminal_capacity_escrow WHERE grant_id=?1",
                [id.as_str()],
                |row| row.get(0),
            )
            .unwrap();
        number(&value).unwrap()
    }
    fn consumed(&self, label: &str) -> Reservation {
        let r = self.reservation(label);
        self.approvals.reserve(&r).unwrap();
        self.approvals.dispatch(&r.id).unwrap();
        self.approvals
            .consume_with_deadline(&r, r.expires_unix_ms)
            .unwrap()
    }
    fn ack(&self, store: &super::super::Approvals, id: &GrantId) {
        let terminal = store.terminal_delivery(id).unwrap().unwrap().decision;
        self.audit
            .append_idempotent(&terminal.event_id, terminal.event.clone())
            .unwrap();
        store
            .acknowledge_terminal(&terminal.event_id, &terminal.event)
            .unwrap();
    }
    fn fill_active(&self) -> Vec<Reservation> {
        let mut rows = Vec::new();
        for index in 0..4096 {
            let r = self.reservation(&format!("fill{index}"));
            match self.approvals.reserve(&r) {
                Ok(()) => rows.push(r),
                Err(GateError::Limit(_)) => return rows,
                Err(error) => panic!("unexpected admission error: {error}"),
            }
        }
        panic!("finite original reservation quota did not close");
    }
    fn remove_accounting_for_old_schema(&self) {
        // A trusted fixture reconstructs the pre-escrow schema, not a product
        // reclamation path. The exact old reservation/outbox bytes remain.
        self.raw().execute_batch("DROP TABLE terminal_capacity_escrow; DROP TABLE terminal_capacity_state; PRAGMA user_version=0;").unwrap();
    }
}

#[test]
fn acknowledged_full_receipts_allow_more_than_387_sequential_operations() {
    let f = Fixture::new();
    for index in 0..420 {
        let r = f.consumed(&format!("receipt{index}"));
        f.approvals
            .finish_terminal(
                &r.id,
                &Outcome::Failed {
                    code: "x".repeat(128),
                },
            )
            .unwrap();
        let before = f
            .approvals
            .terminal_delivery(&r.id)
            .unwrap()
            .unwrap()
            .decision;
        f.ack(&f.approvals, &r.id);
        assert_eq!(f.amount(&r.id), 0);
        // Repeated ACK validates the same complete decision and does not remint.
        f.approvals
            .acknowledge_terminal(&before.event_id, &before.event)
            .unwrap();
    }
    let raw = f.raw();
    let retained: i64 = raw
        .query_row(
            "SELECT COUNT(*) FROM terminal_outbox WHERE delivered=1 AND length(body)>0",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(retained, 420);
    assert!(f.info().total < LIMIT);
    let reopened = super::super::Approvals::open(&f.root, &f.profile).unwrap();
    assert_eq!(
        snapshot(&reopened.connection.lock().unwrap())
            .unwrap()
            .total,
        f.info().total
    );
    f.audit.verify().unwrap();
    assert!(raw.execute("DELETE FROM terminal_outbox", []).is_err());
    assert!(
        raw.execute("UPDATE terminal_outbox SET body=x''", [])
            .is_err()
    );
}

#[test]
fn terminal_and_ack_faults_roll_back_own_escrow_and_complete_evidence() {
    let f = Fixture::new();
    let r = f.consumed("fault");
    let raw = f.raw();
    raw.execute_batch("CREATE TRIGGER capacity_terminal_fault BEFORE INSERT ON terminal_outbox BEGIN SELECT RAISE(ABORT,'injected terminal fault'); END;").unwrap();
    let amount = f.amount(&r.id);
    assert!(
        f.approvals
            .finish_terminal(&r.id, &Outcome::Succeeded)
            .is_err()
    );
    assert_eq!(f.amount(&r.id), amount);
    assert_eq!(
        f.approvals.reservation_state(&r.id).unwrap().as_deref(),
        Some("consumed")
    );
    assert!(f.approvals.terminal_delivery(&r.id).unwrap().is_none());
    raw.execute_batch("DROP TRIGGER capacity_terminal_fault;")
        .unwrap();
    let first = f
        .approvals
        .finish_terminal(&r.id, &Outcome::Succeeded)
        .unwrap()
        .decision;
    raw.execute_batch("CREATE TRIGGER capacity_ack_fault BEFORE UPDATE OF state ON terminal_capacity_escrow WHEN NEW.state='closed' BEGIN SELECT RAISE(ABORT,'injected after delivered update'); END;").unwrap();
    let amount = f.amount(&r.id);
    f.audit
        .append_idempotent(&first.event_id, first.event.clone())
        .unwrap();
    assert!(
        f.approvals
            .acknowledge_terminal(&first.event_id, &first.event)
            .is_err()
    );
    assert_eq!(f.amount(&r.id), amount);
    let kept = f.approvals.terminal_delivery(&r.id).unwrap().unwrap();
    assert!(!kept.acknowledged);
    assert_eq!(kept.decision.event_id, first.event_id);
    raw.execute_batch("DROP TRIGGER capacity_ack_fault;")
        .unwrap();
    let reopened = super::super::Approvals::open(&f.root, &f.profile).unwrap();
    reopened
        .acknowledge_terminal(&first.event_id, &first.event)
        .unwrap();
    assert_eq!(f.amount(&r.id), 0);
}

#[test]
fn ordinary_and_permission_bodies_cannot_spend_terminal_escrow_at_closed_admission() {
    let f = Fixture::new();
    let pack = PackId::parse("local.capacity").unwrap();
    let hash = PackHash::new(Digest::hash(b"capacity"));
    let mut r = f.reservation("permission");
    r.effect = EffectRequest::Permissions {
        pack: pack.clone(),
        hash: hash.clone(),
    }
    .digest()
    .unwrap();
    f.approvals.reserve(&r).unwrap();
    f.approvals.dispatch(&r.id).unwrap();
    let rows = f.fill_active();
    assert!(!rows.is_empty());
    assert!(matches!(
        f.approvals.reserve(&f.reservation("denied")),
        Err(GateError::Limit(_))
    ));
    let before = f.info();
    let amount = f.amount(&r.id);
    let large = PermissionSet {
        ports: (0..4500)
            .map(|i| format!("local.{}.tool{i:05}@1.0", "p".repeat(80)))
            .collect(),
        ..PermissionSet::default()
    };
    assert!(matches!(
        f.approvals
            .set_permissions(&f.profile, &pack, &hash, &large),
        Err(GateError::Limit(_))
    ));
    assert!(matches!(
        f.approvals.commit_permission_effect(
            &r,
            r.expires_unix_ms,
            PermissionEffect::Pack {
                pack: &pack,
                hash: &hash,
                permissions: &large
            }
        ),
        Err(GateError::Limit(_))
    ));
    assert_eq!(f.amount(&r.id), amount);
    assert_eq!(f.info().total, before.total);
    assert!(
        f.approvals
            .permissions(&f.profile, &pack, &hash)
            .unwrap()
            .is_none()
    );
    assert_eq!(
        f.approvals.reservation_state(&r.id).unwrap().as_deref(),
        Some("dispatched")
    );
    // A failed general mutation did not take the already admitted cleanup slot.
    f.approvals.abandon_terminal(&r.id).unwrap();
    f.ack(&f.approvals, &r.id);
    for row in &rows {
        f.approvals.abandon_terminal(&row.id).unwrap();
        f.ack(&f.approvals, &row.id);
    }
    assert!(f.info().total < LIMIT);
}

#[test]
fn sqlite_main_page_limit_stops_a_temporary_oversized_ordinary_write() {
    let f = Fixture::new();
    let r = f.consumed("physical-cap");
    let amount = f.amount(&r.id);
    let before = f.info();
    let result = f.approvals.ordinary(|tx| {
        tx.execute(
            "INSERT INTO permissions(pack,hash,body) VALUES('local.toobig','test',zeroblob(?1))",
            [MAX_TERMINAL_BYTES],
        )
        .map_err(persistence)?;
        Ok(())
    });
    assert!(result.is_err());
    assert_eq!(f.info().bytes, before.bytes);
    assert!(f.info().bytes <= LIMIT);
    assert_eq!(f.amount(&r.id), amount);
    f.approvals
        .finish_terminal(&r.id, &Outcome::Succeeded)
        .unwrap();
    f.ack(&f.approvals, &r.id);
}

#[test]
fn legacy_full_active_and_undelivered_migrate_without_refill_or_new_authority() {
    let f = Fixture::new();
    let r = f.consumed("old-terminal");
    let original = f
        .approvals
        .finish_terminal(&r.id, &Outcome::Succeeded)
        .unwrap()
        .decision;
    f.remove_accounting_for_old_schema();
    let raw = f.raw();
    // The old formula accepted 387 combined entries at 4096-byte pages.
    let mut old = Vec::new();
    for index in 0..386 {
        let r = f.reservation(&format!("old-active{index}"));
        raw.execute(
            "INSERT INTO reservations(id,body,state,expires) VALUES(?1,?2,'pending',?3)",
            params![
                r.id.as_str(),
                crate::canonical(&r).unwrap(),
                i64::try_from(r.expires_unix_ms).unwrap()
            ],
        )
        .unwrap();
        old.push(r);
    }
    let migrated = super::super::Approvals::open(&f.root, &f.profile).unwrap();
    let debt = snapshot(&migrated.connection.lock().unwrap()).unwrap();
    assert!(!debt.debt_closed);
    assert!(debt.total > LIMIT && debt.bytes < LIMIT);
    assert!(matches!(
        migrated.reserve(&f.reservation("new-in-debt")),
        Err(GateError::Limit(_))
    ));
    assert!(migrated.pair_device(&f.profile, "phone", [1; 32]).is_err());
    let second = super::super::Approvals::open(&f.root, &f.profile).unwrap();
    let reopened = snapshot(&second.connection.lock().unwrap()).unwrap();
    assert_eq!(reopened.total, debt.total);
    assert_eq!(reopened.ceiling, debt.ceiling);
    assert_eq!(
        migrated
            .terminal_delivery(&r.id)
            .unwrap()
            .unwrap()
            .decision
            .event_id,
        original.event_id
    );
    f.ack(&migrated, &r.id);
    let mut old_remaining = old.iter();
    loop {
        if snapshot(&migrated.connection.lock().unwrap())
            .unwrap()
            .debt_closed
        {
            break;
        }
        let row = old_remaining.next().unwrap();
        migrated.abandon_terminal(&row.id).unwrap();
        f.ack(&migrated, &row.id);
    }
    assert!(
        snapshot(&migrated.connection.lock().unwrap())
            .unwrap()
            .debt_closed
    );
    assert!(
        raw.execute("UPDATE terminal_capacity_state SET debt_closed=0", [])
            .is_err()
    );
    assert!(
        raw.execute(
            "UPDATE terminal_capacity_state SET legacy_ceiling=x'FFFFFFFFFFFFFFFF'",
            []
        )
        .is_err()
    );
    // Repaying inherited over-reservation is not a new full slot. Existing
    // obligations still own their remaining bytes until verified settlement.
    loop {
        let info = snapshot(&migrated.connection.lock().unwrap()).unwrap();
        let full = slot(page_size(&raw).unwrap()).unwrap();
        if info.total + full <= LIMIT {
            break;
        }
        assert!(matches!(
            migrated.reserve(&f.reservation("new-after-debt")),
            Err(GateError::Limit(_))
        ));
        assert_eq!(
            snapshot(&migrated.connection.lock().unwrap())
                .unwrap()
                .total,
            info.total
        );
        let row = old_remaining.next().unwrap();
        migrated.abandon_terminal(&row.id).unwrap();
        f.ack(&migrated, &row.id);
    }
    migrated.reserve(&f.reservation("new-after-debt")).unwrap();
    let before_reopen = snapshot(&migrated.connection.lock().unwrap()).unwrap();
    let after = super::super::Approvals::open(&f.root, &f.profile).unwrap();
    let after_reopen = snapshot(&after.connection.lock().unwrap()).unwrap();
    assert!(after_reopen.debt_closed);
    assert_eq!(after_reopen.total, before_reopen.total);
}

#[test]
fn missing_marker_or_escrow_mapping_cannot_remint_on_reopen() {
    let f = Fixture::new();
    f.approvals.reserve(&f.reservation("mapping")).unwrap();
    let raw = f.raw();
    assert!(
        raw.execute("DELETE FROM terminal_capacity_state", [])
            .is_err()
    );
    raw.execute_batch(
        "DROP TRIGGER terminal_capacity_state_no_delete; DELETE FROM terminal_capacity_state;",
    )
    .unwrap();
    assert!(matches!(
        super::super::Approvals::open(&f.root, &f.profile),
        Err(crate::StoreError::Gate(GateError::Integrity(_)))
    ));
    let n: i64 = raw
        .query_row("SELECT COUNT(*) FROM terminal_capacity_state", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(n, 0);
    // Even losing both tables cannot discard the independent header marker.
    raw.execute_batch("DROP TABLE terminal_capacity_state; DROP TABLE terminal_capacity_escrow;")
        .unwrap();
    assert!(matches!(
        super::super::Approvals::open(&f.root, &f.profile),
        Err(crate::StoreError::Gate(GateError::Integrity(_)))
    ));
    let tables: i64 = raw.query_row("SELECT COUNT(*) FROM sqlite_schema WHERE name IN ('terminal_capacity_state','terminal_capacity_escrow')", [], |row| row.get(0)).unwrap();
    assert_eq!(tables, 0);

    let f = Fixture::new();
    let r = f.reservation("orphan");
    f.approvals.reserve(&r).unwrap();
    let raw = f.raw();
    raw.execute("DELETE FROM reservations WHERE id=?1", [r.id.as_str()])
        .unwrap();
    assert!(super::super::Approvals::open(&f.root, &f.profile).is_err());
}

#[test]
fn quarantine_many_records_uses_each_own_escrow_and_retains_corruption() {
    let f = Fixture::new();
    let raw = f.raw();
    let mut ids = Vec::new();
    for index in 0..380 {
        let r = f.reservation(&format!("quarantine{index}"));
        f.approvals.reserve(&r).unwrap();
        ids.push(r.id);
    }
    let valid = f.reservation("valid-due");
    f.approvals.reserve(&valid).unwrap();
    let before_corrupt = f.info();
    for id in &ids {
        // Same-width corruption must not free the reservation's overflow pages
        // and accidentally fund the quarantine from already-paid free pages.
        raw.execute(
            "UPDATE reservations SET body=zeroblob(length(body)) WHERE id=?1",
            [id.as_str()],
        )
        .unwrap();
    }
    let before = f.info();
    assert_eq!(before_corrupt.bytes, before.bytes);
    let own_before: u64 = ids.iter().map(|id| f.amount(id)).sum();
    let decisions = f
        .approvals
        .expire_terminal(&f.profile, valid.expires_unix_ms + 1)
        .unwrap();
    assert_eq!(decisions.len(), 1);
    assert_eq!(decisions[0].reservation.id, valid.id);
    assert!(matches!(
        f.approvals.admission_status(&f.profile).unwrap(),
        AdmissionStatus::IntegrityFault { .. }
    ));
    assert!(f.info().total <= before.total);
    let quarantined: i64 = raw
        .query_row("SELECT COUNT(*) FROM reservation_quarantine", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(quarantined, 380);
    let corrupt: i64 = raw.query_row("SELECT COUNT(*) FROM reservations WHERE substr(body,1,1)=x'00' AND length(body)>1 AND state='pending'", [], |row| row.get(0)).unwrap();
    assert_eq!(corrupt, 380);
    let own_after: u64 = ids.iter().map(|id| f.amount(id)).sum();
    assert!(own_before - own_after > FAULT_METADATA_PAGES as u64 * page_size(&raw).unwrap());
    assert!(f.info().bytes > before.bytes);
    for id in &ids {
        assert!(f.amount(id) > 0);
    }
    f.ack(&f.approvals, &valid.id);
    let before_reopen = f.info();
    let reopened = super::super::Approvals::open(&f.root, &f.profile).unwrap();
    assert_eq!(
        snapshot(&reopened.connection.lock().unwrap())
            .unwrap()
            .total,
        before_reopen.total
    );
    assert!(matches!(
        reopened.reserve(&f.reservation("after-fault")),
        Err(GateError::Integrity(_))
    ));
}

#[test]
fn fixed_width_budget_updates_are_monotone_and_include_state_index_growth() {
    let f = Fixture::new();
    let r = f.consumed("fixed");
    let raw = f.raw();
    let before = f.info();
    let amount = f.amount(&r.id);
    assert!(
        raw.execute(
            "UPDATE terminal_capacity_escrow SET remaining=x'FFFFFFFFFFFFFFFF' WHERE grant_id=?1",
            [r.id.as_str()]
        )
        .is_err()
    );
    assert!(
        raw.execute(
            "UPDATE terminal_capacity_escrow SET remaining=x'00' WHERE grant_id=?1",
            [r.id.as_str()]
        )
        .is_err()
    );
    assert!(
        raw.execute(
            "DELETE FROM terminal_capacity_escrow WHERE grant_id=?1",
            [r.id.as_str()]
        )
        .is_err()
    );
    f.approvals
        .finish_terminal(
            &r.id,
            &Outcome::Failed {
                code: "x".repeat(128),
            },
        )
        .unwrap();
    let after = f.info();
    assert!(f.amount(&r.id) <= amount);
    assert_eq!(before.total, after.total);
    let width: i64 = raw
        .query_row(
            "SELECT length(remaining) FROM terminal_capacity_escrow WHERE grant_id=?1",
            [r.id.as_str()],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(width, 8);
    f.ack(&f.approvals, &r.id);
    assert!(f.info().total <= after.total);
    assert!(
        raw.execute(
            "UPDATE terminal_capacity_escrow SET state='active' WHERE grant_id=?1",
            [r.id.as_str()]
        )
        .is_err()
    );
}

fn maximum_terminal_reservation(f: &Fixture, label: &str) -> Reservation {
    use tobkiri_gate::{TerminalDecision, TerminalDisposition};
    let mut r = f.reservation(label);
    r.original_expires_unix_ms = Some(r.expires_unix_ms);
    // The old trusted-reservation fixture API allows variable grant IDs.
    // Tune that bounded fixture to the exact admitted canonical terminal cap;
    // this does not create a sealed Gate authority or an actual Core receipt.
    for revision in [None, Some(Digest::hash(b"max terminal revision"))] {
        r.revision = revision;
        r.id = serde_json::from_value(serde_json::Value::String("grant_x".into())).unwrap();
        let decision = TerminalDecision::new(
            r.clone(),
            Outcome::Failed {
                code: "x".repeat(128),
            },
            TerminalDisposition::ExpiredCleanupUnknown,
        )
        .unwrap();
        let length = crate::canonical(&decision).unwrap().len();
        let available = MAX_TERMINAL_ENTRY_BYTES as usize - length;
        if available % 2 != 0 {
            continue;
        }
        r.id = serde_json::from_value(serde_json::Value::String(format!(
            "grant_x{}",
            "x".repeat(available / 2)
        )))
        .unwrap();
        let decision = TerminalDecision::new(
            r.clone(),
            Outcome::Failed {
                code: "x".repeat(128),
            },
            TerminalDisposition::ExpiredCleanupUnknown,
        )
        .unwrap();
        assert_eq!(
            crate::canonical(&decision).unwrap().len(),
            MAX_TERMINAL_ENTRY_BYTES as usize
        );
        return r;
    }
    panic!("unable to construct exact canonical boundary fixture");
}

fn terminalize_maximum(store: &super::super::Approvals, r: Reservation) {
    let mut conn = store.connection.lock().unwrap();
    let tx = conn
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .unwrap();
    let decision = super::super::Approvals::terminalize(
        &tx,
        r,
        Outcome::Failed {
            code: "x".repeat(128),
        },
        tobkiri_gate::TerminalDisposition::ExpiredCleanupUnknown,
    )
    .unwrap();
    assert_eq!(
        crate::canonical(&decision).unwrap().len(),
        MAX_TERMINAL_ENTRY_BYTES as usize
    );
    tx.commit().unwrap();
}

#[test]
fn maximum_4096_byte_terminal_and_ack_survive_closed_new_admission() {
    let f = Fixture::new();
    let r = maximum_terminal_reservation(&f, "maximum");
    f.approvals.reserve(&r).unwrap();
    f.approvals.dispatch(&r.id).unwrap();
    let r = f
        .approvals
        .consume_with_deadline(&r, r.expires_unix_ms)
        .unwrap();
    let _pending = f.fill_active();
    assert!(matches!(
        f.approvals.reserve(&f.reservation("after-fuse")),
        Err(GateError::Limit(_))
    ));
    let id = r.id.clone();
    terminalize_maximum(&f.approvals, r);
    f.ack(&f.approvals, &id);
    assert_eq!(f.amount(&id), 0);
    assert!(f.info().total <= LIMIT);
}

#[test]
fn minimum_and_maximum_page_sizes_keep_full_original_escrow_and_terminal_bytes() {
    for size in [512u64, 65536] {
        let f = Fixture::new();
        f.remove_accounting_for_old_schema();
        f.raw()
            .execute_batch(&format!("PRAGMA page_size={size}; VACUUM;"))
            .unwrap();
        let reopened = super::super::Approvals::open(&f.root, &f.profile).unwrap();
        assert_eq!(
            page_size(&reopened.connection.lock().unwrap()).unwrap(),
            size
        );
        let r = maximum_terminal_reservation(&f, "page-boundary");
        let before = snapshot(&reopened.connection.lock().unwrap()).unwrap();
        reopened.reserve(&r).unwrap();
        let admitted = snapshot(&reopened.connection.lock().unwrap()).unwrap();
        assert_eq!(admitted.total - before.total, slot(size).unwrap());
        reopened.dispatch(&r.id).unwrap();
        let r = reopened
            .consume_with_deadline(&r, r.expires_unix_ms)
            .unwrap();
        let id = r.id.clone();
        terminalize_maximum(&reopened, r);
        f.ack(&reopened, &id);
        assert!(
            snapshot(&reopened.connection.lock().unwrap())
                .unwrap()
                .total
                <= LIMIT
        );
    }
}

#[test]
fn reopen_schema_growth_cannot_spend_a_legacy_debt_ceiling_gap() {
    let f = Fixture::new();
    f.remove_accounting_for_old_schema();
    let raw = f.raw();
    for index in 0..387 {
        let r = f.reservation(&format!("schema-legacy{index}"));
        raw.execute(
            "INSERT INTO reservations(id,body,state,expires) VALUES(?1,?2,'pending',?3)",
            params![
                r.id.as_str(),
                crate::canonical(&r).unwrap(),
                i64::try_from(r.expires_unix_ms).unwrap()
            ],
        )
        .unwrap();
    }
    let reopened = super::super::Approvals::open(&f.root, &f.profile).unwrap();
    reopened
        .latch_integrity(&f.profile, IntegrityFaultCode::ReservationRecord)
        .unwrap();
    let start = snapshot(&reopened.connection.lock().unwrap()).unwrap();
    assert!(!start.debt_closed && start.total > LIMIT && start.total < start.ceiling);
    let mut conn = reopened.connection.lock().unwrap();
    let tx = conn
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .unwrap();
    let before = reopen_start(&tx).unwrap();
    tx.execute_batch("CREATE TABLE simulated_schema_repair(body BLOB);")
        .unwrap();
    while database_bytes(&tx).unwrap() == start.bytes {
        tx.execute(
            "INSERT INTO simulated_schema_repair(body) VALUES(zeroblob(4096))",
            [],
        )
        .unwrap();
    }
    let grown = snapshot(&tx).unwrap();
    assert!(grown.total > start.total && grown.total <= start.ceiling);
    // Comparing only against a fresh post-DDL snapshot would accept this.
    initialize(&tx).unwrap();
    assert!(matches!(
        reopen_finish(&tx, before),
        Err(GateError::Limit(_))
    ));
    drop(tx);
    drop(conn);
    assert_eq!(
        snapshot(&reopened.connection.lock().unwrap())
            .unwrap()
            .total,
        start.total
    );
}
