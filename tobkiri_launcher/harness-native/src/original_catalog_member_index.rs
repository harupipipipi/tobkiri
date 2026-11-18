//! Ordered identities of bundled catalog members with proved documents.
use super::*;
use crate::original_authority::bundled_schema::CatalogDocumentKind;

const MAX_ID_BYTES: usize = 128;
const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;

struct Member {
    kind: CatalogDocumentKind,
    length: usize,
    id: [u8; MAX_ID_BYTES],
}

/// Retain each member at its lock ordinal and reject repeats within a kind.
pub(super) struct CatalogMemberIndex {
    members: Funded<Vec<Member>>,
}

pub(super) fn document_identity(doc: &Document, kind: CatalogDocumentKind) -> Option<&Text> {
    let id = match kind {
        CatalogDocumentKind::Pack => doc
            .get(doc.root, "pack")
            .and_then(|pack| doc.get(pack, "id")),
        CatalogDocumentKind::Base | CatalogDocumentKind::ExecutableCatalog => {
            doc.get(doc.root, "pack_id")
        }
        CatalogDocumentKind::Shell => doc.get(doc.root, "provider_id"),
        CatalogDocumentKind::Profile => doc.get(doc.root, "profile_id"),
    }?;
    match doc.node(id) {
        Node::String(text) => Some(text),
        _ => None,
    }
}

impl CatalogMemberIndex {
    /// Preallocate only the slots in the actual decoded bundle lock.
    pub(super) fn new<S: OriginalAuthorityOwner>(
        op: &Operation<'_, S>,
        entry_count: usize,
    ) -> Result<Self> {
        let bytes = entry_count
            .checked_mul(size_of::<Member>())
            .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?;
        let capacity = op.reserve(STAGE, bytes)?;
        let mut value = Vec::new();
        value
            .try_reserve_exact(entry_count)
            .map_err(|_| OriginalAuthorityError::capacity(STAGE))?;
        if value.capacity() != entry_count {
            return Err(OriginalAuthorityError::capacity(STAGE));
        }
        Ok(Self {
            members: Funded {
                value,
                _capacity: capacity,
            },
        })
    }

    /// Record the next distinct identity from a strict, validated document.
    pub(super) fn insert<S: OriginalAuthorityOwner>(
        &mut self,
        kind: CatalogDocumentKind,
        ordinal: usize,
        doc: &Document,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<bool> {
        op.check(STAGE)?;
        let entries = &mut self.members.value;
        if ordinal != entries.len() || ordinal == entries.capacity() {
            return Ok(false);
        }

        charge_work(budget, MAX_ID_BYTES as u64)?;
        let Some(text) = document_identity(doc, kind) else {
            return Ok(false);
        };
        let length = text.0.len();
        if !(1..=MAX_ID_BYTES).contains(&length) {
            return Ok(false);
        }
        charge_work(budget, length as u64)?;
        let mut id = [0; MAX_ID_BYTES];
        for (byte, &point) in id.iter_mut().zip(&text.0) {
            if point > 127 {
                return Ok(false);
            }
            *byte = point as u8;
        }

        // Charge the greatest extent of each comparison before searching.
        let work = entries
            .len()
            .checked_mul(length + 1)
            .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?;
        charge_work(budget, work as u64)?;
        if entries
            .iter()
            .any(|entry| entry.kind == kind && entry.id[..entry.length] == id[..length])
        {
            return Ok(false);
        }
        op.check(STAGE)?;
        entries.push(Member { kind, length, id });
        Ok(true)
    }

    pub(super) fn rollback_pending(&mut self, ordinal: usize) {
        if self.members.len().checked_sub(1) == Some(ordinal) {
            self.members.value.pop();
        }
    }

    pub(super) fn entries(&self) -> impl Iterator<Item = (CatalogDocumentKind, &str)> {
        self.members.iter().map(|entry| {
            let id = std::str::from_utf8(&entry.id[..entry.length]).expect("stored ASCII identity");
            (entry.kind, id)
        })
    }
}
