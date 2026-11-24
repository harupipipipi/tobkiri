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
/// A final payload moved out of its Arc, still holding the SAME reservation
/// until payload destruction. Outstanding Weak carriers fund the Arc block.
pub(crate) struct FundedOwned<T> {
    value: Option<T>,
    funding: Option<Funding>,
}
impl<T> FundedOwned<T> {
    pub(crate) fn get_mut(&mut self) -> &mut T {
        self.value.as_mut().expect("live funded owned payload")
    }
}
impl<T> Drop for FundedOwned<T> {
    fn drop(&mut self) {
        drop(self.value.take());
        drop(self.funding.take());
    }
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
    /// Atomically consume a strong carrier and take only the final payload.
    /// Unlike try_unwrap(...).ok(), concurrent consumers cannot accidentally
    /// drop the final payload recursively on the non-final branch. See
    /// https://doc.rust-lang.org/std/sync/struct.Arc.html#method.into_inner.
    pub(crate) fn into_inner(mut self) -> Option<FundedOwned<T>> {
        let funding = self.funding.take().expect("live original funding");
        let value = Arc::into_inner(self.value.take().expect("live funded strong carrier"))?;
        Some(FundedOwned {
            value: Some(value),
            funding: Some(funding),
        })
    }
    /// Release immutable linked metadata without recursive final-owner drops.
    /// `take_parent` is fixed internal field extraction, never an effect or
    /// guest callback. Callers must release their execution/admission guards
    /// before destruction can cascade through the detached payloads.
    pub(crate) fn drop_chain(mut next: Option<Self>, take_parent: fn(&mut T) -> Option<Self>) {
        while let Some(parent) = next.take() {
            if let Some(mut parent) = parent.into_inner() {
                next = take_parent(parent.get_mut());
            }
        }
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
    fn final_owned_payload_keeps_funding_until_payload_and_weak_block_are_gone() {
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
        let alias = value.clone();
        assert!(value.into_inner().is_none());
        assert_eq!(control.usage().bytes, 4096);
        assert_eq!(dropped.load(Ordering::Acquire), 0);
        let payload = alias.into_inner().unwrap();
        assert!(weak.upgrade().is_none());
        assert_eq!(dropped.load(Ordering::Acquire), 0);
        assert_eq!(control.usage().bytes, 4096);
        drop(payload);
        assert_eq!(dropped.load(Ordering::Acquire), 1);
        assert_eq!(control.usage().bytes, 4096);
        drop(weak);
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Frame), 0);
    }
    #[test]
    fn moved_payload_unwind_keeps_original_weak_allocation_funded() {
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
        let payload = value.into_inner().unwrap();
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| drop(payload))).is_err());
        assert_eq!(dropped.load(Ordering::Acquire), 1);
        assert!(weak.upgrade().is_none());
        assert_eq!(control.usage().bytes, 4096);
        drop(weak);
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Frame), 0);
    }
    #[test]
    fn linked_payloads_release_iteratively_with_concurrent_shared_owners() {
        struct Link {
            parent: Option<FundedArc<Link>>,
            dropped: Arc<AtomicUsize>,
            control: CoreControl,
        }
        impl Drop for Link {
            fn drop(&mut self) {
                assert!(self.control.usage().bytes >= 4096);
                self.dropped.fetch_add(1, Ordering::AcqRel);
                FundedArc::drop_chain(self.parent.take(), |link| link.parent.take());
            }
        }
        let control = CoreControl::default();
        let dropped = Arc::new(AtomicUsize::new(0));
        let mut next = None;
        let mut shared = Vec::new();
        let mut weak = None;
        for index in 0..2048 {
            let node = owned(
                Link {
                    parent: next,
                    dropped: dropped.clone(),
                    control: control.clone(),
                },
                &control,
            );
            if index == 1024 {
                weak = Some(node.downgrade());
            }
            if index % 2 == 0 {
                shared.push(node.clone());
            }
            next = Some(node);
        }
        assert_eq!(control.usage().bytes, 2048 * 4096);
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let ready = barrier.clone();
        let tail = std::thread::Builder::new()
            .stack_size(128 * 1024)
            .spawn(move || {
                ready.wait();
                drop(next);
            })
            .unwrap();
        let aliases = std::thread::Builder::new()
            .stack_size(128 * 1024)
            .spawn(move || {
                barrier.wait();
                drop(shared);
            })
            .unwrap();
        tail.join().unwrap();
        aliases.join().unwrap();
        assert_eq!(dropped.load(Ordering::Acquire), 2048);
        assert!(weak.as_ref().unwrap().upgrade().is_none());
        assert_eq!(control.usage().bytes, 4096);
        assert_eq!(control.usage().count(ControlKind::Frame), 1);
        drop(weak);
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Frame), 0);
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
