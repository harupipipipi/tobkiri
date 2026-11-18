//! Confirmation DATA from the exact resolved Profile and Authority inputs.
use super::*;

const CONFIRM_REQUEST: &str = "io.tobkiri.native-bootstrap-fresh-confirmation.v1";
const CONFIRM_RESPONSE: &str = "io.tobkiri.native-bootstrap-fresh-confirmation-result.v1";

#[derive(Clone, Copy, Default)]
pub(super) struct Predecessors<'a> {
    pub(super) definition: Option<&'a CoreValue>,
    pub(super) pack_pin: Option<&'a CoreValue>,
}

pub(super) async fn run<S: BootstrapInputOwner + BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    scope: &CoreValue,
    catalog: &catalog::ResolverCatalog,
    profile_id: &str,
    predecessors: Predecessors<'_>,
) -> listing::Outcome {
    let caller = ReviewCaller::Resolver;
    caller.check(review, pack)?;
    let resolved = CoreValue::parse_guest(
        review
            .actual
            .resolver_profile_result
            .as_ref()
            .context("actual resolved Profile missing")?
            .as_bytes(),
        pack.input_budget,
    )?;
    let getter = catalog::returned(
        review
            .actual
            .resolver_getter
            .as_ref()
            .context("actual resolver Profile ID missing")?,
    )?;
    let authority = review
        .actual
        .resolver_authority
        .as_ref()
        .context("actual resolver Authority snapshot missing")?;
    let nullable = |value: Option<&CoreValue>| match value {
        Some(value) => Ok(value.share()),
        None => CoreValue::null(pack.input_budget),
    };
    let request = denied::frame(
        pack,
        CONFIRM_REQUEST,
        [
            ("resolved", denied::field(&resolved, "resolved")?),
            ("profile_id", catalog::result_root(pack, getter, scope)?),
            (
                "snapshot",
                CoreValue::string(authority.snapshot_digest().as_str(), pack.input_budget)?,
            ),
            (
                "epoch",
                CoreValue::string(authority.security_epoch().decimal(), pack.input_budget)?,
            ),
            ("definition_predecessor", nullable(predecessors.definition)?),
            ("pack_pin_predecessor", nullable(predecessors.pack_pin)?),
        ],
    )?;
    drop(resolved);
    let outcome = pack.observe_bootstrap_confirmation(&request).await?;
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
    review.actual.bootstrap_confirmation = Some(response);
    let value = CoreValue::parse_guest(
        review
            .actual
            .bootstrap_confirmation
            .as_ref()
            .expect("actual confirmation")
            .as_bytes(),
        pack.input_budget,
    )?;
    let authority = review
        .actual
        .resolver_authority
        .as_ref()
        .expect("actual Authority snapshot");
    validate_confirmation(
        &value,
        scope,
        profile_id,
        authority.snapshot_digest(),
        authority.security_epoch().decimal(),
        predecessors,
        pack.input_budget,
    )?;
    let plan = &review
        .actual
        .resolver_identity
        .as_ref()
        .context("original resolved Profile identity missing")?
        .plan;
    let document = denied::field(&value, "confirmation")?;
    if denied::field(&document, "plan_digest")?.as_str() != Some(plan.as_str()) {
        bail!("bootstrap confirmation differs from the original resolved Plan");
    }
    let confirmation = denied::field(&value, "root")?;
    drop((document, value));
    listing::run(review, pack, scope, catalog, &confirmation).await
}

fn validate_confirmation(
    value: &CoreValue,
    scope: &CoreValue,
    profile_id: &str,
    snapshot: &Digest,
    epoch: &str,
    predecessors: Predecessors<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    denied::exact(value, &["format", "scope", "root", "owned", "confirmation"])?;
    if denied::field(value, "format")?.as_str() != Some(CONFIRM_RESPONSE)
        || !denied::same_scope(&denied::field(value, "scope")?, scope)
    {
        bail!("bootstrap confirmation format or scope differs");
    }
    let root = denied::field(value, "root")?;
    denied::reference(&root, scope)?;
    denied::ownership(value, &root, true)?;
    let mut keys = [
        "confirmation_api_version",
        "operation_id",
        "profile_id",
        "catalog_revision",
        "profile_revision",
        "plan_digest",
        "authority_snapshot_digest",
        "security_epoch",
        "base",
        "shell",
        "bindings",
        "definition_predecessor",
        "pack_pin_predecessor",
    ];
    let confirmation = denied::field(value, "confirmation")?;
    let mut fields = keys.len() - 2;
    for (key, predecessor) in [
        ("definition_predecessor", predecessors.definition),
        ("pack_pin_predecessor", predecessors.pack_pin),
    ] {
        if let Some(predecessor) = predecessor {
            if !denied::field(&confirmation, key)?.eq_guest(predecessor, budget)? {
                bail!("bootstrap confirmation {key} differs");
            }
            keys[fields] = key;
            fields += 1;
        }
    }
    let keys = &keys[..fields];
    if confirmation
        .object()
        .is_none_or(|object| object.len() != keys.len() + 1)
    {
        bail!("bootstrap confirmation fields differ");
    }
    for (key, expected) in [
        (
            "confirmation_api_version",
            "io.tobkiri.defaults-confirmation.v1",
        ),
        ("operation_id", "defaults.activate"),
        ("profile_id", profile_id),
        ("authority_snapshot_digest", snapshot.as_str()),
    ] {
        if denied::field(&confirmation, key)?.as_str() != Some(expected) {
            bail!("bootstrap confirmation {key} differs");
        }
    }
    let expected_epoch = epoch
        .parse::<i64>()
        .context("confirmation epoch exceeds canonical range")?;
    if denied::field(&confirmation, "security_epoch")?.as_i64() != Some(expected_epoch) {
        bail!("bootstrap confirmation security epoch differs");
    }
    for key in ["catalog_revision", "profile_revision", "plan_digest"] {
        Digest::parse(
            denied::field(&confirmation, key)?
                .as_str()
                .context("confirmation identity is not text")?,
        )?;
    }
    if denied::field(&confirmation, "base")?.object().is_none()
        || denied::field(&confirmation, "shell")?.object().is_none()
        || denied::field(&confirmation, "bindings")?.array().is_none()
    {
        bail!("bootstrap confirmation Pack fields differ");
    }
    let expected = denied::field(&confirmation, "confirmation_digest")?;
    let body = CoreValue::object_shared(
        keys.iter().copied().map(|key| {
            confirmation
                .get_key(key)
                .map(|value| (key, value))
                .ok_or(tobkiri_canon::AllocationError::Invalid)
        }),
        budget,
    )?;
    if expected.as_str() != Some(body.digest_guest(budget)?.as_str()) {
        bail!("bootstrap confirmation digest differs from its actual body");
    }
    Ok(())
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
    fn fresh_confirmation_validates_scope_fields_and_full_digest() -> Result<()> {
        // This test exercises DATA admission only. The macOS component lane
        // owns the actual OS-backed Store and retirement checks.
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let budget = &budget;
        let scope = CoreValue::parse_guest(br#"["1","2","3","4"]"#, budget)?;
        let snapshot = Digest::hash(b"snapshot");
        let plan = Digest::hash(b"plan");
        let mut confirmation = json!({
            "confirmation_api_version": "io.tobkiri.defaults-confirmation.v1",
            "operation_id": "defaults.activate", "profile_id": "defaults",
            "catalog_revision": plan.as_str(), "profile_revision": plan.as_str(),
            "plan_digest": plan.as_str(), "authority_snapshot_digest": snapshot.as_str(),
            "security_epoch": 1, "base": {"pack_id": "base"}, "shell": {"pack_id": "shell"},
            "bindings": [{"caller_function_id": "fixture", "operation_id": "run"}],
        });
        let body = CoreValue::parse_guest(&serde_json::to_vec(&confirmation)?, budget)?;
        confirmation["confirmation_digest"] = json!(body.digest_guest(budget)?.as_str());
        let packet = json!({
            "format": CONFIRM_RESPONSE, "scope": ["1","2","3","4"],
            "root": {"scope": ["1","2","3","4"], "object": "a"},
            "owned": ["a"], "confirmation": confirmation,
        });
        let validate = |packet: &Value, definition, pack_pin| {
            let value = CoreValue::parse_guest(&serde_json::to_vec(packet)?, budget)?;
            validate_confirmation(
                &value,
                &scope,
                "defaults",
                &snapshot,
                "1",
                Predecessors {
                    definition,
                    pack_pin,
                },
                budget,
            )
        };
        validate(&packet, None, None)?;
        for (path, invalid) in [
            ("/scope/3", json!("5")),
            ("/owned", json!([])),
            ("/confirmation/operation_id", json!("other.activate")),
            ("/confirmation/security_epoch", json!(2)),
            ("/confirmation/bindings/0/operation_id", json!("different")),
        ] {
            let mut changed = packet.clone();
            *changed.pointer_mut(path).context("test path missing")? = invalid;
            assert!(validate(&changed, None, None).is_err(), "{path}");
        }
        let mut handover = packet.clone();
        let add_predecessor = |packet: &mut Value, name: &str, predecessor: Value| -> Result<()> {
            let confirmation = &mut packet["confirmation"];
            confirmation
                .as_object_mut()
                .unwrap()
                .remove("confirmation_digest");
            confirmation[name] = predecessor;
            let body = CoreValue::parse_guest(&serde_json::to_vec(confirmation)?, budget)?;
            confirmation["confirmation_digest"] = json!(body.digest_guest(budget)?.as_str());
            Ok(())
        };
        let binding = json!({"pack_ids": ["one", "two"], "store_generation": 2});
        let expected = CoreValue::parse_guest(&serde_json::to_vec(&binding)?, budget)?;
        add_predecessor(&mut handover, "definition_predecessor", binding)?;
        validate(&handover, Some(&expected), None)?;
        assert!(validate(&handover, None, None).is_err());
        let binding = json!({"activation_id": "fixture", "store_generation": 3});
        let pin = CoreValue::parse_guest(&serde_json::to_vec(&binding)?, budget)?;
        let mut pin_only = packet;
        add_predecessor(&mut pin_only, "pack_pin_predecessor", binding.clone())?;
        validate(&pin_only, None, Some(&pin))?;
        assert!(validate(&pin_only, None, None).is_err());
        add_predecessor(&mut handover, "pack_pin_predecessor", binding)?;
        validate(&handover, Some(&expected), Some(&pin))?;
        assert!(validate(&handover, None, Some(&pin)).is_err());
        let confirmation = &mut handover["confirmation"];
        confirmation
            .as_object_mut()
            .unwrap()
            .remove("confirmation_digest");
        confirmation["definition_predecessor"]["pack_ids"] = json!(["two", "one"]);
        let body = CoreValue::parse_guest(&serde_json::to_vec(confirmation)?, budget)?;
        confirmation["confirmation_digest"] = json!(body.digest_guest(budget)?.as_str());
        assert!(validate(&handover, Some(&expected), Some(&pin)).is_err());
        if let Some(path) = std::env::var_os("TOBKIRI_ORIGINAL_INACTIVE_REGISTRY_REFERENCE") {
            let source = std::fs::read(path)?;
            anyhow::ensure!(
                source.len() <= MAX_BYTES,
                "original review fixture too large"
            );
            let fixture: Value = serde_json::from_slice(&source)?;
            let case = fixture["cases"]
                .as_array()
                .context("original cases missing")?
                .iter()
                .find(|row| row["case"] == "authenticated-handover")
                .context("authenticated original case missing")?;
            let setup_review = &case["setup_review"];
            assert_eq!(setup_review["state"], "review_required");
            let confirmation = &setup_review["confirmation"];
            let epoch = confirmation["security_epoch"]
                .as_u64()
                .context("original review epoch missing")?
                .to_string();
            let snapshot = Digest::parse(
                confirmation["authority_snapshot_digest"]
                    .as_str()
                    .context("original review Authority digest missing")?,
            )?;
            let predecessor = CoreValue::parse_guest(
                &serde_json::to_vec(&case["expected"]["predecessor"])?,
                budget,
            )?;
            let packet = CoreValue::parse_guest(
                &serde_json::to_vec(&json!({
                    "format": CONFIRM_RESPONSE, "scope": ["1","2","3","4"],
                    "root": {"scope": ["1","2","3","4"], "object": "a"},
                    "owned": ["a"], "confirmation": confirmation,
                }))?,
                budget,
            )?;
            validate_confirmation(
                &packet,
                &scope,
                setup_review["profile_id"]
                    .as_str()
                    .context("original review Profile ID missing")?,
                &snapshot,
                &epoch,
                Predecessors {
                    definition: Some(&predecessor),
                    pack_pin: None,
                },
                budget,
            )?;
        }
        Ok(())
    }
}
