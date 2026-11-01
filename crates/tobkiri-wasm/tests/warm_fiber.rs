//! Warm native fibers keep their original physical stack custody while each
//! new invocation uses the currently active producing frame for admission.
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use std::time::{Duration, Instant};
use tobkiri_canon::ids::{ProfileId, RequestId};
use tobkiri_gate::{CallCx, ControlKind, CoreCancellation, CoreControl, Principal, ResourceLimits};
use tobkiri_wasm::WasmEngine;
use wasmtime::component::{
    Component, HostAllocationBudget, HostAllocationError, HostAllocationRequest, Linker, Val,
};

#[derive(Default)]
struct SharedAccount {
    used: AtomicU64,
    peak: AtomicU64,
}
struct ProducingFrame {
    account: Arc<SharedAccount>,
    active: AtomicBool,
    reserves: AtomicU64,
}
impl ProducingFrame {
    fn new(account: Arc<SharedAccount>) -> Arc<Self> {
        Arc::new(Self {
            account,
            active: AtomicBool::new(true),
            reserves: AtomicU64::new(0),
        })
    }
}
impl HostAllocationBudget for ProducingFrame {
    fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError> {
        self.reserves.fetch_add(1, Ordering::AcqRel);
        if !self.active.load(Ordering::Acquire) {
            return Err(HostAllocationError::Unavailable);
        }
        let old = self
            .account
            .used
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(request.bytes)
                    .filter(|next| *next <= 16 * 1024 * 1024)
            })
            .map_err(|_| HostAllocationError::Limit)?;
        self.account
            .peak
            .fetch_max(old + request.bytes, Ordering::AcqRel);
        Ok(())
    }
    fn release(&self, bytes: u64) {
        // Actual physical destruction remains valid after frame retirement.
        self.account
            .used
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_sub(bytes)
            })
            .expect("release only actually held storage");
    }
}

#[tokio::test]
async fn cached_fiber_reuses_stack_after_old_frame_retires_without_new_old_owner_charge() {
    let control = CoreControl::default();
    let engine = WasmEngine::new_with_control(&control).unwrap();
    let cx = CallCx::try_root_with_control(
        ProfileId::parse("warm-fiber").unwrap(),
        Principal::User,
        RequestId::parse("warm-fiber").unwrap(),
        CoreCancellation::new_with_control(&control).unwrap(),
        Instant::now() + Duration::from_secs(10),
        ResourceLimits {
            fuel_per_call: 1_000_000,
            memory_mib: 16,
            wall_ms: 10_000,
            table_elements: 1024,
            output_bytes: 1024,
        },
        control.clone(),
    )
    .unwrap();
    let component = Component::new(
        engine.engine(),
        wat::parse_str(
            r#"
        (component
            (core module $m (func (export "value") (result i32) i32.const 7))
            (core instance $i (instantiate $m))
            (alias core export $i "value" (core func $value))
            (func (export "value") (result u32) (canon lift (core func $value))))
    "#,
        )
        .unwrap(),
    )
    .unwrap();
    let account = Arc::new(SharedAccount::default());
    let old = ProducingFrame::new(account.clone());
    let mut store = engine.store(cx, ()).unwrap();
    store.host_allocation_budget(old.clone());
    let instance = Linker::new(engine.engine())
        .instantiate_async(&mut store, &component)
        .await
        .unwrap();
    let function = instance.get_func(&mut store, "value").unwrap();
    let result = function.call_async_owned(&mut store, &[]).await.unwrap();
    assert!(matches!(result.as_slice(), [Val::U32(7)]));
    drop(result);
    let warm_charge = account.used.load(Ordering::Acquire);
    assert!(
        warm_charge >= 2 * 1024 * 1024,
        "the actual cached stack must remain funded"
    );
    let controls = control.usage();
    assert_eq!(controls.count(ControlKind::Frame), 1);

    old.active.store(false, Ordering::Release);
    assert!(
        old.reserve(HostAllocationRequest {
            kind: wasmtime::component::HostAllocationKind::Metadata,
            bytes: 1,
            elements: 1,
            depth: 0,
            work: 1,
        })
        .is_err(),
        "a retired frame really cannot sponsor new storage"
    );
    let old_requests = old.reserves.load(Ordering::Acquire);
    let current = ProducingFrame::new(account.clone());
    store.host_allocation_budget(current.clone());
    for _ in 0..3 {
        let result = function.call_async_owned(&mut store, &[]).await.unwrap();
        assert!(matches!(result.as_slice(), [Val::U32(7)]));
        drop(result);
        assert_eq!(
            account.used.load(Ordering::Acquire),
            warm_charge,
            "new call control must not accumulate in the cached physical stack lease"
        );
        assert_eq!(control.usage().bytes, controls.bytes);
        assert_eq!(
            old.reserves.load(Ordering::Acquire),
            old_requests,
            "warm reuse must not resurrect the retired producing frame"
        );
    }
    assert!(
        current.reserves.load(Ordering::Acquire) > 0,
        "the current live owner must still admit each call"
    );
    assert!(account.peak.load(Ordering::Acquire) <= 16 * 1024 * 1024);
    drop(store);
    assert_eq!(account.used.load(Ordering::Acquire), 0);
    assert_eq!(control.usage().count(ControlKind::Frame), 0);
}
