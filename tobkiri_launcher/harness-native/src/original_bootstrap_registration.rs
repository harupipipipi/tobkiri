//! Register the reread bundled Profile for confirmed Setup without a predecessor.
use super::*;
use definitions_model::{
    CONFLICT, FirstRevision, State, bootstrap_template_id, find_entry,
    normalize_bootstrap_template, prepare_first_revision, prepare_registration_with_clock,
    python_equal, registration_generation, require_registration_cas,
};
use tobkiri_canon::AllocationError;

fn register<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    user_data: &Path,
    source: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
    mut clock: impl FnMut() -> Result<i64>,
) -> Result<()> {
    let store = Store::new(op, user_data)?;
    let snapshot = store.snapshot(op, budget)?;
    let generation = registration_generation(&snapshot);
    let id = bootstrap_template_id(op, source, budget)?;
    let id_text = source
        .get_key("profile_id")
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let id_text = id_text
        .as_str()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let mut current = read_profile(op, &store, &id, budget)?;
    if current.is_none() {
        normalize_bootstrap_template(op, source, &id, budget)?;
        let now = timestamp(&mut clock)?;
        let created = store.update_definitions(op, budget, |state| {
            prepare_first_revision(
                op,
                state,
                source,
                &id,
                now,
                FirstRevision::Ordinary {
                    expected_generation: generation,
                },
                budget,
            )
            .map(|output| Some((1, output)))
        });
        match created {
            Ok(_) => return Ok(()),
            Err(conflict) if conflict.class == CONFLICT => {
                current = match read_profile(op, &store, &id, budget) {
                    Ok(Some(current)) => Some(current),
                    Ok(None) => return Err(conflict),
                    Err(mut error) => {
                        let mut prior = &mut error.context;
                        while let Some(previous) = prior {
                            prior = &mut previous.context;
                        }
                        *prior = Some(Box::new(conflict));
                        return Err(error);
                    }
                };
            }
            Err(error) => return Err(error),
        }
    }

    let (current, entry) = current.expect("registration entry was checked");
    let doc = &current.doc;
    let conflict = || {
        raw(
            CONFLICT,
            "bootstrap Profile conflicts with the existing definition",
        )
    };
    if !is_false(doc, field(doc, entry, "tombstone")) {
        return Err(conflict());
    }
    let revision = field(doc, entry, "current_revision");
    let version = *history(doc, entry)
        .last()
        .expect("validated revision history");
    let bytes = json::canonical_subtree(doc, field(doc, version, "profile"), None, op, budget)?;
    let existing = CoreValue::parse_guest(&bytes, budget).map_err(capacity)?;
    drop(bytes);
    if python_equal(&existing, source, budget)? {
        return Ok(());
    }
    drop(existing);
    let revision = definitions_model::render_python_str(op, budget, doc, revision)?;
    let revision = revision
        .as_str()
        .map_err(|_| OriginalAuthorityError::unavailable())?;
    drop(current);
    if !unmodified_template(op, &store, &snapshot, &id, id_text, revision, budget)? {
        return Err(conflict());
    }
    drop(snapshot);
    store.update_definitions(op, budget, |state| {
        let entry = require_registration_cas(op, state, id_text, generation, revision, budget)?;
        let output =
            prepare_registration_with_clock(op, state, entry, source, || timestamp(clock), budget)?;
        Ok(Some((1, output)))
    })?;
    Ok(())
}

fn read_profile<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store,
    profile_id: &Text,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<(State, usize)>> {
    // Each get_profile call takes its own snapshot, including tombstones.
    let state = store.snapshot(op, budget)?;
    let entry = find_entry(op, &state, profile_id, budget)?;
    Ok(entry.map(|entry| (state, entry)))
}

fn unmodified_template<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store,
    snapshot: &State,
    profile_id: &Text,
    id_text: &str,
    revision: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    let doc = &snapshot.doc;
    let bootstrap = field(doc, doc.root, "bootstrap");
    if !doc.string_is(field(doc, bootstrap, "state"), "template_available")
        || !doc.string_is(field(doc, bootstrap, "template_profile_revision"), revision)
    {
        return Ok(false);
    }
    let Some(entry) = find_entry(op, snapshot, profile_id, budget)? else {
        return Ok(false);
    };
    let [version] = history(doc, entry) else {
        return Ok(false);
    };
    if !is_false(doc, field(doc, entry, "tombstone"))
        || !doc.string_is(field(doc, entry, "current_revision"), revision)
        || !doc.string_is(field(doc, *version, "profile_revision"), revision)
        || !is_false(doc, field(doc, *version, "tombstone"))
    {
        return Ok(false);
    }
    let _paths = op.reserve(
        OriginalAuthorityStage::Path,
        store
            .user_data
            .as_os_str()
            .len()
            .saturating_mul(4)
            .saturating_add(id_text.len())
            .saturating_add(8192),
    )?;
    candidate_selection::inactive_has_active_profile(op, &store.user_data, id_text)
        .map(|active| !active)
}

fn timestamp(clock: impl FnOnce() -> Result<i64>) -> Result<i64> {
    let now = clock()?;
    if now < 0 {
        return Err(raw(STORE_ERROR, "clock returned a negative timestamp"));
    }
    Ok(now)
}

fn field(doc: &Document, node: usize, key: &str) -> usize {
    doc.get(node, key).expect("validated registry field")
}

fn is_false(doc: &Document, node: usize) -> bool {
    matches!(doc.node(node), Node::Scalar(CanonValue::Bool(false)))
}

fn history(doc: &Document, entry: usize) -> &[usize] {
    let Node::Array(history) = doc.node(field(doc, entry, "revisions")) else {
        unreachable!("validated revision array");
    };
    history
}

fn capacity(_: AllocationError) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::io::publication::definitions::tests as fixture;
    use serde_json::{Value, json};

    fn profile(value: &Value, budget: &Arc<dyn AllocationBudget>) -> CoreValue {
        CoreValue::parse_guest(&serde_json::to_vec(value).unwrap(), budget).unwrap()
    }

    fn register_data(
        source: &fixture::Source,
        read: &PublicationRead,
        cancellation: &CoreCancellation,
        budget: &Arc<dyn AllocationBudget>,
        document: &Value,
        clock: impl FnMut() -> Result<i64>,
    ) -> Result<()> {
        let op = fixture::operation(read, source, cancellation);
        let root = &source.root;
        let directory = Directory::capture(&op, &root.join("profiles"), true)?;
        let mut publication = PublicationLock::acquire(&op, &directory)?;
        let result = register(&op, root, &profile(document, budget), budget, clock);
        assert!(!fixture::lock_free(&root.join("profiles").join(LOCK)));
        assert!(fixture::lock_free(
            &root.join("profiles/.profile-definition.lock")
        ));
        finish_scope(result, publication.close())
    }

    #[tokio::test(flavor = "current_thread")]
    async fn creates_an_ordinary_definition_and_equal_replay_never_writes() {
        fixture::with_source(|source, read, cancel, budget| {
            let index = source.root.join("profiles/index.json");
            let before = fixture::state(vec![fixture::entry("other", 0)]);
            fixture::write_index(source, &fixture::sealed(before.clone()));
            let document = json!({
                "profile_id":"defaults", "display_name":"defaults", "v":[true, {"x":false}]
            });
            register_data(source, read, cancel, budget, &document, || Ok(23)).unwrap();
            let mut expected = before;
            expected["generation"] = json!(2);
            expected["updated_at"] = json!(23);
            let mut defaults = fixture::entry("defaults", 1);
            let digest = fixture::digest(&document);
            defaults["current_revision"] = json!(digest);
            defaults["created_at"] = json!(23);
            defaults["updated_at"] = json!(23);
            defaults["revisions"] = json!([{
                "profile_revision": digest, "parent_revision": null, "profile": document,
                "created_at": 23, "updated_at": 23, "tombstone": false
            }]);
            expected["profiles"].as_array_mut().unwrap().push(defaults);
            let mut expected_bytes = fixture::sealed(expected);
            expected_bytes.push(b'\n');
            assert_eq!(fs::read(&index).unwrap(), expected_bytes);

            let mut equal = document;
            equal["v"] = json!([1, {"x":0}]);
            register_data(source, read, cancel, budget, &equal, || {
                panic!("equal registered Profile must not write")
            })
            .unwrap();
            assert_eq!(fs::read(index).unwrap(), expected_bytes);
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn create_conflict_rereads_target_and_keeps_the_original_generation_error() {
        for target_exists in [false, true] {
            fixture::with_source(|source, read, cancel, budget| {
                let index = source.root.join("profiles/index.json");
                let first = fixture::state(vec![fixture::entry("other", 0)]);
                fixture::write_index(source, &fixture::sealed(first.clone()));
                let mut concurrent = first;
                concurrent["generation"] = json!(2);
                let inserted = if target_exists {
                    "defaults"
                } else {
                    "unrelated"
                };
                concurrent["profiles"]
                    .as_array_mut()
                    .unwrap()
                    .push(fixture::entry(inserted, 1));
                let bytes = fixture::sealed(concurrent);
                let mut attempts = 0;
                let result = register_data(
                    source,
                    read,
                    cancel,
                    budget,
                    &json!({"profile_id":"defaults", "display_name":"defaults"}),
                    || {
                        attempts += 1;
                        fixture::write_index(source, &bytes);
                        Ok(23)
                    },
                );
                if target_exists {
                    result.unwrap();
                } else {
                    let error = result.unwrap_err();
                    assert_eq!(error.class, CONFLICT);
                    assert_eq!(error.message, "Profile store generation is stale");
                }
                assert_eq!(attempts, 1);
                assert_eq!(fs::read(index).unwrap(), bytes);
            });
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn only_the_untouched_template_without_either_active_path_gets_an_update() {
        for active in [
            None,
            Some("profiles/active.json"),
            Some("workspaces/defaults/activation/active.json"),
        ] {
            fixture::with_source(|source, read, cancel, budget| {
                let index = source.root.join("profiles/index.json");
                let first = fixture::entry("defaults", 0);
                let old_digest = first["current_revision"].clone();
                let mut initial = fixture::state(vec![first.clone()]);
                initial["bootstrap"] = json!({
                    "state":"template_available", "template_profile_revision":old_digest
                });
                let before = fixture::sealed(initial);
                fixture::write_index(source, &before);
                if let Some(active) = active {
                    let path = source.root.join(active);
                    fs::create_dir_all(path.parent().unwrap()).unwrap();
                    std::os::unix::fs::symlink("missing", path).unwrap();
                }
                let changed = json!({"profile_id":"defaults", "display_name":"fresh"});
                let result = register_data(source, read, cancel, budget, &changed, || {
                    assert!(active.is_none(), "denied update sampled the clock");
                    Ok(42)
                });
                if active.is_some() {
                    assert_eq!(result.unwrap_err().class, CONFLICT);
                    assert_eq!(fs::read(index).unwrap(), before);
                    return;
                }
                result.unwrap();
                let actual: Value = serde_json::from_slice(&fs::read(&index).unwrap()).unwrap();
                assert_eq!(actual["generation"], 2);
                assert_eq!(actual["bootstrap"]["state"], "not_required");
                assert_eq!(
                    actual["bootstrap"]["template_profile_revision"],
                    Value::Null
                );
                let revisions = actual["profiles"][0]["revisions"].as_array().unwrap();
                assert_eq!(revisions.len(), 2);
                assert_eq!(revisions[0], first["revisions"][0]);
                assert_eq!(revisions[1]["parent_revision"], old_digest);
                assert_eq!(revisions[1]["profile"], changed);
                assert_eq!(revisions[1]["created_at"], 42);
            });
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn ordinary_edited_and_tombstoned_profiles_conflict_without_a_write() {
        for case in ["established", "template-history", "tombstone"] {
            fixture::with_source(|source, read, cancel, budget| {
                let mut first = fixture::entry("defaults", 0);
                if case == "tombstone" {
                    let digest = fixture::digest(&json!({
                        "schema":"io.tobkiri.profile-tombstone.v1",
                        "profile_id":"defaults", "parent_revision":null
                    }));
                    first["tombstone"] = json!(true);
                    first["current_revision"] = json!(digest);
                    first["revisions"][0]["tombstone"] = json!(true);
                    first["revisions"][0]["profile_revision"] = json!(digest);
                    first["revisions"][0]["profile"] = Value::Null;
                }
                let mut initial = fixture::state(vec![first]);
                if case == "template-history" {
                    let entry = &mut initial["profiles"][0];
                    let edited = json!({"profile_id":"defaults", "display_name":"edited"});
                    let digest = fixture::digest(&edited);
                    let parent = entry["current_revision"].clone();
                    entry["current_revision"] = json!(digest);
                    entry["revisions"].as_array_mut().unwrap().push(json!({
                        "profile_revision": digest, "parent_revision": parent,
                        "profile": edited, "created_at": 10, "updated_at": 10,
                        "tombstone": false
                    }));
                    initial["bootstrap"] = json!({
                        "state":"template_available", "template_profile_revision":digest
                    });
                }
                let before = fixture::sealed(initial);
                fixture::write_index(source, &before);
                let document = json!({"profile_id":"defaults", "display_name":"fresh"});
                let error = register_data(source, read, cancel, budget, &document, || {
                    panic!("registration conflict must not write")
                })
                .unwrap_err();
                assert_eq!(error.class, CONFLICT);
                assert_eq!(
                    error.message,
                    "bootstrap Profile conflicts with the existing definition"
                );
                assert_eq!(
                    fs::read(source.root.join("profiles/index.json")).unwrap(),
                    before
                );
            });
        }
    }
}
