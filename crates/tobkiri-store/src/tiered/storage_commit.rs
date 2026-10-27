//! The only native adapter for an already claimed physical DATA transaction.
use super::*;

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use crate::storage_test_fixture::{Fixture, sql, sql_statement};
    use tobkiri_gate::Outcome;

    #[tokio::test]
    async fn same_consumed_permit_cannot_commit_a_second_actual_data_transaction() {
        let f = Fixture::new().await;
        let handle = f.store().await;
        for text in [
            "CREATE TABLE replay_count(value INTEGER NOT NULL)",
            "INSERT INTO replay_count(value) VALUES(0)",
        ] {
            sql(&handle, &f.cx, f.gate.clone(), text, false)
                .await
                .unwrap();
        }
        // A second execution of the same actual SQL must increment again.
        // No triggers or changes to the production SQLite limits are needed.
        let (statement, budget) = sql_statement("UPDATE replay_count SET value=value+1");
        let target = NativeSqlTarget::Profile(handle.clone());
        let tier = target.current_tier(&f.cx, StoreOperation::SqlRead).unwrap();
        let args = statement.value().digest_guest(&budget).unwrap();
        let read_grant = f
            .gate
            .authorize(
                &f.cx,
                target.request(tier, StoreOperation::SqlRead),
                args.clone(),
            )
            .await
            .unwrap();
        let params = owned_sql_params(&statement).unwrap();
        let policy = Some(crate::SqlResultPolicy::ForbidRows);
        let fingerprint =
            classify_typed_sql(&target, &f.cx, &statement, &params, policy, false).unwrap();
        assert!(!fingerprint.readonly);
        drop(read_grant);
        let effect = target.request(tier, StoreOperation::SqlWrite);
        let grant = f
            .gate
            .authorize(&f.cx, effect.clone(), args.clone())
            .await
            .unwrap();
        let permit = grant.consume(&f.cx, &effect, &args).unwrap();
        f.gate.watch_lease(&f.cx, &permit).unwrap();
        for replay in [false, true] {
            let result = execute_authorized_typed_sql(
                &target,
                TypedSqlExecution {
                    cx: &f.cx,
                    statement: &statement,
                    params: &params,
                    expected_fingerprint: &fingerprint,
                    admitted_tier: tier,
                    policy,
                    max_rows: statement.max_rows(),
                    output_limit: effective_output_limit(&f.cx, &permit).unwrap(),
                    output_budget: &budget,
                    deadline: permit.deadline(),
                    fuel_limit: permit.limits().fuel_per_call,
                    admission: CommitAdmission::consumed(&permit, &effect, &args),
                    in_transaction: false,
                },
            );
            if replay {
                assert!(matches!(
                    result,
                    Err(StoreError::Gate(tobkiri_gate::GateError::Consumed))
                ));
            } else {
                result.unwrap();
            }
        }
        {
            let connection = handle.native.try_lock_connection().unwrap();
            let count: i64 = connection
                .query_row("SELECT value FROM replay_count", [], |row| row.get(0))
                .unwrap();
            assert_eq!(
                count, 1,
                "a consumed permit produced another physical effect"
            );
            assert!(connection.is_autocommit());
        }
        f.gate
            .record_outcome(permit.id().clone(), Outcome::Succeeded)
            .await
            .unwrap();
    }
}

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
