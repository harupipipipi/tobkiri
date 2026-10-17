//! Core-owned persistence. Guests receive only grant-backed store/secret handles.
mod approvals;
mod audit;
mod data;
mod data_custody;
mod data_moves;
mod disk_policy;
mod owner;
#[cfg(any(unix, windows))]
mod quota_fs;
#[cfg(any(unix, windows))]
mod quota_vfs;
mod secure;
mod sql;
mod tiered;
mod vault;
#[cfg(unix)]
mod vfs;
#[cfg(windows)]
#[path = "vfs_windows.rs"]
mod vfs;
#[cfg(not(any(unix, windows)))]
#[path = "vfs_unavailable.rs"]
mod vfs;
pub use approvals::*;
pub use audit::*;
pub use data::*;
pub use disk_policy::*;
pub use owner::*;
pub use secure::*;
pub use sql::*;
pub use tiered::*;
pub use vault::*;

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("permission denied: {0}")]
    Denied(String),
    #[error("invalid storage request: {0}")]
    Invalid(String),
    #[error("storage not found")]
    NotFound,
    #[error("storage conflict: {0}")]
    Conflict(String),
    #[error("storage quota exceeded")]
    Quota,
    #[error("storage primitive unavailable: {0}")]
    Unavailable(String),
    #[error("storage-sql-commit-unknown: operation may have committed; do not replay")]
    CommitUnknown,
    #[error("storage-pending-data-move: storage move requires explicit repair; do not replay")]
    PendingDataMove,
    #[error("authority integrity fault: {0:?}")]
    Integrity(tobkiri_gate::IntegrityFaultCode),
    #[error("storage I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("SQLite operation failed: {0}")]
    Sql(#[from] rusqlite::Error),
    #[error(transparent)]
    Gate(#[from] tobkiri_gate::GateError),
    #[error("invalid canonical record: {0}")]
    Canon(String),
}
impl From<tobkiri_gate::CoreControlError> for StoreError {
    fn from(error: tobkiri_gate::CoreControlError) -> Self {
        match error {
            tobkiri_gate::CoreControlError::Invalid => Self::Invalid(error.to_string()),
            tobkiri_gate::CoreControlError::Limit => Self::Quota,
        }
    }
}
pub(crate) fn canonical<T: serde::Serialize>(v: &T) -> Result<Vec<u8>, StoreError> {
    let raw = serde_json::to_vec(v).map_err(|e| StoreError::Canon(e.to_string()))?;
    let value = tobkiri_canon::parse(&raw, tobkiri_canon::Variant::StrictV1)
        .map_err(|e| StoreError::Canon(e.to_string()))?;
    tobkiri_canon::encode(&value, tobkiri_canon::Variant::StrictV1)
        .map_err(|e| StoreError::Canon(e.to_string()))
}
pub(crate) fn decode<T: serde::de::DeserializeOwned>(raw: &[u8]) -> Result<T, StoreError> {
    let v = tobkiri_canon::parse(raw, tobkiri_canon::Variant::StrictV1)
        .map_err(|e| StoreError::Canon(e.to_string()))?;
    let bytes = tobkiri_canon::encode(&v, tobkiri_canon::Variant::StrictV1)
        .map_err(|e| StoreError::Canon(e.to_string()))?;
    serde_json::from_slice(&bytes).map_err(|e| StoreError::Canon(e.to_string()))
}
pub(crate) fn sql_u64(row: &rusqlite::Row<'_>, index: usize) -> rusqlite::Result<u64> {
    let value: i64 = row.get(index)?;
    u64::try_from(value).map_err(|e| {
        rusqlite::Error::FromSqlConversionFailure(
            index,
            rusqlite::types::Type::Integer,
            Box::new(e),
        )
    })
}
