//! A private, stateless DATA port on the original admitted native Store.

use super::{NativeSetupStore, NativeState, entry::ensure_active, legacy_registry::read_component};
use anyhow::{Context, Result, bail, ensure};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tobkiri_canon::{
    AllocationKind, AllocationRequest, CoreBuffer, CoreValue, MAX_BYTES,
    ids::{OpName, ProfileId, ShapeRef},
};
use tobkiri_gate::ControlKind;
use tobkiri_manifest::{Cardinality, RuntimeSpec, ShapeKind, VerifiedPack, WitFunction, WitType};
use wasmtime::{
    Engine, Store,
    component::{
        BudgetedBox, BudgetedString, BudgetedVec, Component, ComponentExportIndex, Func, Linker,
        Val, types::ComponentItem,
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

struct ReadOnlyOperation<'a> {
    function: Func,
    signature: &'a WitFunction,
    call: Option<ActiveCall>,
}

/// One call, or an ordered pair of calls, bound to the original native Store.
///
/// The native caller selects a stateless port, owns the real Profile and
/// application authorization, and retains its worker through physical cleanup.
/// The returned values are DATA and carry no authority to publish an effect.
pub struct NativeReadOnlyPort<'a, T: 'static> {
    store: &'a mut NativeSetupStore<T>,
    operation: ReadOnlyOperation<'a>,
    next: Option<ReadOnlyOperation<'a>>,
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
        self.prepare_read_only_port(
            pack,
            (shape, operation),
            None,
            profile,
            deadline,
            max_wall_clock_calls,
        )
        .await
    }

    /// Bind two distinct operations from one verified Pack in the original Store.
    /// Both calls share the instance, fuel, deadline and allocation owners. The
    /// pair has no wall-clock allowance. The first call uses the document path.
    pub async fn prepare_declared_read_only_pair<'a>(
        &'a mut self,
        pack: &'a VerifiedPack,
        [first, second]: [(&ShapeRef, &OpName); 2],
        profile: &ProfileId,
        deadline: Instant,
    ) -> Result<NativeReadOnlyPort<'a, T>> {
        ensure!(
            first != second,
            "native read-only pair repeats an operation"
        );
        self.prepare_read_only_port(pack, first, Some(second), profile, deadline, 0)
            .await
    }

    async fn prepare_read_only_port<'a>(
        &'a mut self,
        pack: &'a VerifiedPack,
        (shape, operation): (&ShapeRef, &OpName),
        next: Option<(&ShapeRef, &OpName)>,
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
        let next_signature = next
            .map(|(shape, operation)| declared_function(pack, shape, operation))
            .transpose()?;
        if next_signature.is_some() {
            require_document_output(signature)?;
        }
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
        let export = read_only_export(&component, shape, operation)?;
        let next_export = next
            .map(|(shape, operation)| read_only_export(&component, shape, operation))
            .transpose()?;
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
        let function = instance
            .get_func(&mut *store, &export)
            .context("native read-only function export is missing")?;
        let next = if let Some((export, signature)) = next_export.zip(next_signature) {
            let function = instance
                .get_func(&mut *store, &export)
                .context("native read-only function export is missing")?;
            Some(ReadOnlyOperation {
                function,
                signature,
                call: Some(ActiveCall {
                    profile: Some(profile.share()),
                    clock_remaining: 0,
                }),
            })
        } else {
            None
        };
        Ok(NativeReadOnlyPort {
            store: self,
            operation: ReadOnlyOperation {
                function,
                signature,
                call: Some(ActiveCall {
                    profile: Some(profile),
                    clock_remaining: max_wall_clock_calls,
                }),
            },
            next,
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
        self.require_final_call()?;
        self.invoke_bounded(parameters, false).await
    }

    /// Apply the admitted output limit to a successful document's UTF-8 bytes.
    ///
    /// Only a declared result with a string success type supports this path.
    /// The result and errors still use the verified canonical ABI. The caller
    /// validates the document before consuming it or dispatching a child.
    pub async fn invoke_with_document_limit(
        mut self,
        parameters: &[Val],
    ) -> Result<BudgetedVec<Val>> {
        self.require_final_call()?;
        require_document_output(self.operation.signature)?;
        self.invoke_bounded(parameters, true).await
    }

    /// Run the first call of a prepared pair with the document limit. Yield the
    /// second call only after canonical post-return and all output checks pass.
    pub async fn invoke_with_document_limit_then(
        mut self,
        parameters: &[Val],
    ) -> Result<(BudgetedVec<Val>, Self)> {
        let next = self
            .next
            .take()
            .context("native read-only pair is unavailable")?;
        let values = self.invoke_bounded(parameters, true).await?;
        self.operation = next;
        Ok((values, self))
    }

    fn require_final_call(&self) -> Result<()> {
        ensure!(
            self.next.is_none(),
            "native read-only pair requires the first document call"
        );
        Ok(())
    }

    async fn invoke_bounded(
        &mut self,
        parameters: &[Val],
        document_limit: bool,
    ) -> Result<BudgetedVec<Val>> {
        let store = self
            .store
            .store
            .as_mut()
            .context("native Store was retired")?;
        ensure_active(store.data_mut())?;
        store.data_mut().read_only_call = self.operation.call.take();
        let result = self
            .operation
            .function
            .call_async_owned(&mut *store, parameters)
            .await;
        store.data_mut().read_only_call = None;
        let values = result
            .map_err(anyhow::Error::from)
            .context("invoke verified native read-only port")?;
        ensure_active(store.data_mut())?;
        if document_limit && let [Val::Result(Ok(Some(document)))] = values.as_slice() {
            let Val::String(text) = &**document else {
                bail!("native read-only document differs from its declaration");
            };
            ensure!(
                text.as_str().len() as u64 <= self.output_bytes,
                "native read-only port document exceeds its declared budget"
            );
        } else {
            check_output(
                store,
                self.operation.signature,
                values.as_slice(),
                self.output_bytes,
            )?;
        }
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

fn require_document_output(signature: &WitFunction) -> Result<()> {
    ensure!(
        matches!(signature.result.as_ref(), Some(WitType::Result { ok, .. })
            if matches!(ok.as_deref(), Some(WitType::String))),
        "native read-only document output requires a string result"
    );
    Ok(())
}

fn read_only_export(
    component: &Component,
    shape: &ShapeRef,
    operation: &OpName,
) -> Result<ComponentExportIndex> {
    let interface = crate::host::interface_name(shape);
    let Some((ComponentItem::ComponentInstance(_), interface)) =
        component.get_export(None, interface.as_str())
    else {
        bail!("native read-only interface export is missing")
    };
    match component.get_export(Some(&interface), operation.as_str()) {
        Some((ComponentItem::ComponentFunc(function), export)) if !function.async_() => Ok(export),
        _ => bail!("native read-only function export is missing or asynchronous"),
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
