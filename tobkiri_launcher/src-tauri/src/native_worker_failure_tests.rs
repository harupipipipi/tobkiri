//! Projection fixtures test DATA copying, not an initialization/GUI success.
use super::*;
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
};
use tobkiri_core::{CoreBuffer, RuntimeOwner};
use tobkiri_harness_native::InitializationRejection;
use tobkiri_wasm::WasmEngine;

struct Root(PathBuf);
impl Root {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-worker-failure-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        std::fs::create_dir(&root).unwrap();
        Self(root)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn detached_bytes_preserve_encoding_and_field_boundaries_under_the_same_account() {
    let root = Root::new();
    let owner = RuntimeOwner::open(root.0.join("data")).unwrap();
    let invocation = owner.begin_native_setup().unwrap();
    let mut detachment = Detachment::new(Some(invocation.clone()));
    let code = detachment.bytes(b"code").unwrap();
    let message = detachment.bytes("actual λ\0message".as_bytes()).unwrap();
    let details = detachment.bytes(b" { \"n\": 1.0 }\n\xff").unwrap();
    assert_eq!(code, b"code");
    assert_eq!(message, "actual λ\0message".as_bytes());
    assert_eq!(details, b" { \"n\": 1.0 }\n\xff");
    assert_eq!(
        invocation.capacity_snapshot().unwrap().charged_bytes,
        (code.len() + message.len() + details.len()) as u64
    );
    drop((code, message, details));
    drop(detachment);
    assert_eq!(invocation.capacity_snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn copy_reservation_refusal_is_explicit_and_never_partial_success() {
    let root = Root::new();
    let owner = RuntimeOwner::open(root.0.join("data")).unwrap();
    let invocation = owner.begin_native_setup().unwrap();
    let full = invocation
        .reserve(
            CapacityKind::VfsBuffer,
            tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES,
        )
        .unwrap();
    let mut detachment = Detachment::new(Some(invocation));
    assert!(matches!(
        detachment.bytes(b"x"),
        Err(DetachmentIssue::ReservationRejected { .. })
    ));
    drop(full);
}

#[test]
fn exact_initialization_envelope_keeps_actual_buffer_and_all_supplied_metadata_fields() {
    tauri::async_runtime::block_on(async {
        let root = Root::new();
        let owner = RuntimeOwner::open(root.0.join("data")).unwrap();
        let invocation = owner.begin_native_setup().unwrap();
        let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
        let store = engine.native_setup_store(invocation.clone(), ()).unwrap();
        let payload = b" { \"projection-fixture\": [1, 2] }\n";
        let diagnostic = CoreBuffer::copy_from(payload, store.allocation_owners().graph()).unwrap();
        let deadline = invocation.deadline().unwrap();
        // Supplied projection-only metadata; it is not reported as guest progress.
        let trace = InitializationTrace {
            host_phase: "projection-fixture",
            inventory_entries: 2,
            inventory_validated: 1,
            inspect_entries: 4,
            inspect_validated: 3,
            guest_checkpoint: None,
            checkpoint_sequence: 0,
            checkpoint_count: 0,
            last_rejection: InitializationRejection::Frame,
        };
        let error = anyhow::Error::new(OriginalInitializationFailure {
            original: NativeDataIoError::Retired.into(),
            diagnostic: Some(diagnostic),
            trace,
            observation: Some(NativeDataObservation::default()),
            capacity: Some(invocation.capacity_snapshot().unwrap()),
            elapsed_micros: 19,
            deadline: Some(deadline),
        });
        let mut detachment = Detachment::new(Some(invocation));
        let detached = detachment.native(&error, Stage::Initialize);
        assert!(initialization_cleanup_verified(&error));
        match detached {
            DetachedFailure::Initialization {
                diagnostic,
                original,
                trace: actual,
                observation,
                capacity,
                elapsed_micros,
                deadline: actual_deadline,
            } => {
                assert_eq!(diagnostic.unwrap(), payload);
                assert!(matches!(
                    *original,
                    DetachedFailure::Execution {
                        source: SourceRecord::NativeIo(NativeDataIoError::Retired),
                        ..
                    }
                ));
                assert_eq!(actual.host_phase, trace.host_phase);
                assert_eq!(actual.inventory_entries, 2);
                assert_eq!(actual.inventory_validated, 1);
                assert_eq!(actual.inspect_entries, 4);
                assert_eq!(actual.inspect_validated, 3);
                assert_eq!(actual.last_rejection, InitializationRejection::Frame);
                assert!(observation.is_some() && capacity.is_some());
                assert_eq!(elapsed_micros, 19);
                assert_eq!(actual_deadline, Some(deadline));
            }
            other => panic!("expected explicit initialization record, got {other:?}"),
        }
        drop(error);
        drop(store);
    });
}

#[derive(Debug)]
struct Unprojected {
    released: Arc<AtomicBool>,
}
impl std::fmt::Display for Unprojected {
    fn fmt(&self, _: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        panic!("unknown error was formatted");
    }
}
impl StdError for Unprojected {}
impl Drop for Unprojected {
    fn drop(&mut self) {
        self.released.store(true, Ordering::SeqCst);
    }
}

#[test]
fn unknown_and_context_wrapped_errors_are_not_formatted_or_searched_for_cleanup_success() {
    let known = anyhow::Error::new(LifecycleError("native owner admission is already pending"));
    assert!(matches!(
        Detachment::new(None).native(&known, Stage::Admission),
        DetachedFailure::Execution {
            source: SourceRecord::Lifecycle {
                condition: "native owner admission is already pending"
            },
            ..
        }
    ));
    let released = Arc::new(AtomicBool::new(false));
    let error = anyhow::Error::new(Unprojected {
        released: released.clone(),
    })
    .context("opaque context");
    let detached = Detachment::new(None).native(&error, Stage::Initialize);
    assert!(matches!(
        detached,
        DetachedFailure::Execution {
            source: SourceRecord::Unsupported(Unsupported::AnyhowBoundary),
            ..
        }
    ));
    assert!(!initialization_cleanup_verified(&error));
    assert!(!released.load(Ordering::SeqCst));
    drop(error);
    assert!(released.load(Ordering::SeqCst));
}

#[test]
fn cleanup_preserves_permitted_original_success_and_separate_native_cleanup_failure() {
    let value = serde_json::json!({"actual-success-data": [2, 1]});
    let failure = complete(
        Ok(value.clone()),
        Some(DetachedFailure::Execution {
            stage: Stage::Finish,
            source: SourceRecord::NativeIo(NativeDataIoError::CompletionUnknown),
        }),
        true,
    )
    .unwrap_err();
    match failure {
        DetachedFailure::Cleanup { original, cleanup } => {
            assert_eq!(original.unwrap(), value);
            assert!(matches!(
                *cleanup,
                DetachedFailure::Execution {
                    source: SourceRecord::NativeIo(NativeDataIoError::CompletionUnknown),
                    ..
                }
            ));
        }
        _ => unreachable!(),
    }
}

#[test]
fn revoked_success_is_dropped_even_inside_cleanup_and_prior_failure_is_not_overwritten() {
    let failure = complete(
        Ok(serde_json::json!({"must-not-publish": "secret"})),
        Some(DetachedFailure::condition(
            Stage::Finish,
            Condition::NativePanic,
        )),
        false,
    )
    .unwrap_err();
    assert!(matches!(failure, DetachedFailure::Cleanup { original, .. }
        if matches!(*original, Err(DetachedFailure::Publication { operation_succeeded: true, .. }))));
    let prior = complete(
        Err(DetachedFailure::Execution {
            stage: Stage::Operation,
            source: SourceRecord::NativeIo(NativeDataIoError::Deadline),
        }),
        None,
        false,
    );
    assert!(matches!(
        prior,
        Err(DetachedFailure::Execution {
            source: SourceRecord::NativeIo(NativeDataIoError::Deadline),
            ..
        })
    ));
}
