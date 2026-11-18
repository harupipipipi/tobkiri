//! The original bundle lock shape and reached entry checks.
use super::*;
use crate::original_authority::bundled_schema::CatalogDocumentKind;
use native_bundled_catalog::{NativeBundleError, NativeBundleResult};

const SCHEMA: &str = "io.tobkiri.defaultspack-bundle-lock.v1";
const MAX_PATH_BYTES: usize = 8192;
const SOURCE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const PATH: OriginalAuthorityStage = OriginalAuthorityStage::Path;

pub(super) struct LockMember {
    pub(super) relative: String,
    pub(super) kind: CatalogDocumentKind,
    pub(super) expected: Digest,
    _capacity: NativeSetupReservation,
}

/// Check the top-level lock fields in the original order.
pub(super) fn lock_entries(doc: &Document) -> NativeBundleResult<&[usize]> {
    let fields = doc.entries(doc.root);
    let schema = doc.get(doc.root, "schema");
    let entries = doc.get(doc.root, "entries");
    let (Some(schema), Some(entries)) = (schema, entries) else {
        return Err(NativeBundleError::Integrity(
            "bundle lock has unknown or missing fields".into(),
        ));
    };
    if fields.is_none_or(|fields| fields.len() != 2) {
        return Err(NativeBundleError::Integrity(
            "bundle lock has unknown or missing fields".into(),
        ));
    }
    if !doc.string_is(schema, SCHEMA) {
        return Err(NativeBundleError::Integrity(
            "bundle lock schema is not supported".into(),
        ));
    }
    match doc.node(entries) {
        Node::Array(entries) if !entries.is_empty() => Ok(entries),
        _ => Err(NativeBundleError::Integrity(
            "bundle lock entries must be a non-empty array".into(),
        )),
    }
}

/// Validate this lock occurrence after all earlier occurrences have passed.
pub(super) fn member_at<S: OriginalAuthorityOwner>(
    doc: &Document,
    entries: &[usize],
    ordinal: usize,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> NativeBundleResult<LockMember> {
    op.check(SOURCE)?;
    charge_work(budget, 64)?;
    let &entry = entries
        .get(ordinal)
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let (Some(fields), Some(path), Some(kind), Some(expected)) = (
        doc.entries(entry),
        doc.get(entry, "path"),
        doc.get(entry, "kind"),
        doc.get(entry, "digest"),
    ) else {
        return Err(invalid_fields(ordinal));
    };
    if fields.len() != 3 {
        return Err(invalid_fields(ordinal));
    }
    let Node::String(path) = doc.node(path) else {
        return Err(invalid_path(ordinal));
    };
    if path.0.is_empty() {
        return Err(invalid_path(ordinal));
    }
    for &prior in &entries[..ordinal] {
        let previous = doc.get(prior, "path").map(|id| doc.node(id));
        let Some(Node::String(previous)) = previous else {
            return Err(OriginalAuthorityError::unavailable().into());
        };
        let extent = if previous.0.len() == path.0.len() {
            path.0.len()
        } else {
            0
        };
        charge_work(budget, extent as u64 + 8)?;
        if previous == path {
            return Err(invalid_path(ordinal));
        }
    }

    charge_work(budget, 96)?;
    let kind = match doc.node(kind) {
        Node::Array(_) => return Err(NativeBundleError::TypeError("unhashable type: 'list'")),
        Node::Object(_) => return Err(NativeBundleError::TypeError("unhashable type: 'dict'")),
        Node::String(kind) if kind.is("pack") => CatalogDocumentKind::Pack,
        Node::String(kind) if kind.is("executable_catalog") => {
            CatalogDocumentKind::ExecutableCatalog
        }
        Node::String(kind) if kind.is("base") => CatalogDocumentKind::Base,
        Node::String(kind) if kind.is("shell") => CatalogDocumentKind::Shell,
        Node::String(kind) if kind.is("profile") => CatalogDocumentKind::Profile,
        _ => {
            return Err(invalid_value(path, "kind", budget)?);
        }
    };
    let Some(expected) = parse_digest(doc.node(expected)) else {
        return Err(invalid_value(path, "digest", budget)?);
    };
    let path_bytes = path_length(path, budget)?;
    let _capacity = op.reserve(PATH, path_bytes)?;
    charge_work(budget, path_bytes as u64)?;
    let mut relative = String::new();
    relative
        .try_reserve_exact(path_bytes)
        .map_err(|_| OriginalAuthorityError::capacity(PATH))?;
    if relative.capacity() != path_bytes {
        return Err(OriginalAuthorityError::capacity(PATH).into());
    }
    relative.extend(path.0.iter().copied().map(|point| {
        char::from_u32(point).expect("strict path checked when calculating its byte length")
    }));
    Ok(LockMember {
        relative,
        kind,
        expected,
        _capacity,
    })
}

fn invalid_value(
    path: &Text,
    field: &'static str,
    budget: &Arc<dyn AllocationBudget>,
) -> NativeBundleResult<NativeBundleError> {
    let capacity = |_| OriginalAuthorityError::capacity(SOURCE);
    let mut output = CoreBuffer::builder(budget, MAX_BYTES);
    for &point in &path.0 {
        let point = char::from_u32(point).ok_or(NativeBundleError::UnrepresentedDocument)?;
        output
            .append(point.encode_utf8(&mut [0; 4]).as_bytes())
            .map_err(capacity)?;
    }
    Ok(NativeBundleError::InvalidLockEntry {
        path: output.finish().map_err(capacity)?,
        field,
    })
}

fn path_length(path: &Text, budget: &Arc<dyn AllocationBudget>) -> NativeBundleResult<usize> {
    if path.0.len() > MAX_PATH_BYTES {
        return Err(OriginalAuthorityError::capacity(PATH).into());
    }
    charge_work(budget, path.0.len() as u64)?;
    let mut length = 0;
    for &point in &path.0 {
        let point = char::from_u32(point).ok_or(NativeBundleError::UnrepresentedDocument)?;
        length += point.len_utf8();
        if length > MAX_PATH_BYTES {
            return Err(OriginalAuthorityError::capacity(PATH).into());
        }
    }
    Ok(length)
}

fn parse_digest(node: &Node) -> Option<Digest> {
    let Node::String(text) = node else {
        return None;
    };
    let mut bytes = [0; 71];
    if text.0.len() != bytes.len() {
        return None;
    }
    for (byte, &point) in bytes.iter_mut().zip(&text.0) {
        *byte = u8::try_from(point).ok()?;
    }
    Digest::parse(std::str::from_utf8(&bytes).ok()?).ok()
}

fn invalid_fields(ordinal: usize) -> NativeBundleError {
    NativeBundleError::Integrity(format!("bundle entry {ordinal} has invalid fields"))
}

fn invalid_path(ordinal: usize) -> NativeBundleError {
    NativeBundleError::Integrity(format!("bundle entry {ordinal} has an invalid path"))
}
