use anyhow::{Context, Result, bail};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tobkiri_gate::{CallCx, ControlKind, CoreControl};
use tokio::task::JoinHandle;
use wasmtime::{Config, Engine, ResourceLimiter, Store, UpdateDeadline};

const EPOCH_PERIOD: Duration = Duration::from_millis(10);
const CANCEL_GRACE: Duration = Duration::from_secs(2);
// Component composition routinely creates more core instances than exported
// Pack instances. These are finite native structure bounds, independent of the
// approved aggregate guest memory/table-element budgets below. Keep the
// memory/table object bounds unchanged until a supported component needs more.
const MAX_CORE_INSTANCES: usize = 1024;
const MAX_MEMORIES: usize = 16;
const MAX_TABLES: usize = 16;
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

// Limits apply to the sum of all guest memories/tables in a store.
struct AggregateLimits {
    memory_cap: usize,
    table_cap: usize,
    memory_used: usize,
    table_used: usize,
    last_memory_growth: usize,
    last_table_growth: usize,
    physical: Option<Arc<crate::instance_capacity::InstanceCapacity>>,
    last_physical_memory: u64,
    last_physical_table: u64,
}
impl ResourceLimiter for AggregateLimits {
    fn memory_growing(
        &mut self,
        current: usize,
        desired: usize,
        maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        let growth = desired.saturating_sub(current);
        if maximum.is_some_and(|max| desired > max)
            || self
                .memory_used
                .checked_add(growth)
                .is_none_or(|n| n > self.memory_cap)
        {
            return Err(wasmtime::Error::msg(
                "aggregate guest memory limit exceeded",
            ));
        }
        if let Some(physical) = &self.physical {
            self.last_physical_memory = physical
                .memory_growth(current, desired)
                .map_err(|error| wasmtime::Error::msg(error.to_string()))?;
        }
        self.memory_used += growth;
        self.last_memory_growth = growth;
        Ok(true)
    }
    fn memory_grow_failed(&mut self, error: wasmtime::Error) -> wasmtime::Result<()> {
        if let Some(physical) = &self.physical {
            physical
                .failed_growth(self.last_physical_memory)
                .map_err(|error| wasmtime::Error::msg(error.to_string()))?;
            self.last_physical_memory = 0;
        }
        self.memory_used = self.memory_used.saturating_sub(self.last_memory_growth);
        self.last_memory_growth = 0;
        Err(error)
    }
    fn table_growing(
        &mut self,
        current: usize,
        desired: usize,
        maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        let growth = desired.saturating_sub(current);
        if maximum.is_some_and(|max| desired > max)
            || self
                .table_used
                .checked_add(growth)
                .is_none_or(|n| n > self.table_cap)
        {
            return Err(wasmtime::Error::msg("aggregate guest table limit exceeded"));
        }
        if let Some(physical) = &self.physical {
            self.last_physical_table = physical
                .table_growth(current, desired)
                .map_err(|error| wasmtime::Error::msg(error.to_string()))?;
        }
        self.table_used += growth;
        self.last_table_growth = growth;
        Ok(true)
    }
    fn table_grow_failed(&mut self, error: wasmtime::Error) -> wasmtime::Result<()> {
        if let Some(physical) = &self.physical {
            physical
                .failed_growth(self.last_physical_table)
                .map_err(|error| wasmtime::Error::msg(error.to_string()))?;
            self.last_physical_table = 0;
        }
        self.table_used = self.table_used.saturating_sub(self.last_table_growth);
        self.last_table_growth = 0;
        Err(error)
    }
    fn instances(&self) -> usize {
        MAX_CORE_INSTANCES
    }
    fn memories(&self) -> usize {
        MAX_MEMORIES
    }
    fn tables(&self) -> usize {
        MAX_TABLES
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
            if state
                .retirement
                .as_ref()
                .is_some_and(|token| token.is_cancelled())
            {
                return Err(wasmtime::Error::msg("component instance retired"));
            }
            if Instant::now() >= state.cx.deadline() {
                return Err(wasmtime::Error::msg("original component deadline exceeded"));
            }
            if state.cx.cancel().is_cancelled() {
                let observed = state.cancel_observed.get_or_insert_with(Instant::now);
                if observed.elapsed() >= CANCEL_GRACE {
                    return Err(wasmtime::Error::msg(
                        "component cancelled after grace period",
                    ));
                }
            }
            Ok(UpdateDeadline::Yield(1))
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
