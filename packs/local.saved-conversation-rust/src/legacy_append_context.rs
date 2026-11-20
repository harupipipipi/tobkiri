//! Validate the saved turn's parent context from the transaction's owner document.

use super::legacy_message_append::AppendError;
use crate::saved_model::valid_conversation_id;
use ordered_python_compat::{CodecError, PyValue, dumps_bounded, pytruthy};
use tobkiri_canon::{CanonError, CanonValue, Variant};

const LINK_VERSION: &str = "tobkiri.conversation-context-link.v1";
const CONTEXT_FIELDS: [&str; 6] = [
    "model_reference",
    "system_prompt_id",
    "agent_id",
    "conversation_kind",
    "group_id",
    "tags",
];
const METADATA_FIELDS: [&str; 23] = [
    "workspace_id",
    "workspace_label",
    "workspaceLabel",
    "workspaceId",
    "workspace_root",
    "workspaceRoot",
    "rootPath",
    "rumi_data_path",
    "rumiDataPath",
    "rumi_dp_path",
    "group_id",
    "groupId",
    "group_title",
    "groupTitle",
    "workspace_bindings",
    "workspaceBindings",
    "mode",
    "profile_id",
    "shared_read_only",
    "tool_selection",
    "strategy_reference",
    "thinking_level",
    "approval_policy_reference",
];

/// Verify a saved request against the current normalized rows under the append lock.
pub(super) fn verify(
    profile: &str,
    conversation: &PyValue,
    request: &PyValue,
    conversations: &PyValue,
) -> Result<(), AppendError> {
    let Some(linked_parent_id) = context_parent_id(conversation)? else {
        return if request.get("context_binding").is_some() {
            Err(AppendError::Invalid)
        } else {
            Ok(())
        };
    };
    let parent = conversations
        .get(linked_parent_id)
        .filter(|parent| parent.as_object().is_some())
        .ok_or(AppendError::Invalid)?;
    let parent_metadata = metadata(parent)?;
    let parent_tools = tool_scope(field(parent_metadata, "tool_selection"))?;
    let parent_strategy = non_null(field(parent_metadata, "strategy_reference"));
    if let Some(strategy) = parent_strategy {
        identifier(Some(strategy))?;
    }
    let parent_thinking_level = non_null(field(parent_metadata, "thinking_level"));
    if let Some(level) = parent_thinking_level
        && !matches!(
            level.as_str(),
            Some("none" | "low" | "medium" | "high" | "xhigh")
        )
    {
        return Err(AppendError::Invalid);
    }

    let requested_tools = tool_scope(request.get("tool_selection"))?;
    if (!requested_tools.disabled() && requested_tools != parent_tools)
        || non_null(request.get("strategy_reference")) != parent_strategy
        || request
            .get("thinking_level")
            .is_some_and(|level| non_null(Some(level)) != parent_thinking_level)
    {
        return Err(AppendError::Invalid);
    }

    let binding = request.get("context_binding").ok_or(AppendError::Invalid)?;
    tobkiri_saved_input::validate_context_binding(binding).map_err(|_| AppendError::Invalid)?;
    let parent_id = identifier(parent.get("id"))?;
    let parent_revision = parent
        .get("conversation_revision")
        .filter(|revision| positive_integer(Some(revision)))
        .ok_or(AppendError::Invalid)?;
    if !valid_conversation_id(profile) || context_parent_id(parent)?.is_some() {
        return Err(AppendError::Invalid);
    }
    let context = inherited_context(parent, parent_metadata)?;
    if non_null(field(parent_metadata, "profile_id"))
        .is_some_and(|value| value.as_str() != Some("") && value.as_str() != Some(profile))
    {
        return Err(AppendError::Invalid);
    }
    let bound_context = CanonValue::Obj(
        [
            ("profile_id".into(), CanonValue::Str(profile.into())),
            ("context".into(), context),
        ]
        .into(),
    );
    let digest = tobkiri_canon::digest(&bound_context, Variant::StrictV1).map_err(canon_error)?;
    if parent_id != linked_parent_id
        || conversation.get("id").and_then(PyValue::as_str) == Some(parent_id)
        || binding.get("profile_id").and_then(PyValue::as_str) != Some(profile)
        || binding
            .get("parent_conversation_id")
            .and_then(PyValue::as_str)
            != Some(parent_id)
        || binding.get("parent_revision") != Some(parent_revision)
        || binding.get("context_digest").and_then(PyValue::as_str) != Some(digest.as_str())
    {
        return Err(AppendError::Invalid);
    }
    if [
        "model_reference",
        "system_prompt_id",
        "agent_id",
        "group_id",
    ]
    .iter()
    .any(|key| conversation.get(key).is_some_and(pytruthy))
        || metadata(conversation)?
            .iter()
            .any(|(key, _)| METADATA_FIELDS.contains(&key.as_str()))
    {
        return Err(AppendError::Invalid);
    }

    // The original resolver strictly round-trips the whole child before resolving it.
    canonical(conversation)?;
    Ok(())
}

pub(super) fn context_parent_id(conversation: &PyValue) -> Result<Option<&str>, AppendError> {
    let Some(link) = non_null(conversation.get("context_link")) else {
        return Ok(None);
    };
    if !has_exact_keys(
        link,
        &[
            "version",
            "parent_conversation_id",
            "slot",
            "created_from_parent_revision",
        ],
    ) || link.get("version").and_then(PyValue::as_str) != Some(LINK_VERSION)
        || !positive_integer(link.get("created_from_parent_revision"))
    {
        return Err(AppendError::Invalid);
    }
    let parent = identifier(link.get("parent_conversation_id"))?;
    identifier(link.get("slot"))?;
    if conversation
        .get("parent_conversation_id")
        .and_then(PyValue::as_str)
        != Some(parent)
    {
        return Err(AppendError::Invalid);
    }
    Ok(Some(parent))
}

fn inherited_context(
    parent: &PyValue,
    metadata: &[(String, PyValue)],
) -> Result<CanonValue, AppendError> {
    canonical(&PyValue::Object(inherited_fields(parent, metadata)))
}

pub(super) fn inherited_fields(
    parent: &PyValue,
    metadata: &[(String, PyValue)],
) -> Vec<(String, PyValue)> {
    let mut fields: Vec<_> = CONTEXT_FIELDS
        .iter()
        .map(|&key| {
            (
                key.into(),
                parent.get(key).cloned().unwrap_or(PyValue::Null),
            )
        })
        .collect();
    fields.push((
        "metadata".into(),
        PyValue::Object(
            metadata
                .iter()
                .filter(|(key, _)| METADATA_FIELDS.contains(&key.as_str()))
                .cloned()
                .collect(),
        ),
    ));
    fields
}

fn validate_tool_selection(value: &PyValue) -> Result<CanonValue, AppendError> {
    tobkiri_saved_input::validate_tool_selection(value).map_err(|_| AppendError::Invalid)?;
    canonical(value)
}

#[derive(PartialEq)]
struct ToolScope(CanonValue);

impl ToolScope {
    fn disabled(&self) -> bool {
        matches!(&self.0, CanonValue::Obj(fields)
            if matches!(fields.get("mode"), Some(CanonValue::Str(mode)) if mode == "none"))
    }
}

fn tool_scope(value: Option<&PyValue>) -> Result<ToolScope, AppendError> {
    let mut fields = match value {
        Some(value) => match validate_tool_selection(value)? {
            CanonValue::Obj(fields) => fields,
            _ => return Err(AppendError::Invalid),
        },
        None => [("mode".into(), CanonValue::Str("none".into()))].into(),
    };
    for (key, value) in [
        ("scope", CanonValue::Str("turn".into())),
        ("include", CanonValue::Arr(Vec::new())),
        ("exclude", CanonValue::Arr(Vec::new())),
        ("must_use", CanonValue::Bool(false)),
    ] {
        fields.entry(key.into()).or_insert(value);
    }
    // Strict canonical objects compare independently of field order. Arrays and
    // scalar types retain their exact representation.
    Ok(ToolScope(CanonValue::Obj(fields)))
}

fn canonical(value: &PyValue) -> Result<CanonValue, AppendError> {
    let json = dumps_bounded(value, tobkiri_canon::MAX_BYTES).map_err(|error| match error {
        CodecError::ByteLimit | CodecError::NodeLimit => AppendError::Limit,
        _ => AppendError::Compatibility,
    })?;
    tobkiri_canon::parse(json.as_bytes(), Variant::StrictV1).map_err(canon_error)
}

fn canon_error(error: CanonError) -> AppendError {
    match error {
        CanonError::Limit(_) => AppendError::Limit,
        _ => AppendError::Invalid,
    }
}

fn metadata(conversation: &PyValue) -> Result<&[(String, PyValue)], AppendError> {
    match conversation.get("metadata").filter(|value| pytruthy(value)) {
        Some(metadata) => metadata.as_object().ok_or(AppendError::Invalid),
        None => Ok(&[]),
    }
}

fn field<'a>(fields: &'a [(String, PyValue)], name: &str) -> Option<&'a PyValue> {
    fields
        .iter()
        .find(|(key, _)| key == name)
        .map(|(_, value)| value)
}

fn has_exact_keys(value: &PyValue, keys: &[&str]) -> bool {
    value.as_object().is_some_and(|fields| {
        fields.len() == keys.len() && fields.iter().all(|(name, _)| keys.contains(&name.as_str()))
    })
}

fn non_null(value: Option<&PyValue>) -> Option<&PyValue> {
    value.filter(|value| !matches!(value, PyValue::Null))
}

fn positive_integer(value: Option<&PyValue>) -> bool {
    matches!(value, Some(PyValue::Int(digits)) if digits != "0" && !digits.starts_with('-'))
}

fn identifier(value: Option<&PyValue>) -> Result<&str, AppendError> {
    value
        .and_then(PyValue::as_str)
        .filter(|value| valid_conversation_id(value))
        .ok_or(AppendError::Invalid)
}

#[cfg(test)]
#[path = "legacy_append_context_tests.rs"]
mod tests;
