//! The only native adapter for an already claimed physical DATA transaction.
use super::*;

pub(super) enum CommitAdmission<'a> {
    Consumed {
        permit: &'a DispatchPermit,
        effect: &'a EffectRequest,
        args: &'a Digest,
    },
    /// Existing raw SQLite formatter/rollback unit fixtures have no authority.
    /// This variant cannot exist in the production library or guest ABI.
    #[cfg(test)]
    RawReviewFixture,
}

impl<'a> CommitAdmission<'a> {
    pub(super) fn consumed(
        permit: &'a DispatchPermit,
        effect: &'a EffectRequest,
        args: &'a Digest,
    ) -> Self {
        Self::Consumed {
            permit,
            effect,
            args,
        }
    }

    pub(super) fn finish(
        &self,
        database: &NativeDatabase,
        connection: &rusqlite::Connection,
        finish: impl FnOnce() -> Result<(), StoreError>,
    ) -> Result<(), StoreError> {
        let (permit, effect, args) = match self {
            Self::Consumed {
                permit,
                effect,
                args,
            } => (permit, effect, args),
            #[cfg(test)]
            Self::RawReviewFixture => return finish(),
        };
        // The caller holds THIS database's native connection guard throughout
        // SAVEPOINT/SQL/result/commit (or the explicit transaction's claim).
        // Target/args come from the same resolved native operation, not JSON IDs.
        database.check_owner_live()?;
        if connection.is_autocommit() {
            return Err(StoreError::Conflict(
                "physical DATA transaction is missing".into(),
            ));
        }
        let mut adapter = BoundCommit {
            effect,
            args,
            finish: Some(finish),
            result: None,
            completed: false,
            _database: database,
            _connection: connection,
        };
        let fenced = permit.commit_storage(&mut adapter);
        match (adapter.result.take(), fenced) {
            (Some(Ok(())), Ok(())) => Ok(()),
            // Even known DATA success must not be delivered as no effect when
            // releasing/restoring the independent authority connection failed.
            (Some(Ok(())), Err(_)) => Err(StoreError::CommitUnknown),
            (Some(Err(error)), _) => Err(error),
            (None, Err(error)) => Err(error.into()),
            (None, Ok(())) => Err(StoreError::Unavailable(
                "storage fence skipped physical commit".into(),
            )),
        }
    }
}

/// Private construction binds the live physical connection/transaction. This
/// adapter never leaves Core Store and accepts no serialized transaction token.
struct BoundCommit<'a, F> {
    effect: &'a EffectRequest,
    args: &'a Digest,
    finish: Option<F>,
    result: Option<Result<(), StoreError>>,
    completed: bool,
    _database: &'a NativeDatabase,
    _connection: &'a rusqlite::Connection,
}
impl<F> Drop for BoundCommit<'_, F> {
    fn drop(&mut self) {
        if self.finish.is_none() && !self.completed {
            // A native unwind did not establish SQL certainty. Retain the
            // actual database custody and refuse subsequent connection use.
            self._database.quarantine();
        }
    }
}
impl<F: FnOnce() -> Result<(), StoreError>> tobkiri_gate::NativeStorageCommit
    for BoundCommit<'_, F>
{
    fn effect(&self) -> &EffectRequest {
        self.effect
    }
    fn args_digest(&self) -> &Digest {
        self.args
    }
    fn commit(&mut self) -> Result<(), tobkiri_gate::GateError> {
        let finish = self
            .finish
            .take()
            .ok_or(tobkiri_gate::GateError::Consumed)?;
        let result = finish();
        let failed = result.is_err();
        self.result = Some(result);
        self.completed = true;
        if failed {
            Err(tobkiri_gate::GateError::Unavailable)
        } else {
            Ok(())
        }
    }
}
