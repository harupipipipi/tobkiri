//! Exercise the original Host registry transaction with CI-owned fixtures.
use super::*;
use bootstrap_registration::{ReviewedRegistration, register_inactive};

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
            };
            let index = profiles.join(INDEX);
            let before = fs::read(&index).unwrap();
            let before_state: Value = serde_json::from_slice(&before).unwrap();
            assert_eq!(row["previous_snapshot_digest"], digest(&before_state));
            let workspace_name = publication_io::LockName::workspace(profile_id);
            let workspace_lock = workspace.join(workspace_name.text());
            let register = || {
                register_inactive(
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
                    || {
                        assert_eq!(case, "append");
                        assert!(!lock_free(&workspace_lock));
                        assert!(!lock_free(&profiles.join(".active-profile.lock")));
                        assert!(!lock_free(&profiles.join(".profile-definition.lock")));
                        Ok(row["registration_time"].as_i64().unwrap())
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
