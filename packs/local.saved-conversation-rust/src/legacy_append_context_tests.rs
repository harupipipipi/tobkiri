use super::*;
use crate::legacy_read::set_field;
use ordered_python_compat::loads;

fn verify_value(
    child: &PyValue,
    request: &PyValue,
    parent: Option<&PyValue>,
) -> Result<(), AppendError> {
    let rows = PyValue::Object(
        parent
            .into_iter()
            .map(|parent| ("parent".into(), parent.clone()))
            .collect(),
    );
    verify("defaults", child, request, &rows)
}

fn fixture() -> (PyValue, PyValue, PyValue) {
    let child = loads(
        r#"{
            "id":"child",
            "parent_conversation_id":"parent",
            "context_link":{
                "version":"tobkiri.conversation-context-link.v1",
                "parent_conversation_id":"parent",
                "created_from_parent_revision":4,
                "slot":"research"
            },
            "metadata":{"label":"子"}
        }"#,
    )
    .unwrap();
    let parent = loads(
        r#"{
            "id":"parent",
            "conversation_revision":9,
            "model_reference":"profile.model",
            "conversation_kind":"chat",
            "tags":["試験"],
            "messages":[{"id":"p1","usage":1.5}],
            "metadata":{
                "tool_selection":{"mode":"manual","include":[{"kind":"service","id":"test/service"},"second"]},
                "strategy_reference":"strategy.one",
                "thinking_level":"high",
                "workspaceId":"作業",
                "profile_id":"defaults",
                "approval_policy_reference":"policy.one",
                "irrelevant":1.5
            }
        }"#,
    )
    .unwrap();
    // context_binding(parent, "defaults") from the original protocol.
    let request = loads(
        r#"{
            "context_binding":{
                "version":"tobkiri.conversation-context-binding.v1",
                "profile_id":"defaults",
                "parent_conversation_id":"parent",
                "parent_revision":9,
                "context_digest":"sha256:e7168f7bb0123585a0b84282ac1ce5f83371022d053664dbd108831bdef34d21"
            },
            "strategy_reference":"strategy.one",
            "tool_selection":{
                "scope":"turn",
                "mode":"manual",
                "exclude":[],
                "include":[{"id":"test/service","kind":"service"},"second"],
                "must_use":false
            }
        }"#,
    )
    .unwrap();
    (child, request, parent)
}

fn fields(value: &mut PyValue) -> &mut Vec<(String, PyValue)> {
    match value {
        PyValue::Object(fields) => fields,
        _ => panic!("not an object"),
    }
}

fn replace(value: &mut PyValue, key: &str, json: &str) {
    set_field(fields(value), key, loads(json).unwrap());
}

fn rejects(child: &PyValue, request: &PyValue, parent: Option<&PyValue>) {
    assert!(matches!(
        verify_value(child, request, parent),
        Err(AppendError::Invalid)
    ));
}

#[test]
fn bare_parent_id_does_not_create_a_link_and_explicit_binding_is_rejected() {
    let (mut child, request, parent) = fixture();
    replace(&mut child, "context_link", "null");
    assert!(verify_value(&child, &loads("{}").unwrap(), None).is_ok());
    rejects(&child, &request, Some(&parent));
    rejects(&child, &loads(r#"{"context_binding":null}"#).unwrap(), None);
}

#[test]
fn matches_the_python_binding_and_scope_defaults() {
    let (child, mut request, parent) = fixture();
    assert!(verify_value(&child, &request, Some(&parent)).is_ok());
    replace(&mut request, "thinking_level", r#""high""#);
    replace(
        &mut request,
        "tool_selection",
        r#"{"mode":"none","exclude":["unused"]}"#,
    );
    assert!(verify_value(&child, &request, Some(&parent)).is_ok());
}

#[test]
fn rejects_stale_or_missing_parent_and_missing_or_forged_binding() {
    let (child, request, parent) = fixture();
    rejects(&child, &request, None);
    rejects(&child, &request, Some(&PyValue::Null));
    let mut stale = parent.clone();
    replace(&mut stale, "conversation_revision", "10");
    rejects(&child, &request, Some(&stale));
    replace(&mut stale, "conversation_revision", "true");
    rejects(&child, &request, Some(&stale));

    for (key, json) in [
        ("profile_id", r#""other""#),
        ("parent_conversation_id", r#""another-parent""#),
        ("parent_revision", "9.0"),
        (
            "context_digest",
            r#""sha256:0000000000000000000000000000000000000000000000000000000000000000""#,
        ),
        ("extra", "null"),
    ] {
        let mut forged = request.clone();
        let mut binding = forged.get("context_binding").unwrap().clone();
        replace(&mut binding, key, json);
        set_field(fields(&mut forged), "context_binding", binding);
        rejects(&child, &forged, Some(&parent));
    }
    rejects(
        &child,
        &loads(r#"{"strategy_reference":"strategy.one"}"#).unwrap(),
        Some(&parent),
    );
}

#[test]
fn verifies_current_parent_context_even_when_revision_is_unchanged() {
    let (child, request, parent) = fixture();
    let mut mutated = parent.clone();
    replace(&mut mutated, "model_reference", r#""another.model""#);
    rejects(&child, &request, Some(&mutated));
    replace(&mut mutated, "model_reference", "1.5");
    rejects(&child, &request, Some(&mutated));
    let mut mutated = parent.clone();
    replace(&mut mutated, "id", r#""wrong-parent""#);
    rejects(&child, &request, Some(&mutated));
    let mut mutated = parent.clone();
    replace(&mut mutated, "parent_conversation_id", r#""grandparent""#);
    set_field(
        fields(&mut mutated),
        "context_link",
        loads(
            r#"{
                "version":"tobkiri.conversation-context-link.v1",
                "parent_conversation_id":"grandparent",
                "created_from_parent_revision":4,
                "slot":"research"
            }"#,
        )
        .unwrap(),
    );
    rejects(&child, &request, Some(&mutated));
}

#[test]
fn rejects_malformed_link_and_child_overrides_and_checks_strict_child_data() {
    let (child, request, parent) = fixture();
    for (key, json) in [
        ("context_link", "{}"),
        ("parent_conversation_id", r#""wrong-parent""#),
        ("id", r#""parent""#),
        ("model_reference", r#""overridden""#),
        ("metadata", r#"{"workspaceId":null}"#),
        ("messages", r#"[{"usage":1.5}]"#),
    ] {
        let mut mutated = child.clone();
        replace(&mut mutated, key, json);
        rejects(&mutated, &request, Some(&parent));
    }
}

#[test]
fn rejects_tool_scope_order_target_shape_and_execution_option_changes() {
    let (child, request, parent) = fixture();
    for json in [
        r#"{"mode":"auto","include":[{"id":"test/service","kind":"service"},"second"]}"#,
        r#"{"mode":"manual","include":["second",{"id":"test/service","kind":"service"}]}"#,
        r#"{"mode":"manual","include":["test/service","second"]}"#,
        r#"{"mode":"manual","scope":"conversation","include":[{"id":"test/service","kind":"service"},"second"]}"#,
        r#"{"mode":"manual","must_use":true,"include":[{"id":"test/service","kind":"service"},"second"]}"#,
        r#"{"mode":"none","include":["second"]}"#,
        r#"{"mode":"none","must_use":1}"#,
        r#"{"mode":"none","exclude":[{"kind":"tool","id":"-invalid"}]}"#,
        r#"{"mode":"none","approved":true}"#,
        "null",
    ] {
        let mut changed = request.clone();
        replace(&mut changed, "tool_selection", json);
        rejects(&child, &changed, Some(&parent));
    }
    for (key, json) in [
        ("thinking_level", r#""low""#),
        ("thinking_level", "null"),
        ("strategy_reference", "null"),
    ] {
        let mut changed = request.clone();
        replace(&mut changed, key, json);
        rejects(&child, &changed, Some(&parent));
    }
}
