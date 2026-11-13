//! Original Profile catalog and Setup presentation from one retained candidate.
use super::*;
use crate::bootstrap_review::Review;
use crate::{BootstrapReviewFailure, BootstrapReviewOwner};
use review_continuation::ReviewCaller;

pub(super) type Outcome = Result<std::result::Result<CoreBuffer, BootstrapReviewFailure>>;
const PREVIEW_REQUEST: &str = "io.tobkiri.native-bootstrap-preview-catalog.v1";
const PREVIEW_RESPONSE: &str = "io.tobkiri.native-bootstrap-preview-catalog-result.v1";
const LISTING_REQUEST: &str = "io.tobkiri.native-bootstrap-setup-listing.v1";
const LISTING_RESPONSE: &str = "io.tobkiri.native-bootstrap-setup-listing-result.v1";

pub(super) async fn run<S: BootstrapInputOwner + BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    scope: &CoreValue,
    confirmation: &CoreValue,
) -> Outcome {
    let caller = ReviewCaller::Resolver;
    caller.check(review, pack)?;
    let original = catalog::returned(
        review
            .actual
            .catalog
            .as_ref()
            .context("actual catalog missing")?,
    )?;
    let original = catalog::result_root(pack, original, scope)?;
    let resolved = CoreValue::parse_guest(
        review
            .actual
            .resolver_profile_result
            .as_ref()
            .context("actual resolved Profile missing")?
            .as_bytes(),
        pack.input_budget,
    )?;
    let resolved = denied::field(&resolved, "resolved")?;
    let request = denied::frame(
        pack,
        PREVIEW_REQUEST,
        [
            ("catalog", original.clone()),
            ("resolved", resolved),
            ("confirmation", confirmation.clone()),
        ],
    )?;
    let outcome = pack
        .observe_fixed_projection(&request, "bootstrap-preview")
        .await?;
    caller.check(review, pack)?;
    let response = match outcome {
        Ok(response) => response,
        Err(error) => {
            return Ok(Err(catalog::handle_call_failure(
                review, pack, caller, error, false,
            )
            .await?));
        }
    };
    review.actual.bootstrap_preview = Some(response);
    let preview = CoreValue::parse_guest(
        review
            .actual
            .bootstrap_preview
            .as_ref()
            .expect("actual preview")
            .as_bytes(),
        pack.input_budget,
    )?;
    let catalog = preview_reference(&preview, scope, &original, confirmation)?;
    drop((preview, original, request));
    let diagnostic = match review.denial_diagnostic.as_ref() {
        Some(text) => CoreValue::string_from_utf8(text, pack.input_budget)?,
        None => CoreValue::null(pack.input_budget)?,
    };
    let request = denied::frame(
        pack,
        LISTING_REQUEST,
        [
            ("catalog", catalog),
            ("confirmation", confirmation.clone()),
            ("active", CoreValue::bool(review.active, pack.input_budget)?),
            (
                "activation_denied",
                CoreValue::bool(review.activation_denied, pack.input_budget)?,
            ),
            ("denial_diagnostic", diagnostic),
        ],
    )?;
    let outcome = pack
        .observe_fixed_projection(&request, "bootstrap-listing")
        .await?;
    caller.check(review, pack)?;
    let response = match outcome {
        Ok(response) => response,
        Err(error) => {
            return Ok(Err(catalog::handle_call_failure(
                review, pack, caller, error, false,
            )
            .await?));
        }
    };
    review.actual.bootstrap_listing = Some(response);
    let result = CoreValue::parse_guest(
        review
            .actual
            .bootstrap_listing
            .as_ref()
            .expect("actual listing")
            .as_bytes(),
        pack.input_budget,
    )?;
    let original_confirmation = CoreValue::parse_guest(
        review
            .actual
            .bootstrap_confirmation
            .as_ref()
            .expect("actual confirmation")
            .as_bytes(),
        pack.input_budget,
    )?;
    let original_confirmation = denied::field(&original_confirmation, "confirmation")?;
    let presentation = Presentation {
        active: review.active,
        activation_denied: review.activation_denied,
        diagnostic: review
            .denial_diagnostic
            .as_ref()
            .map(CoreBuffer::as_str)
            .transpose()?,
    };
    let listing = listing_data(
        &result,
        scope,
        &original_confirmation,
        presentation,
        pack.input_budget,
    )?;
    Ok(Ok(listing.encode_guest(pack.input_budget)?))
}

fn preview_reference(
    value: &CoreValue,
    scope: &CoreValue,
    original: &CoreValue,
    confirmation: &CoreValue,
) -> Result<CoreValue> {
    denied::exact(value, &["format", "scope", "root", "owned", "confirmation"])?;
    if denied::field(value, "format")?.as_str() != Some(PREVIEW_RESPONSE)
        || !denied::same_scope(&denied::field(value, "scope")?, scope)
        || !denied::same_reference(&denied::field(value, "confirmation")?, confirmation)?
    {
        bail!("bootstrap preview scope or confirmation differs");
    }
    let root = denied::field(value, "root")?;
    denied::reference(&root, scope)?;
    denied::ownership(value, &root, true)?;
    if denied::same_reference(&root, original)? || denied::same_reference(&root, confirmation)? {
        bail!("bootstrap preview did not return a distinct catalog");
    }
    Ok(root)
}

struct Presentation<'a> {
    active: bool,
    activation_denied: bool,
    diagnostic: Option<&'a str>,
}

fn listing_data(
    value: &CoreValue,
    scope: &CoreValue,
    confirmation: &CoreValue,
    presentation: Presentation<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    denied::exact(value, &["format", "scope", "listing"])?;
    if denied::field(value, "format")?.as_str() != Some(LISTING_RESPONSE)
        || !denied::same_scope(&denied::field(value, "scope")?, scope)
    {
        bail!("bootstrap listing scope or format differs");
    }
    let listing = denied::field(value, "listing")?;
    denied::exact(
        &listing,
        &[
            "setup_api_version",
            "state",
            "denial_diagnostic",
            "packs",
            "recommended_default_profile",
            "required_transaction",
        ],
    )?;
    let state = match (presentation.active, presentation.activation_denied) {
        (true, _) => "active",
        (false, true) => "activation_denied",
        (false, false) => "review_required",
    };
    let diagnostic = denied::field(&listing, "denial_diagnostic")?;
    let diagnostic_matches = match presentation.diagnostic {
        Some(text) => diagnostic.as_str() == Some(text),
        None => matches!(diagnostic.inspect(), tobkiri_canon::CoreRef::Null),
    };
    if denied::field(&listing, "setup_api_version")?.as_str() != Some("io.tobkiri.setup-state.v4")
        || denied::field(&listing, "state")?.as_str() != Some(state)
        || !diagnostic_matches
    {
        bail!("bootstrap listing presentation differs from its fixed caller");
    }
    let profile = denied::field(&listing, "recommended_default_profile")?;
    if !denied::field(&profile, "confirmation")?.eq_guest(confirmation, budget)? {
        bail!("bootstrap listing changed its reviewed confirmation");
    }
    Ok(listing)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};
    use tobkiri_canon::{AllocationError, AllocationRequest};

    struct TestBudget;
    impl AllocationBudget for TestBudget {
        fn reserve(&self, _: AllocationRequest) -> Result<(), AllocationError> {
            Ok(())
        }
        fn release(&self, _: u64) {}
    }

    #[test]
    fn preview_and_listing_require_the_same_reviewed_confirmation() -> Result<()> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let budget = &budget;
        let parse = |value: &Value| -> Result<CoreValue> {
            Ok(CoreValue::parse_guest(&serde_json::to_vec(value)?, budget)?)
        };
        let scope = json!(["1", "2", "3", "4"]);
        let reference = |id: &str| json!({"scope":scope,"object":id});
        let original = parse(&reference("a"))?;
        let confirmation_ref = parse(&reference("b"))?;
        let confirmation = json!({"bindings":[{"pack_id":"exact"}]});
        let confirmation_data = parse(&confirmation)?;
        let preview = json!({"format":PREVIEW_RESPONSE,"scope":scope,"root":reference("c"),"owned":["c"],"confirmation":reference("b")});
        let scope = parse(&scope)?;
        preview_reference(&parse(&preview)?, &scope, &original, &confirmation_ref)?;
        let mut swapped = preview;
        swapped["confirmation"] = reference("d");
        assert!(
            preview_reference(&parse(&swapped)?, &scope, &original, &confirmation_ref).is_err()
        );
        let mut packet = json!({"format":LISTING_RESPONSE,"scope":["1","2","3","4"],"listing": {
            "setup_api_version":"io.tobkiri.setup-state.v4", "state":"activation_denied",
            "denial_diagnostic":"diagnostic", "packs":[], "required_transaction":[],
            "recommended_default_profile":{"confirmation":confirmation},
        }});
        let checked = |value: &Value| {
            listing_data(
                &parse(value)?,
                &scope,
                &confirmation_data,
                Presentation {
                    active: false,
                    activation_denied: true,
                    diagnostic: Some("diagnostic"),
                },
                budget,
            )
        };
        checked(&packet)?;
        packet["listing"]["recommended_default_profile"]["confirmation"]["bindings"][0]["pack_id"] =
            json!("different");
        assert!(checked(&packet).is_err());
        Ok(())
    }
}
