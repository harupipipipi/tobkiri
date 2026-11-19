//! Exact confirmed reference syntax and owner snapshot bindings.

use serde_json::Value;
use std::collections::BTreeSet;

use super::{has_fields, identifier, is_digest, py_nonblank};
use crate::{Error, Result, check_canonical_size};

const MAX_SNAPSHOT_BYTES: usize = 64 * 1024;

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct Reference<'a> {
    kind: &'a str,
    profile_id: &'a str,
    id: &'a str,
}

fn chat_references(value: &Value) -> Result<Vec<Reference<'_>>> {
    let items = value
        .as_array()
        .filter(|items| items.len() <= 16)
        .ok_or(Error::Invalid("saved chat references are invalid"))?;
    let mut seen = BTreeSet::new();
    let mut references = Vec::with_capacity(items.len());
    let identity_error = |_| Error::Invalid("saved chat reference identity is invalid");
    for item in items {
        if !has_fields(item, &["kind", "profile_id", "id"]) {
            return Err(Error::Invalid("saved chat reference fields are invalid"));
        }
        let kind = item["kind"]
            .as_str()
            .filter(|kind| matches!(*kind, "chat" | "group"))
            .ok_or(Error::Invalid("saved chat reference kind is invalid"))?;
        let reference = Reference {
            kind,
            profile_id: identifier(&item["profile_id"]).map_err(identity_error)?,
            id: identifier(&item["id"]).map_err(identity_error)?,
        };
        if !seen.insert(reference) {
            return Err(Error::Invalid("duplicate saved chat reference"));
        }
        references.push(reference);
    }
    Ok(references)
}

fn reference_row(row: &Value, reference: Reference<'_>) -> Result<()> {
    if !has_fields(
        row,
        &[
            "kind",
            "id",
            "label",
            "conversation_ids",
            "snapshot_digest",
            "member_count",
            "membership_complete",
        ],
    ) || row["kind"].as_str() != Some(reference.kind)
        || row["id"].as_str() != Some(reference.id)
    {
        return Err(Error::Invalid("resolved chat reference identity mismatch"));
    }
    if !row["label"].as_str().is_some_and(|label| {
        label.len() <= 1024 && label.chars().count() <= 256 && py_nonblank(label)
    }) {
        return Err(Error::Invalid("resolved chat reference label is invalid"));
    }
    let members = row["conversation_ids"]
        .as_array()
        .filter(|members| members.len() <= 256)
        .ok_or(Error::Invalid(
            "resolved chat reference members are invalid",
        ))?;
    let mut unique = BTreeSet::new();
    for member in members {
        let id = identifier(member)
            .map_err(|_| Error::Invalid("resolved chat reference member identity is invalid"))?;
        unique.insert(id);
    }
    if row["member_count"].as_u64() != Some(members.len() as u64)
        || row["membership_complete"].as_bool() != Some(true)
    {
        return Err(Error::Invalid(
            "resolved chat reference membership is incomplete",
        ));
    }
    if unique.len() != members.len() {
        return Err(Error::Invalid("duplicate resolved chat reference member"));
    }
    if reference.kind == "chat" && (members.len() != 1 || members[0].as_str() != Some(reference.id))
    {
        return Err(Error::Invalid(
            "resolved chat reference members do not match chat",
        ));
    }
    if !row["snapshot_digest"].as_str().is_some_and(is_digest) {
        return Err(Error::Invalid("resolved chat reference digest is invalid"));
    }
    Ok(())
}

fn resolved_chat_references(value: &Value, references: &[Reference<'_>]) -> Result<()> {
    if !has_fields(
        value,
        &[
            "kind",
            "profile_id",
            "store_revision",
            "project_revision",
            "references",
            "snapshot_time",
            "expires_at",
            "next_cursor",
            "truncated",
        ],
    ) {
        return Err(Error::Invalid(
            "resolved chat reference snapshot fields are invalid",
        ));
    }
    if value["kind"].as_str() != Some("tobkiri.chat.reference.snapshot.v1") {
        return Err(Error::Invalid(
            "resolved chat reference snapshot kind is invalid",
        ));
    }
    let profile = identifier(&value["profile_id"])
        .map_err(|_| Error::Invalid("resolved chat reference profile is invalid"))?;
    if references
        .iter()
        .any(|reference| reference.profile_id != profile)
    {
        return Err(Error::Invalid("saved chat reference profile mismatch"));
    }
    if ["store_revision", "project_revision"]
        .iter()
        .any(|key| value[*key].as_u64().is_none())
    {
        return Err(Error::Invalid(
            "resolved chat reference revision is invalid",
        ));
    }
    if !value["next_cursor"].is_null() || value["truncated"].as_bool() != Some(false) {
        return Err(Error::Invalid(
            "resolved chat reference snapshot is incomplete",
        ));
    }
    let rows = value["references"]
        .as_array()
        .filter(|rows| rows.len() == references.len())
        .ok_or(Error::Invalid("resolved chat references do not match"))?;
    for (row, reference) in rows.iter().zip(references) {
        reference_row(row, *reference)?;
    }
    let timestamp = value["snapshot_time"].as_u64().ok_or(Error::Invalid(
        "resolved chat reference timestamp is invalid",
    ))?;
    let expiry = value["expires_at"].as_u64().ok_or(Error::Invalid(
        "resolved chat reference timestamp is invalid",
    ))?;
    if timestamp.checked_add(600_000) != Some(expiry) {
        return Err(Error::Invalid("resolved chat reference expiry is invalid"));
    }
    check_canonical_size(value, MAX_SNAPSHOT_BYTES)?;
    Ok(())
}

pub(super) fn validate(value: &Value) -> Result<()> {
    let references = value
        .get("chat_references")
        .map(chat_references)
        .transpose()?
        .unwrap_or_default();
    if let Some(snapshot) = value.get("resolved_chat_references") {
        resolved_chat_references(snapshot, &references)?;
    } else if !references.is_empty() {
        return Err(Error::Invalid(
            "saved chat references require owner resolution",
        ));
    }
    Ok(())
}
