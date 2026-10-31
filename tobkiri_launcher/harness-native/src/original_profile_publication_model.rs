//! The original predecessor/current pointer parser, after intent HMAC.
use super::*;

pub(super) fn pointer_error(message: impl Into<String>) -> OriginalAuthorityError {
    raw(
        "core_runtime.active_profile_store_v4.ActiveProfileStoreIntegrityError",
        message,
    )
}
fn canonical_ascii_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    if !bytes.first().is_some_and(u8::is_ascii_lowercase) {
        return false;
    }
    let mut separator = false;
    for &byte in bytes {
        if byte.is_ascii_lowercase() || byte.is_ascii_digit() {
            separator = false;
        } else if b"._-".contains(&byte) && !separator {
            separator = true;
        } else {
            return false;
        }
    }
    !separator
}
pub(super) fn kind(value: &Node) -> &'static str {
    match value {
        Node::String(_) => "str",
        Node::Array(_) => "list",
        Node::Object(_) => "dict",
        Node::Scalar(CanonValue::Null) => "NoneType",
        Node::Scalar(CanonValue::Bool(_)) => "bool",
        Node::Scalar(CanonValue::Int(_) | CanonValue::BigInt(_)) => "int",
        Node::Scalar(CanonValue::Float(_)) => "float",
        Node::Scalar(_) => unreachable!("scalar arena"),
    }
}
const POINTER_FIELDS: [&str; 10] = [
    "profile_id",
    "profile_revision",
    "activation_id",
    "plan_digest",
    "lock_digest",
    "activation_snapshot_path",
    "activation_snapshot_digest",
    "catalog_revision",
    "generation",
    "updated_at",
];

/// Native-only indexes into one live, funded arena; never a guest reference,
/// authority witness or serialized continuation. Both arenas remain in scope.
pub(super) struct Pointer {
    fields: [usize; 10],
}
impl Pointer {
    pub(super) fn field(&self, name: &str) -> usize {
        self.fields[POINTER_FIELDS
            .iter()
            .position(|field| *field == name)
            .expect("fixed pointer field")]
    }
    pub(super) fn text(&self, doc: &Document, name: &str) -> Result<String> {
        match doc.node(self.field(name)) {
            Node::String(text) => text.utf8(),
            _ => Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Source,
                "validated pointer text",
            )),
        }
    }
    pub(super) fn integer(&self, doc: &Document, name: &str) -> i64 {
        match doc.node(self.field(name)) {
            Node::Scalar(CanonValue::Int(value)) => *value,
            _ => unreachable!("validated pointer integer"),
        }
    }
    pub(super) fn matches(&self, doc: &Document, other: &Self, other_doc: &Document) -> bool {
        self.fields
            .iter()
            .zip(other.fields)
            .all(|(&left, right)| equal_atom(doc.node(left), other_doc.node(right)))
    }
}
pub(super) fn equal_atom(left: &Node, right: &Node) -> bool {
    match (left, right) {
        (Node::String(left), Node::String(right)) => left == right,
        (Node::Scalar(CanonValue::Null), Node::Scalar(CanonValue::Null)) => true,
        (Node::Scalar(CanonValue::Int(left)), Node::Scalar(CanonValue::Int(right))) => {
            left == right
        }
        _ => false,
    }
}

pub(super) fn validate_pointer<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    doc: &mut Document,
    id: usize,
) -> Result<Pointer> {
    const FIELDS: &[&str] = &[
        "schema",
        "profile_id",
        "profile_revision",
        "activation_id",
        "plan_digest",
        "lock_digest",
        "activation_snapshot_path",
        "activation_snapshot_digest",
        "catalog_revision",
        "generation",
        "updated_at",
        "pointer_digest",
    ];
    if !exact(doc, id, FIELDS) {
        let noniterable = match doc.node(id) {
            Node::Scalar(CanonValue::Int(_) | CanonValue::BigInt(_)) => Some("int"),
            Node::Scalar(CanonValue::Bool(_)) => Some("bool"),
            Node::Scalar(CanonValue::Float(_)) => Some("float"),
            _ => None,
        };
        if let Some(kind) = noniterable {
            return Err(raw(
                "builtins.TypeError",
                format!("'{kind}' object is not iterable"),
            ));
        }
        if let Node::Array(items) = doc.node(id) {
            for &item in items {
                if matches!(doc.node(item), Node::Array(_) | Node::Object(_)) {
                    return Err(raw(
                        "builtins.TypeError",
                        format!("unhashable type: '{}'", kind(doc.node(item))),
                    ));
                }
            }
            let set_matches = FIELDS
                .iter()
                .all(|field| items.iter().any(|&item| doc.string_is(item, field)))
                && items
                    .iter()
                    .all(|&item| FIELDS.iter().any(|field| doc.string_is(item, field)));
            if set_matches {
                return Err(raw(
                    "builtins.AttributeError",
                    "'list' object has no attribute 'get'",
                ));
            }
        }
        return Err(pointer_error(
            "active Profile pointer has unknown or missing fields",
        ));
    }
    if !doc
        .get(id, "schema")
        .is_some_and(|id| doc.string_is(id, "io.tobkiri.active-profile-pointer.v1"))
    {
        return Err(pointer_error(
            "active Profile pointer schema is unsupported",
        ));
    }
    let digest_id = doc.get(id, "pointer_digest").expect("exact pointer");
    let digest = match doc.node(digest_id) {
        Node::String(text) => text.utf8()?,
        _ => return Err(pointer_error("active Profile pointer digest is invalid")),
    };
    // Temporarily remove only the actual pointer_digest edge; all values stay
    // in the same arena. Restoration precedes any fallible digest comparison.
    let Node::Object(fields) = &mut doc.nodes[id] else {
        unreachable!("exact pointer")
    };
    let position = fields
        .iter()
        .position(|(key, _)| key.is("pointer_digest"))
        .expect("pointer digest");
    let removed = fields.remove(position);
    let original = doc.root;
    doc.root = id;
    let canonical = json::canonical(doc, None, op);
    doc.root = original;
    let Node::Object(fields) = &mut doc.nodes[id] else {
        unreachable!("exact pointer")
    };
    fields.insert(position, removed);
    if Digest::hash(&canonical?).as_str() != digest {
        return Err(pointer_error("active Profile pointer digest is invalid"));
    }
    for field in [
        "profile_id",
        "profile_revision",
        "plan_digest",
        "lock_digest",
        "activation_snapshot_digest",
        "catalog_revision",
    ] {
        let value = doc.get(id, field).expect("exact pointer");
        if field == "catalog_revision" && matches!(doc.node(value), Node::Scalar(CanonValue::Null))
        {
            continue;
        }
        let text = match doc.node(value) {
            Node::String(text) => text.utf8()?,
            _ => {
                return Err(caused(
                    pointer_error(format!(
                        "{field} must be {}",
                        if field == "profile_id" {
                            "a non-empty string"
                        } else {
                            "a lowercase sha256 digest"
                        }
                    )),
                    raw(
                        "tobkiri_protocol.errors.ProtocolError",
                        format!(
                            "{field} must be {}",
                            if field == "profile_id" {
                                "a non-empty string"
                            } else {
                                "a lowercase sha256 digest"
                            }
                        ),
                    ),
                ));
            }
        };
        if field == "profile_id" {
            if !text.is_ascii() {
                return Err(OriginalAuthorityError::unsupported(
                    OriginalAuthorityStage::Source,
                    "Unicode predecessor identifier validation",
                ));
            }
            let message = if text.is_empty() {
                Some("profile_id must be a non-empty string".into())
            } else if text.len() > 128 {
                Some("profile_id exceeds 128 characters".into())
            } else if text.bytes().any(|byte| byte.is_ascii_uppercase())
                || ["rumi.", "rumiai.", "viewer.", "legacy."]
                    .iter()
                    .any(|prefix| text.starts_with(prefix))
            {
                Some(format!(
                    "profile_id is not a canonical v4 identifier: {}",
                    json::repr_text(&Text::from_str(&text))
                ))
            } else if !canonical_ascii_id(&text) {
                Some(format!(
                    "profile_id has invalid canonical ID syntax: {}",
                    json::repr_text(&Text::from_str(&text))
                ))
            } else {
                None
            };
            if let Some(message) = message {
                return Err(caused(
                    pointer_error(&message),
                    raw("tobkiri_protocol.errors.ProtocolError", message),
                ));
            }
        } else if Digest::parse(&text).is_err() {
            let message = format!("{field} must be a lowercase sha256 digest");
            return Err(caused(
                pointer_error(&message),
                raw("tobkiri_protocol.errors.ProtocolError", message),
            ));
        }
    }
    let text = |field| -> Result<String> {
        match doc.node(doc.get(id, field).expect("exact pointer")) {
            Node::String(text) => text.utf8(),
            _ => Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Source,
                "predecessor field type projection",
            )),
        }
    };
    let activation_node = doc.node(doc.get(id, "activation_id").expect("exact pointer"));
    if !matches!(activation_node, Node::String(_)) {
        return Err(caused(
            pointer_error("active Profile pointer fields have invalid types"),
            raw(
                "builtins.TypeError",
                format!(
                    "expected string or bytes-like object, got '{}'",
                    kind(activation_node)
                ),
            ),
        ));
    }
    let activation = text("activation_id")?;
    let suffix = activation.strip_prefix("activation:").unwrap_or("");
    if !(8..=128).contains(&suffix.len())
        || !suffix
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphanumeric)
        || !suffix.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte)
        })
    {
        return Err(pointer_error("activation_id is not canonical"));
    }
    let path_node = doc.node(
        doc.get(id, "activation_snapshot_path")
            .expect("exact pointer"),
    );
    if !matches!(path_node, Node::String(_)) {
        return Err(pointer_error(
            "activation snapshot path must be relative and traversal-free",
        ));
    }
    let path = text("activation_snapshot_path")?;
    let candidate = Path::new(&path);
    if path.is_empty()
        || candidate.is_absolute()
        || candidate
            .components()
            .any(|part| matches!(part, Component::ParentDir))
        || !candidate.starts_with("workspaces")
    {
        return Err(pointer_error(
            "activation snapshot path must be relative and traversal-free",
        ));
    }
    if path
        != format!(
            "workspaces/{}/activation/activations/{suffix}.json",
            text("profile_id")?
        )
    {
        return Err(pointer_error(
            "activation snapshot path is not bound to the active Profile activation",
        ));
    }
    for (field, minimum, message) in [
        ("generation", 1, "generation must be a positive integer"),
        ("updated_at", 0, "updated_at must be a non-negative integer"),
    ] {
        if !matches!(doc.node(doc.get(id, field).expect("exact pointer")), Node::Scalar(CanonValue::Int(value)) if *value >= minimum)
        {
            return Err(pointer_error(message));
        }
    }
    Ok(Pointer {
        fields: POINTER_FIELDS.map(|name| doc.get(id, name).expect("exact validated pointer")),
    })
}
