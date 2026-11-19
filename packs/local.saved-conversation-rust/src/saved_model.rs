//! Exact model-reference lookup in the legacy conversation owner document.

use ordered_python_compat::{PyValue, pystrip};

pub(super) const STORE_VERSION: &str = "rumi.conversation-store.v1";

/// The reason the owner document could not supply a model reference.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum SelectionError {
    Invalid,
    NotFound,
    Stale,
    Profile,
    Context,
}

/// Borrow the stored model reference for the exact conversation and revision.
pub(super) fn select<'document>(
    document: &'document PyValue,
    profile_id: &str,
    conversation_id: &str,
    revision: u64,
) -> Result<&'document str, SelectionError> {
    if revision == 0 || !valid_conversation_id(conversation_id) {
        return Err(SelectionError::Invalid);
    }
    if profile_id.is_empty()
        || profile_id != pystrip(profile_id)
        || profile_id.contains(['/', '\\'])
        || profile_id.contains("..")
    {
        return Err(SelectionError::Profile);
    }
    if document.get("version").and_then(PyValue::as_str) != Some(STORE_VERSION) {
        return Err(SelectionError::Invalid);
    }
    let owner = document
        .get("profile_id")
        .and_then(PyValue::as_str)
        .ok_or(SelectionError::Invalid)?;
    if owner != profile_id {
        return Err(SelectionError::Profile);
    }
    let conversations = document
        .get("conversations")
        .filter(|value| value.as_object().is_some())
        .ok_or(SelectionError::Invalid)?;
    let conversation = conversations
        .get(conversation_id)
        .filter(|value| value.as_object().is_some())
        .ok_or(SelectionError::NotFound)?;
    if conversation.get("id").and_then(PyValue::as_str) != Some(conversation_id) {
        return Err(SelectionError::Invalid);
    }
    // Linked models come from the separately validated parent context.
    if !matches!(conversation.get("context_link"), None | Some(PyValue::Null)) {
        return Err(SelectionError::Context);
    }
    match conversation.get("conversation_revision") {
        Some(PyValue::Int(stored)) if stored.parse::<u64>().ok() == Some(revision) => {}
        _ => return Err(SelectionError::Stale),
    }
    conversation
        .get("model_reference")
        .and_then(PyValue::as_str)
        .filter(|model| !pystrip(model).is_empty())
        .ok_or(SelectionError::Invalid)
}

pub(super) fn valid_conversation_id(value: &str) -> bool {
    let Some((first, rest)) = value.as_bytes().split_first() else {
        return false;
    };
    value.len() <= 256
        && first.is_ascii_alphanumeric()
        && rest
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ordered_python_compat::loads;

    fn document(row: &str) -> PyValue {
        loads(&format!(
            r#"{{"version":"{STORE_VERSION}","profile_id":"profile-1","revision":50,
            "conversations":{{"C.1_x-y:z":{row}}}}}"#
        ))
        .unwrap()
    }

    #[test]
    fn reads_the_unmodified_reference_at_the_exact_row_revision() {
        let state = document(
            r#"{"id":"C.1_x-y:z","conversation_revision":18446744073709551615,
                "parent_conversation_id":"other","context_link":null,
                "model_reference":" Vendor/Model-v1 "}"#,
        );
        assert_eq!(
            select(&state, "profile-1", "C.1_x-y:z", u64::MAX),
            Ok(" Vendor/Model-v1 ")
        );
        assert_eq!(
            select(&state, "profile-1", "C.1_x-y:z", 50),
            Err(SelectionError::Stale)
        );
        assert_eq!(
            select(&state, "profile-2", "C.1_x-y:z", u64::MAX),
            Err(SelectionError::Profile)
        );
        assert_eq!(
            select(&state, "profile-1", "c.1_x-y:z", u64::MAX),
            Err(SelectionError::NotFound)
        );
    }

    #[test]
    fn rejects_invalid_requests_and_mismatched_row_identity() {
        let state =
            document(r#"{"id":"C.1_x-y:z ","conversation_revision":1,"model_reference":"model"}"#);
        for id in [" C.1_x-y:z", "C.1_x-y:z ", "é", "-leading"] {
            assert_eq!(
                select(&state, "profile-1", id, 1),
                Err(SelectionError::Invalid)
            );
        }
        assert!(valid_conversation_id(&"a".repeat(256)));
        assert!(!valid_conversation_id(&"a".repeat(257)));
        assert_eq!(
            select(&state, "profile-1", "C.1_x-y:z", 0),
            Err(SelectionError::Invalid)
        );
        assert_eq!(
            select(&state, "profile-1", "C.1_x-y:z", 1),
            Err(SelectionError::Invalid)
        );
        assert_eq!(
            select(&document("null"), "profile-1", "C.1_x-y:z", 1),
            Err(SelectionError::NotFound)
        );
    }

    #[test]
    fn rejects_coerced_revisions_and_missing_or_blank_model_references() {
        for revision in ["true", "1.0", r#""1""#, "0", "-1", "18446744073709551616"] {
            let state = document(&format!(
                r#"{{"id":"C.1_x-y:z","conversation_revision":{revision},"model_reference":"model"}}"#
            ));
            assert_eq!(
                select(&state, "profile-1", "C.1_x-y:z", 1),
                Err(SelectionError::Stale)
            );
        }
        for model in ["null", "7", r#""\u001f\u2003""#] {
            let state = document(&format!(
                r#"{{"id":"C.1_x-y:z","conversation_revision":1,"model_reference":{model}}}"#
            ));
            assert_eq!(
                select(&state, "profile-1", "C.1_x-y:z", 1),
                Err(SelectionError::Invalid)
            );
        }
        let state = document(r#"{"id":"C.1_x-y:z","conversation_revision":1}"#);
        assert_eq!(
            select(&state, "profile-1", "C.1_x-y:z", 1),
            Err(SelectionError::Invalid)
        );
    }

    #[test]
    fn refuses_local_model_on_unresolved_linked_records() {
        let state = document(
            r#"{"id":"C.1_x-y:z","conversation_revision":1,"model_reference":"model",
                "parent_conversation_id":"other","context_link":{
                    "version":"tobkiri.conversation-context-link.v1","parent_conversation_id":"other",
                    "slot":"child","created_from_parent_revision":1}}"#,
        );
        assert_eq!(
            select(&state, "profile-1", "C.1_x-y:z", 1),
            Err(SelectionError::Context)
        );
    }
}
