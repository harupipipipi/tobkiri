//! Repair offers for a rejected Profile change (issue #1520 R5.3).
//!
//! A conflict never changes the stored configuration. The offer records the
//! exact original Profile revision and Pack hashes that a generated repair was
//! written against. Applying a repair re-checks that binding, never edits an
//! original Pack, never widens a permission and then uses the ordinary save
//! path, so every approval is still obtained from the trusted Host. Nothing in
//! this module approves anything.
use crate::{Core, CoreError};
use tobkiri_canon::{Digest, ids::ProfileId};
use tobkiri_profile::{ConflictReport, PackPin, Profile};

/// The exact original state a repair is valid for.
#[derive(Clone, Debug)]
pub struct RepairBinding {
    pub profile: ProfileId,
    /// Stored Profile revision at the time the conflict was reported.
    pub head: Digest,
    /// Original Pack pins, including their artifact hashes, at that time.
    pub packs: Vec<PackPin>,
}

/// A conflict report together with the state a repair must be bound to.
#[derive(Clone, Debug)]
pub struct RepairOffer {
    pub binding: RepairBinding,
    pub report: ConflictReport,
}

fn stale() -> CoreError {
    CoreError::Invalid(
        "repair is stale: the original Profile or Pack changed; regenerate it".into(),
    )
}

fn same_pin(left: &PackPin, right: &PackPin) -> bool {
    left.id == right.id && left.version == right.version && left.sha256 == right.sha256
}

impl Core {
    /// Offer a repair for a rejected candidate. `None` means the candidate has
    /// no conflicts. The stored Profile is only read, never changed.
    pub fn offer_repair(
        &self,
        id: &ProfileId,
        rejected: &Profile,
    ) -> Result<Option<RepairOffer>, CoreError> {
        let (original, head) = self.profile(id)?;
        let report = self.check_profile(rejected)?;
        if !report.repair_pack_available {
            return Ok(None);
        }
        Ok(Some(RepairOffer {
            binding: RepairBinding {
                profile: id.clone(),
                head,
                packs: original.packs,
            },
            report,
        }))
    }

    /// Save a generated repair against the exact state it was bound to.
    ///
    /// The repair is rejected as stale when the stored head or any original
    /// Pack hash differs from the binding. It must keep every original Pack
    /// pin unchanged and may not widen the Profile ceiling, the User policy or
    /// any Pack limit. Every permission it needs is still prompted through the
    /// trusted Host by the ordinary save.
    pub async fn apply_repair(
        &self,
        binding: &RepairBinding,
        repaired: Profile,
    ) -> Result<Digest, CoreError> {
        let (current, head) = self.profile(&binding.profile)?;
        if head != binding.head
            || current.packs.len() != binding.packs.len()
            || !binding
                .packs
                .iter()
                .all(|pin| current.packs.iter().any(|now| same_pin(now, pin)))
        {
            return Err(stale());
        }
        if repaired.id != binding.profile {
            return Err(CoreError::Invalid(
                "repair targets a different Profile".into(),
            ));
        }
        if !binding
            .packs
            .iter()
            .all(|pin| repaired.packs.iter().any(|new| same_pin(new, pin)))
        {
            return Err(CoreError::Invalid(
                "repair cannot modify or remove an original Pack".into(),
            ));
        }
        let user_now = current.user_permissions.clone().unwrap_or_default();
        let user_new = repaired.user_permissions.clone().unwrap_or_default();
        let limits_fit = repaired.pack_limits.iter().all(|(pack, limits)| {
            limits.is_subset_of(current.pack_limits.get(pack).unwrap_or(&current.ceiling))
        });
        if !repaired.ceiling.is_subset_of(&current.ceiling)
            || !user_new.is_subset_of(&user_now)
            || !limits_fit
        {
            return Err(CoreError::Invalid(
                "repair cannot widen the Profile ceiling, User policy or Pack limits".into(),
            ));
        }
        self.save_profile(repaired, Some(head)).await
    }
}
