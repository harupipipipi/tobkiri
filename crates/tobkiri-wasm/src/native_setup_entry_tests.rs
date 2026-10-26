//! Genuine component tests for native DATA entry custody and call behavior.
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
        Digest::hash(b"native-data-entry-fixture")
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

fn string_component(
    engine: &crate::WasmEngine,
    imports: &str,
    extra_exports: &str,
) -> Result<Component> {
    let source = format!(
        r#"(component
            {imports}
            (type $text-result (result string (error string)))
            (core module $m
                (memory (export "memory") 1)
                (global $next (mut i32) (i32.const 1024))
                (global $post-count (mut i32) (i32.const 0))
                (data (i32.const 300) "after")
                (func (export "realloc")
                    (param i32 i32 i32 i32) (result i32)
                    local.get 0 local.get 1 i32.or
                    if unreachable end
                    local.get 2 i32.const 1 i32.ne
                    if unreachable end
                    local.get 3 i32.const 65536 global.get $next i32.sub i32.gt_u
                    if unreachable end
                    global.get $next
                    global.get $next local.get 3 i32.add global.set $next)
                (func (export "post-return") (param i32)
                    global.get $post-count i32.const 1 i32.add global.set $post-count
                    i32.const 1024 global.set $next)
                (func (export "run") (param i32 i32) (result i32)
                    global.get $post-count
                    if (result i32)
                        i32.const 0 i32.const 300 i32.store
                        i32.const 4 i32.const 5 i32.store
                        i32.const 0
                    else
                        i32.const 0 local.get 0 i32.store
                        i32.const 4 local.get 1 i32.store
                        i32.const 0
                    end)
                (func (export "empty") (result i32)
                    i32.const 0 i32.const 300 i32.store
                    i32.const 4 i32.const 5 i32.store
                    i32.const 0)
                (func (export "invoke")
                    (param i32 i32 i32 i32) (result i32)
                    unreachable))
            (core instance $i (instantiate $m))
            (alias core export $i "memory" (core memory $memory))
            (alias core export $i "realloc" (core func $realloc))
            (alias core export $i "post-return" (core func $post-return))
            (alias core export $i "run" (core func $run))
            (alias core export $i "empty" (core func $empty))
            (alias core export $i "invoke" (core func $invoke))
            (func $run-value (param "input" string) (result string)
                (canon lift (core func $run) (memory $memory) (realloc $realloc)
                    (post-return $post-return)))
            (func $empty-value (result string)
                (canon lift (core func $empty) (memory $memory) (realloc $realloc)
                    (post-return $post-return)))
            (func $invoke-value
                (param "operation-id" string) (param "payload-json" string)
                (result $text-result)
                (canon lift (core func $invoke) (memory $memory) (realloc $realloc)
                    (post-return $post-return)))
            (instance $entry
                (export "run" (func $run-value))
                (export "empty" (func $empty-value)))
            (export "local:native-test/data@1.0.0" (instance $entry))
            {extra_exports})"#
    );
    Ok(Component::new(engine.engine(), wat::parse_str(source)?)?)
}

fn uint_component(engine: &crate::WasmEngine) -> Result<Component> {
    Ok(Component::new(
        engine.engine(),
        wat::parse_str(
            r#"(component
                (core module $m
                    (func (export "value") (result i32) i32.const 17))
                (core instance $i (instantiate $m))
                (alias core export $i "value" (core func $value))
                (func $value (result u32) (canon lift (core func $value)))
                (export "value" (func $value)))"#,
        )?,
    )?)
}

fn clock_component(engine: &crate::WasmEngine) -> Result<Component> {
    let source = r#"(component
        (type $wall-clock (instance
            (type $datetime-definition
                (record (field "seconds" u64) (field "nanoseconds" u32)))
            (export "datetime" (type $datetime (eq $datetime-definition)))
            (export "now" (func (result $datetime)))
            (export "resolution" (func (result $datetime)))))
        (import "wasi:clocks/wall-clock@0.2.4"
            (instance $wall (type $wall-clock)))
        (type $monotonic-clock (instance
            (export "now" (func (result u64)))
            (export "resolution" (func (result u64)))))
        (import "tobkiri-ui:clock/monotonic@1.0.0"
            (instance $monotonic (type $monotonic-clock)))
        (alias export $wall "now" (func $wall-now))
        (alias export $wall "resolution" (func $wall-resolution))
        (alias export $monotonic "now" (func $monotonic-now))
        (alias export $monotonic "resolution" (func $monotonic-resolution))
        (core module $storage
            (memory (export "memory") 1)
            (global $next (mut i32) (i32.const 1024))
            (func (export "realloc")
                (param $old i32) (param $old-size i32)
                (param $align i32) (param $size i32) (result i32)
                (local $ptr i32)
                local.get $size i32.eqz
                if (result i32)
                    i32.const 0
                else
                    global.get $next
                    local.get $align i32.const 1 i32.sub i32.add
                    i32.const 0
                    local.get $align i32.const 1 i32.sub i32.sub
                    i32.and
                    local.tee $ptr
                    local.get $size i32.add global.set $next
                    local.get $ptr
                end)
            (func (export "post-return") (param i32)
                i32.const 1024 global.set $next))
        (core instance $storage (instantiate $storage))
        (alias core export $storage "memory" (core memory $memory))
        (alias core export $storage "realloc" (core func $realloc))
        (alias core export $storage "post-return" (core func $post-return))
        (core func $wall-now-lowered
            (canon lower (func $wall-now) (memory $memory) (realloc $realloc)))
        (core func $wall-resolution-lowered
            (canon lower (func $wall-resolution) (memory $memory) (realloc $realloc)))
        (core func $monotonic-now-lowered
            (canon lower (func $monotonic-now)))
        (core func $monotonic-resolution-lowered
            (canon lower (func $monotonic-resolution)))
        (core module $entry
            (import "host" "memory" (memory 1))
            (import "host" "wall-now" (func $wall-now (param i32)))
            (import "host" "wall-resolution" (func $wall-resolution (param i32)))
            (import "host" "monotonic-now" (func $monotonic-now (result i64)))
            (import "host" "monotonic-resolution" (func $monotonic-resolution (result i64)))
            (data (i32.const 300) "clocked")
            (func (export "run") (result i32)
                i32.const 64 call $wall-now
                i32.const 80 call $wall-resolution
                call $monotonic-now drop
                call $monotonic-resolution drop
                i32.const 0 i32.const 300 i32.store
                i32.const 4 i32.const 7 i32.store
                i32.const 0))
        (core instance $entry (instantiate $entry
            (with "host" (instance
                (export "memory" (memory $memory))
                (export "wall-now" (func $wall-now-lowered))
                (export "wall-resolution" (func $wall-resolution-lowered))
                (export "monotonic-now" (func $monotonic-now-lowered))
                (export "monotonic-resolution" (func $monotonic-resolution-lowered))))))
        (alias core export $entry "run" (core func $run))
        (func $run-value (result string)
            (canon lift (core func $run) (memory $memory) (realloc $realloc)
                (post-return $post-return)))
        (export "run" (func $run-value)))"#;
    Ok(Component::new(engine.engine(), wat::parse_str(source)?)?)
}

#[tokio::test]
async fn retained_owned_string_calls_share_fuel_deadline_and_post_return() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let expected_deadline = invocation.deadline().unwrap();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let component = string_component(&engine, "", "").unwrap();
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let mut entry = store
        .prepare_data_entry(
            &component,
            NativeDataExport::new(Some("local:native-test/data@1.0.0"), "run"),
        )
        .await
        .unwrap();
    assert_eq!(entry.original_deadline(), expected_deadline);

    let owners = entry.store.as_ref().unwrap().allocation_owners().clone();
    let input = CoreBuffer::copy_from(b"first value", owners.graph()).unwrap();
    let initial_fuel = entry.fuel_remaining().unwrap();
    let first = entry.invoke(Some(&input)).await.unwrap();
    assert_eq!(first.as_str().unwrap(), "first value");
    let first_fuel = entry.fuel_remaining().unwrap();
    let second = entry.invoke(Some(&input)).await.unwrap();
    assert_eq!(second.as_str().unwrap(), "after");
    let second_fuel = entry.fuel_remaining().unwrap();

    assert!(first_fuel < initial_fuel);
    assert!(second_fuel < first_fuel);
    assert_eq!(entry.original_deadline(), expected_deadline);
    assert_eq!(first.as_str().unwrap(), "first value");

    entry.retire();
    assert!(entry.fuel_remaining().is_err());
    assert!(account.snapshot().unwrap().charged_bytes > 0);
    drop(second);
    drop(first);
    drop(input);
    drop(owners);
    drop(entry);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn zero_argument_string_export_is_supported() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let component = string_component(&engine, "", "").unwrap();
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let mut entry = store
        .prepare_data_entry(
            &component,
            NativeDataExport::new(Some("local:native-test/data@1.0.0"), "empty"),
        )
        .await
        .unwrap();
    let output = entry.invoke(None).await.unwrap();
    assert_eq!(output.as_str().unwrap(), "after");
    entry.retire();
    drop(output);
    drop(entry);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn exact_wall_and_monotonic_clock_imports_call_through_owned_abi() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let expected_deadline = invocation.deadline().unwrap();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let component = clock_component(&engine).unwrap();
    let mut imports: Vec<_> = component
        .component_type()
        .imports(engine.engine())
        .map(|(name, _)| name.to_owned())
        .collect();
    imports.sort();
    assert_eq!(
        imports,
        [MONOTONIC_CLOCK_IMPORT, WALL_CLOCK_IMPORT]
            .into_iter()
            .map(str::to_owned)
            .collect::<Vec<_>>()
    );

    let store = engine.native_setup_store(invocation, ()).unwrap();
    let mut entry = store
        .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
        .await
        .unwrap();
    let initial_fuel = entry.fuel_remaining().unwrap();
    let output = entry.invoke(None).await.unwrap();
    assert_eq!(output.as_str().unwrap(), "clocked");
    assert!(entry.fuel_remaining().unwrap() < initial_fuel);
    assert_eq!(entry.original_deadline(), expected_deadline);
    let clock_calls = entry
        .store
        .as_ref()
        .unwrap()
        .store
        .as_ref()
        .unwrap()
        .data()
        .clock_observation
        .clock_calls;
    assert_eq!(clock_calls, 4);
    let observed = entry.observation();
    assert_eq!(observed.clock_calls, 4);
    assert_eq!(observed.clock_completed, 4);
    assert_eq!((observed.wall_now, observed.wall_resolution), (1, 1));
    assert_eq!(
        (observed.monotonic_now, observed.monotonic_resolution),
        (1, 1)
    );
    assert!(observed.wall_resolution_ns.is_some_and(|value| value > 0));
    assert!(
        observed
            .monotonic_resolution_ns
            .is_some_and(|value| value > 0)
    );
    assert!(observed.post_return && !observed.store_retired);
    assert!(account.snapshot().unwrap().charged_bytes > 0);

    drop(output);
    drop(entry);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn ordinary_core_and_pack_identities_are_rejected_before_initialization() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let component = string_component(
        &engine,
        "",
        r#"
        (export "invoke" (func $invoke-value))
        (instance $lifecycle (export "init" (func $run-value)))
        (export "tobkiri:core/lifecycle@1.0.0" (instance $lifecycle))
        (instance $pack (export "invoke" (func $invoke-value)))
        (export "tobkiri:pack/pack@0.1.0" (instance $pack))
        "#,
    )
    .unwrap();
    let store = engine.native_setup_store(invocation, ()).unwrap();
    assert!(
        store
            .prepare_data_entry(
                &component,
                NativeDataExport::new(Some("local:native-test/data@1.0.0"), "run",),
            )
            .await
            .is_err()
    );
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    crate::reap_allocation_owners().unwrap();
}

#[tokio::test]
async fn ordinary_pack_invoke_shape_cannot_match_the_data_entry_abi() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let component =
        string_component(&engine, "", r#"(export "invoke" (func $invoke-value))"#).unwrap();
    let store = engine.native_setup_store(invocation, ()).unwrap();
    assert!(
        store
            .prepare_data_entry(&component, NativeDataExport::new(None, "invoke"))
            .await
            .is_err()
    );
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    crate::reap_allocation_owners().unwrap();
}

#[tokio::test]
async fn unsupported_and_malformed_imports_fail_closed() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    for import in [
        r#"(type $bytes (list u8))
           (import "wasi:random/random@0.2.6" (instance $random
               (export "get-random-bytes"
                   (func (param "len" u64) (result $bytes)))))"#,
        r#"(import "tobkiri-ui:clock/monotonic@1.0.0" (instance $clock
               (export "now" (func (result u32)))
               (export "resolution" (func (result u64))))"#,
    ] {
        let (account, invocation) = funding();
        let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
        let component = string_component(&engine, import, "").unwrap();
        let store = engine.native_setup_store(invocation, ()).unwrap();
        assert!(
            store
                .prepare_data_entry(
                    &component,
                    NativeDataExport::new(Some("local:native-test/data@1.0.0"), "run",),
                )
                .await
                .is_err()
        );
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
        crate::reap_allocation_owners().unwrap();
    }
}

#[tokio::test]
async fn wrong_export_signature_is_rejected_without_a_store_leak() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let component = uint_component(&engine).unwrap();
    let store = engine.native_setup_store(invocation, ()).unwrap();
    assert!(
        store
            .prepare_data_entry(&component, NativeDataExport::new(None, "value"))
            .await
            .is_err()
    );
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    crate::reap_allocation_owners().unwrap();
}

#[tokio::test]
async fn output_envelope_failure_occurs_after_post_return_and_drops_store() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let output_len = MAX_BYTES + 1;
    let source = format!(
        r#"(component
            (core module $m
                (memory (export "memory") 65)
                (func (export "realloc")
                    (param i32 i32 i32 i32) (result i32) i32.const 1024)
                (func (export "post-return") (param i32))
                (func (export "run") (result i32)
                    i32.const 1024 i32.const 120 i32.const {output_len} memory.fill
                    i32.const 0 i32.const 1024 i32.store
                    i32.const 4 i32.const {output_len} i32.store
                    i32.const 0))
            (core instance $i (instantiate $m))
            (alias core export $i "memory" (core memory $memory))
            (alias core export $i "realloc" (core func $realloc))
            (alias core export $i "post-return" (core func $post-return))
            (alias core export $i "run" (core func $run))
            (func $run-value (result string)
                (canon lift (core func $run) (memory $memory) (realloc $realloc)
                    (post-return $post-return)))
            (export "run" (func $run-value)))"#
    );
    let component = Component::new(engine.engine(), wat::parse_str(source).unwrap()).unwrap();
    let store = engine.native_setup_store(invocation, ()).unwrap();
    let mut entry = store
        .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
        .await
        .unwrap();

    // The owned Wasmtime call must lift and post-return the result before the
    // fixed DATA envelope rejects it and retires the actual Store.
    let error = entry.invoke(None).await.unwrap_err();
    assert!(format!("{error:#}").contains("4 MiB string envelope"));
    assert!(entry.store.is_none());
    let observed = entry.observation();
    assert!(observed.post_return && observed.store_retired);
    assert!(
        observed
            .fuel_remaining
            .is_some_and(|fuel| fuel < super::super::FUEL)
    );
    // Retirement keeps only the original deadline metadata, never a new lease.
    assert!(entry.original_deadline() > Instant::now());
    drop(entry);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn dropping_a_suspended_call_destroys_store_and_prevents_reuse() {
    use std::future::Future;
    use std::task::{Context, Poll, Waker};

    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let deadline = invocation.deadline().unwrap();
    let engine = crate::WasmEngine::new_with_control(invocation.control()).unwrap();
    let component = Component::new(
        engine.engine(),
        wat::parse_str(
            r#"(component
        (core module $m
            (memory (export "memory") 1)
            (func (export "run") (result i32)
                (loop $forever (br $forever)) unreachable))
        (core instance $i (instantiate $m))
        (alias core export $i "memory" (core memory $memory))
        (alias core export $i "run" (core func $run))
        (func $run-value (result string)
            (canon lift (core func $run) (memory $memory)))
        (export "run" (func $run-value)))"#,
        )
        .unwrap(),
    )
    .unwrap();
    let mut entry = engine
        .native_setup_store(invocation, ())
        .unwrap()
        .prepare_data_entry(&component, NativeDataExport::new(None, "run"))
        .await
        .unwrap();
    {
        let mut call = std::pin::pin!(entry.invoke(None));
        let mut context = Context::from_waker(Waker::noop());
        assert!(matches!(call.as_mut().poll(&mut context), Poll::Pending));
    }
    assert!(entry.store.is_none());
    assert!(entry.observation().store_retired);
    assert!(!entry.observation().post_return);
    assert_eq!(entry.original_deadline(), deadline);
    assert!(entry.invoke(None).await.is_err());
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}
