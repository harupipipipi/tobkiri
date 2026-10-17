use std::{
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::ids::{ProfileId, RequestId};
use tobkiri_gate::{
    CallCx, ControlKind, CoreCancellation, CoreControl, CoreControlLimits, Principal,
    ResourceLimits,
};
use tobkiri_wasm::{InvocationState, WasmEngine};
use wasmtime::{
    Store,
    component::{
        Component, HostAllocationBudget, HostAllocationError, HostAllocationRequest, Linker,
    },
};

// Pinned async Wasmtime retains its default 2 MiB fiber here. This finite
// fixture account is separate from the unchanged guest linear-memory limit.
const HOST_ALLOCATION_CAP: u64 = 4 * 1024 * 1024;
struct Meter {
    used: AtomicU64,
    peak: AtomicU64,
}
impl HostAllocationBudget for Meter {
    fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError> {
        let previous = self
            .used
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(request.bytes)
                    .filter(|next| *next <= HOST_ALLOCATION_CAP)
            })
            .map_err(|_| HostAllocationError::Limit)?;
        self.peak
            .fetch_max(previous + request.bytes, Ordering::AcqRel);
        Ok(())
    }
    fn release(&self, bytes: u64) {
        self.used
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_sub(bytes)
            })
            .expect("release only actual reserved storage");
    }
}

fn funded_store(
    engine: &WasmEngine,
    control: &CoreControl,
) -> (Store<InvocationState<()>>, Arc<Meter>) {
    let account = Arc::new(Meter {
        used: AtomicU64::new(0),
        peak: AtomicU64::new(0),
    });
    let mut store = engine.store(context(control), ()).unwrap();
    store.host_allocation_budget(account.clone());
    (store, account)
}

fn assert_host_allocations_released(account: &Meter) {
    let peak = account.peak.load(Ordering::Acquire);
    assert!(peak > 0 && peak <= HOST_ALLOCATION_CAP);
    assert_eq!(account.used.load(Ordering::Acquire), 0);
}

fn context(control: &CoreControl) -> CallCx {
    CallCx::try_root_with_control(
        ProfileId::parse("test-profile").unwrap(),
        Principal::User,
        RequestId::parse("component-structure").unwrap(),
        CoreCancellation::new_with_control(control).unwrap(),
        Instant::now() + Duration::from_secs(30),
        ResourceLimits {
            fuel_per_call: 10_000,
            memory_mib: 1,
            wall_ms: 30_000,
            table_elements: 16,
            output_bytes: 1024,
        },
        control.clone(),
    )
    .unwrap()
}

fn repeated_source(count: usize, module_body: &str) -> String {
    let mut source = format!("(component (core module $m {module_body})");
    for _ in 0..count {
        source.push_str("(core instance (instantiate $m))");
    }
    source.push(')');
    source
}

fn repeated_component(engine: &WasmEngine, count: usize, module_body: &str) -> Component {
    let source = repeated_source(count, module_body);
    Component::new(engine.engine(), wat::parse_str(&source).unwrap()).unwrap()
}

fn nested_empty_component(engine: &WasmEngine, count_per_chunk: usize) -> Component {
    // Each component remains below Wasmtime's independent 1,000-instance
    // validation bound. Reusing the chunk exercises the actual per-Store sum.
    let chunk = repeated_source(count_per_chunk, "");
    let source = format!(
        "(component (component $chunk {})
            (instance (instantiate $chunk))
            (instance (instantiate $chunk)))",
        chunk
            .strip_prefix("(component ")
            .unwrap()
            .strip_suffix(')')
            .unwrap(),
    );
    Component::new(engine.engine(), wat::parse_str(&source).unwrap()).unwrap()
}

#[tokio::test]
async fn composed_component_admits_more_than_sixteen_core_instances() {
    let control = CoreControl::default();
    let engine = WasmEngine::new_with_control(&control).unwrap();
    let component = repeated_component(&engine, 17, "");
    let (mut store, account) = funded_store(&engine, &control);
    Linker::new(engine.engine())
        .instantiate_async(&mut store, &component)
        .await
        .unwrap();
    drop(store);
    assert_host_allocations_released(&account);
}

#[tokio::test]
async fn empty_core_instances_still_have_a_finite_store_limit() {
    let control = CoreControl::default();
    let engine = WasmEngine::new_with_control(&control).unwrap();
    let at_limit = nested_empty_component(&engine, 512);
    let (mut store, account) = funded_store(&engine, &control);
    Linker::new(engine.engine())
        .instantiate_async(&mut store, &at_limit)
        .await
        .unwrap();
    drop(store);
    assert_host_allocations_released(&account);

    let over_limit = nested_empty_component(&engine, 513);
    let (mut store, account) = funded_store(&engine, &control);
    let error = Linker::new(engine.engine())
        .instantiate_async(&mut store, &over_limit)
        .await
        .unwrap_err();
    assert!(
        format!("{error:#}").contains("instance count too high at 1025"),
        "{error:#}"
    );
    drop(store);
    assert_host_allocations_released(&account);
}

#[tokio::test]
async fn additional_structure_capacity_keeps_aggregate_guest_limits() {
    let control = CoreControl::default();
    let engine = WasmEngine::new_with_control(&control).unwrap();
    for (module, expected) in [
        ("(memory 9)", "aggregate guest memory limit exceeded"),
        ("(table 9 funcref)", "aggregate guest table limit exceeded"),
    ] {
        let component = repeated_component(&engine, 2, module);
        let (mut store, account) = funded_store(&engine, &control);
        let error = Linker::new(engine.engine())
            .instantiate_async(&mut store, &component)
            .await
            .unwrap_err();
        assert!(format!("{error:#}").contains(expected), "{error:#}");
        drop(store);
        assert_host_allocations_released(&account);
    }
}

#[tokio::test]
async fn store_structure_admission_precedes_construction() {
    for limits in [
        CoreControlLimits {
            max_frames: 0,
            ..Default::default()
        },
        CoreControlLimits {
            total_bytes: 128 * 1024,
            ..Default::default()
        },
    ] {
        let control = CoreControl::new(limits).unwrap();
        let engine = WasmEngine::new_with_control(&control).unwrap();
        let cx = context(&control);
        let before = control.usage();
        assert!(engine.store(cx.clone(), ()).is_err());
        assert_eq!(control.usage(), before);
    }
}

#[tokio::test]
async fn default_core_control_admits_twelve_warm_store_envelopes() {
    let control = CoreControl::default();
    let engine = WasmEngine::new_with_control(&control).unwrap();
    let cx = context(&control);
    let before = control.usage();
    let mut stores = Vec::new();
    for _ in 0..12 {
        stores.push(engine.store(cx.clone(), ()).unwrap());
    }
    assert_eq!(control.usage().count(ControlKind::Frame), 12);
    assert!(control.usage().bytes - before.bytes < 16 * 1024 * 1024);
    cx.cancel().cancel();
    assert_eq!(control.usage().count(ControlKind::Frame), 12);
    drop(stores);
    assert_eq!(control.usage(), before);
}

#[tokio::test]
async fn store_structure_custody_survives_user_data_and_releases_after_store_destruction() {
    struct ObserveDataDrop(CoreControl);
    impl Drop for ObserveDataDrop {
        fn drop(&mut self) {
            // Wasmtime drops user data before its native StoreOpaque.
            assert_eq!(self.0.usage().count(ControlKind::Frame), 1);
        }
    }

    let control = CoreControl::default();
    let engine = WasmEngine::new_with_control(&control).unwrap();
    let cx = context(&control);
    let before = control.usage();
    let store = engine
        .store(cx.clone(), ObserveDataDrop(control.clone()))
        .unwrap();
    assert_eq!(control.usage().count(ControlKind::Frame), 1);
    drop(store);
    assert_eq!(control.usage(), before);

    let store = engine.store(cx, ()).unwrap();
    let data = store.into_data();
    assert_eq!(control.usage().count(ControlKind::Frame), 0);
    drop(data);
}
