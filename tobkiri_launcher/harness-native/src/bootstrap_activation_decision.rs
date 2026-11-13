//! Original application request decisions over one body and the live listing.
use super::*;
use crate::BootstrapReviewFailure;

/// The original Pack's DATA outcome. Matching a confirmation confers no
/// write authority. Activation must independently validate its trusted input.
#[derive(Debug)]
pub enum BootstrapActivationDecision {
    Rejected(CoreBuffer),
    Matched(CoreBuffer),
}

pub(super) enum Decision {
    Continue,
    Rejected(CoreBuffer),
    Matched(CoreBuffer),
}

pub(super) async fn run<S: BootstrapInputOwner>(
    pack: &mut ReviewPackBorrow<'_, S>,
    body: Option<&CoreValue>,
    listing: Option<&CoreBuffer>,
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
    let listing = listing
        .map(|listing| CoreValue::parse_guest(listing.as_bytes(), pack.input_budget))
        .transpose()?;
    Ok(Ok(admit(
        &packet,
        &scope,
        response_format,
        listing.as_ref(),
        pack.input_budget,
    )?))
}

fn admit(
    packet: &CoreValue,
    scope: &CoreValue,
    response_format: &str,
    listing: Option<&CoreValue>,
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
            if listing.is_none() && matches!(value.inspect(), tobkiri_canon::CoreRef::Null) =>
        {
            Ok(Decision::Continue)
        }
        Some("rejected") if value.object().is_some() => {
            Ok(Decision::Rejected(value.encode_guest(budget)?))
        }
        Some("matched") if value.object().is_some() => {
            let listing = listing.context("activation match preceded Setup listing")?;
            let profile = denied::field(listing, "recommended_default_profile")?;
            if !value.eq_guest(&denied::field(&profile, "confirmation")?, budget)? {
                bail!("activation decision changed its reviewed confirmation");
            }
            Ok(Decision::Matched(value.encode_guest(budget)?))
        }
        _ => bail!("activation decision outcome differs"),
    }
}
