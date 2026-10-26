//! Borrowed inspection output on the real kernel; injected portable root proof.
use super::*;

#[cfg(unix)]
#[tokio::test]
async fn inspections_reread_live_fd_and_owned_outputs_survive_store_retirement() {
    use std::os::unix::fs::FileExt;
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();
    let (directory, probe, _dropped) = file_descriptor_probe(invocation.control());
    let path = directory.path().join("native-io-custody-probe");
    let expected = invocation.provenance();
    std::fs::write(&path, b"first").unwrap();
    let fd = probe.file.as_ref().unwrap().clone();
    let first = io
        .inspect(move |output| {
            assert_eq!(output.invocation().provenance(), expected);
            output
                .write_chunk(5, |tail| fd.get().read_at(tail, 0))?
                .map_err(|_| NativeDataIoError::Io)?;
            Ok(())
        })
        .await
        .unwrap();
    std::fs::write(&path, b"after").unwrap();
    let fd = probe.file.as_ref().unwrap().clone();
    let second = io
        .inspect(move |output| {
            output
                .write_chunk(5, |tail| fd.get().read_at(tail, 0))?
                .map_err(|_| NativeDataIoError::Io)?;
            Ok(())
        })
        .await
        .unwrap();
    assert_eq!(first.as_bytes(), b"first");
    assert_eq!(second.as_bytes(), b"after");
    drop((store, probe));
    io.retire_and_wait().await.unwrap();
    drop((io, engine, invocation));
    crate::reap_allocation_owners().unwrap();
    assert!(account.snapshot().unwrap().charged_bytes > 0);
    drop(first);
    assert_eq!(second.as_bytes(), b"after");
    drop(second);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn typed_producer_error_is_preserved_without_publishing_its_partial_tail() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();
    let value = io
        .inspect(|output| {
            let error = output.write_chunk(32, |tail| {
                tail[..7].copy_from_slice(b"partial");
                Err::<usize, _>((17_u32, "original typed producer error"))
            })?;
            assert_eq!(error.unwrap_err(), (17, "original typed producer error"));
            assert!(output.is_empty());
            // A native handler chooses how to represent its original supported
            // error. This fixed test DATA is not an exception-graph parity proof.
            output.append(b"native handler result")
        })
        .await
        .unwrap();
    assert_eq!(value.as_bytes(), b"native handler result");
    drop((value, store));
    io.retire_and_wait().await.unwrap();
    drop((io, invocation, engine));
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn ignored_checkpoint_failure_is_sticky_over_success_and_domain_error() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::super::lock_census_fixture().await;
    for replace_domain in [false, true] {
        let (account, invocation, engine, store, io) = setup();
        let result = io
            .inspect(move |output| {
                let failed = output.write_chunk_checked(8, |tail, checkpoint| {
                    tail.copy_from_slice(b"private!");
                    assert_eq!(
                        checkpoint.charge_work(u64::MAX),
                        Err(NativeDataIoError::Capacity)
                    );
                    assert_eq!(checkpoint.charge_work(0), Err(NativeDataIoError::Capacity));
                    if replace_domain { Err(41_u32) } else { Ok(8) }
                });
                assert_eq!(failed, Err(NativeDataIoError::Capacity));
                assert!(output.is_empty());
                assert_eq!(output.append(b"ignored"), Err(NativeDataIoError::Capacity));
                Ok(())
            })
            .await;
        assert_eq!(result.unwrap_err(), NativeDataIoError::Capacity);
        drop(store);
        io.retire_and_wait().await.unwrap();
        drop((io, invocation, engine));
        crate::reap_allocation_owners().unwrap();
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    }
}

#[tokio::test]
async fn producer_without_checkpoint_cannot_commit_tail_after_retirement() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();
    let retirement = io.0.get().scope.retirement.clone();
    let (observed_tx, observed_rx) = tokio::sync::oneshot::channel();
    let result = io
        .inspect(move |output| {
            let result = output.write_chunk_checked(8, |tail, _unused_checkpoint| {
                tail.copy_from_slice(b"private!");
                retirement.cancel();
                Ok::<_, ()>(8)
            });
            let _ = observed_tx.send((result, output.is_empty()));
            Ok(())
        })
        .await;
    assert!(matches!(result, Err(NativeDataIoError::Retired)));
    // The kernel rolled the tail back before private commit even though this
    // producer never used its checkpoint. The real worker still must finish.
    let (observed, empty) = observed_rx.await.unwrap();
    assert_eq!(observed, Err(NativeDataIoError::Retired));
    assert!(empty);
    drop(store);
    io.retire_and_wait().await.unwrap();
    drop((io, invocation, engine));
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn ignored_capacity_or_invalid_length_failure_cannot_publish_success() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::super::lock_census_fixture().await;
    for (invalid_length, replace_error) in
        [(false, false), (true, false), (false, true), (true, true)]
    {
        let (account, invocation, engine, store, io) = setup();
        let called = Arc::new(AtomicBool::new(false));
        let observed = called.clone();
        let expected = if invalid_length {
            NativeDataIoError::CompletionUnknown
        } else {
            NativeDataIoError::Capacity
        };
        let result = io
            .inspect(move |output| {
                output.append(b"private prefix")?;
                let maximum = if invalid_length {
                    1
                } else {
                    tobkiri_canon::MAX_BYTES + 1
                };
                let failed = output.write_chunk(maximum, |tail| {
                    observed.store(true, Ordering::SeqCst);
                    Ok::<_, ()>(tail.len() + 1)
                });
                assert_eq!(failed.unwrap_err(), expected);
                assert_eq!(output.append(b"ignored failure"), Err(expected));
                // Neither a success nor a replacement error may hide the
                // original accounting/producer-contract failure.
                if replace_error {
                    Err(NativeDataIoError::Io)
                } else {
                    Ok(())
                }
            })
            .await;
        assert_eq!(result.unwrap_err(), expected);
        assert_eq!(called.load(Ordering::SeqCst), invalid_length);
        drop(store);
        io.retire_and_wait().await.unwrap();
        drop((io, invocation, engine));
        crate::reap_allocation_owners().unwrap();
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    }
}

#[tokio::test]
async fn inspection_store_drop_keeps_blocked_fd_and_output_until_physical_join() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = super::super::super::lock_census_fixture().await;
    let (account, invocation, engine, store, io) = setup();
    let (directory, probe, mut dropped) = file_descriptor_probe(invocation.control());
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let mut release = Release(Some(release_tx));
    let operation_io = io.clone();
    let mut operation = Box::pin(operation_io.inspect(move |output| {
        output.append(b"private in-flight inspection bytes")?;
        let _ = started_tx.send(probe.is_open());
        release_rx.recv().map_err(|_| NativeDataIoError::Io)?;
        assert!(probe.is_open());
        drop(probe);
        Ok(())
    }));
    tokio::select! {
        result = &mut operation => panic!("blocked inspection finished: {result:?}"),
        started = started_rx => assert!(started.unwrap()),
    }
    drop(store);
    assert!(matches!(
        operation.await,
        Err(NativeDataIoError::Retired | NativeDataIoError::Cancelled)
    ));
    assert!(account.snapshot().unwrap().charged_bytes > 0);
    assert!(matches!(
        dropped.try_recv(),
        Err(tokio::sync::oneshot::error::TryRecvError::Empty)
    ));
    let cleanup_io = io.clone();
    let mut cleanup = Box::pin(cleanup_io.retire_and_wait());
    tokio::select! {
        biased;
        result = &mut cleanup => panic!("cleanup escaped blocked original IO: {result:?}"),
        _ = tokio::task::yield_now() => {}
    }
    release.send();
    cleanup.await.unwrap();
    let (before, after) = dropped.await.unwrap();
    assert!(before > after);
    drop((io, operation_io, cleanup_io, invocation, engine, directory));
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}
