//! Real file metadata + original kernel writer; portable injected root only.
use super::{MAX_STAT_JSON_BYTES, write_fd_stat};
use std::{
    fs::{File, FileTimes},
    io::Write,
    os::unix::fs::MetadataExt,
    sync::Arc,
    time::{Duration, SystemTime},
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
        Digest::hash(b"fd-stat-writer-portable-root-not-source-admission")
    }
}

#[tokio::test]
async fn original_io_writer_emits_exact_stat_fields_and_signed_nanoseconds() {
    let control = CoreControl::default();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(PortableRoot)))
        .unwrap();
    let invocation = account.begin().unwrap();
    let engine = tobkiri_wasm::WasmEngine::new_with_control(&control).unwrap();
    let mut store = engine.native_setup_store(invocation.clone(), ()).unwrap();
    let io = store.bind_native_data_io().unwrap();
    let mut file = tempfile::tempfile().unwrap();
    file.write_all(b"stat source")
        .and_then(|()| {
            file.set_times(
                FileTimes::new().set_modified(
                    SystemTime::UNIX_EPOCH - Duration::new(1_700_000_000, 123_456_789),
                ),
            )
        })
        .unwrap();
    let expected_metadata = file.metadata().unwrap();
    let expected_mtime_ns = i128::from(expected_metadata.mtime()) * 1_000_000_000
        + i128::from(expected_metadata.mtime_nsec());
    assert!(expected_mtime_ns < -1_000_000_000_000_000);
    let expected = format!(
        "{{\"frame\":{{\"kind\":\"stat\",\"st_mode\":{},\"st_dev\":{},\"st_ino\":{},\"st_size\":{},\"st_mtime_ns\":{}}}}}",
        expected_metadata.mode(),
        expected_metadata.dev(),
        expected_metadata.ino(),
        expected_metadata.size(),
        expected_mtime_ns,
    );
    let lease = control
        .reserve(
            ControlKind::Frame,
            LeasedShared::<File, ControlReservation>::storage_bytes() as u64,
        )
        .unwrap();
    let held = LeasedShared::try_new_precharged(file, lease).unwrap();
    let file = held.clone();
    let result = io
        .inspect(move |output| {
            write_fd_stat(output, file.get(), b"{\"frame\":", b"}")?
                .map_err(|_| tobkiri_wasm::NativeDataIoError::Io)?;
            Ok(())
        })
        .await
        .unwrap();

    assert_eq!(result.as_bytes(), expected.as_bytes());
    let parsed: serde_json::Value = serde_json::from_slice(result.as_bytes()).unwrap();
    assert_eq!(parsed["frame"]["kind"], "stat");
    assert_eq!(
        parsed["frame"]["st_mtime_ns"].as_i64(),
        i64::try_from(expected_mtime_ns).ok()
    );

    drop(store);
    io.retire_and_wait().await.unwrap();
    drop((held, io, invocation, engine));
    drop(result);
    tobkiri_wasm::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn frame_prepayment_failure_is_outer_and_retires_original_io() {
    let control = CoreControl::default();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(PortableRoot)))
        .unwrap();
    let invocation = account.begin().unwrap();
    let engine = tobkiri_wasm::WasmEngine::new_with_control(&control).unwrap();
    let mut store = engine.native_setup_store(invocation.clone(), ()).unwrap();
    let io = store.bind_native_data_io().unwrap();
    let file = tempfile::tempfile().unwrap();
    let lease = control
        .reserve(
            ControlKind::Frame,
            LeasedShared::<File, ControlReservation>::storage_bytes() as u64,
        )
        .unwrap();
    let held = LeasedShared::try_new_precharged(file, lease).unwrap();
    let filler_len = tobkiri_canon::MAX_BYTES - (MAX_STAT_JSON_BYTES - 1);
    let file = held.clone();
    let failed = io
        .inspect(move |output| {
            output
                .write_chunk(filler_len, |tail| {
                    tail.fill(b' ');
                    Ok::<usize, ()>(filler_len)
                })?
                .unwrap();
            let result = write_fd_stat(output, file.get(), b"", b"");
            assert!(matches!(
                result,
                Err(tobkiri_wasm::NativeDataIoError::Capacity)
            ));
            // Ignoring the helper error cannot publish a replacement success.
            Ok(())
        })
        .await;
    assert_eq!(
        failed.unwrap_err(),
        tobkiri_wasm::NativeDataIoError::Capacity
    );

    let retired = io
        .inspect(|_| panic!("retired IO must not execute another stat"))
        .await;
    assert!(matches!(
        retired,
        Err(tobkiri_wasm::NativeDataIoError::Retired)
    ));

    drop(store);
    io.retire_and_wait().await.unwrap();
    drop((held, io, invocation, engine));
    tobkiri_wasm::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}
