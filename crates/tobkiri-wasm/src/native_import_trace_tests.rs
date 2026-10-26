//! Portable custody/signature checks, not actual OS or Python import proof.
use super::*;
use crate::{NativeDataExport, WasmEngine};
use std::sync::Arc;
use tobkiri_canon::Digest;
use tobkiri_gate::{CapacityLedger, CoreControl, GateError, NativeSetupAccount, NativeSetupLease};
use wasmtime::component::Component;

struct Proof;
impl NativeSetupLease for Proof {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"portable-import-trace-not-os-proof")
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
fn fixture(engine: &WasmEngine) -> Component {
    Component::new(
        engine.engine(),
        wat::parse_str(
            r#"(component
      (import "tobkiri-ui:diagnostics/import-trace@1.0.0"
        (instance $trace (export "record-import" (func (param "name" string)))))
      (alias export $trace "record-import" (func $record))
      (core module $memory (memory (export "memory") 1))
      (core instance $memory (instantiate $memory))
      (alias core export $memory "memory" (core memory $memory))
      (core func $record (canon lower (func $record) (memory $memory)))
      (core module $body
        (import "env" "memory" (memory 1))
        (import "host" "record" (func $record (param i32 i32)))
        (data (i32.const 0) "hashlib")
        (func (export "run") (result i32)
          i32.const 0 i32.const 7 call $record i32.const 16))
      (core instance $body (instantiate $body
        (with "env" (instance $memory))
        (with "host" (instance (export "record" (func $record))))))
      (func (export "run") (result string)
        (canon lift (core func $body "run") (memory $memory))))"#,
        )
        .unwrap(),
    )
    .unwrap()
}

#[tokio::test]
async fn diagnostic_trace_requires_one_explicit_binding_and_keeps_bytes_after_store_drop() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (_, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let component = fixture(&engine);
    assert!(
        crate::validate_imports(
            engine.engine(),
            &component,
            &std::collections::BTreeSet::from([INTERFACE.to_owned()])
        )
        .is_err()
    );
    let unbound = engine.native_setup_store(invocation, ()).unwrap();
    assert!(
        unbound
            .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
            .await
            .is_err()
    );

    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let component = fixture(&engine);
    let mut store = engine.native_setup_store(invocation, ()).unwrap();
    let trace = store.enable_import_trace().unwrap();
    assert!(store.enable_import_trace().is_err());
    let mut entry = store
        .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
        .await
        .unwrap();
    let result = entry.invoke(None).await.unwrap();
    assert!(result.is_empty());
    entry.retire();
    drop(entry);
    drop(result);
    assert!(account.snapshot().unwrap().charged_bytes > 0);
    trace.write_snapshot(std::io::sink()).unwrap(); // no new guest allocation
    assert!(trace.record("after_retirement").is_err());
    {
        let state = trace.0.get().state.try_lock().unwrap();
        assert_eq!(state.recorded, 1);
        assert_eq!(state.seen, 1);
        assert_eq!(
            state.rows[0].as_ref().unwrap().name.as_str().unwrap(),
            "hashlib"
        );
        assert!(!state.malformed && !state.overflow);
    }
    drop(trace);
    drop(engine);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn overflow_and_malformed_names_are_explicit_inconclusive_without_replacing_the_import() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (_, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let mut store = engine.native_setup_store(invocation, ()).unwrap();
    let trace = store.enable_import_trace().unwrap();
    for name in ["/private/path", "module..child", "", "λ"] {
        trace.record(name).unwrap();
    }
    for _ in 0..MAX_RECORDS {
        trace.record("valid.module").unwrap();
    }
    trace.record("overflow").unwrap();
    {
        let state = trace.0.get().state.try_lock().unwrap();
        assert_eq!(state.recorded, MAX_RECORDS);
        assert_eq!(state.seen, 133);
        assert!(state.malformed && state.overflow);
        assert_eq!(state.rows[0].as_ref().unwrap().sequence, 4);
    }
    store.retire();
    drop(store);
    trace.write_snapshot(std::io::sink()).unwrap();
    drop(trace);
    crate::reap_allocation_owners().unwrap();
}

#[tokio::test]
async fn original_allocation_refusal_cannot_leave_a_partial_trace_marked_complete() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let retained = invocation.clone();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let mut store = engine.native_setup_store(invocation, ()).unwrap();
    let trace = store.enable_import_trace().unwrap();
    trace.record("known").unwrap();
    let remaining =
        tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES - account.snapshot().unwrap().charged_bytes;
    let blocker = retained
        .reserve(tobkiri_gate::CapacityKind::GuestLinear, remaining)
        .unwrap();
    assert!(trace.record("lost").is_err());
    assert!(trace.0.get().failed.load(Ordering::Acquire));
    drop(blocker);
    store.retire();
    drop(store);
    let mut storage = [0u8; 2048];
    let mut writer = std::io::Cursor::new(storage.as_mut_slice());
    trace.write_snapshot(&mut writer).unwrap();
    let bytes = &writer.get_ref()[..writer.position() as usize];
    assert!(
        bytes
            .windows(b"\"trace_complete\":false".len())
            .any(|part| part == b"\"trace_complete\":false")
    );
    assert!(
        bytes
            .windows(b"\"recording_failed\":true".len())
            .any(|part| part == b"\"recording_failed\":true")
    );
    drop(trace);
    drop(retained);
    drop(engine);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}
