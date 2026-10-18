//! Cleanup knowledge for one actual Store generation, never a dispatch grant.
use anyhow::{Context, Result, bail};
use futures_util::{
    FutureExt,
    future::{BoxFuture, Shared},
};
use std::{
    future::Future,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tobkiri_canon::ids::{PackHash, PackId};
use tobkiri_gate::{ControlKind, ControlReservation, CoreControl};
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

type Completion = Shared<BoxFuture<'static, Result<(), ()>>>;

enum State {
    Live,
    Retiring {
        deadline: Instant,
        completion: Completion,
    },
    Joined,
    // No actual Store was installed, so this is not a destruction receipt.
    Unstarted,
    Unknown,
}

pub(crate) struct GenerationRetirement {
    pack: PackId,
    hash: PackHash,
    number: u64,
    token: CancellationToken,
    destroyed: AtomicBool,
    state: Mutex<State>,
    control: CoreControl,
    _registration: ControlReservation,
}

impl GenerationRetirement {
    pub(crate) fn new(
        control: &CoreControl,
        pack: &PackId,
        hash: &PackHash,
        number: u64,
    ) -> Result<Arc<Self>> {
        if number == 0 {
            bail!("invalid Store generation")
        }
        let registration = control.reserve(ControlKind::Registration, 1024)?;
        Ok(Arc::new(Self {
            pack: pack.clone(),
            hash: hash.clone(),
            number,
            token: CancellationToken::new(),
            destroyed: AtomicBool::new(false),
            state: Mutex::new(State::Live),
            control: control.clone(),
            _registration: registration,
        }))
    }
    pub(crate) fn number(&self) -> u64 {
        self.number
    }
    pub(crate) fn token(&self) -> CancellationToken {
        self.token.clone()
    }
    pub(crate) fn matches(&self, pack: &PackId, hash: &PackHash, number: u64) -> bool {
        &self.pack == pack && &self.hash == hash && self.number == number
    }
    pub(crate) fn is_live(&self) -> bool {
        !self.token.is_cancelled()
            && self
                .state
                .lock()
                .is_ok_and(|state| matches!(*state, State::Live))
    }
    /// Called only after the exact owned Store AND external physical lease drop.
    pub(crate) fn destroyed(&self) {
        self.token.cancel();
        self.destroyed.store(true, Ordering::Release);
        if let Ok(mut state) = self.state.lock() {
            if matches!(*state, State::Live) {
                *state = State::Joined;
            }
            // An existing retirement task must still be joined.
        }
    }
    pub(crate) fn unstarted(&self) {
        self.token.cancel();
        if let Ok(mut state) = self.state.lock() {
            if matches!(*state, State::Live) {
                *state = State::Unstarted;
            }
        }
    }
    pub(crate) fn destruction_observed(&self) -> bool {
        self.destroyed.load(Ordering::Acquire)
    }
    fn complete(&self, result: Result<(), ()>) -> Result<()> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| anyhow::anyhow!("Store retirement state poisoned"))?;
        if result.is_ok() && self.destruction_observed() {
            *state = State::Joined;
            Ok(())
        } else {
            *state = State::Unknown;
            bail!("Store retirement completion unknown")
        }
    }
    /// Poll only an existing task receipt. Never start cleanup or wait here.
    pub(crate) fn replacement_ready(&self) -> Result<bool> {
        let completion = {
            let state = self
                .state
                .lock()
                .map_err(|_| anyhow::anyhow!("Store retirement state poisoned"))?;
            match &*state {
                State::Joined | State::Unstarted => return Ok(true),
                State::Retiring { completion, .. } => completion.clone(),
                State::Live | State::Unknown => return Ok(false),
            }
        };
        match completion.now_or_never() {
            Some(result) => self.complete(result).map(|()| true),
            None => Ok(false),
        }
    }
    /// One task per actual generation. Dropped waiters keep its shared join.
    /// All waiters use the first task's absolute deadline, not a new grace.
    pub(crate) async fn retire<F, Fut>(self: &Arc<Self>, work: F) -> Result<()>
    where
        F: FnOnce() -> Fut,
        Fut: Future<Output = Result<()>> + Send + 'static,
    {
        let (deadline, completion) = {
            let mut state = self
                .state
                .lock()
                .map_err(|_| anyhow::anyhow!("Store retirement state poisoned"))?;
            match &*state {
                State::Joined => return Ok(()),
                State::Unstarted | State::Unknown => bail!("Store retirement completion unknown"),
                State::Retiring {
                    deadline,
                    completion,
                } => (*deadline, completion.clone()),
                State::Live => {
                    self.token.cancel();
                    *state = State::Unknown;
                    let task_control = self.control.reserve(ControlKind::Task, 4096)?;
                    let runtime = tokio::runtime::Handle::try_current()
                        .context("Store retirement runtime unavailable")?;
                    let task = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        let future = work();
                        runtime.spawn(async move {
                            let _task_control = task_control;
                            future.await
                        })
                    }))
                    .map_err(|_| anyhow::anyhow!("Store retirement spawn failed"))?;
                    let completion = async move {
                        match task.await {
                            Ok(Ok(())) => Ok(()),
                            _ => Err(()),
                        }
                    }
                    .boxed()
                    .shared();
                    let deadline = Instant::now() + std::time::Duration::from_secs(2);
                    *state = State::Retiring {
                        deadline,
                        completion: completion.clone(),
                    };
                    (deadline, completion)
                }
            }
        };
        let _waiter = self.control.reserve(ControlKind::Waiter, 256)?;
        let result = tokio::time::timeout_at(deadline, completion)
            .await
            .context("Store retirement completion unknown")?;
        self.complete(result)
    }
}

#[cfg(test)]
mod tests;
