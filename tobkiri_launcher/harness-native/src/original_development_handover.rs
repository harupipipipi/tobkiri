//! Read-only authentication of the original Development Host handover.
use super::*;
use tobkiri_canon::AllocationBudget;

pub(super) const JOURNAL: &str = "development-host-handover.json";
pub(super) const KEY: &str = "development-host-handover.key";
const SCHEMA: &str = "io.tobkiri.development-host-handover-journal.v1";

/// The authenticated payload has no signing key or signature attached.
pub(super) struct Handover {
    pub(super) payload: Funded<Document>,
    pub(super) digest: Digest,
}

/// Read the handover fence without creating any state. Both pre-publication
/// admission and the imported predecessor check need the same witness rules.
pub(super) fn read_optional<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<Handover>> {
    let _paths = op.reserve(
        OriginalAuthorityStage::Path,
        root.as_os_str()
            .len()
            .saturating_mul(4)
            .saturating_add(8192),
    )?;
    use candidate_selection::{predecessor_path_present, predecessor_stat};
    if predecessor_stat(op, root, |path| fs::metadata(path))?.is_none() {
        return Ok(None);
    }
    if predecessor_path_present(op, &root.join(JOURNAL))? {
        return read(op, root, budget).map(Some);
    }
    for witness in [
        root.join(KEY),
        root.join("development-host-staging"),
        root.join("packvm-vz").join(JOURNAL),
    ] {
        if predecessor_path_present(op, &witness)? {
            return Err(value(
                "Development Host handover requires its authenticated recovery journal",
            ));
        }
    }
    Ok(None)
}

fn read<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Handover> {
    let storage = Directory::capture(op, root, false)?;
    if !storage.exists(op, JOURNAL)? {
        return Err(value(
            "Development Host handover journal disappeared during verification",
        ));
    }
    let bytes = storage.read_bounded(op, JOURNAL, 128 * 1024)?;
    let mut payload = json::decode_handover_journal(&bytes, op, budget)?;
    drop(bytes);
    let root = payload.root;
    if !payload
        .get(root, "schema")
        .is_some_and(|schema| payload.string_is(schema, SCHEMA))
    {
        return Err(value("Development Host handover journal is invalid"));
    }
    let Node::Object(fields) = &mut payload.nodes[root] else {
        unreachable!("validated handover object");
    };
    let signature = fields
        .iter()
        .position(|(name, _)| name.is("authentication"))
        .map(|index| fields.remove(index).1);
    let key = storage.read_bounded(op, KEY, 32)?;
    json::validate_strict(&payload, op)?;
    let canonical = json::canonical(&payload, None, op)?;
    let signature = signature.and_then(|id| match payload.node(id) {
        Node::String(text) => Some(text),
        _ => None,
    });
    let mut hex = [b'0'; 64];
    let ascii = signature.is_none_or(|text| text.0.iter().all(|&point| point <= 127));
    let valid_hex = signature.is_some_and(|text| {
        text.0.len() == hex.len()
            && text.0.iter().all(|&point| {
                u8::try_from(point)
                    .is_ok_and(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            })
    });
    if let Some(text) = signature.filter(|_| valid_hex) {
        for (byte, &point) in hex.iter_mut().zip(&text.0) {
            *byte = point as u8;
        }
    }
    // The original computes its HMAC after canonicalization, before the
    // comparison can fail on key length, signature type or non-ASCII text.
    let matched = verify_seal_hex(
        &canonical,
        std::str::from_utf8(&hex).expect("lowercase hex"),
        &key,
    )
    .is_ok();
    if key.len() == 32 && !ascii {
        return Err(raw(
            "builtins.TypeError",
            "comparing strings with non-ASCII characters is not supported",
        ));
    }
    if key.len() != 32 || !valid_hex || !matched {
        return Err(value(
            "Development Host handover journal authentication failed",
        ));
    }
    let digest = Digest::hash(&canonical);
    Ok(Handover { payload, digest })
}
