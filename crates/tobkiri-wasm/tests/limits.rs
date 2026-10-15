use std::time::{Duration, Instant};
use tobkiri_canon::ids::{ProfileId, RequestId};
use tobkiri_gate::CoreCancellation as CancellationToken;
use tobkiri_gate::{CallCx, Principal, ResourceLimits};
use tobkiri_wasm::{WasmEngine, validate_imports};
use wasmtime::component::Component;
use wasmtime::{Instance, Module};

fn context(fuel: u64, memory_mib: u64, wall_ms: u64) -> CallCx {
    CallCx::root(
        ProfileId::parse("test-profile").unwrap(),
        Principal::User,
        RequestId::parse("test-request").unwrap(),
        CancellationToken::new(),
        Instant::now() + Duration::from_millis(wall_ms),
        ResourceLimits {
            fuel_per_call: fuel,
            memory_mib,
            wall_ms,
            table_elements: 1024,
            output_bytes: 1024 * 1024,
        },
    )
}
async fn run(engine: &WasmEngine, cx: CallCx, wat: &str) -> anyhow::Result<()> {
    let module = Module::new(engine.engine(), wat::parse_str(wat)?)?;
    let mut store = engine.store(cx, ())?;
    let instance = Instance::new_async(&mut store, &module, &[]).await?;
    instance
        .get_typed_func::<(), ()>(&mut store, "run")?
        .call_async(&mut store, ())
        .await?;
    Ok(())
}
const LOOP: &str = "(module (func (export \"run\") (loop $again (br $again))))";
const OK: &str = "(module (func (export \"run\")))";

#[tokio::test]
async fn exhausted_fuel_isolated_from_the_next_instance() {
    let engine = WasmEngine::new().unwrap();
    let error = run(&engine, context(10_000, 1, 5000), LOOP)
        .await
        .unwrap_err();
    assert!(error.to_string().contains("wasm"));
    run(&engine, context(10_000, 1, 5000), OK).await.unwrap();
}

#[tokio::test]
async fn original_deadline_interrupts_an_import_free_loop() {
    let engine = WasmEngine::new().unwrap();
    let cx = context(u64::MAX, 1, 40);
    let result = tokio::time::timeout(Duration::from_secs(3), run(&engine, cx, LOOP)).await;
    assert!(result.unwrap().is_err());
    run(&engine, context(10_000, 1, 5000), OK).await.unwrap();
}

#[tokio::test]
async fn cancellation_epoch_grace_interrupts_an_import_free_loop() {
    let engine = WasmEngine::new().unwrap();
    let cx = context(u64::MAX, 1, 5000);
    let cancel = cx.cancel().clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(30)).await;
        cancel.cancel();
    });
    let result = tokio::time::timeout(Duration::from_secs(3), run(&engine, cx, LOOP)).await;
    assert!(result.unwrap().is_err());
    run(&engine, context(10_000, 1, 5000), OK).await.unwrap();
}

#[tokio::test]
async fn memory_bomb_and_aggregate_multiple_memories_are_bounded() {
    let engine = WasmEngine::new().unwrap();
    let bomb = "(module (memory 1) (func (export \"run\") i32.const 100 memory.grow drop))";
    assert!(run(&engine, context(10_000, 1, 5000), bomb).await.is_err());
    let aggregate = "(module (memory 9) (memory 9) (func (export \"run\")))";
    assert!(
        run(&engine, context(10_000, 1, 5000), aggregate)
            .await
            .is_err()
    );
    run(&engine, context(10_000, 1, 5000), OK).await.unwrap();
}

#[tokio::test]
async fn undeclared_core_network_and_ambient_wasi_imports_fail_before_loading() {
    let engine = WasmEngine::new().unwrap();
    for name in [
        "tobkiri:core/network@1.0.0",
        "wasi:sockets/tcp@0.2.6",
        "wasi:http/outgoing-handler@0.2.6",
    ] {
        let wat = format!("(component (import \"{name}\" (instance)))");
        let component = Component::new(engine.engine(), wat::parse_str(&wat).unwrap()).unwrap();
        assert!(validate_imports(engine.engine(), &component, &Default::default()).is_err());
    }
}
