//! Actual multi-export components; portable ownership fixture, not OS/GUI proof.
use super::*;
use crate::{NativeDataErrorContract, NativeDataSuccessType, WasmEngine};
use std::sync::Arc;
use tobkiri_canon::Digest;
use tobkiri_gate::{
    CapacityLedger, CoreControl, GateError, NativeSetupAccount, NativeSetupInvocation,
    NativeSetupLease,
};

struct Proof;
impl NativeSetupLease for Proof {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"multi-export-portable-not-os")
    }
}
fn funding() -> (NativeSetupAccount, NativeSetupInvocation) {
    let control = CoreControl::default();
    let ledger = CapacityLedger::new();
    let account = ledger
        .native_setup_account(&control, || Ok(Arc::new(Proof)))
        .unwrap();
    let invocation = account.begin().unwrap();
    (account, invocation)
}
fn result(success: NativeDataSuccessType) -> NativeDataResultContract<'static> {
    NativeDataResultContract {
        success,
        error: NativeDataErrorContract {
            code_field: "code",
            message_field: "message",
            cases: &["invalid", "internal"],
        },
    }
}
fn bindings() -> [NativeDataBinding<'static>; 3] {
    [
        NativeDataBinding {
            export: NativeDataExport::new(None, "initialize"),
            result: Some(result(NativeDataSuccessType::Unit)),
        },
        NativeDataBinding {
            export: NativeDataExport::new(None, "step"),
            result: Some(result(NativeDataSuccessType::String)),
        },
        NativeDataBinding {
            export: NativeDataExport::new(None, "view"),
            result: None,
        },
    ]
}
fn text(value: NativeDataValue) -> CoreBuffer {
    let NativeDataValue::String(value) = value else {
        panic!("expected actual string")
    };
    value
}
fn component(engine: &WasmEngine, start_trap: bool) -> Component {
    let start = if start_trap {
        "(func $start unreachable) (start $start)"
    } else {
        ""
    };
    let wat = format!(
        r#"(component
        (type $code-definition (enum "invalid" "internal"))
        (export $code "error-code" (type $code-definition))
        (type $error-definition (record (field "code" $code) (field "message" string)))
        (export $error "error" (type $error-definition))
        (type $unit (result (error $error)))
        (type $string (result string (error $error)))
        (core module $m
            {start}
            (memory (export "memory") 1)
            (global $next (mut i32) (i32.const 4096))
            (global $initialized (mut i32) (i32.const 0))
            (global $value (mut i32) (i32.const 0))
            (global $pending (mut i32) (i32.const 0))
            (global $post-trap (mut i32) (i32.const 0))
            (data (i32.const 400) "0")
            (data (i32.const 512) "not ready")
            (func (export "realloc") (param i32 i32 i32 i32) (result i32)
                local.get 0 local.get 1 i32.or if unreachable end
                local.get 2 i32.const 1 i32.ne if unreachable end
                local.get 3 i32.const 65536 global.get $next i32.sub i32.gt_u if unreachable end
                global.get $next global.get $next local.get 3 i32.add global.set $next)
            (func $begin
                global.get $pending if unreachable end
                i32.const 1 global.set $pending)
            (func (export "post") (param i32)
                global.get $post-trap if unreachable end
                global.get $pending i32.const 1 i32.ne if unreachable end
                i32.const 0 global.set $pending)
            (func (export "initialize") (result i32)
                call $begin
                i32.const 1 global.set $initialized
                i32.const 0 i32.const 0 i32.store
                i32.const 0)
            (func (export "step") (param i32 i32) (result i32) (local $kind i32)
                call $begin
                local.get 1 if local.get 0 i32.load8_u local.set $kind end
                local.get $kind i32.const 33 i32.eq if unreachable end
                local.get $kind i32.const 108 i32.eq if (loop $again (br $again)) end
                local.get $kind i32.const 112 i32.eq if i32.const 1 global.set $post-trap end
                global.get $initialized
                if
                    global.get $value i32.const 1 i32.add global.set $value
                    i32.const 400 global.get $value i32.const 48 i32.add i32.store8
                    i32.const 0 i32.const 0 i32.store
                    i32.const 4 i32.const 400 i32.store
                    i32.const 8 i32.const 1 i32.store
                else
                    i32.const 0 i32.const 1 i32.store
                    i32.const 4 i32.const 0 i32.store
                    i32.const 8 i32.const 512 i32.store
                    i32.const 12 i32.const 9 i32.store
                end
                i32.const 0)
            (func (export "view") (result i32)
                call $begin
                i32.const 64 i32.const 400 i32.store
                i32.const 68 i32.const 1 i32.store
                i32.const 64))
        (core instance $i (instantiate $m))
        (alias core export $i "memory" (core memory $memory))
        (alias core export $i "realloc" (core func $realloc))
        (alias core export $i "post" (core func $post))
        (alias core export $i "initialize" (core func $init))
        (alias core export $i "step" (core func $step))
        (alias core export $i "view" (core func $view))
        (func $initialize (result $unit)
            (canon lift (core func $init) (memory $memory) (realloc $realloc) (post-return $post)))
        (func $step-value (param "input" string) (result $string)
            (canon lift (core func $step) (memory $memory) (realloc $realloc) (post-return $post)))
        (func $view-value (result string)
            (canon lift (core func $view) (memory $memory) (realloc $realloc) (post-return $post)))
        (export "initialize" (func $initialize))
        (export "step" (func $step-value))
        (export "view" (func $view-value)))"#
    );
    Component::new(engine.engine(), wat::parse_str(wat).unwrap()).unwrap()
}

#[tokio::test]
async fn fixed_binding_metadata_uses_original_control_without_shrinking_payload_capacity() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let control = invocation.control().clone();
    let engine = WasmEngine::new_with_control(&control).unwrap();
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let before_control = control.usage().bytes;
    let before_payload = account.snapshot().unwrap().charged_bytes;
    let builder = BindingBuilder::new(&store, 12).unwrap();
    assert_eq!(builder.values.as_ref().unwrap().capacity(), 12);
    assert_eq!(account.snapshot().unwrap().charged_bytes, before_payload);
    assert!(
        control.usage().bytes
            >= before_control
                + (12 * size_of::<BoundFunction>() + Bindings::storage_bytes()) as u64
    );
    let table = builder.finish().unwrap();
    assert_eq!(table.get().capacity(), 12);
    assert_eq!(account.snapshot().unwrap().charged_bytes, before_payload);
    drop(table);
    assert_eq!(control.usage().bytes, before_control);
    drop(store);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn explicit_initialization_and_fixed_exports_share_state_and_original_budgets() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let deadline = invocation.deadline().unwrap();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = component(&engine, false);
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let input = CoreBuffer::copy_from(b"x", store.allocation_owners().graph()).unwrap();
    let mut program = store
        .prepare_data_program(&module, &bindings())
        .await
        .unwrap();
    let fuel = program.fuel_remaining().unwrap();
    let error = program.invoke(1, Some(&input)).await.unwrap().unwrap_err();
    assert_eq!(error.message().as_str().unwrap(), "not ready");
    assert!(program.observation().post_return && !program.observation().store_retired);
    assert!(matches!(
        program.invoke(0, None).await.unwrap().unwrap(),
        NativeDataValue::Unit
    ));
    let first = text(program.invoke(1, Some(&input)).await.unwrap().unwrap());
    assert_eq!(first.as_str().unwrap(), "1");
    let view = text(program.invoke(2, None).await.unwrap().unwrap());
    assert_eq!(view.as_str().unwrap(), "1");
    let second = text(program.invoke(1, Some(&input)).await.unwrap().unwrap());
    assert_eq!(second.as_str().unwrap(), "2");
    assert_eq!(
        first.as_str().unwrap(),
        "1",
        "owned output survives later guest writes"
    );
    assert!(program.fuel_remaining().unwrap() < fuel);
    assert_eq!(program.original_deadline(), deadline);
    drop((program, input, first, view, second));
    crate::reap_allocation_owners().unwrap();
    assert!(account.snapshot().unwrap().charged_bytes > 0);
    assert_eq!(error.message().as_str().unwrap(), "not ready");
    drop(error);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn every_binding_is_validated_before_the_same_component_can_instantiate() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    for missing in [false, true] {
        let (account, invocation) = funding();
        let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
        let module = component(&engine, true);
        let mut selected = bindings();
        if missing {
            selected[2].export.function = "absent";
        } else {
            selected[2].result = Some(result(NativeDataSuccessType::Unit));
        }
        let error = engine
            .native_setup_store(invocation, ())
            .unwrap()
            .prepare_data_program(&module, &selected)
            .await
            .err()
            .unwrap();
        let description = format!("{error:#}");
        assert!(description.contains(if missing {
            "function export missing"
        } else {
            "selected result type"
        }));
        assert!(
            error.downcast_ref::<wasmtime::Trap>().is_none(),
            "start must not run"
        );
        crate::reap_allocation_owners().unwrap();
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    }
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = component(&engine, true);
    assert!(
        engine
            .native_setup_store(invocation, ())
            .unwrap()
            .prepare_data_program(&module, &[])
            .await
            .is_err()
    );
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn invalid_slot_retires_instance_while_owned_output_remains_funded() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = component(&engine, false);
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let mut program = store
        .prepare_data_program(&module, &bindings())
        .await
        .unwrap();
    let output = text(program.invoke(2, None).await.unwrap().unwrap());
    assert!(program.invoke(3, None).await.is_err());
    assert!(program.observation().store_retired);
    assert!(program.fuel_remaining().is_err());
    assert!(program.invoke(0, None).await.is_err());
    drop(program);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(output.as_str().unwrap(), "0");
    assert!(account.snapshot().unwrap().charged_bytes > 0);
    drop(output);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn body_and_post_return_traps_retire_all_bound_exports_without_reclassification() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    for (input, post) in [(b"!".as_slice(), false), (b"p".as_slice(), true)] {
        let (account, invocation) = funding();
        let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
        let module = component(&engine, false);
        let store = engine.native_setup_store(invocation, ()).unwrap();
        let input = CoreBuffer::copy_from(input, store.allocation_owners().graph()).unwrap();
        let mut program = store
            .prepare_data_program(&module, &bindings())
            .await
            .unwrap();
        assert!(matches!(
            program.invoke(0, None).await.unwrap().unwrap(),
            NativeDataValue::Unit
        ));
        assert!(program.invoke(1, Some(&input)).await.is_err());
        let observed = program.observation();
        assert!(observed.store_retired);
        assert_eq!(observed.post_return_failed, post);
        assert_eq!(observed.unreachable_trap, !post);
        assert!(program.invoke(2, None).await.is_err());
        drop((program, input));
        crate::reap_allocation_owners().unwrap();
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    }
}

#[tokio::test]
async fn suspended_program_future_drop_prevents_every_later_export_call() {
    use std::{
        future::Future,
        task::{Context, Waker},
    };
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let deadline = invocation.deadline().unwrap();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = component(&engine, false);
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let input = CoreBuffer::copy_from(b"loop", store.allocation_owners().graph()).unwrap();
    let mut program = store
        .prepare_data_program(&module, &bindings())
        .await
        .unwrap();
    assert!(matches!(
        program.invoke(0, None).await.unwrap().unwrap(),
        NativeDataValue::Unit
    ));
    {
        let mut call = std::pin::pin!(program.invoke(1, Some(&input)));
        assert!(
            call.as_mut()
                .poll(&mut Context::from_waker(Waker::noop()))
                .is_pending()
        );
    }
    assert!(program.observation().store_retired);
    assert_eq!(program.original_deadline(), deadline);
    assert!(program.invoke(2, None).await.is_err());
    drop((program, input));
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn original_cancellation_is_shared_by_every_bound_export() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = component(&engine, false);
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let cancel = store.cancellation().clone();
    let mut program = store
        .prepare_data_program(&module, &bindings())
        .await
        .unwrap();
    cancel.cancel();
    assert!(program.invoke(0, None).await.is_err());
    assert!(program.observation().store_retired);
    assert!(program.invoke(2, None).await.is_err());
    drop((program, cancel));
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}
