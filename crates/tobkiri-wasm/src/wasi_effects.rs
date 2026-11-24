//! Clock and entropy imports are asynchronous Gate effects, never the ambient
//! synchronous implementations provided by the upstream WASI blanket linker.
use anyhow::{Context, Result, bail};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use tobkiri_canon::{AllocationError, CoreBuffer};
use tobkiri_gate::{CallCx, EffectRequest, Gate, Outcome};
use wasmtime::component::{Component, Linker, ResourceAny, Val, types::ComponentItem};
use wasmtime::{AsContextMut, StoreContextMut};
use wasmtime_wasi::p2::{Pollable, bindings, subscribe};
use wasmtime_wasi::{WasiView, clocks::WasiClocksView};

use crate::{
    InvocationState,
    effect_values::{EffectValues, uint},
    host::PackState,
};

/// Only native Store state implements this lookup; guest metadata cannot choose
/// a Gate or producer allocator. The generic seam also tests actual imports.
pub(crate) trait WasiEffects: WasiView {
    fn effect_gate(&self) -> Result<Arc<dyn Gate>>;
    fn effect_owners(&self) -> &crate::owned_values::ValueOwners;
}
impl WasiEffects for PackState {
    fn effect_gate(&self) -> Result<Arc<dyn Gate>> {
        Ok(self
            .shared
            .upgrade()
            .context("WASI host has shut down")?
            .gate
            .clone())
    }
    fn effect_owners(&self) -> &crate::owned_values::ValueOwners {
        &self.owners
    }
}

pub(crate) fn link_gated_wasi<T: WasiEffects + 'static>(
    linker: &mut Linker<InvocationState<T>>,
    component: &Component,
    engine: &wasmtime::Engine,
) -> Result<()> {
    for (name, item) in component.component_type().imports(engine) {
        let Some((interface, version)) = name.rsplit_once('@') else {
            continue;
        };
        if !matches!(
            interface,
            "wasi:clocks/wall-clock"
                | "wasi:clocks/monotonic-clock"
                | "wasi:random/random"
                | "wasi:random/insecure"
                | "wasi:random/insecure-seed"
        ) {
            continue;
        }
        let ComponentItem::ComponentInstance(import) = item.ty else {
            bail!("WASI effect import is not an interface")
        };
        let mut instance = linker.instance(name)?;
        for (operation, item) in import.exports(engine) {
            match item.ty {
                ComponentItem::Type(_) => {}
                ComponentItem::ComponentFunc(_) => {
                    let interface = interface.to_owned();
                    let version = version.to_owned();
                    let operation = operation.to_owned();
                    instance.func_new_async(
                        &operation.clone(),
                        move |mut store, _, params, results| {
                            let interface = interface.clone();
                            let version = version.clone();
                            let operation = operation.clone();
                            Box::new(async move {
                                let value =
                                    invoke(&mut store, &interface, &version, &operation, params)
                                        .await
                                        .map_err(wasmtime::Error::from_anyhow)?;
                                if results.len() != value.len() {
                                    return Err(wasmtime::Error::msg(
                                        "WASI effect result arity mismatch",
                                    ));
                                }
                                results.clone_from_slice(&value);
                                Ok(())
                            })
                        },
                    )?;
                }
                _ => bail!("unsupported WASI clock/random import kind"),
            }
        }
    }
    Ok(())
}

async fn invoke<T: WasiEffects>(
    store: &mut StoreContextMut<'_, InvocationState<T>>,
    interface: &str,
    version: &str,
    operation: &str,
    params: &[Val],
) -> Result<Vec<Val>> {
    let gate = store.data().data.effect_gate()?;
    let cx = store.data().cx.clone();
    gate.check_context(&cx)?;
    let owners = store.data().data.effect_owners().clone();
    let codec = EffectValues(&owners);
    let request = if interface.starts_with("wasi:clocks/") {
        EffectRequest::Clock
    } else {
        EffectRequest::Random
    };
    let args = codec.args_hash(interface, version, operation, params)?;
    let grant = gate.authorize(&cx, request.clone(), args.clone()).await?;
    let mut permit = grant.consume(&cx, &request, &args)?;
    let effect_cx = cx.clone().narrow_limits(permit.limits());
    if let Err(error) = gate.narrow_lease(&mut permit, &effect_cx).await {
        // The same consumed grant remains terminalized even if durable
        // narrowing fails. A delivery failure is surfaced, never hidden.
        if Instant::now() >= permit.deadline() {
            gate.settle_deadline(&permit).await?;
        } else {
            gate.record_outcome(
                permit.id().clone(),
                Outcome::Failed {
                    code: "wasi-effect-prepare".into(),
                },
            )
            .await?;
        }
        return Err(error.into());
    }
    let result = (|| -> Result<Vec<Val>> {
        effect_cx.check_active()?;
        match (interface, operation) {
            ("wasi:clocks/wall-clock", "now" | "resolution") => {
                let mut clocks = store.data_mut().clocks();
                let datetime = if operation == "now" {
                    bindings::clocks::wall_clock::Host::now(&mut clocks)?
                } else {
                    bindings::clocks::wall_clock::Host::resolution(&mut clocks)?
                };
                Ok(vec![
                    codec.record(
                        [
                            ("seconds", Val::U64(datetime.seconds)),
                            ("nanoseconds", Val::U32(datetime.nanoseconds)),
                        ]
                        .into_iter(),
                    )?,
                ])
            }
            ("wasi:clocks/monotonic-clock", "now" | "resolution") => {
                let mut clocks = store.data_mut().clocks();
                let value = if operation == "now" {
                    bindings::clocks::monotonic_clock::Host::now(&mut clocks)?
                } else {
                    bindings::clocks::monotonic_clock::Host::resolution(&mut clocks)?
                };
                Ok(vec![Val::U64(value)])
            }
            ("wasi:clocks/monotonic-clock", "subscribe-instant" | "subscribe-duration") => {
                let value = uint(
                    params
                        .first()
                        .context("clock subscription argument missing")?,
                )?;
                let duration = if operation == "subscribe-instant" {
                    let mut clocks = store.data_mut().clocks();
                    let now = bindings::clocks::monotonic_clock::Host::now(&mut clocks)?;
                    Duration::from_nanos(value.saturating_sub(now))
                } else {
                    Duration::from_nanos(value)
                };
                let target = Instant::now()
                    .checked_add(duration)
                    .unwrap_or(effect_cx.deadline())
                    .min(effect_cx.deadline());
                let timer = store.data_mut().ctx().table.push(OwnerDeadline {
                    cx: effect_cx.clone(),
                    target,
                })?;
                let pollable = subscribe(store.data_mut().ctx().table, timer)?;
                let value = ResourceAny::try_from_resource(pollable, store.as_context_mut())?;
                Ok(vec![Val::Resource(value)])
            }
            ("wasi:random/random", "get-random-bytes")
            | ("wasi:random/insecure", "get-insecure-random-bytes") => {
                let count =
                    usize::try_from(uint(params.first().context("random length missing")?)?)?;
                if count > 1024 * 1024 || count as u64 > effect_cx.limits().output_bytes {
                    bail!("WASI random output limit exceeded")
                }
                let bytes = CoreBuffer::write_exact(count, owners.graph(), |out| {
                    getrandom::fill(out).map_err(|_| AllocationError::Unavailable)
                })?;
                Ok(vec![codec.bytes(bytes.as_bytes())?])
            }
            ("wasi:random/random", "get-random-u64")
            | ("wasi:random/insecure", "get-insecure-random-u64") => {
                if effect_cx.limits().output_bytes < 8 {
                    bail!("WASI random scalar exceeds output limit")
                }
                let mut bytes = [0u8; 8];
                getrandom::fill(&mut bytes).map_err(|_| anyhow::anyhow!("entropy unavailable"))?;
                Ok(vec![Val::U64(u64::from_le_bytes(bytes))])
            }
            ("wasi:random/insecure-seed", "insecure-seed") => {
                if effect_cx.limits().output_bytes < 16 {
                    bail!("WASI random seed exceeds output limit")
                }
                let mut bytes = [0u8; 16];
                getrandom::fill(&mut bytes).map_err(|_| anyhow::anyhow!("entropy unavailable"))?;
                let first = u64::from_le_bytes(bytes[..8].try_into()?);
                let second = u64::from_le_bytes(bytes[8..].try_into()?);
                Ok(vec![
                    codec.tuple([Val::U64(first), Val::U64(second)].into_iter())?,
                ])
            }
            _ => bail!("unsupported WASI clock/random operation"),
        }
    })();
    // A timer's creation is the Clock effect. Polling it cannot outlive its
    // original cancellation/deadline, and the bounded table owns its metadata.
    if Instant::now() >= permit.deadline() {
        gate.settle_deadline(&permit).await?;
        bail!("WASI effect deadline elapsed")
    }
    gate.record_outcome(
        permit.id().clone(),
        if result.is_ok() {
            Outcome::Succeeded
        } else if effect_cx.cancel().is_cancelled() {
            Outcome::Cancelled
        } else {
            Outcome::Failed {
                code: "wasi-effect".into(),
            }
        },
    )
    .await?;
    result
}

struct OwnerDeadline {
    cx: CallCx,
    target: Instant,
}
#[async_trait::async_trait]
impl Pollable for OwnerDeadline {
    async fn ready(&mut self) {
        if self.target <= Instant::now() {
            tokio::task::yield_now().await;
            return;
        }
        tokio::select! {
            _ = self.cx.cancel().cancelled() => {},
            _ = tokio::time::sleep_until(self.cx.deadline().into()) => {},
            _ = tokio::time::sleep_until(self.target.into()) => {},
        }
    }
}
