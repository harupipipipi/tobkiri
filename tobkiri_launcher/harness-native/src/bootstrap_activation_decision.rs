//! Original application request decisions over one body and the live listing.
use super::*;
use crate::BootstrapReviewFailure;

/// The original Pack's DATA outcome. Matching a confirmation confers no
/// write authority. Activation must independently validate its trusted input.
#[derive(Debug)]
pub enum BootstrapActivationDecision {
    Rejected(CoreBuffer),
    Matched(BootstrapActivationCandidate),
}

/// Inputs from the exact same-Store candidate accepted by the original Pack.
#[derive(Debug)]
pub struct BootstrapActivationCandidate {
    confirmation: CoreValue,
    source_profile: CoreValue,
    bundle_digest: tobkiri_canon::Digest,
    lock_digest: tobkiri_canon::Digest,
    definition_digest: tobkiri_canon::Digest,
}

impl BootstrapActivationCandidate {
    pub fn confirmation(&self) -> &CoreValue {
        &self.confirmation
    }

    pub fn source_profile(&self) -> &CoreValue {
        &self.source_profile
    }

    pub fn bundle_digest(&self) -> &tobkiri_canon::Digest {
        &self.bundle_digest
    }

    /// Digest of the resolved Profile Lock, separate from the bundle lock.
    pub fn lock_digest(&self) -> &tobkiri_canon::Digest {
        &self.lock_digest
    }

    pub fn definition_digest(&self) -> &tobkiri_canon::Digest {
        &self.definition_digest
    }

    #[cfg(test)]
    pub(crate) fn fixture(
        confirmation: CoreValue,
        source_profile: CoreValue,
        bundle_digest: tobkiri_canon::Digest,
        resolved_digests: [tobkiri_canon::Digest; 3],
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self> {
        let [definition, plan, lock] = resolved_digests;
        let identity = ResolvedProfileIdentity {
            definition,
            plan,
            lock,
        };
        let value = CoreValue::object_shared(
            [
                ("confirmation", confirmation.share()),
                ("source_profile", source_profile),
                (
                    "profile_definition_digest",
                    CoreValue::string(identity.definition.as_str(), budget)?,
                ),
                (
                    "bundle_digest",
                    CoreValue::string(bundle_digest.as_str(), budget)?,
                ),
                (
                    "plan_digest",
                    CoreValue::string(identity.plan.as_str(), budget)?,
                ),
            ]
            .into_iter()
            .map(Ok),
            budget,
        )?;
        admit_candidate(
            &value,
            Reviewed {
                confirmation: &confirmation,
                bundle_digest: &bundle_digest,
                identity: &identity,
            },
            budget,
        )
    }
}

pub(super) enum Decision {
    Continue,
    Rejected(CoreBuffer),
    Matched(BootstrapActivationCandidate),
}

pub(super) struct Reviewed<'a> {
    pub(super) confirmation: &'a CoreValue,
    pub(super) bundle_digest: &'a tobkiri_canon::Digest,
    pub(super) identity: &'a ResolvedProfileIdentity,
}

pub(super) async fn run<S: BootstrapInputOwner>(
    pack: &mut ReviewPackBorrow<'_, S>,
    body: Option<&CoreValue>,
    review: Option<Reviewed<'_>>,
) -> Result<std::result::Result<Decision, BootstrapReviewFailure>> {
    let scope = CoreValue::parse_guest(pack.call.as_bytes(), pack.input_budget)?
        .get_key("scope")
        .context("activation decision scope missing")?;
    let (request, origin, response_format) = match body {
        Some(body) => (
            denied::frame(
                pack,
                "io.tobkiri.native-bootstrap-activation-preflight.v1",
                [("body", body.share())],
            )?,
            "bootstrap-activation-preflight",
            "io.tobkiri.native-bootstrap-activation-preflight-result.v1",
        ),
        None => (
            denied::frame(pack, "io.tobkiri.native-bootstrap-activation-match.v1", [])?,
            "bootstrap-activation-match",
            "io.tobkiri.native-bootstrap-activation-match-result.v1",
        ),
    };
    let response = match pack.observe_fixed_projection(&request, origin).await? {
        Ok(response) => response,
        Err(BootstrapIdentityOutcome::GetterRaised(error)) => {
            return Ok(Err(BootstrapReviewFailure::GetterRaised(error)));
        }
        Err(BootstrapIdentityOutcome::Refused(error)) => {
            return Ok(Err(BootstrapReviewFailure::ObservationRefused(error)));
        }
        Err(BootstrapIdentityOutcome::Returned(_)) => {
            bail!("activation decision did not return a projection");
        }
    };
    let packet = CoreValue::parse_guest(response.as_bytes(), pack.input_budget)?;
    Ok(Ok(admit(
        &packet,
        &scope,
        response_format,
        review,
        pack.input_budget,
    )?))
}

fn admit(
    packet: &CoreValue,
    scope: &CoreValue,
    response_format: &str,
    review: Option<Reviewed<'_>>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Decision> {
    denied::exact(packet, &["format", "scope", "outcome", "value"])?;
    if denied::field(packet, "format")?.as_str() != Some(response_format)
        || !denied::same_scope(&denied::field(packet, "scope")?, scope)
    {
        bail!("activation decision format or scope differs");
    }
    let value = denied::field(packet, "value")?;
    match denied::field(packet, "outcome")?.as_str() {
        Some("continue")
            if review.is_none() && matches!(value.inspect(), tobkiri_canon::CoreRef::Null) =>
        {
            Ok(Decision::Continue)
        }
        Some("rejected") if value.object().is_some() => {
            Ok(Decision::Rejected(value.encode_guest(budget)?))
        }
        Some("matched") if value.object().is_some() => {
            let review = review.context("activation match preceded Setup listing")?;
            admit_candidate(&value, review, budget).map(Decision::Matched)
        }
        _ => bail!("activation decision outcome differs"),
    }
}

fn admit_candidate(
    value: &CoreValue,
    review: Reviewed<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<BootstrapActivationCandidate> {
    denied::exact(
        value,
        &[
            "confirmation",
            "source_profile",
            "profile_definition_digest",
            "bundle_digest",
            "plan_digest",
        ],
    )?;
    let confirmation = denied::field(value, "confirmation")?;
    if !confirmation.eq_guest(review.confirmation, budget)? {
        bail!("activation decision changed its reviewed confirmation");
    }
    let source_profile = denied::field(value, "source_profile")?;
    let source_id = denied::field(&source_profile, "profile_id")?;
    let reviewed_id = denied::field(&confirmation, "profile_id")?;
    let plan = denied::field(&confirmation, "plan_digest")?;
    let plan = plan
        .as_str()
        .context("reviewed activation plan digest missing")?;
    let source_digest = source_profile.digest_guest(budget)?;
    if source_id.as_str().is_none()
        || source_id.as_str() != reviewed_id.as_str()
        || source_digest != review.identity.definition
        || plan != review.identity.plan.as_str()
        || denied::field(value, "profile_definition_digest")?.as_str()
            != Some(source_digest.as_str())
        || denied::field(value, "bundle_digest")?.as_str() != Some(review.bundle_digest.as_str())
        || denied::field(value, "plan_digest")?.as_str() != Some(plan)
    {
        bail!("activation candidate differs from the reviewed Profile or bundle");
    }
    Ok(BootstrapActivationCandidate {
        confirmation,
        source_profile,
        bundle_digest: review.bundle_digest.clone(),
        lock_digest: review.identity.lock.clone(),
        definition_digest: source_digest,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tobkiri_canon::{AllocationError, AllocationRequest, Digest};

    struct TestBudget;
    impl AllocationBudget for TestBudget {
        fn reserve(&self, _: AllocationRequest) -> Result<(), AllocationError> {
            Ok(())
        }
        fn release(&self, _: u64) {}
    }

    #[test]
    fn matched_activation_requires_the_reviewed_source_bundle_and_confirmation() -> Result<()> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let bundle = Digest::hash(b"original lock bytes");
        let plan = Digest::hash(b"plan");
        let source = json!({"profile_id":"defaults", "name":"Harness"});
        let source_digest = CoreValue::parse_guest(&serde_json::to_vec(&source)?, &budget)?
            .digest_guest(&budget)?;
        let confirmation = json!({"profile_id":"defaults","plan_digest":plan.as_str()});
        let expected_confirmation =
            CoreValue::parse_guest(&serde_json::to_vec(&confirmation)?, &budget)?;
        let packet = json!({
            "confirmation": confirmation, "source_profile": source,
            "profile_definition_digest": source_digest.as_str(), "bundle_digest": bundle.as_str(),
            "plan_digest": plan.as_str(),
        });
        let identity = ResolvedProfileIdentity {
            definition: source_digest.clone(),
            plan,
            lock: Digest::hash(b"resolved profile lock"),
        };
        let parse = |value: &serde_json::Value| -> Result<BootstrapActivationCandidate> {
            let value = CoreValue::parse_guest(&serde_json::to_vec(value)?, &budget)?;
            admit_candidate(
                &value,
                Reviewed {
                    confirmation: &expected_confirmation,
                    bundle_digest: &bundle,
                    identity: &identity,
                },
                &budget,
            )
        };
        let matched = parse(&packet)?;
        assert_eq!(matched.bundle_digest(), &bundle);
        assert_eq!(matched.lock_digest(), &identity.lock);
        assert_eq!(
            matched.source_profile().digest_guest(&budget)?,
            source_digest
        );
        for (path, replacement) in [
            ("/source_profile/name", json!("changed source")),
            (
                "/bundle_digest",
                json!(Digest::hash(b"replaced lock").as_str()),
            ),
            (
                "/confirmation/plan_digest",
                json!(Digest::hash(b"changed plan").as_str()),
            ),
            (
                "/plan_digest",
                json!(Digest::hash(b"changed plan").as_str()),
            ),
        ] {
            let mut changed = packet.clone();
            *changed.pointer_mut(path).unwrap() = replacement;
            assert!(parse(&changed).is_err(), "{path}");
        }
        let mut changed = packet;
        changed["source_profile"]["name"] = json!("changed and redigested source");
        let different =
            CoreValue::parse_guest(&serde_json::to_vec(&changed["source_profile"])?, &budget)?
                .digest_guest(&budget)?;
        changed["profile_definition_digest"] = json!(different.as_str());
        assert!(parse(&changed).is_err());
        Ok(())
    }
}
