//! Native Core's prepared provider-entry seam. Preparation carries metadata;
//! only transfer of the same linear admitted dispatch can activate a provider.
use std::sync::Arc;
use tobkiri_canon::{
    AllocationBudget, ControlHandle, CoreValue, Digest,
    ids::{OpName, ShapeRef},
};
use tobkiri_flow::{ActiveCodeFrame, PortInvoker, ReachedNode, StepError};
pub use tobkiri_flow::{
    FlowUserInvocation, FlowUserInvocationPreparation, UiInvocation, UiInvocationPreparation,
};
use tobkiri_gate::{
    AcknowledgedTerminal, AllocationFrameScope, AllocationReason, CallCx, DispatchPermit,
    FrozenPortTarget, SourceReadReceipt,
};
use tobkiri_profile::CompiledInputEntry;

/// Closed origin of a physical dispatch. This is descriptive routing metadata,
/// never an authority, allocation sponsor or substitute for Core's live proof.
#[derive(Clone, Copy)]
pub enum ProviderOrigin<'a> {
    /// Core's direct call entry; its private factory must verify the actual entry.
    Direct,
    /// The interpreter reached this actual frozen node in Core's active frame.
    Reached {
        frame: &'a ControlHandle<dyn ActiveCodeFrame>,
        node: &'a ReachedNode,
    },
    /// The actual private Core source-read invocation. The exact frozen entry
    /// and its preparation are checked again at the one final physical claim.
    NativeInput {
        entry: &'a CompiledInputEntry,
        preparation: &'a Arc<dyn NativeInputPreparation>,
    },
    /// A dynamic call made by the actually executing provider. Its callee
    /// manifest consumes declaration remains mandatory; no caller witness is
    /// inherited or converted into another structural exception.
    Guest { frame: &'a Arc<dyn ProviderFrame> },
    /// Actual private UI request attachment; argument funding is bound to its
    /// verified ingress rather than selected from a provider label.
    UiInvocation {
        entry: &'a UiInvocation<'a>,
        preparation: &'a Arc<dyn UiInvocationPreparation>,
    },
    /// Actual Core User request. Its private preparation validates the User's
    /// current document/profile/revision/epoch and owns argument funding; a
    /// provider label or a synthetic Input source cannot replace this proof.
    UserInvocation {
        entry: &'a FlowUserInvocation<'a>,
        preparation: &'a Arc<dyn FlowUserInvocationPreparation>,
    },
}

/// Borrowed stack-only proposal used on both sides of the final claim. It
/// supplies no authority or funding and must not escape into an owned holder.
pub struct ProviderRequest<'a> {
    /// B supplies its actual allocator/dispatcher by borrow. Core may use only
    /// the synchronous prepared-control reservation and must not retain this
    /// reference in its bridge, authentication state or resulting frame.
    pub control: &'a dyn PortInvoker,
    pub caller: &'a CallCx,
    pub view: &'a ShapeRef,
    pub op: &'a OpName,
    pub target: &'a FrozenPortTarget,
    pub args_digest: &'a Digest,
    pub origin: ProviderOrigin<'a>,
}

/// Actual source-open transition for a returned scoped handle. This borrows
/// the one consumed StreamOpen/CallbackOpen permit; the handle lease retains
/// sole terminal custody. It is not another PortCall or per-pull grant.
pub struct HandleSourceRequest<'a> {
    pub control: &'a dyn PortInvoker,
    pub provider: &'a Arc<dyn ProviderFrame>,
    pub owner: &'a CallCx,
    pub kind: crate::HandleKind,
    pub local_id: u64,
    pub authority: &'a ShapeRef,
    pub generation: u64,
}

/// Injected by the same Core execution factory that authenticates Flow frames.
/// Preparation is borrowed and allocates no holder. The first owned frame and
/// control allocation belongs to bounded native CoreControl. Guest argument and
/// result allocations require the actual producing frame and physical owner;
/// a native registration reservation cannot sponsor a guest payload.
pub trait ProviderEntryBridge: Send + Sync {
    /// Prepare only the actual Core physical Input read. The implementation
    /// must validate the exact startup record, argument signature, source
    /// approval and target before exposing the producing source's argument
    /// scope. A descriptor or source label alone must not create this object.
    fn prepare_native_input(
        &self,
        control: &dyn PortInvoker,
        caller: &CallCx,
        entry: &CompiledInputEntry,
        target: &FrozenPortTarget,
    ) -> Result<Arc<dyn NativeInputPreparation>, StepError>;

    /// Freeze the proposed endpoint before final claim. This must mint no live
    /// allocation scope, change no principal and dispatch no guest/effect work.
    fn validate_prepare(&self, request: &ProviderRequest<'_>) -> Result<(), StepError>;

    /// Enter only after B's one final Gate claim. B's caller-local terminal
    /// keeps the raw permit. Core borrows it only during synchronous factory
    /// verification and retains descriptive evidence, never terminal custody.
    /// An entry failure leaves the caller able to finish that same grant.
    fn enter_provider(
        &self,
        request: &ProviderRequest<'_>,
        consumed: &DispatchPermit,
        actual_args: &CoreValue,
    ) -> Result<Arc<dyn ProviderFrame>, StepError>;

    /// Create the same factory's actual live source activation after its one
    /// source-open claim. This frame remains live through pull/invoke and
    /// proven cleanup; ACK retires it only when the source lease terminates.
    fn enter_handle_source(
        &self,
        _request: &HandleSourceRequest<'_>,
        _consumed: &DispatchPermit,
    ) -> Result<Arc<dyn ProviderFrame>, StepError> {
        Err(StepError::new(
            "unavailable",
            "Core handle source activation is not installed",
        ))
    }
}

/// Private source-entry state created by the same Core factory as provider
/// activation. This is neither a reusable sponsor nor PortCall admission.
pub trait NativeInputPreparation: Send + Sync {
    /// A live source-argument allocation node for this one physical read.
    fn argument_scope(&self) -> Result<AllocationFrameScope, StepError>;
}

/// Actual live provider activation. Its caller keeps the sole terminal owner;
/// this frame never settles a grant or owns the raw consumed DispatchPermit.
pub trait ProviderFrame: tobkiri_flow::GuestFlowFrame {
    /// Verified callee context, including its own approved execution limits.
    fn context(&self) -> &CallCx;

    /// Mint a reason-bound scope from this actual live provider node. Creating
    /// a scope after retirement, from a descriptor or from a caller label fails.
    fn allocation_scope(&self, reason: AllocationReason)
    -> Result<AllocationFrameScope, StepError>;

    /// B supplies the actual validated typed value before fixed final encoding.
    /// Core hashes that same borrowed value under its live OutputBuffer budget
    /// and seals completed-result evidence; it accepts no caller-chosen digest.
    fn output_ready(
        &self,
        result: &CoreValue,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<(), StepError>;

    /// The actual typed stream has been validated and registered by B. This
    /// opens only its bounded encoding node; it conveys no NativeInput read
    /// receipt and cannot be substituted for completed value evidence.
    fn stream_ready(&self) -> Result<(), StepError>;

    /// Validate the actual immutable Gate terminal/ACK and retire. This must
    /// check the same grant, effect, arguments and frozen entry, not trust a
    /// caller-supplied Outcome. No terminal proposal, consume or cost occurs.
    fn terminal_acknowledged(&self, delivery: &AcknowledgedTerminal) -> Result<(), StepError>;

    /// Claim the actual completed source-read output once, after a successful
    /// acknowledged physical terminal. Other origins and streams must fail.
    fn take_source_read_receipt(&self) -> Result<SourceReadReceipt, StepError>;
}
