//! Real FD + original kernel writer. Portable injected root, not OS admission.
use super::write_fd_read;
use std::{
    fs::File,
    io::{Seek, SeekFrom, Write},
    sync::Arc,
};
use tobkiri_canon::{Digest, LeasedShared};
use tobkiri_gate::{
    CapacityLedger, ControlKind, ControlReservation, CoreControl, GateError, NativeSetupLease,
};

struct PortableRoot;
impl NativeSetupLease for PortableRoot {
    fn validate(&self) -> Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"fd-read-writer-portable-root-not-source-admission")
    }
}

fn position(file: &File) -> u64 {
    let mut borrowed = file;
    borrowed.stream_position().unwrap()
}

#[tokio::test]
async fn original_io_writer_prepays_fd_frames_without_replaying_failed_reads() {
    let control = CoreControl::default();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(PortableRoot)))
        .unwrap();
    let invocation = account.begin().unwrap();
    let engine = tobkiri_wasm::WasmEngine::new_with_control(&control).unwrap();
    let mut store = engine.native_setup_store(invocation.clone(), ()).unwrap();
    let io = store.bind_native_data_io().unwrap();
    let mut file = tempfile::tempfile().unwrap();
    file.write_all(b"abcdef").unwrap();
    file.seek(SeekFrom::Start(0)).unwrap();
    let lease = control
        .reserve(
            ControlKind::Frame,
            LeasedShared::<File, ControlReservation>::storage_bytes() as u64,
        )
        .unwrap();
    let held = LeasedShared::try_new_precharged(file, lease).unwrap();
    let mut retained = None;

    for (requested, encoded, position) in [(4, "YWJjZA==", 4), (8, "ZWY=", 6), (8, "", 6)] {
        let file = held.clone();
        let result = io
            .inspect(move |output| {
                write_fd_read(output, file.get(), requested, b"\"", b"\"")?
                    .map_err(|_| tobkiri_wasm::NativeDataIoError::Io)?;
                Ok(())
            })
            .await
            .unwrap();
        assert_eq!(
            serde_json::from_slice::<String>(result.as_bytes()).unwrap(),
            encoded
        );
        assert_eq!(self::position(held.get()), position);
        if position == 4 {
            retained = Some(result);
        }
    }

    let mut borrowed = held.get();
    borrowed.seek(SeekFrom::Start(0)).unwrap();
    let file = held.clone();
    let failed = io
        .inspect(move |output| {
            let result = write_fd_read(output, file.get(), usize::MAX, b"\"", b"\"");
            assert!(matches!(
                result,
                Err(tobkiri_wasm::NativeDataIoError::Capacity)
            ));
            // Even an ignored helper error cannot publish a manufactured success.
            Ok(())
        })
        .await;
    assert_eq!(
        failed.unwrap_err(),
        tobkiri_wasm::NativeDataIoError::Capacity
    );
    assert_eq!(
        position(held.get()),
        0,
        "prepayment failure must precede the actual FD read"
    );

    // Outer accounting failures retire the original IO. They never authorize
    // a fresh read/budget on the same invocation, even after buffers are freed.
    let retired = io
        .inspect(|_| panic!("retired IO must not execute another read"))
        .await;
    assert!(matches!(
        retired,
        Err(tobkiri_wasm::NativeDataIoError::Retired)
    ));
    assert_eq!(position(held.get()), 0);

    drop(store);
    io.retire_and_wait().await.unwrap();
    drop((held, io, invocation, engine));
    tobkiri_wasm::reap_allocation_owners().unwrap();
    assert!(
        account.snapshot().unwrap().charged_bytes > 0,
        "returned DATA must retain original funding after Store retirement"
    );
    drop(retained);
    tobkiri_wasm::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}
