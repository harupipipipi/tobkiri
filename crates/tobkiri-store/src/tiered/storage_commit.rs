//! The only native adapter for an already claimed physical DATA transaction.
use super::*;

pub(super) enum NativeTransactionCommit {
    Refused {
        error: StoreError,
        rolled_back: bool,
    },
    Attempted {
        result: Result<(), StoreError>,
        started: bool,
        succeeded: bool,
    },
}

/// No connection claim or authority guard can escape into the async caller.
pub(super) fn commit_transaction(
    target: &NativeSqlTarget,
    cx: &CallCx,
    permit: &DispatchPermit,
    effect: &EffectRequest,
    args: &Digest,
) -> NativeTransactionCommit {
    let connection = match target.native().try_lock_connection() {
        Ok(connection) => connection,
        Err(error) => {
            return NativeTransactionCommit::Attempted {
                result: Err(error),
                started: false,
                succeeded: false,
            };
        }
    };
    let mut started = false;
    let mut succeeded = false;
    let mut fence_entered = false;
    let result = CommitAdmission::consumed(permit, effect, args).finish(
        target.native(),
        &connection,
        || {
            fence_entered = true;
            with_progress_limit(
                target.native(),
                &connection,
                cx,
                permit.deadline(),
                permit.limits().fuel_per_call,
                || {
                    started = true;
                    with_internal_authorizer(target.native(), &connection, || {
                        connection.execute_batch("COMMIT;")?;
                        succeeded = connection.is_autocommit();
                        if !succeeded {
                            return Err(target.native().commit_unknown());
                        }
                        Ok(())
                    })
                },
            )
        },
    );
    if !fence_entered {
        let error = result
            .err()
            .unwrap_or_else(|| StoreError::Unavailable("storage fence skipped commit".into()));
        let rollback = rollback_native_transaction(target.native(), &connection);
        let rolled_back = rollback.is_ok();
        if rolled_back {
            target
                .native()
                .active_transaction
                .store(false, Ordering::Release);
        }
        return NativeTransactionCommit::Refused {
            error: rollback.err().unwrap_or(error),
            rolled_back,
        };
    }
    NativeTransactionCommit::Attempted {
        result,
        started,
        succeeded,
    }
}

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
