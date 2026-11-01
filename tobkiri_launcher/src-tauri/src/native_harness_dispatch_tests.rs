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
    result_dropped: Arc<AtomicBool>,
    review_diagnostics: bool,
    entered: mpsc::Sender<()>,
    resume: Mutex<mpsc::Receiver<()>>,
}
struct Release(Arc<Controls>);
impl Drop for Release {
    fn drop(&mut self) {
        self.0.released.store(true, Ordering::Release);
        assert_eq!(
            self.0.result_dropped.load(Ordering::Acquire),
            self.0.review_diagnostics,
            "owned Review diagnostics must die before the source/Read; getter buffers must survive finish"
        );
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
impl OriginalAuthorityOwner for Source {
    fn with_original_user_data<R>(
        &self,
        use_root: impl FnOnce(&std::path::Path) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        Ok(use_root(&self.root))
    }
    fn check_original_owner(&self) -> std::result::Result<(), OriginalAuthorityError> {
        // Review enters through this same selected owner's gate, including
        // options without a diagnostic/copy_input call. Use its real test
        // barrier before any journal existence/read, rather than waiting for
        // a getter-only check_live call that this native body never performs.
        BootstrapInputOwner::check_live(self).map_err(|_| OriginalAuthorityError::unavailable())
    }
}
impl tobkiri_harness_native::BootstrapReviewOwner for Source {
    fn with_original_review_user_data<R>(
        &self,
        use_root: impl FnOnce(&std::path::Path) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        Ok(use_root(&self.root))
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
        OriginalOperation::ReviewListing(_) => vec![],
        OriginalOperation::VerifyDenied(_) => vec![],
        OriginalOperation::VerifyDeniedCodec => vec![],
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
    let denied = matches!(
        &operation,
        OriginalOperation::VerifyDenied(_) | OriginalOperation::VerifyDeniedCodec
    );
    let denied_codec = matches!(&operation, OriginalOperation::VerifyDeniedCodec);
    let mut memory = String::new();
    let mut offset = 1024;
    let call = format!(
        "{{\"format\":\"io.tobkiri.native-call.v1\",\"scope\":{actual_scope},\"args\":[],\"kwargs\":[]}}"
    );
    let (call_ptr, call_len) = data(&mut memory, &mut offset, call.as_bytes());
    let message = if denied {
        ""
    } else {
        "actual second getter refusal λ"
    };
    let details = if denied {
        serde_json::json!({"format":"io.tobkiri.native-value-result.v1","scope":actual_scope,"root":{"scope":actual_scope,"object":"ee3"},"owned":["ee3"]}).to_string()
    } else {
        " { \"after\": \"profile-description\" }\n".to_owned()
    };
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
    if denied {
        let argument = serde_json::json!({"scope":actual_scope,"object":"ee1"});
        let root = serde_json::json!({"scope":actual_scope,"object":"ee2"});
        let import = format!(
            r#"{{"format":"io.tobkiri.native-value-input.v1","scope":{actual_scope},"nodes":[{{"kind":"str","value":"{WORKER_DENIED_MESSAGE}"}}],"root":{{"node":0}}}}"#
        );
        let factory = format!(
            r#"{{"format":"io.tobkiri.native-call.v1","scope":{actual_scope},"args":[{argument}],"kwargs":[]}}"#
        );
        let describe = format!(
            r#"{{"format":"io.tobkiri.native-value-describe.v1","scope":{actual_scope},"root":{root}}}"#
        );
        let imported = serde_json::json!({"format":"io.tobkiri.native-values-imported.v1","scope":actual_scope,"root":argument,"owned":["ee1"]}).to_string();
        let returned = serde_json::json!({"format":"io.tobkiri.native-value-result.v1","scope":actual_scope,"root":root,"owned":["ee2"]}).to_string();
        let observed_message = if denied_codec {
            "different actual factory message"
        } else {
            WORKER_DENIED_MESSAGE
        };
        let viewed = serde_json::json!({"format":"io.tobkiri.native-value-view.v1","scope":actual_scope,"root":root,"owned":[],"nodes":[{"ref":root,"kind":"profile-resolution-denied","opaque_native_binding":false,"fields":[],"factory_denied":{"args":[observed_message],"cause":null,"context":null,"suppress_context":false}}]}).to_string();
        for (name, stage, input, output) in [
            ("import", 0, import, imported),
            ("denied", 1, factory, returned),
        ] {
            let (input_ptr, input_len) = data(&mut memory, &mut offset, input.as_bytes());
            let (output_ptr, output_len) = data(&mut memory, &mut offset, output.as_bytes());
            let body = if name == "denied" && matches!(scenario, Scenario::HostDomain) {
                "call $refuse".to_owned()
            } else {
                format!("i32.const {output_ptr} i32.const {output_len} call $emit")
            };
            getters.push_str(&format!(
                "(func (export \"{name}\") (param $p i32) (param $n i32) (result i32)
                global.get $stage i32.const {stage} i32.ne if unreachable end
                local.get $p local.get $n i32.const {input_ptr} i32.const {input_len} call $check
                i32.const {} global.set $stage {body})\n",
                stage + 1
            ));
        }
        let (input_ptr, input_len) = data(&mut memory, &mut offset, describe.as_bytes());
        let (output_ptr, output_len) = data(&mut memory, &mut offset, viewed.as_bytes());
        descriptions.push_str(&format!("global.get $stage i32.const 2 i32.eq if
            local.get $p local.get $n i32.const {input_ptr} i32.const {input_len} call $check
            i32.const 3 global.set $stage i32.const {output_ptr} i32.const {output_len} call $emit return end\n"));
    } else {
        getters.push_str("(func (export \"import\") (param i32 i32) (result i32) unreachable)\n(func (export \"denied\") (param i32 i32) (result i32) unreachable)\n");
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
      (alias core export $m "import" (core func $import))
      (alias core export $m "denied" (core func $denied))
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
      (func $import (param "payload" string) (result $text)
        (canon lift (core func $import) (memory $memory) (realloc $realloc)))
      (func $denied (param "payload" string) (result $text)
        (canon lift (core func $denied) (memory $memory) (realloc $realloc)))
      (instance $runtime
        (export "error-code" (type $code)) (export "error" (type $error))
        (export "init" (func $init)) (export "bootstrap-profile-id" (func $profile))
        (export "host-resource-root" (func $host)) (export "bundled-profile-root" (func $bundle))
        (export "denied" (func $denied)))
      (instance $transport
        (export "error-code" (type $code)) (export "error" (type $error))
        (export "describe-value" (func $describe)) (export "import-values" (func $import)))
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
    launch_with_detachment(operation, scenario, pause_after, cleanup_panic, false)
}
fn launch_with_detachment(
    operation: OriginalOperation,
    scenario: Scenario,
    pause_after: usize,
    cleanup_panic: bool,
    omit_detachment_invocation: bool,
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
        result_dropped: Arc::new(AtomicBool::new(false)),
        review_diagnostics: matches!(&operation, OriginalOperation::ReviewListing(_)),
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
        .clone()
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
            let mut detachment = Detachment::new(if omit_detachment_invocation {
                None
            } else {
                Some(invocation)
            });
            let (outcome, clean, uncertain) = super::super::finish_bound_job(
                runner,
                admitted,
                &job,
                &worker_jobs,
                &mut detachment,
                Some(source_controls.result_dropped.clone()),
            )
            .await;
            assert!(uncertain.is_none());
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
    assert!(controls.result_dropped.load(Ordering::Acquire));
    assert_eq!(invocation.capacity_snapshot().unwrap().charged_bytes, 0);
}
fn outcome(operation: OriginalOperation, scenario: Scenario) -> serde_json::Value {
    let running = launch(operation.clone(), scenario, 0, false);
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
fn finite_review_enters_the_original_no_journal_prefix_on_its_same_store() {
    let running = launch(
        OriginalOperation::ReviewListing(Default::default()),
        Scenario::Normal,
        0,
        false,
    );
    let invocation = receive(&running.ready);
    let deadline = invocation.deadline().unwrap();
    let output = tauri::async_runtime::block_on(running.waiter.result());
    assert!(matches!(
        output,
        Err(DetachedFailure::Execution {
            stage: Stage::Review,
            source: SourceRecord::Integration(
                crate::native_worker_failure::IntegrationFailure::ReviewNotBound(
                    tobkiri_harness_native::BootstrapReviewReachpoint::CatalogNotBound
                )
            ),
        })
    ));
    assert_eq!(invocation.deadline().unwrap(), deadline);
    assert!(!running._root.0.join("profiles").exists());
    assert!(!running._root.0.join("authority").exists());
    assert_eq!(receive(&running.published), (true, true));
    drain(&running.jobs, &running.controls, &invocation, true);
}

#[test]
fn denied_plain_carrier_survives_finish_then_drops_before_join() {
    denied_worker(false, Scenario::Normal);
}

#[test]
fn denied_host_from_cause_and_context_share_identity_through_finish() {
    denied_worker(true, Scenario::Normal);
}

#[test]
fn denied_factory_throw_keeps_ambient_context_without_applying_host_from() {
    denied_worker(true, Scenario::HostDomain);
}

#[test]
fn denied_malformed_view_keeps_pending_outputs_until_store_and_io_retire() {
    let running = launch(
        OriginalOperation::VerifyDeniedCodec,
        Scenario::Normal,
        0,
        false,
    );
    let invocation = receive(&running.ready);
    let deadline = invocation.deadline().unwrap();
    let output = tauri::async_runtime::block_on(running.waiter.result());
    assert!(matches!(
        output,
        Err(DetachedFailure::Execution {
            stage: Stage::Operation,
            source: SourceRecord::Unsupported(Unsupported::AnyhowBoundary)
        })
    ));
    assert_eq!(invocation.deadline().unwrap(), deadline);
    assert_eq!(receive(&running.published), (true, true));
    // The library's debug invariants check actual pending result/view at both
    // sides of Store retire/IO wait. The existing probes check later Source,
    // retained error, physical Join and capacity=0 independently.
    drain(&running.jobs, &running.controls, &invocation, true);
}

fn denied_worker(from_host: bool, scenario: Scenario) {
    let running = launch(
        OriginalOperation::VerifyDenied(from_host),
        scenario,
        0,
        false,
    );
    let invocation = receive(&running.ready);
    let deadline = invocation.deadline().unwrap();
    let output = tauri::async_runtime::block_on(running.waiter.result());
    assert!(matches!(
        output,
        Err(DetachedFailure::Execution {
            stage: Stage::Operation,
            source: SourceRecord::Unsupported(Unsupported::AnyhowBoundary)
        })
    ));
    assert_eq!(invocation.deadline().unwrap(), deadline);
    assert_eq!(receive(&running.published), (true, true));
    // The normal Source/Result destructor probes assert that these funded
    // carriers survive finish, and are gone before physical Join/capacity=0.
    drain(&running.jobs, &running.controls, &invocation, true);
}

#[test]
fn finite_review_preserves_the_original_journal_key_failure_then_finishes() {
    review_missing_key(false);
}

#[test]
fn finite_review_detachment_failure_drops_owned_diagnostics_before_finish() {
    review_missing_key(true);
}

#[test]
fn finite_review_verifies_the_current_snapshot_before_the_next_unbound_call() {
    review_pointer(ReviewPointerCase::Same);
}

#[test]
fn finite_review_discards_a_stale_intent_after_verifying_the_current_snapshot() {
    review_pointer(ReviewPointerCase::Stale);
}

#[test]
fn finite_review_detaches_the_missing_snapshot_error_graph_before_finish() {
    review_pointer(ReviewPointerCase::MissingSnapshot);
}

#[derive(Clone, Copy)]
enum ReviewPointerCase {
    Same,
    Stale,
    MissingSnapshot,
}

// Small fixture encoding only. The independent original driver verifies the
// old codecs, pointers and snapshots; these cases exercise the actual worker.
fn review_fixture_json(value: &serde_json::Value, spaced: bool) -> String {
    use serde_json::Value;
    match value {
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(|value| review_fixture_json(value, spaced))
                .collect::<Vec<_>>()
                .join(if spaced { ", " } else { "," })
        ),
        Value::Object(values) => {
            let mut values: Vec<_> = values.iter().collect();
            values.sort_unstable_by_key(|(key, _)| *key);
            format!(
                "{{{}}}",
                values
                    .into_iter()
                    .map(|(key, value)| format!(
                        "{}{}{}",
                        serde_json::to_string(key).unwrap(),
                        if spaced { ": " } else { ":" },
                        review_fixture_json(value, spaced)
                    ))
                    .collect::<Vec<_>>()
                    .join(if spaced { ", " } else { "," })
            )
        }
        value => serde_json::to_string(value).unwrap(),
    }
}

fn review_pointer(case: ReviewPointerCase) {
    use hmac::{Hmac, Mac};
    use serde_json::json;
    let running = launch(
        OriginalOperation::ReviewListing(Default::default()),
        Scenario::Normal,
        1,
        false,
    );
    let invocation = receive(&running.ready);
    let deadline = invocation.deadline().unwrap();
    receive(&running.entered);
    let profiles = running._root.0.join("profiles");
    fs::create_dir(&profiles).unwrap();
    let digest = format!("sha256:{}", "a".repeat(64));
    let snapshot = json!({
        "profile":{"profile_id":"defaults"},
        "plan":{"profile_revision":digest,"plan_digest":digest},
        "lock":{"lock_digest":digest},
        "activation":{"profile_id":"defaults","profile_revision":digest,
            "activation_id":"activation:fixture1","plan_digest":digest,
            "lock_digest":digest,"state":"active"},
    });
    let snapshot = review_fixture_json(&snapshot, false);
    let relative = "workspaces/defaults/activation/activations/fixture1.json";
    let snapshot_path = running._root.0.join(relative);
    fs::create_dir_all(snapshot_path.parent().unwrap()).unwrap();
    if !matches!(case, ReviewPointerCase::MissingSnapshot) {
        fs::write(&snapshot_path, &snapshot).unwrap();
    }
    let mut pointer = json!({
        "schema":"io.tobkiri.active-profile-pointer.v1",
        "profile_id":"defaults","profile_revision":digest,
        "activation_id":"activation:fixture1","plan_digest":digest,"lock_digest":digest,
        "activation_snapshot_path":relative,"activation_snapshot_digest":Digest::hash(snapshot.as_bytes()).as_str(),
        "catalog_revision":null,"generation":1,"updated_at":1000,
    });
    pointer["pointer_digest"] =
        json!(Digest::hash(review_fixture_json(&pointer, false).as_bytes()).as_str());
    let pointer_bytes = review_fixture_json(&pointer, false);
    fs::write(profiles.join("active.json"), &pointer_bytes).unwrap();
    let payload = json!({
        "schema":"io.tobkiri.profile-publication-intent.v1",
        "predecessor":if matches!(case, ReviewPointerCase::Stale) { serde_json::Value::Null } else { pointer },
        "candidate":{"profile_id":"other","profile_revision":digest,
            "activation_id":"activation:fixture1","plan_digest":digest,"lock_digest":digest},
        "confirmation_digest":null,
    });
    let key = "original-publication-fixture-key-material";
    fs::write(profiles.join(".publication-key"), key).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(
            profiles.join(".publication-key"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
    }
    let mut hmac = Hmac::<sha2::Sha256>::new_from_slice(key.as_bytes()).unwrap();
    hmac.update(review_fixture_json(&payload, true).as_bytes());
    let journal = review_fixture_json(
        &json!({"payload":payload,"hmac":hex::encode(hmac.finalize().into_bytes())}),
        false,
    );
    let journal_path = profiles.join("bootstrap-publication.json");
    fs::write(&journal_path, &journal).unwrap();
    running.resume.send(()).unwrap();
    let output = tauri::async_runtime::block_on(running.waiter.result());
    match (case, output) {
        (ReviewPointerCase::Same, Err(DetachedFailure::Execution { stage: Stage::Review,
            source: SourceRecord::Integration(crate::native_worker_failure::IntegrationFailure::ReviewNotBound(
                tobkiri_harness_native::BootstrapReviewReachpoint::BootstrapProfileIdNotBound)) })) => {}
        (ReviewPointerCase::Stale, Err(DetachedFailure::Execution { stage: Stage::Review,
            source: SourceRecord::Integration(crate::native_worker_failure::IntegrationFailure::ReviewNotBound(
                tobkiri_harness_native::BootstrapReviewReachpoint::CatalogNotBound)) })) => {}
        (ReviewPointerCase::MissingSnapshot, Err(DetachedFailure::Execution { stage: Stage::Review,
            source: SourceRecord::Review(crate::native_worker_failure::ReviewSourceRecord::ResolutionDenied { message, cause }) })) => {
            assert_eq!(message, "Profile publication recovery failed closed");
            let tobkiri_harness_native::OriginalAuthorityCause::Python { class, message, cause: Some(cause), suppressed: true, .. } = cause else { panic!("missing original snapshot wrapper"); };
            assert_eq!(class, "core_runtime.active_profile_store_v4.ActiveProfileStoreIntegrityError");
            assert_eq!(message, "active Profile activation snapshot is unavailable");
            let tobkiri_harness_native::OriginalAuthorityCause::Python { class, message, cause: None, context: Some(context), suppressed: true } = *cause else { panic!("missing synthetic FileNotFoundError"); };
            assert_eq!(class, "builtins.FileNotFoundError");
            assert_eq!(message, "fixture1.json");
            let tobkiri_harness_native::OriginalAuthorityCause::Io(context) = *context else { panic!("missing actual stat context"); };
            assert_eq!(context.errno, Some(2));
            assert_eq!(context.filename.as_deref(), Some("fixture1.json"));
        }
        (_, other) => panic!("unexpected pointer review outcome: {other:?}"),
    }
    assert_eq!(invocation.deadline().unwrap(), deadline);
    assert_eq!(
        fs::read_to_string(profiles.join("active.json")).unwrap(),
        pointer_bytes
    );
    assert_eq!(
        fs::read_to_string(profiles.join(".publication-key")).unwrap(),
        key
    );
    assert_eq!(
        journal_path.exists(),
        !matches!(case, ReviewPointerCase::Stale)
    );
    if journal_path.exists() {
        assert_eq!(fs::read_to_string(&journal_path).unwrap(), journal);
    }
    if snapshot_path.exists() {
        assert_eq!(fs::read_to_string(&snapshot_path).unwrap(), snapshot);
    }
    assert!(!profiles.join(".active-profile.lock").exists());
    assert!(!running._root.0.join("authority").exists());
    assert_eq!(receive(&running.published), (true, true));
    drain(&running.jobs, &running.controls, &invocation, true);
}

fn review_missing_key(omit_detachment_invocation: bool) {
    let options = tobkiri_harness_native::BootstrapReviewOptions {
        include_source_additions: true,
        active: true,
        activation_denied: true,
        denial_diagnostic: Some("original Host diagnostic".into()),
    };
    let running = launch_with_detachment(
        OriginalOperation::ReviewListing(options),
        Scenario::Normal,
        1,
        false,
        omit_detachment_invocation,
    );
    let invocation = receive(&running.ready);
    receive(&running.entered);
    let profiles = running._root.0.join("profiles");
    fs::create_dir(&profiles).unwrap();
    fs::write(profiles.join("bootstrap-publication.json"), b"[]").unwrap();
    running.resume.send(()).unwrap();
    let output = tauri::async_runtime::block_on(running.waiter.result());
    match output {
        Err(DetachedFailure::Detachment {
            stage: Stage::Operation,
            issue: crate::native_worker_failure::DetachmentIssue::NoInvocation,
        }) if omit_detachment_invocation => {}
        Err(DetachedFailure::Execution {
            stage: Stage::Review,
            source:
                SourceRecord::Review(
                    crate::native_worker_failure::ReviewSourceRecord::ResolutionDenied {
                        message,
                        cause,
                    },
                ),
        }) if !omit_detachment_invocation => {
            assert_eq!(message, "Profile publication recovery failed closed");
            match cause {
                tobkiri_harness_native::OriginalAuthorityCause::Python {
                    class,
                    message,
                    cause,
                    context,
                    suppressed,
                } => {
                    assert_eq!(class, "builtins.ValueError");
                    assert_eq!(message, "Profile publication signing key is unavailable");
                    assert!(cause.is_none() && context.is_none() && !suppressed);
                }
                other => panic!("unexpected original journal cause: {other:?}"),
            }
        }
        other => panic!("unexpected finite review failure: {other:?}"),
    }
    assert!(!profiles.join(".publication-key").exists());
    assert!(!running._root.0.join("authority").exists());
    assert!(profiles.join(".bootstrap-publication.lock").is_file());
    assert_eq!(receive(&running.published), (true, true));
    drain(&running.jobs, &running.controls, &invocation, true);
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
