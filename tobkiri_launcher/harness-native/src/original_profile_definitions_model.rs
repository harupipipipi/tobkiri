//! App-owned registry policy. The fresh owned arena has no external aliases;
//! fixed list views replace Python's redundant deepcopy/record conversions.
use super::*;
use crate::unicode15_1::{self, UnicodeError, UnicodeWork};
use tobkiri_canon::{AllocationError, CoreBuffer, CoreValue};

#[path = "original_profile_successor_selection.rs"]
mod successor_selection;
pub(super) use successor_selection::select_successor;
#[path = "original_profile_definitions_display_names.rs"]
mod display_names;
pub(super) use display_names::prepare_display_name_repair;
#[path = "original_profile_definitions_retired.rs"]
mod retired;
pub(super) use retired::prepare_retired_pack_migration;

const SCHEMA: &str = "io.tobkiri.profile-definition-store.v1";
pub(super) const CONFLICT: &str =
    "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreConflict";
pub(super) struct State {
    pub(super) doc: Funded<Document>,
    _scratch: NativeSetupReservation,
}

/// Read-only DATA for the first reachable successor call in physical registry order.
pub(super) enum SuccessorSelection {
    Exhausted,
    BuilderEntry {
        // Indices into the same validated State, without copying its documents.
        item: usize,
        profile_id: usize,
        item_origin: SuccessorItemOrigin,
        source_path: SuccessorSourcePath,
    },
}

/// Which actual registry object Python would pass as `item`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum SuccessorItemOrigin {
    LegacySource,
    CurrentRevision,
}

/// Python's independently selected source path, retaining the rendered bytes' funding.
pub(super) struct SuccessorSourcePath {
    _rendered: Option<CoreBuffer>,
}
#[cfg(test)]
impl SuccessorSourcePath {
    pub(super) fn as_str(&self) -> &str {
        self._rendered
            .as_ref()
            .map_or("legacy/startup_profiles.json", |bytes| {
                bytes
                    .as_str()
                    .expect("strict registry str renders as UTF-8")
            })
    }
}

#[derive(Clone, Copy)]
pub(super) struct StoredProfile {
    // profile_id, profile_revision, profile, parent_revision, legacy_ids.
    pub(super) references: [usize; 5],
    // order, tombstone, created_at, updated_at, preserving original types.
    pub(super) scalars: (i64, bool, i64, i64),
}

pub(super) fn load<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    bytes: Option<&[u8]>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<State> {
    const EMPTY: &[u8] = br#"{"schema":"io.tobkiri.profile-definition-store.v1","generation":0,"updated_at":0,"profiles":[],"bootstrap":{"state":"empty","template_profile_revision":null},"legacy":{"source_digest":null,"source_path":null,"active_profile_id":null,"last_launched_profile_id":null,"legacy_id_map":{},"tombstones":[],"source_document":null},"store_digest":null}"#;
    let mut doc =
        json::decode_profile_definitions(bytes.unwrap_or(EMPTY), op, budget).map_err(unreadable)?;
    let scratch = op.reserve(
        OriginalAuthorityStage::Source,
        doc.node_count()
            .checked_mul(32)
            .and_then(|n| n.checked_add(16384))
            .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?,
    )?;
    if doc.entries(doc.root).is_none() {
        return Err(integrity("Profile definition store is not an object"));
    }
    let root = doc.root;
    let digest = digest_at(&mut doc, root, Some("store_digest"), op, budget)?;
    if bytes.is_none() {
        set_digest(&mut doc, digest.as_str());
    } else if doc
        .get(root, "store_digest")
        .is_none_or(|id| !doc.string_is(id, digest.as_str()))
    {
        return Err(integrity("Profile definition store digest changed"));
    }
    if doc.get(root, "bootstrap").is_none() {
        // Original inference uses raw profiles truthiness BEFORE type/schema
        // validation. Do not turn a malformed but truthy value into empty.
        let established = doc
            .get(root, "profiles")
            .is_some_and(|id| truthy(doc.node(id)));
        let state = push(
            &mut doc,
            Node::String(Text::from_str(if established {
                "not_required"
            } else {
                "empty"
            })),
        );
        let null = push(&mut doc, Node::Scalar(CanonValue::Null));
        let bootstrap = push(
            &mut doc,
            Node::Object(vec![
                (Text::from_str("state"), state),
                (Text::from_str("template_profile_revision"), null),
            ]),
        );
        let Node::Object(fields) = &mut doc.nodes[root] else {
            unreachable!("registry object")
        };
        fields.push((Text::from_str("bootstrap"), bootstrap));
        let digest = digest_at(&mut doc, root, Some("store_digest"), op, budget)?;
        set_digest(&mut doc, digest.as_str());
    }
    validate_state(&mut doc, op, budget)?;
    Ok(State {
        doc,
        _scratch: scratch,
    })
}
fn push(doc: &mut Document, node: Node) -> usize {
    let id = doc.nodes.len();
    doc.nodes.push(node);
    id
}
fn set_digest(doc: &mut Document, digest: &str) {
    let id = doc.get(doc.root, "store_digest").expect("registry digest");
    doc.nodes[id] = Node::String(Text::from_str(digest));
}
fn truthy(node: &Node) -> bool {
    match node {
        Node::String(text) => !text.0.is_empty(),
        Node::Array(items) => !items.is_empty(),
        Node::Object(items) => !items.is_empty(),
        Node::Scalar(CanonValue::Null | CanonValue::Bool(false) | CanonValue::Int(0)) => false,
        Node::Scalar(_) => true,
    }
}
fn field(doc: &Document, id: usize, name: &str) -> usize {
    doc.get(id, name).expect("validated fixed registry fields")
}
fn array(doc: &Document, id: usize) -> Option<&[usize]> {
    match doc.node(id) {
        Node::Array(items) => Some(items),
        _ => None,
    }
}
fn indexes(items: &[usize]) -> Result<Vec<usize>> {
    let mut copy = Vec::new();
    copy.try_reserve_exact(items.len())
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
    copy.extend_from_slice(items);
    Ok(copy)
}
fn nonnegative(doc: &Document, id: usize) -> bool {
    matches!(doc.node(id), Node::Scalar(CanonValue::Int(value)) if *value >= 0)
}
fn boolean(doc: &Document, id: usize) -> Option<bool> {
    match doc.node(id) {
        Node::Scalar(CanonValue::Bool(value)) => Some(*value),
        _ => None,
    }
}
fn integer(doc: &Document, id: usize) -> i64 {
    match doc.node(id) {
        Node::Scalar(CanonValue::Int(value)) => *value,
        _ => unreachable!("registry integer"),
    }
}
fn text(doc: &Document, id: usize) -> Option<&Text> {
    match doc.node(id) {
        Node::String(value) => Some(value),
        _ => None,
    }
}

fn charge<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    work: u64,
) -> Result<()> {
    op.check(OriginalAuthorityStage::Source)?;
    budget
        .reserve(tobkiri_canon::AllocationRequest {
            kind: tobkiri_canon::AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work,
        })
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))
}
fn digest_at<S: OriginalAuthorityOwner>(
    doc: &mut Document,
    id: usize,
    omit: Option<&str>,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Digest> {
    canonical_at(doc, id, omit, op, budget).map(|bytes| Digest::hash(&bytes))
}

fn canonical_at<S: OriginalAuthorityOwner>(
    doc: &mut Document,
    id: usize,
    omit: Option<&str>,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Funded<Vec<u8>>> {
    // Existing canonical writer and allocation bound stay unchanged. Prepay
    // a conservative scan/sort bound at each actually reached digest call.
    let levels = usize::BITS - doc.node_count().max(1).leading_zeros();
    let work = (doc.source_bytes() as u64)
        .checked_mul(u64::from(levels) + 2)
        .and_then(|n| n.checked_mul(8))
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
    charge(op, budget, work)?;
    let original = doc.root;
    let removed = if let Some(key) = omit {
        let Node::Object(fields) = &mut doc.nodes[id] else {
            unreachable!("registry object")
        };
        fields
            .iter()
            .position(|(name, _)| name.is(key))
            .map(|at| (at, fields.remove(at)))
    } else {
        None
    };
    doc.root = id;
    let result = json::canonical(doc, None, op).and_then(|bytes| {
        if bytes.len() > tobkiri_canon::MAX_BYTES {
            return Err(raw(
                "tobkiri_protocol.errors.CanonicalizationError",
                "canonical JSON exceeds size limit",
            ));
        }
        Ok(bytes)
    });
    doc.root = original;
    if let Some((at, pair)) = removed {
        let Node::Object(fields) = &mut doc.nodes[id] else {
            unreachable!("registry object")
        };
        fields.insert(at, pair);
    }
    result
}
fn validate_state<S: OriginalAuthorityOwner>(
    doc: &mut Document,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let root = doc.root;
    if !exact(
        doc,
        root,
        &[
            "schema",
            "generation",
            "updated_at",
            "profiles",
            "legacy",
            "bootstrap",
            "store_digest",
        ],
    ) || !doc.string_is(field(doc, root, "schema"), SCHEMA)
    {
        return Err(integrity("Profile definition store fields are invalid"));
    }
    if !nonnegative(doc, field(doc, root, "generation"))
        || !nonnegative(doc, field(doc, root, "updated_at"))
    {
        return Err(integrity("Profile definition store counters are invalid"));
    }
    let profiles_id = field(doc, root, "profiles");
    let profiles = indexes(
        array(doc, profiles_id)
            .ok_or_else(|| integrity("Profile definition store profiles are invalid"))?,
    )?;
    let mut seen = Vec::<usize>::new();
    seen.try_reserve_exact(profiles.len())
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
    for &entry in &profiles {
        charge(op, budget, 1)?;
        if doc.entries(entry).is_none() {
            return Err(integrity("Profile store entry is invalid"));
        }
        validate_entry(doc, entry, op, budget)?;
        let key = field(doc, entry, "profile_id");
        for &prior in &seen {
            charge(
                op,
                budget,
                1 + text(doc, key).expect("validated ID").0.len() as u64,
            )?;
            if text(doc, key) == text(doc, prior) {
                return Err(integrity("Profile store ID is duplicated"));
            }
        }
        seen.push(key);
    }
    if doc.entries(field(doc, root, "legacy")).is_none() {
        return Err(integrity("Profile migration metadata is invalid"));
    }
    let bootstrap = field(doc, root, "bootstrap");
    if !exact(doc, bootstrap, &["state", "template_profile_revision"]) {
        return Err(integrity("Profile bootstrap metadata is invalid"));
    }
    let state = field(doc, bootstrap, "state");
    let revision = field(doc, bootstrap, "template_profile_revision");
    let is_null = matches!(doc.node(revision), Node::Scalar(CanonValue::Null));
    if !["empty", "template_available", "not_required"]
        .iter()
        .any(|s| doc.string_is(state, s))
        || (!is_null && text(doc, revision).is_none())
    {
        return Err(integrity("Profile bootstrap metadata is invalid"));
    }
    charge(op, budget, profiles.len() as u64)?;
    let mut live = profiles;
    live.retain(|id| boolean(doc, field(doc, *id, "tombstone")) == Some(false));
    if doc.string_is(state, "empty") && (!live.is_empty() || !is_null) {
        return Err(integrity(
            "empty Profile bootstrap metadata is inconsistent",
        ));
    }
    if doc.string_is(state, "not_required") && !is_null {
        return Err(integrity(
            "established Profile bootstrap metadata is inconsistent",
        ));
    }
    if doc.string_is(state, "template_available") {
        let valid = if text(doc, revision).is_some()
            && live.len() == 1
            && text(doc, field(doc, live[0], "current_revision")) == text(doc, revision)
        {
            let mut matching = 0;
            for &id in array(doc, field(doc, live[0], "revisions")).expect("validated history") {
                charge(op, budget, 72)?;
                if text(doc, field(doc, id, "profile_revision")) == text(doc, revision)
                    && boolean(doc, field(doc, id, "tombstone")) == Some(false)
                {
                    matching += 1;
                }
            }
            matching == 1
        } else {
            false
        };
        if !valid {
            return Err(integrity(
                "Profile bootstrap template binding is inconsistent",
            ));
        }
    }
    Ok(())
}
fn validate_entry<S: OriginalAuthorityOwner>(
    doc: &mut Document,
    entry: usize,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    if !exact(
        doc,
        entry,
        &[
            "profile_id",
            "order",
            "tombstone",
            "current_revision",
            "revisions",
            "created_at",
            "updated_at",
            "legacy_ids",
        ],
    ) {
        return Err(integrity("Profile store entry fields are invalid"));
    }
    let profile_id = field(doc, entry, "profile_id");
    let id = text(doc, profile_id).ok_or_else(|| integrity("Profile ID is invalid"))?;
    canonical_id(id, op, budget)?;
    artifact_digest(
        doc,
        field(doc, entry, "current_revision"),
        "current_revision",
    )?;
    if !nonnegative(doc, field(doc, entry, "order")) {
        return Err(integrity("Profile order is invalid"));
    }
    if boolean(doc, field(doc, entry, "tombstone")).is_none() {
        return Err(integrity("Profile tombstone is invalid"));
    }
    let revisions = indexes(
        array(doc, field(doc, entry, "revisions"))
            .filter(|v| !v.is_empty())
            .ok_or_else(|| integrity("Profile revision history is empty"))?,
    )?;
    if array(doc, field(doc, entry, "legacy_ids")).is_none_or(|values| {
        values
            .iter()
            .any(|&id| text(doc, id).is_none_or(|s| s.0.is_empty()))
    }) {
        return Err(integrity("Profile legacy IDs are invalid"));
    }
    if !nonnegative(doc, field(doc, entry, "created_at"))
        || !nonnegative(doc, field(doc, entry, "updated_at"))
    {
        return Err(integrity("Profile timestamps are invalid"));
    }
    let latest = *revisions.last().expect("nonempty");
    if doc.entries(latest).is_none()
        || doc
            .get(latest, "profile_revision")
            .and_then(|id| text(doc, id))
            != text(doc, field(doc, entry, "current_revision"))
    {
        return Err(integrity("Profile current revision is inconsistent"));
    }
    let mut previous = None;
    for (index, revision) in revisions.into_iter().enumerate() {
        charge(op, budget, 1)?;
        if doc.entries(revision).is_none() {
            return Err(integrity("Profile revision is invalid"));
        }
        if !exact(
            doc,
            revision,
            &[
                "profile_revision",
                "parent_revision",
                "profile",
                "created_at",
                "updated_at",
                "tombstone",
            ],
        ) {
            return Err(integrity("Profile revision fields are invalid"));
        }
        if !nonnegative(doc, field(doc, revision, "created_at"))
            || !nonnegative(doc, field(doc, revision, "updated_at"))
        {
            return Err(integrity("Profile revision timestamp is invalid"));
        }
        let tombstone = boolean(doc, field(doc, revision, "tombstone"))
            .ok_or_else(|| integrity("Profile revision tombstone is invalid"))?;
        let digest = field(doc, revision, "profile_revision");
        artifact_digest(doc, digest, "profile_revision")?;
        let parent = field(doc, revision, "parent_revision");
        if index == 0 {
            if !matches!(doc.node(parent), Node::Scalar(CanonValue::Null)) {
                return Err(integrity("first Profile revision cannot have a parent"));
            }
        } else if text(doc, parent) != previous.and_then(|id| text(doc, id)) {
            return Err(integrity("Profile revision successor chain is invalid"));
        }
        if !matches!(doc.node(parent), Node::Scalar(CanonValue::Null)) {
            artifact_digest(doc, parent, "parent_revision")?;
        }
        let profile = field(doc, revision, "profile");
        let expected = if tombstone {
            if !matches!(doc.node(profile), Node::Scalar(CanonValue::Null)) {
                return Err(integrity("Profile tombstone contains a document"));
            }
            let start = doc.nodes.len();
            let schema = push(
                doc,
                Node::String(Text::from_str("io.tobkiri.profile-tombstone.v1")),
            );
            let object = push(
                doc,
                Node::Object(vec![
                    (Text::from_str("schema"), schema),
                    (Text::from_str("profile_id"), profile_id),
                    (Text::from_str("parent_revision"), parent),
                ]),
            );
            let result = digest_at(doc, object, None, op, budget);
            doc.nodes.truncate(start);
            result?
        } else {
            if doc.entries(profile).is_none() {
                return Err(integrity("Profile revision document is invalid"));
            }
            if doc.get(profile, "profile_id").and_then(|id| text(doc, id)) != text(doc, profile_id)
            {
                return Err(integrity("Profile revision ID does not match its entry"));
            }
            digest_at(doc, profile, None, op, budget)?
        };
        if !doc.string_is(digest, expected.as_str()) {
            return Err(integrity(if tombstone {
                "Profile tombstone revision is invalid"
            } else {
                "Profile revision digest changed"
            }));
        }
        previous = Some(digest);
    }
    if boolean(doc, field(doc, latest, "tombstone")) != boolean(doc, field(doc, entry, "tombstone"))
    {
        return Err(integrity("Profile entry tombstone is inconsistent"));
    }
    Ok(())
}
fn artifact_digest(doc: &Document, id: usize, field: &'static str) -> Result<()> {
    let valid = text(doc, id).is_some_and(|s| {
        s.0.len() == 71
            && s.0[..7]
                .iter()
                .copied()
                .eq("sha256:".chars().map(u32::from))
            && s.0[7..]
                .iter()
                .all(|v| (48..=57).contains(v) || (97..=102).contains(v))
    });
    if valid {
        return Ok(());
    }
    let message = format!("{field} must be a lowercase sha256 digest");
    Err(caused(
        integrity(message.clone()),
        raw("tobkiri_protocol.errors.ProtocolError", message),
    ))
}
struct Meter<'a, 'op, S> {
    op: &'a Operation<'op, S>,
    budget: &'a Arc<dyn AllocationBudget>,
}
impl<S: OriginalAuthorityOwner> UnicodeWork for Meter<'_, '_, S> {
    type Error = OriginalAuthorityError;
    fn charge_work(&mut self, work: u64) -> Result<()> {
        charge(self.op, self.budget, work)
    }
}
fn unicode_error(error: UnicodeError<OriginalAuthorityError>) -> OriginalAuthorityError {
    match error {
        UnicodeError::Work(error) => error,
        UnicodeError::Allocation(_) => {
            OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
        }
        _ => OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Source,
            "registry Unicode primitive contract",
        ),
    }
}
pub(super) fn canonical_id<S: OriginalAuthorityOwner>(
    id: &Text,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    charge(op, budget, 4 * id.0.len().min(129) as u64 + 4)?;
    let mut meter = Meter { op, budget };
    let reason = if id.0.is_empty() {
        Some("profile_id must be a non-empty string")
    } else if id.0.len() > 128 {
        Some("profile_id exceeds 128 characters")
    } else {
        let mut scratch = [0_u32; 512];
        if !unicode15_1::is_nfc(&id.0, &mut scratch, &mut meter).map_err(unicode_error)? {
            Some("profile_id is not Unicode-normalized")
        } else {
            None
        }
    };
    let message = if let Some(reason) = reason {
        reason.to_owned()
    } else {
        let lower = unicode15_1::lower_changes(&id.0, &mut meter).map_err(unicode_error)?;
        let legacy = !lower
            && ["rumi.", "rumiai.", "viewer.", "legacy."]
                .iter()
                .any(|prefix| {
                    id.0.iter()
                        .copied()
                        .take(prefix.len())
                        .eq(prefix.chars().map(u32::from))
                });
        let syntax = !lower && !legacy && id.0.first().is_some_and(|v| (97..=122).contains(v)) && {
            let mut separator = false;
            id.0.iter().all(|v| {
                if (97..=122).contains(v) || (48..=57).contains(v) {
                    separator = false;
                    true
                } else if [46, 95, 45].contains(v) && !separator {
                    separator = true;
                    true
                } else {
                    false
                }
            }) && !separator
        };
        if !lower && !legacy && syntax {
            return Ok(());
        }
        let mut repr = tobkiri_canon::CoreBuffer::builder(budget, 2048);
        unicode15_1::write_python_repr(&id.0, &mut repr, &mut meter).map_err(unicode_error)?;
        let repr = repr
            .finish()
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
        format!(
            "profile_id {}: {}",
            if lower || legacy {
                "is not a canonical v4 identifier"
            } else {
                "has invalid canonical ID syntax"
            },
            repr.as_str()
                .map_err(|_| OriginalAuthorityError::unavailable())?
        )
    };
    Err(caused(
        integrity(message.clone()),
        raw("tobkiri_protocol.errors.ProtocolError", message),
    ))
}

pub(super) struct InactiveDefinition {
    pub(super) profile: usize,
    revision: usize,
}

pub(super) fn inactive_definition<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &State,
    profile_id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<InactiveDefinition>> {
    let doc = &state.doc;
    let entries = array(doc, field(doc, doc.root, "profiles")).expect("validated profiles");
    let mut selected = None;
    for &entry in entries {
        charge(op, budget, 1 + profile_id.len() as u64)?;
        if doc.string_is(field(doc, entry, "profile_id"), profile_id) {
            selected = Some(entry);
        }
    }
    let Some(entry) = selected else {
        return Ok(None);
    };
    if boolean(doc, field(doc, entry, "tombstone")).expect("validated bool") {
        return Err(raw(CONFLICT, "imported bootstrap Profile is tombstoned"));
    }
    let revision_id = field(doc, entry, "current_revision");
    let revision = text(doc, revision_id).expect("validated revision");
    let history = array(doc, field(doc, entry, "revisions")).expect("validated history");
    let mut current = None;
    for &version in history {
        charge(op, budget, 1 + revision.0.len() as u64)?;
        if text(doc, field(doc, version, "profile_revision")) == Some(revision)
            && current.replace(version).is_some()
        {
            return Err(raw(CONFLICT, "imported bootstrap revision is unavailable"));
        }
    }
    let current = current
        .filter(|&id| boolean(doc, field(doc, id, "tombstone")) == Some(false))
        .ok_or_else(|| raw(CONFLICT, "imported bootstrap revision is unavailable"))?;
    let profile = field(doc, current, "profile");
    Ok(Some(InactiveDefinition {
        profile,
        revision: revision_id,
    }))
}

pub(super) fn check_inactive_handover<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &mut State,
    journal: &Document,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Digest> {
    let changed = || {
        raw(
            CONFLICT,
            "imported bootstrap registry changed after completed handover",
        )
    };
    let doc = &mut state.doc;
    let root = doc.root;
    let generation = integer(doc, field(doc, root, "generation"));
    if !journal
        .get(journal.root, "stage")
        .is_some_and(|stage| journal.string_is(stage, "completed"))
    {
        return Err(changed());
    }
    let expected_generation = journal
        .get(journal.root, "destination_profile_generation")
        .and_then(|id| match journal.node(id) {
            Node::Scalar(CanonValue::Int(value)) => Some(*value),
            Node::Scalar(CanonValue::Bool(value)) => Some(i64::from(*value)),
            _ => None,
        });
    if expected_generation != Some(generation) {
        return Err(changed());
    }
    let snapshot = digest_at(doc, root, None, op, budget)?;
    let bootstrap = field(doc, root, "bootstrap");
    if !journal
        .get(journal.root, "destination_profile_snapshot_digest")
        .is_some_and(|digest| journal.string_is(digest, snapshot.as_str()))
        || !doc.string_is(field(doc, bootstrap, "state"), "not_required")
        || !matches!(
            doc.node(field(doc, bootstrap, "template_profile_revision")),
            Node::Scalar(CanonValue::Null)
        )
    {
        return Err(changed());
    }
    Ok(snapshot)
}

pub(super) fn require_inert_definition<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &State,
    profile: usize,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let doc = &state.doc;
    let conflict = || raw(CONFLICT, "bootstrap import is not inert definition data");
    let get = |name| doc.get(profile, name);
    let is_null = |id| matches!(doc.node(id), Node::Scalar(CanonValue::Null));
    if !get("state").is_some_and(|id| doc.string_is(id, "needs_resolution"))
        || !get("catalog_revision").is_none_or(is_null)
        || !get("authority_references")
            .and_then(|id| array(doc, id))
            .is_some_and(<[usize]>::is_empty)
        || !get("profile_authority_snapshot_digest").is_none_or(is_null)
        || get("content_projections").is_some()
    {
        return Err(conflict());
    }
    let edges = subscript(doc, profile, "requested_edges")?;
    const REFERENCE: &str = "authority_reference";
    let contains_reference = |value: &Text| {
        value
            .0
            .windows(REFERENCE.len())
            .any(|part| part.iter().copied().eq(REFERENCE.chars().map(u32::from)))
    };
    match doc.node(edges) {
        Node::Array(edges) => {
            for &edge in edges {
                let size = match doc.node(edge) {
                    Node::Object(fields) => fields.len(),
                    Node::Array(items) => items.len(),
                    Node::String(value) => value.0.len(),
                    Node::Scalar(_) => 0,
                };
                charge(
                    op,
                    budget,
                    1 + (size as u64).saturating_mul(REFERENCE.len() as u64 + 1),
                )?;
                let has_reference = match doc.node(edge) {
                    Node::Object(_) => doc.get(edge, REFERENCE).is_some(),
                    Node::Array(values) => values.iter().any(|&id| doc.string_is(id, REFERENCE)),
                    Node::String(value) => contains_reference(value),
                    node => {
                        return Err(raw(
                            "builtins.TypeError",
                            format!("argument of type '{}' is not iterable", model::kind(node)),
                        ));
                    }
                };
                if has_reference {
                    return Err(conflict());
                }
            }
        }
        // Iterating an object or string yields strings. A single character
        // cannot match, but an object key can contain the exact substring.
        Node::Object(fields) => {
            for (name, _) in fields {
                charge(
                    op,
                    budget,
                    1 + (name.0.len() as u64).saturating_mul(REFERENCE.len() as u64 + 1),
                )?;
                if contains_reference(name) {
                    return Err(conflict());
                }
            }
        }
        Node::String(value) => charge(op, budget, value.0.len() as u64)?,
        node => {
            return Err(raw(
                "builtins.TypeError",
                format!("'{}' object is not iterable", model::kind(node)),
            ));
        }
    }
    if !get("mode").is_some_and(|id| doc.string_is(id, "interactive"))
        || get("shell").is_none_or(|id| doc.entries(id).is_none())
    {
        return Err(conflict());
    }
    Ok(())
}

pub(super) fn project_inactive_predecessor<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &mut State,
    definition: &InactiveDefinition,
    digests: &super::InactiveHandoverDigests,
    profile_id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let doc = &mut state.doc;
    let profile = definition.profile;
    // The immutable revision is the exact digest checked by validate_entry
    // against this selected live definition when this snapshot was loaded.
    let capacity = |_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source);
    let mut meter = Meter { op, budget };
    let string = |doc: &Document, id, meter: &mut Meter<'_, '_, S>| -> Result<CoreValue> {
        let bytes = successor_selection::python_str(doc, id, meter)?;
        CoreValue::string_from_utf8(&bytes, budget).map_err(capacity)
    };
    let revision = string(doc, definition.revision, &mut meter)?;
    let generation = CoreValue::int(integer(doc, field(doc, doc.root, "generation")), budget)
        .map_err(capacity)?;
    let display_name = match doc
        .get(profile, "display_name")
        .filter(|&id| truthy(doc.node(id)))
    {
        Some(id) => string(doc, id, &mut meter)?,
        None => CoreValue::string(profile_id, budget).map_err(capacity)?,
    };
    let packs = subscript(doc, profile, "packs")?;
    let packs = match doc.node(packs) {
        Node::Array(packs) => packs.as_slice(),
        Node::Object(fields) if fields.is_empty() => &[],
        Node::String(value) if value.0.is_empty() => &[],
        Node::Object(_) | Node::String(_) => {
            return Err(raw(
                "builtins.TypeError",
                "string indices must be integers, not 'str'",
            ));
        }
        node => {
            return Err(raw(
                "builtins.TypeError",
                format!("'{}' object is not iterable", model::kind(node)),
            ));
        }
    };
    let mut pack_ids = CoreValue::array_builder(budget, packs.len()).map_err(capacity)?;
    for &pack in packs {
        charge(
            op,
            budget,
            1 + doc.entries(pack).map_or(0, |fields| fields.len()) as u64,
        )?;
        pack_ids
            .push(string(doc, subscript(doc, pack, "pack_id")?, &mut meter)?)
            .map_err(capacity)?;
    }
    let base = subscript(doc, profile, "base")?;
    let base_pack_id = string(doc, subscript(doc, base, "pack_id")?, &mut meter)?;
    let shell = subscript(doc, profile, "shell")?;
    let shell_pack_id = string(doc, subscript(doc, shell, "pack_id")?, &mut meter)?;
    let shell_provider_id = string(doc, subscript(doc, shell, "provider_id")?, &mut meter)?;
    let mut shell_value = |key| -> Result<CoreValue> {
        let id = subscript(doc, shell, key)?;
        let bytes = canonical_at(doc, id, None, op, budget)?;
        CoreValue::parse_guest(&bytes, budget).map_err(capacity)
    };
    let digest_value = |digest: &Digest| CoreValue::string(digest.as_str(), budget);
    let fields: [(&str, std::result::Result<CoreValue, AllocationError>); 13] = [
        ("profile_revision", Ok(revision.share())),
        ("definition_digest", Ok(revision)),
        ("store_generation", Ok(generation)),
        ("snapshot_digest", digest_value(&digests.snapshot)),
        ("handover_digest", digest_value(&digests.journal)),
        ("display_name", Ok(display_name)),
        ("pack_ids", pack_ids.finish()),
        ("base_pack_id", Ok(base_pack_id)),
        ("shell_pack_id", Ok(shell_pack_id)),
        ("shell_provider_id", Ok(shell_provider_id)),
        ("shell_artifact_digest", Ok(shell_value("artifact_digest")?)),
        (
            "shell_executable_artifact_digest",
            Ok(shell_value("executable_artifact_digest")?),
        ),
        (
            "shell_definition_revision",
            Ok(shell_value("definition_revision")?),
        ),
    ];
    CoreValue::object_shared(
        fields.into_iter().map(|(name, value)| Ok((name, value?))),
        budget,
    )
    .map_err(capacity)
}

pub(super) fn inactive_definition_bytes<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &mut State,
    definition: &InactiveDefinition,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Funded<Vec<u8>>> {
    canonical_at(&mut state.doc, definition.profile, None, op, budget)
}

pub(super) fn get<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &mut State,
    profile_id: &Text,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<StoredProfile>> {
    let doc = &mut state.doc;
    let entries = array(doc, field(doc, doc.root, "profiles")).expect("validated profiles");
    let mut matching = None;
    for &entry in entries {
        charge(op, budget, 1 + profile_id.0.len() as u64)?;
        if text(doc, field(doc, entry, "profile_id")) == Some(profile_id) {
            matching = Some(entry);
            break;
        }
    }
    let Some(entry) = matching else {
        return Ok(None);
    };
    if boolean(doc, field(doc, entry, "tombstone")).expect("validated bool") {
        return Ok(None);
    }
    Ok(Some(stored_from_entry(doc, entry)))
}

/// Validate the generation and current revision again under the update lock.
#[cfg(test)]
pub(super) fn require_registration_cas<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &mut State,
    profile_id: &str,
    generation: i64,
    revision: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<usize> {
    let doc = &state.doc;
    if integer(doc, field(doc, doc.root, "generation")) != generation {
        return Err(raw(CONFLICT, "Profile store generation is stale"));
    }
    let _id = op.reserve(
        OriginalAuthorityStage::Source,
        profile_id.len().saturating_mul(4),
    )?;
    let profile_id = Text::from_str(profile_id);
    canonical_id(&profile_id, op, budget)?;
    let entries = array(doc, field(doc, doc.root, "profiles")).expect("validated profiles");
    let mut selected = None;
    for &entry in entries {
        charge(op, budget, 1 + profile_id.0.len() as u64)?;
        if text(doc, field(doc, entry, "profile_id")) == Some(&profile_id) {
            selected = Some(entry);
            break;
        }
    }
    let current = selected
        .filter(|&id| boolean(doc, field(doc, id, "tombstone")) == Some(false))
        .ok_or_else(|| {
            raw(
                "core_runtime.profile_definition_store_v4.ProfileDefinitionNotFound",
                json::repr_text(&profile_id),
            )
        })?;
    if !doc.string_is(field(doc, current, "current_revision"), revision) {
        return Err(raw(CONFLICT, "Profile revision is stale"));
    }
    Ok(current)
}

#[cfg(test)]
pub(super) fn prepare_registration<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &State,
    entry: usize,
    source: &CoreValue,
    now: i64,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
    let capacity = |_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source);
    let source_id = source
        .get_key("profile_id")
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    if source_id.as_str().is_none_or(|id| {
        !state
            .doc
            .string_is(field(&state.doc, entry, "profile_id"), id)
    }) {
        return Err(integrity("Profile revision ID does not match its entry"));
    }
    if source.get_key("display_name").is_some_and(|name| {
        name.as_str().is_none_or(|name| {
            name.trim_matches(|ch: char| ch.is_whitespace() || matches!(ch, '\u{1c}'..='\u{1f}'))
                .is_empty()
        })
    }) {
        return Err(integrity("Profile display_name must be a non-empty string"));
    }
    let timestamp = CoreValue::int(now, budget).map_err(capacity)?;
    let doc = &state.doc;
    // One funded copy of the validated snapshot admits shared links to all
    // existing rows. The source, new revision and changed counters are the
    // only new content; the immutable history remains in physical order.
    let bytes = json::canonical(doc, None, op)?;
    let snapshot = CoreValue::parse_guest(&bytes, budget).map_err(capacity)?;
    drop(bytes);
    let profiles = snapshot.get_key("profiles").expect("validated profiles");
    let profiles = profiles.array().expect("validated profiles");
    let entry_position = array(doc, field(doc, doc.root, "profiles"))
        .expect("validated profiles")
        .iter()
        .position(|&id| id == entry)
        .expect("selected registry entry");
    let row = profiles
        .get(entry_position)
        .expect("selected registry entry");
    let row = append_successor(&row, source, &timestamp, budget)?;
    let profiles = CoreValue::array_shared(
        profiles.iter().enumerate().map(|(index, value)| {
            Ok(if index == entry_position {
                row.share()
            } else {
                value
            })
        }),
        budget,
    )
    .map_err(capacity)?;
    encode_successors(&snapshot, profiles, &timestamp, 1, budget)
}

fn append_successor(
    row: &CoreValue,
    source: &CoreValue,
    timestamp: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let capacity = |_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source);
    let revision = source.digest_guest(budget).map_err(capacity)?;
    let revision = CoreValue::string(revision.as_str(), budget).map_err(capacity)?;
    let parent = row.get_key("current_revision").expect("validated revision");
    let successor = CoreValue::object_shared(
        [
            ("profile_revision", revision.share()),
            ("parent_revision", parent),
            ("profile", source.share()),
            ("created_at", timestamp.share()),
            ("updated_at", timestamp.share()),
            (
                "tombstone",
                CoreValue::bool(false, budget).map_err(capacity)?,
            ),
        ]
        .into_iter()
        .map(Ok),
        budget,
    )
    .map_err(capacity)?;
    let old_history = row.get_key("revisions").expect("validated revisions");
    let old_history = old_history.array().expect("validated revisions");
    let mut history = CoreValue::array_builder(budget, old_history.len() + 1).map_err(capacity)?;
    for revision in old_history.iter() {
        history.push(revision).map_err(capacity)?;
    }
    history.push(successor).map_err(capacity)?;
    let history = history.finish().map_err(capacity)?;
    CoreValue::object_shared(
        row.object()
            .expect("validated row")
            .iter()
            .map(|(key, value)| {
                Ok((
                    key,
                    match key {
                        "revisions" => history.share(),
                        "current_revision" => revision.share(),
                        "updated_at" => timestamp.share(),
                        _ => value,
                    },
                ))
            }),
        budget,
    )
    .map_err(capacity)
}

fn encode_successors(
    snapshot: &CoreValue,
    profiles: CoreValue,
    timestamp: &CoreValue,
    changed: i64,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
    let legacy = snapshot
        .get_key("legacy")
        .expect("validated legacy metadata");
    let bootstrap = CoreValue::parse_guest(
        br#"{"state":"not_required","template_profile_revision":null}"#,
        budget,
    )
    .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
    encode_state(
        snapshot,
        profiles,
        timestamp,
        changed,
        [legacy, bootstrap],
        budget,
    )
}

fn encode_state(
    snapshot: &CoreValue,
    profiles: CoreValue,
    timestamp: &CoreValue,
    changed: i64,
    [legacy, bootstrap]: [CoreValue; 2],
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
    let capacity = |_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source);
    let generation = snapshot
        .get_key("generation")
        .and_then(|generation| generation.as_i64())
        .expect("validated generation");
    let generation = generation
        .checked_add(changed)
        .filter(|value| value.unsigned_abs() <= tobkiri_canon::MAX_SAFE_INTEGER)
        .ok_or_else(|| {
            json::strict_error("integer exceeds I-JSON safe range; use a canonical decimal string")
        })?;
    let generation = CoreValue::int(generation, budget).map_err(capacity)?;
    let snapshot = snapshot.object().expect("validated registry");
    let keys = [
        "schema",
        "generation",
        "updated_at",
        "profiles",
        "legacy",
        "bootstrap",
    ];
    let payload = CoreValue::object_shared(
        keys.into_iter().map(|key| {
            Ok((
                key,
                match key {
                    "generation" => generation.share(),
                    "updated_at" => timestamp.share(),
                    "profiles" => profiles.share(),
                    "legacy" => legacy.share(),
                    "bootstrap" => bootstrap.share(),
                    _ => snapshot.get(key).expect("validated registry field"),
                },
            ))
        }),
        budget,
    )
    .map_err(capacity)?;
    let digest = payload.digest_guest(budget).map_err(capacity)?;
    let digest = CoreValue::string(digest.as_str(), budget).map_err(capacity)?;
    let published = CoreValue::object_shared(
        snapshot.iter().map(|(key, _)| {
            Ok((
                key,
                if key == "store_digest" {
                    digest.share()
                } else {
                    payload.get_key(key).expect("validated registry field")
                },
            ))
        }),
        budget,
    )
    .map_err(capacity)?;
    let json = published.encode_guest(budget).map_err(capacity)?;
    let mut line = CoreBuffer::builder(budget, json.len() + 1);
    line.append(json.as_bytes()).map_err(capacity)?;
    line.append(b"\n").map_err(capacity)?;
    line.finish().map_err(capacity)
}

fn stored_from_entry(doc: &mut Document, entry: usize) -> StoredProfile {
    let revisions = array(doc, field(doc, entry, "revisions")).expect("validated history");
    let latest = *revisions.last().expect("nonempty");
    let profile = field(doc, latest, "profile");
    let profile = if matches!(doc.node(profile), Node::Scalar(CanonValue::Null)) {
        revisions[..revisions.len() - 1]
            .iter()
            .rev()
            .find_map(|&id| {
                let profile = field(doc, id, "profile");
                doc.entries(profile).map(|_| profile)
            })
    } else {
        Some(profile)
    };
    let profile = profile.unwrap_or_else(|| {
        let profile_id = field(doc, entry, "profile_id");
        push(
            doc,
            Node::Object(vec![(Text::from_str("profile_id"), profile_id)]),
        )
    });
    StoredProfile {
        references: [
            field(doc, entry, "profile_id"),
            field(doc, entry, "current_revision"),
            profile,
            field(doc, latest, "parent_revision"),
            field(doc, entry, "legacy_ids"),
        ],
        scalars: (
            integer(doc, field(doc, entry, "order")),
            boolean(doc, field(doc, entry, "tombstone")).expect("validated bool"),
            integer(doc, field(doc, entry, "created_at")),
            integer(doc, field(doc, entry, "updated_at")),
        ),
    }
}

pub(super) fn list<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &mut State,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Funded<Vec<StoredProfile>>> {
    let doc = &mut state.doc;
    let entries =
        indexes(array(doc, field(doc, doc.root, "profiles")).expect("validated profiles"))?;
    let capacity = op.reserve(
        OriginalAuthorityStage::Source,
        entries
            .len()
            .checked_mul(std::mem::size_of::<StoredProfile>() * 3)
            .and_then(|n| n.checked_add(8192))
            .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?,
    )?;
    let mut rows = Vec::new();
    rows.try_reserve_exact(entries.len())
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
    for entry in entries {
        charge(op, budget, 1)?;
        rows.push(stored_from_entry(doc, entry));
    }
    // Stable bottom-up merge sort allows each reached comparison to fail
    // funding/cancellation before comparison, without an infallible callback.
    let mut scratch = Vec::new();
    scratch
        .try_reserve_exact(rows.len())
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
    scratch.extend_from_slice(&rows);
    let mut width = 1_usize;
    while width < rows.len() {
        for start in (0..rows.len()).step_by(width * 2) {
            let middle = (start + width).min(rows.len());
            let end = (middle + width).min(rows.len());
            let (mut left, mut right) = (start, middle);
            for destination in &mut scratch[start..end] {
                charge(op, budget, 1)?;
                let take_left = if right == end {
                    true
                } else if left == middle {
                    false
                } else {
                    charge(
                        op,
                        budget,
                        2 + text(doc, rows[left].references[0]).expect("ID").0.len() as u64,
                    )?;
                    (rows[left].scalars.0, text(doc, rows[left].references[0]))
                        <= (rows[right].scalars.0, text(doc, rows[right].references[0]))
                };
                if take_left {
                    *destination = rows[left];
                    left += 1;
                } else {
                    *destination = rows[right];
                    right += 1;
                }
            }
        }
        std::mem::swap(&mut rows, &mut scratch);
        width = width
            .checked_mul(2)
            .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
    }
    drop(scratch);
    Ok(Funded {
        value: rows,
        _capacity: capacity,
    })
}
