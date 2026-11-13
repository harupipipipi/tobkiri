use super::{Approvals, now_ms, persistence, validated_reservation};
use crate::{StoreError, canonical};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use tobkiri_canon::{Digest, ids::PackId};
use tobkiri_gate::{
    EffectRequest, GateError, GrantId, IntegrityFaultCode, PublicWriteApproval, PublicWritePrompt,
    Reservation, StoreTier,
};

pub(super) fn initialize(connection: &Connection) -> Result<(), StoreError> {
    if connection
        .prepare("SELECT public_write_prompt FROM reservations LIMIT 0")
        .is_err()
    {
        connection.execute_batch(
            "ALTER TABLE reservations ADD COLUMN public_write_prompt TEXT
                CHECK(public_write_prompt IS NULL OR length(public_write_prompt)=71);",
        )?;
    }
    connection.execute_batch(
        "CREATE INDEX IF NOT EXISTS pending_public_write_prompt
         ON reservations(public_write_prompt,expires)
         WHERE state='pending' AND public_write_prompt IS NOT NULL;",
    )?;
    Ok(())
}

fn pair_key(approval: &PublicWriteApproval) -> Result<Digest, GateError> {
    Ok(Digest::hash(
        &canonical(&(
            "tobkiri.public-write-prompt/1",
            &approval.profile,
            &approval.writer,
            &approval.owner,
            &approval.store,
        ))
        .map_err(persistence)?,
    ))
}

pub(super) fn prompt_key(
    reservation: &Reservation,
    approval: &PublicWriteApproval,
) -> Result<Digest, GateError> {
    let effect = EffectRequest::Storage {
        profile: approval.profile.clone(),
        owner: approval.owner.clone(),
        store: approval.store.clone(),
        tier: StoreTier::Public,
        operation: approval.operation,
    };
    if approval.profile != reservation.profile
        || !approval.operation.is_write()
        || reservation
            .principal
            .pack()
            .is_none_or(|(writer, _)| writer != &approval.writer)
        || effect.digest()? != reservation.effect
    {
        return Err(GateError::Denied("public writer binding mismatch".into()));
    }
    pair_key(approval)
}

pub(super) fn approved(
    connection: &Connection,
    writer: &PackId,
    owner: &PackId,
    store: &str,
) -> Result<bool, GateError> {
    connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM public_writers WHERE writer=?1 AND owner=?2 AND store=?3)",
            params![writer.as_str(), owner.as_str(), store],
            |row| row.get(0),
        )
        .map_err(persistence)
}

pub(super) fn approve(
    connection: &Connection,
    writer: &PackId,
    owner: &PackId,
    store: &str,
) -> Result<(), GateError> {
    connection
        .execute(
            "INSERT OR IGNORE INTO public_writers(writer,owner,store) VALUES(?1,?2,?3)",
            params![writer.as_str(), owner.as_str(), store],
        )
        .map_err(persistence)?;
    Ok(())
}

pub(super) fn claim_prompt(
    store: &Approvals,
    reservation: &Reservation,
    approval: &PublicWriteApproval,
) -> Result<PublicWritePrompt, GateError> {
    store.local(&approval.profile)?;
    let mut connection = store.lock_connection()?;
    if approved(
        &connection,
        &approval.writer,
        &approval.owner,
        &approval.store,
    )? {
        return Ok(PublicWritePrompt::Approved);
    }
    let key = prompt_key(reservation, approval)?;
    let tx = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(persistence)?;
    let now = now_ms().map_err(persistence)?;
    let (_, existing) = store.pending_dispatch(&tx, &reservation.id, Some(reservation), now)?;
    if existing.as_deref() == Some(key.as_str()) {
        return Ok(PublicWritePrompt::Prompt);
    }
    if existing.is_some() {
        return Err(GateError::Denied(
            "public write prompt claim mismatch".into(),
        ));
    }
    if approved(&tx, &approval.writer, &approval.owner, &approval.store)? {
        return Ok(PublicWritePrompt::Approved);
    }
    // The Profile-local database arbitrates claims across all its connections.
    // Keep an old-epoch claim until its prompt drops or its reservation expires.
    let leader: Option<(String, Vec<u8>, u64)> = tx
        .query_row(
            "SELECT id,body,expires FROM reservations WHERE state='pending' AND public_write_prompt=?1
             AND expires>?2 LIMIT 1",
            params![key.as_str(), i64::try_from(now).map_err(persistence)?],
            |row| Ok((row.get(0)?, row.get(1)?, crate::sql_u64(row, 2)?)),
        )
        .optional()
        .map_err(persistence)?;
    if let Some((id, body, expires)) = leader {
        let leader = validated_reservation(&store.profile, &id, &body)?;
        if leader.expires_unix_ms != expires {
            return Err(GateError::Integrity(IntegrityFaultCode::ReservationRecord));
        }
        return Ok(PublicWritePrompt::Wait(leader.id));
    }
    let changed = tx
        .execute(
            "UPDATE reservations SET public_write_prompt=?2
             WHERE id=?1 AND state='pending' AND public_write_prompt IS NULL",
            params![reservation.id.as_str(), key.as_str()],
        )
        .map_err(persistence)?;
    if changed != 1 {
        return Err(GateError::Consumed);
    }
    tx.commit().map_err(persistence)?;
    Ok(PublicWritePrompt::Prompt)
}

pub(super) fn check_prompt(
    store: &Approvals,
    reservation: &Reservation,
    leader: &GrantId,
    approval: &PublicWriteApproval,
) -> Result<bool, GateError> {
    store.local(&approval.profile)?;
    let key = pair_key(approval)?;
    let connection = store.lock_connection()?;
    let now = i64::try_from(now_ms().map_err(persistence)?).map_err(persistence)?;
    // Read these facts from one snapshot. A successful dispatch clears its
    // claim and records consent atomically, and an epoch change invalidates
    // the waiter's reservation even when the original prompt has already dropped.
    let (epoch, approved, pending): (u64, bool, bool) = connection
        .query_row(
            "SELECT epoch,
                    EXISTS(SELECT 1 FROM public_writers WHERE writer=?1 AND owner=?2 AND store=?3),
                    EXISTS(SELECT 1 FROM reservations WHERE id=?4 AND state='pending'
                           AND public_write_prompt=?5 AND expires>?6)
             FROM security_epoch WHERE singleton=1",
            params![
                approval.writer.as_str(),
                approval.owner.as_str(),
                approval.store,
                leader.as_str(),
                key.as_str(),
                now,
            ],
            |row| Ok((crate::sql_u64(row, 0)?, row.get(1)?, row.get(2)?)),
        )
        .map_err(persistence)?;
    if epoch != reservation.epoch {
        return Err(GateError::StaleEpoch);
    }
    if !approved && !pending {
        return Err(GateError::ApprovalRequired);
    }
    Ok(approved)
}
