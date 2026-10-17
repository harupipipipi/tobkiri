use super::*;
use std::{collections::VecDeque, sync::atomic::AtomicUsize};
use tobkiri_flow::StreamSource;
use tobkiri_gate::{CoreControl, ResourceLimits};

#[derive(Default)]
struct Budget(AtomicU64);
impl AllocationBudget for Budget {
    fn reserve(&self, value: AllocationRequest) -> Result<(), AllocationError> {
        self.0.fetch_add(value.bytes, Ordering::SeqCst);
        Ok(())
    }
    fn release(&self, bytes: u64) {
        self.0.fetch_sub(bytes, Ordering::SeqCst);
    }
}
struct FixtureSource {
    budget: Arc<Budget>,
    batches: VecDeque<&'static [u8]>,
    close_calls: Arc<AtomicUsize>,
    close_error: Option<StepError>,
    pull_error: Option<StepError>,
    paused: Option<Arc<Pause>>,
}
#[derive(Default)]
struct Pause {
    entered: AtomicBool,
    release: Notify,
}
#[async_trait::async_trait]
impl StreamSource for FixtureSource {
    async fn pull(&mut self, _: u32) -> Result<CoreValue, StepError> {
        panic!("native host must use producer-funded pull_final")
    }
    async fn pull_final(&mut self, _: u32) -> Result<CoreBuffer, StepError> {
        if let Some(error) = &self.pull_error {
            return Err(error.clone());
        }
        let budget: Arc<dyn AllocationBudget> = self.budget.clone();
        let buffer =
            CoreBuffer::copy_from(self.batches.pop_front().unwrap_or(b"[]"), &budget).unwrap();
        if let Some(pause) = self.paused.take() {
            pause.entered.store(true, Ordering::Release);
            pause.release.notified().await;
        }
        Ok(buffer)
    }
    fn cancel(&mut self) {}
    async fn close(&mut self) -> Result<(), StepError> {
        self.close_calls.fetch_add(1, Ordering::SeqCst);
        self.close_error.clone().map_or(Ok(()), Err)
    }
}
fn context(control: &CoreControl, duration: Duration) -> CallCx {
    CallCx::try_root_with_control(
        tobkiri_canon::ids::ProfileId::parse("prof_user_stream").unwrap(),
        Principal::User,
        RequestId::parse("req_native_user").unwrap(),
        CancellationToken::new_with_control(control).unwrap(),
        Instant::now() + duration,
        ResourceLimits {
            fuel_per_call: 1_000_000,
            memory_mib: 8,
            wall_ms: 30_000,
            table_elements: 1024,
            output_bytes: 1024 * 1024,
        },
        control.clone(),
    )
    .unwrap()
}
async fn fixture(duration: Duration) -> (Arc<Registry>, Arc<Entry>, Arc<Budget>, Arc<AtomicUsize>) {
    let control = CoreControl::default();
    let registry = Arc::new(Registry::default());
    let entry = registry
        .reserve(context(&control, duration), TokioCancellationToken::new())
        .unwrap();
    let budget = Arc::new(Budget::default());
    let closes = Arc::new(AtomicUsize::new(0));
    *entry.state.lock().await = State::Live(Source {
        stream: CoreStream::new(FixtureSource {
            budget: budget.clone(),
            batches: VecDeque::from([br#"[{"verified":true}]"#.as_slice(), b"[]".as_slice()]),
            close_calls: closes.clone(),
            close_error: None,
            pull_error: None,
            paused: None,
        }),
        output_bytes: 0,
        cleanup_error: None,
    });
    entry.ready.notify_waiters();
    (registry, entry, budget, closes)
}
async fn until(mut predicate: impl FnMut() -> bool) {
    tokio::time::timeout(Duration::from_secs(5), async {
        while !predicate() {
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn registry_stream_eof_closes_once_and_returned_buffer_keeps_its_producer() {
    let (registry, entry, budget, closes) = fixture(Duration::from_secs(30)).await;
    let value = pull_entry(registry.clone(), entry.clone(), 8)
        .await
        .unwrap();
    assert_eq!(value.as_bytes(), br#"[{"verified":true}]"#);
    assert!(budget.0.load(Ordering::SeqCst) > 0);
    let end = pull_entry(registry.clone(), entry.clone(), 8)
        .await
        .unwrap();
    assert_eq!(end.as_bytes(), b"[]");
    assert_eq!(closes.load(Ordering::SeqCst), 1);
    assert!(registry.snapshot().unwrap().is_empty());
    assert!(budget.0.load(Ordering::SeqCst) > 0);
    drop((value, end));
    assert_eq!(budget.0.load(Ordering::SeqCst), 0);
}
#[tokio::test]
async fn registry_handles_are_session_bound_and_capacity_is_reserved_before_dispatch() {
    let (registry, entry, _, _) = fixture(Duration::from_secs(30)).await;
    let handle = UserStreamHandle {
        session: registry.identity.clone(),
        id: entry.id,
    };
    let other = Registry::default();
    assert!(other.entry(&handle).is_err());
    while registry.snapshot().unwrap().len() < MAX_STREAMS {
        registry
            .reserve(entry.cx.clone(), TokioCancellationToken::new())
            .unwrap();
    }
    assert!(
        registry
            .reserve(entry.cx.clone(), TokioCancellationToken::new())
            .is_err()
    );
    // Preparing fixtures have no native dispatch; settle their reserved slots.
    for item in registry.snapshot().unwrap() {
        *item.state.lock().await = State::Finished;
        item.ready.notify_waiters();
        close_entry(registry.clone(), item).await.unwrap();
    }
}
#[tokio::test]
async fn registry_expired_stream_drops_batch_and_closes_its_actual_source() {
    let (registry, entry, budget, closes) = fixture(Duration::from_millis(1)).await;
    tokio::time::sleep(Duration::from_millis(3)).await;
    let error = pull_entry(registry.clone(), entry, 8).await.unwrap_err();
    assert_eq!(error.code, "deadline");
    assert_eq!(closes.load(Ordering::SeqCst), 1);
    assert_eq!(budget.0.load(Ordering::SeqCst), 0);
    assert!(registry.snapshot().unwrap().is_empty());
}
#[tokio::test]
async fn registry_abandoned_pull_retains_buffer_and_registration_until_worker_cleanup() {
    let control = CoreControl::default();
    let registry = Arc::new(Registry::default());
    let entry = registry
        .reserve(
            context(&control, Duration::from_secs(30)),
            TokioCancellationToken::new(),
        )
        .unwrap();
    let pause = Arc::new(Pause::default());
    let budget = Arc::new(Budget::default());
    let closes = Arc::new(AtomicUsize::new(0));
    *entry.state.lock().await = State::Live(Source {
        stream: CoreStream::new(FixtureSource {
            budget: budget.clone(),
            batches: VecDeque::from([b"[1]".as_slice()]),
            close_calls: closes.clone(),
            close_error: None,
            pull_error: None,
            paused: Some(pause.clone()),
        }),
        output_bytes: 0,
        cleanup_error: None,
    });
    let weak = Arc::downgrade(&entry);
    let worker = tokio::spawn(pull_entry(registry.clone(), entry.clone(), 8));
    let guard = RequestGuard {
        cx: entry.cx.clone(),
        cleanup: Some((
            registry.clone(),
            entry.clone(),
            tokio::runtime::Handle::current(),
        )),
        armed: true,
    };
    let host = tokio::spawn(async move {
        let _guard = guard;
        worker.await
    });
    drop(entry);
    until(|| pause.entered.load(Ordering::Acquire)).await;
    host.abort();
    assert!(host.await.unwrap_err().is_cancelled());
    until(|| {
        weak.upgrade()
            .is_some_and(|entry| entry.cx.cancel().is_cancelled())
    })
    .await;
    assert!(budget.0.load(Ordering::SeqCst) > 0);
    assert_eq!(control.usage().count(ControlKind::Registration), 1);
    assert_eq!(closes.load(Ordering::SeqCst), 0);
    pause.release.notify_one();
    until(|| weak.upgrade().is_none()).await;
    assert_eq!(budget.0.load(Ordering::SeqCst), 0);
    assert_eq!(closes.load(Ordering::SeqCst), 1);
    assert_eq!(control.usage().count(ControlKind::Registration), 0);
}
#[tokio::test]
async fn registry_failed_close_retains_original_source_and_does_not_retry_it() {
    let (registry, entry, _, _) = fixture(Duration::from_secs(30)).await;
    let closes = Arc::new(AtomicUsize::new(0));
    *entry.state.lock().await = State::Live(Source {
        stream: CoreStream::new(FixtureSource {
            budget: Arc::new(Budget::default()),
            batches: VecDeque::new(),
            close_calls: closes.clone(),
            close_error: Some(StepError::new("unavailable", "injected unknown close")),
            pull_error: None,
            paused: None,
        }),
        output_bytes: 0,
        cleanup_error: None,
    });
    assert_eq!(
        close_entry(registry.clone(), entry.clone())
            .await
            .unwrap_err()
            .code,
        "unavailable"
    );
    assert!(entry.retained.load(Ordering::Acquire));
    assert_eq!(registry.snapshot().unwrap().len(), 1);
    assert_eq!(
        close_entry(registry, entry).await.unwrap_err().code,
        "unavailable"
    );
    assert_eq!(closes.load(Ordering::SeqCst), 1);
}
#[tokio::test]
async fn registry_filesystem_unknown_survives_cancel_and_cleanup_failure() {
    let (registry, entry, _, _) = fixture(Duration::from_secs(30)).await;
    let closes = Arc::new(AtomicUsize::new(0));
    *entry.state.lock().await = State::Live(Source {
        stream: CoreStream::new(FixtureSource {
            budget: Arc::new(Budget::default()),
            batches: VecDeque::new(),
            close_calls: closes.clone(),
            close_error: Some(StepError::new("cancelled", "injected cleanup cancellation")),
            pull_error: Some(StepError::new(
                "filesystem-commit-unknown",
                "original unknown effect",
            )),
            paused: None,
        }),
        output_bytes: 0,
        cleanup_error: None,
    });
    let error = pull_entry(registry.clone(), entry.clone(), 8)
        .await
        .unwrap_err();
    assert_eq!(error.code, "filesystem-commit-unknown");
    assert_eq!(closes.load(Ordering::SeqCst), 1);
    let closed = closes.load(Ordering::SeqCst);
    assert_eq!(
        close_entry(registry, entry).await.unwrap_err().code,
        "filesystem-commit-unknown"
    );
    assert_eq!(closes.load(Ordering::SeqCst), closed);
    let control = CoreControl::default();
    let cx = context(&control, Duration::from_secs(30));
    cx.note_filesystem_mutation();
    cx.cancel().cancel();
    assert_eq!(
        interruption(&cx, "normal cancellation").code,
        "filesystem-commit-unknown"
    );
}

#[tokio::test]
async fn registry_sticky_effect_cannot_become_successful_eof_or_plain_cleanup_error() {
    for (bound, close_error) in [(false, None), (true, None), (false, Some("cancelled"))] {
        let (registry, entry, budget, closes) = fixture(Duration::from_secs(30)).await;
        entry.cx.note_filesystem_mutation();
        *entry.state.lock().await = State::Live(Source {
            stream: CoreStream::new(FixtureSource {
                budget: budget.clone(),
                batches: VecDeque::new(),
                close_calls: closes.clone(),
                close_error: close_error
                    .map(|code| StepError::new(code, "fixture cleanup failure")),
                pull_error: None,
                paused: None,
            }),
            output_bytes: if bound {
                entry.cx.limits().output_bytes
            } else {
                0
            },
            cleanup_error: None,
        });
        let error = pull_entry(registry.clone(), entry.clone(), 1)
            .await
            .unwrap_err();
        assert_eq!(error.code, "filesystem-commit-unknown");
        assert_eq!(closes.load(Ordering::SeqCst), 1);
        assert!(entry.retained.load(Ordering::Acquire));
        assert_eq!(registry.snapshot().unwrap().len(), 1);
        assert_eq!(
            close_entry(registry, entry).await.unwrap_err().code,
            "filesystem-commit-unknown"
        );
        assert_eq!(closes.load(Ordering::SeqCst), 1);
        assert_eq!(budget.0.load(Ordering::SeqCst), 0);
    }
    let (registry, entry, _, closes) = fixture(Duration::from_secs(30)).await;
    entry.cx.note_filesystem_mutation();
    if let State::Live(source) = &mut *entry.state.lock().await {
        source.stream = CoreStream::new(FixtureSource {
            budget: Arc::new(Budget::default()),
            batches: VecDeque::new(),
            close_calls: closes.clone(),
            close_error: Some(StepError::new("unavailable", "fixture cleanup failure")),
            pull_error: None,
            paused: None,
        });
    }
    assert_eq!(
        close_entry(registry, entry).await.unwrap_err().code,
        "filesystem-commit-unknown"
    );
    assert_eq!(closes.load(Ordering::SeqCst), 1);
}

#[test]
fn registry_cleanup_prefers_typed_unknown_while_preserving_other_failures() {
    let mut failure = Some(unavailable("ordinary cleanup failure"));
    record_failure(
        &mut failure,
        CoreError::Step(StepError::new(
            "filesystem-commit-unknown",
            "uncertain effect",
        )),
    );
    record_failure(&mut failure, unavailable("later cleanup failure"));
    let error = failure.unwrap();
    assert!(is_filesystem_unknown(&error));
    let CoreError::Cleanup { original, .. } = &error else {
        panic!("combined failure expected")
    };
    assert!(is_filesystem_unknown(original));
    let text = error.to_string();
    assert!(text.contains("ordinary cleanup failure"));
    assert!(text.contains("later cleanup failure"));
}

#[tokio::test]
async fn registry_cleanup_task_is_admitted_before_spawn_and_holds_capacity_until_ack() {
    let control = CoreControl::new(tobkiri_gate::CoreControlLimits {
        max_tasks: 1,
        ..Default::default()
    })
    .unwrap();
    let registry = Arc::new(Registry::default());
    let entry = registry
        .reserve(
            context(&control, Duration::from_secs(30)),
            TokioCancellationToken::new(),
        )
        .unwrap();
    let first = schedule_close(
        registry.clone(),
        entry.clone(),
        &tokio::runtime::Handle::current(),
    )
    .unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert!(
        schedule_close(
            registry.clone(),
            entry.clone(),
            &tokio::runtime::Handle::current()
        )
        .is_err()
    );
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    *entry.state.lock().await = State::Finished;
    entry.ready.notify_waiters();
    first.await.unwrap().unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    assert_eq!(control.usage().count(ControlKind::Registration), 1);
    drop(entry);
    assert_eq!(control.usage().count(ControlKind::Registration), 0);
}

struct DropSource(Arc<AtomicUsize>);
impl Drop for DropSource {
    fn drop(&mut self) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}
#[async_trait::async_trait]
impl StreamSource for DropSource {
    async fn pull(&mut self, _: u32) -> Result<CoreValue, StepError> {
        unreachable!()
    }
    fn cancel(&mut self) {}
}
struct PanickingSource {
    calls: Arc<AtomicUsize>,
    drops: Arc<AtomicUsize>,
}
impl Drop for PanickingSource {
    fn drop(&mut self) {
        self.drops.fetch_add(1, Ordering::SeqCst);
    }
}
#[async_trait::async_trait]
impl StreamSource for PanickingSource {
    async fn pull(&mut self, _: u32) -> Result<CoreValue, StepError> {
        unreachable!()
    }
    async fn pull_final(&mut self, _: u32) -> Result<CoreBuffer, StepError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        panic!("fixture producer trap");
    }
    fn cancel(&mut self) {}
    async fn close(&mut self) -> Result<(), StepError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}

#[tokio::test]
async fn registry_stopped_pull_supervisor_cannot_replay_a_live_source() {
    let (registry, entry, _, _) = fixture(Duration::from_secs(30)).await;
    let calls = Arc::new(AtomicUsize::new(0));
    let drops = Arc::new(AtomicUsize::new(0));
    *entry.state.lock().await = State::Live(Source {
        stream: CoreStream::new(PanickingSource {
            calls: calls.clone(),
            drops: drops.clone(),
        }),
        output_bytes: 0,
        cleanup_error: None,
    });
    let worker_registry = registry.clone();
    let target = entry.clone();
    let retention = RetainOnAbandon {
        entry: entry.clone(),
        finished: false,
    };
    let task = tokio::spawn(async move {
        let mut retention = retention;
        let result = pull_entry(worker_registry, target, 1).await;
        retention.finished = true;
        result
    });
    assert!(task.await.is_err());
    assert!(entry.supervisor_failed.load(Ordering::Acquire));
    assert_eq!(
        pull_entry(registry.clone(), entry.clone(), 1)
            .await
            .unwrap_err()
            .code,
        "unavailable"
    );
    assert_eq!(
        close_entry(registry, entry).await.unwrap_err().code,
        "unavailable"
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(drops.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn registry_abandoned_publication_keeps_unpublished_actual_stream_and_producer() {
    let control = CoreControl::default();
    let registry = Arc::new(Registry::default());
    let entry = registry
        .reserve(
            context(&control, Duration::from_secs(30)),
            TokioCancellationToken::new(),
        )
        .unwrap();
    let state = entry.state.lock().await;
    let budget = Arc::new(Budget::default());
    let producer: Arc<dyn AllocationBudget> = budget.clone();
    let drops = Arc::new(AtomicUsize::new(0));
    store_completed(
        &entry,
        Ok(FinalPortOutput::Stream {
            envelope: CoreBuffer::copy_from(b"{}", &producer).unwrap(),
            stream: CoreStream::new(DropSource(drops.clone())),
        }),
    );
    let target = entry.clone();
    let worker_registry = registry.clone();
    let retention = RetainOnAbandon {
        entry: entry.clone(),
        finished: false,
    };
    let worker = tokio::spawn(async move {
        let mut retention = retention;
        let result = publish_completed(worker_registry, target).await;
        retention.finished = true;
        result
    });
    tokio::task::yield_now().await;
    worker.abort();
    assert!(matches!(worker.await, Err(error) if error.is_cancelled()));
    drop(state);
    assert!(entry.retained.load(Ordering::Acquire));
    assert!(entry.completed.lock().unwrap().is_some());
    assert_eq!(drops.load(Ordering::SeqCst), 0);
    assert!(budget.0.load(Ordering::SeqCst) > 0);
    assert_eq!(control.usage().count(ControlKind::Registration), 1);
    // An uncertain supervisor is intentionally retained; no automatic replay
    // or asynchronous disposal of the actual result occurs here.
}

#[test]
fn registry_unpolled_cleanup_retains_source_when_owning_runtime_stops() {
    let control = CoreControl::default();
    let registry = Arc::new(Registry::default());
    let entry = registry
        .reserve(
            context(&control, Duration::from_secs(30)),
            TokioCancellationToken::new(),
        )
        .unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let job = schedule_close(registry, entry.clone(), runtime.handle()).unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    drop(job);
    drop(runtime);
    assert!(entry.retained.load(Ordering::Acquire));
    assert!(matches!(*entry.state.try_lock().unwrap(), State::Failed(_)));
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    assert_eq!(control.usage().count(ControlKind::Registration), 1);
}

#[tokio::test]
async fn registry_abandon_notifies_preparing_waiter_while_state_mutex_is_held() {
    let control = CoreControl::default();
    let registry = Arc::new(Registry::default());
    let entry = registry
        .reserve(
            context(&control, Duration::from_secs(30)),
            TokioCancellationToken::new(),
        )
        .unwrap();
    let state = entry.state.lock().await;
    // This is the exact close_entry window: Notified exists, Preparing is
    // locked, and the waiter has not yet released the state to await readiness.
    let ready = entry.ready.notified();
    drop(RetainOnAbandon {
        entry: entry.clone(),
        finished: false,
    });
    assert!(entry.supervisor_failed.load(Ordering::Acquire));
    assert!(matches!(*state, State::Preparing));
    tokio::time::timeout(Duration::from_secs(1), ready)
        .await
        .unwrap();
    drop(state);
    let error = tokio::time::timeout(Duration::from_secs(1), close_entry(registry, entry.clone()))
        .await
        .unwrap()
        .unwrap_err();
    assert_eq!(error.code, "unavailable");
    assert!(entry.retained.load(Ordering::Acquire));
    assert_eq!(control.usage().count(ControlKind::Registration), 1);
}

async fn user_fixture(allowed: bool) -> (crate::test_support::RuntimeFixture, Session) {
    let mut permissions = crate::test_support::permissions(&["fixture"]);
    permissions.wasm.wall_ms = 30_000;
    permissions.loops.max_wall_ms = 30_000;
    let fixture = crate::test_support::RuntimeFixture::with_permissions(
        &["fixture"],
        false,
        permissions.clone(),
    )
    .await;
    if allowed {
        let mut profile = fixture.profile.clone();
        profile.user_permissions = Some(permissions);
        let (_, head) = fixture.core.profile(&profile.id).unwrap();
        fixture
            .core
            .save_profile(profile, Some(head))
            .await
            .unwrap();
    }
    let session = fixture.session().await;
    (fixture, session)
}
#[tokio::test]
async fn actual_user_port_cannot_bypass_missing_user_policy() {
    let (_fixture, session) = user_fixture(false).await;
    let error = session
        .invoke_user_port(
            ShapeRef::parse("local.fixture.input@1.0").unwrap(),
            OpName::parse("read").unwrap(),
            &CanonValue::Obj(BTreeMap::new()),
            TokioCancellationToken::new(),
        )
        .await;
    assert!(error.is_err());
    assert!(session.user_streams.snapshot().unwrap().is_empty());
    session.close().await.unwrap();
}
#[tokio::test]
async fn actual_user_value_and_stream_follow_the_same_gate_without_ui() {
    let (_fixture, session) = user_fixture(true).await;
    let shape = ShapeRef::parse("local.fixture.input@1.0").unwrap();
    let args = CanonValue::Obj(BTreeMap::new());
    let value = session
        .call_user_port(shape.clone(), OpName::parse("read").unwrap(), &args)
        .await
        .unwrap();
    assert!(matches!(
        tobkiri_canon::parse(value.as_bytes(), tobkiri_canon::Variant::StrictV1).unwrap(),
        CanonValue::Int(_)
    ));
    let value = session
        .invoke_user_port(
            shape.clone(),
            OpName::parse("read").unwrap(),
            &args,
            TokioCancellationToken::new(),
        )
        .await
        .unwrap();
    let UserPortOutput::Value(value) = value else {
        panic!("typed value expected")
    };
    assert!(
        std::str::from_utf8(value.as_bytes())
            .unwrap()
            .contains("\"kind\":\"value\"")
    );
    let output = session
        .invoke_user_port(
            shape,
            OpName::parse("stream").unwrap(),
            &args,
            TokioCancellationToken::new(),
        )
        .await
        .unwrap();
    let UserPortOutput::Stream { envelope, handle } = output else {
        panic!("actual typed stream expected")
    };
    assert!(
        std::str::from_utf8(envelope.as_bytes())
            .unwrap()
            .contains("\"kind\":\"stream\"")
    );
    let mut batches = 0;
    loop {
        let value = session.user_stream_pull_buffer(&handle, 1).await.unwrap();
        if value.as_bytes() == b"[]" {
            break;
        }
        batches += 1;
    }
    assert!(batches > 0);
    session.user_stream_cancel(&handle).await.unwrap();
    session.close().await.unwrap();
}
