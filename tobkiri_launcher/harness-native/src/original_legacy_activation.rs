//! The original `_activate_locked` commit for a verified legacy successor.
use super::*;
use crate::original_authority::io::{activation_authority, incomplete_reservations};
use rusqlite::types::Value;
use tobkiri_canon::{
    AllocationError, AllocationKind, AllocationRequest, CoreBuffer, CoreRef, CoreValue,
};

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const ENVELOPE_SCHEMA: &str = "io.tobkiri.defaultspack-activation-envelope.v1";
const PENDING_SCHEMA: &str = "io.tobkiri.defaultspack-pending-activation.v1";

pub(super) struct Input<'a> {
    pub(super) profile_id: &'a str,
    pub(super) resolved: &'a CoreValue,
    pub(super) created_at: &'a CoreValue,
    pub(super) schemas: BoundProfileSchemas,
}

/// Commit while holding the same workspace process lock and Authority Store.
///
/// The caller must first perform the original `_recover_locked` again under
/// this lock, including incomplete-reservation cleanup. The candidate must
/// have been freshly resolved and compared with the legacy records here, and
/// its selected artifact must already have been verified outside this lock.
pub(super) fn activate<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store<'_, '_, S>,
    state: &Directory,
    workspace_digest: &Digest,
    budget: &Arc<dyn AllocationBudget>,
    input: Input<'_>,
    require_artifact: impl FnMut(&CoreValue) -> Result<()>,
) -> Result<()> {
    activate_with_fault(
        op,
        store,
        state,
        workspace_digest,
        budget,
        input,
        (require_artifact, |_| Ok(())),
    )
}

fn activate_with_fault<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store<'_, '_, S>,
    state: &Directory,
    workspace_digest: &Digest,
    budget: &Arc<dyn AllocationBudget>,
    input: Input<'_>,
    (mut require_artifact, mut fault): (
        impl FnMut(&CoreValue) -> Result<()>,
        impl FnMut(&'static str) -> Result<()>,
    ),
) -> Result<()> {
    let profile = field(input.resolved, "profile")?;
    let lock = field(input.resolved, "lock")?;
    let plan = field(input.resolved, "plan")?;
    let plan_digest = field(&plan, "plan_digest")?;
    let authority_digest = field(&profile, "profile_authority_snapshot_digest")?;
    let epoch = field(&plan, "security_epoch")?;
    let _id_capacity = op.reserve(STAGE, input.profile_id.len().saturating_add(128))?;
    let activation_id = format!(
        "activation:{}-migration-{}",
        input.profile_id,
        &text(&plan_digest)?
            .strip_prefix("sha256:")
            .unwrap_or(text(&plan_digest)?)[..16],
    );
    let reservation = activation_authority::reserve(
        store,
        activation_authority::ReservationInput {
            activation_id: &activation_id,
            profile_id: input.profile_id,
            plan_digest: text(&plan_digest)?,
            profile_authority_digest: text(&authority_digest)?,
            security_epoch: epoch.as_i64().ok_or_else(invalid_scalar)?,
        },
    )?;
    // Original computes this path after reserving, but before its try block.
    let _paths = op.reserve(
        STAGE,
        activation_id.len().saturating_mul(3).saturating_add(128),
    )?;
    let envelope_name = format!("{}.json", &activation_id[11..]);
    let envelope_path = Path::new("activations").join(&envelope_name);
    let binding = pending_reservation::Binding {
        reservation_id: &reservation.reservation_id,
        activation_id: &activation_id,
        profile_id: input.profile_id,
        fencing_token: reservation.fencing_token,
    };
    let mut current_state = "prepared";
    let outcome = (|| {
        for (next, generation) in [
            ("prepared", 1),
            ("ready_without_authority", 2),
            ("committing", 3),
        ] {
            if next != "prepared" {
                activation_authority::transition(
                    store,
                    &reservation.reservation_id,
                    current_state,
                    next,
                )?;
                current_state = next;
            }
            let activation = activation_record(
                op,
                budget,
                input.schemas,
                [&profile, &lock, &plan],
                RecordState {
                    activation_id: &activation_id,
                    state: next,
                    generation,
                    fencing_token: reservation.fencing_token,
                    created_at: input.created_at,
                },
            )?;
            let envelope = envelope(
                budget,
                workspace_digest,
                [&profile, &lock, &plan],
                activation,
            )?;
            let bytes = encode(&envelope, budget)?;
            write_state(op, state, &envelope_path, &bytes, budget)?;
            // The original intentionally leaves the ready pending digest in
            // place while writing the committing envelope.
            if next != "committing" {
                let journal = pending_record(
                    budget,
                    workspace_digest,
                    &binding,
                    &envelope_name,
                    &Digest::hash(bytes.as_bytes()),
                )?;
                write_value(op, state, Path::new("pending.json"), &journal, budget)?;
            }
            fault(next)?;
        }
        let activation = activation_record(
            op,
            budget,
            input.schemas,
            [&profile, &lock, &plan],
            RecordState {
                activation_id: &activation_id,
                state: "active",
                generation: 4,
                fencing_token: reservation.fencing_token,
                created_at: input.created_at,
            },
        )?;
        let envelope = envelope(
            budget,
            workspace_digest,
            [&profile, &lock, &plan],
            activation,
        )?;
        let bytes = encode(&envelope, budget)?;
        let envelope_digest = Digest::hash(bytes.as_bytes());
        write_state(op, state, &envelope_path, &bytes, budget)?;
        let journal = pending_record(
            budget,
            workspace_digest,
            &binding,
            &envelope_name,
            &envelope_digest,
        )?;
        write_value(op, state, Path::new("pending.json"), &journal, budget)?;
        fault("before_authority_commit")?;
        require_artifact(&profile)?;
        activation_authority::transition(
            store,
            &reservation.reservation_id,
            current_state,
            "active",
        )?;
        current_state = "active";
        fault("after_authority_commit")?;
        revalidate(op, store, budget, [&profile, &plan], binding)?;
        require_artifact(&profile)?;
        let pointer = object_value(
            budget,
            [
                ("schema", string(workspace_active::POINTER_SCHEMA, budget)?),
                ("activation_id", string(&activation_id, budget)?),
                ("envelope_path", string(&envelope_name, budget)?),
                ("envelope_digest", string(envelope_digest.as_str(), budget)?),
                (
                    "workspace_digest",
                    string(workspace_digest.as_str(), budget)?,
                ),
            ],
        )?;
        write_value(op, state, Path::new("active.json"), &pointer, budget)?;
        unlink(op, state, Path::new("pending.json"))
    })();
    if outcome.is_err() && current_state != "active" {
        let abort = (|| {
            let row = incomplete_reservations::read_pending(
                op,
                store,
                &reservation.reservation_id,
                ["state"],
            )?;
            if row.as_ref().is_some_and(
                |row| matches!(row[0].as_ref(), Some(Value::Text(value)) if value == current_state),
            ) {
                activation_authority::transition(
                    store,
                    &reservation.reservation_id,
                    current_state,
                    "aborted",
                )?;
            }
            Ok(())
        })();
        // Python's finally runs even if the read or abort raises; each later
        // cleanup failure replaces the preceding exception with its context.
        let cleanup = (|| {
            unlink(op, state, Path::new("pending.json"))?;
            unlink(op, state, &envelope_path)
        })();
        return finish_scope(finish_scope(outcome, abort), cleanup);
    }
    outcome
}

fn capacity(_: AllocationError) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(STAGE)
}

fn invalid_scalar() -> OriginalAuthorityError {
    OriginalAuthorityError::unsupported(STAGE, "validated legacy successor scalar")
}

fn field(value: &CoreValue, name: &str) -> Result<CoreValue> {
    value
        .get_key(name)
        .ok_or_else(|| raw("builtins.KeyError", format!("'{name}'")))
}

fn text(value: &CoreValue) -> Result<&str> {
    value.as_str().ok_or_else(invalid_scalar)
}

fn string(value: &str, budget: &Arc<dyn AllocationBudget>) -> Result<CoreValue> {
    CoreValue::string(value, budget).map_err(capacity)
}

fn object_value<const N: usize>(
    budget: &Arc<dyn AllocationBudget>,
    fields: [(&str, CoreValue); N],
) -> Result<CoreValue> {
    CoreValue::object_shared(fields.into_iter().map(Ok), budget).map_err(capacity)
}

struct RecordState<'a> {
    activation_id: &'a str,
    state: &'a str,
    generation: i64,
    fencing_token: i64,
    created_at: &'a CoreValue,
}

fn activation_record<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    schemas: BoundProfileSchemas,
    [profile, lock, plan]: [&CoreValue; 3],
    RecordState {
        activation_id,
        state,
        generation,
        fencing_token,
        created_at,
    }: RecordState<'_>,
) -> Result<CoreValue> {
    let token = if fencing_token.unsigned_abs() <= tobkiri_canon::MAX_SAFE_INTEGER {
        Some(CoreValue::int(fencing_token, budget).map_err(capacity)?)
    } else {
        None
    };
    let fields = [
        (
            "activation_api_version",
            Some(string("io.tobkiri.activation-record.v2", budget)?),
        ),
        ("profile_id", Some(field(profile, "profile_id")?)),
        ("profile_revision", Some(field(plan, "profile_revision")?)),
        ("activation_id", Some(string(activation_id, budget)?)),
        ("state", Some(string(state, budget)?)),
        (
            "state_generation",
            Some(CoreValue::int(generation, budget).map_err(capacity)?),
        ),
        ("catalog_revision", Some(field(plan, "catalog_revision")?)),
        ("bundle_digest", Some(field(plan, "bundle_digest")?)),
        ("lock_digest", Some(field(lock, "lock_digest")?)),
        ("plan_digest", Some(field(plan, "plan_digest")?)),
        ("closure_digest", Some(field(plan, "closure_digest")?)),
        (
            "profile_authority_snapshot_digest",
            Some(field(profile, "profile_authority_snapshot_digest")?),
        ),
        ("security_epoch", Some(field(plan, "security_epoch")?)),
        ("fencing_token", token),
        ("created_at", Some(created_at.clone())),
        ("committed_at", Some(created_at.clone())),
    ];
    let fields = fields
        .into_iter()
        .take(if state == "active" { 16 } else { 15 });
    if token_is_unsafe(fencing_token) {
        // Schema validation precedes canonical_json in the original. Preserve
        // the actual SQLite token even when CoreValue cannot represent it.
        let bound = fields
            .clone()
            .map(|(key, value)| {
                key.len()
                    + value
                        .as_ref()
                        .and_then(CoreValue::as_str)
                        .map_or(32, str::len)
            })
            .sum::<usize>();
        let _owned = op.reserve(STAGE, bound.saturating_mul(32).saturating_add(8192))?;
        let mut values = std::collections::BTreeMap::new();
        for (key, value) in fields {
            let scalar = match value {
                None => CanonValue::Int(fencing_token),
                Some(value) => match (value.as_str(), value.as_i64()) {
                    (Some(value), _) => CanonValue::Str(value.to_owned()),
                    (_, Some(value)) => CanonValue::Int(value),
                    _ => return Err(invalid_scalar()),
                },
            };
            values.insert(key.to_owned(), scalar);
        }
        let bytes = tobkiri_canon::encode(
            &CanonValue::Obj(values),
            tobkiri_canon::Variant::LegacyFloat,
        )
        .map_err(|_| invalid_scalar())?;
        let document = json::decode_catalog_manifest(&bytes, op, budget)?;
        workspace_active::validate_activation_document(op, budget, schemas, &document)?;
        return Err(json::strict_error(
            "integer exceeds I-JSON safe range; use a canonical decimal string",
        ));
    }
    let record = CoreValue::object_shared(
        fields.map(|(key, value)| Ok((key, value.expect("safe token")))),
        budget,
    )
    .map_err(capacity)?;
    workspace_active::validate_activation(op, budget, schemas, &record)?;
    Ok(record)
}

fn token_is_unsafe(token: i64) -> bool {
    token.unsigned_abs() > tobkiri_canon::MAX_SAFE_INTEGER
}

fn envelope(
    budget: &Arc<dyn AllocationBudget>,
    workspace_digest: &Digest,
    [profile, lock, plan]: [&CoreValue; 3],
    activation: CoreValue,
) -> Result<CoreValue> {
    object_value(
        budget,
        [
            ("schema", string(ENVELOPE_SCHEMA, budget)?),
            (
                "workspace_digest",
                string(workspace_digest.as_str(), budget)?,
            ),
            ("profile", profile.clone()),
            ("lock", lock.clone()),
            ("plan", plan.clone()),
            ("activation", activation),
        ],
    )
}

fn pending_record(
    budget: &Arc<dyn AllocationBudget>,
    workspace_digest: &Digest,
    binding: &pending_reservation::Binding<'_>,
    envelope_name: &str,
    envelope_digest: &Digest,
) -> Result<CoreValue> {
    object_value(
        budget,
        [
            ("schema", string(PENDING_SCHEMA, budget)?),
            ("reservation_id", string(binding.reservation_id, budget)?),
            ("activation_id", string(binding.activation_id, budget)?),
            ("envelope_path", string(envelope_name, budget)?),
            ("envelope_digest", string(envelope_digest.as_str(), budget)?),
            (
                "workspace_digest",
                string(workspace_digest.as_str(), budget)?,
            ),
            (
                "fencing_token",
                CoreValue::int(binding.fencing_token, budget).map_err(capacity)?,
            ),
        ],
    )
}

fn revalidate<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store<'_, '_, S>,
    budget: &Arc<dyn AllocationBudget>,
    [profile, plan]: [&CoreValue; 2],
    binding: pending_reservation::Binding<'_>,
) -> Result<()> {
    let profile = object_value(
        budget,
        [(
            "profile_authority_snapshot_digest",
            field(profile, "profile_authority_snapshot_digest")?,
        )],
    )?;
    let plan = object_value(
        budget,
        [
            ("plan_digest", field(plan, "plan_digest")?),
            ("security_epoch", field(plan, "security_epoch")?),
        ],
    )?;
    let selected = object_value(budget, [("profile", profile), ("plan", plan)])?;
    let bytes = encode(&selected, budget)?;
    let document = json::decode_read_only_strict(bytes.as_bytes(), op, budget)?;
    pending_reservation::revalidate_active(op, store, &document, binding)
}

fn encode(value: &CoreValue, budget: &Arc<dyn AllocationBudget>) -> Result<CoreBuffer> {
    match value.encode_guest(budget) {
        Ok(bytes) => Ok(bytes),
        Err(AllocationError::Limit) => {
            // The encoder uses Limit for both its 4 MiB JSON bound and an
            // owner's work/storage refusal. Distinguish them without another
            // serialization allocation or a replacement allocation owner.
            if encoded_exceeds_limit(value, budget)? {
                Err(json::strict_error("canonical JSON exceeds size limit"))
            } else {
                Err(capacity(AllocationError::Limit))
            }
        }
        Err(AllocationError::Depth) => Err(json::strict_error("json nesting exceeds depth limit")),
        Err(other) => Err(capacity(other)),
    }
}

fn encoded_exceeds_limit(value: &CoreValue, budget: &Arc<dyn AllocationBudget>) -> Result<bool> {
    fn work(budget: &Arc<dyn AllocationBudget>, units: usize) -> Result<()> {
        budget
            .reserve(AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes: 0,
                depth: 0,
                elements: 0,
                work: units as u64,
            })
            .map_err(capacity)
    }
    fn text_size(text: &str, budget: &Arc<dyn AllocationBudget>) -> Result<usize> {
        work(budget, text.len())?;
        Ok(2 + text
            .bytes()
            .map(|byte| match byte {
                b'"' | b'\\' | b'\x08' | b'\t' | b'\n' | b'\x0c' | b'\r' => 2,
                0..=31 => 6,
                _ => 1,
            })
            .sum::<usize>())
    }
    fn size(value: &CoreValue, budget: &Arc<dyn AllocationBudget>, depth: usize) -> Result<usize> {
        if depth > tobkiri_canon::MAX_DEPTH {
            return Err(json::strict_error("json nesting exceeds depth limit"));
        }
        work(budget, 1)?;
        let mut total = match value.inspect() {
            CoreRef::Null => 4,
            CoreRef::Bool(value) => {
                if value {
                    4
                } else {
                    5
                }
            }
            CoreRef::Int(value) => {
                value
                    .unsigned_abs()
                    .checked_ilog10()
                    .map_or(1, |digits| digits as usize + 1)
                    + usize::from(value < 0)
            }
            CoreRef::Str(text) => text_size(text, budget)?,
            CoreRef::Arr(items) => {
                let mut total = 2 + items.len().saturating_sub(1);
                for item in items.iter() {
                    total += size(&item, budget, depth + 1)?;
                    if total > tobkiri_canon::MAX_BYTES {
                        break;
                    }
                }
                total
            }
            CoreRef::Obj(items) => {
                let mut total = 2 + items.len().saturating_sub(1) + items.len();
                for (key, item) in items.iter() {
                    total += text_size(key, budget)? + size(&item, budget, depth + 1)?;
                    if total > tobkiri_canon::MAX_BYTES {
                        break;
                    }
                }
                total
            }
            CoreRef::BigInt(_) | CoreRef::Float(_) => return Err(invalid_scalar()),
        };
        // Saturation here bounds sums even for shared graphs whose complete
        // unfolded encoding would exceed addressable storage.
        total = total.min(tobkiri_canon::MAX_BYTES + 1);
        Ok(total)
    }
    Ok(size(value, budget, 0)? > tobkiri_canon::MAX_BYTES)
}

fn write_value<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &Directory,
    path: &Path,
    value: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    write_state(op, state, path, &encode(value, budget)?, budget)
}

fn write_state<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &Directory,
    path: &Path,
    bytes: &CoreBuffer,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let mut line = CoreBuffer::builder(budget, bytes.len() + 1);
    line.append(bytes.as_bytes()).map_err(capacity)?;
    line.append(b"\n").map_err(capacity)?;
    let line = line.finish().map_err(capacity)?;
    state
        .replace_activation_state(op, path, line.as_bytes())
        .map_err(persistence_error)
}

fn unlink<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &Directory,
    path: &Path,
) -> Result<()> {
    state.unlink_if_present(op, path).map_err(persistence_error)
}

fn persistence_error(error: OriginalAuthorityError) -> OriginalAuthorityError {
    if error.class == RAW_IO || error.class == SECURE_PERSISTENCE_ERROR {
        caused(
            raw(
                recovery_workspace::APP_DENIED,
                "activation persistence is unavailable",
            ),
            error,
        )
    } else {
        error
    }
}

#[cfg(test)]
#[path = "original_legacy_activation_tests.rs"]
mod tests;
