//! Generation-owned terminal reads. The importer may disappear while the
//! accepted owner drops the actual read future and settles its original grant.

use super::effect_outcome;
use crate::{
    child_census::NativeWorkError,
    effect_values::EffectValues,
    host::{NativeWorkSpawner, PackHost, Shared},
    owned_values::ValueOwners,
};
use anyhow::{Context, Result, bail};
use std::{future::Future, pin::Pin, sync::Arc, task::Poll, time::Instant};
use tobkiri_canon::{CoreBuffer, LeasedShared};
use tobkiri_gate::{
    CallCx, CancellationFuture, ConsumptionAttempt, ControlKind, ControlReservation,
    DispatchPermit, EffectRequest, Gate, GateError, Grant, GrantId,
};
use tokio::{sync::oneshot, time::Sleep};
use wasmtime::component::Val;

type Funding = LeasedShared<(), ControlReservation>;

// The result's payload has its original component owners. This additional
// shared charge covers only the fixed handoff/outer-arity native metadata.
struct ReadOutput {
    result: Result<Vec<Val>>,
    _funding: Funding,
}
struct CallerCustody {
    output: oneshot::Receiver<ReadOutput>,
    _funding: Funding,
}
struct ReadCustody {
    grant: Grant,
    request: EffectRequest,
    args: tobkiri_canon::Digest,
    id: GrantId,
    cx: CallCx,
    shared: Arc<Shared>,
    owners: ValueOwners,
    prompt: CoreBuffer,
    output: oneshot::Sender<ReadOutput>,
    deadline: Pin<Box<Sleep>>,
    cancelled: Pin<Box<CancellationFuture>>,
    // All channels, retained handles and waiter/timer storage precede refund.
    funding: Funding,
}

impl ReadCustody {
    fn deliver(self, result: Result<Vec<Val>>) {
        let _ = self.output.send(ReadOutput {
            result,
            _funding: self.funding.clone(),
        });
    }
}

async fn narrow(gate: &dyn Gate, cx: &CallCx, permit: &mut DispatchPermit) -> Result<CallCx> {
    let narrowed = cx.clone().narrow_limits(permit.limits());
    gate.narrow_lease(permit, &narrowed).await?;
    let narrowed = narrowed.cap_deadline(permit.deadline());
    gate.watch_lease(&narrowed, permit)?;
    gate.check_context(&narrowed)?;
    narrowed.check_active()?;
    if Instant::now() >= permit.deadline() {
        return Err(GateError::Deadline.into());
    }
    Ok(narrowed)
}

async fn terminal(
    gate: &dyn Gate,
    permit: &DispatchPermit,
    id: GrantId,
    result: &Result<Vec<Val>>,
    cx: &CallCx,
) -> Result<()> {
    // Select once. Gate owns the immutable durable winner and retries only
    // its delivery; neither cancellation nor receiver loss proposes again.
    let selected = effect_outcome(result, cx);
    if Instant::now() >= permit.deadline() {
        gate.settle_deadline(permit).await?;
    } else if let Err(error) = gate.record_outcome(id, selected).await {
        if matches!(error, GateError::Deadline) {
            gate.settle_deadline(permit).await?;
        } else {
            return Err(error.into());
        }
    }
    Ok(())
}

async fn supervise(mut custody: ReadCustody) -> std::result::Result<(), NativeWorkError> {
    if let Err(error) = custody.shared.gate.check_context(&custody.cx) {
        custody.deliver(Err(error.into()));
        return Ok(());
    }
    // Admit this same owned cancellation waiter before the durable consume.
    // Its allocation failure cannot strand a consumed terminal or enter IO.
    let ready = std::future::poll_fn(|cx| {
        Poll::Ready(match custody.cancelled.as_mut().poll(cx) {
            Poll::Pending => Ok(()),
            Poll::Ready(Ok(_)) => Err(GateError::Cancelled),
            Poll::Ready(Err(_)) => Err(GateError::Unavailable),
        })
    })
    .await;
    if let Err(error) = ready.and_then(|()| custody.cx.check_active()) {
        custody.deliver(Err(error.into()));
        return Ok(());
    }
    let consumed = match custody
        .grant
        .consume_owned(&custody.cx, &custody.request, &custody.args)
    {
        ConsumptionAttempt::BeforeConsumed(error) => {
            // The original unconsumed grant was refused; no dispatch exists.
            let _ = custody.output.send(ReadOutput {
                result: Err(error.into()),
                _funding: custody.funding.clone(),
            });
            return Ok(());
        }
        ConsumptionAttempt::CommitUnknown(unknown) => {
            let resolved = matches!(unknown.reconcile(), Ok(true));
            let _ = custody.output.send(ReadOutput {
                result: Err(GateError::Unavailable.into()),
                _funding: custody.funding.clone(),
            });
            drop(unknown);
            return if resolved {
                Ok(())
            } else {
                Err(NativeWorkError::CompletionUnknown)
            };
        }
        ConsumptionAttempt::Consumed(consumed) => consumed,
    };
    let original_cx = consumed.context().clone();
    let mut permit = match consumed.into_dispatch() {
        Ok(permit) => permit,
        Err(cleanup) => {
            let resolved = matches!(
                cleanup.settle_failure(custody.shared.gate.as_ref()).await,
                Ok(true)
            );
            let _ = custody.output.send(ReadOutput {
                result: Err(GateError::Unavailable.into()),
                _funding: custody.funding.clone(),
            });
            drop(cleanup);
            return if resolved {
                Ok(())
            } else {
                Err(NativeWorkError::CompletionUnknown)
            };
        }
    };
    let cx = match narrow(custody.shared.gate.as_ref(), &original_cx, &mut permit).await {
        Ok(cx) => cx,
        Err(error) => {
            let result = Err(error);
            let settled = terminal(
                custody.shared.gate.as_ref(),
                &permit,
                custody.id,
                &result,
                &original_cx,
            )
            .await;
            let terminal_known = settled.is_ok();
            let _ = custody.output.send(ReadOutput {
                result: settled.and(result),
                _funding: custody.funding.clone(),
            });
            return if terminal_known {
                Ok(())
            } else {
                Err(NativeWorkError::CompletionUnknown)
            };
        }
    };
    custody.deadline.as_mut().reset(cx.deadline().into());
    let result = async {
        let max = usize::try_from(cx.limits().output_bytes.min(1024 * 1024))?;
        // End the actual HostIo future's scope before selecting/persisting a
        // cancelled terminal. No physical read is claimed complete on a wake.
        let line = {
            let read = custody.shared.io.terminal_read_line_budgeted(
                custody.prompt.as_str()?,
                max,
                custody.owners.graph().clone(),
            );
            tokio::pin!(read);
            tokio::select! {
                biased;
                line = &mut read => line?,
                _ = custody.deadline.as_mut() => bail!("terminal input deadline elapsed"),
                _ = custody.cancelled.as_mut() => bail!("terminal input cancelled"),
            }
        };
        if line.len() > max {
            bail!("terminal input exceeds its bound");
        }
        // A terminal read has no Store resource conversion. The same original
        // component owners can finish its complete result in this owner task.
        let codec = EffectValues(&custody.owners);
        codec.ok(Some(codec.string(line.as_str()?)?))
    }
    .await;
    let settled = terminal(
        custody.shared.gate.as_ref(),
        &permit,
        custody.id,
        &result,
        &cx,
    )
    .await;
    let terminal_known = settled.is_ok();
    let _ = custody.output.send(ReadOutput {
        result: settled.and(result),
        _funding: custody.funding.clone(),
    });
    // Dropping the receiver never alters the winner selected above. Original
    // permit, prompt, output owners and native funding live through settlement.
    // A failed persistence/delivery attempt is not a quiescence success even
    // when the importer has disappeared and cannot receive its error.
    if terminal_known {
        Ok(())
    } else {
        Err(NativeWorkError::CompletionUnknown)
    }
}

pub(super) async fn read_line(
    host: &PackHost,
    cx: &CallCx,
    spawner: NativeWorkSpawner,
    grant: Grant,
    prompt: &str,
    owners: &ValueOwners,
) -> Result<Vec<Val>> {
    if !matches!(grant.effect(), EffectRequest::HostIo { operation } if operation == "terminal-read-line")
    {
        bail!("terminal read requires its original HostIo grant");
    }
    // Same fixed one-operation native handoff bound as filesystem imports,
    // plus exact dynamic context/ID/request copies. The generation spawner
    // separately reserves the actual submitted future's measured size.
    // Custody, the consumed original context and its narrowed live context
    // can coexist. Charge all three dynamic copies before any is made.
    let context_bytes = cx
        .clone_storage_bytes()?
        .checked_mul(3)
        .context("terminal read context size overflow")?;
    let bytes = (16usize * 1024)
        .checked_add(context_bytes)
        .and_then(|bytes| bytes.checked_add(grant.id().clone_storage_bytes()))
        .and_then(|bytes| bytes.checked_add("terminal-read-line".len()))
        .context("terminal read handoff size overflow")?;
    let reservation = cx
        .control()
        .reserve(ControlKind::Task, u64::try_from(bytes)?)?;
    let funding = Funding::try_new_precharged((), reservation)?;
    let prompt = CoreBuffer::copy_from(prompt.as_bytes(), owners.graph())?;
    let id = grant.id().try_clone_precharged()?;
    let request = grant.effect().clone();
    let args = grant.args_digest().clone();
    let deadline = Box::pin(tokio::time::sleep_until(cx.deadline().into()));
    let cancelled = Box::pin(cx.cancel().cancelled());
    let (output, receiver) = oneshot::channel();
    let caller = CallerCustody {
        output: receiver,
        _funding: funding.clone(),
    };
    let custody = ReadCustody {
        grant,
        request,
        args,
        id,
        cx: cx.clone(),
        shared: host.shared.clone(),
        owners: owners.clone(),
        prompt,
        output,
        deadline,
        cancelled,
        funding,
    };
    spawner.bind_cancel(cx.cancel().clone())?;
    spawner.require_host_io_read_join();
    // Only this accepted generation owner may consume the original grant.
    // Submission rejection drops it unconsumed outside the registry guards.
    spawner.spawn(supervise(custody))?;
    let output = caller
        .output
        .await
        .context("terminal read owner unavailable")?;
    output.result
}
