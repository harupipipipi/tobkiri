//! The first Host operations after the bundled Pack returns its resolved Profile.
use super::*;
use crate::bootstrap_review::{Review, original_failure};
use crate::{BootstrapReviewFailure, BootstrapReviewOwner, BootstrapReviewReachpoint};
use review_continuation::ReviewCaller;
use tobkiri_canon::Digest;

pub(super) const SOURCE_REQUEST: &str = "io.tobkiri.native-bootstrap-predecessor-source.v1";
const SOURCE_RESPONSE: &str = "io.tobkiri.native-bootstrap-predecessor-source-result.v1";
pub(super) const INACTIVE_REQUEST: &str = "io.tobkiri.native-bootstrap-inactive-source.v1";
const INACTIVE_RESPONSE: &str = "io.tobkiri.native-bootstrap-inactive-source-result.v1";
#[path = "bootstrap_confirmation.rs"]
mod confirmation;

pub(super) async fn run<S: BootstrapInputOwner + BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    scope: &CoreValue,
    profile_id: &str,
) -> Result<BootstrapReviewFailure> {
    let caller = ReviewCaller::Resolver;
    caller.check(review, pack)?;
    pack.require_same_runtime()?;
    let inputs = match review.read.bootstrap_predecessor_inputs(
        &pack.work.get().source,
        pack.cancellation,
        profile_id,
        pack.input_budget,
    ) {
        Ok(inputs) => inputs,
        Err(error) => return Ok(original_failure(error)),
    };
    let needs_digest = inputs.needs_source_digest(profile_id);
    review.actual.predecessor_inputs = Some(inputs);

    let catalog = catalog::returned(
        review
            .actual
            .catalog
            .as_ref()
            .context("actual catalog missing")?,
    )?;
    let id = catalog::returned(
        review
            .actual
            .resolver_getter
            .as_ref()
            .context("actual resolver Profile ID missing")?,
    )?;
    let request = denied::frame(
        pack,
        SOURCE_REQUEST,
        [
            ("catalog", catalog::result_root(pack, catalog, scope)?),
            ("profile_id", catalog::result_root(pack, id, scope)?),
            (
                "needs_digest",
                CoreValue::bool(needs_digest, pack.input_budget)?,
            ),
        ],
    )?;
    let outcome = pack
        .observe_fixed_projection(&request, "bootstrap-predecessor-source")
        .await?;
    caller.check(review, pack)?;
    let response = match outcome {
        Ok(response) => response,
        Err(error) => {
            return catalog::handle_call_failure(review, pack, caller, error, false).await;
        }
    };
    let value = CoreValue::parse_guest(response.as_bytes(), pack.input_budget)?;
    let digest = source_digest(&value, scope, needs_digest)?;
    let inputs = review
        .actual
        .predecessor_inputs
        .as_ref()
        .expect("actual predecessor inputs");
    if digest
        .as_ref()
        .is_some_and(|digest| inputs.matches_source_digest(digest))
    {
        return Ok(BootstrapReviewFailure::NotBound(
            BootstrapReviewReachpoint::BootstrapPredecessorActivationNotBound,
        ));
    }
    drop((value, response, request));
    inactive(review, pack, scope, profile_id).await
}

async fn inactive<S: BootstrapInputOwner + BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    scope: &CoreValue,
    profile_id: &str,
) -> Result<BootstrapReviewFailure> {
    let caller = ReviewCaller::Resolver;
    let source = catalog::returned(
        review
            .actual
            .resolver_source_profile
            .as_ref()
            .context("actual source Profile missing")?,
    )?;
    let request = denied::frame(
        pack,
        INACTIVE_REQUEST,
        [("source", catalog::result_root(pack, source, scope)?)],
    )?;
    let outcome = pack
        .observe_fixed_projection(&request, "bootstrap-inactive-source")
        .await?;
    caller.check(review, pack)?;
    let response = match outcome {
        Ok(response) => response,
        Err(error) => {
            return catalog::handle_call_failure(review, pack, caller, error, false).await;
        }
    };
    let value = CoreValue::parse_guest(response.as_bytes(), pack.input_budget)?;
    denied::exact(&value, &["format", "scope", "profile_id"])?;
    if denied::field(&value, "format")?.as_str() != Some(INACTIVE_RESPONSE)
        || !denied::same_scope(&denied::field(&value, "scope")?, scope)
        || denied::field(&value, "profile_id")?.as_str() != Some(profile_id)
    {
        bail!("inactive predecessor source differs from the validated catalog identity");
    }
    drop((value, response, request));
    match review.read.inactive_predecessor_has_registry(
        &pack.work.get().source,
        pack.cancellation,
        profile_id,
    ) {
        Ok(false) => confirmation::run(review, pack, scope, profile_id).await,
        Ok(true) => Ok(BootstrapReviewFailure::NotBound(
            BootstrapReviewReachpoint::InactiveBootstrapRegistryNotBound,
        )),
        Err(error) => Ok(original_failure(error)),
    }
}

fn source_digest(
    value: &CoreValue,
    scope: &CoreValue,
    needs_digest: bool,
) -> Result<Option<Digest>> {
    denied::exact(value, &["format", "scope", "digest"])?;
    if denied::field(value, "format")?.as_str() != Some(SOURCE_RESPONSE)
        || !denied::same_scope(&denied::field(value, "scope")?, scope)
    {
        bail!("predecessor source response format or scope differs");
    }
    let digest = denied::field(value, "digest")?;
    if matches!(digest.inspect(), tobkiri_canon::CoreRef::Null) {
        return Ok(None);
    }
    if !needs_digest {
        bail!("predecessor source digest was not requested");
    }
    Ok(Some(Digest::parse(
        digest
            .as_str()
            .context("predecessor source digest is not text")?,
    )?))
}
