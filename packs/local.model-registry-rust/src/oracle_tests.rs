//! Consume fixed observations from the original Python model owner.

use crate::{presentation::project_list, registry::Registry};
use ordered_python_compat::{PyValue, dumps, loads};
use sha2::{Digest as _, Sha256};
use std::{path::Path, process::Command};

const FIXTURE: &str = include_str!("../evidence/original-python-read.json");
const SOURCE_COMMIT: &str = "6818f8ef7fae92685794acceccf71924d6eafd6b";
const FIXTURE_SHA256: &str = "f1dffc0f877d15d24692b565b998aa6a292ec1666c37a27846a6012259234129";

fn field<'a>(object: &'a PyValue, key: &str) -> &'a PyValue {
    object.get(key).unwrap_or_else(|| panic!("missing {key}"))
}

fn string<'a>(object: &'a PyValue, key: &str) -> &'a str {
    field(object, key).as_str().unwrap()
}

#[test]
fn matches_pinned_python_model_registry_reads() {
    assert_eq!(format!("{:x}", Sha256::digest(FIXTURE)), FIXTURE_SHA256);
    let fixture = loads(FIXTURE).unwrap();
    assert_eq!(
        string(&fixture, "schema"),
        "io.tobkiri.original-python-model-registry-read.v1"
    );
    assert_eq!(string(&fixture, "source_commit"), SOURCE_COMMIT);
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for source in field(&fixture, "sources").as_array().unwrap() {
        let path = string(source, "path");
        let object = format!("{SOURCE_COMMIT}:{path}");
        let captured = Command::new("git")
            .current_dir(&root)
            .args(["--no-replace-objects", "cat-file", "blob", &object])
            .output()
            .unwrap();
        assert!(
            captured.status.success(),
            "missing captured source {object}"
        );
        assert_eq!(
            format!("{:x}", Sha256::digest(&captured.stdout)),
            string(source, "sha256"),
            "{path}"
        );
    }
    let cases = field(&fixture, "cases").as_array().unwrap();
    assert_eq!(cases.len(), 25);
    for case in cases {
        let id = string(case, "id");
        let document = field(case, "document");
        let document = (!matches!(document, PyValue::Null)).then(|| document.clone());
        let actual = Registry::new(string(case, "profile_id"), document).and_then(|registry| {
            match string(case, "action") {
                "snapshot" => registry.snapshot(),
                "settings" => registry.snapshot().and_then(|value| project_list(&value)),
                "get" => registry.get(field(case, "identifier")),
                "resolve" => registry.resolve(field(case, "identifier")),
                action => panic!("unknown case action {action}"),
            }
        });
        if let Some(expected) = case.get("result") {
            let value = actual.unwrap_or_else(|error| panic!("{id}: {error:?}"));
            // Encoded equality includes original field order and floating-point
            // representations that numeric equality alone would miss.
            assert_eq!(dumps(&value).unwrap(), dumps(expected).unwrap(), "{id}");
        } else {
            let expected = field(case, "error");
            let error = actual.expect_err(id);
            assert_eq!(error.exception_class, string(expected, "class"), "{id}");
            assert_eq!(error.message, string(expected, "message"), "{id}");
        }
    }
}
