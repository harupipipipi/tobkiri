//! Only InvocationLease.from_dict reached by the original v1 upgrade.
//! Input has already passed the legacy digest. No surviving surrogate or
//! nonfinite float reaches this consumer; no JSON is serialized and reparsed.
use super::{
    json::{Document, Node, Text},
    *,
};
use std::{
    cmp::Ordering,
    collections::{BTreeMap, BTreeSet},
};
use tobkiri_canon::{Variant, encode};

const FIELDS: &[&str] = &[
    "lease_id",
    "request_id",
    "caller",
    "target",
    "caller_domain_id",
    "caller_boot_epoch",
    "target_domain_id",
    "target_boot_epoch",
    "request_digest",
    "effect_digest",
    "authorized_scope",
    "resource_namespace",
    "profile_id",
    "activation_id",
    "activation_digest",
    "plan_digest",
    "profile_authority_digest",
    "fencing_token",
    "caller_publisher_lineage",
    "target_publisher_lineage",
    "host_extension_id",
    "provider_authority_id",
    "provider_authority_digest",
    "grant_id",
    "audit_reservation_id",
    "security_epoch",
    "issued_at",
    "expires_at",
    "call_chain",
];

fn invalid(message: impl Into<String>) -> OriginalAuthorityError {
    OriginalAuthorityError {
        stage: OriginalAuthorityStage::Migration,
        class: "core_runtime.authority.v4_models.AuthorityValidationError",
        message: message.into(),
        cause: None,
        context: None,
        suppressed: false,
    }
}
fn raw(class: &'static str, message: impl Into<String>) -> OriginalAuthorityError {
    let mut error = json::raw_error(class, message);
    error.stage = OriginalAuthorityStage::Migration;
    error
}
fn kind(node: &Node) -> &'static str {
    match node {
        Node::Scalar(CanonValue::Null) => "NoneType",
        Node::Scalar(CanonValue::Bool(_)) => "bool",
        Node::Scalar(CanonValue::Int(_) | CanonValue::BigInt(_)) => "int",
        Node::Scalar(CanonValue::Float(_)) => "float",
        Node::String(_) => "str",
        Node::Array(_) => "list",
        Node::Object(_) => "dict",
        Node::Scalar(_) => unreachable!("scalar arena"),
    }
}
fn attribute(node: &Node) -> OriginalAuthorityError {
    raw(
        "builtins.AttributeError",
        format!("'{}' object has no attribute 'get'", kind(node)),
    )
}
fn truth(node: &Node) -> bool {
    match node {
        Node::Scalar(CanonValue::Null) => false,
        Node::Scalar(CanonValue::Bool(v)) => *v,
        Node::Scalar(CanonValue::Int(v)) => *v != 0,
        Node::Scalar(CanonValue::BigInt(v)) => v != "0",
        Node::Scalar(CanonValue::Float(v)) => *v != 0.0,
        Node::String(v) => !v.0.is_empty(),
        Node::Array(v) => !v.is_empty(),
        Node::Object(v) => !v.is_empty(),
        Node::Scalar(_) => unreachable!("scalar arena"),
    }
}
fn scalar(value: &CanonValue) -> String {
    match value {
        CanonValue::Null => "None".into(),
        CanonValue::Bool(true) => "True".into(),
        CanonValue::Bool(false) => "False".into(),
        CanonValue::Int(value) => value.to_string(),
        CanonValue::BigInt(value) => value.clone(),
        CanonValue::Float(_) => {
            String::from_utf8(encode(value, Variant::LegacyFloat).expect("finite scalar"))
                .expect("numeric ASCII")
        }
        _ => unreachable!("scalar arena"),
    }
}
// Container repr cannot satisfy either anchored ID/digest regular expression.
// A container cannot reach a successful constructor through this conversion.
// It nevertheless performs its actual builtin DATA repr in source order;
// malformed models do not introduce recursive Rust calls or recursive Drop.
fn model_text(doc: &Document, id: Option<usize>, empty_false: bool) -> Result<String> {
    let Some(id) = id else {
        return Ok(String::new());
    };
    if empty_false && !truth(doc.node(id)) {
        return Ok(String::new());
    }
    Ok(match doc.node(id) {
        Node::String(value) => value.utf8()?,
        Node::Scalar(value) => scalar(value),
        Node::Array(_) | Node::Object(_) => repr(doc, id)?,
    })
}
fn repr(doc: &Document, id: usize) -> Result<String> {
    enum Part<'a> {
        Node(usize),
        Text(&'a Text),
        Literal(&'static str),
    }
    let mut output = String::new();
    let mut work = vec![Part::Node(id)];
    while let Some(part) = work.pop() {
        match part {
            Part::Literal(value) => output.push_str(value),
            Part::Text(value) => {
                let text = value.utf8()?;
                let quote = if text.contains('\'') && !text.contains('"') {
                    '"'
                } else {
                    '\''
                };
                output.push(quote);
                for ch in text.chars() {
                    match ch {
                        '\\' => output.push_str("\\\\"),
                        '\n' => output.push_str("\\n"),
                        '\r' => output.push_str("\\r"),
                        '\t' => output.push_str("\\t"),
                        ch if ch == quote => {
                            output.push('\\');
                            output.push(ch)
                        }
                        ch if ch.is_control() || (python_space(ch) && ch != ' ') => {
                            use std::fmt::Write;
                            if u32::from(ch) < 256 {
                                let _ = write!(output, "\\x{:02x}", u32::from(ch));
                            } else if u32::from(ch) < 65536 {
                                let _ = write!(output, "\\u{:04x}", u32::from(ch));
                            } else {
                                let _ = write!(output, "\\U{:08x}", u32::from(ch));
                            }
                        }
                        ch => output.push(ch),
                    }
                }
                output.push(quote);
            }
            Part::Node(id) => match doc.node(id) {
                Node::Scalar(value) => output.push_str(&scalar(value)),
                Node::String(text) => work.push(Part::Text(text)),
                Node::Array(values) => {
                    work.push(Part::Literal("]"));
                    for (index, value) in values.iter().enumerate().rev() {
                        work.push(Part::Node(*value));
                        if index != 0 {
                            work.push(Part::Literal(", "))
                        }
                    }
                    work.push(Part::Literal("["));
                }
                Node::Object(values) => {
                    work.push(Part::Literal("}"));
                    for (index, (key, value)) in values.iter().enumerate().rev() {
                        work.push(Part::Node(*value));
                        work.push(Part::Literal(": "));
                        work.push(Part::Text(key));
                        if index != 0 {
                            work.push(Part::Literal(", "))
                        }
                    }
                    work.push(Part::Literal("{"));
                }
            },
        }
    }
    Ok(output)
}
fn require_id(name: &str, value: &str) -> Result<()> {
    if super::schema::valid_id(value) {
        Ok(())
    } else {
        Err(invalid(format!("{name} is invalid")))
    }
}
fn require_digest(name: &str, value: &str) -> Result<()> {
    if super::schema::valid_digest(value) {
        Ok(())
    } else {
        Err(invalid(format!("{name} must be an exact SHA-256 digest")))
    }
}
fn field(doc: &Document, name: &str) -> Result<usize> {
    doc.get(doc.root, name)
        .ok_or_else(|| raw("builtins.KeyError", format!("'{name}'")))
}
fn principal(doc: &Document, id: usize) -> Result<CanonValue> {
    if doc.entries(id).is_none() {
        return Err(attribute(doc.node(id)));
    }
    const NAMES: &[&str] = &[
        "parent_artifact_digest",
        "function_implementation_digest",
        "function_id",
        "contract_revision_digest",
        "operation_id",
    ];
    let mut values = BTreeMap::new();
    for name in NAMES {
        values.insert(
            (*name).into(),
            CanonValue::Str(model_text(doc, doc.get(id, name), true)?),
        );
    }
    for name in [NAMES[0], NAMES[1], NAMES[3]] {
        require_digest(name, values[name].as_str().expect("coerced text"))?;
    }
    for name in [NAMES[2], NAMES[4]] {
        require_id(name, values[name].as_str().expect("coerced text"))?;
    }
    Ok(CanonValue::Obj(values))
}
fn unhashable(node: &Node) -> Option<OriginalAuthorityError> {
    matches!(node, Node::Array(_) | Node::Object(_)).then(|| {
        raw(
            "builtins.TypeError",
            format!("unhashable type: '{}'", kind(node)),
        )
    })
}
fn scope(doc: &Document, id: usize) -> Result<CanonValue> {
    const NAMES: &[&str] = &[
        "capability",
        "semantics_digest",
        "dimensions",
        "quotas",
        "exact_request_digest",
        "opaque",
    ];
    let Some(entries) = doc.entries(id) else {
        // set(value) precedes .get in AuthorityScope.from_dict.
        match doc.node(id) {
            Node::Array(values) => {
                for value in values {
                    if let Some(error) = unhashable(doc.node(*value)) {
                        return Err(error);
                    }
                }
                if values.iter().any(|id| !matches!(doc.node(*id),Node::String(text) if NAMES.iter().any(|name|text.is(name)))) {
                    return Err(invalid("scope contains unknown fields"));
                }
            }
            Node::String(value) if !value.0.is_empty() => {
                return Err(invalid("scope contains unknown fields"));
            }
            Node::String(_) => (),
            node => {
                return Err(raw(
                    "builtins.TypeError",
                    format!("'{}' object is not iterable", kind(node)),
                ));
            }
        }
        return Err(attribute(doc.node(id)));
    };
    if entries
        .iter()
        .any(|(name, _)| !NAMES.iter().any(|key| name.is(key)))
    {
        return Err(invalid("scope contains unknown fields"));
    }
    let dimensions = doc.get(id, "dimensions");
    let quotas = doc.get(id, "quotas");
    let dims = dimensions
        .map(|value| doc.entries(value))
        .unwrap_or(Some(&[]));
    let qs = quotas.map(|value| doc.entries(value)).unwrap_or(Some(&[]));
    let (Some(dims), Some(qs)) = (dims, qs) else {
        return Err(invalid("scope dimensions and quotas must be objects"));
    };
    if dims
        .iter()
        .any(|(_, id)| !matches!(doc.node(*id), Node::Array(_)))
    {
        return Err(invalid("scope dimension values must be arrays"));
    }
    for (_, id) in dims {
        let Node::Array(values) = doc.node(*id) else {
            unreachable!()
        };
        if values
            .iter()
            .any(|id| !matches!(doc.node(*id), Node::String(_)))
        {
            return Err(invalid("scope dimension values must be strings"));
        }
    }
    if qs.iter().any(|(_, id)| !is_int(doc.node(*id))) {
        return Err(invalid("scope quotas must be integers"));
    }
    let capability = model_text(doc, doc.get(id, "capability"), true)?;
    let semantics = model_text(doc, doc.get(id, "semantics_digest"), true)?;
    let exact = doc
        .get(id, "exact_request_digest")
        .filter(|id| !matches!(doc.node(*id), Node::Scalar(CanonValue::Null)))
        .map(|id| model_text(doc, Some(id), false))
        .transpose()?;
    require_id("capability", &capability)?;
    require_digest("semantics_digest", &semantics)?;
    let mut normalized = BTreeMap::new();
    for (name, id) in dims {
        let name = name.utf8()?;
        require_id("scope dimension", &name)?;
        let Node::Array(values) = doc.node(*id) else {
            unreachable!()
        };
        let mut sorted = BTreeSet::new();
        for id in values {
            let Node::String(value) = doc.node(*id) else {
                unreachable!()
            };
            sorted.insert(value.utf8()?);
        }
        if sorted.is_empty() || sorted.contains("") {
            return Err(invalid("scope dimensions cannot be empty"));
        }
        if sorted.len() > 1 && sorted.contains("*") {
            return Err(invalid("scope wildcard cannot mix with values"));
        }
        normalized.insert(
            name,
            CanonValue::Arr(sorted.into_iter().map(CanonValue::Str).collect()),
        );
    }
    let mut normalized_quotas = BTreeMap::new();
    for (name, id) in qs {
        let name = name.utf8()?;
        require_id("scope quota", &name)?;
        if !nonnegative(doc.node(*id)) {
            return Err(invalid("scope quotas must be non-negative integers"));
        }
        normalized_quotas.insert(name, leaf(doc.node(*id))?);
    }
    if let Some(exact) = &exact {
        require_digest("exact_request_digest", exact)?
    }
    let opaque = doc
        .get(id, "opaque")
        .is_some_and(|id| matches!(doc.node(id), Node::Scalar(CanonValue::Bool(true))));
    if opaque && exact.is_none() {
        return Err(invalid("opaque scope requires an exact request digest"));
    }
    Ok(object([
        ("capability", CanonValue::Str(capability)),
        ("semantics_digest", CanonValue::Str(semantics)),
        ("dimensions", CanonValue::Obj(normalized)),
        ("quotas", CanonValue::Obj(normalized_quotas)),
        (
            "exact_request_digest",
            exact.map(CanonValue::Str).unwrap_or(CanonValue::Null),
        ),
        ("opaque", CanonValue::Bool(opaque)),
    ]))
}
fn is_int(node: &Node) -> bool {
    matches!(
        node,
        Node::Scalar(CanonValue::Int(_) | CanonValue::BigInt(_))
    )
}
fn nonnegative(node: &Node) -> bool {
    match node {
        Node::Scalar(CanonValue::Int(v)) => *v >= 0,
        Node::Scalar(CanonValue::BigInt(v)) => !v.starts_with('-'),
        _ => false,
    }
}
fn positive(node: &Node) -> bool {
    nonnegative(node) && !matches!(node, Node::Scalar(CanonValue::Int(0)))
}
fn leaf(node: &Node) -> Result<CanonValue> {
    match node {
        Node::Scalar(value) => Ok(value.clone()),
        Node::String(value) => Ok(CanonValue::Str(value.utf8()?)),
        _ => Err(OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Migration,
            "unvalidated lease container",
        )),
    }
}
fn finite_time(name: &str, node: &Node) -> Result<()> {
    let value = match node {
        Node::Scalar(CanonValue::Int(value)) => *value as f64,
        Node::Scalar(CanonValue::BigInt(value)) => {
            let value = value.parse::<f64>().map_err(|_| {
                raw(
                    "builtins.OverflowError",
                    "int too large to convert to float",
                )
            })?;
            if !value.is_finite() {
                return Err(raw(
                    "builtins.OverflowError",
                    "int too large to convert to float",
                ));
            }
            value
        }
        Node::Scalar(CanonValue::Float(value)) => *value,
        _ => return Err(invalid(format!("{name} must be a finite timestamp"))),
    };
    if !value.is_finite() || value < 0.0 {
        return Err(invalid(format!("{name} must be a finite timestamp")));
    }
    Ok(())
}
fn integer_cmp(one: &str, two: &str) -> Ordering {
    let negative_one = one.starts_with('-');
    let negative_two = two.starts_with('-');
    if negative_one != negative_two {
        return negative_two.cmp(&negative_one);
    }
    let a = one.trim_start_matches('-');
    let b = two.trim_start_matches('-');
    let result = a.len().cmp(&b.len()).then_with(|| a.cmp(b));
    if negative_one {
        result.reverse()
    } else {
        result
    }
}
fn numeric_cmp(one: &CanonValue, two: &CanonValue) -> Ordering {
    match (one, two) {
        (CanonValue::Float(a), CanonValue::Float(b)) => a.partial_cmp(b).expect("finite times"),
        (CanonValue::Float(a), b) => numeric_cmp(b, &CanonValue::Float(*a)).reverse(),
        (a, CanonValue::Float(b)) => {
            let a = scalar(a);
            let floor = if b.floor() == 0.0 {
                "0".into()
            } else {
                format!("{:.0}", b.floor())
            };
            let result = integer_cmp(&a, &floor);
            if result == Ordering::Equal && b.fract() != 0.0 {
                Ordering::Less
            } else {
                result
            }
        }
        (a, b) => integer_cmp(&scalar(a), &scalar(b)),
    }
}
enum ChainItem<'a> {
    Node(usize),
    Text(&'a Text),
    Character(u32),
}
fn chain(doc: &Document) -> Result<Vec<ChainItem<'_>>> {
    let Some(id) = doc.get(doc.root, "call_chain") else {
        return Ok(Vec::new());
    };
    Ok(match doc.node(id) {
        Node::Array(values) => values.iter().copied().map(ChainItem::Node).collect(),
        Node::Object(values) => values.iter().map(|(key, _)| ChainItem::Text(key)).collect(),
        Node::String(value) => value.0.iter().copied().map(ChainItem::Character).collect(),
        node => {
            return Err(raw(
                "builtins.TypeError",
                format!("'{}' object is not iterable", kind(node)),
            ));
        }
    })
}
fn chain_leaf(doc: &Document, value: &ChainItem<'_>) -> Result<CanonValue> {
    match value {
        ChainItem::Node(id) => leaf(doc.node(*id)),
        ChainItem::Text(value) => Ok(CanonValue::Str(value.utf8()?)),
        ChainItem::Character(value) => Ok(CanonValue::Str(
            char::from_u32(*value)
                .expect("canonicalized codepoint")
                .to_string(),
        )),
    }
}
fn equal_scalar(one: &CanonValue, two: &CanonValue) -> bool {
    fn number(value: &CanonValue) -> Option<CanonValue> {
        match value {
            CanonValue::Bool(value) => Some(CanonValue::Int(i64::from(*value))),
            CanonValue::Int(_) | CanonValue::BigInt(_) | CanonValue::Float(_) => {
                Some(value.clone())
            }
            _ => None,
        }
    }
    match (number(one), number(two)) {
        (Some(a), Some(b)) => numeric_cmp(&a, &b) == Ordering::Equal,
        _ => one == two,
    }
}

pub(super) fn normalize<S: OriginalAuthorityOwner>(
    doc: &Document,
    op: &Operation<'_, S>,
) -> Result<Funded<CanonValue>> {
    // BTree nodes, normalized strings, tuple/list intermediates, output clone
    // and scalar conversion copies coexist. Use the same type-derived arena
    // bound plus BTree node overhead, not the old unrelated parser multiplier.
    // Pinned Rust 1.94 alloc/collections/btree/node.rs: at most eleven
    // key/value slots, twelve child pointers, parent/index/length and padding.
    // Prepay a whole such node PER decoded node (including spare slots), plus
    // the arena-derived allowance for repr/string/tuple/scalar temporaries.
    // https://github.com/rust-lang/rust/blob/1.94.0/library/alloc/src/collections/btree/node.rs#L40-L102
    let tree_unit =
        std::mem::size_of::<(String, CanonValue)>() * 12 + std::mem::size_of::<usize>() * 16;
    let bytes =
        doc.storage_bound()
            .checked_add(doc.node_count().checked_mul(tree_unit).ok_or_else(|| {
                OriginalAuthorityError::capacity(OriginalAuthorityStage::Migration)
            })?)
            .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Migration))?;
    let capacity = op.reserve(OriginalAuthorityStage::Migration, bytes)?;
    let caller = principal(doc, field(doc, "caller")?)?;
    let target = principal(doc, field(doc, "target")?)?;
    let authorized_scope = scope(doc, field(doc, "authorized_scope")?)?;
    let chain = chain(doc)?;
    let entries = doc.entries(doc.root).expect("decoded object");
    if let Some((name, _)) = entries
        .iter()
        .find(|(name, _)| !FIELDS.iter().any(|field| name.is(field)))
    {
        return Err(raw(
            "builtins.TypeError",
            format!(
                "InvocationLease.__init__() got an unexpected keyword argument '{}'",
                name.utf8()?
            ),
        ));
    }
    let missing: Vec<_> = FIELDS[..FIELDS.len() - 1]
        .iter()
        .filter(|name| doc.get(doc.root, name).is_none())
        .collect();
    if !missing.is_empty() {
        let mut names: Vec<_> = missing.iter().map(|name| format!("'{name}'")).collect();
        let last = names.pop().expect("missing field");
        let joined = if names.is_empty() {
            last
        } else if names.len() == 1 {
            format!("{} and {last}", names[0])
        } else {
            format!("{}, and {last}", names.join(", "))
        };
        return Err(raw(
            "builtins.TypeError",
            format!(
                "InvocationLease.__init__() missing {} required positional argument{}: {joined}",
                missing.len(),
                if missing.len() == 1 { "" } else { "s" }
            ),
        ));
    }
    for name in [
        "lease_id",
        "request_id",
        "caller_domain_id",
        "target_domain_id",
        "resource_namespace",
        "profile_id",
        "activation_id",
        "caller_publisher_lineage",
        "target_publisher_lineage",
        "host_extension_id",
        "provider_authority_id",
        "grant_id",
        "audit_reservation_id",
    ] {
        require_id(name, &model_text(doc, doc.get(doc.root, name), true)?)?;
    }
    for name in [
        "request_digest",
        "effect_digest",
        "activation_digest",
        "plan_digest",
        "profile_authority_digest",
        "provider_authority_digest",
    ] {
        require_digest(name, &model_text(doc, doc.get(doc.root, name), true)?)?;
    }
    for name in [
        "caller_boot_epoch",
        "target_boot_epoch",
        "security_epoch",
        "fencing_token",
    ] {
        if !positive(doc.node(field(doc, name)?)) {
            return Err(invalid(format!("{name} must be a positive integer")));
        }
    }
    for name in ["issued_at", "expires_at"] {
        finite_time(name, doc.node(field(doc, name)?))?;
    }
    let issued = leaf(doc.node(field(doc, "issued_at")?))?;
    let expires = leaf(doc.node(field(doc, "expires_at")?))?;
    if numeric_cmp(&expires, &issued) != Ordering::Greater {
        return Err(invalid("lease expiry is invalid"));
    }
    if chain.len() > 4 {
        return Err(invalid("lease call chain is cyclic or too deep"));
    }
    let mut normalized_chain = Vec::new();
    let mut duplicate = false;
    for item in &chain {
        if let ChainItem::Node(id) = item
            && let Some(error) = unhashable(doc.node(*id))
        {
            return Err(error);
        }
        let value = chain_leaf(doc, item)?;
        duplicate |= normalized_chain.iter().any(|old| equal_scalar(old, &value));
        normalized_chain.push(value);
    }
    if duplicate {
        return Err(invalid("lease call chain is cyclic or too deep"));
    }
    let mut normalized = BTreeMap::new();
    for (name, id) in entries {
        if ["caller", "target", "authorized_scope", "call_chain"]
            .iter()
            .any(|key| name.is(key))
        {
            continue;
        }
        normalized.insert(name.utf8()?, leaf(doc.node(*id))?);
    }
    normalized.insert("caller".into(), caller);
    normalized.insert("target".into(), target);
    normalized.insert("authorized_scope".into(), authorized_scope);
    normalized.insert("call_chain".into(), CanonValue::Arr(normalized_chain));
    Ok(Funded {
        value: CanonValue::Obj(normalized),
        _capacity: capacity,
    })
}
