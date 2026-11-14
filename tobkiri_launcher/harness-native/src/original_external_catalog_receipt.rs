//! Bind the compiled Pack and actual signed file inventory to the Host receipt.
use super::{charge, compiler, external_catalog_inventory, model};
use crate::original_authority::{
    Funded, Operation, OriginalAuthorityOwner, OriginalAuthorityStage, Result,
    io::TrackedFile,
    json::{self, Document, Node},
};
use sha2::{Digest as _, Sha256};
use std::sync::Arc;
use tobkiri_canon::{AllocationBudget, Digest, MAX_BYTES};
use zeroize::Zeroizing;

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;

pub(super) struct CompiledIdentity<'a> {
    pub(super) definitions: &'a compiler::DirectDefinitions,
    pub(super) artifact_digest: &'a Digest,
    pub(super) host_extension: bool,
}

pub(super) struct Receipt {
    pub(super) host_extension: bool,
    pub(super) _content_digest: Digest,
}

pub(super) fn verify<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    root: &TrackedFile,
    registry: &Document,
    entry: usize,
    compiled: CompiledIdentity<'_>,
    read_signed: impl FnOnce() -> Result<Funded<Zeroizing<Vec<u8>>>>,
) -> Result<Receipt> {
    op.check(STAGE)?;
    let manifest = &compiled.definitions.manifest;
    let pack = manifest.get(manifest.root, "pack").expect("validated Pack");
    let compiled_id = text_field(manifest, pack, "id");
    let receipt_id = text_field(registry, entry, "pack_id");
    charge(op, budget, compiled_id.0.len() + receipt_id.0.len() + 71)?;
    if compiled_id != receipt_id
        || !text_field(registry, entry, "artifact_digest").is(compiled.artifact_digest.as_str())
    {
        return Err(model::denied("external Normal Pack root identity changed"));
    }

    let bytes = read_signed()?;
    let signed = model::decode_host_json(&bytes, op, budget)?;
    drop(bytes);
    if signed.entries(signed.root).is_none() {
        return Err(model::denied(
            "external Normal Pack signature manifest is invalid",
        ));
    }
    external_catalog_inventory::verify(op, root, &signed, budget)?;

    let content_digest = digest_signed_content(op, budget, &signed, compiled.artifact_digest)?;
    if !text_field(registry, entry, "content_digest").is(content_digest.as_str()) {
        return Err(model::denied("external Normal Pack content digest changed"));
    }
    Ok(Receipt {
        host_extension: compiled.host_extension,
        _content_digest: content_digest,
    })
}

fn text_field<'a>(doc: &'a Document, at: usize, name: &str) -> &'a json::Text {
    let field = doc.get(at, name).expect("validated identity field");
    let Node::String(text) = doc.node(field) else {
        unreachable!("validated identity string");
    };
    text
}

fn digest_signed_content<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    signed: &Document,
    artifact_digest: &Digest,
) -> Result<Digest> {
    // The admission envelope sorts artifact_digest before signed_manifest.
    // Its root contributes a level and every byte counts toward the v4 bound.
    const PREFIX: &[u8] = b"{\"artifact_digest\":\"";
    const SEPARATOR: &[u8] = b"\",\"signed_manifest\":";
    const SUFFIX: &[u8] = b"}";

    charge(op, budget, signed.source_bytes())?;
    json::validate_strict_at_depth(signed, op, 1)?;
    let canonical = json::canonical_subtree(signed, signed.root, None, op, budget)?;
    let overhead = PREFIX.len() + artifact_digest.as_str().len() + SEPARATOR.len() + SUFFIX.len();
    if canonical.len() > MAX_BYTES - overhead {
        return Err(json::strict_error("canonical JSON exceeds size limit"));
    }

    let mut hash = Sha256::new();
    charge(op, budget, overhead)?;
    hash.update(PREFIX);
    hash.update(artifact_digest.as_str().as_bytes());
    hash.update(SEPARATOR);
    for chunk in canonical.chunks(64 * 1024) {
        charge(op, budget, chunk.len())?;
        hash.update(chunk);
    }
    hash.update(SUFFIX);
    Ok(Digest::from_sha256(hash.finalize().into()))
}

#[cfg(test)]
mod tests {
    use super::super::super::definitions::tests::{operation, with_source};
    use super::*;

    #[tokio::test(flavor = "current_thread")]
    async fn matches_legacy_manifest_loading_and_counts_the_envelope_depth() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let artifact = Digest::hash(b"artifact");
            let manifest = model::decode_host_json(
                r#"{"z":"日本語","files":[],"a":"replaced","a":"quote\""}"#.as_bytes(),
                &op,
                budget,
            )
            .unwrap();
            let digest = digest_signed_content(&op, budget, &manifest, &artifact).unwrap();
            assert_eq!(
                digest.as_str(),
                "sha256:d443d0b6c6f75926006b5b196b7dd4e89a47b9dd6daf2b64213d03f172d9a5c9"
            );

            let nested = |levels: usize| {
                let source = format!(
                    "{{\"value\":{}0{}}}",
                    "[".repeat(levels),
                    "]".repeat(levels)
                );
                model::decode_host_json(source.as_bytes(), &op, budget).unwrap()
            };
            assert!(digest_signed_content(&op, budget, &nested(62), &artifact).is_ok());
            let error = digest_signed_content(&op, budget, &nested(63), &artifact).unwrap_err();
            assert_eq!(error.message, "json nesting exceeds depth limit");
        });
    }
}
