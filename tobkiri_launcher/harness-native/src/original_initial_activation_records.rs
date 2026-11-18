//! Validate the three resolved records before any activation reservation.
use super::*;
use snapshot_graph::ResolvedRecords;

pub(super) fn validate<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    schemas: BoundProfileSchemas,
    resolved: &CoreValue,
    profile_id: &str,
    activation_id: &str,
) -> Result<()> {
    let canonical_id = activation_id
        .strip_prefix("activation:")
        .is_some_and(|suffix| {
            let bytes = suffix.as_bytes();
            (8..=128).contains(&bytes.len())
                && bytes[0].is_ascii_alphanumeric()
                && bytes.iter().all(|byte| {
                    byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(byte)
                })
        });
    if !canonical_id {
        return Err(raw(APP_DENIED, "activation_id is not canonical"));
    }

    let profile = decode_record(op, budget, resolved, "profile")?;
    let profile = select_record(op, budget, &profile, profile.root, RecordKind::Profile)?;
    let root = load_record_schema(op, budget, schemas, profile.name)?;
    let [common, projection] = schemas.references().ok_or_else(|| {
        OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Source,
            "selected activation record schema references",
        )
    })?;
    let common = json::decode_read_only_strict(common, op, budget)?;
    let projection = json::decode_read_only_strict(projection, op, budget)?;
    validate_record(op, budget, &profile, &[&root, &common, &projection])?;
    drop(root);

    let lock = decode_record(op, budget, resolved, "lock")?;
    let lock = select_record(op, budget, &lock, lock.root, RecordKind::Lock)?;
    validate_selected_record(op, budget, schemas, &lock, [&common, &projection])?;
    let plan = decode_record(op, budget, resolved, "plan")?;
    let plan = select_record(op, budget, &plan, plan.root, RecordKind::Plan)?;
    validate_selected_record(op, budget, schemas, &plan, [&common, &projection])?;
    drop((common, projection));

    snapshot_graph::verify_resolved(
        op,
        budget,
        ResolvedRecords {
            profile: profile.view(),
            lock: lock.view(),
            plan: plan.view(),
        },
    )?;
    let (doc, id) = profile.parts();
    if !field_is(doc, id, "profile_id", profile_id) {
        return Err(raw(
            APP_DENIED,
            "activation Profile identity does not match store",
        ));
    }
    Ok(())
}

fn decode_record<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    resolved: &CoreValue,
    field: &'static str,
) -> Result<Funded<Document>> {
    let record = resolved
        .get_key(field)
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let bytes = record
        .encode_guest(budget)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
    json::decode_read_only_strict(bytes.as_bytes(), op, budget)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{
        operation, with_setup_source, with_source,
    };
    use serde_json::{Value, json};

    const ACTIVATION: &str = "activation:defaults-123456789abcdef0";

    fn core(value: &Value, budget: &Arc<dyn AllocationBudget>) -> CoreValue {
        CoreValue::parse_guest(&serde_json::to_vec(value).unwrap(), budget).unwrap()
    }

    fn original_resolved_bytes() -> Vec<u8> {
        let manifest = PathBuf::from(
            std::env::var_os("TOBKIRI_ORIGINAL_INACTIVE_REGISTRY_REFERENCE").unwrap(),
        );
        let records = manifest
            .with_file_name("inactive-registry-inputs")
            .join("publication-matched/resolved-records.json");
        let bytes = fs::read(records).unwrap();
        assert!(bytes.len() <= tobkiri_canon::MAX_BYTES);
        bytes
    }

    #[tokio::test(flavor = "current_thread")]
    async fn rejects_invalid_id_and_records_before_leaving_the_schema_checks() {
        let fixed: Value = serde_json::from_slice(include_bytes!(
            "../../../tobkiri_runtime/tests/fixtures/profile_v4/pre-e853-activation.json"
        ))
        .unwrap();
        with_source(|source, publication, cancellation, budget| {
            let op = operation(publication, source, cancellation);
            let schemas = super::super::tests::bound_recovery_schemas();
            let check = |input: &Value, id| {
                validate(&op, budget, schemas, &core(input, budget), "defaults", id).unwrap_err()
            };
            for id in [
                "",
                "activation:short",
                "activation:.defaults",
                "activation:Defaults",
                "activation:defaults\n",
            ] {
                assert_eq!(
                    check(&json!({}), id).message,
                    "activation_id is not canonical"
                );
            }
            let mut input = json!({
                "profile": fixed["profile"], "lock": fixed["lock"], "plan": fixed["plan"],
            });
            for (record, field) in [
                ("profile", "profile_api_version"),
                ("lock", "lock_api_version"),
                ("plan", "plan_api_version"),
            ] {
                input[record][field] = json!("unsupported-version");
            }
            for (record, expected) in [
                ("profile", "unsupported profile API version"),
                ("lock", "unsupported profile_lock API version"),
                ("plan", "unsupported resolved_plan API version"),
            ] {
                assert!(matches!(check(&input, ACTIVATION).cause,
                    Some(OriginalAuthorityCause::SchemaValidation { argument, .. })
                        if argument.as_str().ok() == Some(expected)));
                input[record] = fixed[record].clone();
            }
            input["plan"]["bindings"][0]["function_principal"]["function_id"] = json!("BAD ID");
            assert!(matches!(
                check(&input, ACTIVATION).cause,
                Some(OriginalAuthorityCause::Unsupported(
                    "original activation record JSON Schema mismatch diagnostics"
                ))
            ));
            assert!(!source.root.join("authority/v4.sqlite3").exists());
        });
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "requires the pinned original's freshly resolved Profile, Lock and Plan"]
    async fn verifies_original_fresh_graph_and_profile_identity_without_activation() {
        let bytes = original_resolved_bytes();
        with_setup_source(|source, publication, cancellation, budget| {
            let op = operation(publication, source, cancellation);
            let resolved = CoreValue::parse_guest(&bytes, budget).unwrap();
            let schemas = super::super::tests::bound_recovery_schemas();
            assert!(resolved.get_key("activation").is_none());
            validate(&op, budget, schemas, &resolved, "defaults", ACTIVATION).unwrap();
            let mismatch =
                validate(&op, budget, schemas, &resolved, "different", ACTIVATION).unwrap_err();
            assert_eq!(
                mismatch.message,
                "activation Profile identity does not match store"
            );
            drop(resolved);
            let mut changed: Value = serde_json::from_slice(&bytes).unwrap();
            changed["lock"]["profile_revision"] = json!(Digest::hash(b"wrong revision").as_str());
            let mismatch = validate(
                &op,
                budget,
                schemas,
                &core(&changed, budget),
                "different",
                ACTIVATION,
            )
            .unwrap_err();
            assert_eq!(mismatch.message, "ProfileLock or ResolvedPlan is stale");
            assert!(!source.root.join("authority/v4.sqlite3").exists());
        });
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "requires the pinned original's freshly resolved Profile, Lock and Plan"]
    async fn commits_original_fresh_records_with_the_shared_workspace_writer() {
        use std::ops::ControlFlow;

        let bytes = original_resolved_bytes();
        with_setup_source(|owner, publication, cancellation, budget| {
            let op = operation(publication, owner, cancellation);
            let resolved = CoreValue::parse_guest(&bytes, budget).unwrap();
            let schemas = super::super::tests::bound_recovery_schemas();
            let plan = resolved.get_key("plan").unwrap();
            let digest = plan.get_key("plan_digest").unwrap();
            let digest = Digest::parse(digest.as_str().unwrap()).unwrap();
            let activation_id = format!("activation:defaults-{}", &digest.as_str()[7..23]);
            let created_at = CoreValue::string("2026-11-18T01:02:03Z", budget).unwrap();
            let path = owner.root.join("workspaces/defaults");
            fs::create_dir_all(&path).unwrap();
            with_registered_store(&op, &owner.root, |store| {
                let workspace =
                    RecoveryWorkspace::open(&op, &path, LockName::workspace("defaults"))?;
                let first = ready_test(workspace.recover_async(&op, async |state, _, journal| {
                    let recovered = pending_reservation::recover_pending_locked(
                        &op, store, state, journal, "defaults", budget,
                    )?;
                    assert!(recovered.is_none());
                    assert!(!state.exists(&op, "active.json")?);
                    Ok(ControlFlow::<()>::Continue(()))
                }))?;
                assert!(first.is_continue());
                validate(&op, budget, schemas, &resolved, "defaults", &activation_id)?;
                let mut identity_gates = 0;
                let wrote = ready_test(workspace.recover_async(
                    &op,
                    async |state, workspace_digest, journal| {
                        let recovered = pending_reservation::recover_pending_locked(
                            &op, store, state, journal, "defaults", budget,
                        )?;
                        assert!(recovered.is_none());
                        legacy_activation::activate(
                            &op,
                            store,
                            state,
                            workspace_digest,
                            budget,
                            legacy_activation::Input {
                                profile_id: "defaults",
                                activation_id: &activation_id,
                                resolved: &resolved,
                                created_at: &created_at,
                                schemas,
                            },
                            |profile| {
                                // This test covers state IO. The artifact identity
                                // gate is supplied by the future fresh caller.
                                assert_eq!(
                                    profile.get_key("profile_id").unwrap().as_str(),
                                    Some("defaults")
                                );
                                identity_gates += 1;
                                Ok(())
                            },
                        )?;
                        Ok(ControlFlow::<()>::Continue(()))
                    },
                ))?;
                assert!(wrote.is_continue());
                assert_eq!(identity_gates, 2);
                drop(resolved);
                let checked = ready_test(workspace.recover_async(
                    &op,
                    async |state, workspace_digest, journal| {
                        assert!(journal.is_none());
                        let checked = read(
                            &op,
                            state,
                            workspace_digest,
                            budget,
                            Some(schemas),
                            store,
                            ("defaults", async |_| Ok(None)),
                        )
                        .await?;
                        let active = checked.current.expect("verified fresh activation");
                        assert_eq!(
                            active.candidate_field("activation_id").as_str(),
                            Some(activation_id.as_str())
                        );
                        Ok(ControlFlow::<()>::Continue(()))
                    },
                ))?;
                assert!(checked.is_continue());
                Ok(())
            })
            .unwrap();
        });
    }
}
