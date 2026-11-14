//! The first immutable revision created from the packaged bootstrap Profile.
use super::*;

pub(in super::super) fn bootstrap_template_id<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    template: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Funded<Text>> {
    // The original catalog builder indexes a validated Profile document by
    // the very same string stored in its `profile_id` field.
    let id = template.get_key("profile_id");
    let id = match &id {
        Some(value) => value
            .as_str()
            .ok_or_else(OriginalAuthorityError::unavailable)?,
        None => "",
    };
    // Python rejects an overlength ID before looking at its content. Only
    // the first 129 code points need to be materialized for that decision.
    let count = id.chars().take(129).count();
    let capacity = op.reserve(OriginalAuthorityStage::Source, count * size_of::<u32>())?;
    let mut points = Vec::new();
    points.try_reserve_exact(count).map_err(capacity_error)?;
    points.extend(id.chars().take(count).map(u32::from));
    let text = Text(points);
    canonical_id(&text, op, budget)?;
    Ok(Funded {
        value: text,
        _capacity: capacity,
    })
}

pub(in super::super) fn normalize_bootstrap_template<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    template: &CoreValue,
    profile_id: &Text,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    // _profile_document sets the same validated ID. The strictly detached
    // graph is immutable, so retaining its document preserves the copy.
    canonical_id(profile_id, op, budget)?;
    if let Some(name) = template.get_key("display_name") {
        let name = name.as_str().unwrap_or_default();
        charge(op, budget, name.len() as u64)?;
        if name
            .trim_matches(|ch: char| python_space(ch) || matches!(ch, '\u{1c}'..='\u{1f}'))
            .is_empty()
        {
            return Err(integrity("Profile display_name must be a non-empty string"));
        }
    }
    // The original canonicalizes here before sampling the clock, then
    // hashes the candidate only after the duplicate check under create's lock.
    template.encode_guest(budget).map_err(canonical_error)?;
    Ok(())
}

pub(in super::super) fn prepare_bootstrap_template<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &State,
    template: &CoreValue,
    profile_id: &Text,
    now: i64,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
    let id = template
        .get_key("profile_id")
        .expect("validated template ID");
    if find_entry(op, state, profile_id, budget)?.is_some() {
        return Err(raw(
            CONFLICT,
            format!(
                "Profile '{}' already exists, including tombstones",
                id.as_str().expect("validated ID")
            ),
        ));
    }

    let revision = template.digest_guest(budget).map_err(canonical_error)?;
    let revision = CoreValue::string(revision.as_str(), budget).map_err(capacity_error)?;
    if now.unsigned_abs() > tobkiri_canon::MAX_SAFE_INTEGER {
        return Err(json::strict_error(
            "integer exceeds I-JSON safe range; use a canonical decimal string",
        ));
    }
    let timestamp = CoreValue::int(now, budget).map_err(capacity_error)?;
    let tombstone = CoreValue::bool(false, budget).map_err(capacity_error)?;
    let first = CoreValue::object_shared(
        [
            ("profile_revision", revision.share()),
            (
                "parent_revision",
                CoreValue::null(budget).map_err(capacity_error)?,
            ),
            ("profile", template.share()),
            ("created_at", timestamp.share()),
            ("updated_at", timestamp.share()),
            ("tombstone", tombstone.share()),
        ]
        .into_iter()
        .map(Ok),
        budget,
    )
    .map_err(canonical_error)?;

    let bytes = json::canonical(&state.doc, None, op)?;
    let snapshot = CoreValue::parse_guest(&bytes, budget).map_err(capacity_error)?;
    drop(bytes);
    let profiles = snapshot.get_key("profiles").expect("validated profiles");
    let profiles = profiles.array().expect("validated profiles");
    let order = i64::try_from(profiles.len()).map_err(capacity_error)?;
    let first =
        CoreValue::array_shared(std::iter::once(Ok(first)), budget).map_err(canonical_error)?;
    let row = CoreValue::object_shared(
        [
            ("profile_id", id),
            (
                "order",
                CoreValue::int(order, budget).map_err(capacity_error)?,
            ),
            ("tombstone", tombstone),
            ("current_revision", revision.share()),
            ("revisions", first),
            ("created_at", timestamp.share()),
            ("updated_at", timestamp.share()),
            (
                "legacy_ids",
                CoreValue::array_builder(budget, 0)
                    .and_then(|b| b.finish())
                    .map_err(capacity_error)?,
            ),
        ]
        .into_iter()
        .map(Ok),
        budget,
    )
    .map_err(canonical_error)?;
    let mut appended =
        CoreValue::array_builder(budget, profiles.len() + 1).map_err(capacity_error)?;
    for old in profiles.iter() {
        charge(op, budget, 1)?;
        appended.push(old).map_err(canonical_error)?;
    }
    appended.push(row).map_err(canonical_error)?;
    let profiles = appended.finish().map_err(canonical_error)?;

    let available = CoreValue::string("template_available", budget).map_err(capacity_error)?;
    let bootstrap = CoreValue::object_shared(
        [
            ("state", available),
            ("template_profile_revision", revision),
        ]
        .into_iter()
        .map(Ok),
        budget,
    )
    .map_err(capacity_error)?;
    let legacy = snapshot
        .get_key("legacy")
        .expect("validated legacy metadata");
    encode_state(
        &snapshot,
        profiles,
        &timestamp,
        1,
        [legacy, bootstrap],
        budget,
    )
}

fn capacity_error(_: impl Sized) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}

fn canonical_error(error: AllocationError) -> OriginalAuthorityError {
    match error {
        AllocationError::Depth => json::strict_error("json nesting exceeds depth limit"),
        _ => capacity_error(error),
    }
}
