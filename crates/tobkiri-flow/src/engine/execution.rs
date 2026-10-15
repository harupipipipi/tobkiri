use super::*;
use tobkiri_canon::{AllocationError, CoreValue};
use tobkiri_profile::{CallNodeKey, CallNodeKind, CodeLocation, CompiledCallWitness};

impl RunState {
    pub(super) fn execute_steps<'a>(
        &'a self,
        cx: &'a RunCx,
        flow: &'a ResolvedFlow,
        code: &'a CodeLocation,
        steps: &'a [Step],
        env: &'a mut Environment,
        next: Option<&'a NextCx<'a>>,
        depth: usize,
    ) -> BoxFuture<'a, Result<Control, StepError>> {
        Box::pin(async move {
            if depth > 64 {
                return Err(StepError::new(
                    "limit-exceeded",
                    "Flow nesting ceiling exceeded",
                ));
            }
            let mut last = CoreValue::null(&cx.allocation).map_err(allocation)?;
            let mut last_origin = None;
            for step in steps {
                let (kind, witness) = self.node_descriptor(flow, code, step)?;
                let step_cx = RunCx::at_node(
                    cx.frame.clone(),
                    cx.call.clone(),
                    FlowNodePosition {
                        flow,
                        code,
                        step: Some(step),
                        kind,
                        witness,
                    },
                    AllocationReason::RuntimeValue,
                )?;
                self.check_run(&step_cx)?;
                crate::owned_expression::charge_work(&step_cx.allocation, depth, 1)?;
                let id = step.id.as_deref().unwrap_or("anonymous");
                self.event(&step_cx, RunEventKind::StepStarted { step: id.into() })
                    .await?;
                let result = async {
                    let output = if next.is_none() {
                        let overlays = step
                            .id
                            .as_ref()
                            .and_then(|id| flow.ordered_overlays.get(id))
                            .map(Vec::as_slice)
                            .unwrap_or(&[]);
                        self.chain(
                            &step_cx,
                            flow,
                            code,
                            step,
                            overlays,
                            0,
                            env,
                            None,
                            depth + 1,
                        )
                        .await
                    } else {
                        self.execute_kind(&step_cx, flow, code, step, env, next, None, depth + 1)
                            .await
                    };
                    let control = match output {
                        Ok(value) => value,
                        Err(error) => {
                            let continued = error.code != "cancelled"
                                && step
                                    .id
                                    .as_ref()
                                    .and_then(|id| flow.document.on_error.steps.get(id))
                                    .unwrap_or(&flow.document.on_error.default)
                                    == &ErrorAction::Continue;
                            self.event(
                                &step_cx,
                                RunEventKind::StepFailed {
                                    step: id.into(),
                                    error: error.clone(),
                                    continued,
                                },
                            )
                            .await?;
                            if !continued {
                                return Err(error);
                            }
                            Control::Value(
                                CoreValue::null(&step_cx.allocation).map_err(allocation)?,
                            )
                        }
                    };
                    let control = match control {
                        Control::Reached { .. } => control,
                        Control::Value(value) => Control::Reached {
                            value,
                            origin: super::CompletionOrigin {
                                frame: step_cx.frame.clone(),
                                call: step_cx.call.clone(),
                                code: code.clone(),
                                step_address: Some(std::ptr::from_ref(step).addr()),
                            },
                            returned: false,
                        },
                        Control::Return(value) => Control::Reached {
                            value,
                            origin: super::CompletionOrigin {
                                frame: step_cx.frame.clone(),
                                call: step_cx.call.clone(),
                                code: code.clone(),
                                step_address: Some(std::ptr::from_ref(step).addr()),
                            },
                            returned: true,
                        },
                    };
                    let is_return = control.is_return();
                    last = control.value();
                    last_origin = control.origin().cloned();
                    if let Some(name) = &step.out {
                        env.insert(name, last.share(), &step_cx.allocation)?;
                    }
                    self.event(
                        &step_cx,
                        RunEventKind::StepValue {
                            step: id.into(),
                            value: last.share(),
                        },
                    )
                    .await?;
                    Ok(is_return)
                }
                .await;
                if result? {
                    return Ok(Control::Reached {
                        value: last,
                        origin: last_origin.expect("completed AST step has an origin"),
                        returned: true,
                    });
                }
            }
            Ok(match last_origin {
                Some(origin) => Control::Reached {
                    value: last,
                    origin,
                    returned: false,
                },
                None => Control::Value(last),
            })
        })
    }

    // Keep the exact frozen overlay-chain position and captured continuation.
    #[allow(clippy::too_many_arguments)]
    fn chain<'a>(
        &'a self,
        cx: &'a RunCx,
        flow: &'a ResolvedFlow,
        base_code: &'a CodeLocation,
        step: &'a Step,
        overlays: &'a [ResolvedOverlay],
        overlay_index: usize,
        env: &'a mut Environment,
        args: Option<CoreValue>,
        depth: usize,
    ) -> BoxFuture<'a, Result<Control, StepError>> {
        Box::pin(async move {
            if overlay_index < overlays.len() {
                let overlay = &overlays[overlay_index];
                let input = match args {
                    Some(value) => value,
                    None => self.step_input(cx, step, env)?,
                };
                let transition = cx.frame.enter_verified_overlay(
                    &cx.call,
                    &cx.node,
                    OverlayPosition {
                        flow,
                        step,
                        index: overlay_index,
                        overlay,
                    },
                )?;
                let overlay_frame = transition.frame;
                let continuation = transition.continuation;
                let overlay_id = overlay.owner.as_ref().map_or_else(
                    || format!("user:{}", overlay.document.id),
                    |owner| format!("{owner}/{}", overlay.document.id),
                );
                let overlay_code = match (&overlay.owner, &overlay.hash) {
                    (Some(_), Some(_)) => CodeLocation::PackOverlay { id: overlay_id },
                    (None, None) => CodeLocation::UserOverlay { id: overlay_id },
                    _ => {
                        return Err(StepError::new(
                            "invalid",
                            "overlay owner metadata is inconsistent",
                        ));
                    }
                };
                let overlay_cx =
                    RunCx::at_node(
                        overlay_frame.clone(),
                        Arc::new(overlay_frame.context().try_clone_precharged().map_err(
                            |error| StepError::new("limit-exceeded", error.to_string()),
                        )?),
                        FlowNodePosition {
                            flow,
                            code: &overlay_code,
                            step: Some(step),
                            kind: FlowNodeKind::OverlayEntry,
                            witness: None,
                        },
                        AllocationReason::RuntimeValue,
                    )?;
                let mut overlay_env = env.clone();
                overlay_env.insert("input", input.share(), &overlay_cx.allocation)?;
                let next = NextCx {
                    step,
                    overlays,
                    overlay_index: overlay_index + 1,
                    input,
                    base: cx.clone(),
                    base_code: base_code.clone(),
                    overlay_frame,
                    continuation,
                    called: std::sync::atomic::AtomicBool::new(false),
                    output: std::sync::Mutex::new(None),
                    allow_next: true,
                };
                let result = self
                    .execute_steps(
                        &overlay_cx,
                        flow,
                        &overlay_code,
                        &overlay.document.body,
                        &mut overlay_env,
                        Some(&next),
                        depth + 1,
                    )
                    .await?;
                Ok(match result {
                    Control::Return(value) => Control::Value(value),
                    Control::Value(_) => Control::Value(
                        next.output
                            .lock()
                            .map_err(|_| StepError::new("internal", "overlay state poisoned"))?
                            .clone()
                            .unwrap_or(
                                CoreValue::null(&overlay_cx.allocation).map_err(allocation)?,
                            ),
                    ),
                    Control::Reached {
                        value,
                        origin,
                        returned,
                    } => {
                        if returned || next.called.load(Ordering::SeqCst) {
                            Control::Reached {
                                value,
                                origin,
                                returned: false,
                            }
                        } else {
                            let fallback = next
                                .output
                                .lock()
                                .map_err(|_| StepError::new("internal", "overlay state poisoned"))?
                                .clone()
                                .unwrap_or(
                                    CoreValue::null(&overlay_cx.allocation).map_err(allocation)?,
                                );
                            let _ = value;
                            Control::Reached {
                                value: fallback,
                                origin,
                                returned: false,
                            }
                        }
                    }
                })
            } else {
                self.execute_kind(cx, flow, base_code, step, env, None, args, depth + 1)
                    .await
            }
        })
    }

    fn step_input(
        &self,
        cx: &RunCx,
        step: &Step,
        env: &Environment,
    ) -> Result<CoreValue, StepError> {
        match &step.kind {
            StepKind::Call(call) => evaluate(&call.args, env, &cx.allocation),
            StepKind::Flow(flow) => evaluate(&flow.args, env, &cx.allocation),
            _ => match env.get("input")? {
                Some(value) => Ok(value),
                None => CoreValue::null(&cx.allocation).map_err(allocation),
            },
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn execute_kind<'a>(
        &'a self,
        cx: &'a RunCx,
        flow: &'a ResolvedFlow,
        code: &'a CodeLocation,
        step: &'a Step,
        env: &'a mut Environment,
        next: Option<&'a NextCx<'a>>,
        args: Option<CoreValue>,
        depth: usize,
    ) -> BoxFuture<'a, Result<Control, StepError>> {
        Box::pin(async move {
            self.check_run(cx)?;
            match &step.kind {
                StepKind::Call(call) => {
                    let args = match args {
                        Some(value) => value,
                        None => evaluate(&call.args, env, &cx.allocation)?,
                    };
                    let arguments_digest = args.digest_guest(&cx.allocation).map_err(allocation)?;
                    // Admit a small initial collection before producer entry.
                    // Later reference growth is funded inside the stream's
                    // awaited cleanup scope; output bytes are not item slots.
                    let mut collected = match step
                        .stream
                        .as_ref()
                        .and_then(|route| route.collect.as_ref())
                    {
                        Some(_) => {
                            let maximum =
                                usize::try_from(cx.limits().output_bytes).map_err(|_| {
                                    StepError::new("limit-exceeded", "output bound is unsupported")
                                })?;
                            Some(
                                CoreValue::array_builder_incremental(&cx.allocation, maximum)
                                    .map_err(allocation)?,
                            )
                        }
                        None => None,
                    };
                    let output = AssertUnwindSafe(self.invoker.call_reached(
                        &cx.call,
                        &cx.frame,
                        &cx.node,
                        &arguments_digest,
                        args,
                    ))
                    .catch_unwind()
                    .await
                    .map_err(|_| {
                        StepError::new("trap", "provider panicked during a port call")
                    })??;
                    let value = match output {
                        StepOutput::Value(value) => {
                            self.output_size(cx, &value)?;
                            if step.stream.is_some() {
                                return Err(StepError::new(
                                    "invalid",
                                    "stream route requires a provider stream",
                                ));
                            }
                            value
                        }
                        StepOutput::Stream(mut source) => {
                            let Some(route) = step.stream.as_ref() else {
                                let error = StepError::new(
                                    "invalid",
                                    "provider stream requires an emit/collect route",
                                );
                                return Err(close_after_error(&mut source, error).await);
                            };
                            // Retain the actual batch owner through awaited
                            // cleanup if accepting an item or growing the
                            // collection fails after the producer has run.
                            let mut pending_batch = None;
                            let execution = async {
                                let emit_witness =
                                    self.witness(flow, code, step, CallNodeKind::StreamEmit)?;
                                loop {
                                    self.check_run(cx)?;
                                    pending_batch = Some(
                                        AssertUnwindSafe(source.pull(32))
                                            .catch_unwind()
                                            .await
                                            .map_err(|_| {
                                            StepError::new(
                                                "trap",
                                                "provider panicked during stream pull",
                                            )
                                        })??,
                                    );
                                    let values = match pending_batch
                                        .as_ref()
                                        .expect("retained producer batch")
                                        .inspect()
                                    {
                                        CoreRef::Arr(values) => values,
                                        _ => {
                                            return Err(StepError::new(
                                                "invalid",
                                                "stream batch is not an array",
                                            ));
                                        }
                                    };
                                    if values.is_empty() {
                                        break;
                                    }
                                    for index in 0..values.len() {
                                        let value = values.get(index).expect("bounded array index");
                                        self.output_size(cx, &value)?;
                                        let emit_cx = RunCx::at_node(
                                            cx.frame.clone(),
                                            cx.call.clone(),
                                            FlowNodePosition {
                                                flow,
                                                code,
                                                step: Some(step),
                                                kind: FlowNodeKind::StreamEmit,
                                                witness: Some(emit_witness),
                                            },
                                            AllocationReason::EventFrame,
                                        )?;
                                        let event_digest = value
                                            .digest_guest(&emit_cx.allocation)
                                            .map_err(allocation)?;
                                        AssertUnwindSafe(self.invoker.emit_reached(
                                            &emit_cx.call,
                                            &emit_cx.frame,
                                            &emit_cx.node,
                                            &event_digest,
                                            value.share(),
                                        ))
                                        .catch_unwind()
                                        .await
                                        .map_err(
                                            |_| StepError::new("trap", "output provider panicked"),
                                        )??;
                                        self.event(
                                            &emit_cx,
                                            RunEventKind::Emitted {
                                                shape: route.emit.clone(),
                                                value: value.share(),
                                            },
                                        )
                                        .await?;
                                        if let Some(collected) = collected.as_mut() {
                                            collected.push(value).map_err(allocation)?;
                                        }
                                    }
                                    pending_batch = None;
                                }
                                Ok::<(), StepError>(())
                            }
                            .await;
                            if let Err(error) = execution {
                                let error = close_after_error(&mut source, error).await;
                                drop(pending_batch);
                                return Err(error);
                            }
                            source.close().await?;
                            drop(pending_batch);
                            let value = match collected {
                                Some(collected) => collected.finish().map_err(allocation)?,
                                None => CoreValue::null(&cx.allocation).map_err(allocation)?,
                            };
                            if let Some(name) = &route.collect {
                                env.insert(name, value.share(), &cx.allocation)?;
                            }
                            value
                        }
                    };
                    Ok(Control::Value(value))
                }
                StepKind::Flow(subflow) => {
                    let input = match args {
                        Some(value) => value,
                        None => evaluate(&subflow.args, env, &cx.allocation)?,
                    };
                    let args_digest = input.digest_guest(&cx.allocation).map_err(allocation)?;
                    let runner = self.runner.get().and_then(Weak::upgrade).ok_or_else(|| {
                        StepError::new("unavailable", "Core Flow runner is unavailable")
                    })?;
                    let mut stream = if let Some(compiled) = &self.compiled {
                        let mut matching = compiled.flows.iter().filter(|(_, candidate)| {
                            candidate
                                .document
                                .implements
                                .as_ref()
                                .is_some_and(|implemented| {
                                    implemented.name == subflow.implements.name
                                        && implemented.version.major
                                            == subflow.implements.version.major
                                })
                        });
                        let (key, resolved) = matching.next().ok_or_else(|| {
                            StepError::new(
                                "not-found",
                                "compiled subflow implements binding is missing",
                            )
                        })?;
                        if matching.next().is_some() {
                            return Err(StepError::new(
                                "conflict",
                                "compiled subflow implements binding is ambiguous",
                            ));
                        }
                        if resolved.document.implements.is_none() {
                            return Err(StepError::new(
                                "invalid",
                                "compiled subflow has no exact implements target",
                            ));
                        }
                        runner
                            .run_provided_flow_canonical(
                                &cx.call,
                                &cx.frame,
                                &cx.node,
                                key,
                                &args_digest,
                                input,
                            )
                            .await?
                    } else {
                        let mut matching = self.legacy_catalog.values().filter(|candidate| {
                            candidate.get().document.implements.as_ref().is_some_and(
                                |implemented| {
                                    implemented.name == subflow.implements.name
                                        && implemented.version.major
                                            == subflow.implements.version.major
                                },
                            )
                        });
                        let resolved = matching.next().cloned().ok_or_else(|| {
                            StepError::new(
                                "not-found",
                                "compiled subflow implements binding is missing",
                            )
                        })?;
                        if matching.next().is_some() {
                            return Err(StepError::new(
                                "conflict",
                                "compiled subflow implements binding is ambiguous",
                            ));
                        }
                        runner
                            .run_provided_flow(&cx.call, &cx.frame, &cx.node, resolved, input)
                            .await?
                    };
                    let execution = async {
                        let mut result = None;
                        loop {
                            self.check_run(cx)?;
                            let batch = stream.pull(32).await?;
                            let frames = match batch.inspect() {
                                CoreRef::Arr(frames) => frames,
                                _ => {
                                    return Err(StepError::new(
                                        "invalid",
                                        "subflow batch is not an array",
                                    ));
                                }
                            };
                            if frames.is_empty() {
                                break;
                            }
                            for index in 0..frames.len() {
                                let frame = frames.get(index).expect("bounded array index");
                                let event = RunEvent::from_value(&frame)?;
                                match event.kind {
                                    RunEventKind::Completed { value, .. } => {
                                        if result.replace(value).is_some() {
                                            return Err(StepError::new(
                                                "invalid",
                                                "subflow returned multiple completions",
                                            ));
                                        }
                                    }
                                    RunEventKind::Emitted { shape, value } => {
                                        self.event(cx, RunEventKind::Emitted { shape, value })
                                            .await?;
                                    }
                                    RunEventKind::Failed { error } => return Err(error),
                                    RunEventKind::Cancelled { .. } => {
                                        return Err(StepError::new(
                                            "cancelled",
                                            "subflow cancelled",
                                        ));
                                    }
                                    _ => {}
                                }
                            }
                        }
                        Ok::<_, StepError>(Control::Value(result.ok_or_else(|| {
                            StepError::new("invalid", "subflow ended without a completion frame")
                        })?))
                    }
                    .await;
                    let cleanup = stream.close().await;
                    match (execution, cleanup) {
                        (Err(error), Err(cleanup)) => Err(StepError::new(
                            error.code,
                            format!(
                                "{}; nested flow cleanup failed: {}",
                                error.message,
                                cleanup.audit_code()
                            ),
                        )),
                        (Err(error), _) => Err(error),
                        (Ok(_), Err(error)) => Err(error),
                        (Ok(value), Ok(())) => Ok(value),
                    }
                }
                StepKind::Branch(branch) => {
                    let condition = evaluate(&branch.condition, env, &cx.allocation)?;
                    let steps = if boolean(&condition)? {
                        &branch.then
                    } else {
                        &branch.otherwise
                    };
                    self.execute_steps(cx, flow, code, steps, env, next, depth + 1)
                        .await
                }
                StepKind::Parallel(parallel) => {
                    if parallel.max == 0 {
                        return Err(StepError::new("invalid", "parallel bound must be positive"));
                    }
                    let items = evaluate(&parallel.for_each, env, &cx.allocation)?;
                    let values = match items.inspect() {
                        CoreRef::Arr(values) => values,
                        _ => {
                            return Err(StepError::new(
                                "invalid",
                                "parallel for_each must be an array",
                            ));
                        }
                    };
                    let count = values.len();
                    let mut results = CoreValue::array_builder_slots(&cx.allocation, count)
                        .map_err(allocation)?;
                    let original = env.clone();
                    let pending = stream::iter(0..count)
                        .map(|index| {
                            let item = values.get(index).expect("bounded array index");
                            let mut child = original.clone();
                            let inserted = child.insert(&parallel.binding, item, &cx.allocation);
                            let branch_next = next.map(|parent| NextCx {
                                step: parent.step,
                                overlays: parent.overlays,
                                overlay_index: parent.overlay_index,
                                input: parent.input.share(),
                                base: parent.base.clone(),
                                base_code: parent.base_code.clone(),
                                overlay_frame: parent.overlay_frame.clone(),
                                continuation: parent.continuation.clone(),
                                called: std::sync::atomic::AtomicBool::new(false),
                                output: std::sync::Mutex::new(None),
                                allow_next: false,
                            });
                            async move {
                                inserted?;
                                let value = self
                                    .execute_steps(
                                        cx,
                                        flow,
                                        code,
                                        &parallel.body,
                                        &mut child,
                                        branch_next.as_ref(),
                                        depth + 1,
                                    )
                                    .await?
                                    .value();
                                Ok::<_, StepError>((index, value))
                            }
                        })
                        .buffer_unordered(parallel.max as usize);
                    futures::pin_mut!(pending);
                    while let Some(item) = pending.next().await {
                        let (index, value) = item?;
                        results.set(index, value).map_err(allocation)?;
                    }
                    Ok(Control::Value(results.finish().map_err(allocation)?))
                }
                StepKind::Loop(repeat) => {
                    let limits = repeat.limits.as_ref().unwrap_or(&self.limits);
                    if !limits.validate() || !limits.is_subset_of(&self.limits) {
                        return Err(StepError::new(
                            "invalid",
                            "loop may only narrow Profile ceilings",
                        ));
                    }
                    let parent = cx.budget().ok_or_else(|| {
                        StepError::new("unavailable", "Flow execution budget is missing")
                    })?;
                    let (nested, activation) = parent
                        .enter_loop(limits.clone())
                        .map_err(|error| StepError::new(error.code(), error.to_string()))?;
                    let loop_call =
                        Arc::new(cx.call.try_clone_with_budget_precharged(nested).map_err(
                            |error| StepError::new(error.code(), "loop budget inheritance failed"),
                        )?);
                    let (loop_kind, loop_witness) = self.node_descriptor(flow, code, step)?;
                    let mut last = CoreValue::null(&cx.allocation).map_err(allocation)?;
                    loop {
                        let condition = evaluate(&repeat.condition, env, &cx.allocation)?;
                        if !boolean(&condition)? {
                            break;
                        }
                        activation
                            .next_iteration()
                            .map_err(|error| StepError::new(error.code(), error.to_string()))?;
                        let loop_cx = RunCx::at_node(
                            cx.frame.clone(),
                            loop_call.clone(),
                            FlowNodePosition {
                                flow,
                                code,
                                step: Some(step),
                                kind: loop_kind,
                                witness: loop_witness,
                            },
                            AllocationReason::RuntimeValue,
                        )?;
                        self.check_run(&loop_cx)?;
                        let result = tokio::time::timeout_at(
                            loop_cx.deadline().into(),
                            self.execute_steps(
                                &loop_cx,
                                flow,
                                code,
                                &repeat.body,
                                env,
                                next,
                                depth + 1,
                            ),
                        )
                        .await
                        .map_err(|_| {
                            StepError::new("limit-exceeded", "loop wall ceiling exceeded")
                        })??;
                        if result.is_return() {
                            return Ok(result);
                        }
                        last = result.value();
                    }
                    Ok(Control::Value(last))
                }
                StepKind::Return(value) => {
                    if next.is_none() {
                        return Err(StepError::new(
                            "invalid",
                            "return is only valid inside an overlay",
                        ));
                    }
                    let value = evaluate(value, env, &cx.allocation)?;
                    if let Some(ok) = value.get_key("ok") {
                        if value.object().is_some_and(|fields| fields.len() == 1) {
                            return Ok(Control::Return(ok));
                        }
                    }
                    if let Some(error) = value.get_key("error") {
                        if value.object().is_some_and(|fields| fields.len() == 1) {
                            if error.object().is_none() {
                                return Err(StepError::new(
                                    "invalid",
                                    "return error needs an object",
                                ));
                            }
                            let code_value = error.get_key("code");
                            let code = code_value
                                .as_ref()
                                .and_then(|value| value.as_str())
                                .unwrap_or("internal");
                            // An overlay may return an error object assembled from
                            // guest values. Never lift those strings into ordinary
                            // Rust-owned StepError fields; preserve only a closed
                            // host-defined code and its static message.
                            return Err(sanitized_overlay_error(code));
                        }
                    }
                    Err(StepError::new(
                        "invalid",
                        "overlay return requires ok or error",
                    ))
                }
                StepKind::Next(spec) => {
                    let next = next.ok_or_else(|| {
                        StepError::new("invalid", "next is only valid inside an overlay")
                    })?;
                    if !next.allow_next {
                        return Err(StepError::new(
                            "invalid",
                            "next cannot be shared across parallel overlay branches",
                        ));
                    }
                    if next.called.swap(true, Ordering::SeqCst) {
                        return Err(StepError::new(
                            "invalid",
                            "an overlay may call next only once",
                        ));
                    }
                    let input = evaluate(&spec.args, env, &cx.allocation)?;
                    let mut child_env = env.clone();
                    child_env.insert("input", next.input.share(), &next.base.allocation)?;
                    let resumed = next.overlay_frame.resume_captured_base(
                        &cx.call,
                        &cx.node,
                        &next.continuation,
                    )?;
                    let (base_kind, base_witness) =
                        self.node_descriptor(flow, &next.base_code, next.step)?;
                    let resumed_base = RunCx::at_node(
                        resumed.clone(),
                        Arc::new(resumed.context().try_clone_precharged().map_err(|error| {
                            StepError::new("limit-exceeded", error.to_string())
                        })?),
                        FlowNodePosition {
                            flow,
                            code: &next.base_code,
                            step: Some(next.step),
                            kind: base_kind,
                            witness: base_witness,
                        },
                        AllocationReason::RuntimeValue,
                    )?;
                    let output = self
                        .chain(
                            &resumed_base,
                            flow,
                            &next.base_code,
                            next.step,
                            next.overlays,
                            next.overlay_index,
                            &mut child_env,
                            Some(input),
                            depth + 1,
                        )
                        .await?;
                    *next
                        .output
                        .lock()
                        .map_err(|_| StepError::new("internal", "overlay state poisoned"))? =
                        Some(output.clone().value());
                    Ok(output)
                }
            }
        })
    }

    fn witness<'a>(
        &self,
        flow: &'a ResolvedFlow,
        code: &CodeLocation,
        step: &Step,
        kind: CallNodeKind,
    ) -> Result<&'a tobkiri_profile::CompiledCallWitness, StepError> {
        let node = step
            .id
            .as_ref()
            .ok_or_else(|| StepError::new("invalid", "compiled call node has no stable ID"))?;
        flow.call_witnesses
            .get(&CallNodeKey {
                code: code.clone(),
                node: node.clone(),
                kind,
            })
            .ok_or_else(|| {
                StepError::new("unavailable", "Core compiled-call descriptor is missing")
            })
    }

    fn node_descriptor<'a>(
        &self,
        flow: &'a ResolvedFlow,
        code: &CodeLocation,
        step: &'a Step,
    ) -> Result<(FlowNodeKind, Option<&'a CompiledCallWitness>), StepError> {
        match &step.kind {
            StepKind::Call(_) => Ok((
                FlowNodeKind::PortCall,
                Some(self.witness(flow, code, step, CallNodeKind::Call)?),
            )),
            StepKind::Flow(_) => Ok((FlowNodeKind::ProvidedFlow, None)),
            _ => Ok((FlowNodeKind::Step, None)),
        }
    }
}

fn sanitized_overlay_error(code: &str) -> StepError {
    let (code, message) = match code {
        "denied" => ("denied", "operation denied"),
        "approval-required" => ("approval-required", "operation requires approval"),
        "cancelled" => ("cancelled", "operation cancelled"),
        "not-found" => ("not-found", "requested item not found"),
        "invalid" => ("invalid", "operation input is invalid"),
        "limit-exceeded" => ("limit-exceeded", "execution limit exceeded"),
        "unavailable" => ("unavailable", "operation is unavailable"),
        "trap" => ("trap", "provider trapped"),
        "blocked" => ("blocked", "operation blocked"),
        "conflict" => ("conflict", "operation conflicts with current state"),
        _ => ("internal", "overlay returned an error"),
    };
    StepError::new(code, message)
}

fn allocation(error: AllocationError) -> StepError {
    StepError::new(
        match error {
            AllocationError::Limit | AllocationError::Overflow | AllocationError::Depth => {
                "limit-exceeded"
            }
            AllocationError::Invalid => "invalid",
            _ => "unavailable",
        },
        "Flow value allocation failed",
    )
}

async fn close_after_error(source: &mut CoreStream, error: StepError) -> StepError {
    match source.close().await {
        Ok(()) => error,
        Err(cleanup) => StepError::new(
            error.code,
            format!(
                "{}; stream cleanup failed: {}",
                error.message,
                cleanup.audit_code()
            ),
        ),
    }
}
