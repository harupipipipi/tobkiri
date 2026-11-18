//! Structural results from the most recent physical, digest-checked bundle read.
use super::*;
use crate::original_authority::bundled_schema::{CatalogDocumentKind, CatalogSchemas};

const SHAPES: [(&str, CatalogDocumentKind); 5] = [
    ("pack", CatalogDocumentKind::Pack),
    ("executable_catalog", CatalogDocumentKind::ExecutableCatalog),
    ("base", CatalogDocumentKind::Base),
    ("shell", CatalogDocumentKind::Shell),
    ("profile", CatalogDocumentKind::Profile),
];

#[derive(Default)]
pub(super) struct DocumentShapes {
    schemas: Option<CatalogSchemas>,
    receipt: Option<DocumentShape>,
}

struct DocumentShape {
    ordinal: usize,
    kind: &'static str,
    digest: Digest,
    matched: bool,
}

impl DocumentShapes {
    pub(super) fn discard_receipt(&mut self) {
        self.receipt = None;
    }

    pub(super) fn finish(&mut self) {
        self.discard_receipt();
        self.schemas = None;
    }
}

impl CatalogIo {
    /// Select the exact experiment before the original loader starts reading.
    #[cfg(test)]
    pub(crate) fn enable_document_shapes(&mut self) {
        self.document_shapes.schemas = Some(CatalogSchemas::default());
    }

    pub(super) fn prepare_document_shape<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        bytes: &[u8],
    ) -> Result<()> {
        if self.document_shapes.schemas.is_none() {
            return Ok(());
        }
        let (lock, entries) = self.entries()?;
        let entry = *entries
            .get(self.entry)
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let Some((kind, schema)) = SHAPES.into_iter().find(|(name, _)| {
            lock.get(entry, "kind")
                .is_some_and(|id| lock.string_is(id, name))
        }) else {
            return Ok(());
        };
        charge_work(&self.budget, bytes.len() as u64)?;
        let digest = Digest::hash(bytes);
        if !lock
            .get(entry, "digest")
            .is_some_and(|id| lock.string_is(id, digest.as_str()))
        {
            // The loader reports the original digest failure before asking
            // for validation. No schema is visited for a mismatching read.
            return Ok(());
        }
        let matched = self
            .document_shapes
            .schemas
            .as_mut()
            .expect("selected document schemas")
            .validate_shape(op, &self.budget, schema, bytes)?;
        self.document_shapes.receipt = Some(DocumentShape {
            ordinal: self.entry,
            kind,
            digest,
            matched,
        });
        Ok(())
    }

    pub(super) fn take_document_shape(
        &mut self,
        args: &tobkiri_canon::CoreArray<'_>,
    ) -> Result<bool> {
        let missing = OriginalAuthorityError::unavailable;
        let receipt = self.document_shapes.receipt.as_ref().ok_or_else(missing)?;
        let ordinal = args
            .get(0)
            .and_then(|value| value.as_i64())
            .and_then(|value| usize::try_from(value).ok());
        if args.len() != 3
            || ordinal != Some(receipt.ordinal)
            || self.entry.checked_sub(1) != ordinal
            || args
                .get(1)
                .is_none_or(|value| value.as_str() != Some(receipt.kind))
            || args
                .get(2)
                .is_none_or(|value| value.as_str() != Some(receipt.digest.as_str()))
        {
            return Err(missing());
        }
        let matched = receipt.matched;
        self.document_shapes.discard_receipt();
        Ok(matched)
    }
}
