//! No-network pinned Wasmtime export fixtures. Like byte_list_abi, the fixture
//! uses an explicit bounded native owner through the public component API.
//! These tests establish typed traps/fuel at an actual async export, not Core
//! Gate/Profile admission, MiMo completion or the cause of an earlier live error.

use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};
use std::time::Duration;
use wasmtime::component::{
    CanonicalCallStage, Component, HostAllocationBudget, HostAllocationError,
    HostAllocationRequest, Linker, Val,
};
use wasmtime::{Config, Engine, Store, Trap};

#[derive(Default)]
struct FixtureOwner {
    live: AtomicU64,
    requests: AtomicU64,
}

impl HostAllocationBudget for FixtureOwner {
    fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError> {
        self.requests.fetch_add(1, Ordering::AcqRel);
        self.live
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                live.checked_add(request.bytes)
                    .filter(|next| *next <= 32 * 1024 * 1024)
            })
            .map(|_| ())
            .map_err(|_| HostAllocationError::Limit)
    }

    fn release(&self, bytes: u64) {
        self.live
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                live.checked_sub(bytes)
            })
            .expect("fixture only retires physical storage it actually owned");
    }
}

async fn fixture() -> (Store<()>, wasmtime::component::Instance, Arc<FixtureOwner>) {
    let mut config = Config::new();
    config
        .wasm_component_model(true)
        .async_support(true)
        .consume_fuel(true);
    let engine = Engine::new(&config).unwrap();
    let component = Component::new(
        &engine,
        wat::parse_str(
            r#"(component
                (core module $m
                    (func (export "spin") (result i32)
                        (loop $again br $again) unreachable)
                    (func (export "value") (result i32) i32.const 7))
                (core instance $i (instantiate $m))
                (func (export "spin") (result u32)
                    (canon lift (core func $i "spin")))
                (func (export "value") (result u32)
                    (canon lift (core func $i "value"))))"#,
        )
        .unwrap(),
    )
    .unwrap();
    let owner = Arc::new(FixtureOwner::default());
    let mut store = Store::new(&engine, ());
    store.host_allocation_budget(owner.clone());
    store.set_fuel(10_000).unwrap();
    store.fuel_async_yield_interval(Some(1_000)).unwrap();
    let instance = Linker::new(&engine)
        .instantiate_async(&mut store, &component)
        .await
        .unwrap();
    (store, instance, owner)
}

#[tokio::test]
async fn finite_fuel_export_preserves_guest_stage_typed_trap_and_owned_store_measurement() {
    let (mut store, instance, owner) = fixture().await;
    let function = instance.get_func(&mut store, "spin").unwrap();
    let before = store.get_fuel().unwrap();
    let error = tokio::time::timeout(
        Duration::from_secs(10),
        function.call_async_owned(&mut store, &[]),
    )
    .await
    .expect("finite guest fuel must stop the fixture, not its timeout")
    .unwrap_err();
    assert!(before > 0);
    assert_eq!(error.downcast_ref::<Trap>(), Some(&Trap::OutOfFuel));
    assert_eq!(
        error
            .downcast_ref::<CanonicalCallStage>()
            .map(|stage| stage.as_str()),
        Some("guest")
    );
    // This is the same still-owned Store from the failed call. Reading fuel
    // neither reinstantiates the component nor renews any execution budget.
    assert_eq!(store.get_fuel().unwrap(), 0);
    assert_eq!(store.get_fuel().unwrap(), 0);
    assert!(owner.requests.load(Ordering::Acquire) > 0);
    drop(error);
    drop(store);
    assert_eq!(owner.live.load(Ordering::Acquire), 0);
}

#[tokio::test]
async fn successful_export_keeps_its_value_and_positive_remaining_fuel() {
    let (mut store, instance, owner) = fixture().await;
    let function = instance.get_func(&mut store, "value").unwrap();
    let before = store.get_fuel().unwrap();
    let result = function.call_async_owned(&mut store, &[]).await.unwrap();
    assert!(matches!(result.as_slice(), [Val::U32(7)]));
    let after = store.get_fuel().unwrap();
    assert!(after > 0 && after < before);
    assert!(owner.requests.load(Ordering::Acquire) > 0);
    drop(result);
    drop(store);
    assert_eq!(owner.live.load(Ordering::Acquire), 0);
}
