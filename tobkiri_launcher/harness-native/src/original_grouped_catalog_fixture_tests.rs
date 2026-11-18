//! Exercise the grouped native transport against every physical shipped member.
use super::*;
use crate::original_authority::bundled_schema::{CatalogDocumentKind, CatalogSchemas};
use crate::original_authority::io::publication::bundled_catalog_checks::{
    ExecutableCatalogIssue, definition_revision_matches, executable_pin,
};
use std::collections::BTreeMap;

fn open_grouped_catalog(
    io: &mut CatalogIo,
    source: &Source,
    invocation: &NativeSetupInvocation,
    budget: &Arc<dyn AllocationBudget>,
    bundle: &Path,
) -> Vec<u8> {
    io.select_packaged(bundle).unwrap();
    io.enable_document_shapes();
    io.retain_review_pack_dependencies();
    io.bind_loader(CoreValue::string("resolved_module_fixture", budget).unwrap())
        .unwrap();
    let lock_steps = json!([["join", "bundle.lock.json"]]);
    for (operation, steps, args) in [
        (
            "root",
            json!([["parent", ""], ["parent", ""], ["join", "v4"]]),
            json!([]),
        ),
        ("is-symlink", json!([]), json!([])),
        ("resolve", json!([]), json!([true])),
        ("is-symlink", lock_steps.clone(), json!([])),
    ] {
        call(io, source, invocation, budget, operation, steps, args);
    }
    let lock_reply = call(
        io,
        source,
        invocation,
        budget,
        "read-bytes",
        lock_steps,
        json!([]),
    );
    assert_eq!(lock_reply["kind"], "bytes");
    base64::engine::general_purpose::STANDARD
        .decode(lock_reply["value"].as_str().unwrap())
        .unwrap()
}

#[tokio::test(flavor = "current_thread")]
async fn grouped_catalog_reads_entire_shipped_bundle_and_executable_pins() {
    let shipped = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tobkiri_runtime/ecosystem/defaultspack/v4");
    let lock_bytes = fs::read(shipped.join("bundle.lock.json")).unwrap();
    let lock: Value = serde_json::from_slice(&lock_bytes).unwrap();
    let entries = lock["entries"].as_array().unwrap();
    assert_eq!(entries.len(), 305);

    with_source(false, |source, invocation, budget, io, cleanup| {
        let bundle = source.root.join("ecosystem/defaultspack/v4");
        let artifacts = bundle.parent().unwrap().join("platform-artifacts");
        fs::create_dir_all(&bundle).unwrap();
        fs::create_dir(&artifacts).unwrap();
        fs::write(bundle.join("bundle.lock.json"), &lock_bytes).unwrap();
        for entry in entries {
            let relative = entry["path"].as_str().unwrap();
            assert!(
                Path::new(relative)
                    .components()
                    .all(|part| matches!(part, Component::Normal(_))),
                "unexpected shipped member spelling: {relative}"
            );
            let original = shipped.join(relative);
            let member = bundle.join(relative);
            assert!(fs::symlink_metadata(&original).unwrap().is_file());
            fs::create_dir_all(member.parent().unwrap()).unwrap();
            fs::copy(&original, &member).unwrap();
            assert!(fs::symlink_metadata(&member).unwrap().is_file());
        }

        assert_eq!(
            open_grouped_catalog(io, source, invocation, budget, &bundle),
            lock_bytes
        );

        let cancellation = io.cancellation.clone();
        let op = io.operation(source, invocation, &cancellation).unwrap();
        let mut manifests = BTreeMap::new();
        let mut checked_revisions = 0;
        let mut expected_index = Vec::with_capacity(entries.len());
        for (index, entry) in entries.iter().enumerate() {
            let relative = entry["path"].as_str().unwrap();
            let expected = fs::read(shipped.join(relative)).unwrap();
            assert_eq!(
                Digest::hash(&expected).as_str(),
                entry["digest"].as_str().unwrap(),
                "{relative}"
            );
            let reply = call(
                io,
                source,
                invocation,
                budget,
                "read-entry",
                json!([["join", relative]]),
                json!([index]),
            );
            assert_eq!(reply["kind"], "document", "{relative}");
            assert_eq!(reply.as_object().unwrap().len(), 2, "{relative}");
            assert!(
                reply["value"]
                    .as_str()
                    .is_some_and(|value| value.as_bytes() == expected.as_slice()),
                "document bytes differ at {index}: {relative}"
            );
            assert_eq!(io.entry, index + 1);
            use CatalogDocumentKind::{Base, ExecutableCatalog, Pack, Profile, Shell};
            let document: Value = serde_json::from_slice(&expected).unwrap();
            let (kind, id) = match entry["kind"].as_str().unwrap() {
                "pack" => (Pack, &document["pack"]["id"]),
                "executable_catalog" => (ExecutableCatalog, &document["pack_id"]),
                "base" => (Base, &document["pack_id"]),
                "shell" => (Shell, &document["provider_id"]),
                "profile" => (Profile, &document["profile_id"]),
                _ => panic!("unexpected lock kind: {relative}"),
            };
            let id = id.as_str().unwrap();
            expected_index.push((kind, id.to_owned()));
            if kind == Pack {
                assert!(manifests.insert(id.to_owned(), relative).is_none(), "{id}");
            }
            if matches!(kind, Base | Shell) {
                let document = json::decode_read_only_strict(&expected, &op, budget).unwrap();
                assert!(
                    definition_revision_matches(&document, &op, budget).unwrap(),
                    "{relative}"
                );
                checked_revisions += 1;
            }
        }
        assert_eq!(checked_revisions, 3);
        let assert_index = |io: &CatalogIo| {
            let expected: Vec<_> = expected_index
                .iter()
                .map(|(kind, id)| (*kind, id.as_str()))
                .collect();
            assert_eq!(
                io.checked_member_index()
                    .unwrap()
                    .entries()
                    .collect::<Vec<_>>(),
                expected
            );
        };
        assert_index(io);

        let mut successful_pins = 0;
        for (index, entry) in entries.iter().enumerate() {
            if entry["kind"] != "executable_catalog" {
                continue;
            }
            let relative = entry["path"].as_str().unwrap();
            // Use the verified original bytes for the oracle inputs so these
            // checks add no read to the physical loader's controlled copy.
            let executable = fs::read(shipped.join(relative)).unwrap();
            let executable = json::decode_read_only_strict(&executable, &op, budget).unwrap();
            let pack_id = executable.get(executable.root, "pack_id").unwrap();
            let Node::String(pack_id) = executable.node(pack_id) else {
                panic!("executable catalog has an invalid Pack id: {relative}");
            };
            let manifest_path = manifests[&pack_id.utf8().unwrap()];
            let manifest = fs::read(shipped.join(manifest_path)).unwrap();
            let manifest = json::decode_read_only_strict(&manifest, &op, budget).unwrap();
            let pin = executable_pin(&executable, Some(&manifest), &op, budget)
                .unwrap_or_else(|error| panic!("{relative}: {error}"))
                .unwrap_or_else(|issue| panic!("{relative}: {issue:?}"));
            let reply = call(
                io,
                source,
                invocation,
                budget,
                "read-pin-digest",
                json!([["join", relative]]),
                json!([index]),
            );
            assert_eq!(
                reply,
                json!({"kind": "catalog-pin-digest", "value": entry["digest"]}),
                "{relative}"
            );
            assert!(pin.is(reply["value"].as_str().unwrap()), "{relative}");
            successful_pins += 1;
            assert_eq!(io.pins, successful_pins);
        }
        assert_eq!(successful_pins, 148);
        assert!(!io.completed());

        let artifact_steps = json!(ARTIFACT_ROOT_STEPS);
        assert_eq!(
            call(
                io,
                source,
                invocation,
                budget,
                "is-dir",
                artifact_steps.clone(),
                json!([]),
            ),
            json!({"kind": "bool", "value": true})
        );
        let resolved = fs::canonicalize(&artifacts).unwrap();
        assert_eq!(
            call(
                io,
                source,
                invocation,
                budget,
                "resolve",
                artifact_steps.clone(),
                json!([true]),
            ),
            json!({"kind": "path", "binding": "resolved_module_fixture",
                "value": resolved.to_str().unwrap(), "steps": artifact_steps})
        );
        assert!(io.completed());
        assert_index(io);
        assert_eq!(io.artifact_root.as_deref(), Some(resolved.as_path()));
        assert!(io.artifact_parent_identity.is_some());
        assert!(cleanup.verified());
        assert!(
            Instant::now() < invocation.deadline().unwrap(),
            "physical catalog fixture exceeded its original invocation deadline after {:?}",
            invocation.started_at().elapsed()
        );
    });
}

#[tokio::test(flavor = "current_thread")]
async fn grouped_catalog_invalidates_member_index_without_disabling_typed_reads() {
    let base: Value = serde_json::from_slice(include_bytes!(
        "../../../tobkiri_runtime/ecosystem/defaultspack/v4/defaults-basepack.base.v1.json"
    ))
    .unwrap();
    let base = serde_json::to_vec(&base).unwrap();
    let base = base.as_slice();
    let pack = include_bytes!(
        "../../../tobkiri_runtime/ecosystem/defaultspack/v4/packs/defaultspack.pack.v4.json"
    )
    .as_slice();
    let mut unsupported_pack: Value = serde_json::from_slice(include_bytes!(
        "../../../tobkiri_runtime/ecosystem/defaultspack/v4/packs/defaults-basepack.pack.v4.json"
    ))
    .unwrap();
    unsupported_pack["requirements"]["network"] =
        json!({"matrix.bindings": [{"caller_function_id": {"part": "caller"}}]});
    let unsupported_pack = serde_json::to_vec(&unsupported_pack).unwrap();

    for (case, files, first) in [
        (
            "duplicate Base",
            [("base", base), ("base", base), ("pack", pack)],
            (CatalogDocumentKind::Base, "defaults-basepack"),
        ),
        (
            "unsupported Pack",
            [
                ("pack", pack),
                ("pack", unsupported_pack.as_slice()),
                ("base", base),
            ],
            (CatalogDocumentKind::Pack, "defaultspack"),
        ),
    ] {
        with_source(false, |source, invocation, budget, io, _| {
            let bundle = source.root.join("ecosystem/defaultspack/v4");
            fs::create_dir_all(&bundle).unwrap();
            let paths = ["first.json", "fallback.json", "last.json"];
            let entries: Vec<_> = paths
                .iter()
                .zip(files)
                .map(|(path, (kind, bytes))| {
                    fs::write(bundle.join(path), bytes).unwrap();
                    json!({"path": path, "kind": kind, "digest": Digest::hash(bytes).as_str()})
                })
                .collect();
            let lock = serde_json::to_vec(&json!({
                "schema": "io.tobkiri.defaultspack-bundle-lock.v1", "entries": entries
            }))
            .unwrap();
            fs::write(bundle.join("bundle.lock.json"), &lock).unwrap();
            assert_eq!(
                open_grouped_catalog(io, source, invocation, budget, &bundle),
                lock
            );
            if first.0 == CatalogDocumentKind::Base {
                let cancellation = io.cancellation.clone();
                let op = io.operation(source, invocation, &cancellation).unwrap();
                io.ensure_lock(&op).unwrap();
                let steps = CoreValue::parse_guest(br#"[["join","first.json"]]"#, budget).unwrap();
                let args = CoreValue::parse_guest(b"[0]", budget).unwrap();
                let typed = json!({"kind":"document", "value": std::str::from_utf8(base).unwrap()});
                // Fit the inner result but reject the final enclosing brace.
                let limit = serde_json::to_vec(&typed).unwrap().len();
                let error = io
                    .read_checked_entry(
                        &op,
                        CoreBuffer::builder(budget, limit),
                        &steps.array().unwrap(),
                        &args.array().unwrap(),
                    )
                    .unwrap_err();
                assert!(matches!(
                    error.downcast_ref::<tobkiri_canon::AllocationError>(),
                    Some(tobkiri_canon::AllocationError::Limit)
                ));
                assert_eq!(io.entry, 0);
                assert_eq!(io.checked_member_index().unwrap().entries().count(), 0);
            }

            for (index, (path, (_, expected))) in paths.iter().zip(files).enumerate() {
                let reply = call(
                    io,
                    source,
                    invocation,
                    budget,
                    "read-entry",
                    json!([["join", path]]),
                    json!([index]),
                );
                let value = reply["value"].as_str().unwrap();
                if index == 1 {
                    assert_eq!(reply["kind"], "bytes", "{case}");
                    assert_eq!(
                        base64::engine::general_purpose::STANDARD
                            .decode(value)
                            .unwrap(),
                        expected,
                        "{case}"
                    );
                } else {
                    assert_eq!(reply["kind"], "document", "{case}");
                    assert_eq!(value.as_bytes(), expected, "{case}");
                }
                if index == 0 {
                    assert!(
                        io.checked_member_index().unwrap().entries().eq([first]),
                        "{case}"
                    );
                } else {
                    assert!(io.checked_member_index().is_none(), "{case}");
                }
            }
            assert_eq!(
                call(
                    io,
                    source,
                    invocation,
                    budget,
                    "is-dir",
                    json!(ARTIFACT_ROOT_STEPS),
                    json!([])
                ),
                json!({"kind": "bool", "value": false})
            );
            assert!(io.completed(), "{case}");
            assert!(io.checked_member_index().is_none(), "{case}");
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn grouped_catalog_stale_definition_revision_returns_original_bytes() {
    const RELATIVE: &str = "defaults-basepack.base.v1.json";
    let mut base: Value = serde_json::from_slice(include_bytes!(
        "../../../tobkiri_runtime/ecosystem/defaultspack/v4/defaults-basepack.base.v1.json"
    ))
    .unwrap();
    base["definition_revision"] = json!(Digest::hash(b"stale definition revision").as_str());
    let bytes = serde_json::to_vec(&base).unwrap();
    let lock = serde_json::to_vec(&json!({
        "schema": "io.tobkiri.defaultspack-bundle-lock.v1",
        "entries": [{"path": RELATIVE, "kind": "base", "digest": Digest::hash(&bytes).as_str()}],
    }))
    .unwrap();

    with_source(false, |source, invocation, budget, io, _| {
        let bundle = source.root.join("ecosystem/defaultspack/v4");
        fs::create_dir_all(&bundle).unwrap();
        fs::write(bundle.join(RELATIVE), &bytes).unwrap();
        fs::write(bundle.join("bundle.lock.json"), &lock).unwrap();
        assert_eq!(
            open_grouped_catalog(io, source, invocation, budget, &bundle),
            lock
        );

        let cancellation = io.cancellation.clone();
        let op = io.operation(source, invocation, &cancellation).unwrap();
        assert!(
            CatalogSchemas::default()
                .validate_document(&op, budget, CatalogDocumentKind::Base, &bytes)
                .unwrap()
        );
        let reply = call(
            io,
            source,
            invocation,
            budget,
            "read-entry",
            json!([["join", RELATIVE]]),
            json!([0]),
        );
        assert_eq!(reply["kind"], "bytes");
        assert_eq!(
            base64::engine::general_purpose::STANDARD
                .decode(reply["value"].as_str().unwrap())
                .unwrap(),
            bytes
        );
        assert_eq!(io.entry, 1);
    });
}

#[tokio::test(flavor = "current_thread")]
async fn executable_catalog_prechecks_keep_original_error_precedence() {
    let executable: Value = serde_json::from_slice(include_bytes!(
        "../../../tobkiri_runtime/ecosystem/defaultspack/v4/packs/defaultspack.executables.v4.json"
    ))
    .unwrap();
    let manifest: Value = serde_json::from_slice(include_bytes!(
        "../../../tobkiri_runtime/ecosystem/defaultspack/v4/packs/defaultspack.pack.v4.json"
    ))
    .unwrap();
    let pin = manifest["artifacts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|item| item["path"] == "executables.v4.json")
        .unwrap();
    let mut missing_pin = manifest.clone();
    missing_pin["artifacts"] = json!([]);
    let mut duplicate_pin = manifest.clone();
    duplicate_pin["artifacts"] = json!([pin, pin]);
    let mut malformed_digest = pin.clone();
    malformed_digest["digest"] = json!(0);
    let mut malformed_pin = manifest.clone();
    malformed_pin["artifacts"] = json!([malformed_digest]);
    let digest = pin["digest"].as_str().unwrap();
    let digest_with_newline = format!("{digest}\n");
    let mut pin_with_newline = pin.clone();
    pin_with_newline["digest"] = json!(&digest_with_newline);
    let mut manifest_with_newline = manifest.clone();
    manifest_with_newline["artifacts"] = json!([pin_with_newline]);
    let mut stale_catalog = executable.clone();
    stale_catalog["catalog_digest"] = json!(Digest::hash(b"stale digest").as_str());
    let mut stale_source = stale_catalog.clone();
    stale_source["source_identity"] = json!(Digest::hash(b"stale source").as_str());

    with_source(false, |source, invocation, budget, io, _| {
        let cancellation = io.cancellation.clone();
        let op = io.operation(source, invocation, &cancellation).unwrap();
        let decode = |value: &Value| {
            let bytes = serde_json::to_vec(value).unwrap();
            json::decode_read_only_strict(&bytes, &op, budget).unwrap()
        };
        for (document, owner, expected) in [
            (
                &stale_source,
                None,
                ExecutableCatalogIssue::MissingPackManifest,
            ),
            (
                &stale_source,
                Some(&missing_pin),
                ExecutableCatalogIssue::StaleSourceIdentity,
            ),
            (
                &stale_catalog,
                Some(&missing_pin),
                ExecutableCatalogIssue::StaleCatalogDigest,
            ),
            (
                &executable,
                Some(&missing_pin),
                ExecutableCatalogIssue::MissingOrDuplicateArtifactPin,
            ),
            (
                &executable,
                Some(&duplicate_pin),
                ExecutableCatalogIssue::MissingOrDuplicateArtifactPin,
            ),
            (
                &executable,
                Some(&malformed_pin),
                ExecutableCatalogIssue::InvalidArtifactPinDigest,
            ),
        ] {
            let document = decode(document);
            let owner = owner.map(decode);
            assert!(
                matches!(executable_pin(&document, owner.as_deref(), &op, budget).unwrap(),
                    Err(issue) if issue == expected),
                "expected {expected:?}"
            );
        }
        let executable = decode(&executable);
        let manifest = decode(&manifest_with_newline);
        let pin = executable_pin(&executable, Some(&manifest), &op, budget)
            .unwrap()
            .unwrap();
        assert!(pin.is(&digest_with_newline));
        assert!(!pin.is(digest));
    });
}
