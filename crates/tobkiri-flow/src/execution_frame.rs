//! Opaque Core-owned execution frames used by the pure Flow interpreter.
//!
//! These handles carry no authority by themselves. Core creates them only
//! after validating an entry, overlay, or resume transition and validates the
//! opaque proof again whenever Flow requests an allocation or dispatch.

use std::sync::Arc;

use tobkiri_canon::{AllocationBudget, ControlHandle, Digest};
use tobkiri_gate::{AllocationReason, CallCx, CompiledActivation};
use tobkiri_profile::{
    CodeLocation, CodeOwner, CompiledCallWitness, FrozenTarget, ResolvedFlow, ResolvedOverlay, Step,
};

use crate::StepError;

/// Which trusted interpreter boundary was reached in the immutable AST.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FlowNodeKind {
    /// Synthetic entry for preflight and start/completion metadata.
    FlowEntry,
    /// Actual completed Flow return, reached once before final encoding.
    FlowReturn,
    /// Entry into one verified wrapper document at an exact chain position.
    OverlayEntry,
    /// An interpreter lifecycle frame for this exact Flow or AST step.
    EventFrame,
    /// The captured base node resumed by an overlay's `next` continuation.
    ResumeBase,
    /// A non-effect AST step.
    Step,
    /// A physical call step with its compiler witness.
    PortCall,
    /// A FlowRun step with its exact compiled callee.
    ProvidedFlow,
    /// A physical forwarded frame from a witnessed stream-emission node.
    StreamEmit,
}

/// Borrowed, frozen interpreter location. Passing the actual AST references
/// lets Core reject caller-supplied node strings and cloned lookalikes.
pub struct FlowNodePosition<'a> {
    pub flow: &'a ResolvedFlow,
    pub code: &'a CodeLocation,
    pub step: Option<&'a Step>,
    pub kind: FlowNodeKind,
    pub witness: Option<&'a CompiledCallWitness>,
}

/// Exact point in a frozen ordered overlay chain.
pub struct OverlayPosition<'a> {
    pub flow: &'a ResolvedFlow,
    pub step: &'a Step,
    pub index: usize,
    pub overlay: &'a ResolvedOverlay,
}

/// Core-issued proof that this exact node activation was reached.
///
/// The payload is private to Core. A descriptor getter exposes metadata only;
/// Gate admission still needs an activation minted from the live frame proof.
#[derive(Clone)]
pub struct ReachedNode {
    proof: ControlHandle<dyn std::any::Any + Send + Sync>,
    // Kept separate from the Gate-visible proof: when the last interpreter
    // carrier drops, this synchronously retires the node while retained values
    // and allocation scopes keep only their immutable proof/custody alive.
    _retirement: ControlHandle<dyn std::any::Any + Send + Sync>,
    witness: Option<ControlHandle<CompiledCallWitness>>,
}
impl ReachedNode {
    /// Construct a carrier from Core-private, already prepaid activation state.
    #[doc(hidden)]
    pub fn from_prepaid(
        proof: ControlHandle<dyn std::any::Any + Send + Sync>,
        retirement: ControlHandle<dyn std::any::Any + Send + Sync>,
        witness: Option<ControlHandle<CompiledCallWitness>>,
    ) -> Self {
        Self {
            proof,
            _retirement: retirement,
            witness,
        }
    }

    /// Read the immutable compiler descriptor associated with this activation.
    pub fn witness(&self) -> Option<&CompiledCallWitness> {
        self.witness.as_ref().map(ControlHandle::get)
    }

    /// Give Core's registered frame verifier the same opaque proof that was
    /// minted for this actual reached activation. The token carries no public
    /// metadata and cannot be reconstructed from a witness or node identifier.
    #[doc(hidden)]
    pub fn core_token(&self) -> ControlHandle<dyn std::any::Any + Send + Sync> {
        self.proof.clone()
    }

    /// Downcast only inside the Core adapter that minted this proof.
    #[doc(hidden)]
    pub fn core_proof<T: std::any::Any>(&self) -> Option<&T> {
        self.proof.downcast_ref::<T>()
    }
}

/// Opaque Core-owned continuation saved when an overlay frame is entered.
#[derive(Clone)]
pub struct FrameContinuation {
    proof: ControlHandle<dyn std::any::Any + Send + Sync>,
}
impl FrameContinuation {
    /// Construct a carrier from Core-private, already prepaid continuation state.
    #[doc(hidden)]
    pub fn from_prepaid(proof: ControlHandle<dyn std::any::Any + Send + Sync>) -> Self {
        Self { proof }
    }

    /// Downcast only inside the Core adapter that minted this continuation.
    #[doc(hidden)]
    pub fn core_proof<T: std::any::Any>(&self) -> Option<&T> {
        self.proof.downcast_ref::<T>()
    }
}

/// A verified overlay frame and the captured base continuation paired by Core.
pub struct OverlayTransition {
    pub frame: ControlHandle<dyn ActiveCodeFrame>,
    pub continuation: FrameContinuation,
}

/// Core-private live context for one active Flow/code-owner transition.
///
/// Implementations retain the actual entry or overlay provenance and verify
/// every node token before returning budgets or compiled dispatch activations.
pub trait ActiveCodeFrame: Send + Sync {
    /// Current effective authority context; code-owner funding is independent.
    fn context(&self) -> &CallCx;

    /// Actual verified producer of allocations for this frame.
    fn code_owner(&self) -> &CodeOwner;

    /// Lifetime-only child custody inherited from the actual private frame.
    /// This metadata grants neither an entry nor an allocation capability.
    fn task_spawner(&self) -> Option<Arc<dyn crate::RunTaskSpawner>> {
        None
    }

    fn task_execution(&self) -> Option<crate::RunTaskExecution> {
        None
    }

    /// Mint a fresh activation only after verifying the borrowed frozen AST node.
    fn reach_node(
        &self,
        cx: &CallCx,
        position: FlowNodePosition<'_>,
    ) -> Result<ReachedNode, StepError>;

    /// Obtain the one reason-bound owner budget for a reached activation.
    fn budget_at_reached_node(
        &self,
        cx: &CallCx,
        node: &ReachedNode,
        reason: AllocationReason,
    ) -> Result<Arc<dyn AllocationBudget>, StepError>;

    /// Retire a node's capability for new work after the interpreter leaves it.
    /// Existing values keep their physical leases; a stale token cannot fund
    /// further allocations or dispatches.
    fn retire_node(&self, _node: &ReachedNode) {}

    /// Mint a Gate activation from this frame and reached descriptor.
    fn compiled_activation(
        &self,
        cx: &CallCx,
        node: &ReachedNode,
        arguments_digest: &Digest,
        target: &FrozenTarget,
    ) -> Result<CompiledActivation, StepError>;

    /// Enter the next verified overlay position without a new grant or cost.
    fn enter_verified_overlay(
        &self,
        cx: &CallCx,
        caller_node: &ReachedNode,
        position: OverlayPosition<'_>,
    ) -> Result<OverlayTransition, StepError>;

    /// Restore the captured base authority and constraints exactly once.
    fn resume_captured_base(
        &self,
        cx: &CallCx,
        overlay_node: &ReachedNode,
        continuation: &FrameContinuation,
    ) -> Result<ControlHandle<dyn ActiveCodeFrame>, StepError>;

    /// Stable Core identity for binding independent proofs to this frame.
    #[doc(hidden)]
    fn identity(&self) -> Digest;
}
