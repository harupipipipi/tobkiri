//! Grouped inspection of exact bundle lock occurrences.
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

    fn member_path(
        &self,
        index: usize,
        steps: &tobkiri_canon::CoreArray<'_>,
        args: &tobkiri_canon::CoreArray<'_>,
    ) -> Result<String> {
        let unavailable = OriginalAuthorityError::unavailable;
        let ordinal = args.get(0).and_then(|value| value.as_i64());
        if args.len() != 1
            || ordinal.and_then(|i| usize::try_from(i).ok()) != Some(index)
            || steps.len() != 1
        {
            return Err(unavailable());
        }
        let step = steps.get(0).ok_or_else(unavailable)?;
        let pair = step.array().ok_or_else(unavailable)?;
        if pair.len() != 2
            || pair
                .get(0)
                .is_none_or(|value| value.as_str() != Some("join"))
        {
            return Err(unavailable());
        }
        let value = pair.get(1).ok_or_else(unavailable)?;
        let relative = value
            .as_str()
            .filter(|path| path.len() <= 8192)
            .ok_or_else(unavailable)?;
        charge_work(&self.budget, (relative.len() as u64 + 1) * 2)?;
        if self.selected.as_os_str().len() + relative.len() + 1 > 8192 {
            return Err(OriginalAuthorityError::capacity(
                OriginalAuthorityStage::Path,
            ));
        }
        let (lock, entries) = self.entries()?;
        let field = entries
            .get(index)
            .and_then(|&entry| lock.get(entry, "path"));
        if field.is_none_or(|id| !lock.string_is(id, relative)) {
            return Err(unavailable());
        }
        Ok(relative.to_owned())
    }

    pub(super) fn read_checked_entry<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        out: &mut CoreBufferBuilder,
        steps: &tobkiri_canon::CoreArray<'_>,
        args: &tobkiri_canon::CoreArray<'_>,
    ) -> anyhow::Result<()> {
        let relative = self.member_path(self.entry, steps, args)?;
        let lexical = member_path::lexical_member(&self.selected, &relative);
        let candidate = match member_path::resolve_member(op, &self.budget, &lexical) {
            Err(error)
                if error.class == "builtins.ValueError"
                    && error.message == "lstat: embedded null character in path" =>
            {
                return self.reject_member(out, "null-byte");
            }
            result => result?,
        };
        if candidate == self.selected || !candidate.starts_with(&self.selected) {
            return self.reject_member(out, "escape");
        }
        if member_path::member_has_symlink(op, &self.budget, &self.selected, &relative)? {
            return self.reject_member(out, "symlink");
        }
        let bytes = read_file(op, &candidate, &self.budget)?;
        if !self.write_checked_document(op, out, &bytes.value)? {
            write_bytes(out, &bytes.value)?;
        }
        self.retain_pack_pin(op, &bytes.value)?;
        self.retain_shell_input(op, bytes)?;
        self.entry += 1;
        Ok(())
    }

    fn reject_member(&mut self, out: &mut CoreBufferBuilder, reason: &str) -> anyhow::Result<()> {
        out.append(b"{\"kind\":\"catalog-path-rejection\",\"value\":")?;
        crate::bootstrap_metadata::string_parts(out, [reason])?;
        out.append(b"}")?;
        self.loader = Loader::Rejected;
        Ok(())
    }

    pub(super) fn read_checked_pin<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        out: &mut CoreBufferBuilder,
        steps: &tobkiri_canon::CoreArray<'_>,
        args: &tobkiri_canon::CoreArray<'_>,
    ) -> anyhow::Result<()> {
        if self.entry != self.entry_count()? {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let index = self
            .pin_index(self.pins)
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let relative = self.member_path(index, steps, args)?;
        let lexical = member_path::lexical_member(&self.selected, &relative);
        // This is an independent read of the original lexical path. The
        // original caller deliberately does not resolve or walk it again.
        let bytes = read_file(op, &lexical, &self.budget)?;
        if !byte_frame_fits(out, bytes.value.len()) {
            return Err(tobkiri_canon::AllocationError::Limit.into());
        }
        charge_work(&self.budget, bytes.value.len() as u64)?;
        let digest = Digest::hash(&bytes.value);
        out.append(b"{\"kind\":\"catalog-pin-digest\",\"value\":")?;
        crate::bootstrap_metadata::string_parts(out, [digest.as_str()])?;
        out.append(b"}")?;
        self.pins += 1;
        Ok(())
    }

    fn write_checked_document<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        out: &mut CoreBufferBuilder,
        bytes: &[u8],
    ) -> anyhow::Result<bool> {
        // Keep the original byte response's maximum raw input even if JSON
        // escaping this particular document would fit more bytes on the wire.
        if !byte_frame_fits(out, bytes.len()) {
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

fn byte_frame_fits(out: &CoreBufferBuilder, len: usize) -> bool {
    const RAW_FRAME: usize = b"{\"kind\":\"bytes\",\"value\":\"\"}}".len();
    out.len() + RAW_FRAME + 4 * len.div_ceil(3) <= MAX_BYTES
}
