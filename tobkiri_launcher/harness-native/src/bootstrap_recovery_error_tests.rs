//! Native-origin projection vectors and one actual descriptor/lock error.
//! No test here claims an actual Guest custom exception or denied factory.
//! The portable test lease does not qualify physical RuntimeOwner admission.
use super::*;
use crate::{OriginalAuthorityOwner, OriginalAuthorityStage};
use std::{
    fs,
    path::{Path, PathBuf},
};
use tobkiri_canon::Digest;
use tobkiri_gate::{CapacityLedger, CoreCancellation, CoreControl, GateError, NativeSetupLease};

struct Portable;
impl NativeSetupLease for Portable {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"native-recovery-error-test-only-not-source-authority")
    }
}

struct Source(PathBuf);
impl OriginalAuthorityOwner for Source {
    fn with_original_user_data<R>(
        &self,
        f: impl FnOnce(&Path) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        Ok(f(&self.0))
    }
    fn check_original_owner(&self) -> std::result::Result<(), OriginalAuthorityError> {
        Ok(())
    }
}
impl crate::BootstrapReviewOwner for Source {
    fn with_original_review_user_data<R>(
        &self,
        f: impl FnOnce(&Path) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        Ok(f(&self.0))
    }
}

fn with_operation(
    f: impl FnOnce(
        &NativeSetupInvocation,
        &Arc<dyn AllocationBudget>,
        &crate::original_authority::PublicationRead,
        &CoreCancellation,
        &Source,
    ),
) {
    let temporary = tempfile::tempdir().unwrap();
    let parent = temporary.path().canonicalize().unwrap();
    let control = CoreControl::default();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(Portable)))
        .unwrap();
    let invocation = account.begin().unwrap();
    let engine = tobkiri_wasm::WasmEngine::new_with_control(&control).unwrap();
    let store = engine.native_setup_store(invocation.clone(), ()).unwrap();
    let cancel = store.cancellation().clone();
    let budget = store.allocation_owners().graph().clone();
    let read = crate::original_authority::PublicationRead::new(&invocation).unwrap();
    let source = Source(parent.join("user-data"));
    f(&invocation, &budget, &read, &cancel, &source);
    assert!(read.receipt().verified());
    drop((read, budget, cancel, store, invocation, engine));
    tobkiri_wasm::reap_allocation_owners().unwrap();
    let capacity = account.snapshot().unwrap();
    assert_eq!((capacity.charged_bytes, capacity.reservations), (0, 0));
}

fn python(class: &'static str, message: &str) -> OriginalAuthorityCause {
    OriginalAuthorityCause::Python {
        class,
        message: message.into(),
        cause: None,
        context: None,
        suppressed: false,
    }
}
fn caught(cause: OriginalAuthorityCause) -> OriginalAuthorityError {
    OriginalAuthorityError {
        stage: OriginalAuthorityStage::Source,
        class: crate::bootstrap_review::DENIED,
        message: MESSAGE.into(),
        cause: Some(cause),
        context: None,
        suppressed: true,
    }
}

#[tokio::test(flavor = "current_thread")]
async fn fixed_native_app_classes_keep_exact_meaning_and_unset_os_fields() {
    with_operation(|invocation, budget, _, _, _| {
        for (class, kind) in [
            (SECURE, NativeErrorKind::SecurePersistenceError),
            (ACTIVE, NativeErrorKind::ActiveProfileStoreError),
            (INTEGRITY, NativeErrorKind::ActiveProfileStoreIntegrityError),
            (LOCK_TIMEOUT, NativeErrorKind::ActiveProfileStoreLockTimeout),
        ] {
            let error = caught(python(class, "native failure"));
            let graph = project(&error, invocation, budget).unwrap().unwrap();
            assert_eq!(graph.root().kind(), kind);
            assert_eq!(graph.message().as_str().unwrap(), "native failure");
            assert_eq!(graph.root().args().array().unwrap().len(), 1);
            assert!(graph.root().cause().is_none() && graph.root().context().is_none());
            assert!(!graph.root().suppress_context());
            if class == SECURE {
                for name in ["errno", "strerror", "filename", "filename2"] {
                    assert!(matches!(
                        graph.root().fields().get_key(name).unwrap().inspect(),
                        tobkiri_canon::CoreRef::Null
                    ));
                }
            }
        }
    });
}

#[tokio::test(flavor = "current_thread")]
async fn native_lock_timeout_projection_does_not_substitute_builtin_timeout() {
    with_operation(|invocation, budget, _, _, _| {
        let io = std::io::Error::from_raw_os_error(libc::EAGAIN);
        let source = OriginalAuthorityCause::Io(crate::OriginalAuthorityIoError {
            kind: io.kind(),
            errno: io.raw_os_error(),
            filename: None,
            context: None,
        });
        let error = caught(OriginalAuthorityCause::Python {
            class: LOCK_TIMEOUT,
            message: "active Profile lock deadline exceeded".into(),
            cause: Some(Box::new(source)),
            context: None,
            suppressed: true,
        });
        let graph = project(&error, invocation, budget).unwrap().unwrap();
        assert_eq!(
            graph.root().kind(),
            NativeErrorKind::ActiveProfileStoreLockTimeout
        );
        assert_ne!(graph.root().kind(), NativeErrorKind::TimeoutError);
        assert_eq!(graph.root().cause(), Some(1));
        assert_eq!(graph.root().context(), Some(1));
        assert!(graph.root().suppress_context());
        let cause = graph.node(1).unwrap();
        assert_eq!(cause.kind(), NativeErrorKind::BlockingIoError);
        assert_eq!(
            cause.args().get_index(0).unwrap().as_i64(),
            Some(i64::from(libc::EAGAIN))
        );
    });
}

#[tokio::test(flavor = "current_thread")]
async fn explicit_close_context_is_not_replaced_by_the_cause_alias() {
    with_operation(|invocation, budget, _, _, _| {
        let error = caught(OriginalAuthorityCause::Python {
            class: ACTIVE,
            message: "lock unavailable".into(),
            cause: Some(Box::new(python(SECURE, "same text"))),
            context: Some(Box::new(python("builtins.ValueError", "same text"))),
            suppressed: true,
        });
        let graph = project(&error, invocation, budget).unwrap().unwrap();
        assert_eq!(
            (graph.root().cause(), graph.root().context()),
            (Some(1), Some(2))
        );
        assert_eq!(
            graph.node(1).unwrap().kind(),
            NativeErrorKind::SecurePersistenceError
        );
        assert_eq!(graph.node(2).unwrap().kind(), NativeErrorKind::ValueError);
    });
}

#[tokio::test(flavor = "current_thread")]
async fn registry_and_unknown_or_unrepresented_causes_remain_unqualified() {
    with_operation(|invocation, budget, _, _, _| {
        const REGISTRY: &str =
            "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreIntegrityError";
        for class in [
            REGISTRY,
            "unregistered.same_name.ActiveProfileStoreError",
            "json.decoder.JSONDecodeError",
            "builtins.RuntimeError",
        ] {
            let error = caught(python(class, "same text"));
            assert!(project(&error, invocation, budget).unwrap().is_none());
        }
        let mut raw = caught(python(SECURE, "same text"));
        raw.class = REGISTRY;
        assert!(project(&raw, invocation, budget).unwrap().is_none());
        let nested = caught(OriginalAuthorityCause::Python {
            class: INTEGRITY,
            message: "pointer unreadable".into(),
            cause: Some(Box::new(python("json.decoder.JSONDecodeError", "bad JSON"))),
            context: None,
            suppressed: true,
        });
        assert!(project(&nested, invocation, budget).unwrap().is_none());
        let actual = std::io::Error::from_raw_os_error(libc::EIO);
        for (errno, filename) in [
            (None, None),
            (Some(libc::EIO), Some("non-ascii-\u{3042}".to_owned())),
        ] {
            let error = caught(OriginalAuthorityCause::Io(
                crate::OriginalAuthorityIoError {
                    kind: actual.kind(),
                    errno,
                    filename,
                    context: None,
                },
            ));
            assert!(project(&error, invocation, budget).unwrap().is_none());
        }
    });
}

#[tokio::test(flavor = "current_thread")]
async fn actual_native_unsafe_definition_lock_projects_active_and_secure_causes() {
    with_operation(|invocation, budget, read, cancel, source| {
        use std::os::unix::fs::symlink;
        let profiles = source.0.join("profiles");
        fs::create_dir_all(&profiles).unwrap();
        fs::write(profiles.join("actual.lock"), b"").unwrap();
        symlink("actual.lock", profiles.join(".profile-definition.lock")).unwrap();
        let error =
            crate::original_authority::definitions::run(read, source, cancel, budget).unwrap_err();
        let graph = project(&error, invocation, budget).unwrap().unwrap();
        assert_eq!(
            graph.root().kind(),
            NativeErrorKind::ActiveProfileStoreError
        );
        let index = graph.root().cause().unwrap();
        assert_eq!(graph.root().context(), Some(index));
        assert_eq!(
            graph.node(index).unwrap().kind(),
            NativeErrorKind::SecurePersistenceError
        );
        assert!(read.receipt().verified());
        assert!(!source.0.join("authority").exists());
    });
}

#[tokio::test(flavor = "current_thread")]
async fn cancellation_is_processing_failure_not_a_fabricated_domain_cause() {
    with_operation(|invocation, budget, _, cancel, _| {
        let error = caught(python(SECURE, "source failure"));
        cancel.cancel();
        assert!(project(&error, invocation, budget).is_err());
        assert_eq!(error.message, MESSAGE);
    });
}
