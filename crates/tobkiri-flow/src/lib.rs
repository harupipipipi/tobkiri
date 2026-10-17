//! The single effect-independent Flow interpreter.
//!
//! Every operation, including overlay operations, uses [`PortInvoker`]. Production
//! invokers must resolve the frozen binding, authorize, audit and dispatch through
//! the core's mandatory gate. This crate never creates an effect handle.

mod engine;
mod execution_frame;
mod expression;
mod journal;
mod owned_expression;
mod run_stream;
mod stream;

use async_trait::async_trait;
use std::any::Any;
use tobkiri_canon::{
    AllocationBudget, CoreBuffer, CoreValue, Digest,
    ids::{OpName, ShapeRef, UiSessionId},
};
use tobkiri_gate::{AllocationFrameScope, CallCx, CapacityReservation, PreparedFrameVerifier};
use tobkiri_profile::{CompiledCallWitness, CompiledInputEntry, FrozenTarget};

pub use execution_frame::{
    ActiveCodeFrame, FlowNodeKind, FlowNodePosition, FrameContinuation, OverlayPosition,
    OverlayTransition, ReachedNode,
};
pub use tobkiri_gate::AllocationReason;

pub use engine::{FlowEngine, PreparedRun, RunHandle, RunResult};
pub use expression::{Environment, evaluate};
pub use journal::{RunEvent, RunEventKind, RunJournal};
pub use owned_expression::{RuntimeEnvironment, evaluate_owned};
pub use stream::{CoreStream, StreamSource, StreamTerminal};

/// Stable errors returned by providers and by the Flow interpreter.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize, thiserror::Error)]
#[error("{code}: {message}")]
pub struct StepError {
    /// Machine-readable error code; guest traps are isolated as `trap`.
    pub code: String,
    /// Human-readable explanation, without secret arguments.
    pub message: String,
}

impl StepError {
    /// Construct an interpreter or provider error.
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }

    /// A closed code suitable for audit metadata. Guest-defined error strings
    /// can contain arbitrary payloads, so unknown codes are never persisted.
    pub fn audit_code(&self) -> &'static str {
        match self.code.as_str() {
            "denied" => "denied",
            "approval-required" => "approval-required",
            "cancelled" => "cancelled",
            "deadline" => "deadline",
            "not-found" => "not-found",
            "invalid" => "invalid",
            "limit-exceeded" => "limit-exceeded",
            "conflict" => "conflict",
            "unavailable" => "unavailable",
            "trap" => "trap",
            "blocked" => "blocked",
            _ => "internal",
        }
    }
}

/// A port operation returns either a value or a pull-based stream.
pub enum StepOutput {
    /// A complete result.
    Value(CoreValue),
    /// A stream with an isolated provider-local source.
    Stream(CoreStream),
}

/// Closed envelopes supported by Core's final-consumer encoder. The UI handle
/// is a bounded unpublished candidate minted by Session; an invoker may encode
/// it only if the admitted result is an actual stream for that same request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FinalOutputKind {
    /// Return the exact canonical StrictV1 provider value.
    CanonicalValue,
    /// Return the closed UI value/stream envelope using this Core candidate.
    UiInvokeValue { stream_handle: u64 },
    /// A trusted native User call, with a Session-reserved stream candidate.
    /// This is an output encoding only, not a UI attachment or authority.
    UserInvokeValue { stream_handle: u64 },
}

/// Result of a final-consumer port dispatch. A stream is returned together
/// with its already encoded envelope so Core can publish only the matching
/// pre-reserved handle after it owns the actual stream.
pub enum FinalPortOutput {
    /// A non-stream provider value encoded under the live provider frame.
    Value(CoreBuffer),
    /// The fixed UI stream envelope and its actual scoped provider stream.
    Stream {
        envelope: CoreBuffer,
        stream: CoreStream,
    },
}

/// Exact attachment-bound UI request metadata supplied by trusted Core. These
/// fields identify one received invocation; they do not choose its source
/// principal, target or allocation owner.
pub struct UiInvocation<'a> {
    pub invocation: &'a tobkiri_canon::Digest,
    pub session: &'a UiSessionId,
    pub generation: u64,
    pub receiver: &'a tobkiri_canon::Digest,
    pub view: &'a ShapeRef,
    pub op: &'a OpName,
    pub target: &'a FrozenTarget,
}

/// Core-private prepared UI argument phase. The factory binds this proof to
/// the actual received attachment and exact frozen provider endpoint before
/// input materialization. It is never a caller-selected sponsor.
pub trait UiInvocationPreparation: Send + Sync {
    fn argument_scope(&self) -> Result<AllocationFrameScope, StepError>;
}

/// Exact trusted-Core User request metadata for a received CLI/user action.
/// These fields identify one invocation and its already-resolved endpoint;
/// they do not choose a principal, approval, or allocation owner.
pub struct FlowUserInvocation<'a> {
    pub invocation: &'a Digest,
    pub view: &'a ShapeRef,
    pub op: &'a OpName,
    pub target: &'a FrozenTarget,
}

/// Core-private prepared User argument phase. Implementations must bind this
/// proof to the Session's registered User document/revision/epoch approval
/// and the exact resolved target before argument materialization.
pub trait FlowUserInvocationPreparation: Send + Sync {
    fn argument_scope(&self) -> Result<AllocationFrameScope, StepError>;
}

/// The mandatory dispatch boundary used by all Flow and overlay calls.
#[async_trait]
pub trait PortInvoker: Send + Sync {
    /// Transfer the verified, nonclone frame scope to the producer adapter.
    /// The adapter must retain the scope alongside the physical owner lease
    /// and recheck its live capacity on every reserve. An implementation that
    /// cannot do that remains unavailable.
    fn allocation_budget_for_frame(
        &self,
        _cx: &CallCx,
        _scope: AllocationFrameScope,
    ) -> Result<std::sync::Arc<dyn AllocationBudget>, StepError> {
        Err(StepError::new(
            "unavailable",
            "active-frame allocation funding is unavailable",
        ))
    }

    /// Fund Core's first owned frame/control allocation from the exact
    /// borrowed entry being prepared. The verifier is stack-only and must
    /// validate the same live entry that will become the owned frame. This
    /// callback is synchronous and fixed; implementations must not retain the
    /// verifier or call guest/effect code from it.
    fn reserve_frame_control(
        &self,
        _cx: &CallCx,
        _prepared: &dyn PreparedFrameVerifier,
        _reason: AllocationReason,
        _bytes: u64,
    ) -> Result<CapacityReservation, StepError> {
        Err(StepError::new(
            "unavailable",
            "first active-frame control reservation is unavailable",
        ))
    }

    /// Resolve, gate, audit and call a bound port operation.
    async fn call(
        &self,
        cx: &CallCx,
        shape: &ShapeRef,
        op: &OpName,
        args: CoreValue,
    ) -> Result<StepOutput, StepError>;

    /// Gate and emit an event to a declared output shape.
    async fn emit(&self, cx: &CallCx, output: &ShapeRef, event: CoreValue)
    -> Result<(), StepError>;

    /// Dispatch a direct final-consumer port call. The provider must be
    /// admitted and claimed once, then encode the StrictV1 result into a
    /// producer-funded CoreBuffer while its verified OutputBuffer activation
    /// is still live. No arbitrary callback may run between claim and finish.
    async fn call_final(
        &self,
        _cx: &CallCx,
        _shape: &ShapeRef,
        _op: &OpName,
        _output: FinalOutputKind,
        _args: CoreValue,
    ) -> Result<FinalPortOutput, StepError> {
        Err(StepError::new(
            "unavailable",
            "budgeted final port encoding is unavailable",
        ))
    }

    /// Dispatch an already received UI invocation under its exact Core-minted
    /// preparation. Implementations must not route this through Direct or
    /// accept a caller-selected source/target/funding owner.
    async fn call_ui_final(
        &self,
        _cx: &CallCx,
        _invocation: &UiInvocation<'_>,
        _preparation: &std::sync::Arc<dyn UiInvocationPreparation>,
        _arguments_digest: &Digest,
        _args: CoreValue,
        _output: FinalOutputKind,
    ) -> Result<FinalPortOutput, StepError> {
        Err(StepError::new(
            "unavailable",
            "received UI invocation dispatch is unavailable",
        ))
    }

    /// Dispatch a trusted Core User invocation through the same physical
    /// provider claim as every other port call. The prepared source proof and
    /// digest bind the arguments materialized under the explicit User policy;
    /// implementations must not use Direct or substitute a Pack sponsor.
    async fn call_user_final(
        &self,
        _cx: &CallCx,
        _invocation: &FlowUserInvocation<'_>,
        _preparation: &std::sync::Arc<dyn FlowUserInvocationPreparation>,
        _arguments_digest: &Digest,
        _args: CoreValue,
        _output: FinalOutputKind,
    ) -> Result<FinalPortOutput, StepError> {
        Err(StepError::new(
            "unavailable",
            "prepared User invocation dispatch is unavailable",
        ))
    }

    /// Dispatch a statically sealed node reached by the trusted interpreter.
    /// The Core adapter must mint the one-shot activation at this boundary;
    /// the reusable descriptor alone is never permission.
    async fn call_compiled(
        &self,
        _cx: &CallCx,
        _witness: &CompiledCallWitness,
        _args: CoreValue,
    ) -> Result<StepOutput, StepError> {
        Err(StepError::new(
            "unavailable",
            "Core compiled-call admission is unavailable",
        ))
    }

    /// Dispatch a reached compiled node through the Core-minted active frame.
    /// The frame proof binds this one interpreter activation to the frozen
    /// descriptor; a witness by itself is descriptive metadata only.
    async fn call_reached(
        &self,
        _cx: &CallCx,
        _frame: &tobkiri_canon::ControlHandle<dyn ActiveCodeFrame>,
        _node: &ReachedNode,
        _arguments_digest: &tobkiri_canon::Digest,
        _args: CoreValue,
    ) -> Result<StepOutput, StepError> {
        Err(StepError::new(
            "unavailable",
            "Core reached-node port dispatch is unavailable",
        ))
    }

    /// Forward a single frame through the frozen stream-emission call site.
    async fn emit_compiled(
        &self,
        _cx: &CallCx,
        _witness: &CompiledCallWitness,
        _event: CoreValue,
    ) -> Result<(), StepError> {
        Err(StepError::new(
            "unavailable",
            "Core compiled stream admission is unavailable",
        ))
    }

    /// Forward one reached compiled stream frame under a fresh activation.
    async fn emit_reached(
        &self,
        _cx: &CallCx,
        _frame: &tobkiri_canon::ControlHandle<dyn ActiveCodeFrame>,
        _node: &ReachedNode,
        _event_digest: &tobkiri_canon::Digest,
        _event: CoreValue,
    ) -> Result<(), StepError> {
        Err(StepError::new(
            "unavailable",
            "Core reached-node stream dispatch is unavailable",
        ))
    }

    /// Perform a physical Input source read through its sealed source-entry
    /// claim. Implementations validate the exact frozen source view/operation,
    /// fund the empty argument shell from that source, and return only the
    /// opaque output value after the final physical claim.
    async fn read_native_input(
        &self,
        _cx: &CallCx,
        _entry: &CompiledInputEntry,
    ) -> Result<(CoreValue, tobkiri_gate::SourceReadReceipt), StepError> {
        Err(StepError::new(
            "unavailable",
            "sealed native Input source admission is unavailable",
        ))
    }
}

/// The one compiled Flow runner used by native subflow steps and guest calls.
/// Core resolves in `cx.revision`, authorizes FlowRun and returns a run stream.
#[async_trait]
pub trait FlowRunner: Send + Sync {
    /// Run exactly one implementation in the caller's frozen compiled revision.
    /// Enter the exact compiled Flow target from the caller's active frame.
    /// Implementations consume one FlowRun grant, mint a separate callee frame,
    /// and preserve the caller's terminal responsibility for that grant.
    async fn run_provided_flow(
        &self,
        _cx: &CallCx,
        _caller: &tobkiri_canon::ControlHandle<dyn ActiveCodeFrame>,
        _node: &ReachedNode,
        _target: tobkiri_canon::ControlHandle<tobkiri_profile::ResolvedFlow>,
        _input: CoreValue,
    ) -> Result<CoreStream, StepError> {
        Err(StepError::new(
            "unavailable",
            "Core provided-Flow entry is unavailable",
        ))
    }

    /// Enter a canonical Flow by exact key in the Session's immutable
    /// `CompiledProfile`. The selected target and digest are metadata from the
    /// frozen call site. Core resolves the exact endpoint from its immutable
    /// Flow catalog and binds it to the consumed FlowRun permit.
    async fn run_provided_flow_canonical(
        &self,
        _cx: &CallCx,
        _caller: &tobkiri_canon::ControlHandle<dyn ActiveCodeFrame>,
        _node: &ReachedNode,
        _flow_key: &str,
        _args_digest: &tobkiri_canon::Digest,
        _input: CoreValue,
    ) -> Result<CoreStream, StepError> {
        Err(StepError::new(
            "unavailable",
            "Core canonical provided-Flow entry is unavailable",
        ))
    }

    /// Dynamically enter one Flow from an actual guest provider host call.
    /// The opaque provider frame is a Core-issued activation carrier; it is
    /// deliberately separate from AST `ReachedNode` evidence because this
    /// call originates in guest code rather than a compiled Flow step.
    async fn run_guest_flow(
        &self,
        _cx: &CallCx,
        _provider: &dyn GuestFlowFrame,
        _requested_shape: &ShapeRef,
        _arguments_digest: &Digest,
        _input: CoreValue,
    ) -> Result<CoreStream, StepError> {
        Err(StepError::new(
            "unavailable",
            "dynamic guest Flow entry is unavailable",
        ))
    }
}

/// Opaque proof of an actual active Wasm provider invocation. Implementations
/// are recognized only by the Core factory that created the concrete frame;
/// this trait does not make caller-supplied metadata authoritative.
#[doc(hidden)]
pub trait GuestFlowFrame: Any + Send + Sync {
    fn as_any(&self) -> &dyn Any;
}
