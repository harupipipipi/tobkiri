use super::*;
use std::sync::atomic::AtomicUsize;
use tobkiri_canon::Digest;
use tobkiri_gate::CoreControlLimits;
use tokio::sync::oneshot;

fn generation(control: &CoreControl, number: u64) -> Arc<GenerationRetirement> {
    GenerationRetirement::new(
        control,
        &PackId::parse("local.retirement-test").unwrap(),
        &PackHash::new(Digest::hash(b"retirement-test")),
        number,
    )
    .unwrap()
}

fn completion(generation: &GenerationRetirement) -> Completion {
    let state = generation.state.lock().unwrap();
    match &*state {
        State::Retiring { completion, .. } => completion.clone(),
        _ => panic!("expected retained join receipt"),
    }
}

#[tokio::test]
async fn sibling_handles_join_one_actual_task_and_keep_control_until_it_finishes() {
    let control = CoreControl::default();
    let generation = generation(&control, 1);
    let starts = Arc::new(AtomicUsize::new(0));
    let (entered_tx, entered_rx) = oneshot::channel();
    let (release_tx, release_rx) = oneshot::channel();
    let first = {
        let generation = generation.clone();
        let witness = generation.clone();
        let starts = starts.clone();
        tokio::spawn(async move {
            generation
                .retire(move || async move {
                    starts.fetch_add(1, Ordering::SeqCst);
                    entered_tx.send(()).unwrap();
                    release_rx.await.unwrap();
                    witness.destroyed();
                    Ok(())
                })
                .await
        })
    };
    entered_rx.await.unwrap();
    assert!(!generation.is_live());
    assert!(!generation.replacement_ready().unwrap());
    assert!(!generation.destruction_observed());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    let mut sibling = Box::pin(generation.retire(|| async { panic!("duplicate retirement task") }));
    assert!(futures_util::poll!(&mut sibling).is_pending());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    release_tx.send(()).unwrap();
    first.await.unwrap().unwrap();
    sibling.await.unwrap();
    assert_eq!(starts.load(Ordering::SeqCst), 1);
    assert!(generation.replacement_ready().unwrap());
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    assert_eq!(control.usage().count(ControlKind::Waiter), 0);
}

#[tokio::test]
async fn abandoning_a_waiter_does_not_abandon_the_same_retirement_join() {
    let control = CoreControl::default();
    let generation = generation(&control, 1);
    let (release_tx, release_rx) = oneshot::channel();
    let witness = generation.clone();
    let mut abandoned = Box::pin(generation.retire(move || async move {
        release_rx.await.unwrap();
        witness.destroyed();
        Ok(())
    }));
    assert!(futures_util::poll!(&mut abandoned).is_pending());
    drop(abandoned);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert_eq!(control.usage().count(ControlKind::Waiter), 0);
    assert!(!generation.replacement_ready().unwrap());
    release_tx.send(()).unwrap();
    generation
        .retire(|| async { panic!("abandoned waiter must not respawn") })
        .await
        .unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test]
async fn store_destruction_is_not_task_join_and_replacement_waits_for_both() {
    let control = CoreControl::default();
    let generation = generation(&control, 1);
    let (destroyed_tx, destroyed_rx) = oneshot::channel();
    let (join_tx, join_rx) = oneshot::channel();
    let witness = generation.clone();
    let mut waiter = Box::pin(generation.retire(move || async move {
        witness.destroyed();
        destroyed_tx.send(()).unwrap();
        join_rx.await.unwrap();
        Ok(())
    }));
    assert!(futures_util::poll!(&mut waiter).is_pending());
    destroyed_rx.await.unwrap();
    assert!(generation.destruction_observed());
    assert!(!generation.replacement_ready().unwrap());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    join_tx.send(()).unwrap();
    waiter.await.unwrap();
    assert!(generation.replacement_ready().unwrap());
}

#[tokio::test]
async fn old_generation_cleanup_cannot_run_a_replacement_target() {
    let control = CoreControl::default();
    let old = generation(&control, 7);
    old.destroyed();
    assert!(old.replacement_ready().unwrap());
    let replacement = generation(&control, 8);
    let forbidden_target = replacement.clone();
    old.retire(move || async move {
        forbidden_target.destroyed();
        panic!("old joined receipt must not look up a replacement target")
    })
    .await
    .unwrap();
    assert!(replacement.is_live());
    assert!(!replacement.destruction_observed());
    assert!(!old.matches(&old.pack, &old.hash, replacement.number()));
    assert!(!old.matches(
        &PackId::parse("local.other").unwrap(),
        &old.hash,
        old.number()
    ));
    assert!(!old.matches(
        &old.pack,
        &PackHash::new(Digest::hash(b"other")),
        old.number()
    ));
}

#[tokio::test]
async fn task_success_without_owned_store_destruction_is_not_cleanup_success() {
    let generation = generation(&CoreControl::default(), 1);
    assert!(generation.retire(|| async { Ok(()) }).await.is_err());
    assert!(!generation.replacement_ready().unwrap());
    assert!(
        generation
            .retire(|| async { panic!("unknown must not retry") })
            .await
            .is_err()
    );
}

#[tokio::test]
async fn failed_or_panicked_tasks_never_publish_success_even_after_store_drop() {
    for panic_task in [false, true] {
        let generation = generation(&CoreControl::default(), 1);
        let witness = generation.clone();
        assert!(
            generation
                .retire(move || async move {
                    witness.destroyed();
                    if panic_task {
                        panic!("retirement task failure");
                    }
                    bail!("retirement task failure")
                })
                .await
                .is_err()
        );
        assert!(generation.destruction_observed());
        assert!(!generation.replacement_ready().unwrap());
        assert!(generation.retire(|| async { Ok(()) }).await.is_err());
    }
}

#[tokio::test]
async fn retirement_timeout_keeps_the_task_and_original_deadline() {
    let control = CoreControl::default();
    let generation = generation(&control, 1);
    let (release_tx, release_rx) = oneshot::channel();
    let witness = generation.clone();
    let mut first = Box::pin(generation.retire(move || async move {
        release_rx.await.unwrap();
        witness.destroyed();
        Ok(())
    }));
    assert!(futures_util::poll!(&mut first).is_pending());
    drop(first);
    // Force the already-created deadline to expire, without sleeping or
    // changing the production grace. The actual task remains blocked.
    let expired = Instant::now();
    {
        let mut state = generation.state.lock().unwrap();
        let State::Retiring { deadline, .. } = &mut *state else {
            panic!()
        };
        *deadline = expired;
    }
    assert!(
        generation
            .retire(|| async { panic!("timeout must not respawn") })
            .await
            .is_err()
    );
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert!(!generation.replacement_ready().unwrap());
    {
        let state = generation.state.lock().unwrap();
        let State::Retiring { deadline, .. } = &*state else {
            panic!()
        };
        assert_eq!(*deadline, expired);
    }
    let actual_join = completion(&generation);
    release_tx.send(()).unwrap();
    assert_eq!(actual_join.await, Ok(()));
    assert!(generation.replacement_ready().unwrap());
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    // This shared physical proof does not mutate any per-handle unknown receipt.
}

#[tokio::test]
async fn control_failure_does_not_spawn_or_advertise_cleanup_and_no_store_is_not_a_receipt() {
    let control = CoreControl::new(CoreControlLimits {
        max_tasks: 0,
        ..CoreControlLimits::default()
    })
    .unwrap();
    let blocked = generation(&control, 1);
    assert!(
        blocked
            .retire(|| async { panic!("unadmitted task") })
            .await
            .is_err()
    );
    assert!(!blocked.replacement_ready().unwrap());
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    let never_started = generation(&CoreControl::default(), 1);
    never_started.unstarted();
    assert!(never_started.replacement_ready().unwrap());
    assert!(!never_started.destruction_observed());
    assert!(
        never_started
            .retire(|| async { panic!("no actual Store") })
            .await
            .is_err()
    );
}

#[tokio::test]
async fn waiter_admission_failure_retains_the_actual_task_until_a_join() {
    let control = CoreControl::new(CoreControlLimits {
        max_waiters: 0,
        ..CoreControlLimits::default()
    })
    .unwrap();
    let generation = generation(&control, 1);
    let witness = generation.clone();
    let (release_tx, release_rx) = oneshot::channel();
    assert!(
        generation
            .retire(move || async move {
                release_rx.await.unwrap();
                witness.destroyed();
                Ok(())
            })
            .await
            .is_err()
    );
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert!(!generation.replacement_ready().unwrap());
    let actual_join = completion(&generation);
    release_tx.send(()).unwrap();
    assert_eq!(actual_join.await, Ok(()));
    assert!(generation.replacement_ready().unwrap());
}

#[test]
fn generation_control_is_prepaid_and_zero_is_not_a_generation() {
    let control = CoreControl::new(CoreControlLimits {
        max_registrations: 0,
        ..CoreControlLimits::default()
    })
    .unwrap();
    assert!(
        GenerationRetirement::new(
            &control,
            &PackId::parse("local.test").unwrap(),
            &PackHash::new(Digest::hash(b"test")),
            1
        )
        .is_err()
    );
    let control = CoreControl::default();
    assert!(
        GenerationRetirement::new(
            &control,
            &PackId::parse("local.test").unwrap(),
            &PackHash::new(Digest::hash(b"test")),
            0
        )
        .is_err()
    );
    assert_eq!(control.usage().count(ControlKind::Registration), 0);
    let generation = generation(&control, 1);
    assert_eq!(control.usage().count(ControlKind::Registration), 1);
    drop(generation);
    assert_eq!(control.usage().count(ControlKind::Registration), 0);
}

fn entry_context() -> tobkiri_gate::CallCx {
    use tobkiri_canon::ids::{ProfileId, RequestId};
    use tobkiri_gate::{CoreCancellation, Principal, ResourceLimits};
    tobkiri_gate::CallCx::root(
        ProfileId::parse("retirement-entry-profile").unwrap(),
        Principal::Pack {
            pack: PackId::parse("local.retirement-test").unwrap(),
            hash: PackHash::new(Digest::hash(b"retirement-test")),
        },
        RequestId::parse("retirement-entry-request").unwrap(),
        CoreCancellation::new(),
        std::time::Instant::now() + std::time::Duration::from_secs(5),
        ResourceLimits {
            fuel_per_call: 100_000,
            memory_mib: 8,
            wall_ms: 5_000,
            table_elements: 1024,
            output_bytes: 1024,
        },
    )
}

struct EntryDrop(Arc<AtomicUsize>);
impl Drop for EntryDrop {
    fn drop(&mut self) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}
struct EntrySlot {
    generation: Arc<GenerationRetirement>,
    store: Option<EntryDrop>,
    external_lease: Option<EntryDrop>,
    prepares: usize,
}
impl EntrySlot {
    fn drop_owned(&mut self) {
        let had_store = self.store.is_some();
        drop(self.store.take());
        drop(self.external_lease.take());
        if had_store {
            self.generation.destroyed();
        }
    }
    // Fixed test payload bookkeeping corresponding to prepare_generation; the
    // lock/join loop and GenerationRetirement below are the production helpers.
    fn prepare(&mut self) -> Result<Option<Arc<GenerationRetirement>>> {
        self.prepares += 1;
        if self.store.is_some() && self.generation.is_live() {
            return Ok(None);
        }
        self.drop_owned();
        if self.generation.replacement_ready()? {
            Ok(None)
        } else {
            Ok(Some(self.generation.clone()))
        }
    }
}

#[tokio::test]
async fn entry_unlocks_for_the_exact_retirement_task_queued_behind_it() {
    let control = CoreControl::default();
    let old = generation(&control, 1);
    let store_drops = Arc::new(AtomicUsize::new(0));
    let lease_drops = Arc::new(AtomicUsize::new(0));
    let slot = Arc::new(tokio::sync::Mutex::new(EntrySlot {
        generation: old.clone(),
        store: Some(EntryDrop(store_drops.clone())),
        external_lease: Some(EntryDrop(lease_drops.clone())),
        prepares: 0,
    }));
    let cx = entry_context();
    let held = slot.lock().await;
    let mut entry = Box::pin(lock_for_entry(&slot, &cx, false, EntrySlot::prepare));
    assert!(futures_util::poll!(&mut entry).is_pending());
    // Entry is the first FIFO mutex waiter. The exact retirement task is
    // queued second, while both are blocked by the original active holder.
    let task_slot = slot.clone();
    let target = old.clone();
    let starts = Arc::new(AtomicUsize::new(0));
    let task_starts = starts.clone();
    let (queued_tx, queued_rx) = oneshot::channel();
    let mut retirement = Box::pin(old.retire(move || async move {
        task_starts.fetch_add(1, Ordering::SeqCst);
        let mut acquiring = Box::pin(task_slot.lock());
        assert!(futures_util::poll!(&mut acquiring).is_pending());
        queued_tx.send(()).unwrap();
        let mut instance = acquiring.await;
        if Arc::ptr_eq(&instance.generation, &target) {
            instance.drop_owned();
        }
        assert!(target.destruction_observed());
        Ok(())
    }));
    assert!(futures_util::poll!(&mut retirement).is_pending());
    queued_rx.await.unwrap();
    drop(held);
    // The real entry helper acquires first, drops both payload owners, releases
    // the mutex, and waits on the one retained task. It must not return Err.
    assert!(futures_util::poll!(&mut entry).is_pending());
    assert_eq!(store_drops.load(Ordering::SeqCst), 1);
    assert_eq!(lease_drops.load(Ordering::SeqCst), 1);
    let guard = entry.await.unwrap();
    assert_eq!(guard.prepares, 2);
    assert!(Arc::ptr_eq(&guard.generation, &old));
    assert!(old.replacement_ready().unwrap());
    assert_eq!(starts.load(Ordering::SeqCst), 1);
    drop(guard);
    retirement.await.unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    assert_eq!(control.usage().count(ControlKind::Waiter), 0);
}

#[tokio::test]
async fn entry_rechecks_the_current_generation_after_its_old_join() {
    let control = CoreControl::default();
    let old = generation(&control, 7);
    let new = generation(&control, 8);
    let store_drops = Arc::new(AtomicUsize::new(0));
    let lease_drops = Arc::new(AtomicUsize::new(0));
    let slot = Arc::new(tokio::sync::Mutex::new(EntrySlot {
        generation: old.clone(),
        store: Some(EntryDrop(store_drops.clone())),
        external_lease: Some(EntryDrop(lease_drops.clone())),
        prepares: 0,
    }));
    let (release_tx, release_rx) = oneshot::channel();
    let target = old.clone();
    let mut retiring = Box::pin(old.retire(move || async move {
        release_rx.await.unwrap();
        assert!(target.destruction_observed());
        Ok(())
    }));
    assert!(futures_util::poll!(&mut retiring).is_pending());
    let cx = entry_context();
    let mut entry = Box::pin(lock_for_entry(&slot, &cx, false, EntrySlot::prepare));
    assert!(futures_util::poll!(&mut entry).is_pending());
    // Another entry can hold the free mutex while the first waits for join.
    let mut winner = slot
        .try_lock()
        .expect("entry must release the instance mutex");
    let shared_join = completion(&old);
    release_tx.send(()).unwrap();
    assert_eq!(shared_join.await, Ok(()));
    assert!(old.replacement_ready().unwrap());
    let new_store_drops = Arc::new(AtomicUsize::new(0));
    let new_lease_drops = Arc::new(AtomicUsize::new(0));
    winner.generation = new.clone();
    winner.store = Some(EntryDrop(new_store_drops.clone()));
    winner.external_lease = Some(EntryDrop(new_lease_drops.clone()));
    assert!(futures_util::poll!(&mut entry).is_pending());
    drop(winner);
    let guard = entry.await.unwrap();
    assert!(Arc::ptr_eq(&guard.generation, &new));
    assert_eq!(guard.prepares, 2);
    assert!(new.is_live());
    assert_eq!(new_store_drops.load(Ordering::SeqCst), 0);
    assert_eq!(new_lease_drops.load(Ordering::SeqCst), 0);
    drop(guard);
    retiring.await.unwrap();
    // A late observer of old g never selects the new slot/generation for work.
    old.retire(|| async { panic!("old cleanup must not target new g") })
        .await
        .unwrap();
    assert!(new.is_live());
    assert!(!new.destruction_observed());
}

#[tokio::test]
async fn entry_expired_join_is_not_renewed_or_falsely_succeeded() {
    let control = CoreControl::default();
    let old = generation(&control, 1);
    let (release_tx, release_rx) = oneshot::channel();
    let witness = old.clone();
    let mut retirement = Box::pin(old.retire(move || async move {
        release_rx.await.unwrap();
        witness.destroyed();
        Ok(())
    }));
    assert!(futures_util::poll!(&mut retirement).is_pending());
    drop(retirement);
    let original_expired = Instant::now();
    {
        let mut state = old.state.lock().unwrap();
        let State::Retiring { deadline, .. } = &mut *state else {
            panic!()
        };
        *deadline = original_expired;
    }
    let slot = tokio::sync::Mutex::new(old.clone());
    let cx = entry_context();
    assert!(
        lock_for_entry(&slot, &cx, false, |g| Ok(Some(g.clone())))
            .await
            .is_err()
    );
    assert!(slot.try_lock().is_ok());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert!(!old.replacement_ready().unwrap());
    {
        let state = old.state.lock().unwrap();
        let State::Retiring { deadline, .. } = &*state else {
            panic!()
        };
        assert_eq!(*deadline, original_expired);
    }
    let physical_join = completion(&old);
    release_tx.send(()).unwrap();
    assert_eq!(physical_join.await, Ok(()));
    assert!(old.replacement_ready().unwrap());
    // The earlier failed entry remains failed. No per-handle receipt is edited.
}

#[tokio::test]
async fn entry_cancellation_retains_the_task_and_releases_the_mutex_and_waiter() {
    let control = CoreControl::default();
    let old = generation(&control, 1);
    let (release_tx, release_rx) = oneshot::channel();
    let witness = old.clone();
    let mut retiring = Box::pin(old.retire(move || async move {
        release_rx.await.unwrap();
        witness.destroyed();
        Ok(())
    }));
    assert!(futures_util::poll!(&mut retiring).is_pending());
    drop(retiring);
    let slot = tokio::sync::Mutex::new(old.clone());
    let cx = entry_context();
    let mut entry = Box::pin(lock_for_entry(&slot, &cx, false, |g| Ok(Some(g.clone()))));
    assert!(futures_util::poll!(&mut entry).is_pending());
    assert_eq!(control.usage().count(ControlKind::Waiter), 1);
    cx.cancel().cancel();
    assert!(entry.await.is_err());
    assert!(slot.try_lock().is_ok());
    assert_eq!(control.usage().count(ControlKind::Waiter), 0);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    let physical_join = completion(&old);
    release_tx.send(()).unwrap();
    assert_eq!(physical_join.await, Ok(()));
    assert!(old.replacement_ready().unwrap());
}

#[tokio::test]
async fn entry_reentrant_busy_and_original_expiry_do_not_prepare_or_spawn() {
    let control = CoreControl::default();
    let old = generation(&control, 1);
    let slot = tokio::sync::Mutex::new(old.clone());
    let held = slot.lock().await;
    let cx = entry_context();
    let error = lock_for_entry(&slot, &cx, true, |_| panic!("busy nested prepare"))
        .await
        .err()
        .expect("nested entry remains immediate conflict");
    assert_eq!(
        error
            .downcast_ref::<tobkiri_flow::StepError>()
            .unwrap()
            .code,
        "conflict"
    );
    drop(held);
    let expired = entry_context().cap_deadline(std::time::Instant::now());
    let error = lock_for_entry(&slot, &expired, false, |_| panic!("expired entry prepare"))
        .await
        .err()
        .expect("original request deadline remains binding");
    assert!(matches!(
        error.downcast_ref::<tobkiri_gate::GateError>(),
        Some(tobkiri_gate::GateError::Deadline)
    ));
    assert!(old.is_live());
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test]
async fn join_existing_cannot_start_cleanup_or_turn_failed_join_into_success() {
    let control = CoreControl::default();
    let live = generation(&control, 1);
    assert!(live.join_existing().await.is_err());
    assert!(live.is_live());
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    let failed = generation(&control, 2);
    let witness = failed.clone();
    assert!(
        failed
            .retire(move || async move {
                witness.destroyed();
                bail!("actual task failure after physical destruction")
            })
            .await
            .is_err()
    );
    assert!(failed.destruction_observed());
    assert!(failed.join_existing().await.is_err());
    let slot = tokio::sync::Mutex::new(failed.clone());
    assert!(
        lock_for_entry(&slot, &entry_context(), false, |g| Ok(Some(g.clone())))
            .await
            .is_err()
    );
    assert!(!failed.replacement_ready().unwrap());
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test]
async fn entry_waiter_quota_failure_keeps_one_existing_task_without_dispatch() {
    let control = CoreControl::new(CoreControlLimits {
        max_waiters: 0,
        ..CoreControlLimits::default()
    })
    .unwrap();
    let old = generation(&control, 1);
    let (release_tx, release_rx) = oneshot::channel();
    let witness = old.clone();
    assert!(
        old.retire(move || async move {
            release_rx.await.unwrap();
            witness.destroyed();
            Ok(())
        })
        .await
        .is_err()
    );
    let slot = tokio::sync::Mutex::new(old.clone());
    assert!(
        lock_for_entry(&slot, &entry_context(), false, |g| Ok(Some(g.clone())))
            .await
            .is_err()
    );
    assert!(slot.try_lock().is_ok());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert_eq!(control.usage().count(ControlKind::Waiter), 0);
    let physical_join = completion(&old);
    release_tx.send(()).unwrap();
    assert_eq!(physical_join.await, Ok(()));
    assert!(old.replacement_ready().unwrap());
}
