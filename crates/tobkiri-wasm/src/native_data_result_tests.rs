//! Real component tests; the owner is a portable fixture, never OS proof.
use super::*;
use std::sync::Arc;
use tobkiri_canon::Digest;
use tobkiri_gate::{
    CapacityLedger, CoreControl, GateError, NativeSetupAccount, NativeSetupInvocation,
    NativeSetupLease,
};

struct Proof;
impl NativeSetupLease for Proof {
    fn validate(&self) -> Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"native-result-portable-owner-not-os")
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
fn contract<'a>(success: NativeDataSuccessType) -> NativeDataResultContract<'a> {
    NativeDataResultContract {
        success,
        error: NativeDataErrorContract {
            code_field: "code",
            message_field: "message",
            cases: &["invalid", "internal"],
        },
    }
}
fn component(
    engine: &crate::WasmEngine,
    success: NativeDataSuccessType,
    error_len: usize,
    code: u32,
    post_trap: bool,
) -> Component {
    let ok = match success {
        NativeDataSuccessType::String => "string ",
        NativeDataSuccessType::Unit => "",
    };
    let pages = (512 + error_len).div_ceil(65536).max(1);
    let fill = if error_len == 9 {
        String::new()
    } else {
        format!("i32.const 512 i32.const 120 i32.const {error_len} memory.fill")
    };
    let post = if post_trap {
        "unreachable"
    } else {
        "global.get $posts i32.const 1 i32.add global.set $posts"
    };
    let source = format!(
        r#"(component
        (type $code-definition (enum "invalid" "internal"))
        (export $code "error-code" (type $code-definition))
        (type $error-definition (record (field "code" $code) (field "message" string)))
        (export $error "error" (type $error-definition))
        (type $reply (result {ok}(error $error)))
        (core module $m
            (memory (export "memory") {pages})
            (global $next (mut i32) (i32.const 4096))
            (global $calls (mut i32) (i32.const 0))
            (global $posts (mut i32) (i32.const 0))
            (data (i32.const 400) "okay")
            (data (i32.const 512) "bad input")
            (func (export "realloc") (param i32 i32 i32 i32) (result i32)
                local.get 0 local.get 1 i32.or if unreachable end
                local.get 2 i32.const 1 i32.ne if unreachable end
                local.get 3 memory.size i32.const 16 i32.shl global.get $next i32.sub i32.gt_u
                if unreachable end
                global.get $next global.get $next local.get 3 i32.add global.set $next)
            (func (export "post") (param i32) {post})
            (func (export "run") (param i32 i32) (result i32) (local $kind i32)
                global.get $calls global.get $posts i32.ne if unreachable end
                local.get 1 if local.get 0 i32.load8_u local.set $kind end
                local.get $kind i32.const 33 i32.eq if unreachable end
                local.get $kind i32.const 108 i32.eq if (loop $again (br $again)) end
                global.get $calls i32.const 1 i32.add global.set $calls
                local.get 1 i32.eqz
                if
                    {fill}
                    i32.const 0 i32.const 1 i32.store
                    i32.const 4 i32.const {code} i32.store
                    i32.const 8 i32.const 512 i32.store
                    i32.const 12 i32.const {error_len} i32.store
                else
                    i32.const 0 i32.const 0 i32.store
                    i32.const 4 i32.const 400 i32.store
                    i32.const 8 i32.const 4 i32.store
                end
                i32.const 0))
        (core instance $i (instantiate $m))
        (alias core export $i "memory" (core memory $memory))
        (alias core export $i "realloc" (core func $realloc))
        (alias core export $i "post" (core func $post))
        (alias core export $i "run" (core func $run))
        (func $run-value (param "input" string) (result $reply)
            (canon lift (core func $run) (memory $memory) (realloc $realloc) (post-return $post)))
        (export "run" (func $run-value)))"#
    );
    Component::new(engine.engine(), wat::parse_str(source).unwrap()).unwrap()
}

#[tokio::test]
async fn normal_domain_error_completes_post_return_and_reuses_original_instance() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let deadline = invocation.deadline().unwrap();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = component(&engine, NativeDataSuccessType::String, 9, 0, false);
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let owners = store.allocation_owners().clone();
    let empty = CoreBuffer::copy_from(b"", owners.graph()).unwrap();
    let input = CoreBuffer::copy_from(b"x", owners.graph()).unwrap();
    let mut entry = store
        .prepare_result_data_entry(
            &module,
            NativeDataExport::new(None, "run"),
            contract(NativeDataSuccessType::String),
        )
        .await
        .unwrap();
    let before = entry.fuel_remaining().unwrap();
    let error = entry.invoke(Some(&empty)).await.unwrap().unwrap_err();
    assert_eq!(error.code().as_str().unwrap(), "invalid");
    assert_eq!(error.message().as_str().unwrap(), "bad input");
    assert!(entry.observation().post_return && !entry.observation().store_retired);
    let fuel = entry.fuel_remaining().unwrap();
    assert!(fuel < before);
    let NativeDataValue::String(output) = entry.invoke(Some(&input)).await.unwrap().unwrap() else {
        panic!("string result required");
    };
    assert_eq!(output.as_str().unwrap(), "okay");
    assert!(entry.fuel_remaining().unwrap() < fuel);
    assert_eq!(entry.original_deadline(), deadline);
    drop(entry);
    drop(owners);
    drop(input);
    drop(empty);
    drop(output);
    assert!(account.snapshot().unwrap().charged_bytes > 0);
    assert_eq!(error.message().as_str().unwrap(), "bad input");
    drop(error);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn unit_initialization_result_is_not_a_fabricated_string() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = component(&engine, NativeDataSuccessType::Unit, 9, 0, false);
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let input = CoreBuffer::copy_from(b"x", store.allocation_owners().graph()).unwrap();
    let mut entry = store
        .prepare_result_data_entry(
            &module,
            NativeDataExport::new(None, "run"),
            contract(NativeDataSuccessType::Unit),
        )
        .await
        .unwrap();
    assert!(matches!(
        entry.invoke(Some(&input)).await.unwrap().unwrap(),
        NativeDataValue::Unit
    ));
    assert!(entry.observation().post_return);
    drop(entry);
    drop(input);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn native_error_contract_checks_fields_cases_and_order_exactly() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    for (field, cases) in [
        ("wrong", &["invalid", "internal"][..]),
        ("code", &["internal", "invalid"][..]),
        ("code", &["invalid", "another"][..]),
    ] {
        let (account, invocation) = funding();
        let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
        let module = component(&engine, NativeDataSuccessType::String, 9, 0, false);
        let mut expected = contract(NativeDataSuccessType::String);
        expected.error.code_field = field;
        expected.error.cases = cases;
        assert!(
            engine
                .native_setup_store(invocation, ())
                .unwrap()
                .prepare_result_data_entry(&module, NativeDataExport::new(None, "run"), expected)
                .await
                .is_err()
        );
        crate::reap_allocation_owners().unwrap();
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    }
}

#[tokio::test]
async fn result_and_plain_string_or_unit_contracts_are_not_interchangeable() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    for plain in [true, false] {
        let (account, invocation) = funding();
        let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
        let module = component(&engine, NativeDataSuccessType::Unit, 9, 0, false);
        let store = engine.native_setup_store(invocation, ()).unwrap();
        if plain {
            assert!(
                store
                    .prepare_data_entry(&module, NativeDataExport::new(None, "run"))
                    .await
                    .is_err()
            );
        } else {
            assert!(
                store
                    .prepare_result_data_entry(
                        &module,
                        NativeDataExport::new(None, "run"),
                        contract(NativeDataSuccessType::String)
                    )
                    .await
                    .is_err()
            );
        }
        crate::reap_allocation_owners().unwrap();
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    }
}

#[tokio::test]
async fn body_trap_is_not_returned_as_a_normal_domain_error() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = component(&engine, NativeDataSuccessType::String, 9, 0, false);
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let input = CoreBuffer::copy_from(b"!", store.allocation_owners().graph()).unwrap();
    let mut entry = store
        .prepare_result_data_entry(
            &module,
            NativeDataExport::new(None, "run"),
            contract(NativeDataSuccessType::String),
        )
        .await
        .unwrap();
    assert!(entry.invoke(Some(&input)).await.is_err());
    let seen = entry.observation();
    assert!(seen.unreachable_trap && seen.store_retired && !seen.post_return_failed);
    assert!(entry.invoke(Some(&input)).await.is_err());
    drop(input);
    drop(entry);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn post_return_trap_cannot_be_a_body_denial_or_domain_error() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = component(&engine, NativeDataSuccessType::String, 9, 0, true);
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let input = CoreBuffer::copy_from(b"", store.allocation_owners().graph()).unwrap();
    let mut entry = store
        .prepare_result_data_entry(
            &module,
            NativeDataExport::new(None, "run"),
            contract(NativeDataSuccessType::String),
        )
        .await
        .unwrap();
    assert!(entry.invoke(Some(&input)).await.is_err());
    let seen = entry.observation();
    assert!(
        seen.post_return_failed
            && seen.store_retired
            && !seen.unreachable_trap
            && !seen.post_return
    );
    drop(input);
    drop(entry);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn unknown_error_enum_is_an_abi_failure_not_a_domain_error() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = component(&engine, NativeDataSuccessType::String, 9, 255, false);
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let input = CoreBuffer::copy_from(b"", store.allocation_owners().graph()).unwrap();
    let mut entry = store
        .prepare_result_data_entry(
            &module,
            NativeDataExport::new(None, "run"),
            contract(NativeDataSuccessType::String),
        )
        .await
        .unwrap();
    assert!(entry.invoke(Some(&input)).await.is_err());
    assert!(entry.observation().store_retired && !entry.observation().unreachable_trap);
    drop(input);
    drop(entry);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn oversized_error_message_fails_after_post_return_without_truncation() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = component(
        &engine,
        NativeDataSuccessType::String,
        MAX_BYTES + 1,
        0,
        false,
    );
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let input = CoreBuffer::copy_from(b"", store.allocation_owners().graph()).unwrap();
    let mut entry = store
        .prepare_result_data_entry(
            &module,
            NativeDataExport::new(None, "run"),
            contract(NativeDataSuccessType::String),
        )
        .await
        .unwrap();
    assert!(entry.invoke(Some(&input)).await.is_err());
    assert!(entry.observation().post_return && entry.observation().store_retired);
    drop(input);
    drop(entry);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn dropped_result_call_future_retires_the_same_store() {
    use std::future::Future;
    use std::task::{Context, Waker};
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let deadline = invocation.deadline().unwrap();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = component(&engine, NativeDataSuccessType::String, 9, 0, false);
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let input = CoreBuffer::copy_from(b"loop", store.allocation_owners().graph()).unwrap();
    let mut entry = store
        .prepare_result_data_entry(
            &module,
            NativeDataExport::new(None, "run"),
            contract(NativeDataSuccessType::String),
        )
        .await
        .unwrap();
    {
        let mut call = std::pin::pin!(entry.invoke(Some(&input)));
        assert!(
            call.as_mut()
                .poll(&mut Context::from_waker(Waker::noop()))
                .is_pending()
        );
    }
    assert!(entry.observation().store_retired);
    assert_eq!(entry.original_deadline(), deadline);
    assert!(entry.invoke(Some(&input)).await.is_err());
    drop(input);
    drop(entry);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}
