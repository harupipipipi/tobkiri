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
