//! All DATA uses one injected bounded engine. Native SQLite is reserved for
//! genuine authority/catalog metadata; a fixed KV statement is still DATA.
use crate::{SqlBinding, SqlConnection, SqlVfs, StoreDescriptor, StoreError};
use async_trait::async_trait;
use std::sync::Arc;
use tobkiri_canon::ids::{PackHash, ProfileId};
use tobkiri_canon::{AllocationBudget, ControlHandle, CoreRef, CoreValue, Digest};
use tobkiri_gate::{AllocationFrameScope, CallCx, DispatchPermit, StoreOperation};

/// Immutable verified manifest/catalog identity. This descriptor's shared
/// control retains its real compile/load reservation; it is not guest JSON.
#[derive(Clone, Debug)]
pub struct DataBinding {
    pub authority: SqlBinding,
    pub storage_profile: ProfileId,
    pub owner_hash: PackHash,
    pub descriptor: StoreDescriptor,
}
/// Main, rollback journal and temporary disk are separate from linear memory.
/// Absent bounds deny. The broker must bound the aggregate before any write.
#[derive(Clone, Copy, Debug, Default)]
pub struct DataDiskLimits {
    pub main_bytes: u64,
    pub journal_bytes: u64,
    pub temporary_bytes: u64,
    pub aggregate_bytes: u64,
}

/// Store's actual physical loan implementation. Idle façades retain the
/// broker, not this loan. A live connection/statement/transaction and native
/// I/O retain it until proven cleanup; unknown cleanup quarantines occupancy.
/// Implementations pin the physical root, parent and file descriptors, and
/// retain the real root-owner OS lock across all in-flight native operations.
pub trait DataLoanControl: Send + Sync {
    fn binding(&self) -> &DataBinding;
    fn files(&self) -> &dyn SqlVfs;
    fn mark_cleanup_known(&self) -> Result<(), StoreError>;
    fn cleanup_known(&self) -> bool;
    /// Install this strong hold in the admission's already reserved quarantine
    /// slot. Never allocate a new recovery entry during failed cleanup.
    fn quarantine(&self, retained: ControlHandle<dyn DataLoanControl>);
}
/// Noncloneable physical connection loan. Only Store's broker can construct it.
/// Sharing an idle store façade never constructs a second loan/connection.
pub struct DataConnectionLoan {
    pub(crate) control: ControlHandle<dyn DataLoanControl>,
}
impl DataConnectionLoan {
    pub fn binding(&self) -> &DataBinding {
        self.control.binding()
    }
    pub fn files(&self) -> &dyn SqlVfs {
        self.control.files()
    }
    /// Call only after the engine/statement/transaction and every native I/O
    /// operation have actually completed cleanup. Failure preserves occupancy.
    pub fn mark_cleanup_known(&self) -> Result<(), StoreError> {
        self.control.mark_cleanup_known()
    }
    pub fn quarantine(&self) {
        self.control.quarantine(self.control.clone());
    }
}
impl Drop for DataConnectionLoan {
    fn drop(&mut self) {
        if !self.control.cleanup_known() {
            self.control.quarantine(self.control.clone());
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FixedDataOperation {
    Get,
    Put,
    Delete,
    List,
}
impl FixedDataOperation {
    pub fn storage_operation(self) -> StoreOperation {
        match self {
            Self::Get | Self::List => StoreOperation::Read,
            Self::Put => StoreOperation::Write,
            Self::Delete => StoreOperation::Delete,
        }
    }
}
/// Existing KV wire arguments, kept as an already funded immutable graph.
/// The digest is computed with the actual reached producer's controlled encoder
/// before authorization; no plain String/Vec guest payload escapes its lease.
pub struct FixedDataRequest {
    operation: FixedDataOperation,
    arguments: CoreValue,
    digest: Digest,
}
impl FixedDataRequest {
    pub fn from_value(
        arguments: CoreValue,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, StoreError> {
        let CoreRef::Obj(fields) = arguments.inspect() else {
            return Err(StoreError::Invalid("KV argument record required".into()));
        };
        let op = fields
            .get("op")
            .ok_or_else(|| StoreError::Invalid("KV operation required".into()))?;
        let CoreRef::Str(op) = op.inspect() else {
            return Err(StoreError::Invalid("KV operation must be text".into()));
        };
        let operation = match op {
            "kv-get" => FixedDataOperation::Get,
            "kv-put" => FixedDataOperation::Put,
            "kv-delete" => FixedDataOperation::Delete,
            "kv-list" => FixedDataOperation::List,
            _ => {
                return Err(StoreError::Denied(
                    "fixed DATA path cannot execute SQL".into(),
                ));
            }
        };
        let expected = match operation {
            FixedDataOperation::Get | FixedDataOperation::Delete => 2,
            FixedDataOperation::Put => 3,
            FixedDataOperation::List => 4,
        };
        if fields.len() != expected {
            return Err(StoreError::Invalid(
                "KV argument fields must be exact".into(),
            ));
        }
        match operation {
            FixedDataOperation::Get | FixedDataOperation::Put | FixedDataOperation::Delete => {
                let key = fields
                    .get("key")
                    .ok_or_else(|| StoreError::Invalid("KV key required".into()))?;
                if !matches!(key.inspect(),CoreRef::Str(key) if !key.is_empty() && key.len()<=1024 && !key.contains('\0'))
                {
                    return Err(StoreError::Invalid("invalid KV key".into()));
                }
                if operation == FixedDataOperation::Put {
                    let value = fields
                        .get("value")
                        .ok_or_else(|| StoreError::Invalid("KV value required".into()))?;
                    let CoreRef::Arr(bytes) = value.inspect() else {
                        return Err(StoreError::Invalid("KV bytes required".into()));
                    };
                    if bytes.len() > 1024 * 1024 {
                        return Err(StoreError::Quota);
                    }
                    if !bytes
                        .iter()
                        .all(|byte| matches!(byte.inspect(), CoreRef::Int(0..=255)))
                    {
                        return Err(StoreError::Invalid("KV byte outside u8".into()));
                    }
                }
            }
            FixedDataOperation::List => {
                let prefix = fields
                    .get("prefix")
                    .ok_or_else(|| StoreError::Invalid("KV prefix required".into()))?;
                let after = fields
                    .get("after")
                    .ok_or_else(|| StoreError::Invalid("KV continuation required".into()))?;
                let limit = fields
                    .get("limit")
                    .ok_or_else(|| StoreError::Invalid("KV limit required".into()))?;
                if !matches!(prefix.inspect(),CoreRef::Str(value) if value.len()<=1024 && !value.contains('\0'))
                    || !matches!(after.inspect(), CoreRef::Null | CoreRef::Str(_))
                    || !matches!(limit.inspect(), CoreRef::Int(0..=1000))
                {
                    return Err(StoreError::Invalid("invalid KV list bounds".into()));
                }
                if matches!(after.inspect(),CoreRef::Str(value) if value.len()>1024 || value.contains('\0'))
                {
                    return Err(StoreError::Invalid("invalid KV continuation".into()));
                }
            }
        }
        let encoded = arguments
            .encode_guest(budget)
            .map_err(|_| StoreError::Quota)?;
        let digest = Digest::hash(encoded.as_bytes());
        Ok(Self {
            operation,
            arguments,
            digest,
        })
    }
    pub fn operation(&self) -> FixedDataOperation {
        self.operation
    }
    pub fn arguments(&self) -> &CoreValue {
        &self.arguments
    }
    pub fn digest(&self) -> &Digest {
        &self.digest
    }
}

/// Core injects the same private bounded SQLite implementation used by guest
/// typed SQL. Acquire the actual physical loan before instance initialization.
/// Reserve complete linear/stack/table/marshal/VFS bookkeeping capacity first.
#[async_trait]
pub trait DataEngine: Send + Sync {
    async fn connect(
        &self,
        cx: &CallCx,
        loan: DataConnectionLoan,
        funding: AllocationFrameScope,
    ) -> Result<ControlHandle<dyn DataConnection>, StoreError>;
}
/// One connection beneath both fixed DATA and arbitrary SQL interfaces. Fixed
/// KV consumes its existing Storage grant; it does not require an additional
/// arbitrary-SQL privilege. Guest SQL retains its own prepare/write barrier.
#[async_trait]
pub trait DataConnection: SqlConnection {
    async fn execute_fixed(
        &self,
        cx: &CallCx,
        permit: &DispatchPermit,
        request: FixedDataRequest,
        funding: AllocationFrameScope,
    ) -> Result<CoreValue, StoreError>;
    /// Core-only verified manifest seed/registration path, before guest entry.
    /// Retain exact hash/revision approval, normal quotas/audit and the existing
    /// read-only-reseed versus missing-keys-only private/public policy.
    async fn initialize_fixed(
        &self,
        cx: &CallCx,
        seed: Option<CoreValue>,
        hash: &PackHash,
        funding: AllocationFrameScope,
    ) -> Result<(), StoreError>;
}
