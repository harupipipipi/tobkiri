//! Core adapter installed into PackHost before any provider entry.
//!
//! The factory authority is a projection of the Session's actual execution
//! factory. It shares that factory's private verifier/catalog registrations,
//! while intentionally holding no PackHost or PortInvoker reference. The
//! native registration reservation bounds this one adapter for its lifetime;
//! guest-sized values continue to use their actual CodeOwner allocation scope.

use std::{mem::size_of, sync::Arc};

use tobkiri_canon::{ControlHandle, CoreValue};
use tobkiri_flow::{PortInvoker, StepError};
use tobkiri_gate::{
    CallCx, ControlKind, ControlReservation, CoreControl, DispatchPermit, FrozenPortTarget,
    GateError,
};
use tobkiri_profile::CompiledInputEntry;
use tobkiri_wasm::{
    HandleSourceRequest, NativeInputPreparation, ProviderEntryBridge, ProviderFrame,
    ProviderRequest,
};

use crate::execution_frames::CoreProviderAuthority;

/// A bounded, cycle-free bridge from PackHost to the Session's private frame
/// factory. `CoreProviderAuthority` shares the same factory identity and
/// registrations, but has no edge back to PackHost.
pub(crate) struct CoreProviderEntryBridge {
    authority: ControlHandle<CoreProviderAuthority>,
    _registration: ControlReservation,
}

impl CoreProviderEntryBridge {
    /// Reserve the single native registration before creating the shared Arc
    /// used by PackHost and Session. The account is CoreControl, never a guest
    /// CodeOwner budget or target-selected sponsor.
    pub(crate) fn new(
        authority: ControlHandle<CoreProviderAuthority>,
        control: &CoreControl,
    ) -> Result<Arc<dyn ProviderEntryBridge>, GateError> {
        // Include the concrete wrapper and conservative Arc allocation/control
        // overhead. This is a bounded native-control estimate, not an RSS or
        // guest-memory accounting claim.
        let bytes = size_of::<Self>()
            .checked_add(128)
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or(GateError::Unavailable)?;
        let registration = control.reserve(ControlKind::Registration, bytes)?;
        Ok(Arc::new(Self {
            authority,
            _registration: registration,
        }))
    }
}

impl ProviderEntryBridge for CoreProviderEntryBridge {
    fn prepare_native_input(
        &self,
        control: &dyn PortInvoker,
        caller: &CallCx,
        entry: &CompiledInputEntry,
        target: &FrozenPortTarget,
    ) -> Result<Arc<dyn NativeInputPreparation>, StepError> {
        self.authority.prepare_native_input_with_handle(
            self.authority.clone(),
            control,
            caller,
            entry,
            target,
        )
    }

    fn validate_prepare(&self, request: &ProviderRequest<'_>) -> Result<(), StepError> {
        self.authority.validate_prepare(request)
    }

    fn enter_provider(
        &self,
        request: &ProviderRequest<'_>,
        consumed: &DispatchPermit,
        actual_args: &CoreValue,
    ) -> Result<Arc<dyn ProviderFrame>, StepError> {
        self.authority.enter_provider_with_handle(
            self.authority.clone(),
            request,
            consumed,
            actual_args,
        )
    }

    fn enter_handle_source(
        &self,
        request: &HandleSourceRequest<'_>,
        consumed: &DispatchPermit,
    ) -> Result<Arc<dyn ProviderFrame>, StepError> {
        self.authority
            .enter_handle_source_with_handle(self.authority.clone(), request, consumed)
    }
}
