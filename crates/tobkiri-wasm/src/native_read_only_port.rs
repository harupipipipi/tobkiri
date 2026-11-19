//! A private, stateless DATA port on the original admitted native Store.

use super::{NativeSetupStore, NativeState, entry::ensure_active, legacy_registry::read_component};
use anyhow::{Context, Result, bail, ensure};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tobkiri_canon::{
    AllocationKind, AllocationRequest, CoreBuffer, CoreValue, MAX_BYTES,
    ids::{OpName, ProfileId, ShapeRef},
};
use tobkiri_gate::ControlKind;
use tobkiri_manifest::{Cardinality, RuntimeSpec, ShapeKind, VerifiedPack, WitFunction};
use wasmtime::{
    Engine, Store,
    component::{
        BudgetedBox, BudgetedString, BudgetedVec, Component, Func, Linker, Val,
        types::ComponentItem,
    },
};

const TYPES: &str = "tobkiri:core/types@1.2.0";
const FLOW: &str = "tobkiri:core/flow@1.2.0";
const CLOCK: &str = "tobkiri:core/clock@1.2.0";
const MAX_CLOCK_CALLS: u8 = 3;
const MAX_COMPONENT_BYTES: u64 = 4 * 1024 * 1024;

pub(super) struct ActiveCall {
    profile: Option<CoreBuffer>,
    clock_remaining: u8,
}

/// Single invocation of the verified typed port, bound to its original Store.
///
/// The native caller selects a stateless port, owns the real Profile and
/// application authorization, and retains its worker through physical cleanup.
/// The returned values are DATA and carry no authority to publish an effect.
pub struct NativeReadOnlyPort<'a, T: 'static> {
    store: &'a mut NativeSetupStore<T>,
    function: Func,
    signature: &'a WitFunction,
    call: Option<ActiveCall>,
    output_bytes: u64,
}

impl<T: Send + 'static> NativeSetupStore<T> {
    /// Bind a fixed verified, no-config port before entering the source worker.
    ///
    /// Linking exposes only the caller's Profile and the selected number of
    /// fresh wall-clock observations during the one invocation. Incidental
    /// compiler WASI CLI/IO imports link to unconditional traps. The caller
    /// must authenticate the Profile and the verified Pack for its original
    /// operation and retain the authority for any subsequent effect.
    pub async fn prepare_declared_read_only_port<'a>(
        &'a mut self,
        pack: &'a VerifiedPack,
        shape: &ShapeRef,
        operation: &OpName,
        profile: &ProfileId,
        deadline: Instant,
        max_wall_clock_calls: u8,
    ) -> Result<NativeReadOnlyPort<'a, T>> {
        let store = self.store.as_mut().context("native Store was retired")?;
        ensure_active(store.data_mut())?;
        store.data_mut().claim_component()?;
        ensure!(
            max_wall_clock_calls <= MAX_CLOCK_CALLS,
            "native read-only clock allowance exceeds its limit"
        );
        let signature = declared_function(pack, shape, operation)?;
        let permissions = &pack.manifest.permissions.wasm;
        let manifest_deadline = Instant::now()
            .checked_add(Duration::from_millis(permissions.wall_ms))
            .unwrap_or(deadline);
        let fuel = store.get_fuel()?.min(permissions.fuel_per_call);
        let memory = usize::try_from(permissions.memory_mib.saturating_mul(1024 * 1024))
            .unwrap_or(usize::MAX);
        let state = store.data_mut();
        state.deadline = state.deadline.min(deadline).min(manifest_deadline);
        state.limits.memory_cap = state.limits.memory_cap.min(memory);
        state.limits.table_cap = state
            .limits
            .table_cap
            .min(permissions.table_elements as usize);
        ensure_active(state)?;
        ensure!(
            fuel > 0 && state.limits.memory_cap > 0 && state.limits.table_cap > 0,
            "native read-only component has an empty execution budget"
        );

        let RuntimeSpec::Wasm {
            component,
            core_modules,
            config,
            ..
        } = &pack.manifest.runtime
        else {
            bail!("native read-only component must use Wasm")
        };
        ensure!(
            core_modules.is_empty() && config.is_none(),
            "native read-only component declares dependencies or configuration"
        );
        let artifact = pack
            .files
            .get(component)
            .context("verified native read-only component is missing")?;
        let length = artifact.len();
        ensure!(
            (1..=MAX_COMPONENT_BYTES).contains(&length),
            "native read-only component exceeds its source envelope"
        );
        let compilation_bytes = length * 8 + 1024 * 1024;
        self.component_control = Some(
            state
                .invocation
                .control()
                .reserve(ControlKind::Load, compilation_bytes)?,
        );
        let owners = state.owners.clone();
        let profile = CoreBuffer::copy_from(profile.as_str().as_bytes(), owners.graph())?;
        let bytes = read_component(artifact, &owners)?;
        ensure_active(store.data_mut())?;
        let engine = self._engine.engine();
        let component = Component::new(engine, bytes.as_bytes())
            .map_err(anyhow::Error::from)
            .context("compile verified native read-only component")?;
        drop(bytes);
        let image = component.image_range();
        ensure!(
            image.end.addr() - image.start.addr() <= usize::try_from(compilation_bytes)?,
            "compiled native read-only component exceeds its envelope"
        );
        let component_type = component.component_type();
        for (name, item) in component_type.imports(engine) {
            ensure!(
                (matches!(name, TYPES | FLOW | CLOCK) || trap_only_toolchain_wasi(name))
                    && matches!(item.ty, ComponentItem::ComponentInstance(_)),
                "native read-only component has an unsupported import"
            );
        }
        crate::abi::validate_abi_with_shapes(engine, &component, pack, |reference| {
            pack.shapes
                .iter()
                .find(|shape| shape.reference == *reference)
        })?;
        let interface = crate::host::interface_name(shape);
        let Some(ComponentItem::ComponentInstance(interface_type)) = component_type
            .get_export(engine, &interface)
            .map(|item| item.ty)
        else {
            bail!("native read-only interface export is missing")
        };
        ensure!(
            matches!(interface_type.get_export(engine, operation.as_str()).map(|item| item.ty),
                Some(ComponentItem::ComponentFunc(function)) if !function.async_()),
            "native read-only function export is missing or asynchronous"
        );
        let mut linker = Linker::new(engine);
        linker.define_unknown_imports_as_traps(&component)?;
        linker.allow_shadowing(true);
        link_context(&mut linker, engine, &component, &owners)?;
        ensure_active(store.data_mut())?;
        store.set_fuel(fuel)?;
        store.host_allocation_budget(owners.component().clone());
        let instance = linker
            .instantiate_async(&mut *store, &component)
            .await
            .map_err(anyhow::Error::from)
            .context("instantiate verified native read-only component")?;
        ensure_active(store.data_mut())?;
        let interface = instance
            .get_export_index(&mut *store, None, &interface)
            .context("native read-only interface export is missing")?;
        let function = instance
            .get_export_index(&mut *store, Some(&interface), operation.as_str())
            .and_then(|index| instance.get_func(&mut *store, index))
            .context("native read-only function export is missing")?;
        Ok(NativeReadOnlyPort {
            store: self,
            function,
            signature,
            call: Some(ActiveCall {
                profile: Some(profile),
                clock_remaining: max_wall_clock_calls,
            }),
            output_bytes: permissions.output_bytes.min(MAX_BYTES as u64),
        })
    }
}

impl<T: Send + 'static> NativeReadOnlyPort<'_, T> {
    /// Borrow the original funded component and DATA allocators.
    pub fn allocation_owners(&self) -> &crate::AllocationOwners {
        self.store.allocation_owners()
    }

    /// Invoke once, keeping argument and result allocations in the original
    /// account. Arguments must use the original allocation owners. Successful
    /// lifting includes canonical post-return and the declared output bound.
    pub async fn invoke(mut self, parameters: &[Val]) -> Result<BudgetedVec<Val>> {
        let store = self
            .store
            .store
            .as_mut()
            .context("native Store was retired")?;
        ensure_active(store.data_mut())?;
        store.data_mut().read_only_call = self.call.take();
        let result = self
            .function
            .call_async_owned(&mut *store, parameters)
            .await;
        store.data_mut().read_only_call = None;
        let values = result
            .map_err(anyhow::Error::from)
            .context("invoke verified native read-only port")?;
        ensure_active(store.data_mut())?;
        check_output(store, self.signature, values.as_slice(), self.output_bytes)?;
        ensure_active(store.data_mut())?;
        Ok(values)
    }
}

impl<T: 'static> Drop for NativeReadOnlyPort<'_, T> {
    fn drop(&mut self) {
        if let Some(store) = self.store.store.as_mut() {
            store.data_mut().read_only_call = None;
        }
    }
}

fn declared_function<'a>(
    pack: &'a VerifiedPack,
    shape: &ShapeRef,
    operation: &OpName,
) -> Result<&'a WitFunction> {
    ensure!(
        pack.manifest.ports.provides.iter().any(|port| {
            port.shape == shape.name && port.version == shape.version && !port.optional
        }),
        "verified Pack does not provide the required native read-only port"
    );
    let mut matches = pack
        .shapes
        .iter()
        .filter(|candidate| candidate.reference == *shape);
    let definition = matches
        .next()
        .context("native read-only shape is missing")?;
    ensure!(
        matches.next().is_none()
            && definition.kind == ShapeKind::Port
            && definition.cardinality == Cardinality::One
            && definition.streams.is_empty()
            && definition.callbacks.is_empty(),
        "native read-only shape declares an unsupported contract"
    );
    let function = definition
        .functions
        .get(operation)
        .context("native read-only operation is missing")?;
    ensure!(
        !function
            .params
            .iter()
            .any(|(_, ty)| crate::dispatch::has_handle(ty))
            && !function
                .result
                .as_ref()
                .is_some_and(crate::dispatch::has_handle),
        "native read-only port cannot transfer handles"
    );
    Ok(function)
}

fn check_output<T>(
    store: &Store<NativeState<T>>,
    signature: &WitFunction,
    values: &[Val],
    limit: u64,
) -> Result<()> {
    let owners = &store.data().owners;
    let value = match (signature.result.as_ref(), values) {
        (None, []) => CoreValue::null(owners.graph())?,
        (Some(ty), [value]) => crate::owned_values::val_to_core(value, ty, owners)?,
        _ => bail!("native read-only port result differs from its declaration"),
    };
    let encoded = value.encode_guest(owners.graph())?;
    ensure!(
        encoded.len() as u64 <= limit,
        "native read-only port output exceeds its declared budget"
    );
    Ok(())
}

fn trap_only_toolchain_wasi(name: &str) -> bool {
    (name.starts_with("wasi:cli/") || name.starts_with("wasi:io/"))
        && crate::imports::permitted_wasi(name)
}

fn link_context<T: Send + 'static>(
    linker: &mut Linker<NativeState<T>>,
    engine: &Engine,
    component: &Component,
    owners: &crate::AllocationOwners,
) -> Result<()> {
    for (interface, item) in component.component_type().imports(engine) {
        if !matches!(interface, FLOW | CLOCK) {
            continue;
        }
        let ComponentItem::ComponentInstance(ty) = item.ty else {
            bail!("native read-only context declares an unsupported import")
        };
        let mut binding = linker.instance(interface)?;
        for (name, item) in ty.exports(engine) {
            match item.ty {
                ComponentItem::ComponentFunc(function) if !function.async_() => {}
                ComponentItem::Type(_) => continue,
                _ => bail!("native read-only context declares an unsupported import"),
            }
            let current_profile = (interface, name) == (FLOW, "current-profile");
            let wall_clock = (interface, name) == (CLOCK, "now-ms");
            binding.func_new_owned(
                name,
                owners.component(),
                move |mut store, _, args, output| {
                    let state = store.data_mut();
                    let value = if current_profile {
                        profile_value(state, args, output)
                    } else if wall_clock {
                        now_ms(state, args, output)
                    } else {
                        return Err(wasmtime::Error::msg(
                            "native read-only port capability is unavailable",
                        ));
                    };
                    value.map_err(wasmtime::Error::from_anyhow)
                },
            )?;
        }
    }
    Ok(())
}

fn profile_value<T>(state: &mut NativeState<T>, args: &[Val], output: &mut [Val]) -> Result<()> {
    ensure_active(state)?;
    let ([], [result]) = (args, output) else {
        bail!("native read-only Profile signature differs")
    };
    let profile = state
        .read_only_call
        .as_mut()
        .and_then(|call| call.profile.take())
        .context("native read-only Profile is unavailable")?;
    let owner = state.owners.component();
    let value = Val::String(BudgetedString::copy_from(profile.as_str()?, owner, 1)?);
    *result = Val::Result(Ok(Some(BudgetedBox::new(value, owner, 0)?)));
    ensure_active(state)
}

fn now_ms<T>(state: &mut NativeState<T>, args: &[Val], output: &mut [Val]) -> Result<()> {
    ensure_active(state)?;
    let ([], [result]) = (args, output) else {
        bail!("native read-only clock signature differs")
    };
    let call = state
        .read_only_call
        .as_mut()
        .context("native read-only clock is unavailable")?;
    call.clock_remaining = call
        .clock_remaining
        .checked_sub(1)
        .context("native read-only clock allowance exhausted")?;
    state.owners.graph().reserve(AllocationRequest {
        kind: AllocationKind::Temporary,
        bytes: 0,
        elements: 0,
        depth: 0,
        work: 1,
    })?;
    *result = Val::U64(u64::try_from(
        SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis(),
    )?);
    ensure_active(state)
}
