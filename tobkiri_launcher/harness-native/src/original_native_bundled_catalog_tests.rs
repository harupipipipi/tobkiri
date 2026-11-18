//! Read the shipped catalog from a controlled physical copy through its native owner.
use super::*;
use crate::original_authority::bundled_schema::CatalogDocumentKind;

fn copy_bundle(shipped: &Path, bundle: &Path, lock: &[u8], rows: &[Value]) {
    fs::create_dir_all(bundle).unwrap();
    fs::write(bundle.join("bundle.lock.json"), lock).unwrap();
    for row in rows {
        let relative = row["path"].as_str().unwrap();
        assert!(
            Path::new(relative)
                .components()
                .all(|part| matches!(part, Component::Normal(_)))
        );
        let original = shipped.join(relative);
        let destination = bundle.join(relative);
        assert!(fs::symlink_metadata(&original).unwrap().is_file());
        fs::create_dir_all(destination.parent().unwrap()).unwrap();
        fs::copy(original, destination).unwrap();
    }
}

#[tokio::test(flavor = "current_thread")]
async fn native_owned_bundle_preserves_physical_members_pins_and_artifacts() {
    let shipped = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tobkiri_runtime/ecosystem/defaultspack/v4");
    let lock_bytes = fs::read(shipped.join("bundle.lock.json")).unwrap();
    let lock: Value = serde_json::from_slice(&lock_bytes).unwrap();
    let rows = lock["entries"].as_array().unwrap();
    assert_eq!(rows.len(), 305);

    with_source(false, |source, invocation, _, io, cleanup| {
        let bundle = source.root.join("ecosystem/defaultspack/v4");
        let artifacts = bundle.parent().unwrap().join("platform-artifacts");
        copy_bundle(&shipped, &bundle, &lock_bytes, rows);
        fs::create_dir(&artifacts).unwrap();
        io.select_packaged(&bundle).unwrap();
        let selected = fs::canonicalize(&bundle).unwrap();
        let artifacts = fs::canonicalize(&artifacts).unwrap();
        let before = invocation.capacity_snapshot().unwrap();
        let native = io
            .read_owned_bundle(source, invocation)
            .unwrap_or_else(|error| panic!("native bundle: {error}"));
        assert_eq!(AsRef::<Path>::as_ref(&native.root), selected.as_path());
        assert_eq!(native.artifact_root.as_deref(), Some(artifacts.as_path()));
        assert!(native.artifact_parent.is_some());
        assert_eq!(native.pins, 148);
        assert!(cleanup.verified());

        {
            let mut entries = native.entries();
            for (index, row) in rows.iter().enumerate() {
                let relative = row["path"].as_str().unwrap();
                let expected = fs::read(shipped.join(relative)).unwrap();
                let document: Value = serde_json::from_slice(&expected).unwrap();
                use CatalogDocumentKind::{Base, ExecutableCatalog, Pack, Profile, Shell};
                let (kind, id) = match row["kind"].as_str().unwrap() {
                    "pack" => (Pack, &document["pack"]["id"]),
                    "base" => (Base, &document["pack_id"]),
                    "shell" => (Shell, &document["provider_id"]),
                    "profile" => (Profile, &document["profile_id"]),
                    "executable_catalog" => (ExecutableCatalog, &document["pack_id"]),
                    _ => panic!("unexpected lock kind: {relative}"),
                };
                let (actual_kind, actual_id, bytes) = entries
                    .next()
                    .unwrap_or_else(|| panic!("missing native member {index}: {relative}"));
                assert_eq!(
                    (actual_kind, actual_id),
                    (kind, id.as_str().unwrap()),
                    "{index}: {relative}"
                );
                assert!(
                    bytes == expected.as_slice(),
                    "native member bytes differ at {index}: {relative}"
                );
                assert_eq!(
                    Digest::hash(bytes).as_str(),
                    row["digest"].as_str().unwrap(),
                    "{relative}"
                );
            }
            assert!(entries.next().is_none());
        }
        let retained = invocation.capacity_snapshot().unwrap();
        assert!(retained.charged_bytes > before.charged_bytes);
        drop(native);
        let after = invocation.capacity_snapshot().unwrap();
        assert_eq!(
            (after.charged_bytes, after.reservations),
            (before.charged_bytes, before.reservations)
        );
        assert!(cleanup.verified());
        assert!(Instant::now() < invocation.deadline().unwrap());
    });

    with_source(false, |source, invocation, _, io, cleanup| {
        let bundle = source.root.join("ecosystem/defaultspack/v4");
        let base = fs::read(shipped.join("defaults-basepack.base.v1.json")).unwrap();
        fs::create_dir_all(&bundle).unwrap();
        fs::write(bundle.join("first.json"), &base).unwrap();
        fs::write(bundle.join("duplicate.json"), &base).unwrap();
        let digest = Digest::hash(&base);
        let lock = json!({
            "schema": "io.tobkiri.defaultspack-bundle-lock.v1",
            "entries": [
                {"path": "first.json", "kind": "base", "digest": digest.as_str()},
                {"path": "duplicate.json", "kind": "base", "digest": digest.as_str()},
            ],
        });
        fs::write(
            bundle.join("bundle.lock.json"),
            serde_json::to_vec(&lock).unwrap(),
        )
        .unwrap();
        io.select_packaged(&bundle).unwrap();
        assert!(matches!(
            io.read_owned_bundle(source, invocation),
            Err(NativeBundleError::Integrity(message)) if
                message == "duplicate or missing base identity: 'defaults-basepack'"
        ));

        let lock_path = bundle.join("bundle.lock.json");
        fs::remove_file(&lock_path).unwrap();
        let Err(error @ NativeBundleError::Lock { .. }) = io.read_owned_bundle(source, invocation)
        else {
            panic!("missing lock was not represented with its original cause");
        };
        let display = error.to_string();
        assert!(display.starts_with("cannot read bundle lock: [Errno 2]"));
        assert!(display.ends_with("/bundle.lock.json'"));

        let mut lock = json!({
            "schema": "io.tobkiri.defaultspack-bundle-lock.v1",
            "entries": [{"path": "x".repeat(9000), "kind": [], "digest": null}],
        });
        fs::write(&lock_path, serde_json::to_vec(&lock).unwrap()).unwrap();
        assert!(matches!(
            io.read_owned_bundle(source, invocation),
            Err(NativeBundleError::TypeError("unhashable type: 'list'"))
        ));
        lock["entries"][0]["kind"] = json!("base");
        fs::write(&lock_path, serde_json::to_vec(&lock).unwrap()).unwrap();
        let Err(error @ NativeBundleError::InvalidLockEntry { .. }) =
            io.read_owned_bundle(source, invocation)
        else {
            panic!("digest validation must precede physical path admission");
        };
        assert!(error.to_string().ends_with(" has an invalid digest"));

        let raw = fs::read(shipped.join("packs/defaultspack.executables.v4.json")).unwrap();
        fs::write(bundle.join("catalog.json"), &raw).unwrap();
        lock["entries"][0] = json!({
            "path": "catalog.json", "kind": "executable_catalog", "digest": Digest::hash(&raw).as_str()
        });
        fs::write(&lock_path, serde_json::to_vec(&lock).unwrap()).unwrap();
        assert!(matches!(
            io.read_owned_bundle(source, invocation),
            Err(NativeBundleError::Integrity(message)) if
                message == "executable catalog has no bundled Pack manifest: defaultspack"
        ));
        assert!(cleanup.verified());
    });
}

#[tokio::test(flavor = "current_thread")]
async fn owned_catalog_delivery_adopts_only_a_consumed_snapshot() {
    let shipped = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tobkiri_runtime/ecosystem/defaultspack/v4");
    let lock_bytes = fs::read(shipped.join("bundle.lock.json")).unwrap();
    let lock: Value = serde_json::from_slice(&lock_bytes).unwrap();
    let rows = lock["entries"].as_array().unwrap();
    with_source(false, |source, invocation, budget, io, cleanup| {
        let bundle = source.root.join("ecosystem/defaultspack/v4");
        let artifacts = bundle.parent().unwrap().join("platform-artifacts");
        copy_bundle(&shipped, &bundle, &lock_bytes, rows);
        fs::create_dir(&artifacts).unwrap();
        io.select_packaged(&bundle).unwrap();
        io.enable_owned_catalog();
        io.retain_review_pack_dependencies();
        io.retain_publication_inputs();
        io.bind_loader(CoreValue::string("resolved_module_fixture", budget).unwrap())
            .unwrap();

        let request = |operation, args| packet(invocation, budget, operation, json!([]), args);
        let inspect = |io: &mut CatalogIo, operation, args| {
            io.inspect(source, invocation, &request(operation, args), budget)
        };
        assert!(inspect(io, "snapshot-open", json!([])).is_err());
        let root = call(
            io,
            source,
            invocation,
            budget,
            "root",
            json!([["parent", ""], ["parent", ""], ["join", "v4"]]),
            json!([]),
        );
        assert_eq!(root["kind"], "snapshot-root");
        assert!(inspect(io, "is-symlink", json!([])).is_err());
        let opened = call(
            io,
            source,
            invocation,
            budget,
            "snapshot-open",
            json!([]),
            json!([]),
        );
        assert_eq!(
            opened,
            json!({
                "kind": "snapshot-open",
                "root": fs::canonicalize(&bundle).unwrap().to_str().unwrap(),
                "artifact_root": fs::canonicalize(&artifacts).unwrap().to_str().unwrap(),
                "count": rows.len(),
            })
        );
        assert!(!io.completed());
        assert!(inspect(io, "snapshot-open", json!([])).is_err());
        assert!(inspect(io, "snapshot-rows", json!([1])).is_err());
        assert!(inspect(io, "snapshot-close", json!([rows.len()])).is_err());

        // Distribution and adoption must use the completed physical snapshot.
        fs::remove_file(bundle.join(rows[0]["path"].as_str().unwrap())).unwrap();
        let mut offset = 0;
        let mut pages = 0;
        while offset < rows.len() {
            let page = call(
                io,
                source,
                invocation,
                budget,
                "snapshot-rows",
                json!([]),
                json!([offset]),
            );
            assert_eq!(page["kind"], "snapshot-rows");
            let page = page["rows"].as_array().unwrap();
            assert!(!page.is_empty());
            for record in page {
                let row = &rows[offset];
                let expected = fs::read(shipped.join(row["path"].as_str().unwrap())).unwrap();
                let document: Value = serde_json::from_slice(&expected).unwrap();
                let kind = row["kind"].as_str().unwrap();
                let id = match kind {
                    "pack" => &document["pack"]["id"],
                    "base" | "executable_catalog" => &document["pack_id"],
                    "shell" => &document["provider_id"],
                    "profile" => &document["profile_id"],
                    _ => panic!("unexpected kind: {kind}"),
                };
                assert_eq!(record[0], kind);
                assert_eq!(&record[1], id);
                assert_eq!(record[2].as_str().unwrap().as_bytes(), expected);
                offset += 1;
            }
            pages += 1;
        }
        assert!(pages > 1);
        assert_eq!(offset, rows.len());
        assert!(!io.completed());
        assert!(inspect(io, "snapshot-rows", json!([offset])).is_err());
        assert_eq!(
            call(
                io,
                source,
                invocation,
                budget,
                "snapshot-close",
                json!([]),
                json!([offset])
            ),
            json!({"kind":"unit"})
        );
        assert!(io.completed());
        assert_eq!(io.pins, 148);
        assert!(io.recovery_pack_pins.is_some());
        assert!(io.shell_inputs.is_some());
        assert!(io.artifact_parent_identity.is_some());
        assert_eq!(
            io.publication_lock_digest(source, invocation).unwrap(),
            Digest::hash(&lock_bytes)
        );
        assert!(inspect(io, "snapshot-close", json!([offset])).is_err());
        assert!(cleanup.verified());
    });
}

#[tokio::test(flavor = "current_thread")]
async fn owned_catalog_delivery_preserves_a_row_that_overflows_when_escaped() {
    let shipped = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tobkiri_runtime/ecosystem/defaultspack/v4");
    let pack = fs::read(shipped.join("packs/defaults-basepack.pack.v4.json")).unwrap();
    let mut pack: Value = serde_json::from_slice(&pack).unwrap();
    pack["requirements"]["network"] =
        json!({"matrix.bindings": [{"caller_function_id": {"part": ["caller", false, 0]}}]});
    let pack = serde_json::to_vec(&pack).unwrap();
    let mut base = fs::read(shipped.join("defaults-basepack.base.v1.json")).unwrap();
    // Whitespace changes the lock digest and preserves the Base revision.
    // The outer string escapes each LF as six bytes.
    base.resize(base.len() + 700_000, b'\n');

    with_source(false, |source, invocation, budget, io, _| {
        let bundle = source.root.join("ecosystem/defaultspack/v4");
        fs::create_dir_all(&bundle).unwrap();
        fs::write(bundle.join("pack.json"), &pack).unwrap();
        fs::write(bundle.join("base.json"), &base).unwrap();
        let lock = json!({
            "schema": "io.tobkiri.defaultspack-bundle-lock.v1",
            "entries": [
                {"path": "pack.json", "kind": "pack", "digest": Digest::hash(&pack).as_str()},
                {"path": "base.json", "kind": "base", "digest": Digest::hash(&base).as_str()},
            ],
        });
        fs::write(
            bundle.join("bundle.lock.json"),
            serde_json::to_vec(&lock).unwrap(),
        )
        .unwrap();
        io.select_packaged(&bundle).unwrap();
        io.enable_owned_catalog();
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
        let inspect = |io: &mut CatalogIo, operation, args| {
            let request = packet(invocation, budget, operation, json!([]), args);
            io.inspect(source, invocation, &request, budget)
        };
        let result = |io: &mut CatalogIo, operation, args| {
            let output = inspect(io, operation, args).unwrap();
            assert!(output.len() <= MAX_BYTES);
            let mut frame: Value = serde_json::from_slice(output.as_bytes()).unwrap();
            frame["result"].take()
        };
        assert_eq!(result(io, "snapshot-open", json!([]))["count"], 2);
        let ordinary = result(io, "snapshot-rows", json!([0]));
        assert_eq!(ordinary["kind"], "snapshot-rows");
        assert_eq!(ordinary["rows"].as_array().unwrap().len(), 1);
        assert_eq!(ordinary["rows"][0][2].as_str().unwrap().as_bytes(), pack);
        assert!(inspect(io, "snapshot-close", json!([2])).is_err());

        let oversized = result(io, "snapshot-rows", json!([1]));
        assert_eq!(oversized["kind"], "bytes");
        assert_eq!(
            base64::Engine::decode(
                &base64::engine::general_purpose::STANDARD,
                oversized["value"].as_str().unwrap(),
            )
            .unwrap(),
            base
        );
        assert!(inspect(io, "snapshot-rows", json!([1])).is_err());
        assert_eq!(
            result(io, "snapshot-close", json!([2])),
            json!({"kind": "unit"})
        );
        assert!(io.completed());
    });
}

#[tokio::test(flavor = "current_thread")]
async fn owned_catalog_lock_sends_exact_invalid_source_once() {
    for raw in [b"{".as_slice(), &[0xff]] {
        with_source(false, |source, invocation, budget, io, cleanup| {
            let bundle = source.root.join("ecosystem/defaultspack/v4");
            fs::create_dir_all(&bundle).unwrap();
            let lock = bundle.join("bundle.lock.json");
            fs::write(&lock, raw).unwrap();
            io.select_packaged(&bundle).unwrap();
            io.enable_owned_catalog();
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

            let before = invocation.capacity_snapshot().unwrap();
            let opened = call(
                io,
                source,
                invocation,
                budget,
                "snapshot-open",
                json!([]),
                json!([]),
            );
            assert_eq!(opened["kind"], "snapshot-error");
            assert_eq!(opened["error"], "lock-json-source");
            assert_eq!(opened["cause"]["kind"], "bytes");
            assert_eq!(
                base64::Engine::decode(
                    &base64::engine::general_purpose::STANDARD,
                    opened["cause"]["value"].as_str().unwrap(),
                )
                .unwrap(),
                raw
            );
            let after = invocation.capacity_snapshot().unwrap();
            assert_eq!(
                (after.charged_bytes, after.reservations),
                (before.charged_bytes, before.reservations)
            );
            fs::write(&lock, b"{}").unwrap();
            let reopen = packet(invocation, budget, "snapshot-open", json!([]), json!([]));
            assert!(io.inspect(source, invocation, &reopen, budget).is_err());
            assert!(!io.completed());
            assert!(cleanup.verified());
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn owned_catalog_sends_only_first_unverified_document_without_reopening() {
    let pack = include_bytes!(
        "../../../tobkiri_runtime/ecosystem/defaultspack/v4/packs/defaults-basepack.pack.v4.json"
    );
    with_source(false, |source, invocation, budget, io, cleanup| {
        let mut invalid = b"{}".to_vec();
        // The diagnostic header and old maximum-size byte reply are separate.
        invalid.resize(legacy_byte_limit(invocation, budget), b'\n');
        let bundle = source.root.join("ecosystem/defaultspack/v4");
        fs::create_dir_all(&bundle).unwrap();
        fs::write(bundle.join("pack.json"), pack).unwrap();
        let rejected_path = bundle.join("invalid.json");
        fs::write(&rejected_path, &invalid).unwrap();
        let lock = json!({
            "schema": "io.tobkiri.defaultspack-bundle-lock.v1",
            "entries": [
                {"path": "pack.json", "kind": "pack", "digest": Digest::hash(pack).as_str()},
                {"path": "./invalid.json", "kind": "profile", "digest": Digest::hash(&invalid).as_str()},
                {"path": "missing.json", "kind": "base", "digest": Digest::hash(b"{}").as_str()},
            ],
        });
        fs::write(
            bundle.join("bundle.lock.json"),
            serde_json::to_vec(&lock).unwrap(),
        )
        .unwrap();
        io.select_packaged(&bundle).unwrap();
        io.enable_owned_catalog();
        io.retain_review_pack_dependencies();
        io.bind_loader(CoreValue::string("resolved_module_fixture", budget).unwrap())
            .unwrap();
        let inspect = |io: &mut CatalogIo, operation, steps, args| {
            let request = packet(invocation, budget, operation, steps, args);
            io.inspect(source, invocation, &request, budget)
        };
        let result = |io: &mut CatalogIo, operation| {
            call(
                io,
                source,
                invocation,
                budget,
                operation,
                json!([]),
                json!([]),
            )
        };
        let diagnostic = "snapshot-invalid-document-bytes";
        assert!(inspect(io, diagnostic, json!([]), json!([])).is_err());
        call(
            io,
            source,
            invocation,
            budget,
            "root",
            json!([["parent", ""], ["parent", ""], ["join", "v4"]]),
            json!([]),
        );
        assert!(inspect(io, diagnostic, json!([]), json!([])).is_err());
        let baseline = invocation.capacity_snapshot().unwrap();
        assert_eq!(
            result(io, "snapshot-open"),
            json!({
                "kind": "snapshot-invalid-document",
                "document_kind": "profile",
                "path": "./invalid.json",
            })
        );
        assert!(!io.completed());
        assert!(inspect(io, "snapshot-open", json!([]), json!([])).is_err());
        assert!(inspect(io, "snapshot-rows", json!([]), json!([0])).is_err());
        assert!(inspect(io, "snapshot-close", json!([]), json!([1])).is_err());
        assert!(inspect(io, diagnostic, json!([["join", "invalid.json"]]), json!([])).is_err());
        assert!(inspect(io, diagnostic, json!([]), json!([0])).is_err());
        fs::remove_file(rejected_path).unwrap();
        let closed = result(io, diagnostic);
        assert_eq!(closed["kind"], "bytes");
        assert_eq!(
            base64::Engine::decode(
                &base64::engine::general_purpose::STANDARD,
                closed["value"].as_str().unwrap(),
            )
            .unwrap(),
            invalid
        );
        assert!(inspect(io, diagnostic, json!([]), json!([])).is_err());
        assert!(inspect(io, "snapshot-close", json!([]), json!([1])).is_err());
        assert!(!io.completed());
        assert!(io.lock.is_none());
        assert!(io.shell_inputs.is_none());
        let released = invocation.capacity_snapshot().unwrap();
        assert_eq!(
            (released.charged_bytes, released.reservations),
            (baseline.charged_bytes, baseline.reservations),
        );
        assert!(cleanup.verified());
    });
}

#[tokio::test(flavor = "current_thread")]
async fn owned_catalog_checks_byte_limit_before_document_digest() {
    with_source(false, |source, invocation, budget, io, cleanup| {
        let bundle = source.root.join("ecosystem/defaultspack/v4");
        fs::create_dir_all(&bundle).unwrap();
        let mut pack = include_bytes!(
            "../../../tobkiri_runtime/ecosystem/defaultspack/v4/packs/defaults-basepack.pack.v4.json"
        )
        .to_vec();
        pack.resize(legacy_byte_limit(invocation, budget) + 1, b'\n');
        fs::write(bundle.join("pack.json"), &pack).unwrap();
        let lock = json!({
            "schema": "io.tobkiri.defaultspack-bundle-lock.v1",
            "entries": [
                {"path": "pack.json", "kind": "pack", "digest": Digest::hash(b"wrong").as_str()},
                {"path": "missing.json", "kind": "base", "digest": Digest::hash(b"{}").as_str()},
            ],
        });
        fs::write(
            bundle.join("bundle.lock.json"),
            serde_json::to_vec(&lock).unwrap(),
        )
        .unwrap();
        io.select_packaged(&bundle).unwrap();
        io.enable_owned_catalog();
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
        let open = packet(invocation, budget, "snapshot-open", json!([]), json!([]));
        let error = io.inspect(source, invocation, &open, budget).unwrap_err();
        assert!(matches!(
            error.downcast_ref::<tobkiri_canon::AllocationError>(),
            Some(tobkiri_canon::AllocationError::Limit),
        ));
        assert!(io.inspect(source, invocation, &open, budget).is_err());
        assert!(!io.completed());
        assert!(cleanup.verified());
    });
}

fn legacy_byte_limit(
    invocation: &NativeSetupInvocation,
    budget: &Arc<dyn AllocationBudget>,
) -> usize {
    let request = packet(invocation, budget, "snapshot-open", json!([]), json!([]));
    let scope = request.get_key("scope").unwrap();
    let output = inspection_result(&scope, budget).unwrap();
    document_shapes::byte_frame_limit(&output).unwrap()
}
