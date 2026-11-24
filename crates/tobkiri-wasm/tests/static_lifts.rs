//! Real canonical-lower calls exercise the same generated static String/Vec
//! Lift implementation used by ordinary WASI host imports. Guest descriptors
//! deliberately alias a small fixed memory region; each owned copy is charged.
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
};
use tobkiri_canon::ids::{ProfileId, RequestId};
use tobkiri_gate::{CallCx, CoreCancellation, Principal, ResourceLimits};
use tobkiri_wasm::WasmEngine;
use wasmtime::component::{
    Component, HostAllocationBudget, HostAllocationError, HostAllocationRequest, Linker,
};

struct Meter {
    used: AtomicU64,
    peak: AtomicU64,
    cap: u64,
}
impl Meter {
    fn new(cap: u64) -> Arc<Self> {
        Arc::new(Self {
            used: AtomicU64::new(0),
            peak: AtomicU64::new(0),
            cap,
        })
    }
}
impl HostAllocationBudget for Meter {
    fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError> {
        let previous = self
            .used
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(request.bytes)
                    .filter(|next| *next <= self.cap)
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
fn component(engine: &wasmtime::Engine) -> Component {
    Component::new(
        engine,
        wat::parse_str(
            r#"
        (component
            (type $strings (list string))
            (type $bytes (list u8))
            (import "test:payload/receive@1.0.0" (instance $host
                (export "accept" (func (param "texts" $strings) (param "bytes" $bytes)))))
            (alias export $host "accept" (func $accept))
            (core module $storage (memory (export "memory") 1 1))
            (core instance $storage-instance (instantiate $storage))
            (alias core export $storage-instance "memory" (core memory $memory))
            (core func $accept-lowered (canon lower (func $accept) (memory $memory)))
            (core module $driver
                (import "storage" "memory" (memory 1 1))
                (import "host" "accept" (func $accept (param i32 i32 i32 i32)))
                (func $prepare (param $count i32) (local $i i32) (local $slot i32)
                    i32.const 4096 i32.const 97 i32.const 4096 memory.fill
                    i32.const 8192 i32.const 98 i32.const 2048 memory.fill
                    (loop $next
                        i32.const 1024 local.get $i i32.const 8 i32.mul i32.add local.tee $slot
                        i32.const 4096 i32.store
                        local.get $slot i32.const 4 i32.add i32.const 4096 i32.store
                        local.get $i i32.const 1 i32.add local.tee $i
                        local.get $count i32.lt_u br_if $next))
                (func (export "one")
                    i32.const 1 call $prepare
                    i32.const 1024 i32.const 1 i32.const 8192 i32.const 2048 call $accept)
                (func (export "aliases")
                    i32.const 64 call $prepare
                    i32.const 1024 i32.const 64 i32.const 8192 i32.const 2048 call $accept))
            (core instance $driver-instance (instantiate $driver
                (with "storage" (instance (export "memory" (memory $memory))))
                (with "host" (instance (export "accept" (func $accept-lowered))))))
            (alias core export $driver-instance "one" (core func $one))
            (alias core export $driver-instance "aliases" (core func $aliases))
            (func (export "one") (canon lift (core func $one)))
            (func (export "aliases") (canon lift (core func $aliases))))
    "#,
        )
        .unwrap(),
    )
    .unwrap()
}

#[test]
fn generated_static_lift_charges_repeated_alias_copies_before_host_callback() {
    let engine = wasmtime::Engine::default();
    let component = component(&engine);
    let mut linker: Linker<()> = Linker::new(&engine);
    let calls = Arc::new(AtomicUsize::new(0));
    let observed = calls.clone();
    let account = Meter::new(16 * 1024);
    let observed_account = account.clone();
    linker
        .instance("test:payload/receive@1.0.0")
        .unwrap()
        .func_wrap(
            "accept",
            move |_, (texts, bytes): (Vec<String>, Vec<u8>)| {
                assert_eq!(texts.len(), 1);
                assert_eq!(texts[0].len(), 4096);
                assert!(texts[0].bytes().all(|byte| byte == b'a'));
                assert_eq!(bytes.len(), 2048);
                assert!(bytes.iter().all(|byte| *byte == b'b'));
                assert!(
                    observed_account.used.load(Ordering::Acquire) >= 6144,
                    "both actual owned input payloads must remain funded during callback"
                );
                observed.fetch_add(1, Ordering::AcqRel);
                Ok(())
            },
        )
        .unwrap();
    let mut store = wasmtime::Store::new(&engine, ());
    store.host_allocation_budget(account.clone());
    let instance = linker.instantiate(&mut store, &component).unwrap();
    let one = instance.get_func(&mut store, "one").unwrap();
    let result = one.call_owned(&mut store, &[]).unwrap();
    assert!(result.is_empty());
    drop(result);
    assert_eq!(calls.load(Ordering::Acquire), 1);
    // Entering a call warms Store's FundedVec<CallContext> scope storage.
    // pop retires the scope, while its real allocated capacity stays charged
    // until Store destruction. The completed 6144-byte input is not retained.
    let baseline = account.used.load(Ordering::Acquire);
    assert!(baseline < 6144);
    drop(one.call_owned(&mut store, &[]).unwrap());
    assert_eq!(account.used.load(Ordering::Acquire), baseline);
    let aliases = instance.get_func(&mut store, "aliases").unwrap();
    let error = aliases.call_owned(&mut store, &[]).unwrap_err();
    assert!(
        matches!(
            error.downcast_ref::<HostAllocationError>(),
            Some(HostAllocationError::Limit)
        ),
        "{error:#}"
    );
    assert_eq!(
        calls.load(Ordering::Acquire),
        2,
        "aliased expansion must fail before callback"
    );
    assert!(account.peak.load(Ordering::Acquire) <= account.cap);
    drop(store);
    drop(error);
    assert_eq!(account.used.load(Ordering::Acquire), 0);
}

struct AsyncState {
    account: Arc<Meter>,
    block: AtomicBool,
    entered: tokio::sync::Notify,
    calls: AtomicUsize,
    dropped: AtomicUsize,
    baseline: AtomicU64,
}
struct HeldInputs {
    texts: Vec<String>,
    bytes: Vec<u8>,
    state: Arc<AsyncState>,
}
impl Drop for HeldInputs {
    fn drop(&mut self) {
        let payload = self.texts.iter().map(String::len).sum::<usize>() + self.bytes.len();
        assert_eq!(payload, 6144);
        assert!(
            self.state.account.used.load(Ordering::Acquire)
                >= self.state.baseline.load(Ordering::Acquire) + payload as u64,
            "actual input storage must remain charged until its future destroys the inputs"
        );
        self.state.dropped.fetch_add(1, Ordering::AcqRel);
    }
}
fn context() -> CallCx {
    CallCx::root(
        ProfileId::parse("static-lift-test").unwrap(),
        Principal::User,
        RequestId::parse("pending-native-import").unwrap(),
        CoreCancellation::new(),
        std::time::Instant::now() + std::time::Duration::from_secs(10),
        ResourceLimits {
            fuel_per_call: 1_000_000,
            memory_mib: 16,
            wall_ms: 10_000,
            table_elements: 1024,
            output_bytes: 1024 * 1024,
        },
    )
}

#[tokio::test]
async fn generated_async_lift_envelope_retains_inputs_until_actual_future_drop() {
    let engine = WasmEngine::new().unwrap();
    let component = component(engine.engine());
    let mut linker: Linker<tobkiri_wasm::InvocationState<Arc<AsyncState>>> =
        Linker::new(engine.engine());
    linker
        .instance("test:payload/receive@1.0.0")
        .unwrap()
        .func_wrap_async("accept", |store, (texts, bytes): (Vec<String>, Vec<u8>)| {
            let state: Arc<AsyncState> = store.data().data.clone();
            Box::new(async move {
                state.calls.fetch_add(1, Ordering::AcqRel);
                if state.block.load(Ordering::Acquire) {
                    let _held = HeldInputs {
                        texts,
                        bytes,
                        state: state.clone(),
                    };
                    state.entered.notify_one();
                    std::future::pending::<()>().await;
                } else {
                    assert_eq!(texts[0].len() + bytes.len(), 6144);
                }
                Ok(())
            })
        })
        .unwrap();
    // The pinned engine currently retains its default2MiB fiber in this
    // account. Match the actual16MiB Store budget; input assertions compare
    // against a warmed baseline and do not mistake the stack for payload.
    let state = Arc::new(AsyncState {
        account: Meter::new(16 * 1024 * 1024),
        block: AtomicBool::new(false),
        entered: tokio::sync::Notify::new(),
        calls: AtomicUsize::new(0),
        dropped: AtomicUsize::new(0),
        baseline: AtomicU64::new(0),
    });
    let mut store = engine.store(context(), state.clone()).unwrap();
    store.host_allocation_budget(state.account.clone());
    let instance = linker
        .instantiate_async(&mut store, &component)
        .await
        .unwrap();
    let one = instance.get_func(&mut store, "one").unwrap();
    drop(one.call_async_owned(&mut store, &[]).await.unwrap());
    assert_eq!(state.calls.load(Ordering::Acquire), 1);
    let baseline = state.account.used.load(Ordering::Acquire);
    state.baseline.store(baseline, Ordering::Release);
    state.block.store(true, Ordering::Release);
    let mut call = Box::pin(one.call_async_owned(&mut store, &[]));
    tokio::select! {
        _ = state.entered.notified() => {},
        result = &mut call => panic!("pending import unexpectedly ended: {result:?}"),
        _ = tokio::time::sleep(std::time::Duration::from_secs(2)) => panic!("host callback not reached"),
    }
    let pending_charge = state.account.used.load(Ordering::Acquire);
    assert!(pending_charge >= baseline + 6144);
    assert_eq!(state.dropped.load(Ordering::Acquire), 0);
    drop(call);
    assert_eq!(
        state.dropped.load(Ordering::Acquire),
        1,
        "dropping the call must actually destroy its pending native future"
    );
    assert!(state.account.used.load(Ordering::Acquire) <= pending_charge - 6144);
    drop(store);
    assert_eq!(state.account.used.load(Ordering::Acquire), 0);
}
