//! One physical dispatch claim, producing-owner typed conversion, and fixed
//! final-consumer encoding. Native control handles never sponsor guest graphs.
use anyhow::{Context, Result, bail};
use std::sync::Arc;
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, ControlHandle, CoreValue, Digest,
    ids::{OpName, ShapeRef},
};
use tobkiri_flow::{
    ActiveCodeFrame, FinalOutputKind, FinalPortOutput, ReachedNode, StepError, StepOutput,
};
use tobkiri_gate::{
    AcknowledgedTerminal, AllocationReason, CallCx, DispatchPermit, EffectRequest,
    FrozenPortTarget, Gate, GateError, Outcome, PortAdmission, Principal, SourceReadReceipt,
};
use tobkiri_manifest::{WitBindingDirection, WitFunction, WitType};
use tobkiri_profile::{CallNodeKind, CompiledInputEntry};
use wasmtime::component::{BudgetedVec, BudgetedVecBuilder, Val};

use crate::{
    ProviderFrame, ProviderOrigin, ProviderRequest,
    host::{PackHost, interface_name, step_error},
    owned_values::{ValueOwners, core_to_val, project_val_owned, val_to_core},
};

impl PackHost {
    pub(crate) async fn call_reached_owned(
        &self,
        cx: &CallCx,
        frame: &ControlHandle<dyn ActiveCodeFrame>,
        node: &ReachedNode,
        arguments_digest: &Digest,
        args: CoreValue,
    ) -> Result<StepOutput, StepError> {
        self.dispatch_reached_owned(cx, frame, node, arguments_digest, args, CallNodeKind::Call)
            .await
    }

    pub(crate) async fn emit_reached_owned(
        &self,
        cx: &CallCx,
        frame: &ControlHandle<dyn ActiveCodeFrame>,
        node: &ReachedNode,
        event_digest: &Digest,
        event: CoreValue,
    ) -> Result<(), StepError> {
        let witness = node
            .witness()
            .filter(|witness| witness.key().kind == CallNodeKind::StreamEmit)
            .ok_or_else(|| StepError::new("denied", "stream emission has no reached witness"))?;
        let function = self
            .shared
            .views
            .get(witness.shape())
            .and_then(|shape| shape.functions.get(witness.op()))
            .ok_or_else(|| {
                StepError::new(
                    "denied",
                    "stream emission operation is outside its exact view",
                )
            })?;
        let [(parameter, _)] = function.params.as_slice() else {
            return Err(StepError::new(
                "invalid",
                "stream emission requires one declared parameter",
            ));
        };
        let budget = frame.budget_at_reached_node(cx, node, AllocationReason::RuntimeValue)?;
        let actual_event = event
            .digest_guest(&budget)
            .map_err(|error| step_error(error.into()))?;
        if &actual_event != event_digest {
            return Err(StepError::new(
                "denied",
                "stream emission digest differs from its event",
            ));
        }
        // The new argument container belongs to this actual emitter node;
        // the event keeps its original producer's physical graph leases.
        let args =
            CoreValue::object_shared(std::iter::once(Ok((parameter.as_str(), event))), &budget)
                .map_err(|error| step_error(error.into()))?;
        let arguments_digest = args
            .digest_guest(&budget)
            .map_err(|error| step_error(error.into()))?;
        match self
            .dispatch_reached_owned(
                cx,
                frame,
                node,
                &arguments_digest,
                args,
                CallNodeKind::StreamEmit,
            )
            .await?
        {
            StepOutput::Value(_) => Ok(()),
            StepOutput::Stream(mut stream) => {
                stream.close().await?;
                Err(StepError::new(
                    "invalid",
                    "stream emission returned another stream",
                ))
            }
        }
    }

    async fn dispatch_reached_owned(
        &self,
        cx: &CallCx,
        frame: &ControlHandle<dyn ActiveCodeFrame>,
        node: &ReachedNode,
        arguments_digest: &Digest,
        args: CoreValue,
        kind: CallNodeKind,
    ) -> Result<StepOutput, StepError> {
        let witness = node
            .witness()
            .filter(|witness| witness.key().kind == kind)
            .ok_or_else(|| {
                StepError::new("denied", "port dispatch has no matching reached witness")
            })?;
        let (pack, provided, _) = self
            .resolve_for_admission(cx, witness.shape(), witness.op(), false)
            .map_err(step_error)?;
        let target = witness
            .targets()
            .iter()
            .find(|target| {
                target.pack == pack.manifest.id
                    && target.hash == pack.hash
                    && target.provided == provided.reference
            })
            .ok_or_else(|| {
                StepError::new(
                    "denied",
                    "actual provider is outside the frozen call target",
                )
            })?;
        // Core authenticates the live reached activation, not merely these
        // descriptor getters. The same one-shot proof survives final claim.
        let activation = frame.compiled_activation(cx, node, arguments_digest, target)?;
        let admission = self
            .shared
            .gate
            .admit_compiled(cx, activation, arguments_digest.clone())
            .await
            .map_err(|error| step_error(error.into()))?;
        self.dispatch_admitted(cx, admission, args, ProviderOrigin::Reached { frame, node })
            .await
    }

    pub(crate) async fn call_guest_owned(
        &self,
        cx: &CallCx,
        shape: &ShapeRef,
        op: &OpName,
        args: CoreValue,
        frame: &Arc<dyn ProviderFrame>,
    ) -> Result<StepOutput> {
        let admission = self.admit_owned_arguments(cx, shape, op, &args).await?;
        self.dispatch_admitted(cx, admission, args, ProviderOrigin::Guest { frame })
            .await
            .map_err(anyhow::Error::new)
    }
    pub(crate) async fn call_owned(
        &self,
        cx: &CallCx,
        shape: &ShapeRef,
        op: &OpName,
        args: CoreValue,
    ) -> Result<StepOutput, StepError> {
        let admission = self
            .admit_owned_arguments(cx, shape, op, &args)
            .await
            .map_err(step_error)?;
        self.dispatch_admitted(cx, admission, args, ProviderOrigin::Direct)
            .await
    }

    pub(crate) async fn call_owned_final(
        &self,
        cx: &CallCx,
        shape: &ShapeRef,
        op: &OpName,
        output: FinalOutputKind,
        args: CoreValue,
    ) -> Result<FinalPortOutput, StepError> {
        let admission = self
            .admit_owned_arguments(cx, shape, op, &args)
            .await
            .map_err(step_error)?;
        self.dispatch_admitted_final(cx, admission, args, ProviderOrigin::Direct, output)
            .await
    }

    pub(crate) async fn admit_owned_arguments(
        &self,
        cx: &CallCx,
        shape: &ShapeRef,
        op: &OpName,
        args: &CoreValue,
    ) -> Result<PortAdmission> {
        self.shared.gate.check_context(cx)?;
        let (pack, provided, _) = self.resolve_for_admission(cx, shape, op, true)?;
        self.shared
            .views
            .get(shape)
            .and_then(|view| view.functions.get(op))
            .context("operation outside exact frozen caller view")?;
        // This traversal allocates no guest storage. A closed zero-byte work
        // meter cannot sponsor a CoreValue/CoreBuffer/ABI allocation; existing
        // immutable graph owners remain retained throughout the hash. Actual
        // provider entry independently rehashes under its live producer scope.
        let work: Arc<dyn AllocationBudget> = Arc::new(DigestWork(
            std::sync::atomic::AtomicU64::new(cx.limits().fuel_per_call),
        ));
        let digest = args.digest_guest(&work)?;
        let target = FrozenPortTarget {
            pack: pack.manifest.id.clone(),
            hash: pack.hash.clone(),
            provided: provided.reference,
        };
        Ok(self
            .shared
            .gate
            .admit_port(cx, shape.clone(), op.clone(), digest, target)
            .await?)
    }
    pub(crate) async fn invoke_values_owned(
        &self,
        cx: &CallCx,
        shape: &ShapeRef,
        op: &OpName,
        params: &[Val],
        function: &WitFunction,
        owners: &ValueOwners,
        frame: &Arc<dyn ProviderFrame>,
    ) -> Result<BudgetedVec<Val>> {
        if self
            .shared
            .views
            .get(shape)
            .and_then(|view| view.functions.get(op))
            != Some(function)
            || params.len() != function.params.len()
        {
            bail!("typed proxy differs from exact frozen caller view")
        }
        let args = CoreValue::object_from(
            function
                .params
                .iter()
                .zip(params)
                .map(|((name, ty), value)| {
                    Ok((
                        name.as_str(),
                        val_to_core(value, ty, owners).map_err(|_| AllocationError::Invalid)?,
                    ))
                }),
            owners.graph(),
        )?;
        let admission = self.admit_owned_arguments(cx, shape, op, &args).await?;
        match self
            .dispatch_owned(
                cx,
                admission,
                args,
                ProviderOrigin::Guest { frame },
                None,
                true,
            )
            .await?
        {
            DispatchOutput::Typed { values, .. } => Ok(values),
            _ => unreachable!("typed proxy dispatch mode"),
        }
    }

    /// Consume the SAME admission supplied by Core's actual direct/reached
    /// entry. A reusable witness or an argument owner label is not admission.
    pub async fn dispatch_admitted(
        &self,
        cx: &CallCx,
        admission: PortAdmission,
        args: CoreValue,
        origin: ProviderOrigin<'_>,
    ) -> Result<StepOutput, StepError> {
        self.dispatch_owned(cx, admission, args, origin, None, false)
            .await
            .map(|value| match value {
                DispatchOutput::Value(value) => StepOutput::Value(value),
                DispatchOutput::Stream(stream) => StepOutput::Stream(stream),
                DispatchOutput::Final(_) | DispatchOutput::Typed { .. } => {
                    unreachable!("no final encoder requested")
                }
            })
            .map_err(step_error)
    }

    /// Encode before the actual provider activation/terminal retires. A Session
    /// stream candidate remains unpublished until the returned stream exists.
    pub async fn dispatch_admitted_final(
        &self,
        cx: &CallCx,
        admission: PortAdmission,
        args: CoreValue,
        origin: ProviderOrigin<'_>,
        output: FinalOutputKind,
    ) -> Result<FinalPortOutput, StepError> {
        match self
            .dispatch_owned(cx, admission, args, origin, Some(output), false)
            .await
            .map_err(step_error)?
        {
            DispatchOutput::Final(value) => Ok(value),
            DispatchOutput::Value(_) | DispatchOutput::Stream(_) | DispatchOutput::Typed { .. } => {
                unreachable!("fixed final encoder requested")
            }
        }
    }

    async fn dispatch_owned(
        &self,
        cx: &CallCx,
        admission: PortAdmission,
        args: CoreValue,
        origin: ProviderOrigin<'_>,
        final_output: Option<FinalOutputKind>,
        typed: bool,
    ) -> Result<DispatchOutput> {
        let result = async {
            self.shared.gate.check_context(cx)?;
            let (view, op) = match admission.effect() {
                EffectRequest::PortCall { shape, op } => (shape.clone(), op.clone()),
                _ => bail!("physical port admission effect mismatch"),
            };
            let (pack, provided, provider_function) =
                self.resolve_for_admission(cx, &view, &op, admission.requires_dynamic_consumes())?;
            let caller_function = self
                .shared
                .views
                .get(&view)
                .and_then(|shape| shape.functions.get(&op))
                .context("operation outside the exact frozen caller view")?;
            let target = FrozenPortTarget {
                pack: pack.manifest.id.clone(),
                hash: pack.hash.clone(),
                provided: provided.reference.clone(),
            };
            if admission.target() != &target {
                bail!("admission does not select this frozen provider")
            }
            let digest = admission.args_digest().clone();
            let bridge = self
                .shared
                .provider_entry
                .get()
                .context("Core provider-entry bridge not installed")?
                .clone();
            let request = ProviderRequest {
                control: self,
                caller: cx,
                view: &view,
                op: &op,
                target: &target,
                args_digest: &digest,
                origin,
            };
            bridge
                .validate_prepare(&request)
                .map_err(anyhow::Error::new)?;
            // The original unconsumed Grant reaches exactly one final claim and
            // atomic dispatch cost. Provider entry cannot mint another PortCall.
            let dispatch = self
                .shared
                .gate
                .claim_port(cx, admission, &view, &op, &digest, &target)
                .await?;
            let mut terminal =
                ConsumedPortTerminal::new(dispatch.into_permit(), self.shared.gate.clone());
            if let Err(error) = terminal.watch(cx) {
                terminal
                    .finish(Outcome::Failed {
                        code: "lease-supervision".into(),
                    })
                    .await?;
                return Err(error.into());
            }
            let frame = match bridge.enter_provider(&request, terminal.permit(), &args) {
                Ok(frame) => frame,
                Err(error) => {
                    terminal
                        .finish(Outcome::Failed {
                            code: "provider-entry".into(),
                        })
                        .await?;
                    return Err(anyhow::Error::new(error));
                }
            };
            // Only a successfully authenticated actual origin selects whether the
            // caller is Core's canonical view or an executing guest's import WIT.
            let nominal_core_error = self.nominal_core_error(cx, &view, &target, &op, origin);
            if let Err(error) = terminal.bind_execution_deadline(cx, frame.context()).await {
                let receipt = terminal
                    .finish(Outcome::Failed {
                        code: "lease-narrowing".into(),
                    })
                    .await?;
                frame
                    .terminal_acknowledged(&receipt)
                    .map_err(anyhow::Error::new)?;
                return Err(error.into());
            }
            let result = self
                .invoke_owned_frame(
                    cx,
                    &frame,
                    &target,
                    &provided.reference,
                    &op,
                    caller_function,
                    &provider_function,
                    &args,
                    &digest,
                    final_output,
                    typed,
                    nominal_core_error,
                )
                .await;
            let outcome = match &result {
                Ok(DispatchOutput::Typed { error: true, .. }) => Outcome::Failed {
                    code: "provider-error".into(),
                },
                Ok(_) => Outcome::Succeeded,
                Err(error) if crate::core_imports::storage_failure_metadata(error).is_some() => {
                    Outcome::Failed {
                        code: crate::core_imports::storage_failure_metadata(error)
                            .expect("matched closed native failure")
                            .0
                            .into(),
                    }
                }
                Err(_) if std::time::Instant::now() >= frame.context().deadline() => {
                    Outcome::Failed {
                        code: "deadline-failure-unknown".into(),
                    }
                }
                Err(_) if frame.context().cancel().is_cancelled() => Outcome::Cancelled,
                Err(error) => Outcome::Failed {
                    code: step_error(anyhow::anyhow!(error.to_string()))
                        .audit_code()
                        .into(),
                },
            };
            // Do not borrow a result containing CoreStream across the terminal
            // await: the stream is Send and remains owned here, not Sync.
            let successful_output = result
                .as_ref()
                .is_ok_and(|value| !matches!(value, DispatchOutput::Typed { error: true, .. }));
            let delivery = async {
                let receipt = terminal.finish(outcome).await?;
                frame
                    .terminal_acknowledged(&receipt)
                    .map_err(anyhow::Error::new)?;
                if successful_output && receipt.outcome() != &Outcome::Succeeded {
                    return Err(StepError::new(
                        "limit-exceeded",
                        "provider terminal did not acknowledge success",
                    )
                    .into());
                }
                Ok(())
            }
            .await;
            crate::core_imports::finish_effect_delivery(cx, result, delivery)
        }
        .await;
        result.map_err(|error| crate::filesystem::preserve_effect_error(cx, error))
    }

    fn nominal_core_error(
        &self,
        cx: &CallCx,
        caller: &ShapeRef,
        target: &FrozenPortTarget,
        op: &OpName,
        origin: ProviderOrigin<'_>,
    ) -> bool {
        if ![caller, &target.provided].into_iter().all(|reference| {
            self.shared
                .core_error_results
                .get(reference)
                .is_some_and(|operations| operations.contains(op))
        }) {
            return false;
        }
        let Some(bindings) = self.shared.pack_core_errors.get() else {
            return false;
        };
        let actual_binding = |pack, hash, direction, view| {
            bindings.get(pack).is_some_and(|evidence| {
                evidence.hash() == hash && evidence.has_core_error_result(direction, view, op)
            })
        };
        if !actual_binding(
            &target.pack,
            &target.hash,
            WitBindingDirection::Export,
            &target.provided,
        ) {
            return false;
        }
        match origin {
            ProviderOrigin::Guest { frame } => {
                // The bridge validated this actual provider frame and the
                // Store host-call context before entry. Do not infer guest
                // origin merely because a Core Flow's principal is a Pack.
                if frame.context().principal() != cx.principal() {
                    return false;
                }
                match frame.context().principal() {
                    Principal::Pack { pack, hash } => {
                        actual_binding(pack, hash, WitBindingDirection::Import, caller)
                    }
                    _ => false,
                }
            }
            ProviderOrigin::Direct
            | ProviderOrigin::Reached { .. }
            | ProviderOrigin::NativeInput { .. }
            | ProviderOrigin::UiInvocation { .. }
            | ProviderOrigin::UserInvocation { .. } => true,
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn invoke_owned_frame(
        &self,
        recipient: &CallCx,
        frame: &Arc<dyn ProviderFrame>,
        target: &FrozenPortTarget,
        provided: &ShapeRef,
        op: &OpName,
        caller: &WitFunction,
        provider: &WitFunction,
        args: &CoreValue,
        digest: &Digest,
        final_output: Option<FinalOutputKind>,
        typed: bool,
        nominal_core_error: bool,
    ) -> Result<DispatchOutput> {
        let cx = frame.context();
        let result = async {
            let owners = self.allocation_owners_for_frame(
                cx,
                frame
                    .allocation_scope(AllocationReason::RuntimeValue)
                    .map_err(anyhow::Error::new)?,
            )?;
            if args.digest_guest(owners.graph())? != *digest {
                bail!("admitted arguments differ from actual immutable value")
            }
            let object = args.object().context("port args must be an object")?;
            if object.len() != caller.params.len() || caller.params.len() != provider.params.len() {
                bail!("port argument fields do not match the signature")
            }
            let mut params = BudgetedVecBuilder::new(provider.params.len(), owners.component(), 0)?;
            for ((name, from), (provider_name, to)) in caller.params.iter().zip(&provider.params) {
                if name != provider_name {
                    bail!("port parameter names require an adapter")
                }
                let source = object.get(name).context("port parameter missing")?;
                let typed = core_to_val(&source, from, &owners)?;
                let value = project_val_owned(&typed, from, to, &owners)?;
                params.push(self.transfer_argument_handles(&value, to, recipient, cx, &owners)?)?;
            }
            let params = params.finish()?;
            let (results, generation) = self
                .export_call_owned(
                    &target.pack,
                    cx,
                    &interface_name(provided),
                    op.as_str(),
                    params.as_slice(),
                    None,
                    &owners,
                    frame.clone(),
                    frame
                        .allocation_scope(AllocationReason::RuntimeValue)
                        .map_err(anyhow::Error::new)?,
                )
                .await?;
            let results = match (&provider.result, results.as_slice()) {
                (Some(ty), [value]) if has_handle(ty) => BudgetedVec::from_iter(
                    [self
                        .transfer_result_handles(
                            value, ty, cx, recipient, provided, generation, frame, &owners,
                        )
                        .await?]
                    .into_iter(),
                    owners.component(),
                    0,
                )?,
                _ => results,
            };
            if typed {
                let mut projected = BudgetedVecBuilder::new(results.len(), owners.component(), 0)?;
                let value = match (&provider.result, &caller.result, results.as_slice()) {
                    (None, None, []) => CoreValue::null(owners.graph())?,
                    (Some(from), Some(to), [value]) => {
                        let value = project_val_owned(value, from, to, &owners)?;
                        let graph = val_to_core(&value, to, &owners)?;
                        projected.push(value)?;
                        graph
                    }
                    _ => bail!("typed proxy result arity/signature mismatch"),
                };
                let values = projected.finish()?;
                let error = matches!(values.first(), Some(Val::Result(Err(_))));
                let encoded = value.encode_guest(owners.graph())?;
                if encoded.len() as u64 > cx.limits().output_bytes {
                    return Err(tobkiri_gate::GateError::Limit(
                        "component output budget exceeded".into(),
                    )
                    .into());
                }
                frame
                    .output_ready(&value, owners.graph())
                    .map_err(anyhow::Error::new)?;
                return Ok(DispatchOutput::Typed { values, error });
            }
            let value = decode_result(&results, caller, provider, &owners, nominal_core_error)?;
            let returns_stream = self
                .shared
                .views
                .get(provided)
                .is_some_and(|shape| shape.streams.contains_key(op.as_str()));
            if returns_stream {
                let id = value
                    .get_key("id")
                    .context("stream facade result has no handle")?;
                let id = match id.inspect() {
                    tobkiri_canon::CoreRef::Int(id) => u64::try_from(id)?,
                    tobkiri_canon::CoreRef::Str(id) => id.parse()?,
                    _ => bail!("stream facade ID is not an integer"),
                };
                let mut stream = self.component_stream(recipient, id)?;
                frame.stream_ready().map_err(anyhow::Error::new)?;
                if let Some(kind) = final_output {
                    let output = self.allocation_owners_for_frame(
                        cx,
                        frame
                            .allocation_scope(AllocationReason::OutputBuffer)
                            .map_err(anyhow::Error::new)?,
                    )?;
                    match crate::output::encode_stream(kind, output.graph()) {
                        Ok(envelope) => {
                            return Ok(DispatchOutput::Final(FinalPortOutput::Stream {
                                envelope,
                                stream,
                            }));
                        }
                        Err(error) => {
                            stream.close().await.map_err(anyhow::Error::new)?;
                            return Err(error.into());
                        }
                    }
                }
                return Ok(DispatchOutput::Stream(stream));
            }
            frame
                .output_ready(&value, owners.graph())
                .map_err(anyhow::Error::new)?;
            if let Some(kind) = final_output {
                let output = self.allocation_owners_for_frame(
                    cx,
                    frame
                        .allocation_scope(AllocationReason::OutputBuffer)
                        .map_err(anyhow::Error::new)?,
                )?;
                let encoded = crate::output::encode_value(&value, kind, output.graph())?;
                if encoded.len() as u64 > cx.limits().output_bytes {
                    return Err(tobkiri_gate::GateError::Limit(
                        "component output budget exceeded".into(),
                    )
                    .into());
                }
                Ok(DispatchOutput::Final(FinalPortOutput::Value(encoded)))
            } else {
                // Size validation uses a funded temporary buffer; the returned
                // graph retains its producer owner after the buffer is destroyed.
                let encoded = value.encode_guest(owners.graph())?;
                if encoded.len() as u64 > cx.limits().output_bytes {
                    return Err(tobkiri_gate::GateError::Limit(
                        "component output budget exceeded".into(),
                    )
                    .into());
                }
                Ok(DispatchOutput::Value(value))
            }
        }
        .await;
        preserve_dispatch_result(cx, result)
    }

    pub(crate) async fn read_native_input_owned(
        &self,
        cx: &CallCx,
        entry: &CompiledInputEntry,
    ) -> Result<(CoreValue, SourceReadReceipt)> {
        let result = async {
            self.shared.gate.check_context(cx)?;
            if !matches!(cx.principal(), Principal::Input { .. })
                || cx.revision() != Some(entry.profile_revision())
            {
                bail!("native Input entry context mismatch")
            }
            let (pack, provided, _) =
                self.resolve_for_admission(cx, entry.exact_input_view(), entry.operation(), true)?;
            if &pack.manifest.id != entry.source_pack()
                || &pack.hash != entry.source_hash()
                || self
                    .shared
                    .views
                    .get(entry.exact_input_view())
                    .and_then(|view| view.functions.get(entry.operation()))
                    != Some(entry.function())
            {
                bail!("native Input entry differs from frozen source signature")
            }
            let target = FrozenPortTarget {
                pack: pack.manifest.id.clone(),
                hash: pack.hash.clone(),
                provided: provided.reference,
            };
            let bridge = self
                .shared
                .provider_entry
                .get()
                .context("Core provider-entry bridge not installed")?
                .clone();
            let preparation = bridge
                .prepare_native_input(self, cx, entry, &target)
                .map_err(anyhow::Error::new)?;
            let owners = self.allocation_owners_for_frame(
                cx,
                preparation.argument_scope().map_err(anyhow::Error::new)?,
            )?;
            // The compiler fixed only explicit empty-record arguments. Preserve
            // their exact parameter names/arity; never synthesize caller defaults.
            let args = CoreValue::try_from_borrowed(entry.arguments(), owners.graph())?;
            let args_digest = args.digest_guest(owners.graph())?;
            let admission = self
                .shared
                .gate
                .admit_port(
                    cx,
                    entry.exact_input_view().clone(),
                    entry.operation().clone(),
                    args_digest.clone(),
                    target.clone(),
                )
                .await?;
            let request = ProviderRequest {
                control: self,
                caller: cx,
                view: entry.exact_input_view(),
                op: entry.operation(),
                target: &target,
                args_digest: &args_digest,
                origin: ProviderOrigin::NativeInput {
                    entry,
                    preparation: &preparation,
                },
            };
            bridge
                .validate_prepare(&request)
                .map_err(anyhow::Error::new)?;
            let dispatch = self
                .shared
                .gate
                .claim_port(
                    cx,
                    admission,
                    entry.exact_input_view(),
                    entry.operation(),
                    &args_digest,
                    &target,
                )
                .await?;
            let mut terminal =
                ConsumedPortTerminal::new(dispatch.into_permit(), self.shared.gate.clone());
            if let Err(error) = terminal.watch(cx) {
                terminal
                    .finish(Outcome::Failed {
                        code: "lease-supervision".into(),
                    })
                    .await?;
                return Err(error.into());
            }
            let frame = match bridge.enter_provider(&request, terminal.permit(), &args) {
                Ok(frame) => frame,
                Err(error) => {
                    terminal
                        .finish(Outcome::Failed {
                            code: "provider-entry".into(),
                        })
                        .await?;
                    return Err(anyhow::Error::new(error));
                }
            };
            if let Err(error) = terminal.bind_execution_deadline(cx, frame.context()).await {
                let receipt = terminal
                    .finish(Outcome::Failed {
                        code: "lease-narrowing".into(),
                    })
                    .await?;
                frame
                    .terminal_acknowledged(&receipt)
                    .map_err(anyhow::Error::new)?;
                return Err(error.into());
            }
            let provider = self
                .shared
                .views
                .get(&target.provided)
                .and_then(|view| view.functions.get(entry.operation()))
                .context("native Input provider signature missing")?;
            let result = self
                .invoke_owned_frame(
                    cx,
                    &frame,
                    &target,
                    &target.provided,
                    entry.operation(),
                    entry.function(),
                    provider,
                    &args,
                    &args_digest,
                    None,
                    false,
                    self.nominal_core_error(
                        cx,
                        entry.exact_input_view(),
                        &target,
                        entry.operation(),
                        request.origin,
                    ),
                )
                .await;
            match result {
                Ok(DispatchOutput::Value(value)) => {
                    let terminal_receipt = terminal.finish(Outcome::Succeeded).await?;
                    frame
                        .terminal_acknowledged(&terminal_receipt)
                        .map_err(anyhow::Error::new)?;
                    if terminal_receipt.outcome() != &Outcome::Succeeded {
                        return Err(StepError::new(
                            "limit-exceeded",
                            "physical Input read has no acknowledged success",
                        )
                        .into());
                    }
                    let source_receipt = frame
                        .take_source_read_receipt()
                        .map_err(anyhow::Error::new)?;
                    Ok((value, source_receipt))
                }
                Ok(DispatchOutput::Final(_) | DispatchOutput::Typed { .. }) => {
                    unreachable!("physical source read is intermediate")
                }
                Ok(DispatchOutput::Stream(mut stream)) => {
                    let cleanup = stream.close().await;
                    let receipt = terminal
                        .finish(Outcome::Failed {
                            code: "source-input-stream".into(),
                        })
                        .await?;
                    frame
                        .terminal_acknowledged(&receipt)
                        .map_err(anyhow::Error::new)?;
                    cleanup.map_err(anyhow::Error::new)?;
                    Err(StepError::new(
                    "unavailable",
                    "Native Input stream receipt delivery requires the live source-ingress bridge",
                )
                .into())
                }
                Err(error) => {
                    let delivery = async {
                        let code = crate::core_imports::storage_failure_metadata(&error)
                            .map_or("source-read", |(code, _)| code);
                        let receipt = terminal
                            .finish(Outcome::Failed { code: code.into() })
                            .await?;
                        frame
                            .terminal_acknowledged(&receipt)
                            .map_err(anyhow::Error::new)?;
                        Ok(())
                    }
                    .await;
                    crate::core_imports::finish_effect_delivery(cx, Err(error), delivery)
                }
            }
        }
        .await;
        result.map_err(|error| crate::filesystem::preserve_effect_error(cx, error))
    }
}

fn preserve_dispatch_result(cx: &CallCx, result: Result<DispatchOutput>) -> Result<DispatchOutput> {
    match result {
        // A typed provider error is unsuccessful delivery too. Its guest
        // payload cannot erase native evidence that files may have changed.
        Ok(DispatchOutput::Typed { error: true, .. }) if cx.filesystem_mutation_possible() => {
            Err(crate::filesystem::FsError::CommitUnknown.into())
        }
        Err(error) => Err(crate::filesystem::preserve_effect_error(cx, error)),
        result => result,
    }
}

/// Sole caller owner for the consumed physical dispatch. Frame preparation may
/// borrow this permit synchronously; no frame owns or settles a raw permit.
struct ConsumedPortTerminal {
    permit: DispatchPermit,
    gate: Arc<dyn Gate>,
    proposal: Option<Outcome>,
}
impl ConsumedPortTerminal {
    fn new(permit: DispatchPermit, gate: Arc<dyn Gate>) -> Self {
        Self {
            permit,
            gate,
            proposal: None,
        }
    }
    fn permit(&self) -> &DispatchPermit {
        &self.permit
    }
    fn watch(&self, cx: &CallCx) -> Result<(), GateError> {
        self.gate.watch_lease(cx, &self.permit)
    }
    async fn bind_execution_deadline(
        &mut self,
        caller: &CallCx,
        provider: &CallCx,
    ) -> Result<(), GateError> {
        let bounded = caller.clone().cap_deadline(provider.deadline());
        self.gate.narrow_lease(&mut self.permit, &bounded).await?;
        self.gate.watch_lease(&bounded, &self.permit)
    }
    async fn finish(&mut self, outcome: Outcome) -> Result<AcknowledgedTerminal, GateError> {
        if let Some(proposal) = &self.proposal {
            if proposal != &outcome {
                return Err(GateError::Denied(
                    "physical terminal proposal is immutable".into(),
                ));
            }
        } else {
            self.proposal = Some(outcome.clone());
        }
        if std::time::Instant::now() >= self.permit.deadline() {
            self.gate.settle_deadline(&self.permit).await?;
        } else if let Err(error) = self
            .gate
            .record_outcome(self.permit.id().clone(), outcome)
            .await
        {
            if matches!(error, GateError::Deadline) {
                self.gate.settle_deadline(&self.permit).await?;
            } else {
                return Err(error);
            }
        }
        self.gate.acknowledged_terminal(&self.permit)
    }
}

enum DispatchOutput {
    Value(CoreValue),
    Stream(tobkiri_flow::CoreStream),
    Final(FinalPortOutput),
    Typed {
        values: BudgetedVec<Val>,
        error: bool,
    },
}

struct DigestWork(std::sync::atomic::AtomicU64);
impl AllocationBudget for DigestWork {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        if request.bytes != 0 {
            return Err(AllocationError::Unavailable);
        }
        self.0
            .fetch_update(
                std::sync::atomic::Ordering::AcqRel,
                std::sync::atomic::Ordering::Acquire,
                |remaining| remaining.checked_sub(request.work.max(request.elements)),
            )
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }
    fn release(&self, bytes: u64) {
        assert_eq!(bytes, 0, "digest work cannot own physical payload");
    }
}

pub(crate) fn has_handle(ty: &WitType) -> bool {
    match ty {
        WitType::StreamHandle | WitType::CallbackHandle => true,
        WitType::List(ty) | WitType::Option(ty) => has_handle(ty),
        WitType::Tuple(types) => types.iter().any(has_handle),
        WitType::Record(fields) => fields.iter().any(|field| has_handle(&field.ty)),
        WitType::Variant(cases) => cases
            .iter()
            .filter_map(|case| case.ty.as_ref())
            .any(has_handle),
        WitType::Result { ok, err } => {
            ok.as_deref().is_some_and(has_handle) || err.as_deref().is_some_and(has_handle)
        }
        _ => false,
    }
}

/// Common typed facade: unwrap a success payload, retain a payloadless success
/// tag, and keep zero result (null) distinct from an explicit empty record ({}).
fn decode_result(
    results: &[Val],
    caller: &WitFunction,
    provider: &WitFunction,
    owners: &ValueOwners,
    nominal_core_error: bool,
) -> Result<CoreValue> {
    let value = match (&provider.result, &caller.result, results) {
        (None, None, []) => CoreValue::null(owners.graph())?,
        (Some(from), Some(to), [value]) => {
            let projected = project_val_owned(value, from, to, owners)?;
            val_to_core(&projected, to, owners)?
        }
        _ => bail!("component result arity/signature mismatch"),
    };
    if let Some(WitType::Result { ok, .. }) = &caller.result {
        let tag = value.get_key("tag").context("typed result tag missing")?;
        match tag.as_str() {
            Some("err") => {
                return Err(public_provider_error(&value, nominal_core_error).into());
            }
            Some("ok") if ok.is_some() => {
                return value
                    .get_key("val")
                    .context("typed success payload missing");
            }
            Some("ok") => return Ok(value),
            _ => bail!("typed result case invalid"),
        }
    }
    Ok(value)
}

fn public_provider_error(value: &CoreValue, nominal_core_error: bool) -> StepError {
    if nominal_core_error
        && value.object().is_some_and(|fields| fields.len() == 2)
        && value
            .get_key("tag")
            .is_some_and(|tag| tag.as_str() == Some("err"))
        && let Some(payload) = value.get_key("val")
        && payload.object().is_some_and(|fields| fields.len() == 2)
        && payload
            .get_key("message")
            .is_some_and(|message| message.as_str().is_some())
        && let Some(code) = payload.get_key("code")
    {
        // Both frozen view/op bindings must resolve their OUTER result.err to
        // the committed Core type. Even then only closed codes cross the
        // facade: the guest's message is never copied or persisted here.
        let fixed = match code.as_str() {
            Some("denied") => Some(("denied", "provider denied the operation")),
            Some("approval-required") => Some((
                "approval-required",
                "provider reported that approval is required",
            )),
            Some("cancelled") => Some(("cancelled", "provider reported cancellation")),
            Some("not-found") => Some(("not-found", "provider reported a missing item")),
            Some("invalid") => Some(("invalid", "provider reported invalid input")),
            Some("limit-exceeded") => {
                Some(("limit-exceeded", "provider reported a resource limit"))
            }
            Some("conflict") => Some(("conflict", "provider reported a conflict")),
            Some("unavailable") => Some(("unavailable", "provider reported unavailable")),
            Some("internal") => Some(("internal", "provider reported an internal error")),
            _ => None,
        };
        if let Some((code, message)) = fixed {
            return StepError::new(code, message);
        }
    }
    StepError::new("provider-error", "provider returned a typed error")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use tobkiri_canon::{AllocationKind, CoreRef};
    use wasmtime::component::{
        BudgetedBox, BudgetedVec, HostAllocationBudget, HostAllocationError, HostAllocationRequest,
    };

    struct Meter(AtomicU64);
    impl AllocationBudget for Meter {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            self.0
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| {
                    n.checked_add(request.bytes)
                })
                .map(|_| ())
                .map_err(|_| AllocationError::Overflow)
        }
        fn release(&self, bytes: u64) {
            self.0
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| {
                    n.checked_sub(bytes)
                })
                .expect("release follows charged physical storage");
        }
    }
    impl HostAllocationBudget for Meter {
        fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError> {
            AllocationBudget::reserve(
                self,
                AllocationRequest {
                    kind: AllocationKind::Temporary,
                    bytes: request.bytes,
                    elements: request.elements,
                    depth: request.depth,
                    work: request.work,
                },
            )
            .map_err(|_| HostAllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            AllocationBudget::release(self, bytes);
        }
    }
    fn owners() -> (Arc<Meter>, ValueOwners) {
        let meter = Arc::new(Meter(AtomicU64::new(0)));
        (
            meter.clone(),
            ValueOwners {
                graph: meter.clone(),
                component: meter,
                allocation_failure: None,
            },
        )
    }
    fn signature(result: Option<WitType>) -> WitFunction {
        WitFunction {
            params: vec![],
            result,
        }
    }

    fn effect_context() -> CallCx {
        use std::time::{Duration, Instant};
        use tobkiri_canon::ids::{PackHash, PackId, ProfileId, RequestId};
        use tobkiri_gate::{CoreCancellation, ResourceLimits};
        CallCx::root(
            ProfileId::parse("profile-dispatch-effect").unwrap(),
            Principal::Pack {
                pack: PackId::parse("local.dispatch-effect").unwrap(),
                hash: PackHash::new(Digest::hash(b"dispatch-effect-test")),
            },
            RequestId::parse("dispatch-effect-request").unwrap(),
            CoreCancellation::new(),
            Instant::now() + Duration::from_secs(30),
            ResourceLimits::default(),
        )
    }

    #[test]
    fn typed_provider_failure_after_mutation_cannot_erase_native_no_replay_evidence() {
        let cx = effect_context();
        let (meter, owners) = owners();
        let typed = || DispatchOutput::Typed {
            values: BudgetedVec::from_iter(std::iter::empty(), owners.component(), 0).unwrap(),
            error: true,
        };
        assert!(matches!(
            preserve_dispatch_result(&cx, Ok(typed()))
                .unwrap_or_else(|_| panic!("read-only typed result")),
            DispatchOutput::Typed { error: true, .. }
        ));
        cx.note_filesystem_mutation();
        let error = preserve_dispatch_result(&cx, Ok(typed()))
            .err()
            .expect("typed failure is not success");
        assert!(matches!(
            error.downcast_ref::<crate::filesystem::FsError>(),
            Some(crate::filesystem::FsError::CommitUnknown)
        ));
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn result_decode_failure_after_mutation_keeps_the_closed_native_error() {
        let cx = effect_context();
        let (meter, owners) = owners();
        let ty = signature(Some(WitType::Result {
            ok: None,
            err: None,
        }));
        let decode_error = decode_result(&[Val::Result(Err(None))], &ty, &ty, &owners, false)
            .err()
            .expect("provider error must not decode as a value");
        cx.note_filesystem_mutation();
        let error = preserve_dispatch_result(&cx, Err(decode_error))
            .err()
            .expect("decode failure remains unsuccessful");
        assert_eq!(
            crate::core_imports::storage_failure_metadata(&error)
                .unwrap()
                .0,
            "filesystem-commit-unknown"
        );
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn common_codec_distinguishes_zero_result_empty_record_and_payloadless_success() {
        let (meter, owners) = owners();
        let unit = signature(None);
        let no_result = decode_result(&[], &unit, &unit, &owners, false).unwrap();
        assert!(matches!(no_result.inspect(), CoreRef::Null));
        let empty = signature(Some(WitType::Record(vec![])));
        let empty_raw =
            Val::Record(BudgetedVec::from_iter(std::iter::empty(), owners.component(), 0).unwrap());
        let empty_record = decode_result(&[empty_raw], &empty, &empty, &owners, false).unwrap();
        assert_eq!(empty_record.object().unwrap().len(), 0);
        let tagged = signature(Some(WitType::Result {
            ok: None,
            err: None,
        }));
        let payloadless =
            decode_result(&[Val::Result(Ok(None))], &tagged, &tagged, &owners, false).unwrap();
        assert_eq!(payloadless.get_key("tag").unwrap().as_str(), Some("ok"));
        assert!(payloadless.get_key("val").is_none());
        assert_eq!(payloadless.object().unwrap().len(), 1);
        drop((no_result, empty_record, payloadless));
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn explicit_empty_success_payload_stays_record_and_error_never_becomes_input() {
        let (meter, owners) = owners();
        let ty = signature(Some(WitType::Result {
            ok: Some(Box::new(WitType::Record(vec![]))),
            err: None,
        }));
        let empty_raw =
            Val::Record(BudgetedVec::from_iter(std::iter::empty(), owners.component(), 0).unwrap());
        let raw = Val::Result(Ok(Some(
            BudgetedBox::new(empty_raw, owners.component(), 0).unwrap(),
        )));
        let value = decode_result(&[raw], &ty, &ty, &owners, false).unwrap();
        assert_eq!(value.object().unwrap().len(), 0);
        assert!(decode_result(&[Val::Result(Err(None))], &ty, &ty, &owners, false).is_err());
        drop(value);
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    const CORE_ERROR_CODES: [&str; 9] = [
        "denied",
        "approval-required",
        "cancelled",
        "not-found",
        "invalid",
        "limit-exceeded",
        "conflict",
        "unavailable",
        "internal",
    ];
    fn error_graph(code: &str, owners: &ValueOwners) -> CoreValue {
        let payload = CoreValue::object_from(
            [
                Ok(("code", CoreValue::string(code, owners.graph()).unwrap())),
                Ok((
                    "message",
                    CoreValue::string("distinctive-private-provider-message", owners.graph())
                        .unwrap(),
                )),
            ]
            .into_iter(),
            owners.graph(),
        )
        .unwrap();
        CoreValue::object_from(
            [
                Ok(("tag", CoreValue::string("err", owners.graph()).unwrap())),
                Ok(("val", payload)),
            ]
            .into_iter(),
            owners.graph(),
        )
        .unwrap()
    }

    #[test]
    fn verified_core_error_facade_projects_only_closed_codes_without_guest_messages() {
        let (meter, owners) = owners();
        let signature = signature(Some(WitType::Result {
            ok: Some(Box::new(WitType::String)),
            err: Some(Box::new(WitType::Record(vec![
                tobkiri_manifest::WitField {
                    name: "code".into(),
                    ty: WitType::Enum(CORE_ERROR_CODES.iter().map(|code| (*code).into()).collect()),
                },
                tobkiri_manifest::WitField {
                    name: "message".into(),
                    ty: WitType::String,
                },
            ]))),
        }));
        for code in CORE_ERROR_CODES {
            let graph = error_graph(code, &owners);
            let raw = core_to_val(&graph, signature.result.as_ref().unwrap(), &owners).unwrap();
            for verified in [false, true] {
                let error = decode_result(
                    std::slice::from_ref(&raw),
                    &signature,
                    &signature,
                    &owners,
                    verified,
                )
                .unwrap_err()
                .downcast::<StepError>()
                .unwrap();
                assert_eq!(error.code, if verified { code } else { "provider-error" });
                assert!(
                    !error
                        .message
                        .contains("distinctive-private-provider-message")
                );
                // Public projection is only facade behavior. The original WIT
                // error still roundtrips unchanged for a typed guest proxy.
                let round =
                    core_to_val(&graph, signature.result.as_ref().unwrap(), &owners).unwrap();
                assert_eq!(round, raw);
            }
        }
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn unknown_codes_and_malformed_core_error_payloads_stay_masked() {
        let (meter, owners) = owners();
        let unknown = error_graph("guest-private-code", &owners);
        assert_eq!(public_provider_error(&unknown, true).code, "provider-error");
        let incomplete = CoreValue::object_from(
            [
                Ok(("tag", CoreValue::string("err", owners.graph()).unwrap())),
                Ok((
                    "val",
                    CoreValue::object_from(
                        [Ok((
                            "code",
                            CoreValue::string("invalid", owners.graph()).unwrap(),
                        ))]
                        .into_iter(),
                        owners.graph(),
                    )
                    .unwrap(),
                )),
            ]
            .into_iter(),
            owners.graph(),
        )
        .unwrap();
        assert_eq!(
            public_provider_error(&incomplete, true).code,
            "provider-error"
        );
        drop((unknown, incomplete));
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn preclaim_hash_work_meter_cannot_sponsor_payload_or_refill_work() {
        let work = DigestWork(AtomicU64::new(3));
        let mut request = AllocationRequest {
            kind: AllocationKind::Encode,
            bytes: 1,
            elements: 0,
            depth: 0,
            work: 0,
        };
        assert!(work.reserve(request).is_err());
        request.bytes = 0;
        request.work = 2;
        work.reserve(request).unwrap();
        work.release(0);
        assert_eq!(work.reserve(request), Err(AllocationError::Limit));
    }
}
