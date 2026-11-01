//! One permission protocol, with explicit single-effect or atomic-batch
//! transaction ownership. This does not coordinate the Profile head/epoch.
use super::*;

impl Approvals {
    pub(super) fn permission_effect_in_transaction(
        &self,
        tx: &rusqlite::Transaction<'_>,
        r: &Reservation,
        deadline: u64,
        effect: PermissionEffect<'_>,
        allow_existing: bool,
    ) -> Result<TerminalDelivery, GateError> {
        self.local(&r.profile)?;
        if effect.request().digest()? != r.effect {
            return Err(GateError::Denied(
                "permission effect binding mismatch".into(),
            ));
        }
        let digest = Digest::hash(&canonical(&effect).map_err(persistence)?);
        if let Some(existing) = Self::terminal(tx, &self.profile, &r.id)? {
            if !allow_existing {
                return Err(GateError::Consumed);
            }
            if !same_reservation_authority(&existing.decision.reservation, r)? {
                return Err(GateError::Denied(
                    "permission reservation binding mismatch".into(),
                ));
            }
            if existing.decision.outcome == Outcome::Succeeded {
                let retained: String = tx
                    .query_row(
                        "SELECT digest FROM permission_effects WHERE grant_id=?1",
                        [r.id.as_str()],
                        |row| row.get(0),
                    )
                    .map_err(persistence)?;
                if retained != digest.as_str() {
                    return Err(GateError::Denied("permission effect retry differs".into()));
                }
            }
            return Ok(existing); // Never replay the effect or replace an expiry winner.
        }
        deny_new_authority(tx)?;
        let body: Vec<u8> = tx
            .query_row(
                "SELECT body FROM reservations WHERE id=?1 AND state='dispatched'",
                [r.id.as_str()],
                |row| row.get(0),
            )
            .optional()
            .map_err(persistence)?
            .ok_or(GateError::Consumed)?;
        let mut current = validated_reservation(&self.profile, r.id.as_str(), &body)?;
        if canonical(&current).map_err(persistence)? != canonical(r).map_err(persistence)? {
            return Err(GateError::Denied(
                "permission reservation binding mismatch".into(),
            ));
        }
        let epoch: u64 = tx
            .query_row(
                "SELECT epoch FROM security_epoch WHERE singleton=1",
                [],
                |row| crate::sql_u64(row, 0),
            )
            .map_err(persistence)?;
        if epoch != current.epoch {
            return Err(GateError::StaleEpoch);
        }
        current.original_expires_unix_ms = Some(
            current
                .original_expires_unix_ms
                .unwrap_or(current.expires_unix_ms),
        );
        current.expires_unix_ms = current.expires_unix_ms.min(deadline);
        if current.expires_unix_ms <= now_ms().map_err(persistence)? {
            let decision = Self::terminalize(
                tx,
                current,
                Outcome::Failed {
                    code: "deadline-expired-before-use".into(),
                },
                TerminalDisposition::ExpiredBeforeUse,
            )?;
            return Ok(TerminalDelivery {
                decision,
                acknowledged: false,
            });
        }
        tx.execute("UPDATE reservations SET body=?2,expires=?3,state='consumed' WHERE id=?1 AND state='dispatched'",params![r.id.as_str(),canonical(&current).map_err(persistence)?,i64::try_from(current.expires_unix_ms).map_err(persistence)?]).map_err(persistence)?;
        // Candidate approvals do not advance the active revision epoch; the
        // committed Profile CAS performs that advancement under its lock.
        tx.execute_batch("SAVEPOINT permission_mutation")
            .map_err(persistence)?;
        match effect {
            PermissionEffect::Pack {
                pack,
                hash,
                permissions,
            } => {
                tx.execute("INSERT INTO permissions(pack,hash,body) VALUES(?1,?2,?3) ON CONFLICT(pack,hash) DO UPDATE SET body=excluded.body",params![pack.as_str(),hash.as_str(),canonical(permissions).map_err(persistence)?]).map_err(persistence)?;
            }
            PermissionEffect::User {
                profile_hash,
                revision,
                epoch,
                permissions,
            } => {
                tx.execute("INSERT INTO user_approvals(profile_hash,revision,epoch,body) VALUES(?1,?2,?3,?4) ON CONFLICT(profile_hash,revision,epoch) DO UPDATE SET body=excluded.body",params![profile_hash.as_str(),revision.as_str(),i64::try_from(epoch).map_err(persistence)?,canonical(permissions).map_err(persistence)?]).map_err(persistence)?;
            }
        }
        let due = current.expires_unix_ms <= now_ms().map_err(persistence)?;
        if due {
            tx.execute_batch("ROLLBACK TO permission_mutation; RELEASE permission_mutation")
                .map_err(persistence)?;
        } else {
            tx.execute(
                "INSERT INTO permission_effects(grant_id,digest) VALUES(?1,?2)",
                params![r.id.as_str(), digest.as_str()],
            )
            .map_err(persistence)?;
            tx.execute_batch("RELEASE permission_mutation")
                .map_err(persistence)?;
        }
        let decision = Self::terminalize(
            tx,
            current,
            if due {
                Outcome::Failed {
                    code: "deadline-expired-before-use".into(),
                }
            } else {
                Outcome::Succeeded
            },
            if due {
                TerminalDisposition::ExpiredBeforeUse
            } else {
                TerminalDisposition::Completed
            },
        )?;
        Ok(TerminalDelivery {
            decision,
            acknowledged: false,
        })
    }
}
