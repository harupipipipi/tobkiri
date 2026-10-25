//! Genuine Wasmtime allocations with a portable injected accounting proof.
//! Actual native root proof is covered by the supported Core/Store suites.
use super::*;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;
use tobkiri_canon::Digest;
use tobkiri_gate::{CapacityLedger, CoreControl, NativeSetupAccount, NativeSetupLease};
use wasmtime::{
    Instance, Memory, MemoryType, Module, Ref, RefType, ResourceLimiter, Table, TableType,
};

struct Proof;
impl NativeSetupLease for Proof {
    fn validate(&self) -> Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"native-store-fixture")
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

struct ObserveDrop {
    account: NativeSetupAccount,
    observed_charge: Arc<AtomicU64>,
}
impl Drop for ObserveDrop {
    fn drop(&mut self) {
        self.observed_charge.store(
            self.account.snapshot().unwrap().charged_bytes,
            Ordering::Release,
        );
    }
}

#[tokio::test]
async fn actual_guest_memory_remains_charged_through_store_data_destruction() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let observed = Arc::new(AtomicU64::new(0));
    let mut owned = engine
        .native_setup_store(
            invocation,
            ObserveDrop {
                account: account.clone(),
                observed_charge: observed.clone(),
            },
        )
        .unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, WASM_STACK_BYTES);
    {
        let store = owned.store.as_mut().unwrap();
        Memory::new_async(&mut *store, MemoryType::new(2, None))
            .await
            .unwrap();
    }
    let expected = WASM_STACK_BYTES + 2 * 65536;
    assert_eq!(account.snapshot().unwrap().charged_bytes, expected);
    drop(engine);
    // Retiring starts cleanup; it must not refund still-owned memory.
    owned.retire();
    assert_eq!(account.snapshot().unwrap().charged_bytes, expected);
    drop(owned);
    assert_eq!(observed.load(Ordering::Acquire), expected);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn shared_host_reservation_and_stack_deny_real_guest_growth_before_allocation() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let mut owned = engine.native_setup_store(invocation.clone(), ()).unwrap();
    let store = owned.store.as_mut().unwrap();
    assert!(
        Memory::new_async(&mut *store, MemoryType::new(1024, None))
            .await
            .is_err()
    );
    assert_eq!(account.snapshot().unwrap().charged_bytes, WASM_STACK_BYTES);
    let memory = Memory::new_async(&mut *store, MemoryType::new(2, None))
        .await
        .unwrap();
    let remaining = NATIVE_SETUP_MEMORY_BYTES - account.snapshot().unwrap().charged_bytes;
    // A host copy's preallocation reservation, not a physically allocated buffer.
    let output = invocation
        .reserve(CapacityKind::AbiMarshal, remaining)
        .unwrap();
    assert!(memory.grow_async(&mut *store, 1).await.is_err());
    assert_eq!(memory.size(&*store), 2);
    assert_eq!(
        account.snapshot().unwrap().charged_bytes,
        NATIVE_SETUP_MEMORY_BYTES
    );
    drop(output);
    assert_eq!(memory.grow_async(&mut *store, 1).await.unwrap(), 2);
    assert_eq!(memory.size(&*store), 3);
    drop(owned);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn calls_keep_original_deadline_and_spent_fuel_then_retire_without_refund() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let expected_deadline = invocation.deadline().unwrap();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let module = Module::new(engine.engine(), wat::parse_str(
        "(module (func (export \"work\") (local i32) i32.const 1000 local.set 0 (loop $again local.get 0 i32.const 1 i32.sub local.tee 0 br_if $again)))"
    ).unwrap()).unwrap();
    let mut owned = engine.native_setup_store(invocation.clone(), 42).unwrap();
    assert!(engine.native_setup_store(invocation.clone(), 43).is_err());
    assert_eq!(*owned.data(), 42);
    assert_eq!(owned.original_deadline(), expected_deadline);
    assert_eq!(owned.fuel_remaining().unwrap(), FUEL);
    let function = {
        let store = owned.store.as_mut().unwrap();
        let instance = Instance::new_async(&mut *store, &module, &[])
            .await
            .unwrap();
        instance
            .get_typed_func::<(), ()>(&mut *store, "work")
            .unwrap()
    };
    function
        .call_async(owned.store.as_mut().unwrap(), ())
        .await
        .unwrap();
    let after_first = owned.fuel_remaining().unwrap();
    function
        .call_async(owned.store.as_mut().unwrap(), ())
        .await
        .unwrap();
    assert!(after_first < FUEL && owned.fuel_remaining().unwrap() < after_first);
    assert_eq!(owned.original_deadline(), expected_deadline);
    owned.retire();
    engine.engine().increment_epoch();
    let result = function.call_async(owned.store.as_mut().unwrap(), ()).await;
    let Err(error) = result else {
        panic!("retired native Store ran");
    };
    assert!(format!("{error:#}").contains("component instance retired"));
    assert_eq!(account.snapshot().unwrap().charged_bytes, WASM_STACK_BYTES);
    drop(owned);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    assert!(engine.native_setup_store(invocation, 44).is_err());
}

#[tokio::test]
async fn another_engine_account_or_exhausted_root_cannot_construct_a_store() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let unrelated = CoreControl::default();
    let wrong_engine = WasmEngine::new_with_control(&unrelated).unwrap();
    assert!(
        wrong_engine
            .native_setup_store(invocation.clone(), ())
            .is_err()
    );
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let held = invocation
        .reserve(CapacityKind::VfsBuffer, NATIVE_SETUP_MEMORY_BYTES)
        .unwrap();
    assert!(engine.native_setup_store(invocation.clone(), ()).is_err());
    assert_eq!(
        account.snapshot().unwrap().charged_bytes,
        NATIVE_SETUP_MEMORY_BYTES
    );
    drop(held);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    // A valid attempt that failed admission cannot silently reset fuel later.
    assert!(engine.native_setup_store(invocation, ()).is_err());
}

#[tokio::test]
async fn preadmission_memory_type_rejection_cannot_refund_live_memory() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let mut owned = engine.native_setup_store(invocation, ()).unwrap();
    let store = owned.store.as_mut().unwrap();
    let memory = Memory::new_async(&mut *store, MemoryType::new(2, None))
        .await
        .unwrap();
    for pages in 2..=3 {
        let expected = WASM_STACK_BYTES + pages * 65536;
        assert_eq!(account.snapshot().unwrap().charged_bytes, expected);
        assert!(
            memory
                .grow_async(&mut *store, u32::MAX as u64)
                .await
                .is_err()
        );
        assert_eq!(memory.size(&*store), pages);
        assert_eq!(store.data().limits.memory_used as u64, pages * 65536);
        assert_eq!(account.snapshot().unwrap().charged_bytes, expected);
        if pages == 2 {
            assert_eq!(memory.grow_async(&mut *store, 1).await.unwrap(), pages);
        }
    }
    drop(owned);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn preadmission_table_overflow_cannot_refund_live_elements() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let mut owned = engine.native_setup_store(invocation, ()).unwrap();
    let store = owned.store.as_mut().unwrap();
    let table = Table::new_async(
        &mut *store,
        TableType::new(RefType::FUNCREF, 2, None),
        Ref::Func(None),
    )
    .await
    .unwrap();
    for elements in 2..=3 {
        let expected = WASM_STACK_BYTES + elements * size_of::<usize>() as u64 * 2;
        assert_eq!(account.snapshot().unwrap().charged_bytes, expected);
        assert!(
            table
                .grow_async(&mut *store, u64::MAX, Ref::Func(None))
                .await
                .is_err()
        );
        assert_eq!(table.size(&*store), elements);
        assert_eq!(store.data().limits.table_used as u64, elements);
        assert_eq!(account.snapshot().unwrap().charged_bytes, expected);
        if elements == 2 {
            assert_eq!(
                table
                    .grow_async(&mut *store, 1, Ref::Func(None))
                    .await
                    .unwrap(),
                elements
            );
        }
    }
    drop(owned);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

// Only this callback-contract fixture deliberately delegates declared maxima to
// Wasmtime, to force its deterministic post-admission failure path. Production
// AggregateLimits continues checking maxima before permitting any allocation.
struct MaximumFailureProbe(AggregateLimits);
impl ResourceLimiter for MaximumFailureProbe {
    fn memory_growing(
        &mut self,
        current: usize,
        desired: usize,
        _maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        self.0.memory_growing(current, desired, None)
    }
    fn memory_grow_failed(&mut self, error: wasmtime::Error) -> wasmtime::Result<()> {
        self.0.memory_grow_failed(error)
    }
    fn table_growing(
        &mut self,
        current: usize,
        desired: usize,
        _maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        self.0.table_growing(current, desired, None)
    }
    fn table_grow_failed(&mut self, error: wasmtime::Error) -> wasmtime::Result<()> {
        self.0.table_grow_failed(error)
    }
}

#[tokio::test]
async fn admitted_maximum_failure_refunds_only_that_attempts_memory_or_table_delta() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let physical = PhysicalOwner::new(&invocation).unwrap();
    let mut store = Store::new(
        engine.engine(),
        MaximumFailureProbe(AggregateLimits {
            memory_cap: NATIVE_SETUP_MEMORY_BYTES as usize,
            table_cap: TABLE_ELEMENTS,
            memory_used: 0,
            table_used: 0,
            last_memory_growth: 0,
            last_table_growth: 0,
            physical: Some(physical.physical()),
            last_physical_memory: 0,
            last_physical_table: 0,
        }),
    );
    store.limiter(|state| state);
    let memory = Memory::new_async(&mut store, MemoryType::new(2, Some(3)))
        .await
        .unwrap();
    let table = Table::new_async(
        &mut store,
        TableType::new(RefType::FUNCREF, 2, Some(3)),
        Ref::Func(None),
    )
    .await
    .unwrap();
    for live in 2..=3 {
        let expected = WASM_STACK_BYTES + live * (65536 + size_of::<usize>() as u64 * 2);
        assert_eq!(account.snapshot().unwrap().charged_bytes, expected);
        assert!(memory.grow_async(&mut store, 2).await.is_err());
        assert_eq!(memory.size(&store), live);
        assert_eq!(account.snapshot().unwrap().charged_bytes, expected);
        assert_eq!(store.data().0.memory_used as u64, live * 65536);
        assert_eq!(store.data().0.last_physical_memory, 0);
        assert!(
            table
                .grow_async(&mut store, 2, Ref::Func(None))
                .await
                .is_err()
        );
        assert_eq!(table.size(&store), live);
        assert_eq!(account.snapshot().unwrap().charged_bytes, expected);
        assert_eq!(store.data().0.table_used as u64, live);
        assert_eq!(store.data().0.last_physical_table, 0);
        if live == 2 {
            assert_eq!(memory.grow_async(&mut store, 1).await.unwrap(), live);
            assert_eq!(
                table
                    .grow_async(&mut store, 1, Ref::Func(None))
                    .await
                    .unwrap(),
                live
            );
        }
    }
    drop(store);
    assert!(account.snapshot().unwrap().charged_bytes > 0);
    drop(physical);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn actual_graph_and_component_copies_remain_charged_after_store_destruction() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let owned = engine.native_setup_store(invocation, ()).unwrap();
    let owners = owned.allocation_owners().clone();
    let graph = tobkiri_canon::CoreValue::string("retained graph", owners.graph()).unwrap();
    let graph_bytes = account.snapshot().unwrap().charged_bytes - WASM_STACK_BYTES;
    assert!(graph_bytes > 0);
    let shared = graph.clone();
    assert_eq!(
        account.snapshot().unwrap().charged_bytes,
        WASM_STACK_BYTES + graph_bytes
    );
    let component = wasmtime::component::BudgetedString::copy_from(
        "retained component copy",
        owners.component(),
        0,
    )
    .unwrap();
    let both_bytes = account.snapshot().unwrap().charged_bytes - WASM_STACK_BYTES;
    assert!(both_bytes > graph_bytes);
    drop(owned);
    assert_eq!(account.snapshot().unwrap().charged_bytes, both_bytes);
    assert!(tobkiri_canon::CoreValue::string("after retirement", owners.graph()).is_err());
    drop(graph);
    assert_eq!(account.snapshot().unwrap().charged_bytes, both_bytes);
    drop(component);
    assert_eq!(account.snapshot().unwrap().charged_bytes, graph_bytes);
    drop(shared);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    drop(owners);
    crate::reap_allocation_owners().unwrap();
}

#[tokio::test]
async fn graph_and_component_share_checked_depth_bytes_and_irrevocable_host_work() {
    use tobkiri_canon::{AllocationError, AllocationKind, AllocationRequest, MAX_DEPTH};
    use wasmtime::component::{HostAllocationError, HostAllocationKind, HostAllocationRequest};
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let owned = engine.native_setup_store(invocation, ()).unwrap();
    let owners = owned.allocation_owners();
    let request = AllocationRequest {
        kind: AllocationKind::Temporary,
        bytes: 0,
        elements: 0,
        depth: 0,
        work: 0,
    };
    assert_eq!(
        owners.graph().reserve(AllocationRequest {
            depth: MAX_DEPTH as u32 + 1,
            ..request
        }),
        Err(AllocationError::Depth)
    );
    // These are preallocation-admission checks, not actual buffer allocations.
    owners
        .graph()
        .reserve(AllocationRequest {
            bytes: 1,
            ..request
        })
        .unwrap();
    assert_eq!(
        owners.graph().reserve(AllocationRequest {
            bytes: u64::MAX,
            ..request
        }),
        Err(AllocationError::Overflow)
    );
    assert_eq!(
        account.snapshot().unwrap().charged_bytes,
        WASM_STACK_BYTES + 1
    );
    owners
        .component()
        .reserve(HostAllocationRequest {
            kind: HostAllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: FUEL,
        })
        .unwrap();
    owners.graph().release(1);
    assert_eq!(account.snapshot().unwrap().charged_bytes, WASM_STACK_BYTES);
    assert_eq!(
        owners
            .graph()
            .clone()
            .reserve(AllocationRequest { work: 1, ..request }),
        Err(AllocationError::Limit)
    );
    assert_eq!(
        owners.component().reserve(HostAllocationRequest {
            kind: HostAllocationKind::Temporary,
            bytes: 0,
            elements: 1,
            depth: 0,
            work: 0,
        }),
        Err(HostAllocationError::Limit)
    );
    assert_eq!(owned.fuel_remaining().unwrap(), FUEL);
    drop(owned);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn codec_admission_honors_expiry_and_cancellation_but_allows_real_cleanup() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let cancel = CoreCancellation::new_with_control(invocation.control()).unwrap();
    let retirement = tokio_util::sync::CancellationToken::new();
    // A test-only already-expired deadline exercises admission, without sleep
    // or changing the production constructor's fixed original 30-second limit.
    let expired = crate::allocation::native_owners(
        &invocation,
        Instant::now() - Duration::from_secs(1),
        &cancel,
        &retirement,
        FUEL,
    )
    .unwrap();
    assert!(tobkiri_canon::CoreValue::string("expired", expired.graph()).is_err());
    drop(expired);
    crate::reap_allocation_owners().unwrap();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let owned = engine.native_setup_store(invocation, ()).unwrap();
    let owners = owned.allocation_owners().clone();
    let value = tobkiri_canon::CoreValue::string("live before cancel", owners.graph()).unwrap();
    assert!(account.snapshot().unwrap().charged_bytes > WASM_STACK_BYTES);
    owned.cancellation().cancel();
    assert!(tobkiri_canon::CoreValue::string("cancelled", owners.graph()).is_err());
    drop(value);
    assert_eq!(account.snapshot().unwrap().charged_bytes, WASM_STACK_BYTES);
    drop(owned);
    drop(owners);
    crate::reap_allocation_owners().unwrap();
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
}

#[tokio::test]
async fn codec_weak_handle_retains_original_metadata_until_final_block_reap() {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let (account, invocation) = funding();
    let control = invocation.control().clone();
    let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
    let owned = engine.native_setup_store(invocation, ()).unwrap();
    let owners = owned.allocation_owners().clone();
    let weak = Arc::downgrade(owners.graph());
    let value = tobkiri_canon::CoreValue::string("physical value", owners.graph()).unwrap();
    drop(owned);
    assert!(account.snapshot().unwrap().charged_bytes > 0);
    drop(value);
    drop(owners);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    assert_eq!(crate::reap_allocation_owners().unwrap(), 0);
    assert!(weak.upgrade().is_some());
    let held_metadata = control.usage().bytes;
    let upgraded = weak.upgrade().unwrap();
    drop(weak);
    assert_eq!(crate::reap_allocation_owners().unwrap(), 0);
    assert_eq!(control.usage().bytes, held_metadata);
    drop(upgraded);
    assert_eq!(crate::reap_allocation_owners().unwrap(), 1);
    assert!(control.usage().bytes < held_metadata);
}
