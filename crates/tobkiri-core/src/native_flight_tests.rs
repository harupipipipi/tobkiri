//! Real supported-platform coordination; no fake root/account/operation proof.
use super::*;
use std::sync::{
    Arc, Barrier,
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use std::thread::{self, JoinHandle};
use std::time::Duration;
use tobkiri_gate::{CapacityLedger, CoreControl, NATIVE_SETUP_MEMORY_BYTES, NATIVE_SETUP_WALL};
use tobkiri_store::DataRoot;

const ISOLATION: Duration = Duration::from_secs(5);
type NativeWork = JoinHandle<Result<NativeFlightJoin<u64>, GateError>>;

fn actual() -> (
    tempfile::TempDir,
    DataRoot,
    CoreControl,
    CapacityLedger,
    NativeSetupAccount,
) {
    let temporary = tempfile::tempdir().unwrap();
    let root = DataRoot::open(temporary.path().canonicalize().unwrap().join("root")).unwrap();
    let control = CoreControl::default();
    let lease = root.native_setup_owner_lease(&control).unwrap();
    let ledger = CapacityLedger::new();
    let account = ledger
        .native_setup_account(&control, || Ok(Arc::new(lease)))
        .unwrap();
    (temporary, root, control, ledger, account)
}

fn waiter<T>(joined: NativeFlightJoin<T>) -> NativeFlightWaiter {
    match joined {
        NativeFlightJoin::Waiter(waiter) => waiter,
        NativeFlightJoin::Leader(_) => panic!("expected in-progress work"),
    }
}

fn start(
    flights: &NativeFlights,
    operation: &NativeSetupInvocation,
    key: &'static [u8],
) -> (mpsc::Sender<()>, NativeWork) {
    let flights = flights.clone();
    let operation = operation.clone();
    let (ready_tx, ready_rx) = mpsc::channel();
    let (finish_tx, finish_rx) = mpsc::channel();
    let work = thread::spawn(move || {
        flights.run_or_join(&operation, key, || {
            ready_tx.send(()).unwrap();
            finish_rx.recv_timeout(ISOLATION).unwrap();
            17
        })
    });
    ready_rx.recv_timeout(ISOLATION).unwrap();
    (finish_tx, work)
}

#[test]
fn two_actual_operations_join_without_renewal_and_timeout_does_not_finish_work() {
    let (_temporary, _root, _control, _ledger, account) = actual();
    let first = account.begin().unwrap();
    let second = account.begin().unwrap();
    let deadlines = (first.deadline().unwrap(), second.deadline().unwrap());
    let flights = NativeFlights::new(&account).unwrap();
    let (finish, work) = start(&flights, &first, b"opaque-key");
    let waiting = waiter(flights.run_or_join(&second, b"opaque-key", || 99).unwrap());
    assert_eq!(held(&waiting.flight.get().state).waiters, 1);
    assert_eq!(
        waiting.wait_until(Instant::now()).unwrap(),
        NativeFlightObservation::TimedOut
    );
    assert!(!work.is_finished());
    assert_eq!(held(&flights.0.get().registry).flights.len(), 1);
    let abandoned = waiter(flights.run_or_join(&second, b"opaque-key", || 99).unwrap());
    let observer = abandoned.flight.clone();
    drop(abandoned);
    assert_eq!(held(&observer.get().state).waiters, 0);
    let notified = waiter(flights.run_or_join(&second, b"opaque-key", || 99).unwrap());
    finish.send(()).unwrap();
    assert!(matches!(
        work.join().unwrap().unwrap(),
        NativeFlightJoin::Leader(17)
    ));
    assert_eq!(
        notified.wait_until(Instant::now() + ISOLATION).unwrap(),
        NativeFlightObservation::Completed
    );
    // Original successful-waiter accounting is deliberately not a refcount.
    assert_eq!(held(&observer.get().state).waiters, 1);
    assert_eq!(
        (first.deadline().unwrap(), second.deadline().unwrap()),
        deadlines
    );
    drop(observer);
    drop(flights);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    assert_eq!(account.snapshot().unwrap().reservations, 0);
}

#[test]
fn physical_work_and_old_waiter_retain_funds_after_outer_owner_departure() {
    let (_temporary, root, _control, ledger, account) = actual();
    let first = account.begin().unwrap();
    let second = account.begin().unwrap();
    let flights = NativeFlights::new(&account).unwrap();
    let (finish, work) = start(&flights, &first, b"held");
    let waiting = waiter(flights.run_or_join(&second, b"held", || 99).unwrap());
    drop(flights);
    drop(account);
    drop(ledger);
    drop(root);
    assert!(first.capacity_snapshot().unwrap().charged_bytes > 0);
    assert!(!work.is_finished());
    finish.send(()).unwrap();
    assert!(matches!(
        work.join().unwrap().unwrap(),
        NativeFlightJoin::Leader(17)
    ));
    // The real worker ended, but the old waiter still owns physical state.
    assert!(second.capacity_snapshot().unwrap().charged_bytes > 0);
    assert_eq!(
        waiting.wait_until(Instant::now() + ISOLATION).unwrap(),
        NativeFlightObservation::Completed
    );
    assert_eq!(second.capacity_snapshot().unwrap().charged_bytes, 0);
    assert_eq!(second.capacity_snapshot().unwrap().reservations, 0);
}

#[test]
fn completion_racing_same_key_registration_cannot_notify_the_new_flight() {
    let (_temporary, _root, _control, _ledger, account) = actual();
    let first = account.begin().unwrap();
    let second = account.begin().unwrap();
    let flights = NativeFlights::new(&account).unwrap();
    let (first_finish, first_work) = start(&flights, &first, b"same");
    let old = waiter(flights.run_or_join(&second, b"same", || 99).unwrap());
    let barrier = Arc::new(Barrier::new(2));
    let racing = barrier.clone();
    let candidate = flights.clone();
    let next_operation = account.begin().unwrap();
    let (started_tx, started_rx) = mpsc::channel();
    let (next_finish_tx, next_finish_rx) = mpsc::channel();
    let next_work = thread::spawn(move || {
        racing.wait();
        loop {
            match candidate
                .run_or_join(&next_operation, b"same", || {
                    started_tx.send(()).unwrap();
                    next_finish_rx.recv_timeout(ISOLATION).unwrap();
                    23
                })
                .unwrap()
            {
                NativeFlightJoin::Leader(value) => return value,
                NativeFlightJoin::Waiter(waiting) => assert_eq!(
                    waiting.wait_until(Instant::now() + ISOLATION).unwrap(),
                    NativeFlightObservation::Completed,
                ),
            }
        }
    });
    barrier.wait();
    first_finish.send(()).unwrap();
    started_rx.recv_timeout(ISOLATION).unwrap();
    let new = waiter(flights.run_or_join(&second, b"same", || 99).unwrap());
    assert!(!std::ptr::eq(old.flight.get(), new.flight.get()));
    assert_eq!(
        old.wait_until(Instant::now() + ISOLATION).unwrap(),
        NativeFlightObservation::Completed
    );
    assert_eq!(
        new.wait_until(Instant::now()).unwrap(),
        NativeFlightObservation::TimedOut
    );
    assert!(!next_work.is_finished());
    next_finish_tx.send(()).unwrap();
    assert_eq!(next_work.join().unwrap(), 23);
    assert!(matches!(
        first_work.join().unwrap().unwrap(),
        NativeFlightJoin::Leader(17)
    ));
    drop(flights);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn callback_unwind_joins_scoped_work_before_notification_and_preserves_panic() {
    let (_temporary, _root, _control, _ledger, account) = actual();
    let first = account.begin().unwrap();
    let second = account.begin().unwrap();
    let flights = NativeFlights::new(&account).unwrap();
    let worker_flights = flights.clone();
    let cleaned = Arc::new(AtomicBool::new(false));
    let cleaned_worker = cleaned.clone();
    let (ready_tx, ready_rx) = mpsc::channel();
    let (finish_tx, finish_rx) = mpsc::channel();
    let work = thread::spawn(move || {
        worker_flights.run_or_join(&first, b"panic", || {
            thread::scope(|scope| {
                scope.spawn(move || {
                    ready_tx.send(()).unwrap();
                    finish_rx.recv_timeout(ISOLATION).unwrap();
                    cleaned_worker.store(true, Ordering::Release);
                });
                panic!("actual callback panic");
            });
        })
    });
    ready_rx.recv_timeout(ISOLATION).unwrap();
    let waiting = waiter(flights.run_or_join(&second, b"panic", || ()).unwrap());
    let retained = waiting.flight.clone();
    assert_eq!(
        waiting.wait_until(Instant::now()).unwrap(),
        NativeFlightObservation::TimedOut
    );
    assert!(!cleaned.load(Ordering::Acquire));
    assert!(!held(&retained.get().state).completed);
    let observed = waiter(flights.run_or_join(&second, b"panic", || ()).unwrap());
    finish_tx.send(()).unwrap();
    let panic = work
        .join()
        .err()
        .expect("original callback panic must escape");
    assert_eq!(panic.downcast_ref::<&str>(), Some(&"actual callback panic"));
    assert!(cleaned.load(Ordering::Acquire));
    assert_eq!(
        observed.wait_until(Instant::now() + ISOLATION).unwrap(),
        NativeFlightObservation::Completed
    );
    drop(retained);
    drop(flights);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn capacity_denial_does_not_publish_work_or_change_existing_registration() {
    let (_temporary, _root, control, _ledger, account) = actual();
    let first = account.begin().unwrap();
    let second = account.begin().unwrap();
    let flights = NativeFlights::new(&account).unwrap();
    let (finish, work) = start(&flights, &first, b"existing");
    let used = account.snapshot().unwrap().charged_bytes;
    // Admission exhaustion, not a claim to allocate a 64 MiB test payload.
    let rest = second
        .reserve(CapacityKind::VfsBuffer, NATIVE_SETUP_MEMORY_BYTES - used)
        .unwrap();
    let before = account.snapshot().unwrap();
    let control_before = control.usage().bytes;
    let called = AtomicBool::new(false);
    assert!(matches!(
        flights.run_or_join(&second, b"new", || called.store(true, Ordering::Release)),
        Err(GateError::Limit(_))
    ));
    assert!(!called.load(Ordering::Acquire));
    assert_eq!(account.snapshot().unwrap(), before);
    assert_eq!(control.usage().bytes, control_before);
    assert_eq!(held(&flights.0.get().registry).flights.len(), 1);
    // An already-funded enrollment still works without allocating a new flight.
    let waiting = waiter(flights.run_or_join(&second, b"existing", || 99).unwrap());
    drop(waiting);
    drop(rest);
    // Permit the pending replacement vector, but deny the new flight. Its
    // coexisting allocation must be freed/refunded without replacing the old
    // registry. Peak accounting retains that real temporary occupancy.
    let replacement_bytes = 2 * size_of::<FlightRef>() as u64;
    let used = account.snapshot().unwrap().charged_bytes;
    let rest = second
        .reserve(
            CapacityKind::VfsBuffer,
            NATIVE_SETUP_MEMORY_BYTES - used - replacement_bytes,
        )
        .unwrap();
    let before = account.snapshot().unwrap();
    let control_before = control.usage().bytes;
    assert!(matches!(
        flights.run_or_join(&second, b"new", || 99),
        Err(GateError::Limit(_))
    ));
    let after = account.snapshot().unwrap();
    assert_eq!(after.charged_bytes, before.charged_bytes);
    assert_eq!(after.reservations, before.reservations);
    assert_eq!(control.usage().bytes, control_before);
    assert_eq!(held(&flights.0.get().registry).flights.capacity(), 1);
    drop(rest);
    finish.send(()).unwrap();
    assert!(matches!(
        work.join().unwrap().unwrap(),
        NativeFlightJoin::Leader(17)
    ));
    drop(flights);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn actual_account_namespace_and_exact_key_bytes_are_not_serialized_identity() {
    let (_t1, _r1, _c1, _l1, first) = actual();
    let (_t2, _r2, _c2, _l2, foreign) = actual();
    let own = first.begin().unwrap();
    let other = foreign.begin().unwrap();
    assert_eq!(own.provenance(), other.provenance());
    let flights = NativeFlights::new(&first).unwrap();
    assert!(matches!(
        flights.run_or_join(&other, b"key", || 1),
        Err(GateError::Denied(_))
    ));
    let (finish, work) = start(&flights, &own, b"key");
    let distinct_namespace = NativeFlights::new(&first).unwrap();
    assert!(matches!(
        distinct_namespace.run_or_join(&own, b"key", || 2).unwrap(),
        NativeFlightJoin::Leader(2)
    ));
    assert!(matches!(
        flights.run_or_join(&own, b"key\0", || 3).unwrap(),
        NativeFlightJoin::Leader(3)
    ));
    finish.send(()).unwrap();
    assert!(matches!(
        work.join().unwrap().unwrap(),
        NativeFlightJoin::Leader(17)
    ));
    drop(flights);
    drop(distinct_namespace);
    assert_eq!(first.snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn failed_work_keeps_its_actual_outcome_private_and_later_call_runs_again() {
    let (_temporary, _root, _control, _ledger, account) = actual();
    let first = account.begin().unwrap();
    let second = account.begin().unwrap();
    let flights = NativeFlights::new(&account).unwrap();
    let worker_flights = flights.clone();
    let (ready_tx, ready_rx) = mpsc::channel();
    let (finish_tx, finish_rx) = mpsc::channel();
    let worker = thread::spawn(move || {
        worker_flights.run_or_join(&first, b"failure", || {
            ready_tx.send(()).unwrap();
            finish_rx.recv_timeout(ISOLATION).unwrap();
            Err::<(), _>(Arc::new("original private error"))
        })
    });
    ready_rx.recv_timeout(ISOLATION).unwrap();
    let waiting = waiter(
        flights
            .run_or_join(&second, b"failure", || Ok::<_, ()>(()))
            .unwrap(),
    );
    finish_tx.send(()).unwrap();
    let NativeFlightJoin::Leader(Err(error)) = worker.join().unwrap().unwrap() else {
        panic!("leader must retain its original error outcome");
    };
    assert_eq!(Arc::strong_count(&error), 1);
    let weak = Arc::downgrade(&error);
    drop(error);
    assert!(weak.upgrade().is_none());
    assert_eq!(
        waiting.wait_until(Instant::now() + ISOLATION).unwrap(),
        NativeFlightObservation::Completed
    );
    assert!(matches!(
        flights
            .run_or_join(&second, b"failure", || Ok::<_, ()>(()))
            .unwrap(),
        NativeFlightJoin::Leader(Ok(()))
    ));
    drop(flights);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn real_original_deadline_caps_wait_but_never_relabels_running_work_as_finished() {
    let (_temporary, _root, _control, _ledger, account) = actual();
    let first = account.begin().unwrap();
    let second = account.begin().unwrap();
    let deadline = second.deadline().unwrap();
    let flights = NativeFlights::new(&account).unwrap();
    let worker_flights = flights.clone();
    let (ready_tx, ready_rx) = mpsc::channel();
    let (finish_tx, finish_rx) = mpsc::channel();
    let work = thread::spawn(move || {
        worker_flights.run_or_join(&first, b"original-clock", || {
            ready_tx.send(()).unwrap();
            // Harness isolation only: pure blocked work has no effects or
            // extra execution budget. Its custody must outlive caller expiry.
            finish_rx
                .recv_timeout(NATIVE_SETUP_WALL + ISOLATION)
                .unwrap();
            31
        })
    });
    ready_rx.recv_timeout(ISOLATION).unwrap();
    let waiting = waiter(
        flights
            .run_or_join(&second, b"original-clock", || 99)
            .unwrap(),
    );
    let retained = waiting.flight.clone();
    // Real elapsed time, without a fake clock or changing the original30s.
    assert_eq!(
        waiting.wait_until(deadline + ISOLATION).unwrap(),
        NativeFlightObservation::TimedOut
    );
    assert!(Instant::now() >= deadline);
    assert_eq!(second.deadline().unwrap(), deadline);
    assert!(!work.is_finished());
    assert!(!held(&retained.get().state).completed);
    assert_eq!(held(&flights.0.get().registry).flights.len(), 1);
    assert!(matches!(
        flights.run_or_join(&second, b"late", || 99),
        Err(GateError::Deadline)
    ));
    finish_tx.send(()).unwrap();
    assert!(matches!(
        work.join().unwrap().unwrap(),
        NativeFlightJoin::Leader(31)
    ));
    assert!(held(&retained.get().state).completed);
    drop(retained);
    drop(flights);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}
