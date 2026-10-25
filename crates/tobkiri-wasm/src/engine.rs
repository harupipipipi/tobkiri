use crate::aggregate_limits::{AggregateLimits, MAX_CORE_INSTANCES, MAX_MEMORIES, MAX_TABLES};
use crate::execution_epoch::check_epoch;
use anyhow::{Context, Result, bail};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tobkiri_gate::{CallCx, ControlKind, CoreControl};
use tokio::task::JoinHandle;
use wasmtime::{Config, Engine, Store};

const EPOCH_PERIOD: Duration = Duration::from_millis(10);
// Admit the full bounded control-record envelope before creating a Store.
// Module-sized compiled metadata is bounded by catalog/load policy separately;
// this conservative charge is not an exact JIT allocation or RSS measurement.
const STORE_STRUCTURE_CONTROL_BYTES: u64 =
    4096 + 1024 * (MAX_CORE_INSTANCES + MAX_MEMORIES + MAX_TABLES) as u64;

struct EpochDriver {
    task: JoinHandle<()>,
}
impl Drop for EpochDriver {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// Shared compilation engine with an independently bounded epoch ticker.
#[derive(Clone)]
pub struct WasmEngine {
    engine: Engine,
    _epochs: Arc<EpochDriver>,
}

/// Store-owned identity. A guest never supplies this call context.
pub struct InvocationState<T> {
    pub data: T,
    pub cx: CallCx,
    limits: AggregateLimits,
    cancel_observed: Option<Instant>,
    pub(crate) retirement: Option<tokio_util::sync::CancellationToken>,
}
impl<T> InvocationState<T> {
    pub(crate) fn apply_limits(&mut self, resource: &tobkiri_gate::ResourceLimits) -> Result<()> {
        let memory = resource
            .memory_mib
            .checked_mul(1024 * 1024)
            .and_then(|n| usize::try_from(n).ok())
            .context("memory budget overflow")?;
        let tables = usize::try_from(resource.table_elements).context("table budget overflow")?;
        if memory == 0
            || tables == 0
            || resource.fuel_per_call == 0
            || self.limits.memory_used > memory
            || self.limits.table_used > tables
        {
            bail!("current instance exceeds this entry's resource budget")
        }
        self.limits.memory_cap = memory;
        self.limits.table_cap = tables;
        Ok(())
    }
}

impl WasmEngine {
    pub fn new() -> Result<Self> {
        static CONTROL: std::sync::OnceLock<CoreControl> = std::sync::OnceLock::new();
        Self::new_with_control(CONTROL.get_or_init(CoreControl::default))
    }

    /// Native ticker control is bounded independently of guest payload funds.
    /// The slot lives inside the task until its actual completion after abort.
    pub fn new_with_control(control: &CoreControl) -> Result<Self> {
        let runtime = tokio::runtime::Handle::try_current()
            .context("component execution requires the host async runtime")?;
        let task_control = control.reserve(ControlKind::Task, 4096)?;
        let mut config = Config::new();
        #[allow(deprecated)]
        config.async_support(true);
        config.wasm_component_model(true);
        config.consume_fuel(true);
        config.epoch_interruption(true);
        config.max_wasm_stack(512 * 1024);
        // Close the owned instance path to native mmap memory, funcref tables,
        // and the on-demand allocator. GC/continuation heap accounting and
        // pooling are separate unsupported guest allocation paths.
        config.wasm_gc(false);
        config.wasm_stack_switching(false);
        // This pinned build excludes the gc-function-references and threads
        // Cargo features; those proposal toggles are not exposed/enabled.
        config.memory_init_cow(false);
        config.memory_reservation(0);
        config.memory_reservation_for_growth(0);
        config.memory_guard_size(0);
        config.signals_based_traps(true);
        let engine = Engine::new(&config)?;
        let tick_engine = engine.clone();
        let task = runtime.spawn(async move {
            let _task_control = task_control;
            let mut tick = tokio::time::interval(EPOCH_PERIOD);
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                tick.tick().await;
                tick_engine.increment_epoch();
            }
        });
        Ok(Self {
            engine,
            _epochs: Arc::new(EpochDriver { task }),
        })
    }

    pub fn engine(&self) -> &Engine {
        &self.engine
    }

    /// Create an isolated store with effective limits and the original deadline.
    pub fn store<T: Send + 'static>(
        &self,
        cx: CallCx,
        data: T,
    ) -> Result<Store<InvocationState<T>>> {
        cx.check_active()?;
        let resource = cx.limits();
        let memory_bytes = resource
            .memory_mib
            .checked_mul(1024 * 1024)
            .and_then(|n| usize::try_from(n).ok())
            .context("memory limit overflows the host address space")?;
        if memory_bytes == 0 || resource.fuel_per_call == 0 || resource.table_elements == 0 {
            bail!("component resource budget is zero");
        }
        let structure_control = cx
            .control()
            .reserve(ControlKind::Frame, STORE_STRUCTURE_CONTROL_BYTES)?;
        let limiter = AggregateLimits {
            memory_cap: memory_bytes,
            table_cap: resource.table_elements as usize,
            memory_used: 0,
            table_used: 0,
            last_memory_growth: 0,
            last_table_growth: 0,
            physical: None,
            last_physical_memory: 0,
            last_physical_table: 0,
        };
        let fuel = resource.fuel_per_call;
        let mut store = Store::new(
            &self.engine,
            InvocationState {
                data,
                cx,
                limits: limiter,
                cancel_observed: None,
                retirement: None,
            },
        );
        // Pinned Wasmtime drops user data before its native instances. Its
        // StoreInner drops StoreOpaque before this limiter closure, so retain
        // custody here through actual native destruction, also for into_data.
        store.limiter(move |state| {
            let _ = &structure_control;
            &mut state.limits
        });
        store.set_fuel(fuel)?;
        store.fuel_async_yield_interval(Some(fuel.clamp(1, 10_000)))?;
        store.set_epoch_deadline(1);
        store.epoch_deadline_callback(|mut store| {
            let state = store.data_mut();
            check_epoch(
                state.cx.deadline(),
                state.cx.cancel(),
                state.retirement.as_ref(),
                &mut state.cancel_observed,
            )
        });
        Ok(store)
    }

    /// Production component store: every guest linear/table allocation is
    /// admitted through the producing Pack's shared physical reservation.
    /// The caller retains another strong reference until AFTER Store drop.
    pub(crate) fn store_owned<T: Send + 'static>(
        &self,
        cx: CallCx,
        data: T,
        physical: Arc<crate::instance_capacity::InstanceCapacity>,
    ) -> Result<Store<InvocationState<T>>> {
        let mut store = self.store(cx, data)?;
        store.data_mut().limits.physical = Some(physical);
        Ok(store)
    }
}

#[cfg(test)]
mod control_tests {
    use super::*;
    use tobkiri_gate::CoreControlLimits;

    #[tokio::test]
    async fn epoch_task_admits_before_spawn_and_releases_after_actual_exit() {
        let control = CoreControl::new(CoreControlLimits {
            max_tasks: 1,
            ..Default::default()
        })
        .unwrap();
        let engine = WasmEngine::new_with_control(&control).unwrap();
        assert_eq!(control.usage().count(ControlKind::Task), 1);
        assert!(WasmEngine::new_with_control(&control).is_err());
        let retained = engine.clone();
        drop(engine);
        assert_eq!(control.usage().count(ControlKind::Task), 1);
        drop(retained);
        tokio::time::timeout(Duration::from_secs(1), async {
            while control.usage().count(ControlKind::Task) != 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        let next = WasmEngine::new_with_control(&control).unwrap();
        drop(next);
    }

    #[tokio::test]
    async fn exhausted_native_task_control_denies_before_compilation_or_spawn() {
        let control = CoreControl::new(CoreControlLimits {
            max_tasks: 0,
            ..Default::default()
        })
        .unwrap();
        assert!(WasmEngine::new_with_control(&control).is_err());
        assert_eq!(control.usage().count(ControlKind::Task), 0);
    }
}
