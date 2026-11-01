//! Native-selected fixed DATA imports over the original managed IO operation.
use super::{NativeState, entry::ensure_active};
use anyhow::{Context, Result, bail};
use std::any::Any;
use std::sync::atomic::{AtomicU8, Ordering};
use tobkiri_canon::{ControlHandle, CoreBuffer, LeasedShared, MAX_BYTES};
use tobkiri_gate::{ControlKind, ControlReservation, NativeSetupInvocation};
use wasmtime::component::types::{ComponentItem, Type};
use wasmtime::component::{BudgetedFuture, BudgetedString, Linker, Val};
use wasmtime::{Engine, StoreContextMut};

/// NativeKit's typed context must retain its actual original invocation.
/// Deep captures/registries/buffers must retain real funding, not byte estimates.
/// Implementations must not provide an arbitrary userdata/capability bag.
pub trait NativeImportState: Send + Sync + 'static {
    /// Actual shared custody, never a serialized scope, counter or approval flag.
    fn native_import_invocation(&self) -> &NativeSetupInvocation;
}

/// Exact supported import input; output is always one UTF-8 DATA string.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeImportInput {
    /// No input parameters, e.g. a no-IO metadata inventory.
    None,
    /// One string parameter named `payload` under the original envelope.
    String,
}

/// Immutable native-selected import contract. Static code owns names/handlers;
/// nothing in a guest frame chooses or registers Rust code.
pub struct NativeImportBinding<T: 'static> {
    /// Dedicated native namespace; ordinary Core/WASI imports are excluded.
    pub interface: &'static str,
    /// Fixed function name within the interface.
    pub function: &'static str,
    /// Exact parameter shape.
    pub input: NativeImportInput,
    /// Synchronous factory, not a captured closure or arbitrary async handler.
    pub prepare: for<'a> fn(NativeImportContext<'a, T>) -> Result<NativeImportOperation>,
}
impl<T> Copy for NativeImportBinding<T> {}
impl<T> Clone for NativeImportBinding<T> {
    fn clone(&self) -> Self {
        *self
    }
}

type Proof = LeasedShared<AtomicU8, ControlReservation>;
const PREPARING: u8 = 0;
const READY: u8 = 1;
const FAILED: u8 = 2;

/// One import's consumed construction ticket. No raw Store/IO/Gate is exposed.
/// The typed borrowed state is only the registered native binding context.
pub struct NativeImportContext<'a, T> {
    state: &'a T,
    invocation: &'a NativeSetupInvocation,
    owners: &'a crate::AllocationOwners,
    io: crate::NativeDataIo,
    proof: Proof,
    input: Option<CoreBuffer>,
}
impl<T> NativeImportContext<'_, T> {
    /// Borrow the fixed typed context. New deep allocations require original
    /// charged constructors; borrowing/cloning an owner grants no authority.
    pub fn state(&self) -> &T {
        self.state
    }

    /// Borrow the original funded input; clones retain the same allocation.
    pub fn input(&self) -> Option<&CoreBuffer> {
        self.input.as_ref()
    }

    /// Borrow the same original DATA accounts for closed nativeKit decoding.
    /// This neither exposes the Store nor grants any source/effect operation.
    pub fn allocation_owners(&self) -> &crate::AllocationOwners {
        self.owners
    }

    /// Compare genuine custody without exposing account/budget constructors.
    pub fn same_invocation(&self, other: &NativeSetupInvocation) -> bool {
        self.invocation.same_invocation(other)
    }

    /// Consume this call's sole ticket. No worker starts until kernel acceptance.
    /// Only the existing inspect path can produce output; no arbitrary Future,
    /// foreign buffer, worker, domain-error normalizer or new budget is accepted.
    pub fn inspect<F>(self, work: F) -> Result<NativeImportOperation>
    where
        F: for<'scope> FnOnce(
                &mut crate::NativeInspectionOutput<'scope>,
            ) -> std::result::Result<(), crate::NativeDataIoError>
            + Send
            + 'static,
    {
        self.inspect_inner(None, work)
    }

    /// Keep actual source/context custody through both original worker Joins
    /// and trailers, including an abandoned call or uncertain physical receipt.
    /// This handle supplies lifetime only, never a native source permission.
    pub fn inspect_retaining<F>(
        self,
        custody: ControlHandle<dyn Any + Send + Sync>,
        work: F,
    ) -> Result<NativeImportOperation>
    where
        F: for<'scope> FnOnce(
                &mut crate::NativeInspectionOutput<'scope>,
            ) -> std::result::Result<(), crate::NativeDataIoError>
            + Send
            + 'static,
    {
        self.inspect_inner(Some(custody), work)
    }

    fn inspect_inner<F>(
        self,
        custody: Option<ControlHandle<dyn Any + Send + Sync>>,
        work: F,
    ) -> Result<NativeImportOperation>
    where
        F: for<'scope> FnOnce(
                &mut crate::NativeInspectionOutput<'scope>,
            ) -> std::result::Result<(), crate::NativeDataIoError>
            + Send
            + 'static,
    {
        if self
            .proof
            .get()
            .compare_exchange(PREPARING, READY, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            self.proof.get().store(FAILED, Ordering::Release);
            bail!("native import construction ticket already consumed");
        }
        let proof = self.proof;
        let invocation = self.invocation.clone();
        let expected = invocation.clone();
        let io = self.io;
        let held_input = self.input;
        let future = BudgetedFuture::new(
            async move {
                let operation = move |output: &mut crate::NativeInspectionOutput<'_>| {
                    let _held_input = held_input;
                    if !expected.same_invocation(output.invocation()) {
                        return Err(crate::NativeDataIoError::CompletionUnknown);
                    }
                    work(output)
                };
                match custody {
                    Some(custody) => io.inspect_retaining(custody, operation).await,
                    None => io.inspect(operation).await,
                }
                .map_err(anyhow::Error::from)
            },
            self.owners.component(),
            0,
        );
        match future {
            Ok(future) => Ok(NativeImportOperation {
                future,
                proof,
                invocation,
            }),
            Err(error) => {
                proof.get().store(FAILED, Ordering::Release);
                Err(error.into())
            }
        }
    }
}

/// Sealed prepared operation. Intentionally neither Future nor Clone: only the
/// kernel can start/poll it, observe outer failures and publish a complete reply.
pub struct NativeImportOperation {
    future: BudgetedFuture<'static, Result<CoreBuffer>>,
    proof: Proof,
    invocation: NativeSetupInvocation,
}

pub(super) trait Contract {
    fn validate(&self, engine: &Engine, name: &str, item: &ComponentItem) -> Result<bool>;
}

pub(super) struct Table<T: 'static> {
    pub(super) bindings: &'static [NativeImportBinding<T>],
    verify: fn(&T, &NativeSetupInvocation) -> Result<()>,
}
pub(super) type TableRef<T> = LeasedShared<Table<T>, ControlReservation>;
impl<T: NativeImportState> Table<T> {
    pub(super) fn new(
        invocation: &NativeSetupInvocation,
        state: &T,
        bindings: &'static [NativeImportBinding<T>],
    ) -> Result<TableRef<T>> {
        verify_state(state, invocation)?;
        if bindings.is_empty() || bindings.len() > 64 {
            bail!("invalid native import table size");
        }
        for (index, binding) in bindings.iter().enumerate() {
            if !binding.interface.starts_with("tobkiri-native:")
                || binding.interface.len() > 256
                || binding.function.is_empty()
                || binding.function.len() > 128
                || !binding
                    .function
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
                || !binding.function.as_bytes()[0].is_ascii_lowercase()
                || bindings[..index].iter().any(|old| {
                    old.interface == binding.interface && old.function == binding.function
                })
            {
                bail!("invalid or duplicate fixed native import");
            }
        }
        let bytes = u64::try_from(TableRef::<T>::storage_bytes())?;
        let control = invocation.control().reserve(ControlKind::Frame, bytes)?;
        Ok(TableRef::try_new_precharged(
            Self {
                bindings,
                verify: verify_state::<T>,
            },
            control,
        )?)
    }
}
fn verify_state<T: NativeImportState>(state: &T, invocation: &NativeSetupInvocation) -> Result<()> {
    if !state.native_import_invocation().same_invocation(invocation) {
        bail!("native import context belongs to another invocation");
    }
    Ok(())
}

impl<T> Contract for Table<T> {
    fn validate(&self, engine: &Engine, name: &str, item: &ComponentItem) -> Result<bool> {
        if !self.bindings.iter().any(|b| b.interface == name) {
            return Ok(false);
        }
        let ComponentItem::ComponentInstance(instance) = item else {
            bail!("native import must be an interface instance");
        };
        let mut count = 0;
        for (function, item) in instance.exports(engine) {
            let binding = self
                .bindings
                .iter()
                .find(|b| b.interface == name && b.function == function)
                .context("unknown fixed native import function")?;
            let ComponentItem::ComponentFunc(func) = item.ty else {
                bail!("native import export must be a function");
            };
            let parameters_match = match binding.input {
                NativeImportInput::None => func.params().len() == 0,
                NativeImportInput::String => func.params().eq([("payload", Type::String)]),
            };
            if func.async_() || !parameters_match || !func.results().eq([Type::String]) {
                bail!("fixed native import ABI differs");
            }
            count += 1;
        }
        if count != self.bindings.iter().filter(|b| b.interface == name).count() {
            bail!("fixed native import interface is incomplete");
        }
        Ok(true)
    }
}
impl<T> Contract for TableRef<T> {
    fn validate(&self, engine: &Engine, name: &str, item: &ComponentItem) -> Result<bool> {
        self.get().validate(engine, name, item)
    }
}

pub(super) fn define<T: Send + 'static>(
    linker: &mut Linker<NativeState<T>>,
    bindings: &'static [NativeImportBinding<T>],
    owner: &wasmtime::component::HostAllocationOwner,
) -> Result<()> {
    for (index, binding) in bindings.iter().enumerate() {
        if bindings[..index]
            .iter()
            .any(|old| old.interface == binding.interface)
        {
            continue;
        }
        let mut interface = linker.instance(binding.interface)?;
        for binding in bindings.iter().filter(|b| b.interface == binding.interface) {
            let binding = *binding;
            interface.func_new_async_owned(
                binding.function,
                owner,
                move |store, _ty, params, results| {
                    let owner = store.data().owners.component().clone();
                    BudgetedFuture::new(
                        async move {
                            call(store, binding, params, results)
                                .await
                                .map_err(wasmtime::Error::from_anyhow)
                        },
                        &owner,
                        0,
                    )
                },
            )?;
        }
    }
    Ok(())
}

async fn call<T: Send + 'static>(
    mut store: StoreContextMut<'_, NativeState<T>>,
    binding: NativeImportBinding<T>,
    params: &[Val],
    results: &mut [Val],
) -> Result<()> {
    ensure_active(store.data_mut())?;
    if results.len() != 1 {
        bail!("native import result arity differs");
    }
    let input = match (binding.input, params) {
        (NativeImportInput::None, []) => None,
        (NativeImportInput::String, [Val::String(text)]) => {
            if text.len() > MAX_BYTES {
                bail!("native import input exceeds original envelope");
            }
            Some(CoreBuffer::copy_from(
                text.as_bytes(),
                store.data().owners.graph(),
            )?)
        }
        _ => bail!("native import arguments differ"),
    };
    let proof = {
        let bytes = u64::try_from(Proof::storage_bytes())?;
        let reservation = store
            .data()
            .invocation
            .control()
            .reserve(ControlKind::Frame, bytes)?;
        Proof::try_new_precharged(AtomicU8::new(PREPARING), reservation)?
    };
    let operation = {
        let state = store.data();
        let table = state
            .imports
            .as_ref()
            .context("native import table is missing")?
            .get();
        (table.verify)(&state.data, &state.invocation)?;
        let context = NativeImportContext {
            state: &state.data,
            invocation: &state.invocation,
            owners: &state.owners,
            io: state
                .io
                .as_ref()
                .context("native import IO is unbound")?
                .clone(),
            proof: proof.clone(),
            input,
        };
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| (binding.prepare)(context)))
            .map_err(|_| anyhow::anyhow!("native import factory panicked"))??
    };
    if proof.get().load(Ordering::Acquire) != READY
        || !std::ptr::eq(proof.get(), operation.proof.get())
        || !store
            .data()
            .invocation
            .same_invocation(&operation.invocation)
    {
        bail!("native import factory failed or returned a foreign operation");
    }
    ensure_active(store.data_mut())?;
    let bytes = operation.future.await?;
    ensure_active(store.data_mut())?;
    let text = bytes
        .as_str()
        .context("native import output is not UTF-8")?;
    let result = BudgetedString::copy_from(text, store.data().owners.component(), 0)?;
    ensure_active(store.data_mut())?;
    results[0] = Val::String(result);
    Ok(())
}

#[cfg(test)]
#[path = "native_fixed_import_tests.rs"]
mod tests;
