//! Read-only durable exclusion around one separately owned native DATA commit.
use super::*;

struct RestoreBusy<'a> {
    approvals: &'a Approvals,
    connection: &'a rusqlite::Connection,
    previous_ms: u64,
    restored: bool,
}
impl RestoreBusy<'_> {
    fn restore(mut self) -> Result<(), GateError> {
        let result = self
            .connection
            .busy_timeout(std::time::Duration::from_millis(self.previous_ms));
        self.restored = result.is_ok();
        if result.is_err() {
            self.approvals
                .connection_fault
                .store(true, Ordering::Release);
        }
        result.map_err(persistence)
    }
}
impl Drop for RestoreBusy<'_> {
    fn drop(&mut self) {
        if !self.restored
            && self
                .connection
                .busy_timeout(std::time::Duration::from_millis(self.previous_ms))
                .is_err()
        {
            self.approvals
                .connection_fault
                .store(true, Ordering::Release);
        }
    }
}
struct UnwindFault<'a> {
    approvals: &'a Approvals,
    finished: bool,
}
impl Drop for UnwindFault<'_> {
    fn drop(&mut self) {
        if !self.finished {
            self.approvals
                .connection_fault
                .store(true, Ordering::Release);
        }
    }
}

pub(super) fn commit_fenced(
    approvals: &Approvals,
    expected: &Reservation,
    commit: &mut dyn FnMut() -> Result<(), GateError>,
) -> Result<(), GateError> {
    approvals.local(&expected.profile)?;
    validate_reservation_identity(expected)?;
    if expected.original_expires_unix_ms.is_none() {
        return Err(GateError::Consumed);
    }
    // DATA's exact connection claim precedes this lock. Never wait in the
    // inverse direction; a busy authority is a pre-commit availability refusal.
    let conn = approvals
        .connection
        .try_lock()
        .map_err(|_| GateError::Unavailable)?;
    if approvals.connection_fault.load(Ordering::Acquire) {
        return Err(GateError::Unavailable);
    }
    let previous_ms = conn
        .pragma_query_value(None, "busy_timeout", |r| crate::sql_u64(r, 0))
        .map_err(persistence)?;
    let busy = RestoreBusy {
        approvals,
        connection: &conn,
        previous_ms,
        restored: false,
    };
    if let Err(error) = conn.busy_timeout(std::time::Duration::ZERO) {
        approvals.connection_fault.store(true, Ordering::Release);
        let _ = busy.restore();
        return Err(persistence(error));
    }
    let mut unwind = UnwindFault {
        approvals,
        finished: false,
    };
    let result = (|| {
        // This actual authority DB lock also excludes other connections and
        // processes advancing the epoch or committing a Profile decision.
        let tx =
            rusqlite::Transaction::new_unchecked(&conn, rusqlite::TransactionBehavior::Immediate)
                .map_err(persistence)?;
        let result = (|| {
            match admitted_status(&tx)? {
                AdmissionStatus::Allowed => (),
                AdmissionStatus::IntegrityFault { code } => return Err(GateError::Integrity(code)),
            }
            let epoch = tx
                .query_row(
                    "SELECT epoch FROM security_epoch WHERE singleton=1",
                    [],
                    |r| crate::sql_u64(r, 0),
                )
                .map_err(persistence)?;
            if epoch != expected.epoch {
                return Err(GateError::StaleEpoch);
            }
            if Approvals::terminal(&tx, &approvals.profile, &expected.id)?.is_some() {
                return Err(GateError::Consumed);
            }
            let (body, expires): (Vec<u8>, u64) = tx.query_row(
                "SELECT CASE WHEN length(body)<=4096 THEN body ELSE x'' END,expires FROM reservations WHERE id=?1 AND state='consumed'",
                [expected.id.as_str()], |r| Ok((r.get(0)?, crate::sql_u64(r, 1)?)),
            ).optional().map_err(persistence)?.ok_or(GateError::Consumed)?;
            let actual = validated_reservation(&approvals.profile, expected.id.as_str(), &body)?;
            if actual.expires_unix_ms != expires {
                return Err(GateError::Integrity(IntegrityFaultCode::ReservationRecord));
            }
            cleanup_binding(&actual, expected)?;
            if now_ms().map_err(persistence)? >= actual.expires_unix_ms {
                return Err(GateError::Deadline);
            }
            commit()
        })();
        // No authority mutation was made. DATA may already be committed;
        // rollback/restore failure cannot turn its effect into "no write".
        let released = tx.rollback().map_err(persistence);
        if released.is_err() {
            approvals.connection_fault.store(true, Ordering::Release);
        }
        result.and(released)
    })();
    unwind.finished = true;
    result.and(busy.restore())
}
