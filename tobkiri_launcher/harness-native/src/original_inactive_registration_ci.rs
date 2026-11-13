//! Exercise the original Host registry transaction with CI-owned fixtures.
//! The native Setup listing still grants no activation or write authority.
use super::*;
use tobkiri_canon::{CoreBuffer, CoreValue};

fn stale() -> OriginalAuthorityError {
    raw(
        definitions_model::CONFLICT,
        "inactive bootstrap predecessor is stale",
    )
}

fn revalidate<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store,
    source: &CoreValue,
    expected: &CoreValue,
    profile_id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let root = &store.user_data;
    let snapshot = store.snapshot(op, budget)?;
    let _paths = op.reserve(
        OriginalAuthorityStage::Path,
        root.as_os_str()
            .len()
            .saturating_mul(4)
            .saturating_add(profile_id.len())
            .saturating_add(8192),
    )?;
    if candidate_selection::inactive_has_active_profile(op, root, profile_id)? {
        return Err(candidate_selection::active_predecessor_conflict());
    }
    if candidate_selection::inactive_has_active_profile(op, root, profile_id)?
        || !candidate_selection::predecessor_path_present(op, &root.join("profiles/index.json"))?
    {
        return Err(stale());
    }
    let mut registry =
        inactive_registry_from_snapshot(op, snapshot, profile_id, budget)?.ok_or_else(stale)?;
    registry.serialize_definition(op, budget)?;
    let existing = CoreValue::parse_guest(registry.definition(), budget).map_err(capacity)?;
    drop(registry.serialized_definition.take());
    if python_equal(&existing, source, budget)? {
        return Err(stale());
    }
    drop(existing);
    let actual =
        candidate_selection::inactive_handover_at(op, root, profile_id, &mut registry, budget)?
            .ok_or_else(stale)?;
    if !actual.eq_guest(expected, budget).map_err(capacity)? {
        return Err(stale());
    }
    Ok(())
}

fn capacity(_: tobkiri_canon::AllocationError) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}

// Both inputs were admitted as strict, depth-bounded JSON. Object equality
// ignores key order and the original Python allows bool/int equality.
fn python_equal(
    left: &CoreValue,
    right: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    use tobkiri_canon::CoreRef;
    budget
        .reserve(tobkiri_canon::AllocationRequest {
            kind: tobkiri_canon::AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: 1,
        })
        .map_err(capacity)?;
    Ok(match (left.inspect(), right.inspect()) {
        (CoreRef::Int(a), CoreRef::Bool(b)) | (CoreRef::Bool(b), CoreRef::Int(a)) => {
            a == i64::from(b)
        }
        (CoreRef::Arr(a), CoreRef::Arr(b)) => {
            if a.len() != b.len() {
                return Ok(false);
            }
            for (left, right) in a.iter().zip(b.iter()) {
                if !python_equal(&left, &right, budget)? {
                    return Ok(false);
                }
            }
            true
        }
        (CoreRef::Obj(a), CoreRef::Obj(b)) => {
            if a.len() != b.len() {
                return Ok(false);
            }
            for ((ak, av), (bk, bv)) in a.iter().zip(b.iter()) {
                budget
                    .reserve(tobkiri_canon::AllocationRequest {
                        kind: tobkiri_canon::AllocationKind::Temporary,
                        bytes: 0,
                        elements: 0,
                        depth: 0,
                        work: ak.len() as u64,
                    })
                    .map_err(capacity)?;
                if ak != bk || !python_equal(&av, &bv, budget)? {
                    return Ok(false);
                }
            }
            true
        }
        _ => left.eq_guest(right, budget).map_err(capacity)?,
    })
}

struct ReviewedRegistration<'a> {
    source: &'a CoreValue,
    predecessor: &'a CoreValue,
    approved_digest: &'a str,
    profile_id: &'a str,
    now: i64,
}

fn with_inactive_workspace<S: OriginalAuthorityOwner, T>(
    op: &Operation<'_, S>,
    root: &Path,
    profile_id: &str,
    register: impl FnOnce() -> Result<T>,
) -> Result<T> {
    let _paths = op.reserve(
        OriginalAuthorityStage::Path,
        root.as_os_str()
            .len()
            .saturating_mul(4)
            .saturating_add(profile_id.len())
            .saturating_add(8192),
    )?;
    let directory = Directory::capture(
        op,
        &root.join("workspaces").join(profile_id).join("activation"),
        false,
    )?;
    let name = publication_io::LockName::workspace(profile_id);
    let mut lock = PublicationLock::acquire_named(op, &directory, name)?;
    let result = (|| {
        if directory.exists(op, "active.json")? {
            return Err(raw(
                "ecosystem.defaultspack.domain.profile_resolution.ProfileResolutionDenied",
                "inactive bootstrap predecessor has an active workspace",
            ));
        }
        let result = register()?;
        directory.validate_file(op, name.text(), lock.file.as_ref().expect("workspace lock"))?;
        Ok(result)
    })();
    finish_scope(result, lock.close())
}

fn with_registration<S: OriginalAuthorityOwner, T>(
    op: &Operation<'_, S>,
    root: &Path,
    review: &ReviewedRegistration<'_>,
    budget: &Arc<dyn AllocationBudget>,
    before_cas: impl FnOnce(),
    register: impl FnOnce(&Store, CoreBuffer) -> Result<T>,
) -> Result<T> {
    let store = Store::new(op, root)?;
    let selection_dir = Directory::capture(op, &root.join("profiles"), false)?;
    let selection = publication_io::LockName::HostSelection;
    let mut host = PublicationLock::acquire_named(op, &selection_dir, selection)?;
    let result = (|| {
        revalidate(
            op,
            &store,
            review.source,
            review.predecessor,
            review.profile_id,
            budget,
        )?;
        let missing = || OriginalAuthorityError::unavailable();
        let definition_digest = review
            .predecessor
            .get_key("definition_digest")
            .ok_or_else(missing)?;
        if definition_digest.as_str() != Some(review.approved_digest) {
            return Err(raw(
                definitions_model::CONFLICT,
                "inactive bootstrap predecessor approval is mismatched",
            ));
        }
        let generation = review
            .predecessor
            .get_key("store_generation")
            .and_then(|value| value.as_i64())
            .ok_or_else(missing)?;
        let revision = review
            .predecessor
            .get_key("profile_revision")
            .ok_or_else(missing)?;
        let revision = revision.as_str().ok_or_else(missing)?;
        before_cas();
        let mut definition = store.lock(op)?;
        let result = (|| {
            let mut state = store.read_state(op, budget)?;
            let entry = definitions_model::require_registration_cas(
                op,
                &mut state,
                review.profile_id,
                generation,
                revision,
                budget,
            )?;
            let output = definitions_model::prepare_registration(
                op,
                &state,
                entry,
                review.source,
                review.now,
                budget,
            )?;
            register(&store, output)
        })();
        finish_snapshot(op, &store.directory, &mut definition, result)
    })();
    let result = result.and_then(|value| {
        selection_dir.validate_file(
            op,
            selection.text(),
            host.file.as_ref().expect("Host selection lock"),
        )?;
        Ok(value)
    });
    finish_scope(result, host.close())
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires fresh pinned original registration fixtures"]
async fn original_inactive_registration_driver() {
    use serde_json::{Value, json};
    use std::os::unix::fs::symlink;
    use tests::{digest, lock_free, operation, with_source};

    let path = PathBuf::from(
        std::env::var_os("TOBKIRI_ORIGINAL_INACTIVE_REGISTRY_REFERENCE")
            .expect("fresh original inactive registry fixture"),
    );
    let input = path.with_file_name("inactive-registry-inputs");
    let fixture: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    assert_eq!(
        fixture["format"],
        "io.tobkiri.original-inactive-registry-reference.v1"
    );
    assert_eq!(
        fixture["original_head"],
        "1cf025f1eeab31a3daa2861b1f26a025c6954b6e"
    );
    let reviewed = fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["case"] == "authenticated-handover")
        .unwrap();
    let cases = reviewed["registration_cases"].as_array().unwrap();
    assert_eq!(cases.len(), 4);
    let packaged = fs::read(input.join("source-profile.json")).unwrap();
    for case in [
        "append",
        "registry-edited",
        "host-active",
        "workspace-active",
        "workspace-regular-pointer",
        "workspace-dangling-pointer",
        "late-registry-edit",
        "wrong-approval-digest",
    ] {
        with_source(|owner, read, cancel, budget| {
            let op = operation(read, owner, cancel);
            let pinned_case = if matches!(
                case,
                "late-registry-edit"
                    | "wrong-approval-digest"
                    | "workspace-regular-pointer"
                    | "workspace-dangling-pointer"
            ) {
                "append"
            } else {
                case
            };
            let row = cases.iter().find(|row| row["case"] == pinned_case).unwrap();
            let seed = input.join(format!("register-input-{pinned_case}/user-data"));
            let root = &owner.root;
            let profiles = root.join("profiles");
            fs::create_dir_all(&profiles).unwrap();
            for relative in [
                "profiles/index.json",
                development_handover::JOURNAL,
                development_handover::KEY,
            ] {
                fs::copy(seed.join(relative), root.join(relative)).unwrap();
            }
            let document = CoreValue::parse_guest(&packaged, budget).unwrap();
            let profile = document.get_key("profile_id").unwrap();
            let profile_id = profile.as_str().unwrap();
            let workspace = root.join("workspaces").join(profile_id).join("activation");
            let guarded = matches!(
                case,
                "append" | "workspace-regular-pointer" | "workspace-dangling-pointer"
            );
            if guarded {
                fs::create_dir_all(&workspace).unwrap();
            }
            let active = match case {
                "host-active" => Some(profiles.join("active.json")),
                "workspace-active" | "workspace-regular-pointer" | "workspace-dangling-pointer" => {
                    Some(workspace.join("active.json"))
                }
                _ => None,
            };
            if let Some(path) = &active {
                fs::create_dir_all(path.parent().unwrap()).unwrap();
                if case == "workspace-regular-pointer" {
                    fs::write(path, b"{}\n").unwrap();
                } else {
                    symlink("missing-fixture-pointer", path).unwrap();
                }
            }
            let predecessor = CoreValue::parse_guest(
                &serde_json::to_vec(&reviewed["expected"]["predecessor"]).unwrap(),
                budget,
            )
            .unwrap();
            let definition_digest = predecessor.get_key("definition_digest").unwrap();
            let approved_digest = if case == "wrong-approval-digest" {
                "mismatched"
            } else {
                definition_digest.as_str().unwrap()
            };
            let review = ReviewedRegistration {
                source: &document,
                predecessor: &predecessor,
                approved_digest,
                profile_id,
                now: row["registration_time"].as_i64().unwrap(),
            };
            let index = profiles.join(INDEX);
            let before = fs::read(&index).unwrap();
            let before_state: Value = serde_json::from_slice(&before).unwrap();
            assert_eq!(row["previous_snapshot_digest"], digest(&before_state));
            let workspace_name = publication_io::LockName::workspace(profile_id);
            let workspace_lock = workspace.join(workspace_name.text());
            let register = || {
                with_registration(
                    &op,
                    root,
                    &review,
                    budget,
                    || {
                        let host_lock = profiles.join(".active-profile.lock");
                        assert!(!lock_free(&host_lock));
                        let definition_lock = profiles.join(".profile-definition.lock");
                        assert!(lock_free(&definition_lock));
                        if case == "late-registry-edit" {
                            let path = input.join(
                                "register-input-registry-edited/user-data/profiles/index.json",
                            );
                            let store = Store::new(&op, root).unwrap();
                            let mut lock = store.lock(&op).unwrap();
                            store
                                .directory
                                .replace_definition(&op, &fs::read(path).unwrap())
                                .unwrap();
                            lock.close().unwrap();
                        }
                    },
                    |store, output| {
                        assert_eq!(case, "append");
                        assert!(!lock_free(&workspace_lock));
                        assert!(!lock_free(&profiles.join(".active-profile.lock")));
                        assert!(!lock_free(&profiles.join(".profile-definition.lock")));
                        store.directory.replace_definition(&op, output.as_bytes())
                    },
                )
            };
            let result = if guarded {
                with_inactive_workspace(&op, root, profile_id, register)
            } else {
                register()
            };
            let after = fs::read(&index).unwrap();
            match case {
                "append" => {
                    result.unwrap();
                    let after_state: Value = serde_json::from_slice(&after).unwrap();
                    let row_after = &after_state["profiles"][0];
                    let history = row_after["revisions"].as_array().unwrap();
                    let current = history.last().unwrap();
                    assert_eq!(
                        row["expected"],
                        json!({
                            "outcome": "appended", "generation": after_state["generation"],
                            "parent_revision": current["parent_revision"],
                            "profile_revision": current["profile_revision"],
                            "history_length": history.len(), "snapshot_digest": digest(&after_state),
                        })
                    );
                    assert_eq!(
                        after,
                        fs::read(input.join("register-append/user-data/profiles/index.json"))
                            .unwrap()
                    );
                }
                "late-registry-edit" => {
                    assert_eq!(
                        result.unwrap_err().message,
                        "Profile store generation is stale"
                    );
                    assert_eq!(
                        after,
                        fs::read(
                            input.join(
                                "register-input-registry-edited/user-data/profiles/index.json"
                            )
                        )
                        .unwrap()
                    );
                }
                "wrong-approval-digest" => {
                    assert_eq!(
                        result.unwrap_err().message,
                        "inactive bootstrap predecessor approval is mismatched"
                    );
                    assert_eq!(after, before);
                }
                "workspace-regular-pointer" | "workspace-dangling-pointer" => {
                    let message = if case == "workspace-regular-pointer" {
                        "inactive bootstrap predecessor has an active workspace"
                    } else {
                        "persistence entry is symlinked"
                    };
                    assert_eq!(result.unwrap_err().message, message);
                    assert_eq!(after, before);
                }
                _ => {
                    assert_eq!(
                        row["expected"],
                        reference_ci::original_error(&result.unwrap_err())
                    );
                    assert_eq!(after, before);
                }
            }
            if guarded {
                assert!(lock_free(&workspace_lock));
            }
            if matches!(
                case,
                "workspace-regular-pointer" | "workspace-dangling-pointer"
            ) {
                assert_eq!(fs::read_dir(&profiles).unwrap().count(), 1);
            } else {
                assert!(lock_free(&profiles.join(".active-profile.lock")));
                assert!(lock_free(&profiles.join(".profile-definition.lock")));
                assert_eq!(
                    fs::read_dir(&profiles).unwrap().count(),
                    3 + usize::from(case == "host-active")
                );
            }
        });
    }
}
