//! Private read-to-route custody. A numeric handle, opaque retained memory, or
//! an acknowledged parent read cannot independently authorize a source use.
use super::*;

pub(super) struct NativeInputForwarding {
    auth: ControlHandle<FactoryAuth>,
    original_input: CallCx,
    source: SourceReadMetadata,
    entry_index: usize,
    handles: ControlHandle<tobkiri_wasm::NativeInputHandles>,
}

impl CoreProviderFrame {
    pub(super) fn seal_input_forwarding(
        &self,
        handles: ControlHandle<tobkiri_wasm::NativeInputHandles>,
    ) -> Result<ControlHandle<dyn std::any::Any + Send + Sync>, StepError> {
        let ReachedOrigin::Provider(ProviderStateData {
            native: Some(native),
            grant,
            ..
        }) = &self.state.origin
        else {
            return Err(step(denied(
                "handle inventory is not an actual NativeInput read",
            )));
        };
        if !same_context_pair(handles.recipient(), &self.state.entry_context) {
            return Err(step(denied("Input inventory recipient changed")));
        }
        // Reservation precedes owned metadata copies. The inventory owns the
        // original source leases, not this provider frame or its retirement.
        let bytes = ControlHandle::<NativeInputForwarding>::storage_bytes::<ControlReservation>()
            .checked_add(
                native_source_metadata_storage_bytes(native, grant)
                    .ok_or_else(|| step(GateError::Unavailable))?,
            )
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or_else(|| step(GateError::Unavailable))?;
        let reservation = self
            .state
            .context
            .control()
            .reserve(ControlKind::Frame, bytes)
            .map_err(|error| step(GateError::from(error)))?;
        let _admission = self
            .state
            .auth
            .admission
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        let lifecycle = self
            .state
            .state
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if self.state.auth.closing.load(Ordering::Acquire)
            || !lifecycle.live
            || !lifecycle.claimed
            || lifecycle.acknowledged
            || lifecycle.output_digest.as_ref() != Some(handles.payload_digest())
        {
            return Err(step(denied(
                "Input inventory has no same live sealed output",
            )));
        }
        let source = SourceReadMetadata {
            source: native
                .source
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            view: native
                .view
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            op: native
                .op
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            target: clone_gate_target(&native.target).map_err(|_| step(GateError::Unavailable))?,
            payload: handles.payload_digest().clone(),
            read_grant: grant
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            frame: self.state.frame.clone(),
            activation: self.state.activation,
            path: self.state.path.clone(),
        };
        drop(lifecycle);
        drop(_admission);
        let custody = ControlHandle::try_new_precharged(
            NativeInputForwarding {
                auth: self.state.auth.clone(),
                original_input: self.state.entry_context.clone(),
                source,
                entry_index: native.index,
                handles,
            },
            reservation,
        )
        .map_err(|_| step(GateError::Unavailable))?;
        Ok(custody.project(|value| value as &(dyn std::any::Any + Send + Sync)))
    }
}

impl ExecutionFactory {
    pub(super) fn authenticate_input_forwarding(
        &self,
        custody: Option<&ControlHandle<dyn std::any::Any + Send + Sync>>,
        caller: &CallCx,
        receipt: &SourceReadReceipt,
        flow_key: &str,
    ) -> Result<Option<ControlHandle<NativeInputForwarding>>, StepError> {
        let Some(custody) = custody else {
            return Ok(None);
        };
        let custody = custody.try_project(|value| {
            value
                .downcast_ref::<NativeInputForwarding>()
                .ok_or_else(|| step(denied("Input custody is not issued by Core")))
        })?;
        let record = custody.get();
        self.validate_source_receipt(receipt).map_err(step)?;
        receipt.validate_context(caller).map_err(step)?;
        if !ControlHandle::ptr_eq(&record.auth, &self.provider.auth)
            || !same_context_pair(&record.original_input, caller)
            || &record.source != receipt.metadata()
            || record.handles.payload_digest() != &receipt.metadata().payload
        {
            return Err(step(denied(
                "Input custody differs from its original read receipt",
            )));
        }
        let entry = self
            .compiled
            .input_entries()
            .get(record.entry_index)
            .ok_or_else(|| step(denied("Input custody has no frozen source entry")))?;
        if entry.flow_routes().len() != 1
            || entry.flow_routes()[0] != flow_key
            || entry.source_pack() != &record.source.target.pack
            || entry.source_hash() != &record.source.target.hash
            || entry.exact_input_view() != &record.source.view
            || entry.operation() != &record.source.op
        {
            return Err(step(denied(
                "handle Input requires its one exact frozen route",
            )));
        }
        Ok(Some(custody))
    }
}

impl CoreProviderAuthority {
    pub(crate) fn native_input_argument(
        &self,
        provider: &Arc<dyn ProviderFrame>,
        kind: tobkiri_wasm::HandleKind,
        id: u64,
    ) -> Result<Option<ControlHandle<tobkiri_wasm::NativeInputHandles>>, StepError> {
        let key = Arc::as_ptr(provider) as *const () as usize;
        // Upgrade before admission: the strong frame outlives every guard and
        // prevents a final payload/retirement drop inside a registry lock.
        let frame = self
            .auth
            .live_provider_frames
            .lock()
            .map_err(|_| step(GateError::Unavailable))?
            .get(&key)
            .and_then(Weak::upgrade)
            .ok_or_else(|| step(denied("Input recipient is not a live Core provider")))?;
        let _admission = self
            .auth
            .admission
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        let lifecycle = frame
            .state
            .state
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if self.auth.closing.load(Ordering::Acquire)
            || !ControlHandle::ptr_eq(&self.auth, &frame.state.auth)
            || !lifecycle.live
            || !lifecycle.claimed
            || lifecycle.acknowledged
            || !same_context_pair(provider.context(), &frame.state.context)
        {
            return Err(step(denied(
                "Input recipient activation retired or changed",
            )));
        }
        let result = frame
            .state
            .input_forwarding
            .as_ref()
            .filter(|forwarding| forwarding.handles.contains(kind, id))
            .map(|forwarding| forwarding.handles.clone());
        drop(lifecycle);
        drop(_admission);
        Ok(result)
    }
}
