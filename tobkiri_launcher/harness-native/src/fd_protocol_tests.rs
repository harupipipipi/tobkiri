//! Real kernel/File protocol mechanics; portable root, not source admission.
use super::*;
use std::{
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
        Digest::hash(b"fd-protocol-portable-root-not-source-admission")
    }
}

fn request(protocol: &NativeFdProtocol, operation: &str, args: &str) -> String {
    let p = protocol.invocation.provenance();
    format!(
        "{{\"format\":\"{REQUEST}\",\"scope\":[\"{:x}\",\"{:x}\",\"{:x}\",\"{:x}\"],\"target\":{{\"binding\":\"fd_{:x}\",\"steps\":[]}},\"operation\":\"{operation}\",\"args\":{args}}}",
        p.account_generation, p.invocation, protocol.store, protocol.generation, protocol.binding
    )
}

fn held_file(control: &CoreControl, file: File) -> LeasedShared<File, ControlReservation> {
    let lease = control
        .reserve(
            ControlKind::Frame,
            LeasedShared::<File, ControlReservation>::storage_bytes() as u64,
        )
        .unwrap();
    LeasedShared::try_new_precharged(file, lease).unwrap()
}

#[tokio::test]
async fn original_writer_dispatches_framed_read_stat_short_read_and_each_eof() {
    let control = CoreControl::default();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(PortableRoot)))
        .unwrap();
    let invocation = account.begin().unwrap();
    let engine = tobkiri_wasm::WasmEngine::new_with_control(&control).unwrap();
    let mut store = engine.native_setup_store(invocation.clone(), ()).unwrap();
    let io = store.bind_native_data_io().unwrap();
    let protocol = NativeFdProtocol::new(invocation.clone(), 3, 4, 1).unwrap();
    let mut file = tempfile::tempfile().unwrap();
    file.write_all(b"abcdef").unwrap();
    file.seek(SeekFrom::Start(0)).unwrap();
    let file = held_file(&control, file);
    for (op, args, expected) in [
        ("read", "[4]", Some("YWJjZA==")),
        ("fstat", "[]", None),
        ("read", "[8]", Some("ZWY=")),
        ("read", "[8]", Some("")),
        ("read", "[8]", Some("")),
        ("read", "[0]", Some("")),
    ] {
        let incoming = request(&protocol, op, args);
        let protocol = protocol.clone();
        let source = file.clone();
        let frame = io
            .inspect(move |output| {
                let incoming =
                    CoreBuffer::copy_from(incoming.as_bytes(), output.allocation_owners().graph())
                        .map_err(|_| NativeDataIoError::Capacity)?;
                protocol
                    .inspect(output, &incoming, source.get())?
                    .map_err(|_| NativeDataIoError::Io)?;
                Ok(())
            })
            .await
            .unwrap();
        let decoded: serde_json::Value = serde_json::from_slice(frame.as_bytes()).unwrap();
        assert_eq!(decoded["format"], RESPONSE);
        assert_eq!(decoded["scope"][2], "3");
        if let Some(expected) = expected {
            assert_eq!(decoded["result"]["kind"], "bytes");
            assert_eq!(decoded["result"]["value"], expected);
        } else {
            assert_eq!(decoded["result"]["kind"], "stat");
            assert_eq!(decoded["result"]["st_size"], 6);
        }
    }
    let mut borrowed = file.get();
    assert_eq!(borrowed.stream_position().unwrap(), 6);
    drop(store);
    io.retire_and_wait().await.unwrap();
    drop((file, protocol, io, invocation, engine));
    tobkiri_wasm::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn foreign_malformed_negative_and_unsupported_requests_never_access_file_or_publish() {
    for variant in 0..9 {
        let control = CoreControl::default();
        let account = CapacityLedger::new()
            .native_setup_account(&control, || Ok(Arc::new(PortableRoot)))
            .unwrap();
        let invocation = account.begin().unwrap();
        let foreign = account.begin().unwrap();
        let engine = tobkiri_wasm::WasmEngine::new_with_control(&control).unwrap();
        let mut store = engine.native_setup_store(invocation.clone(), ()).unwrap();
        let io = store.bind_native_data_io().unwrap();
        let mut protocol = NativeFdProtocol::new(invocation.clone(), 3, 4, 1).unwrap();
        let mut incoming = request(&protocol, "read", "[1]");
        match variant {
            0 => incoming = request(&protocol, "read", "[-1]"),
            1 => incoming = incoming.replace("fd_1", "fd_2"),
            2 => incoming = incoming.replace("\"steps\":[]", "\"steps\":[[\"parent\",\"\"]]"),
            3 => incoming = incoming.replace("\"scope\":[", "\"extra\":true,\"scope\":["),
            4 => incoming = request(&protocol, "close", "[]"),
            5 => incoming = incoming.replace("\"fd_1\"", "\"fd_01\""),
            6 => protocol.store = 5,
            7 => protocol.invocation = foreign.clone(),
            8 => incoming = request(&protocol, "read", "[1,2]"),
            _ => unreachable!(),
        }
        let mut file = tempfile::tempfile().unwrap();
        file.write_all(b"unread").unwrap();
        file.seek(SeekFrom::Start(0)).unwrap();
        let file = held_file(&control, file);
        let source = file.clone();
        let failed = io
            .inspect(move |output| {
                let incoming =
                    CoreBuffer::copy_from(incoming.as_bytes(), output.allocation_owners().graph())
                        .map_err(|_| NativeDataIoError::Capacity)?;
                output.append(b"private prefix")?;
                assert!(matches!(
                    protocol.inspect(output, &incoming, source.get()),
                    Err(NativeDataIoError::Unavailable)
                ));
                // Deliberately ignore rejection; private prefix must not escape.
                Ok(())
            })
            .await;
        assert_eq!(failed.unwrap_err(), NativeDataIoError::Unavailable);
        let mut borrowed = file.get();
        assert_eq!(borrowed.stream_position().unwrap(), 0);
        drop(store);
        io.retire_and_wait().await.unwrap();
        drop((file, io, invocation, foreign, engine));
        tobkiri_wasm::reap_allocation_owners().unwrap();
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    }
}

#[test]
fn canonical_scope_and_binding_numbers_reject_zero_padding_uppercase_and_overflow() {
    assert_eq!(canonical_hex("1"), Some(1));
    assert_eq!(canonical_hex("ffffffffffffffff"), Some(u64::MAX));
    for invalid in ["", "0", "01", "A", "-1", " 1", "10000000000000000"] {
        assert_eq!(canonical_hex(invalid), None);
    }
}
