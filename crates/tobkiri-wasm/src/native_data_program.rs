//! Fixed nativeKit export bindings over one original native DATA instance.
use super::{
    InputShape, NativeDataEntry, NativeDataExport, NativeDataObservation, NativeDataReply,
    NativeDataResultContract, NativeDataValue, NativeSetupStore, OutputShape, charge_selector,
    locate_function, validate_component,
};
use anyhow::{Context, Result, bail};
use std::time::Instant;
use tobkiri_canon::{AllocationKind, AllocationRequest, CoreBuffer, LeasedShared};
use tobkiri_gate::{ControlKind, ControlReservation};
use wasmtime::component::{Component, Func};

/// Borrowed export contract selected by trusted nativeKit before instantiation.
/// It is not an IPC operation, guest selector, Pack identity or effect grant.
#[derive(Clone, Copy)]
pub struct NativeDataBinding<'a> {
    /// The root/interface function to bind once in this exact component.
    pub export: NativeDataExport<'a>,
    /// None requires a plain string; Some preserves the exact result contract.
    pub result: Option<NativeDataResultContract<'a>>,
}

struct BoundFunction {
    function: Option<Func>,
    input: InputShape,
    output: OutputShape,
}

// LeasedShared drops the complete Vec and physically frees its shared block
// before releasing the original control reservation. These fixed nativeKit
// bindings are control metadata, not guest DATA in the 64 MiB payload account.
type Bindings = LeasedShared<Vec<BoundFunction>, ControlReservation>;

struct BindingBuilder {
    values: Option<Vec<BoundFunction>>,
    release: Option<ControlReservation>,
}
impl BindingBuilder {
    fn new<T: 'static>(store: &NativeSetupStore<T>, count: usize) -> Result<Self> {
        let state = store
            .store
            .as_ref()
            .context("native Setup Store was retired")?
            .data();
        let bytes = count
            .checked_mul(size_of::<BoundFunction>())
            .and_then(|n| n.checked_add(Bindings::storage_bytes()))
            .and_then(|n| n.checked_add(size_of::<Self>()))
            .and_then(|n| n.checked_add(size_of::<NativeDataProgram<T>>()))
            .and_then(|n| u64::try_from(n).ok())
            .context("native DATA binding storage overflow")?;
        state.owners.graph().reserve(AllocationRequest {
            kind: AllocationKind::Sequence,
            bytes: 0,
            elements: u64::try_from(count)?,
            depth: 0,
            work: u64::try_from(count)?
                .checked_add(1)
                .context("binding work overflow")?,
        })?;
        let control = state
            .invocation
            .control()
            .reserve(ControlKind::Frame, bytes)?;
        let mut builder = Self {
            values: Some(Vec::new()),
            release: Some(control),
        };
        let values = builder.values.as_mut().expect("new binding storage");
        values.try_reserve_exact(count)?;
        if values.capacity() != count {
            bail!("native DATA binding allocator capacity is unsupported");
        }
        Ok(builder)
    }

    fn finish(mut self) -> Result<Bindings> {
        Ok(Bindings::try_new_precharged(
            self.values.take().expect("live binding storage"),
            self.release.take().expect("live binding reservation"),
        )?)
    }
}
impl Drop for BindingBuilder {
    fn drop(&mut self) {
        drop(self.values.take());
        drop(self.release.take());
    }
}

/// Fixed typed functions on one original Store and component instance.
///
/// NativeKit owns initialization, domain state and retry rules. This runner
/// supplies no init bypass and never combines separate native invocations into
/// a renewed lease. All calls, including domain errors, retain original fuel,
/// deadline, cancellation, memory and host-work accounting.
pub struct NativeDataProgram<T: 'static> {
    entry: NativeDataEntry<T>,
    bindings: Option<Bindings>,
}

impl<T: Send + 'static> NativeSetupStore<T> {
    /// Validate all fixed signatures before instantiating this exact component.
    /// Only the existing explicit DATA imports are available. The borrowed
    /// selectors/contracts are neither copied nor retained after binding.
    pub async fn prepare_data_program(
        self,
        component: &Component,
        bindings: &[NativeDataBinding<'_>],
    ) -> Result<NativeDataProgram<T>> {
        let first = bindings.first().context("native DATA bindings are empty")?;
        let mut table = BindingBuilder::new(&self, bindings.len())?;
        for binding in bindings {
            charge_selector(self.allocation_owners(), &binding.export)?;
            if let Some(contract) = binding.result {
                contract.charge(self.allocation_owners())?;
            }
            let (input, output) = validate_component(
                self._engine.engine(),
                component,
                &binding.export,
                binding.result,
                self.native_data_io().is_some() && self.captured_inputs().is_some(),
            )?;
            table
                .values
                .as_mut()
                .expect("live binding storage")
                .push(BoundFunction {
                    function: None,
                    input,
                    output,
                });
        }
        // Existing preparation activates all original limits before guest
        // instantiation. It does not call any nativeKit initialization export.
        let mut entry = self
            .prepare_entry(component, first.export, first.result)
            .await?;
        let instance = entry
            ._instance
            .as_ref()
            .context("native DATA instance missing")?;
        let store = entry.store.as_mut().context("native DATA Store missing")?;
        for (bound, binding) in table
            .values
            .as_mut()
            .expect("live binding storage")
            .iter_mut()
            .zip(bindings)
        {
            charge_selector(store.allocation_owners(), &binding.export)?;
            bound.function = Some(locate_function(
                instance,
                store
                    .store
                    .as_mut()
                    .context("native Setup Store was retired")?,
                &binding.export,
            )?);
        }
        Ok(NativeDataProgram {
            entry,
            bindings: Some(table.finish()?),
        })
    }
}

impl<T: Send + 'static> NativeDataProgram<T> {
    /// Invoke one already-bound slot chosen by trusted nativeKit. Exclusive
    /// access lasts through owned lifting/post-return/output custody. The inner
    /// error is only DATA; nativeKit must enforce its original state machine.
    pub async fn invoke(
        &mut self,
        slot: usize,
        input: Option<&CoreBuffer>,
    ) -> Result<std::result::Result<NativeDataValue, super::NativeDataError>> {
        let Some(bound) = self
            .bindings
            .as_ref()
            .and_then(|table| table.get().get(slot))
        else {
            self.retire();
            bail!("native DATA binding slot is unavailable");
        };
        self.entry.function = bound.function;
        self.entry.input = bound.input.clone();
        self.entry.output = bound.output;
        match self.entry.call(input).await {
            Ok(NativeDataReply::Value(value)) => Ok(Ok(value)),
            Ok(NativeDataReply::DomainError(error)) => Ok(Err(error)),
            Err(error) => {
                self.retire();
                Err(error)
            }
        }
    }
    /// Original absolute deadline, including after retirement.
    pub fn original_deadline(&self) -> Instant {
        self.entry.original_deadline()
    }
    /// Remaining fuel in the original Store; never a fresh allowance.
    pub fn fuel_remaining(&self) -> Result<u64> {
        self.entry.fuel_remaining()
    }
    /// Most recent bounded observation, with no approval or effect meaning.
    pub fn observation(&self) -> NativeDataObservation {
        self.entry.observation()
    }
}
impl<T: 'static> NativeDataProgram<T> {
    /// Actually retire the Store before releasing its fixed binding metadata.
    pub fn retire(&mut self) {
        self.entry.retire();
        drop(self.bindings.take());
    }
}
impl<T: 'static> Drop for NativeDataProgram<T> {
    fn drop(&mut self) {
        self.retire();
    }
}

#[cfg(test)]
#[path = "native_data_program_tests.rs"]
mod tests;
