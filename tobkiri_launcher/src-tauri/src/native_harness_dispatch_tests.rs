//! Actual small-component/worker contracts, not original Python or GUI parity.
use super::*;
use crate::native_worker_failure::{SourceRecord, Unsupported};
use std::{
    fs::{self, File},
    io::Write,
    path::PathBuf,
    sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc,
    },
    time::Duration,
};
use tobkiri_harness_native::BootstrapModule;

struct Root(PathBuf);
impl Root {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "tobkiri-finite-dispatch-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        fs::create_dir(&path).unwrap();
        Self(path.canonicalize().unwrap())
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn receive<T>(receiver: &mpsc::Receiver<T>) -> T {
    receiver.recv_timeout(Duration::from_secs(30)).unwrap()
}
struct Controls {
    pause_after: AtomicUsize,
    release_panics: AtomicBool,
    released: AtomicBool,
    entered: mpsc::Sender<()>,
    resume: Mutex<mpsc::Receiver<()>>,
}
struct Release(Arc<Controls>);
impl Drop for Release {
    fn drop(&mut self) {
        self.0.released.store(true, Ordering::Release);
        assert!(
            !self.0.release_panics.load(Ordering::Acquire),
            "supplied source destruction panic"
        );
    }
}
struct Source {
    root: PathBuf,
    _directory: File,
    _custody: SourceCustody,
    release: Release,
}
impl BootstrapInputOwner for Source {
    fn original_module_metadata_policy(&self) -> Option<&'static ModuleMetadataPolicy> {
        Some(&ORIGINAL_PROFILE_SOURCE_METADATA)
    }
    fn with_roots<R>(
        &self,
        use_roots: impl for<'a> FnOnce(BootstrapRoots<'a>) -> R,
    ) -> std::result::Result<R, NativeDataIoError> {
        Ok(use_roots(BootstrapRoots {
            runtime: &self.root,
            working_directory: &self.root,
            development_workspace: None,
        }))
    }
    fn check_live(&self) -> std::result::Result<(), NativeDataIoError> {
        if self.release.0.pause_after.fetch_update(
            Ordering::AcqRel,
            Ordering::Acquire,
            |remaining| remaining.checked_sub(1),
        ) == Ok(1)
        {
            self.release.0.entered.send(()).unwrap();
            receive(&self.release.0.resume.lock().unwrap());
        }
        Ok(())
    }
}
static INDEX: BootstrapModuleIndex = BootstrapModuleIndex {
    sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    modules: &[BootstrapModule {
        module: "fixture",
        relative_file: "fixture.py",
    }],
};

#[derive(Clone, Copy)]
enum Scenario {
    Normal,
    HostDomain,
    DescribeTrap,
    MalformedResult,
}
fn expected_getters(operation: OriginalOperation) -> Vec<OriginalGetter> {
    match operation {
        OriginalOperation::Observe(getter) => vec![getter],
        OriginalOperation::VerifyBoundGetters => vec![
            OriginalGetter::BootstrapProfileId,
            OriginalGetter::HostResourceRoot,
            OriginalGetter::BundledProfileRoot,
        ],
        OriginalOperation::Unbound(_) => panic!("unbound fixture must not compile"),
    }
}
fn scope(invocation: &NativeSetupInvocation) -> serde_json::Value {
    let actual = invocation.provenance();
    serde_json::json!([
        format!("{:x}", actual.account_generation),
        format!("{:x}", actual.invocation),
        "1",
        "1"
    ])
}
fn reference(scope: &serde_json::Value, getter: OriginalGetter) -> serde_json::Value {
    // The toy guest deliberately issues one alias across the first/third
    // getter. These are fixture DATA, never a fabricated Profile/native Path.
    let object = match getter {
        OriginalGetter::HostResourceRoot => "2c9",
        _ => "d71",
    };
    serde_json::json!({"scope": scope, "object": object})
}
fn view(scope: &serde_json::Value, getter: OriginalGetter) -> String {
    let root = reference(scope, getter);
    let description = serde_json::json!({
        "format": "io.tobkiri.native-value-view.v1", "scope": scope,
        "root": root, "owned": [],
        "nodes": [{"ref": root, "kind": "str", "value": "fixture-only λ"}],
    })
    .to_string();
    // Preserve the actual returned description spelling, including whitespace;
    // parsing and regenerating a description is not an object observation.
    format!(" \n{description}\n")
}
fn data(memory: &mut String, offset: &mut usize, bytes: &[u8]) -> (usize, usize) {
    let start = *offset;
    let escaped: String = bytes.iter().map(|byte| format!("\\{byte:02x}")).collect();
    memory.push_str(&format!("(data (i32.const {start}) \"{escaped}\")\n"));
    *offset += bytes.len() + 16;
    (start, bytes.len())
}

/// The guest state machine independently checks exact call arguments, actual
/// invocation scope, issued object identity, and getter/description ordering.
fn component(
    invocation: &NativeSetupInvocation,
    operation: OriginalOperation,
    scenario: Scenario,
) -> Vec<u8> {
    let actual_scope = scope(invocation);
    let mut memory = String::new();
    let mut offset = 1024;
    let call = format!(
        "{{\"format\":\"io.tobkiri.native-call.v1\",\"scope\":{actual_scope},\"args\":[],\"kwargs\":[]}}"
    );
    let (call_ptr, call_len) = data(&mut memory, &mut offset, call.as_bytes());
    let message = "actual second getter refusal λ";
    let details = " { \"after\": \"profile-description\" }\n";
    let (message_ptr, message_len) = data(&mut memory, &mut offset, message.as_bytes());
    let (details_ptr, details_len) = data(&mut memory, &mut offset, details.as_bytes());
    let mut getters = String::new();
    let mut descriptions = String::new();
    let selected = expected_getters(operation);
    for getter in [
        OriginalGetter::BootstrapProfileId,
        OriginalGetter::HostResourceRoot,
        OriginalGetter::BundledProfileRoot,
    ] {
        let name = match getter {
            OriginalGetter::BootstrapProfileId => "profile",
            OriginalGetter::HostResourceRoot => "host",
            OriginalGetter::BundledProfileRoot => "bundle",
        };
        let Some(index) = selected.iter().position(|value| *value == getter) else {
            getters.push_str(&format!(
                "(func (export \"{name}\") (param i32 i32) (result i32) unreachable)\n"
            ));
            continue;
        };
        let stage = index * 2;
        let root = reference(&actual_scope, getter);
        let result = if matches!(scenario, Scenario::MalformedResult) {
            serde_json::json!({"format":"io.tobkiri.native-value-result.v1", "scope":actual_scope,"owned":[]})
        } else {
            serde_json::json!({"format":"io.tobkiri.native-value-result.v1", "scope":actual_scope,"root":root,"owned":[root["object"]]})
        };
        let (result_ptr, result_len) =
            data(&mut memory, &mut offset, result.to_string().as_bytes());
        let result_body = if matches!(scenario, Scenario::HostDomain)
            && getter == OriginalGetter::HostResourceRoot
        {
            "call $refuse".to_owned()
        } else {
            format!("i32.const {result_ptr} i32.const {result_len} call $emit")
        };
        getters.push_str(&format!(
            "(func (export \"{name}\") (param $p i32) (param $n i32) (result i32)
               global.get $stage i32.const {stage} i32.ne if unreachable end
               local.get $p local.get $n i32.const {call_ptr} i32.const {call_len} call $check
               i32.const {} global.set $stage {result_body})\n",
            stage + 1,
        ));
        // CoreValue::object_from encodes its closed frame in key order. This
        // independent expected packet contains this guest-issued root only.
        let request = serde_json::json!({"format":"io.tobkiri.native-value-describe.v1", "scope":actual_scope,"root":root}).to_string();
        let (request_ptr, request_len) = data(&mut memory, &mut offset, request.as_bytes());
        let (view_ptr, view_len) = data(
            &mut memory,
            &mut offset,
            view(&actual_scope, getter).as_bytes(),
        );
        let described = if matches!(scenario, Scenario::DescribeTrap) {
            "unreachable".to_owned()
        } else {
            format!("i32.const {view_ptr} i32.const {view_len} call $emit return")
        };
        descriptions.push_str(&format!(
            "global.get $stage i32.const {} i32.eq if
               local.get $p local.get $n i32.const {request_ptr} i32.const {request_len} call $check
               i32.const {} global.set $stage {described} end\n",
            stage + 1,
            stage + 2,
        ));
    }
    let source = r#"(component
      (type $code (enum "not-initialized" "already-initialized" "invalid-frame"
        "invalid-reference" "unsupported-value" "method-raised"))
      (type $error (record (field "code" $code) (field "message" string) (field "details" string)))
      (type $unit (result (error $error)))
      (type $text (result string (error $error)))
      (core module $m
        (memory (export "memory") 4)
        (global $next (mut i32) (i32.const 65536))
        (global $stage (mut i32) (i32.const 0))
        @MEMORY@
        (func (export "realloc") (param i32 i32 i32 i32) (result i32)
          global.get $next global.get $next local.get 3 i32.add global.set $next)
        (func $check (param $p i32) (param $n i32) (param $e i32) (param $size i32)
          (local $i i32)
          local.get $n local.get $size i32.ne if unreachable end
          block $done loop $bytes
            local.get $i local.get $size i32.ge_u br_if $done
            local.get $p local.get $i i32.add i32.load8_u
            local.get $e local.get $i i32.add i32.load8_u
            i32.ne if unreachable end
            local.get $i i32.const 1 i32.add local.set $i br $bytes
          end end)
        (func $emit (param $p i32) (param $n i32) (result i32)
          i32.const 128 i32.const 0 i32.store
          i32.const 132 local.get $p i32.store
          i32.const 136 local.get $n i32.store i32.const 128)
        (func $refuse (result i32)
          i32.const 128 i32.const 1 i32.store
          i32.const 132 i32.const 5 i32.store
          i32.const 136 i32.const @MESSAGE_PTR@ i32.store
          i32.const 140 i32.const @MESSAGE_LEN@ i32.store
          i32.const 144 i32.const @DETAILS_PTR@ i32.store
          i32.const 148 i32.const @DETAILS_LEN@ i32.store i32.const 128)
        (func (export "init") (param i32 i32) (result i32)
          i32.const 128 i32.const 0 i32.store i32.const 128)
        @GETTERS@
        (func (export "describe") (param $p i32) (param $n i32) (result i32)
          @DESCRIPTIONS@ unreachable))
      (core instance $m (instantiate $m))
      (alias core export $m "memory" (core memory $memory))
      (alias core export $m "realloc" (core func $realloc))
      (alias core export $m "init" (core func $init))
      (alias core export $m "profile" (core func $profile))
      (alias core export $m "host" (core func $host))
      (alias core export $m "bundle" (core func $bundle))
      (alias core export $m "describe" (core func $describe))
      (func $init (param "payload" string) (result $unit)
        (canon lift (core func $init) (memory $memory) (realloc $realloc)))
      (func $profile (param "payload" string) (result $text)
        (canon lift (core func $profile) (memory $memory) (realloc $realloc)))
      (func $host (param "payload" string) (result $text)
        (canon lift (core func $host) (memory $memory) (realloc $realloc)))
      (func $bundle (param "payload" string) (result $text)
        (canon lift (core func $bundle) (memory $memory) (realloc $realloc)))
      (func $describe (param "payload" string) (result $text)
        (canon lift (core func $describe) (memory $memory) (realloc $realloc)))
      (instance $runtime
        (export "error-code" (type $code)) (export "error" (type $error))
        (export "init" (func $init)) (export "bootstrap-profile-id" (func $profile))
        (export "host-resource-root" (func $host)) (export "bundled-profile-root" (func $bundle)))
      (instance $transport
        (export "error-code" (type $code)) (export "error" (type $error))
        (export "describe-value" (func $describe)))
      (export "tobkiri-ui:harness-native/runtime@1.0.0" (instance $runtime))
      (export "tobkiri-ui:harness-native/transport@1.0.0" (instance $transport)))"#
        .replace("@MEMORY@", &memory)
        .replace("@GETTERS@", &getters)
        .replace("@DESCRIPTIONS@", &descriptions)
        .replace("@MESSAGE_PTR@", &message_ptr.to_string())
        .replace("@MESSAGE_LEN@", &message_len.to_string())
        .replace("@DETAILS_PTR@", &details_ptr.to_string())
        .replace("@DETAILS_LEN@", &details_len.to_string());
    wat::parse_str(source).unwrap()
}

struct Running {
    _root: Root,
    jobs: Jobs,
    waiter: OriginalComponentJob,
    ready: mpsc::Receiver<NativeSetupInvocation>,
    published: mpsc::Receiver<(bool, bool)>,
    controls: Arc<Controls>,
    entered: mpsc::Receiver<()>,
    resume: mpsc::Sender<()>,
}
fn launch(
    operation: OriginalOperation,
    scenario: Scenario,
    pause_after: usize,
    cleanup_panic: bool,
) -> Running {
    let root = Root::new();
    let path = root.0.clone();
    let jobs = Jobs::new(Arc::new(AtomicBool::new(false)));
    let (entered, entering) = mpsc::channel();
    let (resume, resuming) = mpsc::channel();
    let controls = Arc::new(Controls {
        pause_after: AtomicUsize::new(0),
        release_panics: AtomicBool::new(false),
        released: AtomicBool::new(false),
        entered,
        resume: Mutex::new(resuming),
    });
    let source_controls = controls.clone();
    let (ready, ready_receiver) = mpsc::channel();
    let (published, published_receiver) = mpsc::channel();
    let (send, result) = tokio::sync::oneshot::channel();
    let lifecycle = jobs.clone();
    let worker_jobs = jobs.clone();
    let admitted = operation
        .admit()
        .unwrap_or_else(|_| panic!("unbound operation admitted"));
    jobs.spawn(move |job| {
        tauri::async_runtime::block_on(async move {
            let owner = RuntimeOwner::open(path.join("data")).unwrap();
            let invocation = owner.begin_native_setup().unwrap();
            let parent = job.attach_invocation(&invocation).unwrap();
            let bytes = component(&invocation, operation, scenario);
            let artifact = path.join("finite-fixture.wasm");
            File::create(&artifact).unwrap().write_all(&bytes).unwrap();
            let source = Source {
                root: path.clone(),
                _directory: File::open(&path).unwrap(),
                _custody: job.source_custody().unwrap(),
                release: Release(source_controls.clone()),
            };
            let prepared = NativeComponentPreparation::new_with_cancellation_parent(
                invocation.clone(),
                source,
                &parent,
            )
            .unwrap()
            .compile(File::open(artifact).unwrap(), &Digest::hash(&bytes))
            .unwrap();
            let runner = prepared.initialize(&INDEX).await.unwrap().unwrap();
            ready.send(invocation.clone()).unwrap();
            source_controls
                .release_panics
                .store(cleanup_panic, Ordering::Release);
            source_controls
                .pause_after
                .store(pause_after, Ordering::Release);
            let mut detachment = Detachment::new(Some(invocation));
            let (outcome, clean) = super::super::finish_bound_job(
                runner,
                admitted,
                &job,
                &worker_jobs,
                &mut detachment,
            )
            .await;
            drop(detachment);
            if !clean {
                lifecycle.cleanup_failed();
            }
            let delivered = send.send(outcome).is_ok();
            published.send((clean, delivered)).unwrap();
            drop(owner);
            WorkerExit {
                cleanup_verified: clean,
            }
        })
    })
    .unwrap();
    Running {
        _root: root,
        jobs,
        waiter: OriginalComponentJob { result },
        ready: ready_receiver,
        published: published_receiver,
        controls,
        entered: entering,
        resume,
    }
}
fn drain(jobs: &Jobs, controls: &Controls, invocation: &NativeSetupInvocation, clean: bool) {
    let result = jobs.drain_until(Instant::now() + Duration::from_secs(30));
    if clean {
        result.unwrap();
        jobs.mark_closed().unwrap();
    } else {
        assert!(result.is_err());
        assert!(jobs.mark_closed().is_err());
    }
    assert!(controls.released.load(Ordering::Acquire));
    assert_eq!(invocation.capacity_snapshot().unwrap().charged_bytes, 0);
}
fn outcome(operation: OriginalOperation, scenario: Scenario) -> serde_json::Value {
    let running = launch(operation, scenario, 0, false);
    let invocation = receive(&running.ready);
    let result = tauri::async_runtime::block_on(running.waiter.result()).unwrap();
    assert_eq!(receive(&running.published), (true, true));
    drain(&running.jobs, &running.controls, &invocation, true);
    let expected: Vec<_> = expected_getters(operation)
        .into_iter()
        .map(|getter| {
            serde_json::json!({"getter":getter.name(),"description":view(&scope(&invocation),getter)})
        })
        .collect();
    assert_eq!(result, serde_json::json!({"observations":expected}));
    // The actual result still exists here, after Store/Source release and Join.
    result
}

#[test]
fn every_unbound_export_is_an_integration_failure_before_any_worker() {
    use UnboundOperation::*;
    for operation in [
        SetupProfilePreview,
        Confirmation,
        BootstrapConfirmation,
        ProfileConfirmation,
        SetupListing,
        SetupPreview,
        SetupActivationDecision,
        SetupActivationSuccess,
        SetupActivationFailure,
        RetiredSetupResponse,
        LoadCatalog,
        CatalogWithPacks,
        CatalogWithProfiles,
        ResolveProfile,
        DynamicProfileEdges,
        Denied,
        ImportValues,
        CopyDataDictionary,
        ReleaseValues,
        AppendDataItem,
        SetDataItem,
        NativeInputs,
    ] {
        assert!(matches!(
            OriginalOperation::Unbound(operation).admit(),
            Err(DetachedFailure::Execution {
                stage: Stage::Operation,
                source: SourceRecord::Integration(IntegrationFailure::OperationNotBound(actual)),
            }) if actual == operation
        ));
    }
}

#[test]
fn each_single_getter_uses_original_no_argument_call_and_actual_description() {
    for getter in [
        OriginalGetter::BootstrapProfileId,
        OriginalGetter::HostResourceRoot,
        OriginalGetter::BundledProfileRoot,
    ] {
        outcome(OriginalOperation::Observe(getter), Scenario::Normal);
    }
}

#[test]
fn finite_verification_sequence_preserves_store_scope_and_cross_method_aliases() {
    let result = outcome(OriginalOperation::VerifyBoundGetters, Scenario::Normal);
    let values = result["observations"].as_array().unwrap();
    assert_eq!(values[0]["description"], values[2]["description"]);
    assert_ne!(values[0]["description"], values[1]["description"]);
}

#[test]
fn second_getter_domain_failure_stops_the_sequence_and_keeps_actual_error_bytes() {
    let running = launch(
        OriginalOperation::VerifyBoundGetters,
        Scenario::HostDomain,
        0,
        false,
    );
    let invocation = receive(&running.ready);
    let result = tauri::async_runtime::block_on(running.waiter.result());
    assert!(matches!(
        result,
        Err(DetachedFailure::Domain {code,message,details})
        if code == b"method-raised"
            && message == "actual second getter refusal λ".as_bytes()
            && details.as_deref() == Some(b" { \"after\": \"profile-description\" }\n".as_slice())
    ));
    assert_eq!(receive(&running.published), (true, true));
    drain(&running.jobs, &running.controls, &invocation, true);
}

#[test]
fn malformed_issued_result_is_integration_failure_and_never_a_domain_success() {
    let running = launch(
        OriginalOperation::Observe(OriginalGetter::BootstrapProfileId),
        Scenario::MalformedResult,
        0,
        false,
    );
    let invocation = receive(&running.ready);
    let result = tauri::async_runtime::block_on(running.waiter.result());
    assert!(matches!(
        result,
        Err(DetachedFailure::Execution {
            stage: Stage::Operation,
            source: SourceRecord::Integration(IntegrationFailure::InvalidValueResult),
        })
    ));
    assert_eq!(receive(&running.published), (true, true));
    drain(&running.jobs, &running.controls, &invocation, true);
}

#[test]
fn actual_description_trap_stays_an_outer_failure_and_the_same_runner_finishes() {
    let running = launch(
        OriginalOperation::Observe(OriginalGetter::BootstrapProfileId),
        Scenario::DescribeTrap,
        0,
        false,
    );
    let invocation = receive(&running.ready);
    let result = tauri::async_runtime::block_on(running.waiter.result());
    assert!(matches!(
        result,
        Err(DetachedFailure::Execution {
            stage: Stage::Operation,
            source: SourceRecord::Unsupported(Unsupported::AnyhowBoundary),
        })
    ));
    assert_eq!(receive(&running.published), (true, true));
    drain(&running.jobs, &running.controls, &invocation, true);
}

#[test]
fn disappearing_caller_does_not_detach_the_finite_worker_or_its_source() {
    let running = launch(
        OriginalOperation::Observe(OriginalGetter::BootstrapProfileId),
        Scenario::Normal,
        1,
        false,
    );
    let invocation = receive(&running.ready);
    receive(&running.entered);
    drop(running.waiter);
    assert!(!running.controls.released.load(Ordering::Acquire));
    running.resume.send(()).unwrap();
    assert_eq!(receive(&running.published), (true, false));
    drain(&running.jobs, &running.controls, &invocation, true);
}

#[test]
fn cancel_after_actual_description_blocks_late_success_before_cleanup_and_join() {
    let running = launch(
        OriginalOperation::Observe(OriginalGetter::BootstrapProfileId),
        Scenario::Normal,
        // string_call's pre/post source checks occur for the getter, then
        // describe. The fourth check retains the actual completed description
        // before returning it; there are no more guest entries after resume.
        4,
        false,
    );
    let invocation = receive(&running.ready);
    receive(&running.entered);
    running.jobs.request_shutdown();
    running.resume.send(()).unwrap();
    let result = tauri::async_runtime::block_on(running.waiter.result());
    assert!(matches!(
        result,
        Err(DetachedFailure::Publication {
            reason: Condition::AdmissionRetired,
            operation_succeeded: true,
        })
    ));
    assert_eq!(receive(&running.published), (true, true));
    drain(&running.jobs, &running.controls, &invocation, true);
}

#[test]
fn actual_finish_destruction_panic_preserves_success_and_prevents_closed_receipt() {
    let running = launch(
        OriginalOperation::Observe(OriginalGetter::BootstrapProfileId),
        Scenario::Normal,
        0,
        true,
    );
    let invocation = receive(&running.ready);
    let result = tauri::async_runtime::block_on(running.waiter.result());
    match result {
        Err(DetachedFailure::Cleanup { original, cleanup }) => {
            assert_eq!(
                original.as_ref().as_ref().unwrap()["observations"]
                    .as_array()
                    .unwrap()
                    .len(),
                1
            );
            assert!(matches!(
                *cleanup,
                DetachedFailure::Execution {
                    stage: Stage::Finish,
                    source: SourceRecord::Condition(Condition::NativePanic),
                }
            ));
        }
        other => panic!("finish failure was lost: {other:?}"),
    }
    assert_eq!(receive(&running.published), (false, true));
    drain(&running.jobs, &running.controls, &invocation, false);
}
