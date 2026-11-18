//! Bind a Pack artifact-pin review to the independently verified registry and pointer.
use super::*;
use crate::original_authority::{catalog::CatalogIo, schema};
use tobkiri_canon::{AllocationKind, AllocationRequest, CoreRef};

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const PIN: &str = "artifact_digest";

impl BootstrapPredecessorInputs {
    pub(in crate::original_authority::io::publication) fn verified_pointer(
        &self,
    ) -> Option<&active_profile::LoadedPointer> {
        self.pointer.as_ref()
    }

    /// Project the first registry snapshot after the caller checks the active identity.
    pub(in crate::original_authority::io::publication) fn pack_pin_predecessor<
        S: crate::BootstrapReviewOwner,
    >(
        &self,
        op: &Operation<'_, S>,
        profile_id: &str,
        active_definition: &Digest,
        catalog: &CatalogIo,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<CoreValue>> {
        op.check(STAGE)?;
        let (Some(registered), Some(pointer)) = (&self.registered, self.verified_pointer()) else {
            return Ok(None);
        };
        let [id, revision, ..] = registered.profile.references;
        if !pointer.profile_id().is(profile_id)
            || !registered.state.doc.string_is(id, profile_id)
            || registered
                .state
                .doc
                .string_is(revision, active_definition.as_str())
        {
            return Ok(None);
        }
        let current = registered.review_profile(op, budget)?;
        let current_digest = current.digest_guest(budget).map_err(capacity)?;
        let Node::String(id) = registered.state.doc.node(id) else {
            unreachable!("verified registry Profile ID");
        };
        let Some(previous) = direct_predecessor(
            op,
            &self._snapshot,
            id,
            &current_digest,
            active_definition,
            budget,
        )?
        else {
            return Ok(None);
        };
        if !only_provider_pins_changed(op, &previous, &current, budget, |id, digest| {
            catalog.bundled_review_provider_pin_matches(id, digest)
        })? {
            return Ok(None);
        }
        project_binding(
            op,
            pointer,
            &self._snapshot,
            profile_id,
            active_definition,
            &current_digest,
            budget,
        )
        .map(Some)
    }
}

fn direct_predecessor<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    snapshot: &definitions_model::State,
    profile_id: &Text,
    current: &Digest,
    previous: &Digest,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<CoreValue>> {
    let Some(entry) = definitions_model::find_entry(op, snapshot, profile_id, budget)? else {
        return Ok(None);
    };
    let doc = &snapshot.doc;
    let field = |object, key| doc.get(object, key).expect("validated registry field");
    if !matches!(
        doc.node(field(entry, "tombstone")),
        Node::Scalar(CanonValue::Bool(false))
    ) || !doc.string_is(field(entry, "current_revision"), current.as_str())
    {
        return Ok(None);
    }
    let Node::Array(revisions) = doc.node(field(entry, "revisions")) else {
        unreachable!("validated registry history");
    };
    let [.., parent, latest] = revisions.as_slice() else {
        return Ok(None);
    };
    if !doc.string_is(field(*latest, "parent_revision"), previous.as_str()) {
        return Ok(None);
    }
    // A valid history may return to a definition seen before. Both digests
    // must name exactly one row, even when the latest parent already matches.
    for &revision in &revisions[..revisions.len() - 2] {
        op.check(STAGE)?;
        charge(budget, 2 * current.as_str().len())?;
        let digest = field(revision, "profile_revision");
        if doc.string_is(digest, current.as_str()) || doc.string_is(digest, previous.as_str()) {
            return Ok(None);
        }
    }
    let profile = field(*parent, "profile");
    if doc.entries(profile).is_none() {
        return Ok(None);
    }
    let bytes = json::canonical_subtree(doc, profile, None, op, budget)?;
    if &Digest::hash(&bytes) != previous {
        return Ok(None);
    }
    CoreValue::parse_guest(&bytes, budget)
        .map(Some)
        .map_err(capacity)
}

fn only_provider_pins_changed<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    previous: &CoreValue,
    current: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
    matches_catalog: impl Fn(&str, &str) -> Result<bool>,
) -> Result<bool> {
    let (Some(old_packs), Some(new_packs)) = (previous.get_key("packs"), current.get_key("packs"))
    else {
        return Ok(false);
    };
    let (Some(old_packs), Some(new_packs)) = (old_packs.array(), new_packs.array()) else {
        return Ok(false);
    };
    if old_packs.len() != new_packs.len() {
        return Ok(false);
    }
    let excluded = ["base", "shell"].map(|role| {
        previous
            .get_key(role)
            .and_then(|pack| pack.get_key("pack_id"))
    });
    let mut changed = false;
    for (old, new) in old_packs.iter().zip(new_packs.iter()) {
        op.check(STAGE)?;
        if old.object().is_none()
            || new.object().is_none()
            || !equal_except(&old, &new, PIN, budget)?
        {
            return Ok(false);
        }
        let old_pin = old.get_key(PIN);
        let new_pin = new.get_key(PIN);
        let same_pin = match (&old_pin, &new_pin) {
            (Some(old), Some(new)) => python_equal(old, new, budget)?,
            (Some(value), None) | (None, Some(value)) => matches!(value.inspect(), CoreRef::Null),
            (None, None) => true,
        };
        if same_pin {
            continue;
        }
        let Some(id) = new.get_key("pack_id") else {
            return Ok(false);
        };
        let (Some(id), Some(digest)) = (id.as_str(), new_pin.as_ref().and_then(CoreValue::as_str))
        else {
            return Ok(false);
        };
        if new
            .get_key("role")
            .is_some_and(|role| role.as_str() != Some("provider"))
            || excluded
                .iter()
                .flatten()
                .any(|value| value.as_str() == Some(id))
            || old_pin.as_ref().and_then(CoreValue::as_str).is_none()
            || !digest.starts_with("sha256:")
            || !schema::valid_digest(digest)
            || !matches_catalog(id, digest)?
        {
            return Ok(false);
        }
        changed = true;
    }
    Ok(changed && equal_except(previous, current, "packs", budget)?)
}

fn project_binding<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    pointer: &active_profile::LoadedPointer,
    snapshot: &definitions_model::State,
    profile_id: &str,
    active_definition: &Digest,
    current: &Digest,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let doc = &snapshot.doc;
    let bytes = json::canonical_subtree(doc, doc.root, None, op, budget)?;
    if bytes.len() > tobkiri_canon::MAX_BYTES {
        return Err(raw(
            "tobkiri_protocol.errors.CanonicalizationError",
            "canonical JSON exceeds size limit",
        ));
    }
    let snapshot_digest = Digest::hash(&bytes);
    drop(bytes);
    let pointer_value = |key| {
        let doc = &pointer.doc;
        let index = doc.get(doc.root, key).expect("verified pointer field");
        let bytes = json::canonical_subtree(doc, index, None, op, budget)?;
        CoreValue::parse_guest(&bytes, budget).map_err(capacity)
    };
    let string = |value| CoreValue::string(value, budget).map_err(capacity);
    CoreValue::object_shared(
        [
            (
                "schema",
                string("io.tobkiri.defaults-pack-pin-predecessor.v1")?,
            ),
            ("pointer_digest", pointer_value("pointer_digest")?),
            ("profile_id", string(profile_id)?),
            ("profile_revision", pointer_value("profile_revision")?),
            ("activation_id", pointer_value("activation_id")?),
            ("plan_digest", pointer_value("plan_digest")?),
            ("lock_digest", pointer_value("lock_digest")?),
            ("generation", pointer_value("generation")?),
            (
                "active_definition_digest",
                string(active_definition.as_str())?,
            ),
            ("registry_profile_revision", string(current.as_str())?),
            (
                "registry_store_generation",
                CoreValue::int(definitions_model::registration_generation(snapshot), budget)
                    .map_err(capacity)?,
            ),
            (
                "registry_snapshot_digest",
                string(snapshot_digest.as_str())?,
            ),
        ]
        .into_iter()
        .map(Ok),
        budget,
    )
    .map_err(capacity)
}

pub(in crate::original_authority::io::publication) fn equal_except(
    left: &CoreValue,
    right: &CoreValue,
    omitted: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    let left = left
        .object()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let right = right
        .object()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let count = |object: &tobkiri_canon::CoreObject<'_>| {
        object.len() - usize::from(object.get(omitted).is_some())
    };
    if count(&left) != count(&right) {
        return Ok(false);
    }
    let left = left.iter().filter(|(key, _)| *key != omitted);
    let right = right.iter().filter(|(key, _)| *key != omitted);
    for ((left_key, left), (right_key, right)) in left.zip(right) {
        charge(budget, left_key.len() + 1)?;
        if left_key != right_key || !python_equal(&left, &right, budget)? {
            return Ok(false);
        }
    }
    Ok(true)
}

fn charge(budget: &Arc<dyn AllocationBudget>, work: usize) -> Result<()> {
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: work as u64,
        })
        .map_err(capacity)
}

fn capacity(_: impl Sized) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(STAGE)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{
        digest, entry, operation, sealed, state, with_source,
    };
    use serde_json::{Value, json};

    const OLD: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const NEW: &str = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    fn parse(value: &Value, budget: &Arc<dyn AllocationBudget>) -> CoreValue {
        CoreValue::parse_guest(&serde_json::to_vec(value).unwrap(), budget).unwrap()
    }

    fn profile(pin: &str) -> Value {
        json!({
            "profile_id": "defaults",
            "base": {"pack_id": "local.base"},
            "shell": {"pack_id": "local.shell"},
            "packs": [
                {"pack_id": "local.provider", "artifact_digest": pin, "meta": {"scope": true}},
                {"pack_id": "local.other", "artifact_digest": OLD},
            ],
        })
    }

    #[tokio::test(flavor = "current_thread")]
    async fn pin_only_comparison_preserves_order_roles_and_python_field_equality() {
        with_source(|owner, read, cancellation, budget| {
            let op = operation(read, owner, cancellation);
            let old = profile(OLD);
            let mut new = profile(NEW);
            new["packs"][0]["meta"]["scope"] = json!(1);
            let exact = |left: &Value, right: &Value, catalog_digest: &str| {
                only_provider_pins_changed(
                    &op,
                    &parse(left, budget),
                    &parse(right, budget),
                    budget,
                    |id, digest| Ok(id == "local.provider" && digest == catalog_digest),
                )
                .unwrap()
            };
            assert!(exact(&old, &new, NEW));
            assert!(!exact(&old, &new, OLD));
            assert!(!exact(&old, &old, OLD));
            let mut reordered = new.clone();
            reordered["packs"].as_array_mut().unwrap().swap(0, 1);
            assert!(!exact(&old, &reordered, NEW));
            let mut unpinned = old.clone();
            unpinned["packs"][0]["artifact_digest"] = Value::Null;
            assert!(!exact(&unpinned, &new, NEW));
            let mut old_shell = old.clone();
            old_shell["shell"]["pack_id"] = json!("local.provider");
            let mut new_shell = new.clone();
            new_shell["shell"]["pack_id"] = json!("local.provider");
            assert!(!exact(&old_shell, &new_shell, NEW));
            let mut null_role_old = old;
            null_role_old["packs"][0]["role"] = Value::Null;
            let mut null_role_new = new;
            null_role_new["packs"][0]["role"] = Value::Null;
            assert!(!exact(&null_role_old, &null_role_new, NEW));
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn verified_history_rejects_repeated_parent_and_current_revisions() {
        with_source(|owner, read, cancellation, budget| {
            let op = operation(read, owner, cancellation);
            let old = profile(OLD);
            let new = profile(NEW);
            let old_digest = Digest::parse(&digest(&old)).unwrap();
            let new_digest = Digest::parse(&digest(&new)).unwrap();
            let profile_id = Text::from_str("defaults");
            let intermediate = json!({"profile_id":"defaults", "display_name":"intermediate"});
            let verify = |history: &[&Value]| {
                let mut row = entry("defaults", 0);
                let mut parent = None;
                let revisions = history
                    .iter()
                    .map(|profile| {
                        let revision = digest(profile);
                        let value = json!({
                            "profile_revision": revision,
                            "parent_revision": parent,
                            "profile": profile,
                            "created_at": 10,
                            "updated_at": 10,
                            "tombstone": false,
                        });
                        parent = Some(revision);
                        value
                    })
                    .collect::<Vec<_>>();
                row["current_revision"] = json!(parent);
                row["revisions"] = json!(revisions);
                let bytes = sealed(state(vec![row]));
                let snapshot = definitions_model::load(&op, Some(&bytes), budget).unwrap();
                direct_predecessor(
                    &op,
                    &snapshot,
                    &profile_id,
                    &new_digest,
                    &old_digest,
                    budget,
                )
                .unwrap()
                .is_some()
            };
            assert!(verify(&[&old, &new]));
            assert!(!verify(&[&old, &intermediate, &old, &new]));
            assert!(!verify(&[&new, &old, &new]));
            assert!(!verify(&[&old, &intermediate, &new]));
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn binding_projects_the_complete_first_snapshot_and_verified_pointer() {
        with_source(|owner, read, cancellation, budget| {
            let op = operation(read, owner, cancellation);
            let registry = sealed(state(vec![entry("defaults", 0)]));
            let registry_json: Value = serde_json::from_slice(&registry).unwrap();
            let snapshot = definitions_model::load(&op, Some(&registry), budget).unwrap();
            let old = Digest::parse(&digest(&profile(OLD))).unwrap();
            let new = Digest::parse(&digest(&profile(NEW))).unwrap();
            let pointer_revision = digest(&json!("resolved revision"));
            let plan = digest(&json!("plan"));
            let lock = digest(&json!("lock"));
            let mut pointer_json = json!({
                "schema": "io.tobkiri.active-profile-pointer.v1", "profile_id": "defaults",
                "profile_revision": pointer_revision, "activation_id": "activation:defaults-fixture",
                "plan_digest": plan, "lock_digest": lock, "catalog_revision": null,
                "activation_snapshot_path": "workspaces/defaults/activation/activations/defaults-fixture.json",
                "activation_snapshot_digest": OLD, "generation": 9, "updated_at": 10,
            });
            pointer_json["pointer_digest"] = json!(digest(&pointer_json));
            let pointer_bytes = serde_json::to_vec(&pointer_json).unwrap();
            let mut doc = json::decode_strict(&pointer_bytes, &op).unwrap();
            let root = doc.root;
            let pointer = model::validate_pointer(&op, &mut doc, root).unwrap();
            let pointer = active_profile::LoadedPointer { doc, pointer };
            let encoded =
                project_binding(&op, &pointer, &snapshot, "defaults", &old, &new, budget).unwrap();
            let expected = json!({
                "schema": "io.tobkiri.defaults-pack-pin-predecessor.v1",
                "pointer_digest": pointer_json["pointer_digest"], "profile_id": "defaults",
                "profile_revision": pointer_revision, "activation_id": "activation:defaults-fixture",
                "plan_digest": plan, "lock_digest": lock, "generation": 9,
                "active_definition_digest": old.as_str(), "registry_profile_revision": new.as_str(),
                "registry_store_generation": 1, "registry_snapshot_digest": digest(&registry_json),
            });
            assert!(encoded.eq_guest(&parse(&expected, budget), budget).unwrap());
        });
    }
}
