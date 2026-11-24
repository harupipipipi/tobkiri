//! Native callback-port fixtures, not actual Python Context/worker-pool proof.
use super::*;
use std::sync::{
    Arc, Barrier,
    atomic::{AtomicUsize, Ordering},
    mpsc,
};
use std::thread;
use std::time::Duration;
use tobkiri_gate::{CapacityLedger, CoreControl, NATIVE_SETUP_MEMORY_BYTES};
use tobkiri_store::DataRoot;

const ISOLATION: Duration = Duration::from_secs(5);
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
fn ready<T>(future: &NativeJobFuture<T>) -> NativeJobResult<T> {
    match future.wait_until(Instant::now() + ISOLATION).unwrap() {
        NativeJobObservation::Ready(value) => value,
        _ => panic!("expected the retained result"),
    }
}

#[test]
fn active_owner_fence_releases_caller_but_holds_work_and_admission_until_return() {
    let (_temporary, _root, _control, _ledger, account) = actual();
    let operation = account.begin().unwrap();
    let original = operation.deadline().unwrap();
    let jobs = NativeJobs::<u64>::new(&account, 1).unwrap();
    let owner = jobs.new_owner().unwrap();
    let (future, ticket) = jobs.submit(&owner, &operation, original).unwrap();
    let (entered_tx, entered_rx) = mpsc::channel();
    let (finish_tx, finish_rx) = mpsc::channel();
    let (done_tx, done_rx) = mpsc::channel();
    let worker = thread::spawn(move || {
        ticket.run(|context| {
            assert_eq!(context.operation().deadline().unwrap(), original);
            context.checkpoint().unwrap();
            entered_tx.send(()).unwrap();
            finish_rx.recv_timeout(ISOLATION).unwrap();
            assert_eq!(context.checkpoint(), Err(NativeJobFailure::Cancelled));
            Ok(17)
        });
        done_tx.send(()).unwrap();
    });
    entered_rx.recv_timeout(ISOLATION).unwrap();
    assert_eq!(
        jobs.snapshot().unwrap(),
        NativeJobsSnapshot {
            admitted: 1,
            queued: 0,
            running: 1
        }
    );
    jobs.fence(&owner).unwrap();
    assert!(matches!(
        future.wait_until(Instant::now() + ISOLATION).unwrap(),
        NativeJobObservation::Cancelled
    ));
    assert_eq!(jobs.snapshot().unwrap().admitted, 1);
    assert!(!worker.is_finished());
    assert!(matches!(
        jobs.submit(&owner, &operation, original),
        Err(GateError::Cancelled)
    ));
    finish_tx.send(()).unwrap();
    done_rx.recv_timeout(ISOLATION).unwrap();
    worker.join().unwrap();
    assert_eq!(jobs.snapshot().unwrap().admitted, 0);
    assert!(matches!(
        future.wait_until(Instant::now()).unwrap(),
        NativeJobObservation::Cancelled
    ));
    assert_eq!(operation.deadline().unwrap(), original);
    drop(future);
    drop(owner);
    drop(jobs);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn queued_cancel_is_drained_without_callback_then_new_owner_and_new_job_reuse_slots() {
    let (_temporary, _root, _control, _ledger, account) = actual();
    let operation = account.begin().unwrap();
    let jobs = NativeJobs::<u64>::new(&account, 2).unwrap();
    let old_owner = jobs.new_owner().unwrap();
    let (first, first_ticket) = jobs
        .submit(&old_owner, &operation, operation.deadline().unwrap())
        .unwrap();
    let (second, second_ticket) = jobs
        .submit(&old_owner, &operation, operation.deadline().unwrap())
        .unwrap();
    assert!(!std::ptr::eq(first.0.get(), second.0.get()));
    // Deterministically fence AFTER genuine root/clock validation but BEFORE
    // callback-start admission. The production run path consumes this same
    // private boundary; no fake owner/clock/checkpoint result is supplied.
    let context = NativeJobContext {
        job: &first_ticket.job,
    };
    let prepared = context.prepare_start();
    jobs.fence(&old_owner).unwrap();
    assert!(!prepared.enter());
    assert_eq!(
        jobs.snapshot().unwrap(),
        NativeJobsSnapshot {
            admitted: 2,
            queued: 2,
            running: 0
        }
    );
    first_ticket.run(|_| panic!("cancelled queued callback must not execute"));
    assert_eq!(jobs.snapshot().unwrap().admitted, 1);
    second_ticket.run(|_| panic!("cancelled queued callback must not execute"));
    assert_eq!(jobs.snapshot().unwrap().admitted, 0);
    assert!(matches!(
        first.wait_until(Instant::now()).unwrap(),
        NativeJobObservation::Cancelled
    ));
    assert!(matches!(
        second.wait_until(Instant::now()).unwrap(),
        NativeJobObservation::Cancelled
    ));
    let new_owner = jobs.new_owner().unwrap();
    let (new, new_ticket) = jobs
        .submit(&new_owner, &operation, operation.deadline().unwrap())
        .unwrap();
    assert!(!std::ptr::eq(first.0.get(), new.0.get()));
    new_ticket.run(|context| {
        context.checkpoint()?;
        Ok(29)
    });
    assert_eq!(*ready(&new).get(), 29);
    assert_eq!(jobs.snapshot().unwrap().admitted, 0);
    drop(first);
    drop(second);
    drop(new);
    drop(old_owner);
    drop(new_owner);
    drop(jobs);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn result_views_retain_same_inline_value_and_funding_after_slot_and_outer_owners_leave() {
    let (_temporary, root, _control, ledger, account) = actual();
    let operation = account.begin().unwrap();
    let jobs = NativeJobs::<u64>::new(&account, 1).unwrap();
    let owner = jobs.new_owner().unwrap();
    let (future, ticket) = jobs
        .submit(&owner, &operation, operation.deadline().unwrap())
        .unwrap();
    let other = future.clone();
    ticket.run(|_| Ok(41));
    assert_eq!(jobs.snapshot().unwrap().admitted, 0);
    let first = ready(&future);
    let second = ready(&other);
    assert!(std::ptr::eq(first.get(), second.get()));
    assert!(!future.cancel().unwrap());
    jobs.fence(&owner).unwrap();
    assert_eq!(*ready(&future).get(), 41);
    drop(future);
    drop(other);
    drop(owner);
    drop(jobs);
    drop(account);
    drop(ledger);
    drop(root);
    assert_eq!(*first.get(), 41);
    assert!(operation.capacity_snapshot().unwrap().charged_bytes > 0);
    drop(first);
    assert!(operation.capacity_snapshot().unwrap().charged_bytes > 0);
    drop(second);
    assert_eq!(operation.capacity_snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn concurrent_fence_and_submit_leave_no_unfenced_old_owner_job() {
    let (_temporary, _root, _control, _ledger, account) = actual();
    let operation = account.begin().unwrap();
    let jobs = NativeJobs::<u64>::new(&account, 1).unwrap();
    for _ in 0..16 {
        let owner = jobs.new_owner().unwrap();
        let barrier = Arc::new(Barrier::new(3));
        let fence_barrier = barrier.clone();
        let fence_jobs = jobs.clone();
        let fence_owner = owner.clone();
        let fence = thread::spawn(move || {
            fence_barrier.wait();
            fence_jobs.fence(&fence_owner).unwrap();
        });
        let submit_barrier = barrier.clone();
        let submit_jobs = jobs.clone();
        let submit_owner = owner.clone();
        let submit_operation = operation.clone();
        let submit = thread::spawn(move || {
            submit_barrier.wait();
            submit_jobs.submit(
                &submit_owner,
                &submit_operation,
                submit_operation.deadline().unwrap(),
            )
        });
        barrier.wait();
        fence.join().unwrap();
        match submit.join().unwrap() {
            Ok((future, ticket)) => {
                assert!(matches!(
                    future.wait_until(Instant::now()).unwrap(),
                    NativeJobObservation::Cancelled
                ));
                ticket.run(|_| panic!("fenced queued work must not execute"));
            }
            Err(GateError::Cancelled) => {}
            Err(error) => panic!("unexpected submit result: {error}"),
        }
        assert_eq!(jobs.snapshot().unwrap().admitted, 0);
    }
    drop(jobs);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

// These Arcs are fixture instrumentation, not a proof of nested production
// payload funding. Drop calls snapshot to detect destruction under either lock.
struct DropProbe {
    jobs: NativeJobs<DropProbe>,
    drops: Arc<AtomicUsize>,
}
impl Drop for DropProbe {
    fn drop(&mut self) {
        assert!(self.jobs.snapshot().unwrap().admitted <= 1);
        self.drops.fetch_add(1, Ordering::AcqRel);
    }
}

#[test]
fn cancel_and_publish_have_one_winner_and_result_destruction_is_outside_locks() {
    let (_temporary, _root, _control, _ledger, account) = actual();
    let operation = account.begin().unwrap();
    let jobs = NativeJobs::<DropProbe>::new(&account, 1).unwrap();
    for _ in 0..16 {
        let owner = jobs.new_owner().unwrap();
        let (future, ticket) = jobs
            .submit(&owner, &operation, operation.deadline().unwrap())
            .unwrap();
        let boundary = Arc::new(Barrier::new(2));
        let worker_boundary = boundary.clone();
        let drops = Arc::new(AtomicUsize::new(0));
        let result = DropProbe {
            jobs: jobs.clone(),
            drops: drops.clone(),
        };
        let (done_tx, done_rx) = mpsc::channel();
        let worker = thread::spawn(move || {
            ticket.run(|_| {
                worker_boundary.wait();
                Ok(result)
            });
            done_tx.send(()).unwrap();
        });
        boundary.wait();
        let cancelled = future.cancel().unwrap();
        done_rx.recv_timeout(ISOLATION).unwrap();
        worker.join().unwrap();
        assert_eq!(jobs.snapshot().unwrap().admitted, 0);
        match future.wait_until(Instant::now()).unwrap() {
            NativeJobObservation::Ready(view) => {
                assert!(!cancelled);
                assert_eq!(drops.load(Ordering::Acquire), 0);
                assert!(!future.cancel().unwrap());
                jobs.fence(&owner).unwrap();
                drop(view);
            }
            NativeJobObservation::Cancelled => assert!(cancelled),
            _ => panic!("publication/cancel must have one terminal winner"),
        }
        drop(future);
        drop(owner);
        assert_eq!(drops.load(Ordering::Acquire), 1);
    }
    drop(jobs);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn callback_panic_waits_for_scoped_cleanup_before_slot_recovery() {
    let (_temporary, _root, _control, _ledger, account) = actual();
    let operation = account.begin().unwrap();
    let jobs = NativeJobs::<u64>::new(&account, 1).unwrap();
    let owner = jobs.new_owner().unwrap();
    let (future, ticket) = jobs
        .submit(&owner, &operation, operation.deadline().unwrap())
        .unwrap();
    let (entered_tx, entered_rx) = mpsc::channel();
    let (finish_tx, finish_rx) = mpsc::channel();
    let worker = thread::spawn(move || {
        ticket.run(|_| {
            thread::scope(|scope| {
                scope.spawn(move || {
                    entered_tx.send(()).unwrap();
                    finish_rx.recv_timeout(ISOLATION).unwrap();
                });
                panic!("actual native callback panic");
            })
        })
    });
    entered_rx.recv_timeout(ISOLATION).unwrap();
    jobs.fence(&owner).unwrap();
    assert!(matches!(
        future.wait_until(Instant::now()).unwrap(),
        NativeJobObservation::Cancelled
    ));
    assert_eq!(jobs.snapshot().unwrap().admitted, 1);
    assert!(!worker.is_finished());
    finish_tx.send(()).unwrap();
    let panic = worker.join().expect_err("original Rust panic must escape");
    assert_eq!(
        panic.downcast_ref::<&str>(),
        Some(&"actual native callback panic")
    );
    assert_eq!(jobs.snapshot().unwrap().admitted, 0);
    drop(owner);
    let owner = jobs.new_owner().unwrap();
    let (uncancelled, ticket) = jobs
        .submit(&owner, &operation, operation.deadline().unwrap())
        .unwrap();
    let panic = thread::spawn(move || ticket.run(|_| panic!("uncancelled callback panic"))).join();
    assert!(panic.is_err());
    assert!(matches!(
        uncancelled.wait_until(Instant::now()).unwrap(),
        NativeJobObservation::Failed(NativeJobFailure::CallbackPanicked)
    ));
    assert_eq!(jobs.snapshot().unwrap().admitted, 0);
    drop(uncancelled);
    drop(future);
    drop(owner);
    drop(jobs);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn real_account_and_namespace_admission_rollback_and_explicit_queue_discard() {
    let (_t1, _r1, control, _l1, account) = actual();
    let (_t2, _r2, _c2, _l2, other) = actual();
    let operation = account.begin().unwrap();
    let foreign_operation = other.begin().unwrap();
    assert_eq!(operation.provenance(), foreign_operation.provenance());
    let jobs = NativeJobs::<u64>::new(&account, 1).unwrap();
    let owner = jobs.new_owner().unwrap();
    let another = NativeJobs::<u64>::new(&account, 1).unwrap();
    let wrong_namespace = another.new_owner().unwrap();
    assert!(matches!(
        jobs.submit(&wrong_namespace, &operation, operation.deadline().unwrap()),
        Err(GateError::Denied(_))
    ));
    assert!(matches!(
        jobs.submit(
            &owner,
            &foreign_operation,
            foreign_operation.deadline().unwrap()
        ),
        Err(GateError::Denied(_))
    ));
    drop(wrong_namespace);
    drop(another);
    let used = account.snapshot().unwrap().charged_bytes;
    let exhausted = operation
        .reserve(CapacityKind::VfsBuffer, NATIVE_SETUP_MEMORY_BYTES - used)
        .unwrap();
    let before = account.snapshot().unwrap();
    let metadata = control.usage().bytes;
    assert!(matches!(
        jobs.submit(&owner, &operation, operation.deadline().unwrap()),
        Err(GateError::Limit(_))
    ));
    assert_eq!(account.snapshot().unwrap(), before);
    assert_eq!(control.usage().bytes, metadata);
    assert_eq!(jobs.snapshot().unwrap().admitted, 0);
    drop(exhausted);
    let (future, ticket) = jobs
        .submit(&owner, &operation, operation.deadline().unwrap())
        .unwrap();
    assert!(matches!(
        jobs.submit(&owner, &operation, operation.deadline().unwrap()),
        Err(GateError::Limit(_))
    ));
    // Explicit unexecuted queue discard is defined. It proves neither the
    // original pool's shutdown policy nor physical completion of active work.
    drop(ticket);
    assert_eq!(jobs.snapshot().unwrap().admitted, 0);
    assert!(matches!(
        future.wait_until(Instant::now()).unwrap(),
        NativeJobObservation::Cancelled
    ));
    drop(future);
    drop(owner);
    drop(jobs);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}
