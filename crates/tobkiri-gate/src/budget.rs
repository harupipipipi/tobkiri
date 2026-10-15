//! Pure accounting shared by one top-level Core execution and all descendants.
use crate::LoopLimits;
use std::{
    fmt,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

/// Bounded number of simultaneously active loop scopes, excluding the root.
pub const MAX_ACTIVE_LOOP_SCOPES: usize = 32;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChargeKind {
    PortDispatch,
    SubFlow,
    ForwardedFrame,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BudgetError {
    Allocation,
    InvalidLimits,
    Deadline,
    Cost,
    Iterations,
    ScopeDepth,
    ContextMismatch,
    Poisoned,
}
impl BudgetError {
    pub fn code(self) -> &'static str {
        match self {
            Self::Allocation
            | Self::Deadline
            | Self::Cost
            | Self::Iterations
            | Self::ScopeDepth => "limit-exceeded",
            Self::InvalidLimits | Self::ContextMismatch => "invalid",
            Self::Poisoned => "internal",
        }
    }
}
impl fmt::Display for BudgetError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Allocation => "budget context storage allocation failed",
            Self::InvalidLimits => "budget limits must narrow the execution ceiling",
            Self::Deadline => "execution budget deadline expired",
            Self::Cost => "execution cost ceiling exceeded",
            Self::Iterations => "loop iteration ceiling exceeded",
            Self::ScopeDepth => "active loop scope depth exceeded",
            Self::ContextMismatch => "budget contexts belong to different executions",
            Self::Poisoned => "execution budget lock poisoned",
        })
    }
}
impl std::error::Error for BudgetError {}

#[derive(Debug)]
struct Ledger {
    lock: Mutex<()>,
}
#[derive(Debug)]
struct Scope {
    deadline: Instant,
    limits: LoopLimits,
    cost: AtomicU64,
    iterations: AtomicU64,
}
/// Immutable scope inheritance. Cloning is bounded by the active-scope limit;
/// streams retain the exact scopes they own and cannot reset counters.
#[derive(Clone, Debug)]
pub struct BudgetContext {
    ledger: Arc<Ledger>,
    scopes: Vec<Arc<Scope>>,
}
/// A successful irreversible unit charge. It is noncloneable and has no refund.
#[derive(Debug)]
pub struct ChargedUnit {
    kind: ChargeKind,
}
impl ChargedUnit {
    pub fn kind(&self) -> ChargeKind {
        self.kind
    }
}
/// One loop activation's counter, independent of other activations/iterations.
#[derive(Debug)]
pub struct LoopActivation {
    context: BudgetContext,
    scope: Arc<Scope>,
}
impl BudgetContext {
    /// Core creates a fresh ledger only at a top-level execution boundary.
    pub fn new(limits: LoopLimits, original_deadline: Instant) -> Result<Self, BudgetError> {
        if !limits.validate() {
            return Err(BudgetError::InvalidLimits);
        }
        let deadline = bounded_deadline(original_deadline, limits.max_wall_ms)?;
        let root = Arc::new(Scope {
            deadline,
            limits,
            cost: AtomicU64::new(0),
            iterations: AtomicU64::new(0),
        });
        let mut scopes = Vec::new();
        scopes
            .try_reserve_exact(1)
            .map_err(|_| BudgetError::Allocation)?;
        scopes.push(root);
        Ok(Self {
            ledger: Arc::new(Ledger {
                lock: Mutex::new(()),
            }),
            scopes,
        })
    }
    /// Heap bytes needed for a fallible copy of the bounded scope handles.
    pub fn clone_storage_bytes(&self) -> Result<u64, BudgetError> {
        let count = u64::try_from(self.scopes.len()).map_err(|_| BudgetError::Allocation)?;
        count
            .checked_mul(std::mem::size_of::<Arc<Scope>>() as u64)
            .ok_or(BudgetError::Allocation)
    }
    /// Clone active references while sharing the ledger and all counters.
    pub fn try_clone_precharged(&self) -> Result<Self, BudgetError> {
        let mut scopes = Vec::new();
        scopes
            .try_reserve_exact(self.scopes.len())
            .map_err(|_| BudgetError::Allocation)?;
        scopes.extend(self.scopes.iter().cloned());
        Ok(Self {
            ledger: self.ledger.clone(),
            scopes,
        })
    }
    pub fn check(&self) -> Result<(), BudgetError> {
        let _guard = self.ledger.lock.lock().map_err(|_| BudgetError::Poisoned)?;
        self.check_deadlines()
    }
    fn check_deadlines(&self) -> Result<(), BudgetError> {
        if self
            .scopes
            .iter()
            .any(|scope| Instant::now() >= scope.deadline)
        {
            Err(BudgetError::Deadline)
        } else {
            Ok(())
        }
    }
    /// Precheck every applicable cap, then charge all scopes exactly once.
    pub fn charge(&self, kind: ChargeKind) -> Result<ChargedUnit, BudgetError> {
        let _guard = self.ledger.lock.lock().map_err(|_| BudgetError::Poisoned)?;
        self.check_deadlines()?;
        if self
            .scopes
            .iter()
            .any(|scope| scope.cost.load(Ordering::SeqCst) >= scope.limits.max_cost_units)
        {
            return Err(BudgetError::Cost);
        }
        for scope in &self.scopes {
            scope.cost.fetch_add(1, Ordering::SeqCst);
        }
        Ok(ChargedUnit { kind })
    }
    /// Add a cap without resetting any parent counter. The local cap may be
    /// wider than an active parent because every active scope is checked.
    pub fn enter_loop(&self, limits: LoopLimits) -> Result<(Self, LoopActivation), BudgetError> {
        if !limits.validate() || !limits.is_subset_of(&self.scopes[0].limits) {
            return Err(BudgetError::InvalidLimits);
        }
        if self.scopes.len() - 1 >= MAX_ACTIVE_LOOP_SCOPES {
            return Err(BudgetError::ScopeDepth);
        }
        let _guard = self.ledger.lock.lock().map_err(|_| BudgetError::Poisoned)?;
        self.check_deadlines()?;
        let inherited_deadline = self
            .scopes
            .iter()
            .map(|scope| scope.deadline)
            .min()
            .expect("root scope");
        let deadline = bounded_deadline(inherited_deadline, limits.max_wall_ms)?;
        let scope = Arc::new(Scope {
            deadline,
            limits,
            cost: AtomicU64::new(0),
            iterations: AtomicU64::new(0),
        });
        let mut context = self.try_clone_precharged()?;
        context
            .scopes
            .try_reserve_exact(1)
            .map_err(|_| BudgetError::Allocation)?;
        context.scopes.push(scope.clone());
        let activation_context = context.try_clone_precharged()?;
        let activation = LoopActivation {
            context: activation_context,
            scope,
        };
        Ok((context, activation))
    }
    pub fn cost_units(&self) -> u64 {
        self.scopes[0].cost.load(Ordering::SeqCst)
    }
    pub fn active_scope_count(&self) -> usize {
        self.scopes.len().saturating_sub(1)
    }
    pub fn inherits(&self, parent: &Self) -> bool {
        Arc::ptr_eq(&self.ledger, &parent.ledger)
            && parent.scopes.iter().all(|parent_scope| {
                self.scopes
                    .iter()
                    .any(|scope| Arc::ptr_eq(scope, parent_scope))
            })
    }
    pub fn same_execution(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.ledger, &other.ledger)
    }
    pub fn deadline(&self) -> Instant {
        self.scopes
            .iter()
            .map(|scope| scope.deadline)
            .min()
            .expect("root scope")
    }
}
impl LoopActivation {
    /// Increment only this activation after checking every active cap.
    pub fn next_iteration(&self) -> Result<(), BudgetError> {
        let _guard = self
            .context
            .ledger
            .lock
            .lock()
            .map_err(|_| BudgetError::Poisoned)?;
        self.context.check_deadlines()?;
        if self
            .context
            .scopes
            .iter()
            .any(|scope| scope.cost.load(Ordering::SeqCst) >= scope.limits.max_cost_units)
        {
            return Err(BudgetError::Cost);
        }
        if self.scope.iterations.load(Ordering::SeqCst) >= self.scope.limits.max_iterations {
            return Err(BudgetError::Iterations);
        }
        self.scope.iterations.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
    pub fn iterations(&self) -> u64 {
        self.scope.iterations.load(Ordering::SeqCst)
    }
}
fn bounded_deadline(original: Instant, wall_ms: u64) -> Result<Instant, BudgetError> {
    let now = Instant::now();
    let local = now
        .checked_add(Duration::from_millis(wall_ms))
        .ok_or(BudgetError::InvalidLimits)?;
    let deadline = original.min(local);
    if now >= deadline {
        Err(BudgetError::Deadline)
    } else {
        Ok(deadline)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn limits(cost: u64) -> LoopLimits {
        LoopLimits {
            max_iterations: 3,
            max_wall_ms: 10_000,
            max_cost_units: cost,
        }
    }
    fn root(cost: u64) -> BudgetContext {
        BudgetContext::new(limits(cost), Instant::now() + Duration::from_secs(10)).unwrap()
    }
    #[test]
    fn descendant_charges_share_root_and_all_active_loop_caps() {
        let root = root(3);
        let (outer, outer_loop) = root.enter_loop(limits(2)).unwrap();
        let (inner, inner_loop) = outer.enter_loop(limits(1)).unwrap();
        outer_loop.next_iteration().unwrap();
        inner_loop.next_iteration().unwrap();
        inner.charge(ChargeKind::PortDispatch).unwrap();
        assert_eq!(root.cost_units(), 1);
        assert_eq!(outer.scopes[1].cost.load(Ordering::SeqCst), 1);
        assert_eq!(
            inner.charge(ChargeKind::SubFlow).unwrap_err(),
            BudgetError::Cost
        );
        assert_eq!(root.cost_units(), 1);
        assert_eq!(outer.scopes[1].cost.load(Ordering::SeqCst), 1);
        outer.charge(ChargeKind::ForwardedFrame).unwrap();
        assert_eq!(root.cost_units(), 2);
    }
    #[test]
    fn parallel_charges_precheck_all_scopes_atomically() {
        let root = root(100);
        let (scope, _) = root.enter_loop(limits(7)).unwrap();
        let handles: Vec<_> = (0..16)
            .map(|_| {
                let scope = scope.clone();
                std::thread::spawn(move || scope.charge(ChargeKind::PortDispatch).is_ok())
            })
            .collect();
        let accepted = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .filter(|accepted| *accepted)
            .count();
        assert_eq!(accepted, 7);
        assert_eq!(root.cost_units(), 7);
    }
    #[test]
    fn loop_iterations_are_per_activation_and_do_not_bill_cost() {
        let root = root(10);
        let (_, first) = root.enter_loop(limits(10)).unwrap();
        let (_, second) = root.enter_loop(limits(10)).unwrap();
        for _ in 0..3 {
            first.next_iteration().unwrap();
        }
        assert_eq!(first.next_iteration().unwrap_err(), BudgetError::Iterations);
        second.next_iteration().unwrap();
        assert_eq!(second.iterations(), 1);
        assert_eq!(root.cost_units(), 0);
    }
    #[test]
    fn retained_stream_context_keeps_caps_after_activation_drops() {
        let root = root(10);
        let (stream, activation) = root.enter_loop(limits(1)).unwrap();
        drop(activation);
        stream.charge(ChargeKind::ForwardedFrame).unwrap();
        assert_eq!(
            stream.charge(ChargeKind::ForwardedFrame).unwrap_err(),
            BudgetError::Cost
        );
        assert_eq!(root.cost_units(), 1);
    }
    #[test]
    fn separate_top_level_execution_cannot_replace_an_inherited_ledger() {
        let first = root(2);
        let second = root(2);
        let (loop_context, _) = first.enter_loop(limits(1)).unwrap();
        assert!(loop_context.inherits(&first));
        assert!(!first.inherits(&loop_context));
        assert!(!second.inherits(&first));
        first.charge(ChargeKind::SubFlow).unwrap();
        assert_eq!(second.cost_units(), 0);
    }
    #[test]
    fn scope_count_is_bounded_and_deadlines_do_not_extend() {
        let root = root(100);
        let limits = LoopLimits {
            max_iterations: 3,
            max_wall_ms: 9_000,
            max_cost_units: 100,
        };
        let mut context = root;
        for _ in 0..MAX_ACTIVE_LOOP_SCOPES {
            context = context.enter_loop(limits.clone()).unwrap().0;
        }
        assert_eq!(context.active_scope_count(), MAX_ACTIVE_LOOP_SCOPES);
        assert_eq!(
            context.enter_loop(limits).unwrap_err(),
            BudgetError::ScopeDepth
        );
    }
}
