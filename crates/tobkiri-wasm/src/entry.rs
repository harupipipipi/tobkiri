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

    /// Resolve a physical Input argument only from this actual live provider
    /// activation and Core's private sealed source/route provenance. Numeric
    /// membership without that provenance must never supply an inventory.
    fn native_input_argument(
        &self,
        _provider: &Arc<dyn ProviderFrame>,
        _kind: crate::HandleKind,
        _id: u64,
    ) -> Result<Option<ControlHandle<crate::NativeInputHandles>>, StepError> {
        Ok(None)
    }

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

/// Borrowed actual Core lifecycle state, held coherent through selection.
#[derive(Clone, Copy)]
pub struct CleanupLifecycle {
    pub live: bool,
    pub closing: bool,
    pub requested: Option<tobkiri_flow::StreamCloseReason>,
}

/// Result of one synchronous lifecycle inspection. `Selected` means only that
/// its callback completed normally; it does not prove native cause selection,
/// grant authority, physical cleanup or terminal delivery.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CleanupLifecycleAttempt {
    Selected,
    /// The concrete owner authenticated its own mutex contention before any
    /// callback ran. A generic error or callback failure cannot supply this.
    RetryableBusyBeforeCallback,
    Uncertain(StepError),
}

/// Typed result of one synchronous native-identity attempt. `Busy` is only
/// the concrete owner's authenticated pre-publication mutex contention; a
/// legacy implementation can never derive it from an error code or message.
#[derive(Clone)]
pub enum NativeIdAttempt {
    Ready(crate::NativeExecutionId),
    Busy,
    Uncertain(StepError),
}

/// Typed result of one bind attempt. `Busy` returns the unchanged funded
/// candidate from authenticated pre-commit contention so the same scope can
/// be retried; every other failure consumes it.
pub enum ProviderBindAttempt {
    Bound,
    Busy(crate::ProviderOperationScope),
    Uncertain(StepError),
}

fn legacy_cleanup_lifecycle_attempt(
    invoke: impl FnOnce(
        &mut dyn FnMut(CleanupLifecycle) -> Result<(), StepError>,
    ) -> Result<(), StepError>,
    select: &mut dyn FnMut(CleanupLifecycle) -> Result<(), StepError>,
) -> CleanupLifecycleAttempt {
    let mut called = false;
    let mut repeated = false;
    let mut callback_error = None;
    let result = invoke(&mut |lifecycle| {
        if called {
            repeated = true;
            return Err(StepError::new(
                "unavailable",
                "cleanup lifecycle callback repeated",
            ));
        }
        called = true;
        let result = select(lifecycle);
        if let Err(error) = &result {
            callback_error = Some(error.clone());
        }
        result
    });
    match result {
        Err(error) => CleanupLifecycleAttempt::Uncertain(error),
        Ok(()) if repeated => CleanupLifecycleAttempt::Uncertain(StepError::new(
            "unavailable",
            "cleanup lifecycle callback repeated",
        )),
        Ok(()) if callback_error.is_some() => {
            CleanupLifecycleAttempt::Uncertain(callback_error.expect("known callback error"))
        }
        Ok(()) if called => CleanupLifecycleAttempt::Selected,
        Ok(()) => CleanupLifecycleAttempt::Uncertain(StepError::new(
            "unavailable",
            "cleanup lifecycle callback was not invoked",
        )),
    }
}

/// Actual live provider activation. Its caller keeps the sole terminal owner;
/// this frame never settles a grant or owns the raw consumed DispatchPermit.
pub trait ProviderFrame: tobkiri_flow::GuestFlowFrame {
    /// Host-minted lifetime inventory for this actual provider operation.
    /// Binding grants neither an effect nor an allocation capability.
    fn bind_provider_operation(
        &self,
        _scope: crate::ProviderOperationScope,
    ) -> Result<(), StepError> {
        Err(StepError::new(
            "unavailable",
            "provider operation binding unavailable",
        ))
    }
    /// Attempt the same guarded bind with the unchanged candidate returned on
    /// authenticated own pre-commit contention. Legacy results never classify
    /// Busy: success binds and every error is Uncertain.
    fn try_bind_provider_operation(
        &self,
        scope: crate::ProviderOperationScope,
    ) -> ProviderBindAttempt {
        match self.bind_provider_operation(scope) {
            Ok(()) => ProviderBindAttempt::Bound,
            Err(error) => ProviderBindAttempt::Uncertain(error),
        }
    }
    fn provider_operation(&self) -> Result<crate::ProviderOperationScope, StepError> {
        Err(StepError::new(
            "unavailable",
            "provider operation scope unavailable",
        ))
    }
    /// Only the actual Core origin may supply an already bound parent. This
    /// getter is lifetime metadata, never a substitute for entry verification.
    fn provider_operation_parent(&self) -> Option<crate::ProviderOperationScope> {
        None
    }
    /// Original private Core activation identity, for native lifetime custody
    /// only. Unsupported/foreign frame implementations fail closed.
    fn native_execution_id(&self) -> Result<crate::NativeExecutionId, StepError> {
        Err(StepError::new(
            "unavailable",
            "Core native execution identity unavailable",
        ))
    }
    /// Attempt the same private identity derivation with authenticated own
    /// pre-publication contention distinguished. Legacy results never
    /// classify Busy: success is Ready and every error is Uncertain.
    fn try_native_execution_id(&self) -> NativeIdAttempt {
        match self.native_execution_id() {
            Ok(id) => NativeIdAttempt::Ready(id),
            Err(error) => NativeIdAttempt::Uncertain(error),
        }
    }
    /// Clone only the already bound exact source companion. A normal provider
    /// frame has none; this creates no source or cleanup authority.
    fn cleanup_control(&self) -> Option<Arc<dyn tobkiri_flow::StreamCloseControl>> {
        None
    }

    /// Hold the exact factory admission and source-node lifecycle guards while
    /// native B takes generation/cause guards and observes Gate's snapshot.
    fn with_cleanup_lifecycle(
        &self,
        _select: &mut dyn FnMut(CleanupLifecycle) -> Result<(), StepError>,
    ) -> Result<(), StepError> {
        Err(StepError::new(
            "unavailable",
            "Core cleanup lifecycle is unavailable",
        ))
    }

    /// Attempt the same guarded inspection with authenticated pre-callback
    /// contention distinguished from uncertainty. Legacy implementations never
    /// produce retry permission from an error code, even before their callback.
    fn try_cleanup_lifecycle(
        &self,
        select: &mut dyn FnMut(CleanupLifecycle) -> Result<(), StepError>,
    ) -> CleanupLifecycleAttempt {
        legacy_cleanup_lifecycle_attempt(|checked| self.with_cleanup_lifecycle(checked), select)
    }

    /// Attach before publishing or starting the reaper. A previously requested
    /// execution close is synchronously applied before this method returns.
    fn bind_cleanup_control(
        &self,
        _control: &Arc<dyn tobkiri_flow::StreamCloseControl>,
    ) -> Result<(), StepError> {
        Err(StepError::new(
            "unavailable",
            "Core cleanup registration is unavailable",
        ))
    }

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

    /// Bind the actual typed Input inventory to this private source activation
    /// and its sealed output. The opaque result conveys no authority alone;
    /// Core must verify the original successful read receipt before attaching
    /// it to the actual frozen one-route Flow activation.
    fn seal_native_input_handles(
        &self,
        _handles: ControlHandle<crate::NativeInputHandles>,
    ) -> Result<ControlHandle<dyn std::any::Any + Send + Sync>, StepError> {
        Err(StepError::new(
            "unavailable",
            "native Input forwarding is unavailable",
        ))
    }

    /// Claim the actual completed source-read output once, after a successful
    /// acknowledged physical terminal. Other origins and streams must fail.
    fn take_source_read_receipt(&self) -> Result<SourceReadReceipt, StepError>;
}

#[cfg(test)]
mod cleanup_lifecycle_tests {
    use super::*;

    fn lifecycle() -> CleanupLifecycle {
        CleanupLifecycle {
            live: true,
            closing: false,
            requested: None,
        }
    }

    #[test]
    fn legacy_error_before_callback_never_authenticates_contention() {
        let error = StepError::new("busy", "legacy failure with no lock evidence");
        let mut calls = 0;
        let attempt = legacy_cleanup_lifecycle_attempt(|_| Err(error.clone()), &mut |_| {
            calls += 1;
            Ok(())
        });
        assert_eq!(attempt, CleanupLifecycleAttempt::Uncertain(error));
        assert_eq!(calls, 0);
    }

    #[test]
    fn legacy_success_without_callback_is_uncertain() {
        let attempt = legacy_cleanup_lifecycle_attempt(|_| Ok(()), &mut |_| {
            panic!("legacy implementation did not invoke its callback")
        });
        assert!(matches!(attempt, CleanupLifecycleAttempt::Uncertain(_)));
    }

    #[test]
    fn successful_callback_is_selected_without_asserting_a_cause() {
        let mut calls = 0;
        let attempt = legacy_cleanup_lifecycle_attempt(|select| select(lifecycle()), &mut |_| {
            calls += 1;
            Ok(())
        });
        assert_eq!(attempt, CleanupLifecycleAttempt::Selected);
        assert_eq!(calls, 1);
    }

    #[test]
    fn repeated_legacy_callback_cannot_repeat_the_actual_selection() {
        let mut calls = 0;
        let attempt = legacy_cleanup_lifecycle_attempt(
            |select| {
                select(lifecycle())?;
                let _ = select(lifecycle());
                Ok(())
            },
            &mut |_| {
                calls += 1;
                Ok(())
            },
        );
        assert!(matches!(attempt, CleanupLifecycleAttempt::Uncertain(_)));
        assert_eq!(calls, 1);
    }

    #[test]
    fn ignored_callback_error_is_still_uncertain() {
        let error = StepError::new("busy", "callback has no Core lock provenance");
        let attempt = legacy_cleanup_lifecycle_attempt(
            |select| {
                let _ = select(lifecycle());
                Ok(())
            },
            &mut |_| Err(error.clone()),
        );
        assert_eq!(attempt, CleanupLifecycleAttempt::Uncertain(error));
    }

    #[test]
    fn outer_error_after_normal_callback_takes_priority() {
        let error = StepError::new("unavailable", "legacy inspection failed after callback");
        let mut calls = 0;
        let attempt = legacy_cleanup_lifecycle_attempt(
            |select| {
                select(lifecycle())?;
                Err(error.clone())
            },
            &mut |_| {
                calls += 1;
                Ok(())
            },
        );
        assert_eq!(attempt, CleanupLifecycleAttempt::Uncertain(error));
        assert_eq!(calls, 1);
    }
}
