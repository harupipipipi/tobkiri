//! Real portable Wasm imports and funded worker custody; not nativeKit/GUI proof.
use super::*;
use crate::{NativeDataBinding, NativeDataExport, NativeDataValue, WasmEngine};
use std::sync::{Arc, Mutex};
use tobkiri_canon::Digest;
use tobkiri_gate::{
    CapacityKind, CapacityLedger, CoreControl, GateError, NATIVE_SETUP_MEMORY_BYTES,
    NativeSetupAccount, NativeSetupLease,
};
use wasmtime::component::Component;

const INTERFACE: &str = "tobkiri-native:test/inventory@1.0.0";
struct ProofLease;
impl NativeSetupLease for ProofLease {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"fixed-import-portable-not-os")
    }
}
fn funding() -> (NativeSetupAccount, NativeSetupInvocation) {
    let account = CapacityLedger::new()
        .native_setup_account(&CoreControl::default(), || Ok(Arc::new(ProofLease)))
        .unwrap();
    let invocation = account.begin().unwrap();
    (account, invocation)
}
struct State {
    invocation: NativeSetupInvocation,
}
impl NativeImportState for State {
    fn native_import_invocation(&self) -> &NativeSetupInvocation {
        &self.invocation
    }
}
fn inventory(context: NativeImportContext<'_, State>) -> Result<NativeImportOperation> {
    assert!(context.same_invocation(&context.state().invocation));
    let input = context.input().cloned();
    context.inspect(move |output| {
        if let Some(input) = input {
            output.append(input.as_bytes())?;
        } else {
            output.append(b"ABC")?;
        }
        Ok(())
    })
}
fn ignored_writer_failure(
    context: NativeImportContext<'_, State>,
) -> Result<NativeImportOperation> {
    context.inspect(|output| {
        let _ = output.write_chunk::<(), _>(0, |_| Ok(1));
        Ok(())
    })
}
static BINDINGS: [NativeImportBinding<State>; 1] = [NativeImportBinding {
    interface: INTERFACE,
    function: "inventory",
    input: NativeImportInput::None,
    prepare: inventory,
}];
static STRING_BINDINGS: [NativeImportBinding<State>; 1] = [NativeImportBinding {
    interface: INTERFACE,
    function: "inventory",
    input: NativeImportInput::String,
    prepare: inventory,
}];
static FAILED: [NativeImportBinding<State>; 1] = [NativeImportBinding {
    interface: INTERFACE,
    function: "inventory",
    input: NativeImportInput::None,
    prepare: ignored_writer_failure,
}];
fn component(
    engine: &WasmEngine,
    string_input: bool,
    extra: bool,
    during_start: bool,
) -> Component {
    let params = if string_input {
        "(param \"payload\" string)"
    } else {
        ""
    };
    let extra = if extra {
        "(export \"unknown\" (func (result string)))"
    } else {
        ""
    };
    let args = if string_input {
        "i32.const 32 i32.const 3"
    } else {
        ""
    };
    let signature = if string_input { "i32 i32 i32" } else { "i32" };
    let start = if during_start {
        format!("(func $start {args} i32.const 0 call $inspect) (start $start)")
    } else {
        String::new()
    };
    let source = format!(
        r#"(component
        (import "{INTERFACE}" (instance $native
          (export "inventory" (func {params} (result string))) {extra}))
        (alias export $native "inventory" (func $inspect))
        (core module $mem
          (memory (export "memory") 1)
          (data (i32.const 32) "XYZ")
          (global $next (mut i32) (i32.const 4096))
          (func (export "realloc") (param i32 i32 i32 i32) (result i32)
            local.get 0 local.get 1 i32.or if unreachable end
            local.get 2 i32.const 1 i32.ne if unreachable end
            local.get 3 i32.const 65536 global.get $next i32.sub i32.gt_u if unreachable end
            global.get $next global.get $next local.get 3 i32.add global.set $next))
        (core instance $mi (instantiate $mem))
        (alias core export $mi "memory" (core memory $memory))
        (alias core export $mi "realloc" (core func $realloc))
        (core func $call (canon lower (func $inspect) (memory $memory) (realloc $realloc)))
        (core module $body
          (import "env" "memory" (memory 1))
          (import "env" "inspect" (func $inspect (param {signature})))
          {start}
          (func (export "run") (result i32) {args} i32.const 0 call $inspect i32.const 0))
        (core instance $bi (instantiate $body (with "env" (instance
          (export "memory" (memory $memory)) (export "inspect" (func $call))))))
        (alias core export $bi "run" (core func $run))
        (func $value (result string) (canon lift (core func $run) (memory $memory)))
        (export "run" (func $value)))"#
    );
    Component::new(engine.engine(), wat::parse_str(source).unwrap()).unwrap()
}

#[tokio::test]
async fn native_fixed_import_context_and_duplicate_contracts_are_checked_before_store_creation() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (_account, invocation) = funding();
    let (_, other) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    assert!(
        engine
            .native_setup_store_with_imports(
                invocation.clone(),
                State { invocation: other },
                &BINDINGS
            )
            .is_err()
    );
    static DUPLICATE: [NativeImportBinding<State>; 2] = [BINDINGS[0], BINDINGS[0]];
    assert!(
        engine
            .native_setup_store_with_imports(
                invocation.clone(),
                State {
                    invocation: invocation.clone()
                },
                &DUPLICATE
            )
            .is_err()
    );
    // Neither rejection claimed the one actual Store attempt.
    let store = engine
        .native_setup_store_with_imports(invocation.clone(), State { invocation }, &BINDINGS)
        .unwrap();
    let io = store.native_data_io().unwrap().clone();
    drop(store);
    io.retire_and_wait().await.unwrap();
    drop(io);
    crate::reap_allocation_owners().unwrap();
}

#[tokio::test]
async fn native_fixed_import_abi_and_unknown_functions_fail_before_start() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    for (string, extra) in [(true, false), (false, true)] {
        let (account, invocation) = funding();
        let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
        let component = component(&engine, string, extra, true);
        let store = engine
            .native_setup_store_with_imports(invocation.clone(), State { invocation }, &BINDINGS)
            .unwrap();
        let io = store.native_data_io().unwrap().clone();
        let error = store
            .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
            .await
            .err()
            .unwrap();
        assert!(error.downcast_ref::<wasmtime::Trap>().is_none());
        io.retire_and_wait().await.unwrap();
        drop(io);
        crate::reap_allocation_owners().unwrap();
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    }
}

#[tokio::test]
async fn native_fixed_import_real_worker_runs_during_start_and_across_typed_export_calls() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    for string in [false, true] {
        let (account, invocation) = funding();
        let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
        let component = component(&engine, string, false, true);
        let store = engine
            .native_setup_store_with_imports(
                invocation.clone(),
                State { invocation },
                if string { &STRING_BINDINGS } else { &BINDINGS },
            )
            .unwrap();
        let io = store.native_data_io().unwrap().clone();
        let mut program = store
            .prepare_data_program(
                &component,
                &[NativeDataBinding {
                    export: NativeDataExport::new(None, "run"),
                    result: None,
                }],
            )
            .await
            .unwrap();
        for _ in 0..2 {
            let NativeDataValue::String(bytes) = program.invoke(0, None).await.unwrap().unwrap()
            else {
                panic!("string expected")
            };
            assert_eq!(bytes.as_bytes(), if string { b"XYZ" } else { b"ABC" });
        }
        drop(program);
        io.retire_and_wait().await.unwrap();
        drop(io);
        crate::reap_allocation_owners().unwrap();
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    }
}

#[tokio::test]
async fn native_fixed_import_ignored_writer_failure_cannot_publish_success() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let component = component(&engine, false, false, false);
    let store = engine
        .native_setup_store_with_imports(invocation.clone(), State { invocation }, &FAILED)
        .unwrap();
    let io = store.native_data_io().unwrap().clone();
    let mut entry = store
        .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
        .await
        .unwrap();
    assert!(entry.invoke(None).await.is_err());
    assert!(entry.observation().store_retired);
    drop(entry);
    io.retire_and_wait().await.unwrap();
    drop(io);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

// A prepared operation cannot be carried to another call/invocation. Its worker
// has not started while parked; its actual funded captures stay alive.
static PARKED: Mutex<Option<NativeImportOperation>> = Mutex::new(None);
fn park(context: NativeImportContext<'_, State>) -> Result<NativeImportOperation> {
    *PARKED.lock().unwrap() = Some(context.inspect(|output| output.append(b"old"))?);
    bail!("parked operation intentionally not accepted")
}
fn foreign(_context: NativeImportContext<'_, State>) -> Result<NativeImportOperation> {
    PARKED
        .lock()
        .unwrap()
        .take()
        .context("parked operation missing")
}
static PARK: [NativeImportBinding<State>; 1] = [NativeImportBinding {
    interface: INTERFACE,
    function: "inventory",
    input: NativeImportInput::None,
    prepare: park,
}];
static FOREIGN: [NativeImportBinding<State>; 1] = [NativeImportBinding {
    interface: INTERFACE,
    function: "inventory",
    input: NativeImportInput::None,
    prepare: foreign,
}];

struct ExhaustingState {
    invocation: NativeSetupInvocation,
    account: NativeSetupAccount,
}
impl NativeImportState for ExhaustingState {
    fn native_import_invocation(&self) -> &NativeSetupInvocation {
        &self.invocation
    }
}
fn exhausted_factory(
    context: NativeImportContext<'_, ExhaustingState>,
) -> Result<NativeImportOperation> {
    let proof = context.proof.clone();
    let remaining = NATIVE_SETUP_MEMORY_BYTES - context.state().account.snapshot()?.charged_bytes;
    // Exhaust the real account by reservation, without a large physical test
    // allocation. FutureBlock admission itself must then take its real Err path.
    let held = context
        .state()
        .invocation
        .reserve(CapacityKind::VfsBuffer, remaining)?;
    assert!(
        context
            .inspect(|output| output.append(b"must not publish"))
            .is_err()
    );
    assert_eq!(proof.get().load(Ordering::Acquire), FAILED);
    drop(held);
    assert_eq!(proof.get().load(Ordering::Acquire), FAILED);
    bail!("caught factory allocation failure stays terminal")
}
static EXHAUST: [NativeImportBinding<ExhaustingState>; 1] = [NativeImportBinding {
    interface: INTERFACE,
    function: "inventory",
    input: NativeImportInput::None,
    prepare: exhausted_factory,
}];

#[tokio::test]
async fn native_fixed_import_real_future_allocation_failure_is_sticky_after_capacity_returns() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let component = component(&engine, false, false, false);
    let state = ExhaustingState {
        invocation: invocation.clone(),
        account: account.clone(),
    };
    let store = engine
        .native_setup_store_with_imports(invocation, state, &EXHAUST)
        .unwrap();
    let io = store.native_data_io().unwrap().clone();
    let mut entry = store
        .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
        .await
        .unwrap();
    assert!(entry.invoke(None).await.is_err());
    assert!(entry.observation().store_retired);
    drop(entry);
    io.retire_and_wait().await.unwrap();
    drop(io);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn native_fixed_import_foreign_operation_is_rejected_and_retains_original_funding() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    let mut accounts = Vec::new();
    for bindings in [&PARK, &FOREIGN] {
        let (account, invocation) = funding();
        let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
        let component = component(&engine, false, false, false);
        let store = engine
            .native_setup_store_with_imports(invocation.clone(), State { invocation }, bindings)
            .unwrap();
        let io = store.native_data_io().unwrap().clone();
        let mut entry = store
            .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
            .await
            .unwrap();
        assert!(entry.invoke(None).await.is_err());
        drop(entry);
        io.retire_and_wait().await.unwrap();
        drop(io);
        crate::reap_allocation_owners().unwrap();
        if accounts.is_empty() {
            assert!(account.snapshot().unwrap().charged_bytes > 0);
        }
        accounts.push(account);
    }
    assert!(PARKED.lock().unwrap().is_none());
    crate::reap_allocation_owners().unwrap();
    for account in accounts {
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    }
}
