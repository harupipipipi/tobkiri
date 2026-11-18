//! Validation of a first physical, digest-checked bundle read.
use super::*;
use crate::original_authority::bundled_schema::CatalogDocumentKind;
pub(super) use crate::original_authority::bundled_schema::CatalogSchemas;

const SHAPES: [(&str, CatalogDocumentKind); 5] = [
    ("pack", CatalogDocumentKind::Pack),
    ("executable_catalog", CatalogDocumentKind::ExecutableCatalog),
    ("base", CatalogDocumentKind::Base),
    ("shell", CatalogDocumentKind::Shell),
    ("profile", CatalogDocumentKind::Profile),
];

impl CatalogIo {
    /// Select the exact experiment before the original loader starts reading.
    #[cfg(test)]
    pub(crate) fn enable_document_shapes(&mut self) {
        self.document_shapes = Some(CatalogSchemas::default());
    }

    pub(super) fn write_checked_document<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        out: &mut CoreBufferBuilder,
        bytes: &[u8],
    ) -> anyhow::Result<bool> {
        const RAW_FRAME: usize = b"{\"kind\":\"bytes\",\"value\":\"\"}}".len();
        // Keep the original byte response's maximum raw input even if JSON
        // escaping this particular document would fit more bytes on the wire.
        if out.len() + RAW_FRAME + 4 * bytes.len().div_ceil(3) > MAX_BYTES {
            return Ok(false);
        }
        let (lock, entries) = self.entries()?;
        let entry = *entries
            .get(self.entry)
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let Some((_, schema)) = SHAPES.into_iter().find(|(name, _)| {
            lock.get(entry, "kind")
                .is_some_and(|id| lock.string_is(id, name))
        }) else {
            return Ok(false);
        };
        charge_work(&self.budget, bytes.len() as u64)?;
        let digest = Digest::hash(bytes);
        if !lock
            .get(entry, "digest")
            .is_some_and(|id| lock.string_is(id, digest.as_str()))
        {
            // The original loader reports the actual and expected digests.
            return Ok(false);
        }
        if !self
            .document_shapes
            .as_mut()
            .expect("selected document schemas")
            .validate_document(op, &self.budget, schema, bytes)?
        {
            // The original validator owns strict, schema and semantic errors.
            return Ok(false);
        }
        let document =
            std::str::from_utf8(bytes).map_err(|_| OriginalAuthorityError::unavailable())?;
        const PREFIX: &[u8] = b"{\"kind\":\"document\",\"value\":";
        // Keep the validated source text opaque to the transport. This
        // preserves its object insertion order and costs one wire scalar.
        // Reserve room for quotes, result and response closing braces.
        charge_work(&self.budget, bytes.len() as u64)?;
        let mut remaining = MAX_BYTES.saturating_sub(out.len() + PREFIX.len() + 4);
        for byte in bytes {
            let width = match byte {
                b'"' | b'\\' => 2,
                0..=31 => 6,
                _ => 1,
            };
            let Some(left) = remaining.checked_sub(width) else {
                // The byte response keeps its existing transport limit.
                return Ok(false);
            };
            remaining = left;
        }
        out.append(PREFIX)?;
        crate::bootstrap_metadata::string_parts(out, [document])?;
        out.append(b"}")?;
        Ok(true)
    }
}
