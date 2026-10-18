//! Storage custody only: no authority, execution, cleanup or terminal proof.
//! Every live strong and weak carrier retains the same original release plan.
use std::sync::{Arc, Weak};
use tobkiri_canon::LeasedShared;
use tobkiri_gate::{ControlReservation, GateError};

pub(crate) type Funding = LeasedShared<(), ControlReservation>;

/// The caller includes this exact block before its original reservation, then
/// constructs it before any other owned allocation in that funded bundle.
pub(crate) fn new_funding(reservation: ControlReservation) -> Result<Funding, GateError> {
    Funding::try_new_precharged((), reservation).map_err(|_| GateError::Unavailable)
}

pub(crate) struct FundedArc<T> {
    value: Option<Arc<T>>,
    funding: Option<Funding>,
}
pub(crate) struct FundedWeak<T> {
    value: Option<Weak<T>>,
    funding: Option<Funding>,
}
impl<T> FundedArc<T> {
    /// All owned storage inside value and its Arc layout must already be
    /// covered by this exact original funding. No raw Arc is accepted/exposed.
    pub(crate) fn new(value: T, funding: Funding) -> Self {
        let value = Arc::new(value);
        Self {
            value: Some(value),
            funding: Some(funding),
        }
    }
    pub(crate) fn get(&self) -> &T {
        self.value.as_deref().expect("live funded strong carrier")
    }
    pub(crate) fn funding(&self) -> Funding {
        self.funding
            .as_ref()
            .expect("live original funding")
            .clone()
    }
    pub(crate) fn downgrade(&self) -> FundedWeak<T> {
        let funding = self.funding();
        let value = Arc::downgrade(self.value.as_ref().expect("live funded strong carrier"));
        FundedWeak {
            value: Some(value),
            funding: Some(funding),
        }
    }
    pub(crate) fn ptr_eq(this: &Self, other: &Self) -> bool {
        Arc::ptr_eq(
            this.value.as_ref().expect("live funded strong carrier"),
            other.value.as_ref().expect("live funded strong carrier"),
        )
    }
    #[cfg(test)]
    pub(crate) fn strong_count(&self) -> usize {
        Arc::strong_count(self.value.as_ref().expect("live funded strong carrier"))
    }
}
impl<T> Clone for FundedArc<T> {
    fn clone(&self) -> Self {
        let funding = self.funding();
        let value = self
            .value
            .as_ref()
            .expect("live funded strong carrier")
            .clone();
        Self {
            value: Some(value),
            funding: Some(funding),
        }
    }
}
impl<T> std::ops::Deref for FundedArc<T> {
    type Target = T;
    fn deref(&self) -> &T {
        self.get()
    }
}
impl<T> Drop for FundedArc<T> {
    fn drop(&mut self) {
        // Arc's payload/implicit-Weak unwind completes before funding. If it
        // panics, field-drop glue still drops the remaining funding afterward.
        drop(self.value.take());
        drop(self.funding.take());
    }
}
impl<T> FundedWeak<T> {
    pub(crate) fn upgrade(&self) -> Option<FundedArc<T>> {
        let funding = self
            .funding
            .as_ref()
            .expect("live original funding")
            .clone();
        // On None the provisional clone drops, while THIS weak carrier still
        // retains its original funding and the original Arc allocation.
        let value = self
            .value
            .as_ref()
            .expect("live funded weak carrier")
            .upgrade()?;
        Some(FundedArc {
            value: Some(value),
            funding: Some(funding),
        })
    }
    pub(crate) fn strong_count(&self) -> usize {
        self.value
            .as_ref()
            .expect("live funded weak carrier")
            .strong_count()
    }
}
impl<T> Clone for FundedWeak<T> {
    fn clone(&self) -> Self {
        let funding = self
            .funding
            .as_ref()
            .expect("live original funding")
            .clone();
        let value = self
            .value
            .as_ref()
            .expect("live funded weak carrier")
            .clone();
        Self {
            value: Some(value),
            funding: Some(funding),
        }
    }
}
impl<T> Drop for FundedWeak<T> {
    fn drop(&mut self) {
        // The final raw Weak physically frees ArcInner before the last Funding
        // frees its own leased block and only then refunds ControlReservation.
        drop(self.value.take());
        drop(self.funding.take());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tobkiri_gate::{ControlKind, CoreControl};

    fn owned<T>(value: T, control: &CoreControl) -> FundedArc<T> {
        assert!(
            crate::ui_operation_delivery::arc_owned_bytes::<T>().unwrap()
                + Funding::storage_bytes()
                <= 4096
        );
        let reservation = control.reserve(ControlKind::Frame, 4096).unwrap();
        let funding = new_funding(reservation).unwrap();
        FundedArc::new(value, funding)
    }
    struct Payload {
        dropped: Arc<AtomicUsize>,
        control: CoreControl,
        panic: bool,
    }
    impl Drop for Payload {
        fn drop(&mut self) {
            assert_eq!(self.control.usage().bytes, 4096);
            self.dropped.fetch_add(1, Ordering::AcqRel);
            if self.panic {
                panic!("deterministic funded payload unwind");
            }
        }
    }
    #[test]
    fn last_strong_destroys_payload_but_live_weak_keeps_original_charge() {
        let control = CoreControl::default();
        let dropped = Arc::new(AtomicUsize::new(0));
        let value = owned(
            Payload {
                dropped: dropped.clone(),
                control: control.clone(),
                panic: false,
            },
            &control,
        );
        let weak = value.downgrade();
        let other_weak = weak.clone();
        drop(value);
        assert_eq!(dropped.load(Ordering::Acquire), 1);
        assert!(weak.upgrade().is_none());
        assert_eq!(control.usage().bytes, 4096);
        drop(weak);
        assert_eq!(control.usage().bytes, 4096);
        drop(other_weak);
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Frame), 0);
    }
    #[test]
    fn clone_and_upgrade_share_identity_and_one_original_reservation() {
        let control = CoreControl::default();
        let value = owned(17_u64, &control);
        let weak = value.downgrade();
        let copy = value.clone();
        let upgraded = weak.upgrade().unwrap();
        assert!(FundedArc::ptr_eq(&value, &copy));
        assert!(FundedArc::ptr_eq(&value, &upgraded));
        assert_eq!(control.usage().count(ControlKind::Frame), 1);
        assert_eq!(control.usage().bytes, 4096);
        drop(value);
        drop(copy);
        assert_eq!(*upgraded, 17);
        drop(upgraded);
        assert!(weak.upgrade().is_none());
        assert_eq!(control.usage().bytes, 4096);
        drop(weak);
        assert_eq!(control.usage().bytes, 0);
    }
    #[test]
    fn payload_unwind_cannot_refund_while_original_weak_allocation_remains() {
        let control = CoreControl::default();
        let dropped = Arc::new(AtomicUsize::new(0));
        let value = owned(
            Payload {
                dropped: dropped.clone(),
                control: control.clone(),
                panic: true,
            },
            &control,
        );
        let weak = value.downgrade();
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| drop(value))).is_err());
        assert_eq!(dropped.load(Ordering::Acquire), 1);
        assert!(weak.upgrade().is_none());
        assert_eq!(control.usage().bytes, 4096);
        drop(weak);
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(dropped.load(Ordering::Acquire), 1);
    }
}
