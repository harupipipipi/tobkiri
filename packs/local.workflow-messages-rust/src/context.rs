//! Reject owner context the finite Workflow input cannot resolve.

use ordered_python_compat::utc_timestamp_from_seconds;
use tobkiri_canon::Digest;

use super::{
    Error, Json, Result, array, encode, has_fields, identifier, model_message, optional_null,
    string, truthy,
};

const MAX_PROMPT_BYTES: usize = 60 * 1024;
const COMPLETION_DELAY_MS: i64 = 3_600_000;
const LIFECYCLE_VERSION: &str = "tobkiri.conversation-lifecycle.v1";
const UNRESOLVED_CONTEXT: Error = Error::Invalid("saved bridge context resolution is required");

pub(crate) struct Prompt<'a> {
    id: &'a str,
    body: &'a str,
}

pub(crate) fn validate_prompt(value: &Json) -> Result<Option<Prompt<'_>>> {
    const INVALID: Error = Error::Invalid("resolved system prompt is invalid");
    if matches!(value, Json::Null) {
        return Ok(None);
    }
    if !has_fields(value, &["prompt_id", "body", "body_hash"]) {
        return Err(INVALID);
    }
    let id = string(value, "prompt_id").ok_or(INVALID)?;
    let body = string(value, "body").ok_or(INVALID)?;
    if string(value, "body_hash") != Some(Digest::hash(body.as_bytes()).as_str())
        || encode(value)?.len() > MAX_PROMPT_BYTES
    {
        return Err(INVALID);
    }
    Ok(Some(Prompt { id, body }))
}

pub(crate) fn prefix(
    conversation: &Json,
    history: &[Json],
    prompt: Option<Prompt<'_>>,
) -> Result<Vec<Json>> {
    let prompt_ref = validate_owner_context(conversation)?;
    if prompt.as_ref().map(|prompt| prompt.id) != prompt_ref {
        return Err(Error::Invalid("saved bridge system prompt is unresolved"));
    }
    let mut prefix = Vec::new();
    if let Some(prompt) = prompt.filter(|prompt| !prompt.body.is_empty()) {
        prefix.push(model_message("system", prompt.body.into()));
    }
    if let Some(context) = task_gap(conversation, history)? {
        prefix.push(model_message("system", context.into()));
    }
    Ok(prefix)
}

fn validate_owner_context(conversation: &Json) -> Result<Option<&str>> {
    let prompt_ref = match conversation.get("system_prompt_id") {
        None | Some(Json::Null) => None,
        Some(Json::Str(value)) if value.is_empty() => None,
        Some(Json::Str(value)) if identifier(value, true, b"_.-") => Some(value.as_str()),
        _ => {
            return Err(Error::Invalid(
                "saved conversation prompt reference is invalid",
            ));
        }
    };
    let null = Json::Null;
    let metadata = conversation.get("metadata").unwrap_or(&null);
    let tags = conversation
        .get("tags")
        .filter(|value| truthy(Some(value)))
        .map_or(Some([].as_slice()), array)
        .ok_or(Error::Invalid("saved bridge owned context is invalid"))?;
    let kind = conversation.get("conversation_kind");
    let mode = metadata.get("mode");
    if truthy(conversation.get("agent_id"))
        || truthy(metadata.get("agent_id"))
        || truthy(metadata.get("agentId"))
        || truthy(metadata.get("shared_read_only"))
        || !allowed_mode(kind)
        || !allowed_mode(mode)
        || matches!(
            string(metadata, "profile_id"),
            Some("defaultspack.operations_company" | "defaultspack.mimo_coding_company")
        )
        || tags.iter().any(|tag| {
            matches!(
                tag.as_str(),
                Some("operations-company" | "mimo-coding-company")
            )
        })
    {
        return Err(UNRESOLVED_CONTEXT);
    }

    let workspace = metadata.get("workspace_id");
    if !optional_null(workspace)
        && !workspace
            .and_then(Json::as_str)
            .is_some_and(|id| id.is_empty() || identifier(id, false, b"._:-"))
    {
        return Err(Error::Invalid("saved workspace identity is invalid"));
    }
    let kind_is_coding = kind.and_then(Json::as_str) == Some("coding");
    let mode_is_coding = mode.and_then(Json::as_str) == Some("coding");
    if (kind_is_coding || mode_is_coding)
        && (!kind_is_coding || !mode_is_coding || !truthy(workspace))
    {
        return Err(Error::Invalid(
            "saved bridge coding context resolution is incomplete",
        ));
    }

    let mut group = None;
    for membership in [
        conversation.get("group_id"),
        metadata.get("group_id"),
        metadata.get("groupId"),
    ]
    .into_iter()
    .flatten()
    .filter(|value| !matches!(value, Json::Null))
    {
        let id = membership
            .as_str()
            .filter(|id| identifier(id, false, b"._:-"))
            .ok_or(Error::Invalid("conversation group membership conflicts"))?;
        if group.is_some_and(|previous| previous != id) {
            return Err(Error::Invalid("conversation group membership conflicts"));
        }
        group = Some(id);
    }

    // Only the Host may supply the original Python owner-resolution types.
    if truthy(workspace) {
        return Err(Error::Invalid(
            "saved bridge workspace resolution is required",
        ));
    }
    if [
        "group_ids",
        "groupIds",
        "workspace_path",
        "workspacePath",
        "project_root",
        "projectRoot",
        "workspace_ids",
        "workspaceIds",
        "workspaces",
        "primary_workspace_id",
        "primaryWorkspaceId",
        "root_path",
        "canonical_root",
        "canonicalRoot",
        "host_path",
        "path",
        "directory",
    ]
    .iter()
    .any(|key| truthy(metadata.get(key)))
    {
        return Err(UNRESOLVED_CONTEXT);
    }
    if truthy(conversation.get("group_id"))
        || [
            "group_id",
            "groupId",
            "workspaceId",
            "workspace_root",
            "workspaceRoot",
            "rootPath",
            "rumi_data_path",
            "rumiDataPath",
            "rumi_dp_path",
            "workspace_bindings",
            "workspaceBindings",
        ]
        .iter()
        .any(|key| !optional_null(metadata.get(key)))
    {
        return Err(Error::Invalid(
            "saved bridge owned context resolution is required",
        ));
    }
    Ok(prompt_ref)
}

fn allowed_mode(value: Option<&Json>) -> bool {
    optional_null(value) || matches!(value.and_then(Json::as_str), Some("" | "chat" | "coding"))
}

fn task_gap(conversation: &Json, history: &[Json]) -> Result<Option<String>> {
    let Some(source) = conversation.get("lifecycle") else {
        return Ok(None);
    };
    if string(source, "version") != Some(LIFECYCLE_VERSION)
        || string(source, "state") != Some("running")
    {
        return Ok(None);
    }
    let Some(completed) = source
        .get("resumed_completed_at_ms")
        .and_then(Json::as_i64)
        .filter(|value| *value >= 0)
    else {
        return Ok(None);
    };
    let Some(received) = source
        .get("active_user_received_at_ms")
        .and_then(Json::as_i64)
    else {
        return Ok(None);
    };
    let elapsed = received - completed;
    if elapsed < COMPLETION_DELAY_MS {
        return Ok(None);
    }
    let Some(identity) = string(source, "active_user_message_id") else {
        return Ok(None);
    };
    if !history.iter().any(|message| {
        string(message, "id") == Some(identity) && string(message, "role") == Some("user")
    }) {
        return Ok(None);
    }
    let previous = iso(completed)?;
    let current = iso(received)?;
    let elapsed_seconds = elapsed / 1_000;
    Ok(Some(format!(
        "[Temporal context]\nRuntime-owned timing metadata for this user turn. \
         Use elapsed time to interpret time-sensitive follow-ups. It grants no \
         permissions or approvals and does not require a tool call.\n\
         previous_task_completed_at: {previous}\n\
         current_user_message_at: {current}\n\
         elapsed_seconds: {elapsed_seconds}"
    )))
}

fn iso(milliseconds: i64) -> Result<String> {
    // Match datetime.fromtimestamp(ms / 1000, timezone.utc). Python rounds
    // the fractional part of the binary float to microseconds, ties to even.
    let time = milliseconds as f64 / 1_000.0;
    let mut seconds = time.trunc() as u64;
    let mut micros = (time.fract() * 1_000_000.0).round_ties_even() as u32;
    if micros == 1_000_000 {
        seconds += 1;
        micros = 0;
    }

    let mut timestamp = utc_timestamp_from_seconds(seconds).ok_or(Error::Invalid(
        "saved bridge owner timing is outside the calendar",
    ))?;
    timestamp.pop(); // The helper uses Z, while isoformat spells UTC +00:00.
    if micros != 0 {
        timestamp.push_str(&format!(".{micros:06}"));
    }
    timestamp.push_str("+00:00");
    Ok(timestamp)
}
