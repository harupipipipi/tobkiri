use super::*;
use tobkiri_canon::CoreBuffer;
use tobkiri_flow::UiInvocation;

#[path = "ui_source.rs"]
mod ui_source;

const MAX_UI_GENERATION: u64 = 9_007_199_254_740_991;

impl Session {
    /// Mint and bind a UI token after checking the owning Pack's manifest.
    pub(crate) fn register_ui(
        &self,
        id: UiSessionId,
        host_session: UiSessionId,
        pack: PackId,
        hash: PackHash,
        ports: BTreeSet<ShapeRef>,
    ) -> Result<(), CoreError> {
        let mut sessions = self
            .ui
            .lock()
            .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
        if sessions.contains_key(&id) {
            return Err(CoreError::Invalid(
                "UI session identifier was reused".into(),
            ));
        }
        let receiver = super::ui_receiver_identity(&id);
        sessions.insert(
            id,
            UiPrincipal {
                pack,
                hash,
                ports,
                host_session,
                generation: None,
                receiver,
                cancel: self.shutdown.child_with_control(self.gate.control())?,
            },
        );
        Ok(())
    }

    /// Bind the trusted Launcher generation to this Session's Core-minted UI
    /// attachments. The Launcher calls this once after Core startup and before
    /// publishing any frame or accepting a UI message.
    pub fn bind_ui_generation(&self, generation: u64) -> Result<(), CoreError> {
        if generation == 0 || generation > MAX_UI_GENERATION {
            return Err(CoreError::Invalid("invalid UI generation".into()));
        }
        self.check_snapshot()?;
        let mut sessions = self
            .ui
            .lock()
            .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
        if sessions
            .values()
            .any(|principal| principal.generation.is_some())
        {
            return Err(CoreError::Invalid(
                "UI generation was already bound for this Session".into(),
            ));
        }
        let materials = self
            .materials
            .lock()
            .map_err(|_| CoreError::Runtime("UI materials poisoned".into()))?;
        for (session, principal) in sessions.iter() {
            let pin = self
                .compiled
                .profile
                .packs
                .iter()
                .find(|pin| pin.id == principal.pack)
                .ok_or_else(|| CoreError::Invalid("UI source Pack is not pinned".into()))?;
            if self.compiled.profile.startup.ui.as_ref() != Some(&principal.pack)
                || pin.sha256 != principal.hash
                || principal.receiver != super::ui_receiver_identity(session)
                || !materials.contains_key(&(principal.pack.clone(), principal.hash.clone()))
                || principal
                    .ports
                    .iter()
                    .any(|port| !self.compiled.views.contains_key(port))
            {
                return Err(CoreError::Invalid(
                    "UI attachment differs from the frozen verified source".into(),
                ));
            }
        }
        for principal in sessions.values_mut() {
            principal.generation = Some(generation);
        }
        Ok(())
    }

    /// The host binds each rendered frame's MessageChannel to its Core token.
    pub fn ui_attachments(&self) -> Result<Vec<UiAttachment>, CoreError> {
        let sessions = self
            .ui
            .lock()
            .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
        let materials = self
            .materials
            .lock()
            .map_err(|_| CoreError::Runtime("UI materials poisoned".into()))?;
        sessions
            .iter()
            .map(|(session, principal)| {
                let generation = principal.generation.ok_or_else(|| {
                    CoreError::Invalid("UI generation has not been bound by Launcher".into())
                })?;
                if principal.receiver != super::ui_receiver_identity(session) {
                    return Err(CoreError::Invalid(
                        "UI receiver differs from its Core attachment".into(),
                    ));
                }
                let material = materials
                    .get(&(principal.pack.clone(), principal.hash.clone()))
                    .ok_or_else(|| CoreError::Invalid("verified UI entry is missing".into()))?;
                Ok(UiAttachment {
                    session: session.clone(),
                    host_session: principal.host_session.clone(),
                    pack: principal.pack.clone(),
                    hash: principal.hash.clone(),
                    entry: material.entry.clone(),
                    generation: Some(generation),
                    receiver: principal.receiver.clone(),
                })
            })
            .collect()
    }

    /// Read bytes from the immutable verified Pack, scoped to an attached frame.
    pub fn verified_ui_asset(
        &self,
        session: &UiSessionId,
        path: &str,
    ) -> Result<Vec<u8>, CoreError> {
        self.check_snapshot()?;
        if path.is_empty()
            || path.starts_with('/')
            || path
                .split('/')
                .any(|p| p.is_empty() || p == "." || p == "..")
            || path.contains('\\')
        {
            return Err(CoreError::Invalid("invalid UI asset path".into()));
        }
        let sessions = self
            .ui
            .lock()
            .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
        let ui = sessions
            .get(session)
            .ok_or_else(|| CoreError::Invalid("unknown UI session".into()))?;
        let materials = self
            .materials
            .lock()
            .map_err(|_| CoreError::Runtime("UI materials poisoned".into()))?;
        materials
            .get(&(ui.pack.clone(), ui.hash.clone()))
            .and_then(|m| m.files.get(path))
            .cloned()
            .ok_or_else(|| CoreError::NotFound("verified UI asset is missing".into()))
    }

    /// Compatibility entry for pre-receive callers. This API has no received
    /// attachment evidence and therefore cannot invoke a UI operation.
    pub async fn ui_invoke(
        &self,
        _session: UiSessionId,
        _shape: ShapeRef,
        _op: OpName,
        _args: CanonValue,
    ) -> Result<UiCallResult, CoreError> {
        Err(CoreError::Invalid(
            "UI invocation requires an authenticated received attachment".into(),
        ))
    }
    /// Compatibility entry for pre-receive callers. Use
    /// `ui_invoke_received_with_cancel` for the source-backed invocation path.
    pub async fn ui_invoke_with_cancel(
        &self,
        _session: UiSessionId,
        _shape: ShapeRef,
        _op: OpName,
        _args: CanonValue,
        _cancel: TokioCancellationToken,
    ) -> Result<UiCallResult, CoreError> {
        Err(CoreError::Invalid(
            "UI invocation requires an authenticated received attachment".into(),
        ))
    }

    /// Accept one invocation after the native host has authenticated its
    /// namespace, operation and attachment. Core checks those claims against
    /// the Session record before it materializes the argument graph.
    pub async fn ui_invoke_received(
        &self,
        session: &UiSessionId,
        host_session: &UiSessionId,
        generation: u64,
        receiver: &Digest,
        shape: ShapeRef,
        op: OpName,
        args: &CanonValue,
    ) -> Result<CoreBuffer, CoreError> {
        self.ui_invoke_received_with_cancel(
            session,
            host_session,
            generation,
            receiver,
            shape,
            op,
            args,
            TokioCancellationToken::new(),
        )
        .await
    }

    /// Received UI entry with request-scoped cancellation. Its source budget
    /// is prepared before `CanonValue` is copied into Core ownership.
    pub async fn ui_invoke_received_with_cancel(
        &self,
        session: &UiSessionId,
        host_session: &UiSessionId,
        generation: u64,
        receiver: &Digest,
        shape: ShapeRef,
        op: OpName,
        args: &CanonValue,
        cancel: TokioCancellationToken,
    ) -> Result<CoreBuffer, CoreError> {
        self.ensure_started().await?;
        self.check_snapshot()?;
        if generation == 0 || generation > MAX_UI_GENERATION {
            return Err(CoreError::Invalid("invalid UI generation claim".into()));
        }

        // Resolve source identity exclusively from Core's attachment table.
        // The received fields are match claims; none can select a principal,
        // owner, provider target, or budget.
        let (pack, hash, token) = {
            let sessions = self
                .ui
                .lock()
                .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
            let principal = sessions
                .get(session)
                .ok_or_else(|| CoreError::Invalid("unknown UI session".into()))?;
            let pin = self
                .compiled
                .profile
                .packs
                .iter()
                .find(|pin| pin.id == principal.pack)
                .ok_or_else(|| CoreError::Invalid("UI source Pack is not pinned".into()))?;
            if principal.host_session != *host_session
                || principal.generation != Some(generation)
                || principal.receiver != *receiver
                || principal.receiver != super::ui_receiver_identity(session)
                || self.compiled.profile.startup.ui.as_ref() != Some(&principal.pack)
                || pin.sha256 != principal.hash
                || !principal.ports.contains(&shape)
                || !self
                    .compiled
                    .views
                    .get(&shape)
                    .is_some_and(|view| view.functions.contains_key(&op))
            {
                return Err(CoreError::Invalid(
                    "received UI invocation differs from its verified attachment".into(),
                ));
            }
            let materials = self
                .materials
                .lock()
                .map_err(|_| CoreError::Runtime("UI materials poisoned".into()))?;
            if !materials.contains_key(&(principal.pack.clone(), principal.hash.clone())) {
                return Err(CoreError::Invalid(
                    "verified UI source material is missing".into(),
                ));
            }
            if principal.cancel.is_cancelled() {
                return Err(CoreError::Invalid("UI attachment is closed".into()));
            }
            (
                principal.pack.clone(),
                principal.hash.clone(),
                principal.cancel.child_with_control(self.gate.control())?,
            )
        };

        let base_context = CallCx::try_root_with_control(
            self.compiled.profile.id.clone(),
            Principal::PackUi {
                pack,
                hash,
                ui: session.clone(),
            },
            RequestId::new(format!("req_{}", ulid::Ulid::new()))?,
            token,
            deadline_after(self.compiled.profile.ceiling.wasm.wall_ms)?,
            self.compiled.profile.ceiling.wasm.clone(),
            self.gate.control().clone(),
        )?
        .with_security_epoch(self.epoch)
        .with_revision(self.compiled.plan_digest.clone());
        let source_permissions = self.gate.effective_permissions(&base_context)?;
        let context = base_context.narrow_limits(&source_permissions.wasm);
        // This received invocation is a top-level execution. Its argument
        // preparation and every provider/stream descendant must share the
        // actual UI caller's bounded ledger, just like User and Input entries.
        let budget = BudgetContext::new(source_permissions.loops, context.deadline())
            .map_err(|error| CoreError::Invalid(error.to_string()))?;
        let context = context.with_budget(budget)?;
        if cancel.is_cancelled() || context.cancel().is_cancelled() {
            return Err(CoreError::Step(StepError::new(
                "cancelled",
                "UI request was cancelled before preparation",
            )));
        }

        // Preparation resolves the unique frozen endpoint and reserves the
        // actual UI source's RuntimeValue scope before CoreValue allocation.
        let factory = self.execution_factory.as_ref().ok_or_else(|| {
            CoreError::Step(StepError::new(
                "unavailable",
                "Core UI invocation factory is unavailable",
            ))
        })?;
        let prepared =
            factory.prepare_ui_invocation(&context, session, generation, receiver, &shape, &op)?;
        if cancel.is_cancelled() || context.cancel().is_cancelled() {
            return Err(CoreError::Step(StepError::new(
                "cancelled",
                "UI request was cancelled before argument materialization",
            )));
        }

        ui_source::validate_args(args).map_err(|error| match error {
            ui_source::Error::Invalid => {
                CoreError::Step(StepError::new("invalid", "UI arguments are not StrictV1"))
            }
            ui_source::Error::Limit => CoreError::Step(StepError::new(
                "limit-exceeded",
                "UI arguments exceed the native control bound",
            )),
        })?;

        let invocation = UiInvocation {
            invocation: prepared.invocation(),
            session,
            generation,
            receiver,
            view: &shape,
            op: &op,
            target: prepared.target(),
        };
        let preparation = prepared.preparation();
        let argument_scope = preparation.argument_scope()?;
        let budget = self
            .invoker
            .allocation_budget_for_frame(&context, argument_scope)?;
        let owned_args =
            CoreValue::try_from_borrowed(args, &budget).map_err(ui_allocation_error)?;
        // The same source budget verifies the exact StrictV1 digest before the
        // final port admission; provider entry will rehash under its callee.
        let arguments_digest = owned_args
            .digest_guest(&budget)
            .map_err(ui_allocation_error)?;

        // The output adapter may publish this fixed candidate only if the
        // actual provider result is a stream. Gaps are harmless; IDs never
        // exceed JavaScript's exact integer range.
        let stream_handle = self
            .next_stream
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |id| {
                (id < (1u64 << 53) - 1).then_some(id + 1)
            })
            .map_err(|_| CoreError::Invalid("UI handle space exhausted".into()))?;

        let dispatch = self.invoker.call_ui_final(
            &context,
            &invocation,
            &preparation,
            &arguments_digest,
            owned_args,
            FinalOutputKind::UiInvokeValue { stream_handle },
        );
        tokio::pin!(dispatch);
        let mut watch_external = true;
        let mut watch_context = true;
        let mut watch_deadline = true;
        let output = loop {
            tokio::select! {
                _ = cancel.cancelled(), if watch_external => {
                    watch_external = false;
                    context.cancel().cancel();
                }
                _ = context.cancel().cancelled(), if watch_context => {
                    watch_context = false;
                }
                _ = tokio::time::sleep_until(context.deadline().into()), if watch_deadline => {
                    watch_deadline = false;
                    context.cancel().cancel();
                }
                result = &mut dispatch => break result?,
            }
        };

        match output {
            FinalPortOutput::Value(buffer) => Ok(buffer),
            FinalPortOutput::Stream { envelope, stream } => {
                let mut candidate = Some(stream);
                let inserted = {
                    let sessions = self
                        .ui
                        .lock()
                        .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
                    let mut registry = self
                        .streams
                        .lock()
                        .map_err(|_| CoreError::Runtime("UI stream registry poisoned".into()))?;
                    let still_attached = sessions.get(session).is_some_and(|principal| {
                        principal.host_session == *host_session
                            && principal.generation == Some(generation)
                            && principal.receiver == *receiver
                    });
                    if !still_attached
                        || registry.len() >= UI_STREAMS_TOTAL
                        || registry
                            .values()
                            .filter(|entry| &entry.ui == session)
                            .count()
                            >= UI_STREAMS_PER_FRAME
                        || context.cancel().is_cancelled()
                        || cancel.is_cancelled()
                    {
                        false
                    } else {
                        registry.insert(
                            stream_handle,
                            Arc::new(UiStreamEntry {
                                ui: session.clone(),
                                cx: context.clone(),
                                external_cancel: cancel.clone(),
                                state: tokio::sync::Mutex::new(Some(UiStreamState {
                                    stream: candidate.take().expect("new stream"),
                                    output_bytes: 0,
                                    pending_buffer: None,
                                })),
                            }),
                        );
                        true
                    }
                };
                if !inserted {
                    let source = UiStreamState {
                        stream: candidate.expect("unregistered stream"),
                        output_bytes: 0,
                        pending_buffer: None,
                    };
                    self.finish_ui_stream(source).await?;
                    return Err(CoreError::Invalid(
                        "UI stream capacity exceeded or attachment closed".into(),
                    ));
                }
                Ok(envelope)
            }
        }
    }
    fn stream_entry(&self, ui: &UiSessionId, handle: u64) -> Result<Arc<UiStreamEntry>, CoreError> {
        let entry = self
            .streams
            .lock()
            .map_err(|_| CoreError::Runtime("UI stream registry poisoned".into()))?
            .get(&handle)
            .cloned()
            .ok_or_else(|| CoreError::Invalid("unknown UI stream handle".into()))?;
        if &entry.ui != ui {
            return Err(CoreError::Invalid(
                "UI stream belongs to another frame".into(),
            ));
        }
        Ok(entry)
    }
    /// Compatibility surface for callers that still request materialized
    /// CanonValue batches. Received UI streams use the producer-encoded
    /// CoreBuffer path below so guest output never crosses this Vec boundary.
    pub async fn ui_stream_pull(
        &self,
        _ui: &UiSessionId,
        _handle: u64,
        _max: u32,
    ) -> Result<Vec<CanonValue>, CoreError> {
        Err(CoreError::Step(StepError::new(
            "unavailable",
            "UI stream output requires the fixed CoreBuffer pull path",
        )))
    }

    /// Pull a producer-encoded StrictV1 batch for the trusted host transport.
    /// This path preserves CoreBuffer ownership through the bounded host copy
    /// and never materializes guest stream frames as CanonValue/Vec values.
    pub async fn ui_stream_pull_buffer(
        &self,
        ui: &UiSessionId,
        handle: u64,
        max: u32,
    ) -> Result<CoreBuffer, CoreError> {
        if max == 0 || max > UI_STREAM_MAX_BATCH {
            return Err(CoreError::Invalid("UI pull bound must be in 1..=32".into()));
        }
        self.ensure_started().await?;
        let entry = self.stream_entry(ui, handle)?;
        if self.check_snapshot().is_err()
            || entry.cx.check_active().is_err()
            || entry.external_cancel.is_cancelled()
        {
            self.ui_stream_cancel(ui, handle).await?;
            return Err(CoreError::Invalid("UI stream authority expired".into()));
        }
        let mut state = entry.state.lock().await;
        let source = state
            .as_mut()
            .ok_or_else(|| CoreError::Invalid("UI stream closed".into()))?;
        let result = if let Some(buffer) = source.pending_buffer.take() {
            Ok(buffer)
        } else {
            tokio::select! {
                _=entry.external_cancel.cancelled()=>{entry.cx.cancel().cancel();Err(StepError::new("cancelled","UI request cancelled"))},
                _=entry.cx.cancel().cancelled()=>Err(StepError::new("cancelled","UI stream cancelled")),
                _=tokio::time::sleep_until(entry.cx.deadline().into())=>Err(StepError::new("cancelled","original UI deadline expired")),
                result=source.stream.pull_final(max)=>result,
            }
        };
        let result = match result {
            Ok(buffer) if buffer.len() > UI_STREAM_MAX_BUFFER_BYTES => Err(StepError::new(
                "batch-too-large",
                "encoded UI stream batch exceeds the host copy limit",
            )),
            Ok(buffer) if buffer.as_bytes() != b"[]" => match u64::try_from(buffer.len()) {
                Ok(bytes) => match source
                    .output_bytes
                    .checked_add(bytes)
                    .filter(|total| *total <= entry.cx.limits().output_bytes)
                {
                    Some(total) => {
                        source.output_bytes = total;
                        Ok(buffer)
                    }
                    None => Err(StepError::new(
                        "limit-exceeded",
                        "UI stream byte limit exceeded",
                    )),
                },
                Err(_) => Err(StepError::new(
                    "limit-exceeded",
                    "UI stream byte limit exceeded",
                )),
            },
            result => result,
        };
        let ended = matches!(&result, Ok(buffer) if buffer.as_bytes() == b"[]");
        if ended || result.is_err() {
            let source = state.take().expect("live stream");
            self.streams
                .lock()
                .map_err(|_| CoreError::Runtime("UI stream registry poisoned".into()))?
                .remove(&handle);
            self.finish_ui_stream(source).await?;
        }
        Ok(result?)
    }

    /// Cancel exactly one handle bound to this frame.
    pub async fn ui_stream_cancel(&self, ui: &UiSessionId, handle: u64) -> Result<(), CoreError> {
        let entry = self.stream_entry(ui, handle)?;
        entry.cx.cancel().cancel();
        self.streams
            .lock()
            .map_err(|_| CoreError::Runtime("UI stream registry poisoned".into()))?
            .remove(&handle);
        if let Some(source) = entry.state.lock().await.take() {
            self.finish_ui_stream(source).await?;
        }
        Ok(())
    }
    /// Detach a rendered frame and close every outstanding request and stream.
    pub async fn detach_ui(&self, ui: &UiSessionId) -> Result<(), CoreError> {
        let principal = self
            .ui
            .lock()
            .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?
            .remove(ui)
            .ok_or_else(|| CoreError::Invalid("unknown UI session".into()))?;
        principal.cancel.cancel();
        let entries = {
            let mut registry = self
                .streams
                .lock()
                .map_err(|_| CoreError::Runtime("UI stream registry poisoned".into()))?;
            let handles: Vec<_> = registry
                .iter()
                .filter(|(_, entry)| &entry.ui == ui)
                .map(|(id, _)| *id)
                .collect();
            handles
                .into_iter()
                .filter_map(|id| registry.remove(&id))
                .collect::<Vec<_>>()
        };
        let mut failure = None;
        for entry in entries {
            entry.cx.cancel().cancel();
            if let Some(source) = entry.state.lock().await.take()
                && let Err(error) = self.finish_ui_stream(source).await
            {
                failure.get_or_insert(error);
            }
        }
        match failure {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }
    /// Idempotently stop new work and signal every child without awaiting
    /// physical cleanup. Hosts must retain this Session and await `close`.
    pub fn request_shutdown(&self) {
        self.shutdown.cancel();
        if let Some(factory) = &self.execution_factory {
            factory.close();
        }
        self.admission.request_shutdown();
    }
    /// Stop this Session without opening a replacement revision.
    pub async fn close(&self) -> Result<(), CoreError> {
        self.request_shutdown();
        let frames: Vec<_> = self
            .ui
            .lock()
            .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?
            .keys()
            .cloned()
            .collect();
        let mut failure = None;
        if let Err(error) = self.close_user_streams().await {
            super::user_ports::record_failure(&mut failure, error);
        }
        for frame in frames {
            if let Err(error) = self.detach_ui(&frame).await {
                super::user_ports::record_failure(&mut failure, error);
            }
        }
        if let Err(error) = self.admission.close().await {
            super::user_ports::record_failure(&mut failure, error);
        }
        match failure {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }
    async fn finish_ui_stream(&self, mut source: UiStreamState) -> Result<(), CoreError> {
        // This job remains owned by the runtime if a native caller abandons its
        // close future after handle removal. An awaited close still returns all
        // cleanup/audit failures to that caller.
        let cleanup = async move { source.stream.close().await.map_err(CoreError::from) };
        let task =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.runtime.spawn(cleanup)))
                .map_err(|_| {
                    CoreError::Runtime("owning runtime could not schedule UI cleanup".into())
                })?;
        task.await
            .map_err(|_| CoreError::Runtime("owning runtime did not complete UI cleanup".into()))?
    }
}

fn ui_allocation_error(error: tobkiri_canon::AllocationError) -> CoreError {
    let (code, message) = match error {
        tobkiri_canon::AllocationError::Invalid => {
            ("invalid", "UI arguments are not valid StrictV1 data")
        }
        tobkiri_canon::AllocationError::Limit
        | tobkiri_canon::AllocationError::Overflow
        | tobkiri_canon::AllocationError::Depth => {
            ("limit-exceeded", "UI argument RuntimeValue limit exceeded")
        }
        tobkiri_canon::AllocationError::Unavailable | tobkiri_canon::AllocationError::Allocator => {
            ("unavailable", "UI source allocation is unavailable")
        }
    };
    CoreError::Step(StepError::new(code, message))
}
