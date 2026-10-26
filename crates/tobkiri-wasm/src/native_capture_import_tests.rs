//! Exact sealed-input import tests; native OS integration is separately scoped.
use super::*;
use crate::{NativeDataExport, WasmEngine};
use std::sync::Arc;
use tobkiri_canon::Digest;
use tobkiri_gate::{
    CapacityLedger, CoreControl, GateError, NativeSetupAccount, NativeSetupInvocation,
    NativeSetupLease,
};
use wasmtime::component::Component;

struct Proof;
impl NativeSetupLease for Proof {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"native-capture-import-portable-not-os")
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
fn import_type(offset: &str, element: &str, extra: &str) -> String {
    format!(
        r#"(import "{INTERFACE}" (instance $capture
        (export "read-range" (func (param "ordinal" u32) (param "offset" {offset})
            (param "length" u32) (result (list {element})))) {extra}))"#
    )
}
fn unused_import(engine: &WasmEngine, imports: &str, start_trap: bool) -> Component {
    let start = if start_trap {
        "(func $start unreachable) (start $start)"
    } else {
        ""
    };
    let source = format!(
        r#"(component {imports}
        (core module $m {start}
            (memory (export "memory") 1)
            (data (i32.const 32) "ABC")
            (func (export "run") (result i32)
                i32.const 0 i32.const 32 i32.store
                i32.const 4 i32.const 3 i32.store
                i32.const 0))
        (core instance $i (instantiate $m))
        (alias core export $i "memory" (core memory $memory))
        (alias core export $i "run" (core func $run))
        (func $value (result string) (canon lift (core func $run) (memory $memory)))
        (export "run" (func $value)))"#
    );
    Component::new(engine.engine(), wat::parse_str(source).unwrap()).unwrap()
}
fn reading_component(engine: &WasmEngine) -> Component {
    let imports = import_type("u64", "u8", "");
    let source = format!(
        r#"(component {imports}
        (alias export $capture "read-range" (func $read-range))
        (core module $mem
            (memory (export "memory") 1)
            (global $next (mut i32) (i32.const 1024))
            (func (export "realloc") (param i32 i32 i32 i32) (result i32)
                local.get 0 local.get 1 i32.or if unreachable end
                local.get 2 i32.const 1 i32.ne if unreachable end
                local.get 3 i32.const 65536 global.get $next i32.sub i32.gt_u if unreachable end
                global.get $next global.get $next local.get 3 i32.add global.set $next))
        (core instance $memory-instance (instantiate $mem))
        (alias core export $memory-instance "memory" (core memory $memory))
        (alias core export $memory-instance "realloc" (core func $realloc))
        (core func $read (canon lower (func $read-range) (memory $memory) (realloc $realloc)))
        (core module $body
            (import "env" "memory" (memory 1))
            (import "env" "read" (func $read (param i32 i64 i32 i32)))
            (func (export "run") (result i32)
                i32.const 0 i64.const 0 i32.const 3 i32.const 0 call $read
                i32.const 0)
            (func (export "post") (param i32)
                i32.const 0 i32.load i32.const 120 i32.store8))
        (core instance $i (instantiate $body (with "env" (instance
            (export "memory" (memory $memory)) (export "read" (func $read))))))
        (alias core export $i "run" (core func $run))
        (alias core export $i "post" (core func $post))
        (func $value (result string) (canon lift (core func $run) (memory $memory) (post-return $post)))
        (export "run" (func $value)))"#
    );
    Component::new(engine.engine(), wat::parse_str(source).unwrap()).unwrap()
}

#[tokio::test]
async fn input_import_refuses_unbound_or_unsealed_io_before_instantiation() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    for bind in [false, true] {
        let (account, invocation) = funding();
        let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
        let component = unused_import(&engine, &import_type("u64", "u8", ""), true);
        let mut store = engine.native_setup_store(invocation, ()).unwrap();
        let io = if bind {
            Some(store.bind_native_data_io().unwrap())
        } else {
            None
        };
        let error = store
            .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
            .await
            .err()
            .unwrap();
        assert!(format!("{error:#}").contains("sealed IO binding"));
        assert!(error.downcast_ref::<wasmtime::Trap>().is_none());
        if let Some(io) = io {
            io.retire_and_wait().await.unwrap();
            drop(io);
        }
        crate::reap_allocation_owners().unwrap();
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    }
}

#[tokio::test]
async fn captured_import_signature_and_export_set_are_exact() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    for (offset, element, extra) in [
        ("u32", "u8", ""),
        ("u64", "u32", ""),
        ("u64", "u8", "(export \"discover\" (func))"),
    ] {
        let (account, invocation) = funding();
        let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
        let component = unused_import(&engine, &import_type(offset, element, extra), true);
        let mut store = engine.native_setup_store(invocation, ()).unwrap();
        let io = store.bind_native_data_io().unwrap();
        drop(store.seal_captured_inputs(&[]).unwrap());
        let error = store
            .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
            .await
            .err()
            .unwrap();
        assert!(format!("{error:#}").contains("native captured"));
        assert!(error.downcast_ref::<wasmtime::Trap>().is_none());
        io.retire_and_wait().await.unwrap();
        drop(io);
        crate::reap_allocation_owners().unwrap();
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    }
}

#[tokio::test]
async fn accepted_input_import_never_falls_back_from_a_missing_ordinal_to_os_io() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let component = reading_component(&engine);
    let mut store = engine.native_setup_store(invocation, ()).unwrap();
    let io = store.bind_native_data_io().unwrap();
    drop(store.seal_captured_inputs(&[]).unwrap());
    let mut entry = store
        .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
        .await
        .unwrap();
    assert!(entry.invoke(None).await.is_err());
    let seen = entry.observation();
    assert!(seen.store_retired && !seen.unreachable_trap && !seen.post_return);
    drop(entry);
    io.retire_and_wait().await.unwrap();
    drop(io);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn even_empty_captures_cannot_cross_actual_native_invocation_identity() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    let (account, first) = funding();
    let second = account.begin().unwrap();
    let engine = WasmEngine::new_with_control(first.control()).unwrap();
    let mut first_store = engine.native_setup_store(first, ()).unwrap();
    let foreign = first_store.seal_captured_inputs(&[]).unwrap();
    let mut second_store = engine.native_setup_store(second, ()).unwrap();
    let io = second_store.bind_native_data_io().unwrap();
    assert!(io.bind_captured_inputs(foreign).is_err());
    drop(second_store.seal_captured_inputs(&[]).unwrap());
    drop((first_store, second_store));
    io.retire_and_wait().await.unwrap();
    drop(io);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[tokio::test]
async fn actual_apfs_snapshot_is_read_through_owned_wasm_import_and_post_return() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    let directory = tempfile::tempdir().unwrap();
    std::fs::write(directory.path().join("input"), b"ABC").unwrap();
    let root = tobkiri_store::DataRoot::open(directory.path().join("data")).unwrap();
    let control = CoreControl::default();
    let owner = root.native_setup_owner_lease(&control).unwrap();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(owner.clone())))
        .unwrap();
    let invocation = account.begin().unwrap();
    let deadline = invocation.deadline().unwrap();
    let engine = WasmEngine::new_with_control(&control).unwrap();
    let component = reading_component(&engine);
    let mut store = engine.native_setup_store(invocation, ()).unwrap();
    let io = store.bind_native_data_io().unwrap();
    let digest = Digest::hash(b"ABC");
    let snapshot = io
        .capture_file_snapshot(
            &owner,
            tobkiri_store::NativeSnapshotSource {
                root: directory.path(),
                relative: "input",
                length: 3,
                sha256: &digest,
            },
        )
        .await
        .unwrap();
    std::fs::write(directory.path().join("input"), b"XYZ").unwrap();
    drop(
        store
            .seal_captured_inputs(&[crate::NativeCapturedFile {
                path: "input",
                snapshot: &snapshot,
            }])
            .unwrap(),
    );
    let mut entry = store
        .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
        .await
        .unwrap();
    let output = entry.invoke(None).await.unwrap();
    assert_eq!(output.as_str().unwrap(), "ABC");
    assert!(entry.observation().post_return && !entry.observation().store_retired);
    assert_eq!(entry.original_deadline(), deadline);
    drop(entry);
    io.retire_and_wait().await.unwrap();
    drop((io, snapshot, owner, root));
    assert_eq!(output.as_str().unwrap(), "ABC");
    assert!(account.snapshot().unwrap().charged_bytes > 0);
    drop(output);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}
