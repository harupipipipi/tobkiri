//! The live reservation and SecurityEpoch fence before committed republishing.
use super::*;

const FIELDS: [&str; 7] = [
    "activation_id",
    "profile_id",
    "plan_digest",
    "profile_authority_digest",
    "security_epoch",
    "fencing_token",
    "state",
];

pub(super) struct Binding<'a> {
    pub(super) reservation_id: &'a str,
    pub(super) activation_id: &'a str,
    pub(super) profile_id: &'a str,
    pub(super) fencing_token: i64,
}

pub(super) fn revalidate<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store<'_, '_, S>,
    envelope: &Document,
    binding: Binding<'_>,
) -> Result<()> {
    // The call arguments precede the independent Authority lookup. Access
    // inside those two values starts only after the new lookup completes.
    let plan = subscript(envelope, envelope.root, "plan")?;
    let profile = subscript(envelope, envelope.root, "profile")?;
    let row = incomplete_reservations::read_pending(op, store, binding.reservation_id, FIELDS)?;
    let plan_digest = envelope.node(subscript(envelope, plan, "plan_digest")?);
    let authority_digest = envelope.node(subscript(
        envelope,
        profile,
        "profile_authority_snapshot_digest",
    )?);
    let expected_epoch = envelope.node(subscript(envelope, plan, "security_epoch")?);
    let stale = || {
        raw(
            APP_DENIED,
            "activation reservation is stale immediately before publish",
        )
    };
    let Some(row) = row else {
        return Err(stale());
    };
    let fields = row.each_ref().map(Option::as_ref);
    let [activation, profile, plan, authority, epoch, token, state] = fields;
    if !sql_text_is(activation, binding.activation_id)
        || !sql_text_is(profile, binding.profile_id)
        || !sql_matches(plan, plan_digest)
        || !sql_matches(authority, authority_digest)
        || !sql_matches(epoch, expected_epoch)
        || !sql_int_is(token, binding.fencing_token)
        || !sql_text_is(state, "active")
    {
        return Err(stale());
    }
    // Match the original boolean short-circuit. A stale reservation cannot
    // perform the later epoch query, which uses a different held connection.
    let epoch = incomplete_reservations::read_epoch(op, store)?;
    if !integer_node(expected_epoch).is_some_and(|expected| epoch.decimal().parse() == Ok(expected))
    {
        return Err(stale());
    }
    Ok(())
}

fn sql_matches(value: Option<&Value>, node: &Node) -> bool {
    match node {
        Node::Scalar(CanonValue::Null) => matches!(value, None | Some(Value::Null)),
        Node::String(text) => matches!(value, Some(Value::Text(value)) if text.is(value)),
        _ => integer_node(node).is_some_and(|expected| sql_int_is(value, expected)),
    }
}

fn integer_node(node: &Node) -> Option<i64> {
    match node {
        Node::Scalar(CanonValue::Int(value)) => Some(*value),
        Node::Scalar(CanonValue::Bool(value)) => Some(i64::from(*value)),
        _ => None,
    }
}

fn sql_int_is(value: Option<&Value>, expected: i64) -> bool {
    match value {
        Some(Value::Integer(value)) => *value == expected,
        // Python compares integral floats to integers without rounding the
        // integer first. Handle a real value left by a pre-STRICT schema.
        Some(Value::Real(value)) => {
            *value >= i64::MIN as f64
                && *value < i64::MAX as f64
                && *value == value.trunc()
                && *value as i64 == expected
        }
        _ => false,
    }
}

fn sql_text_is(value: Option<&Value>, expected: &str) -> bool {
    matches!(value, Some(Value::Text(value)) if value == expected)
}
