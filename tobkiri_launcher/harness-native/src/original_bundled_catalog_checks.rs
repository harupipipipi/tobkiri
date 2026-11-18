//! Checks after strict schema validation of bundled catalog documents.
use super::*;
use tobkiri_canon::{AllocationBudget, AllocationKind, AllocationRequest, MAX_BYTES};

/// Check a Base or Shell revision against the document with that field omitted.
pub(super) fn definition_revision_matches<S: OriginalAuthorityOwner>(
    doc: &Document,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    let revision = digest_without(doc, "definition_revision", op, budget)?;
    Ok(doc
        .get(doc.root, "definition_revision")
        .is_some_and(|id| doc.string_is(id, revision.as_str())))
}

/// A failed executable catalog check before the independent physical reread.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum ExecutableCatalogIssue {
    MissingPackManifest,
    StaleSourceIdentity,
    StaleCatalogDigest,
    MissingOrDuplicateArtifactPin,
    InvalidArtifactPinDigest,
}

/// Return the manifest pin text for the executable's Pack, selected by its ID.
/// Preserve the text verbatim for comparison after the physical reread.
pub(super) fn executable_pin<'a, S: OriginalAuthorityOwner>(
    executable: &Document,
    manifest: Option<&'a Document>,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<std::result::Result<&'a Text, ExecutableCatalogIssue>> {
    use ExecutableCatalogIssue::*;

    op.check(OriginalAuthorityStage::Source)?;
    let Some(manifest) = manifest else {
        return Ok(Err(MissingPackManifest));
    };
    let executable_source = executable.get(executable.root, "source_identity");
    let manifest_source = manifest
        .get(manifest.root, "integrity")
        .and_then(|integrity| manifest.get(integrity, "source_identity"));
    let (Some(Node::String(executable_source)), Some(Node::String(manifest_source))) = (
        executable_source.map(|id| executable.node(id)),
        manifest_source.map(|id| manifest.node(id)),
    ) else {
        return Ok(Err(StaleSourceIdentity));
    };
    charge(op, budget, executable_source.0.len() as u64 + 1)?;
    if executable_source != manifest_source {
        return Ok(Err(StaleSourceIdentity));
    }

    let catalog_digest = digest_without(executable, "catalog_digest", op, budget)?;
    if executable
        .get(executable.root, "catalog_digest")
        .is_none_or(|id| !executable.string_is(id, catalog_digest.as_str()))
    {
        return Ok(Err(StaleCatalogDigest));
    }

    let Some(Node::Array(artifacts)) = manifest
        .get(manifest.root, "artifacts")
        .map(|id| manifest.node(id))
    else {
        return Ok(Err(MissingOrDuplicateArtifactPin));
    };
    let mut pin = None;
    for &artifact in artifacts {
        charge(op, budget, 32)?;
        let Some(Node::String(path)) = manifest.get(artifact, "path").map(|id| manifest.node(id))
        else {
            return Ok(Err(MissingOrDuplicateArtifactPin));
        };
        if path.is("executables.v4.json") && pin.replace(artifact).is_some() {
            return Ok(Err(MissingOrDuplicateArtifactPin));
        }
    }
    let Some(pin) = pin else {
        return Ok(Err(MissingOrDuplicateArtifactPin));
    };
    match manifest.get(pin, "digest").map(|id| manifest.node(id)) {
        Some(Node::String(digest)) => Ok(Ok(digest)),
        _ => Ok(Err(InvalidArtifactPinDigest)),
    }
}

fn digest_without<S: OriginalAuthorityOwner>(
    doc: &Document,
    field: &str,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Digest> {
    let bytes = json::canonical_subtree(doc, doc.root, Some(field), op, budget)?;
    if bytes.len() > MAX_BYTES {
        return Err(json::strict_error("canonical JSON exceeds size limit"));
    }
    charge(op, budget, bytes.len() as u64)?;
    Ok(Digest::hash(&bytes))
}

fn charge<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    work: u64,
) -> Result<()> {
    const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
    op.check(STAGE)?;
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            depth: 0,
            elements: 0,
            work,
        })
        .map_err(|_| OriginalAuthorityError::capacity(STAGE))
}
