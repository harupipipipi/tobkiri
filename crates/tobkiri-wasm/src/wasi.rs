//! Restricted Preview 2 capabilities. Generated argument lifting is bounded by
//! the Store's producer owner; retained output has its own CoreBuffer leases.
use anyhow::{Context as _, Result, bail};
use bytes::Bytes;
use std::{
    io,
    pin::Pin,
    sync::{Arc, Mutex, OnceLock},
    task::{Context, Poll},
};
use tobkiri_canon::{AllocationBudget, CoreBuffer};
use tobkiri_gate::{CallCx, ControlKind, ControlReservation, CoreControl};
use tokio::io::AsyncWrite;
use wasmtime::component::{HasData, Linker, ResourceTable};
use wasmtime_wasi::cli::{IsTerminal, StdoutStream, WasiCli, WasiCliView};
use wasmtime_wasi::filesystem::{WasiFilesystem, WasiFilesystemView};
use wasmtime_wasi::p2::{OutputStream, Pollable, StreamError, StreamResult, bindings};
use wasmtime_wasi::{WasiCtx, WasiCtxBuilder, WasiCtxView, WasiView};

use crate::InvocationState;

const MAX_OUTPUT_BYTES: usize = 1024 * 1024;
const MAX_OUTPUT_CHUNKS: usize = 1024;

struct OutputActivation {
    cx: CallCx,
    budget: Arc<dyn AllocationBudget>,
    generation: u64,
}
struct OutputState {
    chunks: Vec<CoreBuffer>,
    bytes: usize,
    cap: usize,
    generation: u64,
    active: Option<OutputActivation>,
    // Native bounded queue metadata, never a guest-payload allocation sponsor.
    _control: ControlReservation,
}
/// Captured output is private to the Store. It never inherits a host terminal.
/// Every retained chunk is copied only after the actual producer reserves it.
#[derive(Clone)]
pub struct OwnedOutputPipe(Arc<Mutex<OutputState>>);
impl OwnedOutputPipe {
    fn new(cap: usize, control: &CoreControl) -> Result<Self> {
        let metadata = std::mem::size_of::<OutputState>()
            .checked_add(MAX_OUTPUT_CHUNKS * std::mem::size_of::<CoreBuffer>())
            .context("WASI output queue metadata overflow")?;
        let reservation = control.reserve(ControlKind::Queue, u64::try_from(metadata)?)?;
        let mut chunks = Vec::new();
        chunks.try_reserve_exact(MAX_OUTPUT_CHUNKS)?;
        Ok(Self(Arc::new(Mutex::new(OutputState {
            chunks,
            bytes: 0,
            cap,
            generation: 0,
            active: None,
            _control: reservation,
        }))))
    }
    fn activate(&self, cx: &CallCx, budget: &Arc<dyn AllocationBudget>) -> Result<OutputGuard> {
        cx.check_active()?;
        let mut state = self
            .0
            .lock()
            .map_err(|_| anyhow::anyhow!("WASI output poisoned"))?;
        if state.active.is_some() {
            bail!("WASI output activation overlaps another call")
        }
        state.generation = state
            .generation
            .checked_add(1)
            .context("WASI output generations exhausted")?;
        let generation = state.generation;
        state.active = Some(OutputActivation {
            cx: cx.clone(),
            budget: budget.clone(),
            generation,
        });
        Ok(OutputGuard {
            pipe: self.clone(),
            generation,
        })
    }
    fn remaining(&self) -> Result<usize> {
        let state = self
            .0
            .lock()
            .map_err(|_| anyhow::anyhow!("WASI output poisoned"))?;
        let active = state
            .active
            .as_ref()
            .context("WASI output has no live producer")?;
        active.cx.check_active()?;
        if state.chunks.len() >= MAX_OUTPUT_CHUNKS {
            return Ok(0);
        }
        Ok(state.cap.saturating_sub(state.bytes))
    }
    fn append(&self, bytes: &[u8]) -> Result<()> {
        let mut state = self
            .0
            .lock()
            .map_err(|_| anyhow::anyhow!("WASI output poisoned"))?;
        let active = state
            .active
            .as_ref()
            .context("WASI output has no live producer")?;
        active.cx.check_active()?;
        if bytes.is_empty() {
            return Ok(());
        }
        let next = state
            .bytes
            .checked_add(bytes.len())
            .context("WASI output size overflow")?;
        if next > state.cap || state.chunks.len() == MAX_OUTPUT_CHUNKS {
            bail!("WASI captured output limit exceeded")
        }
        // Copy and reserve before publishing a chunk or advancing the counter.
        let chunk = CoreBuffer::copy_from(bytes, &active.budget)?;
        state.chunks.push(chunk);
        state.bytes = next;
        Ok(())
    }
}
struct OutputGuard {
    pipe: OwnedOutputPipe,
    generation: u64,
}
impl Drop for OutputGuard {
    fn drop(&mut self) {
        if let Ok(mut state) = self.pipe.0.lock() {
            if state
                .active
                .as_ref()
                .is_some_and(|active| active.generation == self.generation)
            {
                state.active.take();
            }
        }
        // Stored chunks retain their original owner independently of activation.
    }
}
pub(crate) struct WasiOutputGuard {
    _output: OutputGuard,
}
impl IsTerminal for OwnedOutputPipe {
    fn is_terminal(&self) -> bool {
        false
    }
}
impl StdoutStream for OwnedOutputPipe {
    fn async_stream(&self) -> Box<dyn AsyncWrite + Send + Sync> {
        Box::new(self.clone())
    }
    fn p2_stream(&self) -> Box<dyn OutputStream> {
        Box::new(self.clone())
    }
}
#[async_trait::async_trait]
impl Pollable for OwnedOutputPipe {
    async fn ready(&mut self) {}
}
#[async_trait::async_trait]
impl OutputStream for OwnedOutputPipe {
    fn write(&mut self, bytes: Bytes) -> StreamResult<()> {
        self.append(&bytes)
            .map_err(|error| StreamError::Trap(wasmtime::Error::from_anyhow(error)))
    }
    fn flush(&mut self) -> StreamResult<()> {
        self.remaining()
            .map(|_| ())
            .map_err(|error| StreamError::Trap(wasmtime::Error::from_anyhow(error)))
    }
    fn check_write(&mut self) -> StreamResult<usize> {
        match self
            .remaining()
            .map_err(|error| StreamError::Trap(wasmtime::Error::from_anyhow(error)))?
        {
            0 => Err(StreamError::Closed),
            remaining => Ok(remaining),
        }
    }
}
impl AsyncWrite for OwnedOutputPipe {
    fn poll_write(
        self: Pin<&mut Self>,
        _: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        Poll::Ready(
            self.append(bytes)
                .map(|()| bytes.len())
                .map_err(io::Error::other),
        )
    }
    fn poll_flush(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(self.remaining().map(|_| ()).map_err(io::Error::other))
    }
    fn poll_shutdown(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

/// Empty environment/stdin/preopens and bounded owner-leased captured output.
/// No ambient filesystem, network, inherited terminal or host args are exposed.
pub struct RestrictedWasi {
    pub ctx: WasiCtx,
    pub table: ResourceTable,
    pub stdout: OwnedOutputPipe,
    pub stderr: OwnedOutputPipe,
}
impl RestrictedWasi {
    pub fn new(output_bytes: u64) -> Result<Self> {
        static CONTROL: OnceLock<CoreControl> = OnceLock::new();
        Self::new_with_control(output_bytes, CONTROL.get_or_init(CoreControl::default))
    }
    pub(crate) fn new_with_control(output_bytes: u64, control: &CoreControl) -> Result<Self> {
        let cap = usize::try_from(output_bytes.min(MAX_OUTPUT_BYTES as u64))?;
        if cap == 0 {
            bail!("WASI output budget is zero")
        }
        let stdout = OwnedOutputPipe::new(cap, control)?;
        // Both channels share one aggregate byte/chunk bound for this Store.
        let stderr = stdout.clone();
        let mut builder = WasiCtxBuilder::new();
        builder
            .stdout(stdout.clone())
            .stderr(stderr.clone())
            .allow_tcp(false)
            .allow_udp(false)
            .allow_ip_name_lookup(false);
        // No preopens: no guest filesystem/scratch authority is granted here.
        let mut table = ResourceTable::new();
        table.set_max_capacity(1024);
        Ok(Self {
            ctx: builder.build(),
            table,
            stdout,
            stderr,
        })
    }
    pub(crate) fn activate_output(
        &self,
        cx: &CallCx,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<WasiOutputGuard> {
        let output = self.stdout.activate(cx, budget)?;
        Ok(WasiOutputGuard { _output: output })
    }
}
impl WasiView for RestrictedWasi {
    fn ctx(&mut self) -> WasiCtxView<'_> {
        WasiCtxView {
            ctx: &mut self.ctx,
            table: &mut self.table,
        }
    }
}
impl<T: WasiView> WasiView for InvocationState<T> {
    fn ctx(&mut self) -> WasiCtxView<'_> {
        self.data.ctx()
    }
}
struct HasIo;
impl HasData for HasIo {
    type Data<'a> = &'a mut ResourceTable;
}

/// Link only the capability-free interfaces. Clocks/random are installed by
/// `link_gated_wasi` using dynamic callbacks with actual Gate authorization.
/// Socket/HTTP interfaces are never linked.
pub fn link_restricted_wasi<T: WasiView>(linker: &mut Linker<T>) -> Result<()> {
    bindings::io::error::add_to_linker::<T, HasIo>(linker, |s| s.ctx().table)?;
    bindings::io::poll::add_to_linker::<T, HasIo>(linker, |s| s.ctx().table)?;
    bindings::io::streams::add_to_linker::<T, HasIo>(linker, |s| s.ctx().table)?;
    bindings::filesystem::types::add_to_linker::<T, WasiFilesystem>(linker, T::filesystem)?;
    bindings::filesystem::preopens::add_to_linker::<T, WasiFilesystem>(linker, T::filesystem)?;
    bindings::cli::environment::add_to_linker::<T, WasiCli>(linker, T::cli)?;
    bindings::cli::exit::add_to_linker::<T, WasiCli>(linker, T::cli)?;
    bindings::cli::stdin::add_to_linker::<T, WasiCli>(linker, T::cli)?;
    bindings::cli::stdout::add_to_linker::<T, WasiCli>(linker, T::cli)?;
    bindings::cli::stderr::add_to_linker::<T, WasiCli>(linker, T::cli)?;
    bindings::cli::terminal_input::add_to_linker::<T, WasiCli>(linker, T::cli)?;
    bindings::cli::terminal_output::add_to_linker::<T, WasiCli>(linker, T::cli)?;
    bindings::cli::terminal_stdin::add_to_linker::<T, WasiCli>(linker, T::cli)?;
    bindings::cli::terminal_stdout::add_to_linker::<T, WasiCli>(linker, T::cli)?;
    bindings::cli::terminal_stderr::add_to_linker::<T, WasiCli>(linker, T::cli)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        sync::atomic::{AtomicU64, Ordering},
        time::{Duration, Instant},
    };
    use tobkiri_canon::ids::{ProfileId, RequestId};
    use tobkiri_canon::{AllocationError, AllocationKind, AllocationRequest};
    use tobkiri_gate::{CoreCancellation, Principal, ResourceLimits};
    use wasmtime::component::{HostAllocationBudget, HostAllocationError, HostAllocationRequest};

    struct Meter {
        used: AtomicU64,
        cap: u64,
    }
    impl AllocationBudget for Meter {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            self.used
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                    used.checked_add(request.bytes)
                        .filter(|next| *next <= self.cap)
                })
                .map(|_| ())
                .map_err(|_| AllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            self.used
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                    used.checked_sub(bytes)
                })
                .expect("release only charged output");
        }
    }
    impl HostAllocationBudget for Meter {
        fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError> {
            AllocationBudget::reserve(
                self,
                AllocationRequest {
                    kind: AllocationKind::Temporary,
                    bytes: request.bytes,
                    elements: request.elements,
                    depth: request.depth,
                    work: request.work,
                },
            )
            .map_err(|_| HostAllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            AllocationBudget::release(self, bytes)
        }
    }
    fn meter(cap: u64) -> Arc<Meter> {
        Arc::new(Meter {
            used: AtomicU64::new(0),
            cap,
        })
    }
    fn context() -> CallCx {
        CallCx::root(
            ProfileId::parse("wasi-test").unwrap(),
            Principal::User,
            RequestId::parse("output-test").unwrap(),
            CoreCancellation::new(),
            Instant::now() + Duration::from_secs(10),
            ResourceLimits {
                fuel_per_call: 100_000,
                memory_mib: 16,
                wall_ms: 10_000,
                table_elements: 1024,
                output_bytes: 1024 * 1024,
            },
        )
    }

    #[test]
    fn retained_output_keeps_each_actual_owner_after_call_and_pipe_clone() {
        let control = CoreControl::default();
        let pipe = OwnedOutputPipe::new(32, &control).unwrap();
        assert!(pipe.append(b"no producer").is_err());
        let first = meter(4096);
        let budget: Arc<dyn AllocationBudget> = first.clone();
        let active = pipe.activate(&context(), &budget).unwrap();
        pipe.append(b"first").unwrap();
        let first_charge = first.used.load(Ordering::Acquire);
        assert!(first_charge >= 5);
        drop(active);
        assert_eq!(first.used.load(Ordering::Acquire), first_charge);
        assert!(pipe.append(b"retired").is_err());
        let second = meter(4096);
        let budget: Arc<dyn AllocationBudget> = second.clone();
        let active = pipe.activate(&context(), &budget).unwrap();
        pipe.append(b"second").unwrap();
        drop(active);
        let retained = pipe.clone();
        drop(pipe);
        assert_eq!(first.used.load(Ordering::Acquire), first_charge);
        assert!(second.used.load(Ordering::Acquire) >= 6);
        assert_eq!(control.usage().count(ControlKind::Queue), 1);
        drop(retained);
        assert_eq!(first.used.load(Ordering::Acquire), 0);
        assert_eq!(second.used.load(Ordering::Acquire), 0);
        assert_eq!(control.usage().count(ControlKind::Queue), 0);
    }

    #[test]
    fn failed_payload_or_capacity_admission_publishes_no_partial_output() {
        let control = CoreControl::default();
        let pipe = OwnedOutputPipe::new(4, &control).unwrap();
        let denied = meter(1);
        let budget: Arc<dyn AllocationBudget> = denied.clone();
        let active = pipe.activate(&context(), &budget).unwrap();
        assert!(pipe.append(b"x").is_err());
        assert_eq!(denied.used.load(Ordering::Acquire), 0);
        assert_eq!(pipe.0.lock().unwrap().bytes, 0);
        drop(active);
        let allowed = meter(4096);
        let budget: Arc<dyn AllocationBudget> = allowed.clone();
        let active = pipe.activate(&context(), &budget).unwrap();
        pipe.append(b"four").unwrap();
        let charge = allowed.used.load(Ordering::Acquire);
        assert!(pipe.append(b"x").is_err());
        assert_eq!(allowed.used.load(Ordering::Acquire), charge);
        assert_eq!(pipe.0.lock().unwrap().bytes, 4);
        assert_eq!(pipe.0.lock().unwrap().chunks.len(), 1);
        drop((active, pipe));
        assert_eq!(allowed.used.load(Ordering::Acquire), 0);
    }

    #[test]
    fn output_queue_bound_and_original_cancellation_are_enforced() {
        let control = CoreControl::default();
        let pipe = OwnedOutputPipe::new(MAX_OUTPUT_BYTES, &control).unwrap();
        let account = meter(1024 * 1024);
        let budget: Arc<dyn AllocationBudget> = account.clone();
        let cx = context();
        let active = pipe.activate(&cx, &budget).unwrap();
        for _ in 0..MAX_OUTPUT_CHUNKS {
            pipe.append(b"x").unwrap();
        }
        let charge = account.used.load(Ordering::Acquire);
        assert!(pipe.append(b"x").is_err());
        assert_eq!(account.used.load(Ordering::Acquire), charge);
        cx.cancel().cancel();
        assert!(pipe.remaining().is_err());
        drop((active, pipe));
        assert_eq!(account.used.load(Ordering::Acquire), 0);
    }

    #[tokio::test]
    async fn actual_component_environment_imports_receive_no_host_arguments_or_env() {
        let mut config = wasmtime::Config::new();
        config.wasm_component_model(true);
        let engine = wasmtime::Engine::new(&config).unwrap();
        let component = wasmtime::component::Component::new(
            &engine,
            wat::parse_str(
                r#"
            (component
                (type $strings (list string))
                (type $environment (list (tuple string string)))
                (import "wasi:cli/environment@0.2.6" (instance $env
                    (export "get-arguments" (func (result $strings)))
                    (export "get-environment" (func (result $environment)))))
                (alias export $env "get-arguments" (func $args))
                (alias export $env "get-environment" (func $vars))
                (core module $allocator
                    (memory (export "memory") 1)
                    (global $heap (mut i32) (i32.const 4096))
                    (func (export "realloc") (param i32 i32) (param $align i32) (param $len i32) (result i32)
                        (local $ptr i32) (local $end i32)
                        global.get $heap local.get $align i32.const 1 i32.sub i32.add
                        i32.const 0 local.get $align i32.sub i32.and local.tee $ptr
                        local.get $len i32.add local.tee $end i32.const 65536 i32.gt_u
                        if unreachable end
                        local.get $end global.set $heap local.get $ptr))
                (core instance $storage (instantiate $allocator))
                (alias core export $storage "memory" (core memory $memory))
                (alias core export $storage "realloc" (core func $realloc))
                (core func $args-lowered (canon lower (func $args) (memory $memory) (realloc $realloc)))
                (core func $vars-lowered (canon lower (func $vars) (memory $memory) (realloc $realloc)))
                (core module $wrapper
                    (import "host" "args" (func $args (param i32)))
                    (import "host" "env" (func $vars (param i32)))
                    (func (export "args") (result i32) i32.const 0 call $args i32.const 0)
                    (func (export "env") (result i32) i32.const 0 call $vars i32.const 0))
                (core instance $wrapped (instantiate $wrapper (with "host" (instance
                    (export "args" (func $args-lowered))
                    (export "env" (func $vars-lowered))))))
                (alias core export $wrapped "args" (core func $args-wrapped))
                (alias core export $wrapped "env" (core func $vars-wrapped))
                (func $args-export (result $strings) (canon lift (core func $args-wrapped) (memory $memory) (realloc $realloc)))
                (func $vars-export (result $environment) (canon lift (core func $vars-wrapped) (memory $memory) (realloc $realloc)))
                (export "args" (func $args-export))
                (export "env" (func $vars-export)))
        "#,
            )
            .unwrap(),
        )
        .unwrap();
        let mut linker = Linker::new(&engine);
        link_restricted_wasi(&mut linker).unwrap();
        let control = CoreControl::default();
        let wasi = RestrictedWasi::new_with_control(1024, &control).unwrap();
        // Match the declared16MiB Store account: this pinned runtime
        // conservatively also retains its default2MiB async stack here.
        let account = meter(16 * 1024 * 1024);
        let mut store = wasmtime::Store::new(&engine, wasi);
        store.host_allocation_budget(account.clone());
        let instance = linker
            .instantiate_async(&mut store, &component)
            .await
            .unwrap();
        for name in ["args", "env"] {
            let function = instance.get_func(&mut store, name).unwrap();
            let results = function.call_async_owned(&mut store, &[]).await.unwrap();
            assert!(
                matches!(results.as_slice(), [wasmtime::component::Val::List(values)] if values.is_empty())
            );
        }
        drop(store);
        assert_eq!(account.used.load(Ordering::Acquire), 0);
    }

    struct DeniedEffects {
        calls: AtomicU64,
        denials: AtomicU64,
    }
    #[async_trait::async_trait]
    impl tobkiri_gate::Gate for DeniedEffects {
        fn check_context(&self, cx: &CallCx) -> Result<(), tobkiri_gate::GateError> {
            cx.check_active()
        }
        async fn authorize(
            &self,
            cx: &CallCx,
            effect: tobkiri_gate::EffectRequest,
            _: tobkiri_canon::Digest,
        ) -> Result<tobkiri_gate::Grant, tobkiri_gate::GateError> {
            assert!(matches!(
                effect,
                tobkiri_gate::EffectRequest::Clock | tobkiri_gate::EffectRequest::Random
            ));
            self.calls.fetch_add(1, Ordering::AcqRel);
            self.record_denial(cx, "wasi-effect", "denied").await?;
            Err(tobkiri_gate::GateError::Denied("test effect denied".into()))
        }
        async fn record_denial(
            &self,
            cx: &CallCx,
            kind: &'static str,
            code: &'static str,
        ) -> Result<(), tobkiri_gate::GateError> {
            cx.check_active()?;
            assert_eq!((kind, code), ("wasi-effect", "denied"));
            self.denials.fetch_add(1, Ordering::AcqRel);
            Ok(())
        }
        async fn record_outcome(
            &self,
            _: tobkiri_gate::GrantId,
            _: tobkiri_gate::Outcome,
        ) -> Result<(), tobkiri_gate::GateError> {
            panic!("no grant was consumed")
        }
    }
    struct TestEffects {
        wasi: RestrictedWasi,
        gate: Arc<dyn tobkiri_gate::Gate>,
        owners: crate::AllocationOwners,
    }
    impl WasiView for TestEffects {
        fn ctx(&mut self) -> WasiCtxView<'_> {
            self.wasi.ctx()
        }
    }
    impl crate::wasi_effects::WasiEffects for TestEffects {
        fn effect_gate(&self) -> Result<Arc<dyn tobkiri_gate::Gate>> {
            Ok(self.gate.clone())
        }
        fn effect_owners(&self) -> &crate::AllocationOwners {
            &self.owners
        }
    }

    #[tokio::test]
    async fn actual_component_clock_and_random_imports_cannot_bypass_gate_denial() {
        let engine = crate::WasmEngine::new().unwrap();
        let component = wasmtime::component::Component::new(
            engine.engine(),
            wat::parse_str(
                r#"
            (component
                (type $bytes (list u8))
                (import "wasi:clocks/monotonic-clock@0.2.6" (instance $clock
                    (export "now" (func (result u64)))))
                (import "wasi:random/random@0.2.6" (instance $random
                    (export "get-random-bytes" (func (param "len" u64) (result $bytes)))))
                (alias export $clock "now" (func $now))
                (alias export $random "get-random-bytes" (func $bytes))
                (core module $allocator
                    (memory (export "memory") 1)
                    (global $heap (mut i32) (i32.const 4096))
                    (func (export "realloc") (param i32 i32) (param $align i32) (param $len i32) (result i32)
                        (local $ptr i32) (local $end i32)
                        global.get $heap local.get $align i32.const 1 i32.sub i32.add
                        i32.const 0 local.get $align i32.sub i32.and local.tee $ptr
                        local.get $len i32.add local.tee $end i32.const 65536 i32.gt_u
                        if unreachable end
                        local.get $end global.set $heap local.get $ptr))
                (core instance $storage (instantiate $allocator))
                (alias core export $storage "memory" (core memory $memory))
                (alias core export $storage "realloc" (core func $realloc))
                (core func $now-lowered (canon lower (func $now)))
                (core func $bytes-lowered (canon lower (func $bytes) (memory $memory) (realloc $realloc)))
                (core module $wrapper
                    (import "host" "clock" (func $now (result i64)))
                    (import "host" "random" (func $bytes (param i64 i32)))
                    (func (export "clock") (result i64) call $now)
                    (func (export "random") (param $len i64) (result i32)
                        local.get $len i32.const 0 call $bytes i32.const 0))
                (core instance $wrapped (instantiate $wrapper (with "host" (instance
                    (export "clock" (func $now-lowered))
                    (export "random" (func $bytes-lowered))))))
                (alias core export $wrapped "clock" (core func $clock-wrapped))
                (alias core export $wrapped "random" (core func $random-wrapped))
                (func $clock-export (result u64) (canon lift (core func $clock-wrapped)))
                (func $random-export (param "len" u64) (result $bytes)
                    (canon lift (core func $random-wrapped) (memory $memory) (realloc $realloc)))
                (export "clock" (func $clock-export))
                (export "random" (func $random-export)))
        "#,
            )
            .unwrap(),
        )
        .unwrap();
        let mut linker = Linker::new(engine.engine());
        link_restricted_wasi(&mut linker).unwrap();
        crate::wasi_effects::link_gated_wasi(&mut linker, &component, engine.engine()).unwrap();
        let gate = Arc::new(DeniedEffects {
            calls: AtomicU64::new(0),
            denials: AtomicU64::new(0),
        });
        // Match the declared16MiB Store account: this pinned runtime
        // conservatively also retains its default2MiB async stack here.
        let account = meter(16 * 1024 * 1024);
        for (expected, name) in [(1, "clock"), (2, "random")] {
            // A trapped component instance is poisoned. Production discards it;
            // use a fresh Store here so the second assertion observes Random
            // authorization rather than the earlier Clock instance's poison.
            let owners = crate::AllocationOwners::from_budgets(account.clone(), account.clone());
            let data = TestEffects {
                wasi: RestrictedWasi::new(1024).unwrap(),
                gate: gate.clone(),
                owners,
            };
            let mut store = engine.store(context(), data).unwrap();
            store.host_allocation_budget(account.clone());
            let instance = linker
                .instantiate_async(&mut store, &component)
                .await
                .unwrap();
            let function = instance.get_func(&mut store, name).unwrap();
            let params = if name == "clock" {
                vec![]
            } else {
                vec![wasmtime::component::Val::U64(4)]
            };
            assert!(
                function
                    .call_async_owned(&mut store, &params)
                    .await
                    .is_err()
            );
            assert_eq!(gate.calls.load(Ordering::Acquire), expected);
            assert_eq!(gate.denials.load(Ordering::Acquire), expected);
            drop(store);
            assert_eq!(account.used.load(Ordering::Acquire), 0);
        }
    }
}
