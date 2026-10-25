//! SQL half of the Profile save: one durable decision and immutable exact
//! membership. No filesystem locks/recovery are acquired from a SQL transaction.
use super::*;

#[cfg(unix)]
impl Approvals {
    fn durable_save_connection(conn: &rusqlite::Connection) -> Result<(), StoreError> {
        // DELETE journals require directory synchronization for durability:
        // https://www.sqlite.org/pragma.html#pragma_synchronous
        conn.pragma_update(None, "synchronous", "EXTRA")?;
        let synchronous: i64 = conn.pragma_query_value(None, "synchronous", |row| row.get(0))?;
        let journal: String = conn.pragma_query_value(None, "journal_mode", |row| row.get(0))?;
        if synchronous != 3 || !journal.eq_ignore_ascii_case("delete") {
            return Err(StoreError::Unavailable(
                "durable Profile save primitives unavailable".into(),
            ));
        }
        Ok(())
    }

    pub(super) fn checked_save<T>(&self, result: Result<T, StoreError>) -> Result<T, StoreError> {
        let result = result.map_err(|error| match error {
            StoreError::Gate(GateError::Integrity(code)) => StoreError::Integrity(code),
            error => error,
        });
        fn integrity(error: &StoreError) -> Option<IntegrityFaultCode> {
            match error {
                StoreError::Integrity(code) | StoreError::Gate(GateError::Integrity(code)) => {
                    Some(*code)
                }
                StoreError::ProfileCommitUncertain { source, .. } => integrity(source),
                _ => None,
            }
        }
        if let Some(code) = result.as_ref().err().and_then(integrity) {
            self.latch_integrity(&self.profile, code)?;
        }
        result
    }

    pub(super) fn decide_profile_save(
        &self,
        batch: &PermissionApprovalBatch<'_, '_>,
        previous: Option<&Digest>,
        revision: &Digest,
    ) -> Result<Decision, StoreError> {
        use sha2::{Digest as _, Sha256};
        batch.check_active()?;
        let first = batch
            .iter()
            .next()
            .ok_or_else(|| StoreError::Invalid("empty save".into()))?;
        self.local(&first.reservation.profile)?;
        let from_epoch = first.reservation.epoch;
        let epoch = from_epoch
            .checked_add(1)
            .filter(|value| *value <= i64::MAX as u64)
            .ok_or_else(|| StoreError::Invalid("security epoch exhausted".into()))?;
        let plan = first
            .reservation
            .revision
            .as_ref()
            .ok_or_else(|| StoreError::Invalid("save requires a compiled revision".into()))?;
        let mut user = None;
        for item in batch.iter() {
            if let PermissionEffect::User {
                profile_hash,
                revision: approved_plan,
                epoch: approved_epoch,
                ..
            } = item.effect
            {
                if user.is_some()
                    || profile_hash != revision
                    || approved_plan != plan
                    || approved_epoch != epoch
                {
                    return Err(StoreError::Invalid("save User approval differs".into()));
                }
                user = Some(item.reservation);
            }
        }
        let user = user.ok_or_else(|| StoreError::Invalid("save requires User approval".into()))?;
        let mut decision = Decision {
            format: FORMAT.into(),
            profile: self.profile.clone(),
            operation: user.id.clone(),
            request: user.request.clone(),
            previous: previous.cloned(),
            revision: revision.clone(),
            plan: plan.clone(),
            from_epoch,
            epoch,
            batch: Digest::hash(b"uncommitted"),
            members: 0,
        };
        let mut conn = self.connection.lock().map_err(persistence)?;
        Self::durable_save_connection(&conn)?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        batch.check_active()?;
        deny_new_authority(&tx)?;
        let count: i64 =
            tx.query_row("SELECT COUNT(*) FROM profile_save_decisions", [], |row| {
                row.get(0)
            })?;
        if count >= MAX_DECISIONS {
            return Err(StoreError::Quota);
        }
        let mut identity = Sha256::new();
        let mut earliest = u64::MAX;
        for item in batch.iter() {
            batch.check_active()?;
            let terminal = self.permission_effect_in_transaction(
                &tx,
                item.reservation,
                item.expires_unix_ms,
                item.effect,
                false,
            )?;
            if terminal.decision.outcome != Outcome::Succeeded {
                return Err(GateError::Deadline.into());
            }
            earliest = earliest.min(terminal.decision.reservation.expires_unix_ms);
            tx.execute(
                "INSERT INTO profile_save_members(operation,ordinal,grant_id) VALUES(?1,?2,?3)",
                params![
                    decision.operation.as_str(),
                    decision.members,
                    item.reservation.id.as_str()
                ],
            )?;
            decision.members = decision
                .members
                .checked_add(1)
                .filter(|count| *count <= 4096)
                .ok_or(StoreError::Quota)?;
            let effect: String = tx.query_row(
                "SELECT digest FROM permission_effects WHERE grant_id=?1",
                [item.reservation.id.as_str()],
                |row| row.get(0),
            )?;
            let effect = Digest::parse(&effect)
                .map_err(|_| StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase))?;
            let binding = canonical(&(&terminal.decision.reservation, effect))?;
            identity.update((binding.len() as u64).to_be_bytes());
            identity.update(&binding);
        }
        decision.batch = Digest::parse(&format!("sha256:{}", hex::encode(identity.finalize())))
            .map_err(|_| StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase))?;
        batch.check_active()?;
        if earliest <= now_ms()? {
            return Err(GateError::Deadline.into());
        }
        let changed = tx.execute(
            "UPDATE security_epoch SET epoch=?1 WHERE singleton=1 AND epoch=?2",
            params![epoch as i64, from_epoch as i64],
        )?;
        if changed != 1 {
            return Err(GateError::StaleEpoch.into());
        }
        tx.execute(
            "INSERT INTO profile_save_decisions(operation,body) VALUES(?1,?2)",
            params![decision.operation.as_str(), canonical(&decision)?],
        )?;
        // Metadata writes and triggers can observe cancellation too. Select
        // the decision only after all SQL mutations, immediately before COMMIT.
        batch.check_active()?;
        if earliest <= now_ms()? {
            return Err(GateError::Deadline.into());
        }
        tx.commit().map_err(|error| {
            uncertain(
                &decision,
                crate::ProfileCommitProgress::EpochCommitAttempted,
                error.into(),
            )
        })?;
        Ok(decision)
    }

    pub(super) fn save_decision(
        &self,
        operation: Option<&GrantId>,
    ) -> Result<Option<(Decision, bool)>, StoreError> {
        let mut conn = self.connection.lock().map_err(persistence)?;
        let tx = conn.transaction()?;
        deny_faulted(&tx)?;
        let raw: Option<(String, Vec<u8>, bool)> = tx
            .query_row(
                "SELECT CASE WHEN length(CAST(operation AS BLOB))<=128 THEN operation ELSE '' END,CASE WHEN length(CAST(body AS BLOB))<=2048 THEN body ELSE x'' END,settled
             FROM profile_save_decisions WHERE (?1 IS NULL AND settled=0) OR operation=?1 LIMIT 1",
                [operation.map(GrantId::as_str)],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        let result = raw
            .map(|(id, body, settled)| {
                let d: Decision = decode(&body)
                    .map_err(|_| StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase))?;
                if d.format != FORMAT
                    || d.profile != self.profile
                    || d.operation.as_str() != id
                    || d.from_epoch.checked_add(1) != Some(d.epoch)
                    || d.epoch > i64::MAX as u64
                    || d.members == 0
                    || d.members > 4096
                {
                    return Err(StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase));
                }
                let terminal = Self::terminal(&tx, &self.profile, &d.operation)?
                    .ok_or(StoreError::Integrity(IntegrityFaultCode::TerminalRecord))?;
                let r = &terminal.decision.reservation;
                let effect = tobkiri_gate::EffectRequest::UserPermissions {
                    profile_hash: d.revision.clone(),
                    revision: d.plan.clone(),
                    epoch: d.epoch,
                };
                if terminal.decision.outcome != Outcome::Succeeded
                    || r.epoch != d.from_epoch
                    || r.request != d.request
                    || r.revision.as_ref() != Some(&d.plan)
                    || !matches!(r.principal, tobkiri_gate::Principal::User)
                    || r.effect != effect.digest()?
                {
                    return Err(StoreError::Integrity(IntegrityFaultCode::TerminalRecord));
                }
                self.validate_save_members(&tx, &d)?;
                let epoch: u64 = tx.query_row(
                    "SELECT epoch FROM security_epoch WHERE singleton=1",
                    [],
                    |row| crate::sql_u64(row, 0),
                )?;
                if epoch < d.epoch {
                    return Err(StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase));
                }
                Ok((d, settled))
            })
            .transpose()?;
        tx.commit()?;
        Ok(result)
    }

    fn validate_save_members(
        &self,
        tx: &rusqlite::Transaction<'_>,
        d: &Decision,
    ) -> Result<(), StoreError> {
        use sha2::{Digest as _, Sha256};
        let mut statement = tx.prepare(
            "SELECT ordinal,CASE WHEN length(CAST(grant_id AS BLOB))<=128 THEN grant_id ELSE '' END
             FROM profile_save_members WHERE operation=?1 ORDER BY ordinal LIMIT 4097",
        )?;
        let mut rows = statement.query([d.operation.as_str()])?;
        let mut count = 0_u32;
        let mut user_found = false;
        let mut identity = Sha256::new();
        while let Some(row) = rows.next()? {
            let ordinal: i64 = row.get(0)?;
            let id: String = row.get(1)?;
            if ordinal != i64::from(count) || count >= d.members || id.is_empty() {
                return Err(StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase));
            }
            let terminal = Self::terminal_by_id(tx, &self.profile, &id)?
                .ok_or(StoreError::Integrity(IntegrityFaultCode::TerminalRecord))?;
            let r = &terminal.decision.reservation;
            if terminal.decision.outcome != Outcome::Succeeded
                || terminal.decision.disposition != TerminalDisposition::Completed
                || r.epoch != d.from_epoch
                || r.request != d.request
                || r.revision.as_ref() != Some(&d.plan)
                || !matches!(r.principal, tobkiri_gate::Principal::User)
            {
                return Err(StoreError::Integrity(IntegrityFaultCode::TerminalRecord));
            }
            user_found |= r.id == d.operation;
            let effect: Option<String> = tx
                .query_row(
                    "SELECT CASE WHEN length(CAST(e.digest AS BLOB))<=71 THEN e.digest ELSE '' END
                 FROM permission_effects e JOIN terminal_outbox t ON t.grant_id=e.grant_id
                 WHERE e.grant_id=?1 AND t.quarantined=0",
                    [&id],
                    |row| row.get(0),
                )
                .optional()?;
            let effect =
                effect.ok_or(StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase))?;
            let effect = Digest::parse(&effect)
                .map_err(|_| StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase))?;
            let binding = canonical(&(r, effect))?;
            identity.update((binding.len() as u64).to_be_bytes());
            identity.update(&binding);
            count += 1;
        }
        if count != d.members
            || !user_found
            || format!("sha256:{}", hex::encode(identity.finalize())) != d.batch.as_str()
        {
            return Err(StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase));
        }
        Ok(())
    }

    pub(super) fn settle_profile_save(&self, d: &Decision) -> Result<(), StoreError> {
        let mut conn = self.connection.lock().map_err(persistence)?;
        Self::durable_save_connection(&conn)?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        deny_faulted(&tx)?;
        let epoch: u64 = tx.query_row(
            "SELECT epoch FROM security_epoch WHERE singleton=1",
            [],
            |row| crate::sql_u64(row, 0),
        )?;
        // Later revocation remains authoritative; never restore an older epoch.
        if epoch < d.epoch {
            return Err(StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase));
        }
        let changed = tx.execute("UPDATE profile_save_decisions SET settled=1 WHERE operation=?1 AND body=?2 AND settled=0", params![d.operation.as_str(),canonical(d)?])?;
        if changed != 1 {
            return Err(StoreError::Conflict("save decision changed".into()));
        }
        tx.commit()?;
        Ok(())
    }
}
