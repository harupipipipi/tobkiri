//! Compare the bounded document evaluator against the shipped bundle inventory.
use super::*;
use crate::original_authority::bundled_schema::{CatalogDocumentKind, CatalogSchemas};
use crate::original_authority::definitions::tests::{operation, with_source};
use serde_json::{Value, json};
use std::{fs, path::PathBuf};

#[tokio::test(flavor = "current_thread")]
async fn applies_original_validation_to_every_bundled_document() {
    with_source(|source, read, cancellation, budget| {
        let op = operation(read, source, cancellation);
        let runtime = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tobkiri_runtime");
        let bundle = runtime.join("ecosystem/defaultspack/v4");
        let mut schemas = CatalogSchemas::default();
        let lock: Value =
            serde_json::from_slice(&fs::read(bundle.join("bundle.lock.json")).unwrap()).unwrap();
        let entries = lock["entries"].as_array().unwrap();
        assert_eq!(entries.len(), 305);
        for entry in entries {
            let path = entry["path"].as_str().unwrap();
            let bytes = fs::read(bundle.join(path)).unwrap();
            assert_eq!(
                Digest::hash(&bytes).as_str(),
                entry["digest"].as_str().unwrap()
            );
            let kind = match entry["kind"].as_str().unwrap() {
                "pack" => CatalogDocumentKind::Pack,
                "executable_catalog" => CatalogDocumentKind::ExecutableCatalog,
                "base" => CatalogDocumentKind::Base,
                "shell" => CatalogDocumentKind::Shell,
                "profile" => CatalogDocumentKind::Profile,
                _ => panic!("unexpected original bundle kind: {path}"),
            };
            assert!(
                schemas
                    .validate_document(&op, budget, kind, &bytes)
                    .unwrap_or_else(|error| panic!("{path}: {error}")),
                "{path}"
            );
            if let Some(invalid) = invalid_structure(path, &bytes) {
                let bytes = serde_json::to_vec(&invalid).unwrap();
                assert!(
                    !schemas
                        .validate_document(&op, budget, kind, &bytes)
                        .unwrap(),
                    "{path}"
                );
            }
        }
    });
}

#[tokio::test(flavor = "current_thread")]
async fn structural_eligibility_declines_wrong_kind_version_and_strict_json_errors() {
    with_source(|source, read, cancellation, budget| {
        let op = operation(read, source, cancellation);
        let mut schemas = CatalogSchemas::default();
        let original = include_bytes!(
            "../../../tobkiri_runtime/tests/fixtures/profile_v4/pre-e853-activation.json"
        );
        let original: Value = serde_json::from_slice(original).unwrap();
        let mut profile = original["profile"].clone();
        let bytes = serde_json::to_vec(&profile).unwrap();
        assert!(
            schemas
                .validate_document(&op, budget, CatalogDocumentKind::Profile, &bytes)
                .unwrap()
        );
        for kind in [
            CatalogDocumentKind::Pack,
            CatalogDocumentKind::ExecutableCatalog,
            CatalogDocumentKind::Base,
            CatalogDocumentKind::Shell,
        ] {
            assert!(
                !schemas
                    .validate_document(&op, budget, kind, &bytes)
                    .unwrap()
            );
        }
        profile["profile_api_version"] = json!("io.tobkiri.profile.v6");
        let wrong_version = serde_json::to_vec(&profile).unwrap();
        for invalid in [
            wrong_version.as_slice(),
            b"[",
            b"\xff",
            br#"{"profile_api_version":"io.tobkiri.profile.v4","x":1,"x":2}"#,
            br#"{"profile_api_version":"io.tobkiri.profile.v4","x":1.0}"#,
        ] {
            assert!(
                !schemas
                    .validate_document(&op, budget, CatalogDocumentKind::Profile, invalid)
                    .unwrap()
            );
        }
    });
}

#[tokio::test(flavor = "current_thread")]
async fn semantic_proof_declines_invalid_and_unrepresented_pack_data() {
    with_source(|source, read, cancellation, budget| {
        let op = operation(read, source, cancellation);
        let mut schemas = CatalogSchemas::default();
        let kind = CatalogDocumentKind::Pack;
        let raw = include_bytes!(
            "../../../tobkiri_runtime/ecosystem/defaultspack/v4/packs/defaults-basepack.pack.v4.json"
        );
        let mut pack: Value = serde_json::from_slice(raw).unwrap();
        for network in [
            json!({"id": "legacy.provider"}),
            json!({"grant": null}),
            json!({"matrix.operations": [{"operation_id": "read"}, {"operation_id": "read"}]}),
            // Python accepts these single bindings through its string coercion.
            json!({"matrix.bindings": [{"caller_function_id": 7}]}),
            json!({"matrix.bindings": [{"caller_function_id": {"part": "caller"}}]}),
        ] {
            pack["requirements"]["network"] = network;
            let bytes = serde_json::to_vec(&pack).unwrap();
            assert!(
                !schemas
                    .validate_document(&op, budget, kind, &bytes)
                    .unwrap(),
                "{pack}"
            );
        }
        pack["requirements"]["network"] =
            json!({"matrix.bindings": [{"caller_function_id": "caller"}]});
        let bytes = serde_json::to_vec(&pack).unwrap();
        assert!(
            schemas
                .validate_document(&op, budget, kind, &bytes)
                .unwrap()
        );
    });
}

fn invalid_structure(path: &str, bytes: &[u8]) -> Option<Value> {
    const DIGEST: &str = "sha256:0000000000000000000000000000000000000000000000000000000000000000";
    let (field, value) = match path {
        "packs/defaults-basepack.pack.v4.json" => ("/migration/sunset_at", json!("2025-02-29")),
        "packs/defaultspack.executables.v4.json" => (
            "/variants/0/implementation_path",
            json!("resource/../entry"),
        ),
        "shell.tauri.default.shell.v1.json" => (
            "/launch/variants",
            json!([{
                "platform": "darwin", "architecture": "aarch64", "artifact_digest": DIGEST,
                "entrypoint_digest": DIGEST, "relative_path": "bin/shell", "entrypoint": "shell",
                "bundle_identity": "dev.tobkiri.shell"
            }]),
        ),
        _ => return None,
    };
    let mut doc: Value = serde_json::from_slice(bytes).unwrap();
    *doc.pointer_mut(field).unwrap() = value;
    Some(doc)
}

#[tokio::test(flavor = "current_thread")]
async fn profile_v4_checks_nested_requested_edge_references_against_its_list() {
    with_source(|source, read, cancellation, budget| {
        let op = operation(read, source, cancellation);
        let original = include_bytes!(
            "../../../tobkiri_runtime/tests/fixtures/profile_v4/pre-e853-activation.json"
        );
        let mut input: Value = serde_json::from_slice(original).unwrap();
        let profile = &mut input["profile"];
        profile["requested_edges"]
            .as_array_mut()
            .unwrap()
            .truncate(1);
        let listed = profile["authority_references"][0].clone();
        for (value, accepted) in [
            (listed, true),
            (Value::Null, true),
            (json!("authority-ref:unlisted"), false),
        ] {
            profile["requested_edges"][0]["requested_scope_template"] = json!({
                "nested": [{"authority_reference": value}]
            });
            let bytes = serde_json::to_vec(&profile).unwrap();
            let doc = json::decode_read_only_strict(&bytes, &op, budget).unwrap();
            assert_eq!(
                external_catalog::validate_profile_semantics(&doc, doc.root, &op, budget).unwrap(),
                accepted,
            );
        }
    });
}
