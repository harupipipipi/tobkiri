//! Bounded retry for authenticated pre-mutation contention during native
//! preparation. One original owner shares a single attempt counter across
//! the identity and bind phases; only the concrete Core implementation can
//! report retryable Busy, and every wait races the original CallCx
//! cancellation and absolute deadline.

use anyhow::{Context, Result, bail};
use std::sync::Arc;
use tobkiri_gate::{CallCx, ControlKind, GateError};

use crate::{NativeIdAttempt, ProviderBindAttempt, ProviderFrame};

/// Post-busy waits indexed by the consumed combined attempt; the eighth
/// attempt never waits.
const BUSY_WAIT_MS: [u64; 7] = [1, 2, 4, 8, 16, 16, 16];
const MAX_ATTEMPTS: u8 = 8;

/// One original owner's bounded preparation. Its physical retry/timer
/// metadata reservation is precharged and retained across every wait; no
/// task is spawned and no Core/Host guard is held across one.
pub(crate) struct NativePreparation {
    cx: CallCx,
    attempts: u8,
    _reservation: tobkiri_gate::ControlReservation,
}

impl NativePreparation {
    /// Precharge this owner's retry metadata before any attempt.
    pub(crate) fn new(cx: &CallCx) -> Result<Self> {
        // The deadline and backoff branches retain separate Sleep futures
        // simultaneously while select! waits, so precharge both layouts.
        let bytes = std::mem::size_of::<tokio::time::Sleep>()
            .checked_mul(2)
            .and_then(|timers| std::mem::size_of::<Self>().checked_add(timers))
            .context("native preparation bound overflow")?;
        let reservation = cx
            .control()
            .reserve(ControlKind::Frame, u64::try_from(bytes)?)?;
        Ok(Self {
            cx: cx.clone(),
            attempts: 0,
            _reservation: reservation,
        })
    }

    /// Consume one combined attempt BEFORE the call. Successes consume one
    /// too; attempts are never refunded or reset between phases.
    fn consume(&mut self) -> Result<u8> {
        self.cx.check_active()?;
        if self.attempts >= MAX_ATTEMPTS {
            bail!("native preparation attempts exhausted")
        }
        self.attempts += 1;
        Ok(self.attempts)
    }

    /// One fixed post-busy wait racing the ORIGINAL cancellation and
    /// absolute deadline; there is no wait once attempt eight is consumed.
    async fn post_busy_wait(&self, consumed: u8) -> Result<()> {
        let Some(wait) = usize::from(consumed)
            .checked_sub(1)
            .and_then(|index| BUSY_WAIT_MS.get(index))
        else {
            return Ok(());
        };
        tokio::select! {
            _ = self.cx.cancel().cancelled() => Err(GateError::Cancelled.into()),
            _ = tokio::time::sleep_until(self.cx.deadline().into()) => {
                Err(GateError::Deadline.into())
            }
            _ = tokio::time::sleep(std::time::Duration::from_millis(*wait)) => Ok(()),
        }
    }

    /// Bounded identity acquisition: only positive Busy waits and retries;
    /// Uncertain and context failures fail closed.
    pub(crate) async fn native_execution_id(
        &mut self,
        frame: &Arc<dyn ProviderFrame>,
    ) -> Result<crate::NativeExecutionId> {
        loop {
            let consumed = self.consume()?;
            let outcome = frame.try_native_execution_id();
            self.cx.check_active()?;
            match outcome {
                NativeIdAttempt::Ready(id) => return Ok(id),
                NativeIdAttempt::Uncertain(error) => {
                    return Err(anyhow::Error::new(error));
                }
                NativeIdAttempt::Busy => {
                    self.post_busy_wait(consumed).await?;
                    if self.attempts >= MAX_ATTEMPTS {
                        bail!("native execution identity preparation busy")
                    }
                }
            }
        }
    }

    /// Bounded bind of the SAME already funded scope and index registration.
    /// Busy returns the unchanged candidate; at most one OnceLock commit can
    /// occur and nothing is retried or undone afterwards.
    pub(crate) async fn bind_provider_operation(
        &mut self,
        frame: &Arc<dyn ProviderFrame>,
        mut scope: crate::ProviderOperationScope,
    ) -> Result<()> {
        loop {
            let consumed = self.consume()?;
            let outcome = frame.try_bind_provider_operation(scope);
            self.cx.check_active()?;
            match outcome {
                ProviderBindAttempt::Bound => return Ok(()),
                ProviderBindAttempt::Uncertain(error) => {
                    return Err(anyhow::Error::new(error));
                }
                ProviderBindAttempt::Busy(returned) => {
                    scope = returned;
                    self.post_busy_wait(consumed).await?;
                    if self.attempts >= MAX_ATTEMPTS {
                        bail!("provider operation bind preparation busy")
                    }
                }
            }
        }
    }
}
