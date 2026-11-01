//! Native DATA safety ceilings. These limits confer no storage authority.
use crate::StoreError;

/// Logical-extent ceilings for one physical DATA database and its sidecars.
/// The journal allowance is reserved before ordinary database writes, so quota
/// exhaustion cannot spend rollback/recovery capacity. Filesystem blocks and
/// operating-system metadata are outside this accounting boundary.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StoreDiskPolicy {
    database_bytes: u64,
    journal_bytes: u64,
    aggregate_bytes: u64,
}
impl Default for StoreDiskPolicy {
    fn default() -> Self {
        Self {
            database_bytes: 64 * 1024 * 1024,
            journal_bytes: 64 * 1024 * 1024,
            aggregate_bytes: 128 * 1024 * 1024,
        }
    }
}
impl StoreDiskPolicy {
    /// Construct explicit native ceilings. Effective limits never exceed the
    /// production safety ceilings, and reserve at least one journal allowance.
    pub fn new(
        database_bytes: u64,
        journal_bytes: u64,
        aggregate_bytes: u64,
    ) -> Result<Self, StoreError> {
        if database_bytes == 0 || journal_bytes == 0 || aggregate_bytes < 2 {
            return Err(StoreError::Invalid(
                "DATA disk ceilings must be positive".into(),
            ));
        }
        Ok(Self::default().narrow(Self {
            database_bytes,
            journal_bytes,
            aggregate_bytes,
        }))
    }
    pub fn database_bytes(self) -> u64 {
        self.database_bytes
    }
    pub fn journal_bytes(self) -> u64 {
        self.journal_bytes
    }
    pub fn aggregate_bytes(self) -> u64 {
        self.aggregate_bytes
    }
    pub(crate) fn narrow(self, other: Self) -> Self {
        let aggregate_bytes = self.aggregate_bytes.min(other.aggregate_bytes);
        Self {
            database_bytes: self
                .database_bytes
                .min(other.database_bytes)
                .min(aggregate_bytes / 2),
            journal_bytes: self
                .journal_bytes
                .min(other.journal_bytes)
                .min(aggregate_bytes / 2),
            aggregate_bytes,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn explicit_policy_only_narrows_and_keeps_recovery_room() {
        let safety = StoreDiskPolicy::default();
        let larger = StoreDiskPolicy::new(u64::MAX, u64::MAX, u64::MAX).unwrap();
        assert_eq!(larger, safety);
        let narrow = StoreDiskPolicy::new(1000, 1000, 100).unwrap();
        assert_eq!(narrow.database_bytes(), 50);
        assert_eq!(narrow.journal_bytes(), 50);
        assert_eq!(narrow.aggregate_bytes(), 100);
        assert!(StoreDiskPolicy::new(0, 1, 2).is_err());
    }
}
