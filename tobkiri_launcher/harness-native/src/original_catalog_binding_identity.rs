//! Python's duplicate-binding field coercion for strict catalog JSON DATA.
use crate::original_authority::{
    Operation, OriginalAuthorityOwner, Result,
    io::publication::definitions,
    json::{Document, Node},
};
use std::{
    hash::{Hash, Hasher},
    sync::Arc,
};
use tobkiri_canon::{AllocationBudget, CanonValue, CoreBuffer};

pub(super) enum BindingIdentity<'a> {
    Borrowed(&'a [u32]),
    Rendered(CoreBuffer),
}

impl BindingIdentity<'_> {
    pub(super) fn work(&self) -> u64 {
        match self {
            Self::Borrowed(value) => value.len() as u64 + 1,
            Self::Rendered(value) => (value.len() as u64).saturating_mul(2) + 1,
        }
    }
}

impl PartialEq for BindingIdentity<'_> {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Borrowed(a), Self::Borrowed(b)) => a == b,
            (Self::Rendered(a), Self::Rendered(b)) => a.as_bytes() == b.as_bytes(),
            (Self::Borrowed(text), Self::Rendered(rendered))
            | (Self::Rendered(rendered), Self::Borrowed(text)) => {
                codepoints(rendered).eq(text.iter().copied())
            }
        }
    }
}

impl Eq for BindingIdentity<'_> {}

impl Hash for BindingIdentity<'_> {
    fn hash<H: Hasher>(&self, state: &mut H) {
        match self {
            Self::Borrowed(text) => hash_text(text.iter().copied(), state),
            Self::Rendered(text) => hash_text(codepoints(text), state),
        }
    }
}

fn codepoints(text: &CoreBuffer) -> impl Iterator<Item = u32> + '_ {
    text.as_str()
        .expect("funded Python binding identity is UTF-8")
        .chars()
        .map(u32::from)
}

fn hash_text(text: impl Iterator<Item = u32>, state: &mut impl Hasher) {
    for point in text {
        state.write_u32(point);
    }
    // Terminate this part outside the Unicode range to distinguish adjacent fields.
    state.write_u32(0x11_0000);
}

/// Compare a binding field as Python would compare `str(value or "")`.
pub(super) fn binding_identity<'a, S: OriginalAuthorityOwner>(
    doc: &'a Document,
    id: Option<usize>,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<BindingIdentity<'a>> {
    let Some(id) = id else {
        return Ok(BindingIdentity::Borrowed(&[]));
    };
    let identity = match doc.node(id) {
        Node::Scalar(CanonValue::Null | CanonValue::Bool(false) | CanonValue::Int(0)) => {
            BindingIdentity::Borrowed(&[])
        }
        Node::Object(entries) if entries.is_empty() => BindingIdentity::Borrowed(&[]),
        Node::Array(items) if items.is_empty() => BindingIdentity::Borrowed(&[]),
        Node::String(value) => BindingIdentity::Borrowed(&value.0),
        _ => {
            // Strict decoding has already bounded depth and rejected surrogates,
            // floating-point values and integers outside the safe range.
            BindingIdentity::Rendered(definitions::render_python_str(op, budget, doc, id)?)
        }
    };
    Ok(identity)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::{
        io::publication::definitions::tests::{operation, with_source},
        json,
    };

    #[tokio::test(flavor = "current_thread")]
    async fn reproduces_python_truthiness_and_nested_repr() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            // Expected strings are the pinned Python str(value or "") results.
            for (raw, expected) in [
                (r#"null"#, ""),
                (r#"false"#, ""),
                (r#"-0"#, ""),
                (r#""""#, ""),
                (r#"[]"#, ""),
                (r#"{}"#, ""),
                (r#"true"#, "True"),
                (r#"-9007199254740991"#, "-9007199254740991"),
                (r#""\t'\u0085𮯰""#, "\t'\u{85}𮯰"),
                (
                    r#"{"z":[null,false,0,"a'b","\t'\u0085𮯰","\u007f"],"a":{}}"#,
                    r#"{'z': [None, False, 0, "a'b", "\t'\x85𮯰", '\x7f'], 'a': {}}"#,
                ),
            ] {
                let doc = json::decode_read_only_strict(raw.as_bytes(), &op, budget).unwrap();
                let identity = binding_identity(&doc, Some(doc.root), &op, budget).unwrap();
                let expected: Vec<_> = expected.chars().map(u32::from).collect();
                let expected = BindingIdentity::Borrowed(&expected);
                let mut seen = std::collections::HashSet::from([identity]);
                assert!(!seen.insert(expected), "{raw}");
                let missing = binding_identity(&doc, None, &op, budget).unwrap();
                assert!(missing == BindingIdentity::Borrowed(&[]));
            }
        });
    }
}
