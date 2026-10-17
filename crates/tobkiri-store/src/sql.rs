//! Capability-only boundary for the bounded internal SQLite engine injected by core.
//! Native SQLite remains limited to genuine authority/catalog metadata.
use crate::StoreError;
use async_trait::async_trait;
use tobkiri_canon::{
    ControlHandle, CoreRef, CoreValue, Digest,
    ids::{PackId, ProfileId},
};
use tobkiri_gate::{CallCx, Principal};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SqlInteger(String);
impl SqlInteger {
    pub fn parse(value: &str) -> Result<Self, StoreError> {
        let number = value
            .parse::<i64>()
            .map_err(|_| StoreError::Invalid("invalid SQL integer".into()))?;
        if number.to_string() != value {
            return Err(StoreError::Invalid("noncanonical SQL integer".into()));
        }
        Ok(Self(value.into()))
    }
    pub fn new(value: i64) -> Self {
        Self(value.to_string())
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SqlReal(String);
impl SqlReal {
    pub fn parse(value: &str) -> Result<Self, StoreError> {
        let number = value
            .parse::<f64>()
            .map_err(|_| StoreError::Invalid("invalid SQL real".into()))?;
        let canonical = Self::new(number)?;
        if canonical.as_str() != value {
            return Err(StoreError::Invalid(
                "noncanonical scientific SQL real".into(),
            ));
        }
        Ok(canonical)
    }
    pub fn new(value: f64) -> Result<Self, StoreError> {
        if !value.is_finite() {
            return Err(StoreError::Invalid("nonfinite SQL real".into()));
        }
        Ok(Self(format!("{value:e}")))
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
}
/// Lossless cells; the 1.1 ABI owns the JSON tag encoding.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SqlCell {
    Null,
    Integer(SqlInteger),
    Real(SqlReal),
    Text(String),
    Blob(Vec<u8>),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TypedSqlResult {
    pub columns: Vec<String>,
    pub rows: Vec<Vec<SqlCell>>,
    pub affected_rows: Option<SqlInteger>,
    pub last_insert_rowid: Option<SqlInteger>,
}
/// Immutable guest SQL input. All borrowed/detached fields retain the graph's
/// actual capacity owner. Plain SqlStatement remains a trusted codec utility.
#[derive(Clone, Debug)]
pub struct OwnedSqlStatement {
    value: CoreValue,
    statement: CoreValue,
    params: CoreValue,
    max_rows: u32,
}
impl OwnedSqlStatement {
    /// Accept exactly the bounded internal statement record, after the caller
    /// has already funded CoreValue construction before guest ABI lifting.
    pub fn from_value(value: CoreValue) -> Result<Self, StoreError> {
        let CoreRef::Obj(record) = value.inspect() else {
            return Err(StoreError::Invalid("SQL statement record required".into()));
        };
        if record.len() != 3 {
            return Err(StoreError::Invalid(
                "SQL statement fields must be exact".into(),
            ));
        }
        let statement = record
            .get("statement")
            .ok_or_else(|| StoreError::Invalid("missing SQL statement".into()))?;
        let params = record
            .get("params")
            .ok_or_else(|| StoreError::Invalid("missing SQL parameters".into()))?;
        let limit = record
            .get("max-rows")
            .ok_or_else(|| StoreError::Invalid("missing SQL row limit".into()))?;
        if !matches!(statement.inspect(), CoreRef::Str(_)) {
            return Err(StoreError::Invalid("SQL statement must be text".into()));
        }
        let CoreRef::Int(limit) = limit.inspect() else {
            return Err(StoreError::Invalid("SQL row limit must be u32".into()));
        };
        let max_rows = u32::try_from(limit)
            .map_err(|_| StoreError::Invalid("SQL row limit exceeds u32".into()))?;
        let CoreRef::Arr(cells) = params.inspect() else {
            return Err(StoreError::Invalid(
                "positional SQL parameters required".into(),
            ));
        };
        for cell in cells.iter() {
            validate_owned_cell(&cell)?;
        }
        Ok(Self {
            value,
            statement,
            params,
            max_rows,
        })
    }
    pub fn statement(&self) -> &str {
        match self.statement.inspect() {
            CoreRef::Str(value) => value,
            _ => unreachable!("validated immutable SQL statement"),
        }
    }
    pub fn params(&self) -> &CoreValue {
        &self.params
    }
    pub fn max_rows(&self) -> u32 {
        self.max_rows
    }
    pub fn value(&self) -> &CoreValue {
        &self.value
    }
}
/// Guest results retain actual allocation custody after connection/Core close.
/// Shared clones and detached row/column values share the same physical graph.
#[derive(Clone, Debug)]
pub struct OwnedSqlResult {
    value: CoreValue,
    // Native completion evidence is separate from guest-shaped result fields.
    // Public result parsing can never manufacture this proof.
    confirmed_write_commit: bool,
}
impl OwnedSqlResult {
    pub fn from_value(value: CoreValue) -> Result<Self, StoreError> {
        let CoreRef::Obj(record) = value.inspect() else {
            return Err(StoreError::Invalid("SQL result record required".into()));
        };
        if record.len() != 4 {
            return Err(StoreError::Invalid(
                "SQL result fields must be exact".into(),
            ));
        }
        let columns = record
            .get("columns")
            .ok_or_else(|| StoreError::Invalid("SQL columns missing".into()))?;
        let rows = record
            .get("rows")
            .ok_or_else(|| StoreError::Invalid("SQL rows missing".into()))?;
        let CoreRef::Arr(columns) = columns.inspect() else {
            return Err(StoreError::Invalid("SQL columns must be a list".into()));
        };
        for column in columns.iter() {
            if !matches!(column.inspect(), CoreRef::Str(_)) {
                return Err(StoreError::Invalid("SQL column name must be text".into()));
            }
        }
        let CoreRef::Arr(rows) = rows.inspect() else {
            return Err(StoreError::Invalid("SQL rows must be a list".into()));
        };
        for row in rows.iter() {
            let CoreRef::Arr(row) = row.inspect() else {
                return Err(StoreError::Invalid("SQL row must be a list".into()));
            };
            if row.len() != columns.len() {
                return Err(StoreError::Invalid("SQL row/column count mismatch".into()));
            }
            for cell in row.iter() {
                validate_owned_cell(&cell)?;
            }
        }
        for key in ["affected-rows", "last-insert-rowid"] {
            let metadata = record
                .get(key)
                .ok_or_else(|| StoreError::Invalid("SQL result metadata missing".into()))?;
            match metadata.inspect() {
                CoreRef::Null => (),
                CoreRef::Str(v) if valid_integer(v) => (),
                _ => {
                    return Err(StoreError::Invalid(
                        "SQL result integer metadata must be canonical".into(),
                    ));
                }
            }
        }
        Ok(Self {
            value,
            confirmed_write_commit: false,
        })
    }
    /// The native backend confirmed an actual write's autocommit boundary.
    /// This is delivery evidence only; it confers no permission or replay right.
    pub fn confirmed_write_commit(&self) -> bool {
        self.confirmed_write_commit
    }
    pub(crate) fn mark_confirmed_write_commit(&mut self) {
        self.confirmed_write_commit = true;
    }
    pub fn value(&self) -> &CoreValue {
        &self.value
    }
    pub fn into_value(self) -> CoreValue {
        self.value
    }
}
fn valid_integer(value: &str) -> bool {
    let digits = value.strip_prefix('-').unwrap_or(value);
    !digits.is_empty()
        && digits.bytes().all(|b| b.is_ascii_digit())
        && !(digits.len() > 1 && digits.starts_with('0'))
        && value != "-0"
        && value.parse::<i64>().is_ok()
}
fn valid_real(value: &str) -> bool {
    use std::fmt::Write;
    let Ok(number) = value.parse::<f64>() else {
        return false;
    };
    if !number.is_finite() {
        return false;
    }
    struct Stack {
        bytes: [u8; 64],
        len: usize,
    }
    impl std::fmt::Write for Stack {
        fn write_str(&mut self, text: &str) -> std::fmt::Result {
            let end = self
                .len
                .checked_add(text.len())
                .filter(|end| *end <= self.bytes.len())
                .ok_or(std::fmt::Error)?;
            self.bytes[self.len..end].copy_from_slice(text.as_bytes());
            self.len = end;
            Ok(())
        }
    }
    let mut canonical = Stack {
        bytes: [0; 64],
        len: 0,
    };
    write!(&mut canonical, "{number:e}").is_ok()
        && value.as_bytes() == &canonical.bytes[..canonical.len]
}
fn validate_owned_cell(cell: &CoreValue) -> Result<(), StoreError> {
    let CoreRef::Obj(record) = cell.inspect() else {
        return Err(StoreError::Invalid("SQL cell must be tagged".into()));
    };
    if record.len() != 2 {
        return Err(StoreError::Invalid(
            "SQL cell fields must be exactly type and value".into(),
        ));
    }
    let kind = record
        .get("type")
        .ok_or_else(|| StoreError::Invalid("SQL cell type missing".into()))?;
    let CoreRef::Str(kind) = kind.inspect() else {
        return Err(StoreError::Invalid("SQL cell type must be text".into()));
    };
    let value = record
        .get("value")
        .ok_or_else(|| StoreError::Invalid("SQL cell value missing".into()))?;
    let valid = match (kind, value.inspect()) {
        ("null", CoreRef::Null) => true,
        ("integer", CoreRef::Str(v)) => valid_integer(v),
        ("real", CoreRef::Str(v)) => valid_real(v),
        ("text", CoreRef::Str(_)) => true,
        ("blob", CoreRef::Arr(bytes)) => bytes
            .iter()
            .all(|byte| matches!(byte.inspect(), CoreRef::Int(0..=255))),
        _ => false,
    };
    if valid {
        Ok(())
    } else {
        Err(StoreError::Invalid("SQL cell type/value mismatch".into()))
    }
}
#[derive(Clone, Debug)]
pub struct SqlStatement {
    pub statement: String,
    pub params: Vec<SqlCell>,
    pub max_rows: u32,
}
/// `ForbidRows` rejects any prepared result-column count before the first step,
/// including a statement that would produce zero rows.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SqlResultPolicy {
    AllowRows,
    ForbidRows,
}
/// Every connection is pinned to this immutable authority context.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SqlBinding {
    pub profile: ProfileId,
    pub caller: Principal,
    pub owner: PackId,
    pub store: String,
    pub revision: Digest,
    pub security_epoch: u64,
    pub read_only: bool,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SqlFileKind {
    Main,
    Journal,
    Wal,
    SharedMemory,
    Temporary(u64),
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SqlLock {
    None,
    Shared,
    Reserved,
    Pending,
    Exclusive,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SqlShmLock {
    Shared,
    Exclusive,
    UnlockShared,
    UnlockExclusive,
}
/// No ambient paths. Implementations pin descriptors and bound aggregate disk use.
pub trait SqlFile: Send + Sync {
    fn read_at(&self, offset: u64, output: &mut [u8]) -> Result<usize, StoreError>;
    fn write_at(&self, offset: u64, bytes: &[u8]) -> Result<(), StoreError>;
    fn truncate(&self, length: u64) -> Result<(), StoreError>;
    fn size(&self) -> Result<u64, StoreError>;
    fn sync(&self) -> Result<(), StoreError>;
    fn lock(&self, level: SqlLock) -> Result<(), StoreError>;
    fn shm_lock(&self, offset: u32, count: u32, level: SqlShmLock) -> Result<(), StoreError>;
    fn shm_barrier(&self) -> Result<(), StoreError>;
    fn close(&self) -> Result<(), StoreError>;
}
pub trait SqlVfs: Send + Sync {
    fn open(
        &self,
        kind: SqlFileKind,
        create: bool,
    ) -> Result<ControlHandle<dyn SqlFile>, StoreError>;
    fn remove(&self, kind: SqlFileKind) -> Result<(), StoreError>;
    fn exists(&self, kind: SqlFileKind) -> Result<bool, StoreError>;
}
impl SqlCell {
    pub fn to_value(&self) -> tobkiri_canon::CanonValue {
        use tobkiri_canon::CanonValue as V;
        let (kind, value) = match self {
            Self::Null => ("null", V::Null),
            Self::Integer(v) => ("integer", V::Str(v.as_str().into())),
            Self::Real(v) => ("real", V::Str(v.as_str().into())),
            Self::Text(v) => ("text", V::Str(v.clone())),
            Self::Blob(v) => (
                "blob",
                V::Arr(v.iter().map(|b| V::Int(i64::from(*b))).collect()),
            ),
        };
        V::Obj(std::collections::BTreeMap::from([
            ("type".into(), V::Str(kind.into())),
            ("value".into(), value),
        ]))
    }
    pub fn from_value(value: &tobkiri_canon::CanonValue) -> Result<Self, StoreError> {
        use tobkiri_canon::CanonValue as V;
        let V::Obj(fields) = value else {
            return Err(StoreError::Invalid("SQL cell must be tagged".into()));
        };
        if fields.len() != 2 {
            return Err(StoreError::Invalid(
                "SQL cell fields must be exactly type and value".into(),
            ));
        }
        let Some(V::Str(kind)) = fields.get("type") else {
            return Err(StoreError::Invalid("SQL cell has no type tag".into()));
        };
        let value = fields
            .get("value")
            .ok_or_else(|| StoreError::Invalid("SQL cell has no value".into()))?;
        match (kind.as_str(), value) {
            ("null", V::Null) => Ok(Self::Null),
            ("integer", V::Str(v)) => Ok(Self::Integer(SqlInteger::parse(v)?)),
            ("real", V::Str(v)) => Ok(Self::Real(SqlReal::parse(v)?)),
            ("text", V::Str(v)) => Ok(Self::Text(v.clone())),
            ("blob", V::Arr(v)) => Ok(Self::Blob(
                v.iter()
                    .map(|item| match item {
                        V::Int(n) => u8::try_from(*n)
                            .map_err(|_| StoreError::Invalid("SQL blob byte out of range".into())),
                        _ => Err(StoreError::Invalid("SQL blob must be byte integers".into())),
                    })
                    .collect::<Result<Vec<_>, _>>()?,
            )),
            _ => Err(StoreError::Invalid("SQL cell type/value mismatch".into())),
        }
    }
}
/// Core injects a backend that reserves instance+host-buffer capacity before init.
#[async_trait]
pub trait SqlExecutor: Send + Sync {
    async fn connect(
        &self,
        cx: &CallCx,
        binding: SqlBinding,
        files: crate::DataConnectionLoan,
        funding: tobkiri_gate::AllocationFrameScope,
    ) -> Result<ControlHandle<dyn SqlConnection>, StoreError>;
}
/// Serialize a connection. Unrelated operations conflict while a transaction is live.
/// Trap/poison discards the instance; recovery never replays a mutation implicitly.
#[async_trait]
pub trait SqlConnection: Send + Sync {
    fn binding(&self) -> &SqlBinding;
    async fn exec_typed(
        &self,
        cx: &CallCx,
        statement: OwnedSqlStatement,
        result_policy: SqlResultPolicy,
    ) -> Result<OwnedSqlResult, StoreError>;
    async fn query_typed(
        &self,
        cx: &CallCx,
        statement: OwnedSqlStatement,
    ) -> Result<OwnedSqlResult, StoreError>;
    async fn sql_begin(&self, cx: &CallCx)
    -> Result<ControlHandle<dyn SqlTransaction>, StoreError>;
    async fn close(&self) -> Result<(), StoreError>;
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SqlTransactionState {
    Active,
    RollbackOnly,
    Committed,
    RolledBack,
    CommitUnknown,
}
/// BEGIN DEFERRED pins one connection; failures make it rollback-only. A repeated
/// terminal operation is idempotent, while the opposite terminal operation conflicts.
#[async_trait]
pub trait SqlTransaction: Send + Sync {
    fn binding(&self) -> &SqlBinding;
    fn state(&self) -> SqlTransactionState;
    async fn exec_typed(
        &self,
        cx: &CallCx,
        statement: OwnedSqlStatement,
        result_policy: SqlResultPolicy,
    ) -> Result<OwnedSqlResult, StoreError>;
    async fn query_typed(
        &self,
        cx: &CallCx,
        statement: OwnedSqlStatement,
    ) -> Result<OwnedSqlResult, StoreError>;
    async fn commit(&self, cx: &CallCx) -> Result<(), StoreError>;
    async fn rollback(&self, cx: &CallCx) -> Result<(), StoreError>;
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use tobkiri_canon::{AllocationBudget, AllocationError, AllocationRequest};
    use tobkiri_canon::{CanonValue as V, Variant};
    struct TestValues;
    impl AllocationBudget for TestValues {
        fn reserve(&self, _: AllocationRequest) -> Result<(), AllocationError> {
            Ok(())
        }
        fn release(&self, _: u64) {}
    }
    #[test]
    fn parsed_write_shaped_metadata_cannot_claim_a_native_commit() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestValues);
        let value = V::Obj(std::collections::BTreeMap::from([
            ("columns".into(), V::Arr(vec![])),
            ("rows".into(), V::Arr(vec![])),
            ("affected-rows".into(), V::Str("1".into())),
            ("last-insert-rowid".into(), V::Str("1".into())),
        ]));
        let result =
            OwnedSqlResult::from_value(CoreValue::try_from_borrowed(&value, &budget).unwrap())
                .unwrap();
        assert!(!result.confirmed_write_commit());
    }
    #[test]
    fn lossless_tagged_cells_preserve_i64_signed_zero_text_nul_and_blob() {
        for cell in [
            SqlCell::Null,
            SqlCell::Integer(SqlInteger::new(i64::MIN)),
            SqlCell::Integer(SqlInteger::new(i64::MAX)),
            SqlCell::Real(SqlReal::new(-0.0).unwrap()),
            SqlCell::Real(SqlReal::new(f64::MIN_POSITIVE).unwrap()),
            SqlCell::Text("\0{\"type\":\"blob\"}λ".into()),
            SqlCell::Blob(vec![0, 255]),
        ] {
            let value = cell.to_value();
            let bytes = tobkiri_canon::encode(&value, Variant::StrictV1).unwrap();
            let decoded = tobkiri_canon::parse(&bytes, Variant::StrictV1).unwrap();
            assert_eq!(SqlCell::from_value(&decoded).unwrap(), cell);
        }
        assert_eq!(SqlReal::new(-0.0).unwrap().as_str(), "-0e0");
        for v in ["+1", "01", "-0", "1e0", "9223372036854775808"] {
            assert!(SqlInteger::parse(v).is_err());
        }
        for v in ["1", "1.0e0", "NaN", "inf", "-0", "1e+0"] {
            assert!(SqlReal::parse(v).is_err());
        }
        let mut fields = std::collections::BTreeMap::from([
            ("type".into(), V::Str("blob".into())),
            ("value".into(), V::Arr(vec![V::Bool(true)])),
        ]);
        assert!(SqlCell::from_value(&V::Obj(fields.clone())).is_err());
        fields.insert("extra".into(), V::Null);
        assert!(SqlCell::from_value(&V::Obj(fields)).is_err());
        assert!(SqlCell::from_value(&V::Str("plain parameter".into())).is_err());
    }
}
