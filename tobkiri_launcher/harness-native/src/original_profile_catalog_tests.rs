//! Actual finite resource mechanics with a controlled portable source. Not
//! AppHandle permission, Pack validation, Guest execution or full Setup proof.
use super::*;
use serde_json::{Value, json};
use tobkiri_canon::Digest;
use tobkiri_gate::{CapacityLedger, CoreControl, GateError, NativeSetupLease};

struct Portable;
impl NativeSetupLease for Portable {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"catalog-reader-test-not-app-admission")
    }
}
struct Source {
    root: PathBuf,
    development: bool,
}
impl OriginalAuthorityOwner for Source {
    fn with_original_user_data<R>(&self, operation: impl FnOnce(&Path) -> R) -> Result<R> {
        Ok(operation(&self.root))
    }
    fn check_original_owner(&self) -> Result<()> {
        Ok(())
    }
}
impl crate::BootstrapReviewOwner for Source {
    fn with_original_review_user_data<R>(&self, operation: impl FnOnce(&Path) -> R) -> Result<R> {
        Ok(operation(&self.root))
    }
    fn with_original_catalog_source<R>(
        &self,
        operation: impl for<'a> FnOnce(crate::BootstrapCatalogSource<'a>) -> R,
    ) -> Result<R> {
        Ok(operation(crate::BootstrapCatalogSource {
            runtime: &self.root,
            environment: if self.development {
                "development"
            } else {
                "production"
            },
            configured_app: self.development.then_some(self.root.as_path()),
        }))
    }
}
fn with_source(
    development: bool,
    run: impl FnOnce(
        &Source,
        &NativeSetupInvocation,
        &Arc<dyn AllocationBudget>,
        &mut CatalogIo,
        &Arc<CleanupState>,
    ),
) {
    let directory = tempfile::tempdir().unwrap();
    let root = directory.path().join("runtime");
    fs::create_dir_all(&root).unwrap();
    let source = Source { root, development };
    let control = CoreControl::default();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(Portable)))
        .unwrap();
    let invocation = account.begin().unwrap();
    let engine = tobkiri_wasm::WasmEngine::new_with_control(&control).unwrap();
    let store = engine.native_setup_store(invocation.clone(), ()).unwrap();
    let budget = store.allocation_owners().graph().clone();
    let cancellation = store.cancellation().clone();
    let cleanup = Arc::new(CleanupState::new(
        control
            .reserve(tobkiri_gate::ControlKind::Context, 4096)
            .unwrap(),
    ));
    let mut io = CatalogIo::new(
        &source,
        &invocation,
        &cancellation,
        cleanup.clone(),
        &source.root,
        &budget,
    )
    .unwrap();
    run(&source, &invocation, &budget, &mut io, &cleanup);
    assert!(cleanup.receipt().verified());
    assert!(!source.root.join("profiles").exists());
    drop((io, cleanup, budget, store, cancellation, invocation, engine));
    tobkiri_wasm::reap_allocation_owners().unwrap();
    let remaining = account.snapshot().unwrap();
    assert_eq!((remaining.charged_bytes, remaining.reservations), (0, 0));
}
fn packet(
    invocation: &NativeSetupInvocation,
    budget: &Arc<dyn AllocationBudget>,
    operation: &str,
    steps: Value,
    args: Value,
) -> CoreValue {
    let scope = invocation.provenance();
    let bytes = serde_json::to_vec(&json!({"format":"io.tobkiri.native-catalog-request.v1", "scope":[format!("{:x}",scope.account_generation),format!("{:x}",scope.invocation),"1","1"], "target":{"binding":"resolved_module_fixture","steps":steps}, "operation":operation,"args":args})).unwrap();
    CoreValue::parse_guest(&bytes, budget).unwrap()
}
fn call(
    io: &mut CatalogIo,
    source: &Source,
    invocation: &NativeSetupInvocation,
    budget: &Arc<dyn AllocationBudget>,
    operation: &str,
    steps: Value,
    args: Value,
) -> Value {
    let output = io
        .inspect(
            source,
            invocation,
            &packet(invocation, budget, operation, steps, args),
            budget,
        )
        .unwrap();
    assert!(io.cleanup.receipt().verified());
    serde_json::from_slice::<Value>(output.as_bytes()).unwrap()["result"].clone()
}

#[tokio::test(flavor = "current_thread")]
async fn resolver_hashes_the_second_physical_lock_after_a_new_bundle_selection() {
    for development in [false, true] {
        with_source(development, |source, invocation, _, io, cleanup| {
            let bundle = if development {
                let root = source.root.join("bundled/dev-defaults");
                fs::create_dir_all(root.join("platform-artifacts")).unwrap();
                root.join("v4")
            } else {
                source.root.join("ecosystem/defaultspack/v4")
            };
            fs::create_dir_all(&bundle).unwrap();
            if development {
                assert!(io.select_development(source, invocation).unwrap());
            } else {
                assert!(!io.select_development(source, invocation).unwrap());
                io.select_packaged(&bundle).unwrap();
            }
            let path = bundle.join("bundle.lock.json");
            assert!(matches!(
                io.resolver_lock_digest(source, invocation).unwrap_err().cause,
                Some(OriginalAuthorityCause::Io(error))
                    if error.errno == Some(libc::ENOENT) && error.filename.as_deref() == path.to_str()
            ));
            let first = br#"{"entries":[]}"#;
            fs::write(&path, first).unwrap();
            assert_eq!(
                io.resolver_lock_digest(source, invocation).unwrap(),
                Digest::hash(first)
            );
            let second = b"{\"entries\":[]}\n";
            fs::write(&path, second).unwrap();
            assert_eq!(
                io.resolver_lock_digest(source, invocation).unwrap(),
                Digest::hash(second)
            );
            assert!(cleanup.receipt().verified());
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn pack_resolver_lock_only_rereads_the_completed_loaders_fixed_occurrence() {
    for case in ["reread", "symlink", "missing", "read-error"] {
        with_source(false, |source, invocation, budget, io, cleanup| {
            let bundle = source.root.join("ecosystem/defaultspack/v4");
            fs::create_dir_all(&bundle).unwrap();
            let path = bundle.join("bundle.lock.json");
            fs::write(&path, br#"{"entries":[]}"#).unwrap();
            io.select_packaged(&bundle).unwrap();
            io.bind_loader(CoreValue::string("resolved_module_fixture", budget).unwrap())
                .unwrap();
            let lock = json!([["join", "bundle.lock.json"]]);
            let inspect = |io: &mut CatalogIo, operation: &str, steps: Value, args: Value| {
                io.inspect_pack_resolve(
                    source,
                    invocation,
                    &packet(invocation, budget, operation, steps, args),
                    budget,
                )
            };
            let result = |io: &mut CatalogIo, operation: &str| {
                let output = inspect(io, operation, lock.clone(), json!([])).unwrap();
                assert!(cleanup.receipt().verified());
                serde_json::from_slice::<Value>(output.as_bytes()).unwrap()["result"].clone()
            };
            assert!(io.begin_pack_resolve().is_err());
            assert!(inspect(io, "is-symlink", lock.clone(), json!([])).is_err());
            for (operation, steps, args) in [
                (
                    "root",
                    json!([["parent", ""], ["parent", ""], ["join", "v4"]]),
                    json!([]),
                ),
                ("is-symlink", json!([]), json!([])),
                ("resolve", json!([]), json!([true])),
                ("is-symlink", lock.clone(), json!([])),
                ("read-bytes", lock.clone(), json!([])),
                (
                    "is-dir",
                    json!([["parent", ""], ["join", "platform-artifacts"]]),
                    json!([]),
                ),
            ] {
                call(io, source, invocation, budget, operation, steps, args);
            }
            assert!(io.completed());
            assert!(io.select_packaged(&source.root).is_err());
            if case == "reread" {
                fs::write(&path, b"new resolver bytes").unwrap();
            } else if matches!(case, "symlink" | "missing") {
                let moved = bundle.join("old-lock.json");
                fs::rename(&path, &moved).unwrap();
                if case == "symlink" {
                    std::os::unix::fs::symlink(&moved, &path).unwrap();
                }
            }
            io.begin_pack_resolve().unwrap();
            assert!(io.begin_pack_resolve().is_err());
            assert!(inspect(io, "read-bytes", lock.clone(), json!([])).is_err());
            assert!(inspect(io, "is-symlink", lock.clone(), json!([false])).is_err());
            let request = packet(invocation, budget, "is-symlink", lock.clone(), json!([]));
            let request: Value =
                serde_json::from_slice(request.encode_guest(budget).unwrap().as_bytes()).unwrap();
            for (field, replacement) in [
                ("scope", json!(["0", "0", "1", "1"])),
                (
                    "target",
                    json!({"binding": "different_loader", "steps": lock.clone()}),
                ),
            ] {
                let mut forged = request.clone();
                forged[field] = replacement;
                let forged =
                    CoreValue::parse_guest(&serde_json::to_vec(&forged).unwrap(), budget).unwrap();
                assert!(
                    io.inspect_pack_resolve(source, invocation, &forged, budget)
                        .is_err()
                );
            }
            assert!(
                inspect(io, "is-symlink", json!([["join", "other.json"]]), json!([]),).is_err()
            );
            assert!(
                inspect(
                    io,
                    "is-symlink",
                    json!([["join", "./bundle.lock.json"]]),
                    json!([]),
                )
                .is_err()
            );
            assert!(
                inspect(
                    io,
                    "is-symlink",
                    json!([["join", "."], ["join", "bundle.lock.json"]]),
                    json!([]),
                )
                .is_err()
            );
            assert_eq!(result(io, "is-symlink")["value"], case == "symlink");
            assert!(inspect(io, "is-symlink", lock.clone(), json!([])).is_err());
            if case == "symlink" {
                assert!(inspect(io, "is-file", lock.clone(), json!([])).is_err());
            } else {
                assert_eq!(result(io, "is-file")["value"], case != "missing");
            }
            assert!(inspect(io, "is-file", lock.clone(), json!([])).is_err());
            assert!(!io.pack_resolve_lock_completed());
            if case == "reread" {
                assert_eq!(
                    result(io, "read-bytes")["value"],
                    "bmV3IHJlc29sdmVyIGJ5dGVz"
                );
            } else if case == "read-error" {
                let moved = bundle.join("old-lock.json");
                fs::rename(&path, &moved).unwrap();
                let error = inspect(io, "read-bytes", lock.clone(), json!([])).unwrap_err();
                let error = error.downcast_ref::<OriginalAuthorityError>().unwrap();
                assert!(matches!(
                    &error.cause,
                    Some(OriginalAuthorityCause::Io(error))
                        if error.errno == Some(libc::ENOENT)
                            && error.filename.as_deref() == io.selected.join("bundle.lock.json").to_str()
                ));
                assert_eq!(
                    io.error_graph(error, budget)
                        .unwrap()
                        .code()
                        .as_str()
                        .unwrap(),
                    "file-not-found-error",
                );
                fs::rename(moved, &path).unwrap();
            }
            assert!(inspect(io, "read-bytes", lock.clone(), json!([])).is_err());
            assert!(io.begin_pack_resolve().is_err());
            assert_eq!(io.pack_resolve_lock_completed(), case == "reread");
            assert!(cleanup.receipt().verified());
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn pack_resolver_shell_root_uses_original_and_resolved_paths_in_order() {
    for case in [
        "present",
        "missing",
        "sibling-link",
        "outside-link",
        "file",
        "parent-replaced",
    ] {
        with_source(false, |source, invocation, budget, io, cleanup| {
            let parent = source.root.join("ecosystem/defaultspack");
            let bundle = parent.join("v4");
            fs::create_dir_all(&bundle).unwrap();
            let parent = parent.canonicalize().unwrap();
            let artifact = parent.join("platform-artifacts");
            fs::create_dir(&artifact).unwrap();
            fs::write(bundle.join("bundle.lock.json"), br#"{"entries":[]}"#).unwrap();
            io.select_packaged(&bundle).unwrap();
            io.bind_loader(CoreValue::string("resolved_module_fixture", budget).unwrap())
                .unwrap();
            let original = json!([["parent", ""], ["join", "platform-artifacts"]]);
            let resolved = json!([["shell-resolved-root", ""]]);
            let lock = json!([["join", "bundle.lock.json"]]);
            let sequence = [
                ("is-symlink", lock.clone(), json!([])),
                ("is-file", lock.clone(), json!([])),
                ("read-bytes", lock.clone(), json!([])),
            ];
            for (operation, steps, args) in [
                (
                    "root",
                    json!([["parent", ""], ["parent", ""], ["join", "v4"]]),
                    json!([]),
                ),
                ("is-symlink", json!([]), json!([])),
                ("resolve", json!([]), json!([true])),
                ("is-symlink", lock.clone(), json!([])),
                ("read-bytes", lock, json!([])),
                ("is-dir", original.clone(), json!([])),
                ("resolve", original.clone(), json!([true])),
            ] {
                call(io, source, invocation, budget, operation, steps, args);
            }
            let inspect = |io: &mut CatalogIo, operation: &str, steps: Value, args: Value| {
                io.inspect_pack_resolve(
                    source,
                    invocation,
                    &packet(invocation, budget, operation, steps, args),
                    budget,
                )
                .map(|value| {
                    assert!(cleanup.receipt().verified());
                    serde_json::from_slice::<Value>(value.as_bytes()).unwrap()["result"].clone()
                })
            };
            if case != "present" {
                fs::rename(&artifact, parent.join("parked")).unwrap();
                match case {
                    "sibling-link" => std::os::unix::fs::symlink("parked", &artifact).unwrap(),
                    "outside-link" => {
                        std::os::unix::fs::symlink("../out-of-scope", &artifact).unwrap()
                    }
                    "file" => fs::write(&artifact, b"replaced directory").unwrap(),
                    _ => {}
                }
            }
            io.begin_pack_resolve().unwrap();
            assert!(inspect(io, "resolve", original.clone(), json!([true])).is_err());
            for (operation, steps, args) in sequence {
                inspect(io, operation, steps, args).unwrap();
            }
            if case == "parent-replaced" {
                fs::rename(&parent, parent.with_file_name("replaced-parent")).unwrap();
                fs::create_dir_all(&artifact).unwrap();
            }
            assert!(inspect(io, "resolve", original.clone(), json!([false])).is_err());
            assert!(inspect(io, "resolve", resolved.clone(), json!([true])).is_err());
            assert!(inspect(io, "is-symlink", original.clone(), json!([])).is_err());
            let path = inspect(io, "resolve", original.clone(), json!([true]));
            if matches!(case, "missing" | "outside-link" | "parent-replaced") {
                let error = path.unwrap_err();
                let error = error.downcast_ref::<OriginalAuthorityError>().unwrap();
                if case == "missing" {
                    assert!(
                        matches!(&error.cause, Some(OriginalAuthorityCause::Io(error))
                        if error.errno == Some(libc::ENOENT)
                        && error.filename.as_deref() == artifact.to_str())
                    );
                } else {
                    assert!(matches!(
                        &error.cause,
                        Some(OriginalAuthorityCause::SourceUnavailable)
                    ));
                }
                assert!(inspect(io, "resolve", original, json!([true])).is_err());
                assert!(!io.pack_resolve_shell_root_completed());
                return;
            }
            let expected = if case == "sibling-link" {
                parent.join("parked")
            } else {
                artifact
            };
            let path = path.unwrap();
            assert_eq!(path["steps"], resolved);
            assert_eq!(path["value"], expected.to_str().unwrap());
            assert!(inspect(io, "is-dir", resolved.clone(), json!([])).is_err());
            assert_eq!(
                inspect(io, "is-symlink", original, json!([])).unwrap()["value"],
                case == "sibling-link",
            );
            if case == "sibling-link" {
                assert!(inspect(io, "is-dir", resolved, json!([])).is_err());
            } else {
                assert_eq!(
                    inspect(io, "is-dir", resolved.clone(), json!([])).unwrap()["value"],
                    case == "present",
                );
                assert!(inspect(io, "is-dir", resolved, json!([])).is_err());
            }
            assert_eq!(io.pack_resolve_shell_root_completed(), case == "present");
            assert!(
                inspect(
                    io,
                    "is-symlink",
                    json!([["shell-resolved-root", ""], ["join", "Tobkiri.app"]]),
                    json!([])
                )
                .is_err()
            );
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn catalog_reader_preserves_pin_reread_and_closes_each_actual_file() {
    with_source(false, |source, invocation, budget, io, _| {
        let bundle = source.root.join("ecosystem/defaultspack/v4");
        fs::create_dir_all(&bundle).unwrap();
        fs::write(bundle.join("catalog.json"), b"first").unwrap();
        fs::write(bundle.join("bundle.lock.json"), br#"{"schema":"test-resource-index-only","entries":[{"path":"catalog.json","kind":"executable_catalog","digest":"not-a-Pack-validation-test"}]}"#).unwrap();
        io.select_packaged(&bundle).unwrap();
        io.bind_loader(CoreValue::string("resolved_module_fixture", budget).unwrap())
            .unwrap();
        call(
            io,
            source,
            invocation,
            budget,
            "root",
            json!([["parent", ""], ["parent", ""], ["join", "v4"]]),
            json!([]),
        );
        call(
            io,
            source,
            invocation,
            budget,
            "is-symlink",
            json!([]),
            json!([]),
        );
        call(
            io,
            source,
            invocation,
            budget,
            "resolve",
            json!([]),
            json!([true]),
        );
        let lock = json!([["join", "bundle.lock.json"]]);
        call(
            io,
            source,
            invocation,
            budget,
            "is-symlink",
            lock.clone(),
            json!([]),
        );
        call(
            io,
            source,
            invocation,
            budget,
            "read-bytes",
            lock,
            json!([]),
        );
        let entry = json!([["join", "catalog.json"]]);
        call(
            io,
            source,
            invocation,
            budget,
            "resolve",
            entry.clone(),
            json!([true]),
        );
        call(
            io,
            source,
            invocation,
            budget,
            "is-symlink",
            entry.clone(),
            json!([]),
        );
        assert_eq!(
            call(
                io,
                source,
                invocation,
                budget,
                "read-bytes",
                entry.clone(),
                json!([])
            )["value"],
            "Zmlyc3Q="
        );
        fs::write(bundle.join("catalog.json"), b"second").unwrap();
        assert_eq!(
            call(
                io,
                source,
                invocation,
                budget,
                "read-bytes",
                entry,
                json!([])
            )["value"],
            "c2Vjb25k"
        );
        call(
            io,
            source,
            invocation,
            budget,
            "is-dir",
            json!([["parent", ""], ["join", "platform-artifacts"]]),
            json!([]),
        );
        assert!(io.completed());
        assert!(
            io.inspect(
                source,
                invocation,
                &packet(
                    invocation,
                    budget,
                    "read-bytes",
                    json!([["join", "catalog.json"]]),
                    json!([])
                ),
                budget
            )
            .is_err()
        );
        let oversized = io
            .inspect(
                source,
                invocation,
                &packet(
                    invocation,
                    budget,
                    "read-bytes",
                    json!([["join", "x".repeat(8192)]]),
                    json!([]),
                ),
                budget,
            )
            .err()
            .unwrap();
        assert!(matches!(
            oversized
                .downcast_ref::<OriginalAuthorityError>()
                .unwrap()
                .cause,
            Some(OriginalAuthorityCause::Capacity)
        ));
        assert!(io.cleanup.receipt().verified());
    });
}

#[tokio::test(flavor = "current_thread")]
async fn catalog_development_selection_uses_original_inputs_and_skips_manifest() {
    with_source(true, |source, invocation, _, io, _| {
        let generated = source.root.join("bundled/dev-defaults");
        fs::create_dir_all(generated.join("v4")).unwrap();
        fs::create_dir_all(generated.join("platform-artifacts")).unwrap();
        assert!(io.select_development(source, invocation).unwrap());
        assert!(io.manifest(source, invocation).unwrap().is_none());
        assert_eq!(io.selected, generated.join("v4"));
    });
}

#[tokio::test(flavor = "current_thread")]
async fn catalog_cancelled_read_refuses_before_open_and_leaves_no_descriptor() {
    with_source(false, |source, invocation, budget, io, cleanup| {
        let cancellation = io.cancellation.clone();
        let op = io.operation(source, invocation, &cancellation).unwrap();
        io.cancellation.cancel();
        let failure = read_file(&op, &source.root.join("unopened-catalog.json"), budget)
            .err()
            .unwrap();
        assert!(matches!(
            failure.cause,
            Some(OriginalAuthorityCause::Cancelled)
        ));
        // The cancelled operation never opens even this missing resource;
        // this is cancellation priority, not a synthesized ENOENT result.
        assert!(!source.root.join("unopened-catalog.json").exists());
        assert!(cleanup.receipt().verified());
        assert!(Arc::ptr_eq(&io.budget, budget));
    });
}

#[tokio::test(flavor = "current_thread")]
async fn catalog_binding_walk_uses_actual_hashes_preserves_ignored_surrogates_and_detects_change() {
    with_source(false, |source, invocation, _, io, _| {
        let bundle = source.root.join("ecosystem/defaultspack/v4");
        fs::create_dir_all(&bundle).unwrap();
        fs::create_dir_all(bundle.parent().unwrap().join("platform-artifacts")).unwrap();
        fs::write(bundle.join("catalog.json"), b"abc").unwrap();
        io.select_packaged(&bundle).unwrap();
        let manifest = br#"{"schema":"io.tobkiri.runtime-resource-manifest.v1","ignored":NaN,"entries":[{"path":"ecosystem/defaultspack/v4/catalog.json","size":3,"sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"},{"path":"\ud800","size":0,"sha256":"0000000000000000000000000000000000000000000000000000000000000000"}]}"#;
        assert!(io.verify_binding(source, invocation, manifest).is_ok());
        fs::write(bundle.join("catalog.json"), b"bad").unwrap();
        let failure = io.verify_binding(source, invocation, manifest);
        assert!(
            matches!(failure, Err(BindingFailure::Original(error)) if error.class == crate::bootstrap_review::DENIED && error.message.contains("not launcher-bound"))
        );
    });
}
