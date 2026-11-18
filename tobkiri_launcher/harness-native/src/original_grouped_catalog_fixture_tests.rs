//! Exercise the grouped native transport against every physical shipped member.
use super::*;

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

        io.select_packaged(&bundle).unwrap();
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
        assert_eq!(
            base64::engine::general_purpose::STANDARD
                .decode(lock_reply["value"].as_str().unwrap())
                .unwrap(),
            lock_bytes
        );

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
        }

        let mut successful_pins = 0;
        for (index, entry) in entries.iter().enumerate() {
            if entry["kind"] != "executable_catalog" {
                continue;
            }
            let relative = entry["path"].as_str().unwrap();
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
