use super::*;
use crate::original_authority::catalog::initial_catalog_fixture;
use crate::original_authority::definitions::tests::{
    Source, lock_free, operation, with_existing_setup_source, with_setup_source,
};
use rusqlite::Connection;
use serde_json::Value;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Outcome {
    Published,
    MissingShell,
    ConflictingDefinition,
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires the original approved Profile records and physical bundled Shell"]
async fn commits_confirmed_initial_publication_to_the_host_pointer() {
    let manifest =
        PathBuf::from(std::env::var_os("TOBKIRI_ORIGINAL_INACTIVE_REGISTRY_REFERENCE").unwrap());
    let rows: Value = serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    let imported = rows["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["case"] == "authenticated-handover")
        .unwrap();
    let checks = imported["publication_checks"].as_array().unwrap();
    let release = manifest.with_file_name("shell-release");
    let fixtures = manifest.with_file_name("inactive-registry-inputs");
    for (case, outcome) in [
        ("matched", Outcome::Published),
        ("matched-with-predecessor", Outcome::Published),
        ("matched-with-predecessor", Outcome::MissingShell),
        ("matched-with-predecessor", Outcome::ConflictingDefinition),
    ] {
        let row = checks.iter().find(|row| row["case"] == case).unwrap();
        let intent = &row["outcome"]["candidate"]["intent"]["payload"];
        let witness = fixtures.join(format!("publication-{case}"));
        let approved = row
            .get("confirmation")
            .unwrap_or(&imported["setup_review"]["confirmation"]);
        assert_eq!(
            approved.get("definition_predecessor").is_some(),
            case == "matched"
        );
        with_setup_source(|owner, read, cancellation, budget| {
            let root = &owner.root;
            seed(
                root,
                &witness.join("user-data"),
                &intent["predecessor"],
                case == "matched" || outcome == Outcome::ConflictingDefinition,
            );
            let approved =
                CoreValue::parse_guest(&serde_json::to_vec(approved).unwrap(), budget).unwrap();
            let original_resolved = load_document(&witness.join("resolved-records.json"), budget);
            let definition = load_document(&witness.join("reread-source.json"), budget);
            let expected = &intent["candidate"];
            let digest = |key| Digest::parse(expected[key].as_str().unwrap()).unwrap();
            let definition_digest = digest("profile_definition_digest");
            let (candidate, resolved) = BootstrapActivationCandidate::fixture(
                approved.share(),
                definition.share(),
                original_resolved.share(),
                Digest::parse(row["reread_bundle_digest"].as_str().unwrap()).unwrap(),
                [
                    definition_digest.clone(),
                    digest("plan_digest"),
                    digest("lock_digest"),
                ],
                budget,
            )
            .unwrap();
            assert_eq!(
                read.begin_confirmed(owner, cancellation, approved, budget)
                    .unwrap(),
                Prefix::NoIntent
            );
            let op = operation(read, owner, cancellation);
            let missing_release = root.join("empty-artifacts");
            let artifacts = if outcome == Outcome::MissingShell {
                fs::create_dir(&missing_release).unwrap();
                &missing_release
            } else {
                &release
            };
            let loader = Mutex::new(Some(
                initial_catalog_fixture::load(&op, budget, &witness, artifacts).unwrap(),
            ));
            let activated = ready_test(read.activate_initial(
                owner,
                cancellation,
                budget,
                InitialPublication {
                    candidate: &candidate,
                    resolved: &resolved,
                    source_profile: &definition,
                    loader: &loader,
                    schemas: workspace_active::tests::bound_recovery_schemas(),
                },
            ));
            let profiles = root.join("profiles");
            let lock = profiles.join(LOCK);
            assert!(!lock_free(&lock));
            if outcome != Outcome::Published {
                let error = activated.unwrap_err();
                let journal = read_intent(&op, &profiles).unwrap().unwrap();
                verify_recovery_confirmation(&journal, candidate.confirmation(), budget).unwrap();
                assert_eq!(
                    fs::read(profiles.join("active.json")).unwrap(),
                    fs::read(witness.join("user-data/profiles/active.json")).unwrap()
                );
                let workspace = root.join("workspaces/defaults/activation");
                assert!(
                    !workspace.join("active.json").exists()
                        && !workspace.join("pending.json").exists()
                );
                if outcome == Outcome::MissingShell {
                    assert_eq!(error.class, recovery_workspace::APP_DENIED);
                    assert_eq!(
                        error.message,
                        "active Profile Shell artifact identity is unavailable"
                    );
                    assert!(workspace.is_dir());
                    let db = Connection::open(root.join("authority/v4.sqlite3")).unwrap();
                    let reserved: i64 = db
                        .query_row("SELECT COUNT(*) FROM activation_reservations", [], |row| {
                            row.get(0)
                        })
                        .unwrap();
                    assert_eq!(reserved, 0);
                } else {
                    assert_eq!(
                        error.class,
                        "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreConflict"
                    );
                    assert_eq!(
                        error.message,
                        "bootstrap Profile conflicts with the existing definition"
                    );
                    assert!(!root.join("authority").exists());
                    assert!(!workspace.exists());
                }
                drop((journal, loader));
                read.close_continuation().unwrap();
                assert!(lock_free(&lock));
                return;
            }
            activated.unwrap_or_else(|error| panic!("{case}: {error:?}"));
            assert!(!root.join("profiles").join(JOURNAL).exists());
            assert!(root.join("profiles").join(KEY).is_file());
            let active = active_profile::load_verified(&op, root, budget)
                .unwrap()
                .unwrap();
            assert_eq!(active.profile_id().utf8().unwrap(), "defaults");
            let activation_id = expected["activation_id"].as_str().unwrap();
            assert_eq!(
                active.pointer.text(&active.doc, "activation_id").unwrap(),
                activation_id
            );
            let generation = intent["predecessor"]["generation"].as_i64().unwrap_or(0) + 1;
            assert_eq!(
                active.pointer.integer(&active.doc, "generation"),
                generation
            );
            let index: Value =
                serde_json::from_slice(&fs::read(root.join("profiles/index.json")).unwrap())
                    .unwrap();
            let registered = index["profiles"]
                .as_array()
                .unwrap()
                .iter()
                .find(|row| row["profile_id"] == "defaults")
                .unwrap();
            assert_eq!(
                registered["current_revision"].as_str(),
                Some(definition_digest.as_str())
            );
            assert!(
                !root
                    .join("workspaces/defaults/activation/pending.json")
                    .exists()
            );
            let db = Connection::open(root.join("authority/v4.sqlite3")).unwrap();
            let state: String = db
                .query_row(
                    "SELECT state FROM activation_reservations WHERE activation_id=?",
                    [activation_id],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(state, "active");
            drop((active, db, loader));
            read.close_continuation().unwrap();
            assert!(lock_free(&lock));

            let files = [
                "profiles/active.json",
                "workspaces/defaults/activation/active.json",
                "authority/v4.sqlite3",
                "authority/v4.key",
                "authority/.v4.sqlite3.lifecycle.lock",
            ];
            let capture = || files.map(|path| fs::read(root.join(path)).unwrap());
            let before = capture();
            let observer = OriginalActivationInspection::new(&read.invocation).unwrap();
            let schemas = &workspace_active::tests::RECOVERY_SCHEMAS;
            let component = Digest::parse(schemas.component_sha256).unwrap();
            let read_until = |deadline| {
                ready_test(observer.read(
                    owner,
                    cancellation,
                    budget,
                    schemas,
                    &component,
                    deadline,
                ))
            };
            let read_current = || read_until(read.invocation.deadline().unwrap());
            let expired = read_until(Instant::now())
                .err()
                .expect("expired Launch is refused");
            assert!(matches!(
                expired.cause,
                Some(OriginalAuthorityCause::Deadline)
            ));
            assert!(observer.close_receipt().verified());
            assert_eq!(capture(), before);
            let observed = read_current().unwrap_or_else(|error| panic!("{case}: {error:?}"));
            let prepare = crate::OriginalProviderPrepareSelection::select(&observed, budget)
                .expect("authenticate the selected Provider prepare binding")
                .expect("confirmed Profile selects the bundled Provider prepare");
            assert_eq!(prepare.provider().pack_id(), "rumi_provider_registry_pack");
            assert_eq!(
                prepare.provider().operation_id(),
                "rumi_provider_registry_pack.provider-configure-prepare"
            );
            let models = crate::OriginalModelSettingsSelection::select(&observed, budget)
                .expect("authenticate the selected Model List binding")
                .expect("confirmed Profile selects the bundled Model List");
            let model_list = models.model_list();
            assert_eq!(model_list.caller_function_id(), "shell.tauri.default");
            assert_eq!(model_list.pack_id(), "rumi_model_registry_pack");
            assert_eq!(
                model_list.function_id(),
                "rumi_model_registry_pack.model-registry.profile"
            );
            assert_eq!(
                model_list.contract_id(),
                "tobkiri.resource.ai.model.profile.v1"
            );
            assert_eq!(
                model_list.operation_id(),
                "rumi_model_registry_pack.model-profile-resource"
            );
            assert_eq!(
                model_list.variant_id(),
                "rumi_model_registry_pack.model-registry.profile.python"
            );
            assert_eq!(
                model_list.requested_scope_digest(),
                "sha256:64fa11ae6949412f39039e7069da0a614e78401c55071be40755f2920b28932e"
            );
            check_model_save_grants(&observed, budget);
            check_conversation_read_grants(&observed, budget);
            check_saved_receipt_grants(&observed, budget);
            check_model_resolve_grants(&observed, budget);
            assert_eq!(
                observed
                    .identity()
                    .get_key("activation_id")
                    .unwrap()
                    .as_str(),
                Some(activation_id)
            );
            let (profile, original_lock, plan) = observed.records();
            assert_eq!(
                profile.get_key("profile_id").unwrap().as_str(),
                Some("defaults")
            );
            assert_eq!(
                original_lock.get_key("lock_digest").unwrap().as_str(),
                expected["lock_digest"].as_str()
            );
            assert_eq!(
                plan.get_key("plan_digest").unwrap().as_str(),
                expected["plan_digest"].as_str()
            );
            assert!(observer.close_receipt().verified());
            assert_eq!(capture(), before);
            for suffix in ["-wal", "-shm", "-journal"] {
                assert!(!root.join(format!("authority/v4.sqlite3{suffix}")).exists());
            }
            drop(observed);

            let pending = root.join("workspaces/defaults/activation/pending.json");
            fs::write(&pending, b"pending work belongs to recovery").unwrap();
            assert!(read_current().is_err());
            assert!(observer.close_receipt().verified());
            assert_eq!(
                fs::read(&pending).unwrap(),
                b"pending work belongs to recovery"
            );
            assert_eq!(capture(), before);
            fs::remove_file(pending).unwrap();

            let journal = root.join("authority/v4.sqlite3-wal");
            fs::write(&journal, b"WAL requires the existing recovery path").unwrap();
            let failure = read_current()
                .err()
                .expect("uncheckpointed database is refused");
            assert_eq!(
                failure.message,
                "existing Authority database requires its normal recovery path"
            );
            assert!(observer.close_receipt().verified());
            assert_eq!(
                fs::read(&journal).unwrap(),
                b"WAL requires the existing recovery path"
            );
            assert_eq!(capture(), before);
            fs::remove_file(journal).unwrap();

            let host_pointer = fs::read(profiles.join("active.json")).unwrap();
            let workspace = root.join("workspaces/defaults/activation");
            let workspace_pointer = fs::read(workspace.join("active.json")).unwrap();
            let check_snapshot = |witness: &Path, successor_required: bool| {
                read_registered(owner, witness, artifacts, |records, budget| {
                    let assert_record = |actual: &CoreValue, expected: &CoreValue| {
                        assert_eq!(
                            actual.encode_guest(budget).unwrap().as_bytes(),
                            expected.encode_guest(budget).unwrap().as_bytes()
                        );
                    };
                    assert_record(&records.registered_profile, &definition);
                    assert_record(
                        &records.active_profile,
                        &original_resolved.get_key("profile").unwrap(),
                    );
                    assert_eq!(
                        records.verified_definition_digest.as_str(),
                        Some(definition_digest.as_str())
                    );
                    if successor_required {
                        assert!(records.active_plan.is_none());
                    } else {
                        assert_record(
                            &records.active_plan.unwrap(),
                            &original_resolved.get_key("plan").unwrap(),
                        );
                    }
                })
                .unwrap();
            };
            check_snapshot(&witness, false);
            let bundled_successor = root.join("successor-catalog");
            seed_successor_catalog(&witness, &bundled_successor);
            check_snapshot(&bundled_successor, true);
            let resolved_source = witness.join("reread-source.json");
            assert!(
                read_post_resolve_predecessor(owner, &resolved_source, &witness, artifacts)
                    .unwrap()
            );
            let Err(error) = read_post_resolve_predecessor(
                owner,
                &resolved_source,
                &bundled_successor,
                artifacts,
            ) else {
                panic!("the retained overlay still pins the old Shell definition");
            };
            assert_eq!(error.class, recovery_workspace::APP_DENIED);
            assert_eq!(
                error.message,
                "active Profile Shell artifact is unavailable"
            );
            assert_eq!(
                fs::read(profiles.join("active.json")).unwrap(),
                host_pointer
            );
            with_existing_setup_source(owner, |_, _, _, budget| {
                replace_host_selection(root, &host_pointer, budget);
            });
            let predecessor = fs::read(profiles.join("active.json")).unwrap();
            let error = read_registered(owner, &witness, artifacts, |_, _| {
                panic!("the first read must reject the replacement pointer");
            })
            .unwrap_err();
            assert_eq!(
                error.class,
                "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreConflict",
            );
            assert_eq!(
                error.message,
                "bootstrap review does not match the verified active definition"
            );
            let Err(error) =
                read_post_resolve_predecessor(owner, &resolved_source, &witness, artifacts)
            else {
                panic!("the independently loaded Authority must reject the replacement pointer");
            };
            assert_eq!(
                error.class,
                "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreConflict"
            );
            assert_eq!(
                error.message,
                "Pack pin review does not match the verified active pointer"
            );
            assert_eq!(fs::read(profiles.join("active.json")).unwrap(), predecessor);
            assert_eq!(
                fs::read(workspace.join("active.json")).unwrap(),
                workspace_pointer
            );
            assert!(!profiles.join(JOURNAL).exists());
            assert!(lock_free(
                &workspace.join(LockName::workspace("defaults").text())
            ));
        });
    }
}

fn check_model_save_grants(
    observed: &crate::OriginalActiveProfile,
    budget: &Arc<dyn AllocationBudget>,
) {
    let save = crate::OriginalModelSaveSelection::select(observed, budget)
        .expect("authenticate both Model save bindings")
        .expect("confirmed Profile selects Model Manage and its Provider Registry read");
    let manage = save.model_manage();
    assert_eq!(manage.caller_function_id(), "shell.tauri.default");
    assert_eq!(
        manage.function_id(),
        "rumi_model_registry_pack.model-registry.manage"
    );
    assert_eq!(
        manage.variant_id(),
        "rumi_model_registry_pack.model-registry.manage.python"
    );
    assert_eq!(
        manage.contract_id(),
        "tobkiri.action.ai.model.profile.manage.v1"
    );
    assert_eq!(
        manage.operation_id(),
        "rumi_model_registry_pack.model-profile-manage"
    );
    assert_eq!(
        manage.requested_scope_digest(),
        "sha256:7e5d3aa5434e2f9a7d23ed7df2bab9df80d2bd8bcf5811ef9af33aa1bada567b"
    );
    let provider = save.provider_read();
    assert_eq!(provider.caller_function_id(), manage.function_id());
    assert_eq!(
        provider.function_id(),
        "rumi_provider_registry_pack.provider-registry.resource"
    );
    assert_eq!(
        provider.variant_id(),
        "rumi_provider_registry_pack.provider-registry.resource.python"
    );
    assert_eq!(
        provider.contract_id(),
        "tobkiri.resource.ai.provider.registry.v1"
    );
    assert_eq!(
        provider.operation_id(),
        "rumi_provider_registry_pack.provider-registry-resource"
    );
    assert_eq!(
        provider.requested_scope_digest(),
        "sha256:1b58a95d7c67e6f789b5e17a10a61c360c706443adbbe3026502e7e29395bd6f"
    );
}

fn check_conversation_read_grants(
    observed: &crate::OriginalActiveProfile,
    budget: &Arc<dyn AllocationBudget>,
) {
    let read = crate::OriginalConversationReadSelection::select(observed, budget)
        .expect("authenticate the selected Conversation owner")
        .expect("confirmed Profile selects the Conversation owner");
    for (caller, pin) in [
        ("shell.tauri.default", read.shell()),
        (
            "rumi_turn_runtime_pack.turn-runtime.saved",
            read.saved_turn(),
        ),
        (
            "tobkiri_conversation_orchestration_pack.saved",
            read.saved_orchestration(),
        ),
    ] {
        let pin = pin.expect("each original caller has its own signed read grant");
        assert_eq!(pin.caller_function_id(), caller);
        assert_eq!(
            pin.function_id(),
            "rumi_conversation_store_pack.conversation-store.resource"
        );
        assert_eq!(
            pin.operation_id(),
            "rumi_conversation_store_pack.conversation-resource"
        );
        assert_eq!(
            pin.requested_scope_digest(),
            "sha256:d0069dfedd4def12d6921155433e7f280b2e2fb4842b9c32d07062f913dfaa8c"
        );
    }
}

fn check_saved_receipt_grants(
    observed: &crate::OriginalActiveProfile,
    budget: &Arc<dyn AllocationBudget>,
) {
    use crate::OriginalSavedReceiptRole::{SavedReconcile, SavedTurn};

    let receipt = crate::OriginalSavedReceiptSelection::select(observed, budget)
        .expect("authenticate the original Saved Receipt grants")
        .expect("confirmed Profile selects the Conversation owner for Turn Runtime");
    let mut authorities = std::collections::BTreeSet::new();
    for (role, caller) in [
        (SavedTurn, "rumi_turn_runtime_pack.turn-runtime.saved"),
        (
            SavedReconcile,
            "rumi_turn_runtime_pack.turn-runtime.reconcile",
        ),
    ] {
        let pin = receipt.pin(role).expect("each role has a signed grant");
        assert_eq!(pin.caller_function_id(), caller);
        assert_eq!(pin.contract_id(), "tobkiri.resource.conversation.v1");
        assert_eq!(
            pin.function_id(),
            "rumi_conversation_store_pack.conversation-store.resource"
        );
        assert_eq!(
            pin.operation_id(),
            "rumi_conversation_store_pack.conversation-resource"
        );
        assert_eq!(
            pin.requested_scope_digest(),
            "sha256:d0069dfedd4def12d6921155433e7f280b2e2fb4842b9c32d07062f913dfaa8c"
        );
        assert!(authorities.insert(pin.authority_reference()));
    }
}

fn check_model_resolve_grants(
    observed: &crate::OriginalActiveProfile,
    budget: &Arc<dyn AllocationBudget>,
) {
    use crate::OriginalModelResolveRole::{Generate, QuoteGenerate, QuoteStream, Stream};
    const GENERATE_OPERATION: &str = "rumi_model_registry_pack.model-profile-resource.generate";
    const STREAM_OPERATION: &str = "rumi_model_registry_pack.model-profile-resource.stream";
    const GENERATE_SCOPE: &str =
        "sha256:e0adca96baa7c877bc049187d16aee8eee3dbc00c5a06f5160e7c92baa7be0f6";
    const STREAM_SCOPE: &str =
        "sha256:f5746a0621a144db146e8e05bd24564378fb7c72fd8d3b4d07ecf7d9a4369472";
    const QUOTE_CALLER: &str = "rumi_ai_gateway_pack.ai-gateway.route-quote";

    let resolve = crate::OriginalModelResolveSelection::select(observed, budget)
        .expect("authenticate the Model Registry Resolve bindings")
        .expect("confirmed Profile selects Model Registry Resolve");
    let mut authorities = std::collections::BTreeSet::new();
    for (role, caller, operation, scope) in [
        (
            QuoteGenerate,
            QUOTE_CALLER,
            GENERATE_OPERATION,
            GENERATE_SCOPE,
        ),
        (QuoteStream, QUOTE_CALLER, STREAM_OPERATION, STREAM_SCOPE),
        (
            Generate,
            "rumi_ai_gateway_pack.ai-gateway.generate",
            GENERATE_OPERATION,
            GENERATE_SCOPE,
        ),
        (
            Stream,
            "rumi_ai_gateway_pack.ai-gateway.stream",
            STREAM_OPERATION,
            STREAM_SCOPE,
        ),
    ] {
        let pin = resolve
            .pin(role)
            .expect("each role has its own signed grant");
        assert_eq!(pin.caller_function_id(), caller);
        assert_eq!(pin.pack_id(), "rumi_model_registry_pack");
        assert_eq!(
            pin.function_id(),
            "rumi_model_registry_pack.model-registry.profile"
        );
        assert_eq!(
            pin.variant_id(),
            "rumi_model_registry_pack.model-registry.profile.python"
        );
        assert_eq!(pin.contract_id(), "tobkiri.resource.ai.model.profile.v1");
        assert_eq!(pin.operation_id(), operation);
        assert_eq!(pin.requested_scope_digest(), scope);
        assert!(authorities.insert(pin.authority_reference()));
    }
}

fn seed_successor_catalog(original: &Path, successor: &Path) {
    fs::create_dir(successor).unwrap();
    let revision = Value::from(Digest::hash(b"successor shell definition").to_string());
    // Retain the physical artifact and Pack pin while independently changing
    // the bundled Shell definition revision that triggers reconfirmation.
    for (name, shell_in_profile) in [("reread-source.json", true), ("reread-shell.json", false)] {
        let mut record: Value =
            serde_json::from_slice(&fs::read(original.join(name)).unwrap()).unwrap();
        let shell = if shell_in_profile {
            &mut record["shell"]
        } else {
            &mut record
        };
        shell["definition_revision"] = revision.clone();
        fs::write(successor.join(name), serde_json::to_vec(&record).unwrap()).unwrap();
    }
    fs::copy(
        original.join("reread-pack.json"),
        successor.join("reread-pack.json"),
    )
    .unwrap();
}

fn read_registered(
    owner: &Source,
    witness: &Path,
    artifacts: &Path,
    check: impl FnOnce(RegisteredCatalogPolicy, &Arc<dyn AllocationBudget>),
) -> Result<()> {
    with_existing_setup_source(owner, |owner, listing, cancellation, budget| {
        for discard in [true, false] {
            assert_eq!(
                listing
                    .enter_recovery(owner, cancellation, discard)
                    .unwrap(),
                Prefix::NoJournal
            );
        }
        listing.acquire_candidate_root(owner, cancellation).unwrap();
        assert!(matches!(
            listing
                .candidate_selection(owner, cancellation, "defaults", false, budget)
                .unwrap(),
            CandidateSelection::ActiveAuthority
        ));
        let op = operation(listing, owner, cancellation);
        let loader = Mutex::new(Some(
            initial_catalog_fixture::load(&op, budget, witness, artifacts).unwrap(),
        ));
        let outcome = ready_test(listing.review_registered_activation(
            owner,
            cancellation,
            budget,
            &loader,
            Some(workspace_active::tests::bound_recovery_schemas()),
        ));
        assert!(listing.receipt().verified());
        let RegisteredSnapshotReached::CatalogPolicy(records) = outcome? else {
            panic!("expected the authenticated catalog policy inputs");
        };
        // This publication fixture exports only the Shell Pack manifest.
        // That retained pin cannot substitute for the missing active closure.
        assert_eq!(
            listing
                .review_registered_catalog(owner, cancellation, budget, &loader, &records)
                .unwrap(),
            RegisteredCatalogSelection::AdmittedClosure
        );
        check(records, budget);
        listing.close_continuation()
    })
}

fn read_post_resolve_predecessor(
    owner: &Source,
    resolved_source: &Path,
    witness: &Path,
    artifacts: &Path,
) -> Result<bool> {
    with_existing_setup_source(owner, |owner, listing, cancellation, budget| {
        for discard in [true, false] {
            assert_eq!(
                listing.enter_recovery(owner, cancellation, discard)?,
                Prefix::NoJournal
            );
        }
        listing.acquire_candidate_root(owner, cancellation)?;
        assert!(matches!(
            listing.candidate_selection(owner, cancellation, "defaults", false, budget)?,
            CandidateSelection::ActiveAuthority
        ));
        // This fixture starts after the earlier resolved candidate. In particular
        // it allows the Host pointer to change independently of the active graph.
        listing.enter_registered_candidate()?;
        listing.select_registered_resolver_catalog()?;
        listing.acquire_resolver_root(owner, cancellation)?;
        assert!(
            listing
                .resolver_authority_snapshot(owner, cancellation, Digest::hash(b"resolved bundle"))?
                .is_some()
        );
        let definition = load_document(resolved_source, budget);
        let source_digest = definition.digest_guest(budget).unwrap();
        let inputs =
            listing.bootstrap_predecessor_inputs(owner, cancellation, "defaults", budget)?;
        assert!(
            inputs.needs_source_digest("defaults") && inputs.matches_source_digest(&source_digest)
        );
        let op = operation(listing, owner, cancellation);
        let loader = Mutex::new(Some(initial_catalog_fixture::resolved_review(
            &op,
            budget,
            witness,
            artifacts,
            &definition,
        )?));
        let outcome = ready_test(listing.review_predecessor_activation(
            owner,
            cancellation,
            budget,
            PredecessorActivation {
                inputs: &inputs,
                source_digest: &source_digest,
                profile_id: "defaults",
                loader: &loader,
                schemas: Some(workspace_active::tests::bound_recovery_schemas()),
            },
        ));
        assert!(listing.receipt().verified());
        if outcome.is_ok() {
            assert!(!listing.inactive_predecessor_has_registry(owner, cancellation, "defaults")?);
        }
        assert!(lock_free(
            &owner.root.join("authority/.v4.sqlite3.lifecycle.lock")
        ));
        listing.close_continuation()?;
        outcome.map(|reached| matches!(reached, PredecessorActivationReached::Binding(None)))
    })
}

fn replace_host_selection(root: &Path, predecessor: &[u8], budget: &Arc<dyn AllocationBudget>) {
    let mut pointer: Value = serde_json::from_slice(predecessor).unwrap();
    let prior = root.join(pointer["activation_snapshot_path"].as_str().unwrap());
    let mut snapshot: Value = serde_json::from_slice(&fs::read(prior).unwrap()).unwrap();
    let other_id = "activation:defaults-other";
    snapshot["activation"]["activation_id"] = Value::from(other_id);
    let relative = "workspaces/defaults/activation/activations/defaults-other.json";
    let payload = serde_json::to_vec(&snapshot).unwrap();
    let digest = |payload: &[u8]| {
        CoreValue::parse_guest(payload, budget)
            .unwrap()
            .digest_guest(budget)
            .unwrap()
            .to_string()
    };
    let path = root.join(relative);
    fs::write(&path, &payload).unwrap();
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    pointer["activation_id"] = Value::from(other_id);
    pointer["activation_snapshot_path"] = Value::from(relative);
    pointer["activation_snapshot_digest"] = Value::from(digest(&payload));
    pointer.as_object_mut().unwrap().remove("pointer_digest");
    pointer["pointer_digest"] = Value::from(digest(&serde_json::to_vec(&pointer).unwrap()));
    fs::write(
        root.join("profiles/active.json"),
        serde_json::to_vec(&pointer).unwrap(),
    )
    .unwrap();
}

fn load_document(path: &Path, budget: &Arc<dyn AllocationBudget>) -> CoreValue {
    let data = fs::read(path).unwrap();
    assert!(data.len() < tobkiri_canon::MAX_BYTES);
    CoreValue::parse_guest(&data, budget).unwrap()
}

fn seed(root: &Path, source: &Path, predecessor: &Value, definition_exists: bool) {
    let copy = |relative: &Path| {
        assert!(
            relative
                .components()
                .all(|segment| matches!(segment, std::path::Component::Normal(_)))
        );
        let target = root.join(relative);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::copy(source.join(relative), target).unwrap();
    };
    for name in [
        "development-host-handover.key",
        "development-host-handover.json",
    ] {
        copy(Path::new(name));
    }
    // The original oracle stops before registration. Its prior-pointer case
    // can also exercise ordinary create with no stored definition for defaults.
    if definition_exists {
        copy(Path::new("profiles/index.json"));
    }
    if let Some(record) = predecessor["activation_snapshot_path"].as_str() {
        copy(Path::new("profiles/active.json"));
        assert!(record.starts_with("workspaces/prior-profile/activation/activations/"));
        copy(Path::new(record));
    }
}
