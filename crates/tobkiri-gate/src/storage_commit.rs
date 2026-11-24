//! Trusted native Store bridge; deliberately absent from guest WIT and IPC.
use crate::{DispatchPermit, EffectRequest, GateError, StoreOperation};
use tobkiri_canon::Digest;

/// The fixed native Store adapter must retain the actual connection claim,
/// target and transaction and derive these bindings from that same operation.
/// Implementing this native trait is not authentication. Only the sealed
/// consumed permit and its original authority owner can admit a commit.
/// No guest/port invocation, Gate reentry, notification or Join is allowed here.
pub trait NativeStorageCommit {
    fn effect(&self) -> &EffectRequest;
    fn args_digest(&self) -> &Digest;
    fn commit(&mut self) -> Result<(), GateError>;
}

impl DispatchPermit {
    /// Commit this exact native DATA operation at most once, while its original
    /// durable authority excludes epoch writers. This is not a new grant or a
    /// generic callback utility; Core Store must bind the actual transaction.
    pub fn commit_storage(&self, operation: &mut dyn NativeStorageCommit) -> Result<(), GateError> {
        let supported = matches!(
            self.effect(),
            EffectRequest::Storage {
                operation: StoreOperation::Write
                    | StoreOperation::Delete
                    | StoreOperation::SqlWrite,
                ..
            } | EffectRequest::UserGlobalStorage {
                operation: StoreOperation::Write
                    | StoreOperation::Delete
                    | StoreOperation::SqlWrite,
                ..
            }
        );
        if !supported
            || self.effect() != operation.effect()
            || self.args_digest() != operation.args_digest()
        {
            return Err(GateError::Denied(
                "native storage commit binding differs".into(),
            ));
        }
        self._consumption_owner
            .commit_storage(&self.reservation, self.deadline, operation)
    }
}
