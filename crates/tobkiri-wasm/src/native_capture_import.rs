//! Exact closed ordinal DATA import; no path discovery or generic WASI IO.
use super::{NativeState, ensure_active};
use anyhow::{Context, Result, bail};
use wasmtime::component::types::{ComponentItem, Type};
use wasmtime::component::{BudgetedFuture, BudgetedVec, HostAllocationOwner, Linker, Val};
use wasmtime::{Engine, StoreContextMut};

pub(super) const INTERFACE: &str = "tobkiri-ui:inputs/captured@1.0.0";

pub(super) fn validate(engine: &Engine, item: &ComponentItem) -> Result<()> {
    let ComponentItem::ComponentInstance(instance) = item else {
        bail!("native captured input import must be a component instance");
    };
    let mut exports = instance.exports(engine);
    let Some(("read-range", export)) = exports.next() else {
        bail!("native captured input import must contain read-range");
    };
    if exports.next().is_some() {
        bail!("native captured input import has extra exports");
    }
    let ComponentItem::ComponentFunc(function) = export.ty else {
        bail!("native captured read-range must be a function");
    };
    if function.async_()
        || !function.params().eq([
            ("ordinal", Type::U32),
            ("offset", Type::U64),
            ("length", Type::U32),
        ])
        || function.results().len() != 1
    {
        bail!("native captured read-range signature differs");
    }
    let Some(Type::List(list)) = function.results().next() else {
        bail!("native captured read-range must return bytes");
    };
    if list.ty() != Type::U8 {
        bail!("native captured read-range must return bytes");
    }
    Ok(())
}

pub(super) fn define<T: Send + 'static>(
    linker: &mut Linker<NativeState<T>>,
    owner: &HostAllocationOwner,
) -> Result<()> {
    linker.instance(INTERFACE)?.func_new_async_owned(
        "read-range",
        owner,
        |store, _ty, params, results| {
            let owner = store.data().owners.component().clone();
            BudgetedFuture::new(
                async move {
                    read_range(store, params, results)
                        .await
                        .map_err(wasmtime::Error::from_anyhow)
                },
                &owner,
                0,
            )
        },
    )?;
    Ok(())
}

async fn read_range<T: Send + 'static>(
    mut store: StoreContextMut<'_, NativeState<T>>,
    params: &[Val],
    results: &mut [Val],
) -> Result<()> {
    let [Val::U32(ordinal), Val::U64(offset), Val::U32(length)] = params else {
        bail!("native captured read-range arguments differ");
    };
    if results.len() != 1 {
        bail!("native captured read-range result arity differs");
    }
    ensure_active(store.data_mut())?;
    let io = store
        .data()
        .io
        .as_ref()
        .context("native captured IO is unbound")?
        .clone();
    let bytes = io.read_range(*ordinal, *offset, *length).await?;
    ensure_active(store.data_mut())?;
    let values = BudgetedVec::from_iter(
        bytes.as_bytes().iter().copied().map(Val::U8),
        store.data().owners.component(),
        0,
    )?;
    // The funded source and destination coexist through copying/lowering.
    // A retirement during that copy must not publish a guest result.
    ensure_active(store.data_mut())?;
    results[0] = Val::List(values);
    Ok(())
}

#[cfg(test)]
#[path = "native_capture_import_tests.rs"]
mod tests;
