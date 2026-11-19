//! Project the original Conversation owner's append lifecycle evidence.

use std::collections::{BTreeMap, BTreeSet};

use ordered_python_compat::{PyValue, pytruthy};

use super::legacy_message_append::AppendError;
use super::legacy_read::set_field;

type Fields = Vec<(String, PyValue)>;

const VERSION: &str = "tobkiri.conversation-lifecycle.v1";
const SUCCESS: &[&str] = &["stop", "end_turn", "completed", "complete"];
const WAIT_USER: &[&str] = &["waiting_user", "waiting_for_user", "awaiting_user"];
const WAIT_APPROVAL: &[&str] = &[
    "waiting_approval",
    "awaiting_approval",
    "approval_required",
    "authority_approval_required",
];
const RUNNING: &[&str] = &["running", "streaming", "pending", "tool_call", "tool_calls"];
const CANCELLED: &[&str] = &["cancelled", "canceled", "interrupted"];
const FAILED: &[&str] = &["error", "failed", "failure"];

/// Update the lifecycle from one normalized message and the pre-append
/// transcript. A receipt is supplied only for a verified saved assistant.
pub(super) fn record(
    conversation: &mut Fields,
    message: &PyValue,
    messages: &[PyValue],
    now_ms: i64,
    receipt: Option<&PyValue>,
) -> Result<(), AppendError> {
    let mut source = match lookup(conversation, "lifecycle") {
        Some(value @ PyValue::Object(fields)) => {
            if value.get("version").and_then(PyValue::as_str) == Some(VERSION) {
                fields.clone()
            } else {
                empty_lifecycle()
            }
        }
        Some(value) if pytruthy(value) => return Err(AppendError::Compatibility),
        _ => empty_lifecycle(),
    };
    match message.get("role").and_then(PyValue::as_str) {
        Some("user") => {
            let resumed = if lookup(&source, "state").and_then(PyValue::as_str) == Some("completed")
            {
                lookup(&source, "completed_at_ms")
                    .cloned()
                    .unwrap_or(PyValue::Null)
            } else {
                PyValue::Null
            };
            let id = message.get("id").ok_or(AppendError::Compatibility)?;
            set_field(&mut source, "completion_candidate", PyValue::Null);
            set_field(&mut source, "resumed_completed_at_ms", resumed);
            set_field(&mut source, "state", text("running"));
            set_field(&mut source, "active_user_message_id", id.clone());
            set_field(&mut source, "active_user_received_at_ms", integer(now_ms));
        }
        Some("assistant") => {
            let active_user = lookup(&source, "active_user_message_id");
            if !matches_active_turn(message, messages, active_user)? {
                return Ok(());
            }
            let mut state = message_task_state(message)?;
            let candidate = match (state, receipt) {
                ("completed", Some(receipt)) => {
                    state = "running";
                    PyValue::Object(vec![
                        ("completed_at_ms".into(), integer(now_ms)),
                        ("receipt".into(), receipt.clone()),
                    ])
                }
                ("completed", None) => {
                    // A transcript success can only preserve the same completion.
                    if lookup(&source, "state").and_then(PyValue::as_str) != Some("completed")
                        || lookup(&source, "completion_message_id")
                            != Some(message.get("id").ok_or(AppendError::Compatibility)?)
                    {
                        state = "unknown";
                    }
                    PyValue::Null
                }
                _ => PyValue::Null,
            };
            set_field(&mut source, "completion_candidate", candidate);
            set_field(&mut source, "state", text(state));
        }
        _ => return Ok(()),
    }
    set_field(conversation, "lifecycle", PyValue::Object(source));
    Ok(())
}

fn matches_active_turn(
    message: &PyValue,
    messages: &[PyValue],
    active_user: Option<&PyValue>,
) -> Result<bool, AppendError> {
    let mut by_id = BTreeMap::new();
    for item in messages {
        let id = item
            .get("id")
            .and_then(PyValue::as_str)
            .ok_or(AppendError::Compatibility)?;
        by_id.insert(id, item);
    }
    let mut ancestor = message;
    let mut visited = BTreeSet::new();
    while let Some(parent) = ancestor.get("parent_id") {
        let parent_id = match parent {
            PyValue::String(id) => id.as_str(),
            PyValue::Array(_) | PyValue::Object(_) => return Err(AppendError::Compatibility),
            _ => break,
        };
        let Some(&parent) = by_id.get(parent_id) else {
            break;
        };
        if !visited.insert(parent_id) {
            return Ok(false);
        }
        ancestor = parent;
        if ancestor.get("role").and_then(PyValue::as_str) == Some("user") {
            break;
        }
    }
    Ok(active_user.is_none_or(|active_user| {
        !pytruthy(active_user)
            || (ancestor.get("role").and_then(PyValue::as_str) == Some("user")
                && ancestor.get("id") == Some(active_user))
    }))
}

/// Use the same terminal classifier for append candidates and confirmation.
pub(super) fn assistant_completed(message: &PyValue) -> Result<bool, AppendError> {
    Ok(message_task_state(message)? == "completed")
}

fn message_task_state(message: &PyValue) -> Result<&'static str, AppendError> {
    match message.get("role").and_then(PyValue::as_str) {
        Some("user") => return Ok("running"),
        Some("assistant") => {}
        _ => return Ok("unknown"),
    }
    let metadata = message.get("metadata");
    let flag = |key| metadata.and_then(|value| value.get(key)) == Some(&PyValue::Bool(true));
    let explicit = [
        metadata.and_then(|value| value.get("task_state")),
        metadata
            .and_then(|value| value.get("thinking"))
            .and_then(|value| value.get("state")),
    ];
    let status = message.get("status");
    let finish = message.get("finish_reason");
    let states = [status, finish, explicit[0], explicit[1]];
    for (names, flags, state) in [
        (CANCELLED, &["cancelled"][..], "cancelled"),
        (
            WAIT_APPROVAL,
            &["approval_required"][..],
            "waiting_approval",
        ),
        (WAIT_USER, &["waiting_for_user"][..], "waiting_user"),
        (FAILED, &[][..], "failed"),
        (RUNNING, &["draft", "streaming"][..], "running"),
    ] {
        if states.into_iter().any(|value| in_states(value, names))
            || flags.iter().any(|&key| flag(key))
        {
            return Ok(state);
        }
    }
    // Waiting, cancelled and running signals have already been handled.
    // An explicit idle, unknown, or invalid state blocks even a successful finish.
    if explicit.into_iter().flatten().any(|value| {
        !matches!(value, PyValue::Null)
            && value.as_str() != Some("")
            && !in_states(Some(value), SUCCESS)
    }) {
        return Ok("unknown");
    }
    if in_states(finish, SUCCESS) {
        match status {
            None | Some(PyValue::Null) => return Ok("completed"),
            Some(PyValue::String(status))
                if ["", "complete", "completed", "success", "succeeded"]
                    .contains(&status.as_str()) =>
            {
                return Ok("completed");
            }
            // Python only attempts membership of status in a set in this branch.
            Some(PyValue::Array(_) | PyValue::Object(_)) => {
                return Err(AppendError::Compatibility);
            }
            _ => {}
        }
    }
    Ok(if finish.is_some_and(pytruthy) {
        "failed"
    } else {
        "unknown"
    })
}

fn in_states(value: Option<&PyValue>, states: &[&str]) -> bool {
    // Only strings can lower to the protocol's ASCII state names. The
    // Python representations of other JSON values never match those names.
    value
        .and_then(PyValue::as_str)
        .is_some_and(|value| states.iter().any(|state| value.eq_ignore_ascii_case(state)))
}

fn empty_lifecycle() -> Fields {
    [
        ("version", text(VERSION)),
        ("state", text("unknown")),
        ("completed_at_ms", PyValue::Null),
        ("completion_message_id", PyValue::Null),
        ("active_user_message_id", PyValue::Null),
        ("active_user_received_at_ms", PyValue::Null),
        ("resumed_completed_at_ms", PyValue::Null),
        ("completion_candidate", PyValue::Null),
        ("confirmed_completion", PyValue::Null),
    ]
    .into_iter()
    .map(|(key, value)| (key.into(), value))
    .collect()
}

fn lookup<'a>(fields: &'a [(String, PyValue)], key: &str) -> Option<&'a PyValue> {
    fields
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

fn integer(value: i64) -> PyValue {
    PyValue::Int(value.to_string())
}

fn text(value: &str) -> PyValue {
    PyValue::String(value.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use ordered_python_compat::loads;

    fn parse(json: &str) -> PyValue {
        loads(json).unwrap()
    }

    #[test]
    fn user_receipt_resumes_the_prior_completion_and_keeps_its_evidence() {
        let mut row = parse(
            r#"{"lifecycle":{"version":"tobkiri.conversation-lifecycle.v1",
            "state":"completed","completed_at_ms":500,"completion_message_id":"prior-reply",
            "completion_candidate":{"retired":true},"confirmed_completion":{"turn_id":"prior"},
            "legacy_extension":[1,2]}}"#,
        )
        .into_object()
        .unwrap();
        let user = parse(r#"{"role":"user","id":"new-user","created_at":10}"#);
        record(&mut row, &user, &[], 800, None).unwrap();
        assert_eq!(
            lookup(&row, "lifecycle"),
            Some(&parse(
                r#"{"version":"tobkiri.conversation-lifecycle.v1","state":"running",
                "completed_at_ms":500,"completion_message_id":"prior-reply",
                "completion_candidate":null,"confirmed_completion":{"turn_id":"prior"},
                "legacy_extension":[1,2],"resumed_completed_at_ms":500,
                "active_user_message_id":"new-user","active_user_received_at_ms":800}"#,
            ))
        );
    }

    #[test]
    fn assistant_candidate_requires_verified_receipt_and_live_user_ancestry() {
        let user = parse(r#"{"role":"user","id":"user"}"#);
        let assistant = parse(
            r#"{"role":"assistant","id":"result","parent_id":"intermediate",
            "status":"complete","finish_reason":"STOP"}"#,
        );
        let receipt = parse(r#"{"turn_id":"turn","result_reference":{"digest":"ref"}}"#);
        let prior = [
            user.clone(),
            parse(r#"{"role":"assistant","id":"intermediate","parent_id":"user"}"#),
        ];
        let mut row = Vec::new();
        record(&mut row, &user, &[], 50, None).unwrap();
        record(&mut row, &assistant, &prior, 80, None).unwrap();
        let lifecycle = lookup(&row, "lifecycle").unwrap();
        assert_eq!(lifecycle.get("state"), Some(&text("unknown")));
        assert_eq!(lifecycle.get("completion_candidate"), Some(&PyValue::Null));

        record(&mut row, &assistant, &prior, 90, Some(&receipt)).unwrap();
        let lifecycle = lookup(&row, "lifecycle").unwrap();
        assert_eq!(lifecycle.get("state"), Some(&text("running")));
        let candidate = lifecycle.get("completion_candidate").unwrap();
        assert_eq!(candidate.get("receipt"), Some(&receipt));
        assert_eq!(candidate.get("completed_at_ms"), Some(&integer(90)));
        assert_eq!(lifecycle.get("confirmed_completion"), Some(&PyValue::Null));

        let newer = parse(r#"{"role":"user","id":"newer"}"#);
        record(&mut row, &newer, &prior, 100, None).unwrap();
        let before = row.clone();
        record(&mut row, &assistant, &prior, 110, Some(&receipt)).unwrap();
        assert_eq!(row, before);
    }

    #[test]
    fn a_cycle_leaves_even_an_unrecognized_lifecycle_version_untouched() {
        let mut row = parse(r#"{"lifecycle":{"version":"future","retained":true}}"#)
            .into_object()
            .unwrap();
        let before = row.clone();
        let assistant = parse(r#"{"role":"assistant","id":"answer","parent_id":"first"}"#);
        let messages = [
            parse(r#"{"role":"assistant","id":"first","parent_id":"second"}"#),
            parse(r#"{"role":"assistant","id":"second","parent_id":"first"}"#),
        ];
        record(&mut row, &assistant, &messages, 0, None).unwrap();
        assert_eq!(row, before);
    }

    #[test]
    fn classifier_preserves_precedence_and_explicit_unknown_states() {
        for (fields, expected) in [
            (
                r#""status":"error","finish_reason":"WAITING_USER","metadata":{"cancelled":true}"#,
                "cancelled",
            ),
            (
                r#""finish_reason":"waiting_for_user","metadata":{"approval_required":true}"#,
                "waiting_approval",
            ),
            (
                r#""status":"FAILED","finish_reason":"awaiting_user""#,
                "waiting_user",
            ),
            (r#""status":"running","finish_reason":"error""#, "failed"),
            (
                r#""finish_reason":"stop","metadata":{"thinking":{"state":"TOOL_CALL"}}"#,
                "running",
            ),
            (
                r#""status":"complete","finish_reason":"stop","metadata":{"task_state":"idle"}"#,
                "unknown",
            ),
            (
                r#""status":"complete","finish_reason":"stop","metadata":{"task_state":false}"#,
                "unknown",
            ),
            (r#""status":"COMPLETE","finish_reason":"stop""#, "failed"),
            (
                r#""status":"complete","finish_reason":"END_TURN","metadata":{"cancelled":1}"#,
                "completed",
            ),
            (r#""status":"complete","finish_reason":123"#, "failed"),
            (r#""status":"complete","finish_reason":0"#, "unknown"),
        ] {
            let message = parse(&format!(r#"{{"role":"assistant",{fields}}}"#));
            assert_eq!(message_task_state(&message), Ok(expected), "{fields}");
            assert_eq!(assistant_completed(&message), Ok(expected == "completed"));
        }
    }

    #[test]
    fn malformed_lifecycle_and_unhashable_terminal_status_request_compatibility() {
        let mut row = parse(r#"{"lifecycle":true}"#).into_object().unwrap();
        let before = row.clone();
        let user = parse(r#"{"role":"user","id":"user"}"#);
        assert_eq!(
            record(&mut row, &user, &[], 0, None),
            Err(AppendError::Compatibility)
        );
        assert_eq!(row, before);
        let terminal = parse(r#"{"role":"assistant","status":[],"finish_reason":"stop"}"#);
        let blocked = parse(
            r#"{"role":"assistant","status":[],"finish_reason":"stop","metadata":{"draft":true}}"#,
        );
        assert_eq!(
            assistant_completed(&terminal),
            Err(AppendError::Compatibility)
        );
        assert_eq!(assistant_completed(&blocked), Ok(false));
    }
}
