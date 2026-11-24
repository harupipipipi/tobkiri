//! Physical shared-state vectors; allocation-free retained carrier copies only.
use tobkiri_canon::LeasedShared;
use tobkiri_gate::{CapacityKind, GateError, NativeSetupAccount, NativeSharedStateReservation};

pub(super) type NativeRef<T> = LeasedShared<T, NativeSharedStateReservation>;

pub(super) struct NativeRegistry<T> {
    // Destroy storage before returning its original physical funding.
    pub(super) entries: Vec<NativeRef<T>>,
    funding: Option<NativeSharedStateReservation>,
}
impl<T> Default for NativeRegistry<T> {
    fn default() -> Self {
        Self {
            entries: Vec::new(),
            funding: None,
        }
    }
}
pub(super) struct Replacement<T> {
    entries: Vec<NativeRef<T>>,
    funding: NativeSharedStateReservation,
}
impl<T> NativeRegistry<T> {
    /// Prepare replacement storage while the original remains prepaid and
    /// unchanged. The caller publishes only after its new entry also succeeds.
    pub(super) fn prepare_slot(
        &self,
        account: &NativeSetupAccount,
    ) -> Result<Option<Replacement<T>>, GateError> {
        if self.entries.len() < self.entries.capacity() {
            return Ok(None);
        }
        let capacity = self
            .entries
            .capacity()
            .checked_mul(2)
            .ok_or(GateError::Unavailable)?
            .max(1);
        let bytes = capacity
            .checked_mul(size_of::<NativeRef<T>>())
            .and_then(|n| u64::try_from(n).ok())
            .ok_or(GateError::Unavailable)?;
        let funding = account.reserve_shared_state(CapacityKind::VfsBuffer, bytes)?;
        let mut entries = Vec::new();
        entries
            .try_reserve_exact(capacity)
            .map_err(|_| GateError::Unavailable)?;
        if entries.capacity() != capacity {
            return Err(GateError::Unavailable);
        }
        entries.extend(self.entries.iter().cloned());
        Ok(Some(Replacement { entries, funding }))
    }
    /// Called under the same registry lock as prepare_slot. No new allocation
    /// is allowed here; old storage dies before its funding is replaced.
    pub(super) fn publish(&mut self, entry: NativeRef<T>, replacement: Option<Replacement<T>>) {
        if let Some(replacement) = replacement {
            let old = std::mem::replace(&mut self.entries, replacement.entries);
            drop(old);
            self.funding = Some(replacement.funding);
        }
        assert!(self.entries.len() < self.entries.capacity());
        self.entries.push(entry);
    }
    /// Return the removed carrier; its destructor must run outside the lock.
    pub(super) fn remove(&mut self, entry: &NativeRef<T>) -> Option<NativeRef<T>> {
        self.entries
            .iter()
            .position(|other| std::ptr::eq(other.get(), entry.get()))
            .map(|index| self.entries.remove(index))
    }
}
