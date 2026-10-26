//! Trusted nativeKit-only runner for selected DATA components.
//!
//! Component and export selection is a nativeKit responsibility. This API is
//! not an IPC or guest selector, and its restricted imports do not authenticate
//! the caller's application role. In particular, generic Core/Pack components
//! remain on the ordinary Pack lifecycle and invocation path.
use super::{NativeSetupStore, NativeState};
use crate::execution_epoch::check_epoch;
use anyhow::{Context, Result, bail};
use std::time::{Instant, SystemTime, UNIX_EPOCH};
use tobkiri_canon::{AllocationKind, AllocationRequest, CoreBuffer, MAX_BYTES};
use wasmtime::component::types::{ComponentFunc, ComponentItem, Type};
use wasmtime::component::{
    BudgetedString, BudgetedVecBuilder, Component, Func, Instance, Linker, Val,
};
use wasmtime::{Engine, StoreContextMut};

const WALL_CLOCK_IMPORT: &str = "wasi:clocks/wall-clock@0.2.4";
const MONOTONIC_CLOCK_IMPORT: &str = "tobkiri-ui:clock/monotonic@1.0.0";
const CLOCK_CALL_LIMIT: u64 = 100_000;
#[path = "native_data_program.rs"]
mod program;
pub use program::{NativeDataBinding, NativeDataProgram};
#[path = "native_data_result.rs"]
mod result;
pub use result::{
    NativeDataError, NativeDataErrorContract, NativeDataResultContract, NativeDataResultEntry,
    NativeDataSuccessType, NativeDataValue,
};
#[cfg(not(unix))]
const RESOLUTION_SAMPLE_LIMIT: u64 = 100_000;

#[derive(Clone, Debug, PartialEq, Eq)]
enum InputShape {
    None,
    String,
}

#[derive(Clone, Copy)]
enum OutputShape {
    String,
    Result(NativeDataSuccessType),
}

enum NativeDataReply {
    Value(NativeDataValue),
    DomainError(NativeDataError),
}

/// Generic component export selector for the trusted nativeKit DATA runner.
///
/// The function is either a root component export or an export within the
/// optional component interface instance. It carries no Pack identity, shape,
/// capability, or serialized-artifact privilege.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NativeDataExport<'a> {
    /// Optional root export naming the component instance that contains the
    /// selected function.
    pub interface: Option<&'a str>,
    /// Selected component function name.
    pub function: &'a str,
}

impl<'a> NativeDataExport<'a> {
    /// Create a generic root or interface-scoped function selector.
    pub fn new(interface: Option<&'a str>, function: &'a str) -> Self {
        Self {
            interface,
            function,
        }
    }
}

/// Bounded execution metadata. These counters confer no authority.
#[derive(Clone, Copy, Debug, Default, serde::Serialize)]
pub struct NativeDataObservation {
    /// Fuel sampled from the original Store, including unsuccessful calls.
    pub fuel_remaining: Option<u64>,
    /// The most recent owned call completed canonical post-return.
    pub post_return: bool,
    /// The guest body, rather than lifting or post-return, trapped unreachable.
    pub unreachable_trap: bool,
    /// Owned canonical post-return failed; this is never a body IO denial.
    pub post_return_failed: bool,
    /// Actual Store destruction has completed.
    pub store_retired: bool,
    /// The original absolute deadline was already reached when sampled.
    pub deadline_exceeded: bool,
    /// Number of clock callbacks admitted across both clock interfaces.
    pub clock_calls: u64,
    /// Number of admitted callbacks that completed successfully.
    pub clock_completed: u64,
    /// Wall-clock now callbacks admitted.
    pub wall_now: u64,
    /// Wall-clock resolution callbacks admitted.
    pub wall_resolution: u64,
    /// Monotonic now callbacks admitted.
    pub monotonic_now: u64,
    /// Monotonic resolution callbacks admitted.
    pub monotonic_resolution: u64,
    /// Actual wall-clock resolution returned by the host, if requested.
    pub wall_resolution_ns: Option<u64>,
    /// Actual monotonic resolution returned by the host, if requested.
    pub monotonic_resolution_ns: Option<u64>,
}

/// Retained native DATA component instance using one original Setup Store.
///
/// Preparation instantiates once. Repeated invocations reuse this exact Store
/// and component instance; fuel, deadline, cancellation, and host-work budgets
/// remain those of the original native Setup invocation.
pub struct NativeDataEntry<T: 'static> {
    store: Option<NativeSetupStore<T>>,
    _instance: Option<Instance>,
    function: Option<Func>,
    input: InputShape,
    output: OutputShape,
    deadline: Instant,
    observation: NativeDataObservation,
}

impl<T: Send + 'static> NativeSetupStore<T> {
    /// Validate and instantiate a component selected by trusted nativeKit.
    ///
    /// Only the two explicit clock interfaces are supported as imports. File
    /// capture and every other host capability are refused until bound by a
    /// dedicated native interface.
    pub async fn prepare_data_entry(
        self,
        component: &Component,
        export: NativeDataExport<'_>,
    ) -> Result<NativeDataEntry<T>> {
        self.prepare_entry(component, export, None).await
    }

    /// Bind a nativeKit's exact result/error contract, without importing Core
    /// authority or treating a normal domain error as a Wasm execution failure.
    pub async fn prepare_result_data_entry(
        self,
        component: &Component,
        export: NativeDataExport<'_>,
        contract: NativeDataResultContract<'_>,
    ) -> Result<NativeDataResultEntry<T>> {
        self.prepare_entry(component, export, Some(contract))
            .await
            .map(NativeDataResultEntry::new)
    }

    async fn prepare_entry(
        mut self,
        component: &Component,
        export: NativeDataExport<'_>,
        contract: Option<NativeDataResultContract<'_>>,
    ) -> Result<NativeDataEntry<T>> {
        let result = async {
            // The selector is borrowed from trusted nativeKit, never copied or
            // retained here. Account for lookup work before scanning its bytes.
            charge_selector(self.allocation_owners(), &export)?;
            if let Some(contract) = contract {
                contract.charge(self.allocation_owners())?;
            }
            let engine = self._engine.engine();
            let (input, output) = validate_component(engine, component, &export, contract)?;

            let mut linker = Linker::<NativeState<T>>::new(engine);
            let mut wall_clock = false;
            let mut monotonic_clock = false;
            for (name, import) in component.component_type().imports(engine) {
                match name {
                    WALL_CLOCK_IMPORT => {
                        validate_clock_instance(engine, &import.ty, ClockKind::Wall)?;
                        wall_clock = true;
                    }
                    MONOTONIC_CLOCK_IMPORT => {
                        validate_clock_instance(engine, &import.ty, ClockKind::Monotonic)?;
                        monotonic_clock = true;
                    }
                    _ => bail!("unsupported native DATA component import"),
                }
            }
            if wall_clock {
                define_wall_clock(&mut linker)?;
            }
            if monotonic_clock {
                define_monotonic_clock(&mut linker)?;
            }

            let wasmtime_store = self
                .store
                .as_mut()
                .context("native Setup Store was retired")?;
            let owner = wasmtime_store.data().owners.component().clone();
            wasmtime_store.host_allocation_budget(owner);
            let instance = linker
                .instantiate_async(wasmtime_store, component)
                .await
                .map_err(anyhow::Error::from)
                .context("instantiate native DATA component")?;
            let function = locate_function(
                &instance,
                self.store
                    .as_mut()
                    .context("native Setup Store was retired")?,
                &export,
            )?;

            Ok::<_, anyhow::Error>((instance, function, input, output))
        }
        .await;

        match result {
            Ok((instance, function, input, output)) => {
                let deadline = self.original_deadline();
                Ok(NativeDataEntry {
                    store: Some(self),
                    _instance: Some(instance),
                    function: Some(function),
                    input,
                    output,
                    deadline,
                    observation: NativeDataObservation::default(),
                })
            }
            Err(error) => {
                self.retire();
                Err(error)
            }
        }
    }
}

impl<T: Send + 'static> NativeDataEntry<T> {
    /// Invoke the retained DATA function with an optional immutable string
    /// envelope and return an immutable CoreBuffer owned by the original
    /// native graph budget.
    pub async fn invoke(&mut self, input: Option<&CoreBuffer>) -> Result<CoreBuffer> {
        match self.call(input).await? {
            NativeDataReply::Value(NativeDataValue::String(output)) => Ok(output),
            _ => {
                self.retire();
                bail!("native DATA string entry returned an unexpected result shape")
            }
        }
    }

    async fn call(&mut self, input: Option<&CoreBuffer>) -> Result<NativeDataReply> {
        // The future owns the live Store while suspended. Dropping the future
        // therefore destroys it; an interrupted entry can never be reused.
        // A successful call alone transfers custody back to the retained entry.
        let mut active = Self {
            store: self.store.take(),
            _instance: self._instance.take(),
            function: self.function.take(),
            input: self.input.clone(),
            output: self.output,
            deadline: self.deadline,
            observation: NativeDataObservation::default(),
        };
        self.observation = NativeDataObservation {
            store_retired: true,
            ..NativeDataObservation::default()
        };
        let result = active.invoke_inner(input).await;
        active.sample_observation();
        if result.is_ok() {
            self.store = active.store.take();
            self._instance = active._instance.take();
            self.function = active.function.take();
        } else {
            active.retire();
        }
        self.observation = active.observation;
        result
    }

    /// Return the original absolute deadline; calls never renew it.
    pub fn original_deadline(&self) -> Instant {
        self.deadline
    }

    /// Observe the most recent call, including an error that retired the Store.
    /// After future cancellation only `store_retired` is available as proof;
    /// counters from an unfinished call are deliberately not fabricated.
    pub fn observation(&self) -> NativeDataObservation {
        self.observation
    }

    /// Inspect remaining fuel without exposing the Store.
    pub fn fuel_remaining(&self) -> Result<u64> {
        self.store
            .as_ref()
            .context("native DATA entry was retired")?
            .fuel_remaining()
    }

    async fn invoke_inner(&mut self, input: Option<&CoreBuffer>) -> Result<NativeDataReply> {
        let store = self
            .store
            .as_mut()
            .context("native DATA entry was retired")?;
        let function = self
            .function
            .as_ref()
            .context("native DATA entry was retired")?;
        let (component_owner, owners) = {
            let state = store
                .store
                .as_mut()
                .context("native Setup Store was retired")?;
            let state = state.data_mut();
            ensure_active(state)?;
            (state.owners.component().clone(), state.owners.clone())
        };

        let param = match (&self.input, input) {
            (InputShape::None, None) => None,
            (InputShape::None, Some(_)) => bail!("native DATA function accepts no input"),
            (InputShape::String, None) => bail!("native DATA function requires string input"),
            (InputShape::String, Some(input)) => {
                if input.len() > MAX_BYTES {
                    bail!("native DATA input exceeds the 4 MiB string envelope")
                }
                let text = input.as_str().context("native DATA input is not UTF-8")?;
                let component_input = BudgetedString::copy_from(text, &component_owner, 0)?;
                Some(Val::String(component_input))
            }
        };

        // `call_async_owned` retains both parameter and result allocations
        // under the original ValueOwners, and runs canonical post-return before
        // it returns the lifted result.
        let wasmtime_store = store
            .store
            .as_mut()
            .context("native Setup Store was retired")?;
        let result = match param.as_ref() {
            None => function.call_async_owned(wasmtime_store, &[]).await,
            Some(param) => {
                function
                    .call_async_owned(wasmtime_store, std::slice::from_ref(param))
                    .await
            }
        };
        self.observation.unreachable_trap = result.as_ref().err().is_some_and(|error| {
            matches!(
                error.downcast_ref::<wasmtime::component::CanonicalCallStage>(),
                Some(wasmtime::component::CanonicalCallStage::Guest)
            ) && error.downcast_ref::<wasmtime::Trap>()
                == Some(&wasmtime::Trap::UnreachableCodeReached)
        });
        self.observation.post_return_failed = result.as_ref().err().is_some_and(|error| {
            matches!(
                error.downcast_ref::<wasmtime::component::CanonicalCallStage>(),
                Some(wasmtime::component::CanonicalCallStage::PostReturn)
            )
        });
        let result = result
            .map_err(anyhow::Error::from)
            .context("invoke native DATA function")?;
        self.observation.post_return = true;
        ensure_active(
            store
                .store
                .as_mut()
                .context("native Setup Store was retired")?
                .data_mut(),
        )?;
        let output = match self.output {
            OutputShape::String => {
                let [Val::String(text)] = result.as_slice() else {
                    bail!("native DATA function returned an unexpected value");
                };
                Ok(NativeDataReply::Value(NativeDataValue::String(
                    result::string(text.as_str(), &owners)?,
                )))
            }
            OutputShape::Result(success) => result::decode(result.as_slice(), success, &owners),
        }?;
        ensure_active(
            store
                .store
                .as_mut()
                .context("native Setup Store was retired")?
                .data_mut(),
        )?;
        Ok(output)
    }
}

impl<T: 'static> NativeDataEntry<T> {
    fn sample_observation(&mut self) {
        let Some(store) = self.store.as_ref().and_then(|store| store.store.as_ref()) else {
            return;
        };
        let state = store.data();
        let mut observation = state.clock_observation;
        observation.fuel_remaining = store.get_fuel().ok();
        observation.post_return = self.observation.post_return;
        observation.unreachable_trap = self.observation.unreachable_trap;
        observation.post_return_failed = self.observation.post_return_failed;
        observation.deadline_exceeded = Instant::now() >= self.deadline;
        self.observation = observation;
    }

    /// Retire and actually drop this entry's component instance and Store.
    pub fn retire(&mut self) {
        self.sample_observation();
        if let Some(store) = self.store.as_ref() {
            store.retire();
        }
        self.function.take();
        self._instance.take();
        drop(self.store.take());
        self.observation.store_retired = true;
    }
}

impl<T: 'static> Drop for NativeDataEntry<T> {
    fn drop(&mut self) {
        self.retire();
    }
}

fn charge_selector(owners: &crate::AllocationOwners, export: &NativeDataExport<'_>) -> Result<()> {
    let work = export
        .interface
        .map_or(0, str::len)
        .checked_add(export.function.len())
        .and_then(|bytes| u64::try_from(bytes).ok())
        .and_then(|bytes| bytes.checked_mul(4))
        .and_then(|work| work.checked_add(1))
        .context("native DATA selector work overflow")?;
    owners.graph().reserve(AllocationRequest {
        kind: AllocationKind::Temporary,
        bytes: 0,
        elements: 0,
        depth: 0,
        work,
    })?;
    Ok(())
}

fn locate_function<T: Send + 'static>(
    instance: &Instance,
    store: &mut wasmtime::Store<NativeState<T>>,
    export: &NativeDataExport<'_>,
) -> Result<Func> {
    let index = match export.interface {
        Some(interface) => {
            let interface_index = instance
                .get_export_index(&mut *store, None, interface)
                .context("native DATA interface export missing")?;
            instance
                .get_export_index(&mut *store, Some(&interface_index), export.function)
                .context("native DATA function export missing")?
        }
        None => instance
            .get_export_index(&mut *store, None, export.function)
            .context("native DATA function export missing")?,
    };
    instance
        .get_func(&mut *store, index)
        .context("selected native DATA export is not a function")
}

fn validate_component(
    engine: &Engine,
    component: &Component,
    export: &NativeDataExport<'_>,
    contract: Option<NativeDataResultContract<'_>>,
) -> Result<(InputShape, OutputShape)> {
    if !Engine::same(engine, component.engine()) {
        bail!("native DATA component belongs to another WasmEngine")
    }
    if export.function.is_empty() || export.interface.is_some_and(str::is_empty) {
        bail!("native DATA export selector is empty")
    }

    let component_type = component.component_type();
    for (name, item) in component_type.imports(engine) {
        if is_generic_core_identity(name) || item.implements.is_some_and(is_generic_core_identity) {
            bail!("ordinary Core Pack import is not a native DATA capability")
        }
        if !matches!(name, WALL_CLOCK_IMPORT | MONOTONIC_CLOCK_IMPORT) {
            bail!("unsupported native DATA component import")
        }
    }
    for (name, item) in component_type.exports(engine) {
        if is_generic_core_identity(name) || item.implements.is_some_and(is_generic_core_identity) {
            bail!("ordinary Core Pack ABI export is refused by native DATA")
        }
    }

    let function = match export.interface {
        Some(interface) => {
            let outer = component_type
                .get_export(engine, interface)
                .context("native DATA interface export missing")?;
            let ComponentItem::ComponentInstance(interface_type) = outer.ty else {
                bail!("selected native DATA interface is not a component instance")
            };
            let selected = interface_type
                .get_export(engine, export.function)
                .context("native DATA function export missing")?;
            match selected.ty {
                ComponentItem::ComponentFunc(function) => function,
                _ => bail!("selected native DATA export is not a component function"),
            }
        }
        None => match component_type
            .get_export(engine, export.function)
            .context("native DATA function export missing")?
            .ty
        {
            ComponentItem::ComponentFunc(function) => function,
            _ => bail!("selected native DATA export is not a component function"),
        },
    };
    validate_entry_function(&function, contract)
}

fn is_generic_core_identity(name: &str) -> bool {
    name.starts_with("tobkiri:core/")
        || name.starts_with("tobkiri:core@")
        || name.starts_with("tobkiri:pack/")
        || name.starts_with("tobkiri:pack@")
}

fn validate_entry_function(
    function: &ComponentFunc,
    contract: Option<NativeDataResultContract<'_>>,
) -> Result<(InputShape, OutputShape)> {
    if function.async_() {
        bail!("native DATA function must be synchronous")
    }
    if function.results().len() != 1 {
        bail!("native DATA function must return exactly one selected value");
    }
    let ty = function.results().next().expect("one DATA result");
    let output = match contract {
        None if ty == Type::String => OutputShape::String,
        None => bail!("native DATA function must return exactly one string"),
        Some(contract) => {
            result::validate(&ty, contract)?;
            OutputShape::Result(contract.success)
        }
    };
    let mut params = function.params();
    let input = match params.len() {
        0 => InputShape::None,
        1 if params.next().is_some_and(|(_, ty)| ty == Type::String) => InputShape::String,
        _ => bail!("native DATA function must accept zero arguments or one string"),
    };
    Ok((input, output))
}

#[derive(Clone, Copy)]
enum ClockKind {
    Wall,
    Monotonic,
}

fn validate_clock_instance(engine: &Engine, item: &ComponentItem, kind: ClockKind) -> Result<()> {
    let ComponentItem::ComponentInstance(instance) = item else {
        bail!("native DATA clock import must be a component instance")
    };
    let mut now = false;
    let mut resolution = false;
    let mut exports = 0;
    for (name, export) in instance.exports(engine) {
        exports += 1;
        let function = match export.ty {
            ComponentItem::ComponentFunc(function) => function,
            other if matches!((kind, name), (ClockKind::Wall, "datetime")) => {
                validate_datetime_type(&other)?;
                continue;
            }
            _ => bail!("native DATA clock has an unexpected export"),
        };
        if function.async_() || function.params().len() != 0 || function.results().len() != 1 {
            bail!("native DATA clock function has an unexpected signature")
        }
        let result = function.results().next().expect("one clock result");
        let valid_result = match kind {
            ClockKind::Wall => is_datetime_type(&result),
            ClockKind::Monotonic => result == Type::U64,
        };
        if !valid_result {
            bail!("native DATA clock function has an unexpected result")
        }
        match name {
            "now" if !now => now = true,
            "resolution" if !resolution => resolution = true,
            "now" | "resolution" => bail!("native DATA clock export is duplicated"),
            _ => bail!("native DATA clock has an unexpected export"),
        }
    }
    let expected_exports = if matches!(kind, ClockKind::Wall) {
        3
    } else {
        2
    };
    if exports != expected_exports || !now || !resolution {
        bail!("native DATA clock interface does not match its frozen WIT")
    }
    Ok(())
}

fn is_datetime_type(ty: &Type) -> bool {
    let Type::Record(record) = ty else {
        return false;
    };
    let mut fields = record.fields();
    let Some(seconds) = fields.next() else {
        return false;
    };
    let Some(nanoseconds) = fields.next() else {
        return false;
    };
    seconds.name == "seconds"
        && seconds.ty == Type::U64
        && nanoseconds.name == "nanoseconds"
        && nanoseconds.ty == Type::U32
        && fields.next().is_none()
}

fn validate_datetime_type(item: &ComponentItem) -> Result<()> {
    let ComponentItem::Type(ty) = item else {
        bail!("native DATA wall-clock datetime export must be a type")
    };
    if !is_datetime_type(ty) {
        bail!("native DATA wall-clock datetime type must be seconds:u64,nanoseconds:u32")
    }
    Ok(())
}

fn define_wall_clock<T: Send + 'static>(linker: &mut Linker<NativeState<T>>) -> Result<()> {
    let mut root = linker.root();
    let mut wall = root.instance(WALL_CLOCK_IMPORT)?;
    wall.func_new("now", |mut store, _ty, params, results| {
        clock_callback(&mut store, params, results, ClockOperation::WallNow)
            .map_err(wasmtime::Error::from_anyhow)
    })?;
    wall.func_new("resolution", |mut store, _ty, params, results| {
        clock_callback(&mut store, params, results, ClockOperation::WallResolution)
            .map_err(wasmtime::Error::from_anyhow)
    })?;
    Ok(())
}

fn define_monotonic_clock<T: Send + 'static>(linker: &mut Linker<NativeState<T>>) -> Result<()> {
    let mut root = linker.root();
    let mut monotonic = root.instance(MONOTONIC_CLOCK_IMPORT)?;
    monotonic.func_new("now", |mut store, _ty, params, results| {
        clock_callback(&mut store, params, results, ClockOperation::MonotonicNow)
            .map_err(wasmtime::Error::from_anyhow)
    })?;
    monotonic.func_new("resolution", |mut store, _ty, params, results| {
        clock_callback(
            &mut store,
            params,
            results,
            ClockOperation::MonotonicResolution,
        )
        .map_err(wasmtime::Error::from_anyhow)
    })?;
    Ok(())
}

fn clock_callback<T>(
    store: &mut StoreContextMut<'_, NativeState<T>>,
    params: &[Val],
    results: &mut [Val],
    operation: ClockOperation,
) -> Result<()> {
    if !params.is_empty() || results.len() != 1 {
        bail!("native DATA clock callback signature mismatch");
    }
    let work = match operation {
        ClockOperation::WallResolution | ClockOperation::MonotonicResolution => {
            RESOLUTION_HOST_WORK
        }
        _ => 1,
    };
    begin_clock_call(store, operation, work)?;
    let resolution = match operation {
        ClockOperation::WallNow => {
            let timestamp = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .context("native DATA host wall clock failed")?;
            results[0] = datetime_value(
                store.data().owners.component(),
                timestamp.as_secs(),
                timestamp.subsec_nanos(),
            )?;
            None
        }
        ClockOperation::WallResolution => {
            let value = wall_resolution()?;
            results[0] = datetime_value(
                store.data().owners.component(),
                value / 1_000_000_000,
                u32::try_from(value % 1_000_000_000)?,
            )?;
            Some(value)
        }
        ClockOperation::MonotonicNow => {
            results[0] = Val::U64(monotonic_now()?);
            None
        }
        ClockOperation::MonotonicResolution => {
            let value = monotonic_resolution()?;
            results[0] = Val::U64(value);
            Some(value)
        }
    };
    end_clock_call(store, operation, resolution)
}

fn datetime_value(
    owner: &wasmtime::component::HostAllocationOwner,
    seconds: u64,
    nanoseconds: u32,
) -> Result<Val> {
    let mut fields = BudgetedVecBuilder::new(2, owner, 0)?;
    fields.push((
        BudgetedString::copy_from("seconds", owner, 0)?,
        Val::U64(seconds),
    ))?;
    fields.push((
        BudgetedString::copy_from("nanoseconds", owner, 0)?,
        Val::U32(nanoseconds),
    ))?;
    Ok(Val::Record(fields.finish()?))
}

#[derive(Clone, Copy)]
enum ClockOperation {
    WallNow,
    WallResolution,
    MonotonicNow,
    MonotonicResolution,
}

fn begin_clock_call<T>(
    store: &mut StoreContextMut<'_, NativeState<T>>,
    operation: ClockOperation,
    work: u64,
) -> Result<()> {
    let state = store.data_mut();
    ensure_active(state)?;
    if state.clock_observation.clock_calls >= CLOCK_CALL_LIMIT {
        bail!("native DATA clock callback call limit exceeded")
    }
    state.clock_observation.clock_calls += 1;
    let count = match operation {
        ClockOperation::WallNow => &mut state.clock_observation.wall_now,
        ClockOperation::WallResolution => &mut state.clock_observation.wall_resolution,
        ClockOperation::MonotonicNow => &mut state.clock_observation.monotonic_now,
        ClockOperation::MonotonicResolution => &mut state.clock_observation.monotonic_resolution,
    };
    *count += 1;
    state
        .owners
        .graph()
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work,
        })
        .map_err(|_| anyhow::anyhow!("native DATA clock host-work budget exceeded"))?;
    ensure_active(state)
}

fn end_clock_call<T>(
    store: &mut StoreContextMut<'_, NativeState<T>>,
    operation: ClockOperation,
    resolution: Option<u64>,
) -> Result<()> {
    let state = store.data_mut();
    ensure_active(state)?;
    state.clock_observation.clock_completed += 1;
    match operation {
        ClockOperation::WallResolution => state.clock_observation.wall_resolution_ns = resolution,
        ClockOperation::MonotonicResolution => {
            state.clock_observation.monotonic_resolution_ns = resolution
        }
        ClockOperation::WallNow | ClockOperation::MonotonicNow => {}
    }
    Ok(())
}

fn ensure_active<T>(state: &mut NativeState<T>) -> Result<()> {
    check_epoch(
        state.deadline,
        &state.cancel,
        Some(&state.retirement),
        &mut state.cancel_observed,
    )?;
    Ok(())
}

#[cfg(unix)]
fn wall_resolution() -> Result<u64> {
    clock_resolution(libc::CLOCK_REALTIME)
}

#[cfg(unix)]
const RESOLUTION_HOST_WORK: u64 = 1;

#[cfg(unix)]
fn monotonic_now() -> Result<u64> {
    clock_now(libc::CLOCK_MONOTONIC)
}

#[cfg(unix)]
fn monotonic_resolution() -> Result<u64> {
    clock_resolution(libc::CLOCK_MONOTONIC)
}

#[cfg(unix)]
fn clock_now(clock: libc::clockid_t) -> Result<u64> {
    let mut value = std::mem::MaybeUninit::<libc::timespec>::uninit();
    // SAFETY: `clock_gettime` initializes the supplied timespec on success.
    if unsafe { libc::clock_gettime(clock, value.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error()).context("native DATA host clock failed");
    }
    // SAFETY: the preceding successful call initialized `value`.
    let value = unsafe { value.assume_init() };
    if value.tv_sec < 0 || !(0..1_000_000_000).contains(&value.tv_nsec) {
        bail!("native DATA host clock returned an invalid timestamp")
    }
    let nanos = u128::try_from(value.tv_sec)?
        .checked_mul(1_000_000_000)
        .and_then(|seconds| seconds.checked_add(u128::try_from(value.tv_nsec).ok()?))
        .context("native DATA host clock value overflow")?;
    Ok(u64::try_from(nanos)?)
}

#[cfg(unix)]
fn clock_resolution(clock: libc::clockid_t) -> Result<u64> {
    let mut value = std::mem::MaybeUninit::<libc::timespec>::uninit();
    // SAFETY: `clock_getres` initializes the supplied timespec on success.
    if unsafe { libc::clock_getres(clock, value.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error())
            .context("native DATA host clock resolution failed");
    }
    // SAFETY: the preceding successful call initialized `value`.
    let value = unsafe { value.assume_init() };
    if value.tv_sec < 0 || !(0..1_000_000_000).contains(&value.tv_nsec) {
        bail!("native DATA host clock resolution is invalid")
    }
    let nanos = u128::try_from(value.tv_sec)?
        .checked_mul(1_000_000_000)
        .and_then(|seconds| seconds.checked_add(u128::try_from(value.tv_nsec).ok()?))
        .context("native DATA host clock resolution overflow")?;
    u64::try_from(nanos.max(1)).context("native DATA host clock resolution overflow")
}

#[cfg(not(unix))]
fn wall_resolution() -> Result<u64> {
    measured_resolution(|| {
        let now = SystemTime::now().duration_since(UNIX_EPOCH)?;
        u64::try_from(now.as_nanos()).context("native DATA wall clock value overflow")
    })
}

#[cfg(not(unix))]
const RESOLUTION_HOST_WORK: u64 = RESOLUTION_SAMPLE_LIMIT;

#[cfg(not(unix))]
fn monotonic_now() -> Result<u64> {
    static START: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
    u64::try_from(START.get_or_init(Instant::now).elapsed().as_nanos())
        .context("native DATA monotonic clock value overflow")
}

#[cfg(not(unix))]
fn monotonic_resolution() -> Result<u64> {
    measured_resolution(monotonic_now)
}

#[cfg(not(unix))]
fn measured_resolution(mut sample: impl FnMut() -> Result<u64>) -> Result<u64> {
    let mut previous = sample()?;
    let mut minimum = None::<u64>;
    for _ in 0..RESOLUTION_SAMPLE_LIMIT {
        let current = sample()?;
        if let Some(delta) = current.checked_sub(previous).filter(|delta| *delta > 0) {
            minimum = Some(minimum.map_or(delta, |known| known.min(delta)));
            if minimum == Some(1) {
                break;
            }
        }
        previous = current;
    }
    minimum.context("native DATA host clock resolution could not be measured")
}

#[cfg(test)]
#[path = "native_setup_entry_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "native_data_result_tests.rs"]
mod result_tests;

#[cfg(test)]
#[path = "native_data_schema_probe.rs"]
mod schema_probe;
