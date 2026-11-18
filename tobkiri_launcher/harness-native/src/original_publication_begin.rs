//! Prepare and persist the original journal under the confirmed publication lock.
use super::*;
use crate::BootstrapActivationCandidate;
use hmac::{Hmac, Mac};
use sha2::{Digest as _, Sha256};
use tobkiri_canon::{AllocationBudget, AllocationError, CoreBuffer, CoreValue, MAX_BYTES};

const SCHEMA: &str = "io.tobkiri.profile-publication-intent.v1";

fn capacity(_: AllocationError) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}

impl PublicationRead {
    pub(crate) fn prepare_initial_publication<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        cancellation: &CoreCancellation,
        candidate: &BootstrapActivationCandidate,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<CoreValue> {
        self.verify_confirmed(owner, cancellation)?;
        let confirmed = self
            .confirmed
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let approved = confirmed
            .confirmation
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        if !approved
            .eq_guest(candidate.confirmation(), budget)
            .map_err(capacity)?
        {
            return Err(raw(
                crate::bootstrap_review::DENIED,
                "bootstrap activation confirmation is stale or tampered",
            ));
        }
        let op = self.candidate_operation(owner, cancellation)?;
        payload(&op, confirmed.predecessor.as_ref(), candidate, budget)
    }
}

pub(super) fn payload<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    predecessor: Option<&active_profile::LoadedPointer>,
    candidate: &BootstrapActivationCandidate,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let required = |name| {
        candidate
            .confirmation()
            .get_key(name)
            .ok_or_else(|| value("confirmed publication identity is missing"))
    };
    let profile = required("profile_id")?;
    let profile_id = profile
        .as_str()
        .ok_or_else(|| value("confirmed publication Profile is invalid"))?;
    if profile_id.is_empty() || profile_id.len() > 128 {
        return Err(value("confirmed publication Profile is invalid"));
    }
    let plan = required("plan_digest")?;
    let digest = plan
        .as_str()
        .and_then(|text| Digest::parse(text).ok())
        .ok_or_else(|| value("confirmed publication Plan is invalid"))?;
    let mut activation = CoreBuffer::builder(budget, 160);
    for part in ["activation:", profile_id, "-", &digest.as_str()[7..23]] {
        activation.append(part.as_bytes()).map_err(capacity)?;
    }
    let activation = activation.finish().map_err(capacity)?;
    let activation_candidate = CoreValue::object_shared(
        [
            ("profile_id", profile),
            ("profile_revision", required("profile_revision")?),
            (
                "activation_id",
                CoreValue::string_from_utf8(&activation, budget).map_err(capacity)?,
            ),
            ("plan_digest", plan),
            (
                "lock_digest",
                CoreValue::string(candidate.lock_digest().as_str(), budget).map_err(capacity)?,
            ),
            (
                "profile_definition_digest",
                CoreValue::string(candidate.definition_digest().as_str(), budget)
                    .map_err(capacity)?,
            ),
            ("security_epoch", required("security_epoch")?),
        ]
        .into_iter()
        .map(Ok),
        budget,
    )
    .map_err(capacity)?;
    let confirmed = candidate
        .confirmation()
        .digest_guest(budget)
        .map_err(capacity)?;
    let predecessor = match predecessor {
        Some(pointer) => {
            let bytes = json::canonical(&pointer.doc, None, op)?;
            CoreValue::parse_guest(&bytes, budget).map_err(capacity)?
        }
        None => CoreValue::null(budget).map_err(capacity)?,
    };
    CoreValue::object_shared(
        [
            (
                "schema",
                CoreValue::string(SCHEMA, budget).map_err(capacity)?,
            ),
            ("predecessor", predecessor),
            ("candidate", activation_candidate),
            (
                "confirmation_digest",
                CoreValue::string(confirmed.as_str(), budget).map_err(capacity)?,
            ),
        ]
        .into_iter()
        .map(Ok),
        budget,
    )
    .map_err(capacity)
}

/// Durably record the prepared intent while borrowing the existing lock.
pub(super) fn persist_initial_intent<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    scope: &RecoveryScope,
    payload: CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    scope.validate(op)?;
    let directory = &scope.directory;
    if directory.exists(op, JOURNAL)? {
        return Err(value(
            "Profile publication already has an unfinished intent",
        ));
    }
    let key_path = directory.publication_key_path(op)?;
    let mut generated = Zeroizing::new([0_u8; 64]);
    op.check(OriginalAuthorityStage::Key)?;
    // Let the original key loader diagnose an existing or redirected key.
    let existing = if key_path.is_symlink() || key_path.exists() {
        Some(signing_key(op, &key_path)?)
    } else {
        let mut random = Zeroizing::new([0_u8; 32]);
        getrandom::fill(&mut random[..]).map_err(|_| OriginalAuthorityError::unavailable())?;
        let mut digest = Sha256::digest(&random[..]);
        for (pair, &byte) in generated.chunks_exact_mut(2).zip(digest.iter()) {
            let hex = b"0123456789abcdef";
            pair[0] = hex[usize::from(byte >> 4)];
            pair[1] = hex[usize::from(byte & 0xf)];
        }
        zeroize::Zeroize::zeroize(&mut digest[..]);
        directory.create_publication_key(op, &generated[..])?;
        None
    };
    let expected = match &existing {
        Some(existing) => existing.as_slice(),
        None => &generated[..],
    };
    directory.sync_publication_key(op)?;
    let key = signing_key(op, &key_path)?;
    if key.as_slice() != expected {
        return Err(value("Profile publication signing key changed"));
    }
    let intent = encode(op, payload, &key, budget)?;
    directory.create_publication_intent(op, intent.as_bytes())
}

fn encode<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    payload: CoreValue,
    key: &[u8],
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
    let compact = payload.encode_guest(budget).map_err(capacity)?;
    let preimage = python_default_spacing(op, compact.as_bytes())?;
    let mut mac =
        Hmac::<Sha256>::new_from_slice(key).map_err(|_| OriginalAuthorityError::unavailable())?;
    mac.update(&preimage);
    let hex = Digest::from_sha256(mac.finalize().into_bytes().into());
    drop((preimage, compact));
    let envelope = CoreValue::object_shared(
        [
            ("payload", payload),
            (
                "hmac",
                CoreValue::string(&hex.as_str()[7..], budget).map_err(capacity)?,
            ),
        ]
        .into_iter()
        .map(Ok),
        budget,
    )
    .map_err(capacity)?;
    let envelope = envelope.encode_guest(budget).map_err(capacity)?;
    let mut result = CoreBuffer::builder(budget, MAX_BYTES);
    result.append(envelope.as_bytes()).map_err(capacity)?;
    result.append(b"\n").map_err(capacity)?;
    result.finish().map_err(capacity)
}
