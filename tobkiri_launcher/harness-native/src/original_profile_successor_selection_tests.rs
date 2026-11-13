//! Selector tests on a real portable allocation owner and strictly loaded registry.
use super::*;
use crate::original_authority::definitions::tests::{
    Source, digest, entry, sealed, state, with_source,
};
use serde_json::{Value, json};

fn with_operation(run: impl FnOnce(&Operation<'_, Source>, &Arc<dyn AllocationBudget>)) {
    with_source(|source, read, cancellation, budget| {
        let op = Operation {
            owner: source,
            invocation: &read.invocation,
            cancellation,
            deadline: read.invocation.deadline().unwrap(),
            pid: std::process::id(),
            cleanup: read.cleanup.clone(),
            fixture: None,
        };
        run(&op, budget);
    });
}

fn legacy_entry(id: &str, order: i64, legacy_ids: &[&str], version: Value) -> Value {
    let mut row = entry(id, order);
    let profile = &mut row["revisions"][0]["profile"];
    profile["profile_api_version"] = version;
    let revision = json!(digest(profile));
    row["revisions"][0]["profile_revision"] = revision.clone();
    row["current_revision"] = revision;
    row["legacy_ids"] = json!(legacy_ids);
    row
}

fn append_revision(row: &mut Value, profile: Value) {
    let parent = row["current_revision"].clone();
    let tombstone = profile.is_null();
    let revision = if tombstone {
        digest(&json!({"schema":"io.tobkiri.profile-tombstone.v1",
            "profile_id":row["profile_id"],"parent_revision":parent}))
    } else {
        digest(&profile)
    };
    row["revisions"].as_array_mut().unwrap().push(json!({
        "profile_revision":revision,"parent_revision":parent,"profile":profile,
        "created_at":10,"updated_at":10,"tombstone":tombstone
    }));
    row["current_revision"] = json!(revision);
    row["tombstone"] = json!(tombstone);
}

fn current(doc: &Document, physical_index: usize) -> usize {
    let profiles = array(doc, doc.get(doc.root, "profiles").unwrap()).unwrap();
    let revisions = array(doc, doc.get(profiles[physical_index], "revisions").unwrap()).unwrap();
    doc.get(*revisions.last().unwrap(), "profile").unwrap()
}

fn expect_entry(
    doc: &Document,
    selected: SuccessorSelection,
    expected_id: &str,
    expected_origin: SuccessorItemOrigin,
    expected_path: &str,
) -> usize {
    let SuccessorSelection::BuilderEntry {
        item,
        profile_id,
        item_origin,
        source_path,
    } = selected
    else {
        panic!("expected first successor builder arguments");
    };
    assert!(doc.string_is(profile_id, expected_id));
    assert_eq!(item_origin, expected_origin);
    assert_eq!(source_path.as_str(), expected_path);
    item
}

#[tokio::test(flavor = "current_thread")]
async fn physical_order_skips_tombstones_idless_and_v4_v5_before_exact_first_legacy_key() {
    with_operation(|op, budget| {
        let mut tombstone = legacy_entry("deleted", 1, &["old"], json!([]));
        append_revision(&mut tombstone, Value::Null);
        let mut registry = state(vec![
            tombstone,
            legacy_entry("idless", 1, &[], json!({})),
            legacy_entry("already-v4", 2, &["old"], json!("io.tobkiri.profile.v4")),
            legacy_entry("already-v5", 3, &["old"], json!("io.tobkiri.profile.v5")),
            legacy_entry("physical-first", 99, &["旧🦀", "backup"], json!(false)),
            legacy_entry("sorts-first", 0, &["backup"], json!({})),
        ]);
        registry["legacy"] = json!({"source_document":{"profiles":{
            "旧🦀 ":{"wrong":true},"旧🦀":{"exact":true},"backup":{"wrong":true}
        }},"source_path":" raw\n旧🦀 "});
        let loaded = load(op, Some(&sealed(registry)), budget).unwrap();
        let doc = &loaded.doc;
        let before = op.invocation.capacity_snapshot().unwrap().charged_bytes;
        let selected = select_successor(op, &loaded, budget).unwrap();
        assert!(op.invocation.capacity_snapshot().unwrap().charged_bytes > before);
        let item = expect_entry(
            doc,
            selected,
            "physical-first",
            SuccessorItemOrigin::LegacySource,
            " raw\n旧🦀 ",
        );
        let legacy = doc.get(doc.root, "legacy").unwrap();
        let source = doc.get(legacy, "source_document").unwrap();
        let profiles = doc.get(source, "profiles").unwrap();
        assert_eq!(Some(item), doc.get(profiles, "旧🦀"));
        assert_ne!(item, current(doc, 4));
        assert_eq!(
            op.invocation.capacity_snapshot().unwrap().charged_bytes,
            before
        );
    });
}

#[tokio::test(flavor = "current_thread")]
async fn list_lookup_uses_first_truthy_id_and_python_repr_in_input_order() {
    with_operation(|op, budget| {
        const ORDERED: &str = r#"{"z":["O'Reilly","\u00a0","😀"],"a":[false,null,-12,"\n"]}"#;
        const PYTHON: &str = r#"{'z': ["O'Reilly", '\xa0', '😀'], 'a': [False, None, -12, '\n']}"#;
        let nested: Value = serde_json::from_str(ORDERED).unwrap();
        let mut registry = state(vec![legacy_entry("target", 0, &[PYTHON], json!(7))]);
        registry["legacy"] = json!({"source_document":{"profiles":[
            true,
            {"profile_id":"truthy and different","id":PYTHON,"wrong":true},
            {"profile_id":[],"id":nested,"selected":true},
            {"profile_id":PYTHON,"wrong":true}
        ]},"source_path":nested});
        // The digest sorts keys; the physical registry's insertion order must
        // survive decoding and is observable in Python's nested dict repr.
        let original = String::from_utf8(sealed(registry)).unwrap();
        let input = original.replace(&nested.to_string(), ORDERED);
        assert_ne!(input, original);
        let loaded = load(op, Some(input.as_bytes()), budget).unwrap();
        let doc = &loaded.doc;
        let selected = select_successor(op, &loaded, budget).unwrap();
        let item = expect_entry(
            doc,
            selected,
            "target",
            SuccessorItemOrigin::LegacySource,
            PYTHON,
        );
        let legacy = doc.get(doc.root, "legacy").unwrap();
        let source = doc.get(legacy, "source_document").unwrap();
        let profiles = array(doc, doc.get(source, "profiles").unwrap()).unwrap();
        assert_eq!(item, profiles[2]);
    });
}

#[tokio::test(flavor = "current_thread")]
async fn absent_wrong_or_empty_source_falls_back_to_actual_latest_with_independent_path() {
    with_operation(|op, budget| {
        let mut row = legacy_entry(
            "target",
            0,
            &["old", "other"],
            json!("io.tobkiri.profile.v4"),
        );
        append_revision(
            &mut row,
            json!({"profile_id":"target","actual_latest":true}),
        );
        let fallback = "legacy/startup_profiles.json";
        for (legacy, expected_path) in [
            (json!({}), fallback),
            (
                json!({"source_document":{"profiles":{"old":{}}},"source_path":false}),
                fallback,
            ),
            (
                json!({"source_document":{"profiles":{"old":[{}]}},"source_path":0}),
                fallback,
            ),
            (
                json!({"source_document":{"profiles":{"other":{"wrong":true}}},"source_path":{}}),
                fallback,
            ),
            (
                json!({"source_document":{"profiles":[{"profile_id":true,"id":"old"}]},"source_path":[]}),
                fallback,
            ),
            (
                json!({"source_document":{"profiles":null},"source_path":""}),
                fallback,
            ),
            (
                json!({"source_document":"unrelated","source_path":true}),
                "True",
            ),
            (json!({"source_document":[{}],"source_path":-57}), "-57"),
        ] {
            let mut registry = state(vec![row.clone()]);
            registry["legacy"] = legacy;
            let loaded = load(op, Some(&sealed(registry)), budget).unwrap();
            let doc = &loaded.doc;
            let selected = select_successor(op, &loaded, budget).unwrap();
            let item = expect_entry(
                doc,
                selected,
                "target",
                SuccessorItemOrigin::CurrentRevision,
                expected_path,
            );
            assert_eq!(item, current(doc, 0));
        }
    });
}

#[tokio::test(flavor = "current_thread")]
async fn empty_registry_exhausts_and_reached_unhashable_versions_raise_raw_type_error() {
    with_operation(|op, budget| {
        let empty = load(op, None, budget).unwrap();
        assert!(matches!(
            select_successor(op, &empty, budget).unwrap(),
            SuccessorSelection::Exhausted
        ));
        for (version, message) in [
            (json!([]), "unhashable type: 'list'"),
            (json!({}), "unhashable type: 'dict'"),
        ] {
            let registry = state(vec![legacy_entry("target", 0, &["old"], version)]);
            let loaded = load(op, Some(&sealed(registry)), budget).unwrap();
            let error = select_successor(op, &loaded, budget).err().unwrap();
            assert_eq!(error.stage, OriginalAuthorityStage::Source);
            assert_eq!(error.class, "builtins.TypeError");
            assert_eq!(error.message, message);
            assert!(error.cause.is_none() && error.context.is_none() && !error.suppressed);
        }
        op.cancellation.cancel();
        let error = select_successor(op, &empty, budget).err().unwrap();
        assert_eq!(error.stage, OriginalAuthorityStage::Source);
        assert!(matches!(
            error.cause,
            Some(OriginalAuthorityCause::Cancelled)
        ));
    });
}
