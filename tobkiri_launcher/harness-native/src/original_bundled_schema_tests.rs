//! Compare the bounded structural evaluator against the shipped bundle inventory.
use super::*;
use crate::original_authority::definitions::tests::{operation, with_source};
use serde_json::{Value, json};
use std::{fs, path::PathBuf};

const PACK: &str = "https://schemas.tobkiri.local/io.tobkiri/pack-manifest.v4.schema.json";
const EXECUTABLE: &str =
    "https://schemas.tobkiri.local/io.tobkiri/executable-catalog.v4.schema.json";
const BASE: &str = "https://schemas.tobkiri.local/io.tobkiri/base-definition.v4.schema.json";
const SHELL_V4: &str = "https://schemas.tobkiri.local/io.tobkiri/shell-definition.v4.schema.json";
const SHELL_V5: &str = "https://schemas.tobkiri.local/io.tobkiri/shell-definition.v5.schema.json";

#[tokio::test(flavor = "current_thread")]
async fn applies_original_structural_schemas_to_every_bundled_document() {
    with_source(|source, read, cancellation, budget| {
        let op = operation(read, source, cancellation);
        let runtime = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tobkiri_runtime");
        let bundle = runtime.join("ecosystem/defaultspack/v4");
        let schemas = runtime.join("tobkiri_protocol/schemas");
        let schema = |name| {
            let bytes = fs::read(schemas.join(name)).unwrap();
            json::decode_read_only_strict(&bytes, &op, budget).unwrap()
        };
        let pack = schema("pack_manifest_v4.schema.json");
        let executable = schema("executable_catalog_v4.schema.json");
        let base = schema("base_definition_v4.schema.json");
        let shell4 = schema("shell_definition_v4.schema.json");
        let shell5 = schema("shell_definition_v5.schema.json");
        let profile4 = schema("profile_v4.schema.json");
        let profile5 = schema("profile_v5.schema.json");
        let common = schema("common_v1.schema.json");
        let projection = schema("profile_content_projection_v1.schema.json");
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
            let doc = json::decode_read_only_strict(&bytes, &op, budget).unwrap();
            let (id, schema, extra): (_, _, Option<&Document>) =
                match entry["kind"].as_str().unwrap() {
                    "pack" => (PACK, &*pack, None),
                    "executable_catalog" => (EXECUTABLE, &*executable, None),
                    "base" => (BASE, &*base, None),
                    "shell" => {
                        let version = doc.get(doc.root, "shell_api_version").unwrap();
                        if doc.string_is(version, "io.tobkiri.shell.v4") {
                            (SHELL_V4, &*shell4, None)
                        } else {
                            assert!(doc.string_is(version, "io.tobkiri.shell.v5"));
                            (SHELL_V5, &*shell5, None)
                        }
                    }
                    "profile" => {
                        let version = doc.get(doc.root, "profile_api_version").unwrap();
                        if doc.string_is(version, "io.tobkiri.profile.v4") {
                            (PROFILE_V4, &*profile4, Some(&*projection))
                        } else {
                            assert!(doc.string_is(version, "io.tobkiri.profile.v5"));
                            (PROFILE_V5, &*profile5, Some(&*projection))
                        }
                    }
                    _ => panic!("unexpected original bundle kind: {path}"),
                };
            let closure = [schema, &*common, extra.unwrap_or(&common)];
            let closure = &closure[..2 + usize::from(extra.is_some())];
            assert!(
                matches_shape(&op, budget, closure, &[id], &doc, doc.root)
                    .unwrap_or_else(|error| panic!("{path}: {error}")),
                "{path}"
            );
            drop(doc);
            if let Some(invalid) = invalid_structure(path, &bytes) {
                let bytes = serde_json::to_vec(&invalid).unwrap();
                let doc = json::decode_read_only_strict(&bytes, &op, budget).unwrap();
                assert!(
                    !matches_shape(&op, budget, closure, &[id], &doc, doc.root).unwrap(),
                    "{path}"
                );
            }
        }
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
