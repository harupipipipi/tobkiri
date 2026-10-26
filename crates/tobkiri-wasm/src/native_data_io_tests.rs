//! Portable spawn_blocking/join custody tests.
//! The file/control probe checks source-FD and ControlReservation lifetime only;
//! it does not prove native-root, APFS capture, fixed-range, or host-ABI behavior.
//! Those positive checks require the separate hosted-macOS tests.
use super::*;
use std::fs::File;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use tobkiri_canon::{CoreBuffer, Digest, LeasedShared};
use tobkiri_gate::{
    CapacityLedger, ControlKind, ControlReservation, CoreControl, GateError, NativeSetupAccount,
    NativeSetupLease,
};

struct Proof;
impl NativeSetupLease for Proof {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }

    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"native-data-io-portable-owner-not-os-proof")
    }
}

fn funding() -> (NativeSetupAccount, NativeSetupInvocation) {
    let control = CoreControl::default();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(Proof)))
        .unwrap();
    let invocation = account.begin().unwrap();
    (account, invocation)
}

fn setup() -> (
    NativeSetupAccount,
    NativeSetupInvocation,
    crate::WasmEngine,
    crate::NativeSetupStore<()>,
    NativeDataIo,
) {
    let (account, invocation) = funding();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let mut store = engine.native_setup_store(invocation.clone(), ()).unwrap();
    let io = store.bind_native_data_io().unwrap();
    (account, invocation, engine, store, io)
}

fn owned_bytes(owners: crate::AllocationOwners, bytes: &'static [u8]) -> Result<Value> {
    CoreBuffer::copy_from(bytes, owners.graph())
        .map(Value::Bytes)
        .map_err(|_| NativeDataIoError::Capacity)
}

struct Release(Option<mpsc::Sender<()>>);
impl Release {
    fn send(&mut self) {
        if let Some(sender) = self.0.take() {
            let _ = sender.send(());
        }
    }
}
impl Drop for Release {
    fn drop(&mut self) {
        self.send();
    }
}

struct DropSignal(Option<tokio::sync::oneshot::Sender<()>>);
impl Drop for DropSignal {
    fn drop(&mut self) {
        if let Some(sender) = self.0.take() {
            let _ = sender.send(());
        }
    }
}

struct FileDescriptorProbe {
    file: Option<LeasedShared<File, ControlReservation>>,
    control: CoreControl,
    dropped: Option<tokio::sync::oneshot::Sender<(u64, u64)>>,
}

impl FileDescriptorProbe {
    fn is_open(&self) -> bool {
        self.file
            .as_ref()
            .is_some_and(|file| file.get().metadata().is_ok())
    }
}

impl Drop for FileDescriptorProbe {
    fn drop(&mut self) {
        let before = self.control.usage().bytes;
        drop(self.file.take());
        let after = self.control.usage().bytes;
        if let Some(sender) = self.dropped.take() {
            let _ = sender.send((before, after));
        }
    }
}

fn file_descriptor_probe(
    control: &CoreControl,
) -> (
    tempfile::TempDir,
    FileDescriptorProbe,
    tokio::sync::oneshot::Receiver<(u64, u64)>,
) {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("native-io-custody-probe");
    std::fs::write(&path, b"native file descriptor custody").unwrap();
    let file = File::open(path).unwrap();
    let charge = LeasedShared::<File, ControlReservation>::storage_bytes() as u64;
    let reservation = control.reserve(ControlKind::Registration, charge).unwrap();
    let file = LeasedShared::try_new_precharged(file, reservation).unwrap();
    let (dropped_tx, dropped_rx) = tokio::sync::oneshot::channel();
    (
        directory,
        FileDescriptorProbe {
            file: Some(file),
            control: control.clone(),
            dropped: Some(dropped_tx),
        },
        dropped_rx,
    )
}

#[tokio::test]
async fn successful_blocking_operation_keeps_original_scope_and_owned_bytes() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();
    let deadline = invocation.deadline().unwrap();
    let original_charge = account.snapshot().unwrap().charged_bytes;
    let owners = store.allocation_owners().clone();

    let value = io
        .execute(move || owned_bytes(owners, b"native data bytes"))
        .await
        .unwrap();
    let Value::Bytes(output) = value else {
        panic!("native byte operation returned another value kind");
    };

    assert_eq!(output.as_bytes(), b"native data bytes");
    assert!(account.snapshot().unwrap().charged_bytes > original_charge);
    assert_eq!(store.original_deadline(), deadline);
    assert_eq!(io.0.get().scope.deadline, deadline);
    assert!(io.0.get().scope.invocation.same_invocation(&invocation));
    assert!(
        invocation
            .control()
            .same_account(io.0.get().scope.invocation.control())
    );

    drop(output);
    assert_eq!(account.snapshot().unwrap().charged_bytes, original_charge);
    drop(store);
    io.retire_and_wait().await.unwrap();
    drop(io);
    drop(engine);
    drop(invocation);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn blocked_operation_is_busy_through_clones_without_another_slot() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();
    let owners = store.allocation_owners().clone();
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let mut release = Release(Some(release_tx));
    let work = move || {
        let output = CoreBuffer::copy_from(b"blocked bytes", owners.graph())
            .map(Value::Bytes)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let _ = started_tx.send(());
        release_rx.recv().map_err(|_| NativeDataIoError::Io)?;
        Ok(output)
    };
    let (id, request) = io.submit(work).unwrap();
    let original_slot = request.get().slot;
    started_rx.await.expect("spawn_blocking worker started");

    let clone = io.clone();
    let control_before = invocation.control().usage();
    assert!(matches!(
        io.submit(|| Err(NativeDataIoError::Io)),
        Err(NativeDataIoError::Busy)
    ));
    assert!(matches!(
        clone.submit(|| Err(NativeDataIoError::Io)),
        Err(NativeDataIoError::Busy)
    ));
    let control_after = invocation.control().usage();
    assert_eq!(
        control_after.count(tobkiri_gate::ControlKind::Task),
        control_before.count(tobkiri_gate::ControlKind::Task)
    );
    assert_eq!(
        control_after.count(tobkiri_gate::ControlKind::Queue),
        control_before.count(tobkiri_gate::ControlKind::Queue)
    );
    {
        let state = io.0.get().state.lock().unwrap();
        let (pending_id, pending_request) = state.pending.as_ref().unwrap();
        assert_eq!(*pending_id, id);
        assert_eq!(pending_request.get().slot, original_slot);
        assert_eq!(state.next, id + 1);
    }

    release.send();
    let value = await_read(io.clone(), id, request).await.unwrap();
    let Value::Bytes(output) = value else {
        panic!("native byte operation returned another value kind");
    };
    assert_eq!(output.as_bytes(), b"blocked bytes");
    drop(output);
    drop(clone);
    drop(store);
    io.retire_and_wait().await.unwrap();
    drop(io);
    drop(engine);
    drop(invocation);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn joined_but_unconsumed_completion_keeps_the_original_slot_busy() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();
    let original_charge = account.snapshot().unwrap().charged_bytes;
    let owners = store.allocation_owners().clone();
    let (id, request) = io
        .submit(move || owned_bytes(owners, b"unconsumed completion"))
        .unwrap();
    let original_slot = request.get().slot;

    assert!(matches!(
        wait_physical(&request).await,
        Ok(PhysicalCompletion::Joined)
    ));
    assert!(request.get().value.lock().unwrap().is_some());
    assert!(account.snapshot().unwrap().charged_bytes > original_charge);
    let request_address = request.get() as *const Pending;
    drop(request);

    assert!(matches!(
        io.clone().submit(|| Err(NativeDataIoError::Io)),
        Err(NativeDataIoError::Busy)
    ));
    {
        let state = io.0.get().state.lock().unwrap();
        let (pending_id, pending_request) = state.pending.as_ref().unwrap();
        assert_eq!(*pending_id, id);
        assert_eq!(pending_request.get().slot, original_slot);
        assert_eq!(pending_request.get() as *const Pending, request_address);
    }

    let value = {
        let pending_request =
            io.0.get()
                .state
                .lock()
                .unwrap()
                .pending
                .as_ref()
                .unwrap()
                .1
                .clone();
        await_read(io.clone(), id, pending_request).await.unwrap()
    };
    let Value::Bytes(output) = value else {
        panic!("native byte operation returned another value kind");
    };
    assert_eq!(output.as_bytes(), b"unconsumed completion");
    drop(output);
    assert_eq!(account.snapshot().unwrap().charged_bytes, original_charge);
    drop(store);
    io.retire_and_wait().await.unwrap();
    drop(io);
    drop(engine);
    drop(invocation);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn store_drop_waits_for_blocked_body_before_releasing_fd_and_output() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();
    let control = invocation.control().clone();
    let (probe_directory, file_probe, mut file_probe_dropped) = file_descriptor_probe(&control);
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let (fd_checked_tx, fd_checked_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let mut release = Release(Some(release_tx));
    let owners = store.allocation_owners().clone();
    let store_charge = account.snapshot().unwrap().charged_bytes;
    let baseline_control = control.usage();
    let work = move || {
        let output = CoreBuffer::copy_from(b"late native bytes", owners.graph())
            .map(Value::Bytes)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let _ = started_tx.send(file_probe.is_open());
        release_rx.recv().map_err(|_| NativeDataIoError::Io)?;
        let fd_open_after_release = file_probe.is_open();
        drop(file_probe);
        let _ = fd_checked_tx.send(fd_open_after_release);
        Ok(output)
    };
    let (id, request) = io.submit(work).unwrap();
    let slot = request.get().slot;
    assert!(started_rx.await.expect("spawn_blocking worker started"));
    drop(request);
    let active_charge = account.snapshot().unwrap().charged_bytes;
    assert!(active_charge > store_charge);

    drop(store);
    let after_store_drop = account.snapshot().unwrap().charged_bytes;
    assert!(after_store_drop > 0 && after_store_drop < active_charge);
    assert!(io.0.get().scope.retirement.is_cancelled());
    assert!(matches!(
        file_probe_dropped.try_recv(),
        Err(tokio::sync::oneshot::error::TryRecvError::Empty)
    ));
    let held_control = control.usage();
    assert!(held_control.count(tobkiri_gate::ControlKind::Task) >= 2);
    assert!(held_control.count(tobkiri_gate::ControlKind::Queue) >= 1);
    assert_eq!(
        held_control.count(tobkiri_gate::ControlKind::Registration),
        baseline_control.count(tobkiri_gate::ControlKind::Registration)
    );
    {
        let state = io.0.get().state.lock().unwrap();
        let (pending_id, request) = state.pending.as_ref().unwrap();
        assert_eq!(*pending_id, id);
        assert_eq!(request.get().slot, slot);
        assert!(request.get().value.lock().unwrap().is_none());
    }

    let cleanup_io = io.clone();
    let mut cleanup = Box::pin(cleanup_io.retire_and_wait());
    tokio::select! {
        biased;
        result = &mut cleanup => panic!("cleanup finished before release: {result:?}"),
        _ = tokio::task::yield_now() => {}
    }
    assert!(matches!(
        file_probe_dropped.try_recv(),
        Err(tokio::sync::oneshot::error::TryRecvError::Empty)
    ));
    assert_eq!(account.snapshot().unwrap().charged_bytes, after_store_drop);

    release.send();
    assert!(
        fd_checked_rx
            .await
            .expect("file descriptor remained open until release")
    );
    cleanup.await.unwrap();
    let (fd_charge_before_drop, fd_charge_after_drop) = file_probe_dropped.await.unwrap();
    assert!(fd_charge_before_drop > fd_charge_after_drop);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    let cleaned_control = control.usage();
    assert_eq!(
        cleaned_control.count(tobkiri_gate::ControlKind::Task),
        baseline_control.count(tobkiri_gate::ControlKind::Task)
    );
    assert_eq!(
        cleaned_control.count(tobkiri_gate::ControlKind::Queue),
        baseline_control.count(tobkiri_gate::ControlKind::Queue)
    );
    assert!(io.0.get().state.lock().unwrap().pending.is_none());
    drop(probe_directory);
    drop(io);
    drop(cleanup_io);
    drop(engine);
    drop(invocation);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn dropping_read_and_cleanup_observers_reuses_the_same_join_and_receiver() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();
    let control = invocation.control().clone();
    let (probe_directory, file_probe, mut file_probe_dropped) = file_descriptor_probe(&control);
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let (fd_checked_tx, fd_checked_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let mut release = Release(Some(release_tx));
    let owners = store.allocation_owners().clone();
    let store_charge = account.snapshot().unwrap().charged_bytes;
    let baseline_control = control.usage();
    let work = move || {
        let output = CoreBuffer::copy_from(b"dropped reader bytes", owners.graph())
            .map(Value::Bytes)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let _ = started_tx.send(file_probe.is_open());
        release_rx.recv().map_err(|_| NativeDataIoError::Io)?;
        let fd_open_after_release = file_probe.is_open();
        drop(file_probe);
        let _ = fd_checked_tx.send(fd_open_after_release);
        Ok(output)
    };
    let mut started_rx = started_rx;
    let mut read = Box::pin(io.execute(work));
    tokio::select! {
        biased;
        result = &mut read => {
            drop(result);
            panic!("blocked read completed before release");
        },
        started = &mut started_rx => assert!(started.expect("spawn_blocking worker started")),
    }
    drop(read);
    drop(store);

    let (id, original_request, slot) = {
        let state = io.0.get().state.lock().unwrap();
        let (id, request) = state.pending.as_ref().unwrap();
        (*id, request.get() as *const Pending, request.get().slot)
    };
    assert!(io.0.get().scope.retirement.is_cancelled());
    let retained_charge = account.snapshot().unwrap().charged_bytes;
    assert!(retained_charge > 0 && retained_charge < store_charge);
    assert!(matches!(
        file_probe_dropped.try_recv(),
        Err(tokio::sync::oneshot::error::TryRecvError::Empty)
    ));
    assert_eq!(
        control
            .usage()
            .count(tobkiri_gate::ControlKind::Registration),
        baseline_control.count(tobkiri_gate::ControlKind::Registration)
    );

    let first_cleanup_io = io.clone();
    let mut first_cleanup = Box::pin(first_cleanup_io.retire_and_wait());
    tokio::select! {
        biased;
        result = &mut first_cleanup => panic!("cleanup completed before body release: {result:?}"),
        _ = tokio::task::yield_now() => {}
    }
    drop(first_cleanup);
    {
        let state = io.0.get().state.lock().unwrap();
        assert!(!state.cleanup_waiting);
        let (pending_id, request) = state.pending.as_ref().unwrap();
        assert_eq!(*pending_id, id);
        assert_eq!(request.get().slot, slot);
        assert_eq!(request.get() as *const Pending, original_request);
    }
    assert_eq!(account.snapshot().unwrap().charged_bytes, retained_charge);
    assert!(matches!(
        file_probe_dropped.try_recv(),
        Err(tokio::sync::oneshot::error::TryRecvError::Empty)
    ));
    drop(first_cleanup_io);

    let second_cleanup_io = io.clone();
    let mut second_cleanup = Box::pin(second_cleanup_io.retire_and_wait());
    tokio::select! {
        biased;
        result = &mut second_cleanup => panic!("retry finished before release: {result:?}"),
        _ = tokio::task::yield_now() => {}
    }
    {
        let state = io.0.get().state.lock().unwrap();
        assert!(state.cleanup_waiting);
        let (pending_id, request) = state.pending.as_ref().unwrap();
        assert_eq!(*pending_id, id);
        assert_eq!(request.get().slot, slot);
        assert_eq!(request.get() as *const Pending, original_request);
    }

    release.send();
    assert!(
        fd_checked_rx
            .await
            .expect("file descriptor remained open until release")
    );
    second_cleanup.await.unwrap();
    let (fd_charge_before_drop, fd_charge_after_drop) = file_probe_dropped.await.unwrap();
    assert!(fd_charge_before_drop > fd_charge_after_drop);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    let cleaned_control = control.usage();
    assert_eq!(
        cleaned_control.count(tobkiri_gate::ControlKind::Task),
        baseline_control.count(tobkiri_gate::ControlKind::Task)
    );
    assert_eq!(
        cleaned_control.count(tobkiri_gate::ControlKind::Queue),
        baseline_control.count(tobkiri_gate::ControlKind::Queue)
    );
    assert!(io.0.get().state.lock().unwrap().pending.is_none());
    drop(probe_directory);
    drop(io);
    drop(second_cleanup_io);
    drop(engine);
    drop(invocation);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test(flavor = "current_thread")]
async fn retirement_before_worker_start_drops_but_never_calls_user_work() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();
    let called = Arc::new(AtomicBool::new(false));
    let called_by_work = called.clone();
    let (dropped_tx, dropped_rx) = tokio::sync::oneshot::channel();
    let drop_signal = DropSignal(Some(dropped_tx));
    let work = move || {
        let _drop_signal = drop_signal;
        called_by_work.store(true, Ordering::Release);
        Err(NativeDataIoError::Io)
    };
    let (_, request) = io.submit(work).unwrap();
    drop(request);

    // No yield occurs between publication and retirement. The existing monitor
    // must account for and join its cleanup body, but it must destroy this
    // unstarted closure without invoking it.
    io.retire();
    io.retire_and_wait().await.unwrap();
    assert!(!called.load(Ordering::Acquire));
    dropped_rx
        .await
        .expect("unstarted closure was destroyed after retirement");
    assert!(io.0.get().state.lock().unwrap().pending.is_none());

    drop(store);
    drop(io);
    drop(engine);
    drop(invocation);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn original_deadline_expires_while_blocked_io_retains_actual_fd_and_joins() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();
    let deadline = invocation.deadline().unwrap();
    let control = invocation.control().clone();
    let (directory, file_probe, mut file_dropped) = file_descriptor_probe(&control);
    let owners = store.allocation_owners().clone();
    let (started_tx, mut started_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let mut release = Release(Some(release_tx));
    let mut read = Box::pin(io.execute(move || {
        let output = owned_bytes(owners, b"bytes held beyond original deadline")?;
        let _ = started_tx.send(file_probe.is_open());
        release_rx.recv().map_err(|_| NativeDataIoError::Io)?;
        assert!(file_probe.is_open());
        drop(file_probe);
        Ok(output)
    }));
    tokio::select! {
        biased;
        result = &mut read => {
            drop(result);
            panic!("blocked IO completed before its native worker started");
        },
        started = &mut started_rx => assert!(started.unwrap()),
    }
    // Use the actual immutable invocation deadline, without a substituted
    // clock, shortened production lease, or IO-specific timeout allowance.
    let result = tokio::time::timeout(std::time::Duration::from_secs(40), &mut read)
        .await
        .expect("original native deadline must wake the reader");
    assert!(matches!(result, Err(NativeDataIoError::Deadline)));
    drop(read);
    assert!(std::time::Instant::now() >= deadline);
    assert_eq!(store.original_deadline(), deadline);
    assert_eq!(io.0.get().scope.deadline, deadline);
    drop(store);
    assert!(account.snapshot().unwrap().charged_bytes > 0);
    assert!(control.usage().count(ControlKind::Task) >= 2);
    assert!(control.usage().count(ControlKind::Queue) >= 1);
    assert!(matches!(
        file_dropped.try_recv(),
        Err(tokio::sync::oneshot::error::TryRecvError::Empty)
    ));
    let cleanup_io = io.clone();
    let mut cleanup = Box::pin(cleanup_io.retire_and_wait());
    tokio::select! {
        biased;
        result = &mut cleanup => panic!("cleanup completed before OS IO: {result:?}"),
        _ = tokio::task::yield_now() => {},
    }
    assert!(io.0.get().state.lock().unwrap().pending.is_some());
    release.send();
    cleanup.await.unwrap();
    let (before_fd_drop, after_fd_drop) = file_dropped.await.unwrap();
    assert!(before_fd_drop > after_fd_drop);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    assert!(io.0.get().state.lock().unwrap().pending.is_none());
    drop(directory);
    drop(cleanup_io);
    drop(io);
    drop(engine);
    drop(invocation);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn real_file_error_is_not_success_and_retires_the_io_slot() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();
    let directory = tempfile::tempdir().unwrap();
    let missing = directory.path().join("not-present");
    let result = io
        .execute(move || match std::fs::read(missing) {
            Ok(_) => Err(NativeDataIoError::CompletionUnknown),
            Err(_) => Err(NativeDataIoError::Io),
        })
        .await;
    assert!(matches!(result, Err(NativeDataIoError::Io)));
    assert!(io.0.get().scope.retirement.is_cancelled());

    let called = Arc::new(AtomicBool::new(false));
    let called_by_later = called.clone();
    assert!(matches!(
        io.execute(move || {
            called_by_later.store(true, Ordering::Release);
            Err(NativeDataIoError::Io)
        })
        .await,
        Err(NativeDataIoError::Retired)
    ));
    assert!(!called.load(Ordering::Acquire));
    io.retire_and_wait().await.unwrap();
    drop(directory);
    drop(store);
    drop(io);
    drop(engine);
    drop(invocation);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn worker_panic_is_completion_unknown_and_cannot_start_a_later_slot() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();

    let result = io
        .execute(|| -> Result<Value> { panic!("bounded native worker panic") })
        .await;
    assert!(matches!(result, Err(NativeDataIoError::CompletionUnknown)));
    assert!(io.0.get().scope.retirement.is_cancelled());

    let called = Arc::new(AtomicBool::new(false));
    let called_by_later = called.clone();
    assert!(matches!(
        io.execute(move || {
            called_by_later.store(true, Ordering::Release);
            Err(NativeDataIoError::Io)
        })
        .await,
        Err(NativeDataIoError::Retired)
    ));
    assert!(!called.load(Ordering::Acquire));
    assert_eq!(
        io.retire_and_wait().await.unwrap_err(),
        NativeDataIoError::CompletionUnknown
    );

    drop(store);
    drop(io);
    drop(engine);
    drop(invocation);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}
