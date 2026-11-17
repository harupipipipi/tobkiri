//! Bind the held activation journal to its live Authority reservation.
use super::*;
use crate::original_authority::io::{Store, abort_transaction, incomplete_reservations};
use recovery_workspace::{APP_DENIED, RecoveryWorkspace, pending};
use rusqlite::types::Value;
use tobkiri_canon::{CoreBuffer, CoreValue, Variant};

#[path = "original_recovery_committed_activation.rs"]
mod committed;

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::PendingReservation;

pub(super) fn abort_incomplete<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store<'_, '_, S>,
    profile_id: &str,
) -> Result<()> {
    // The earlier fetchall must finish before any transaction. Each row is
    // then fenced and audited independently, in the original token order.
    let inventory = incomplete_reservations::read(op, store, profile_id)?;
    for [id, state] in inventory.rows() {
        abort_transaction::abort(
            store,
            column_text(id, "reservation_id")?,
            column_text(state, "state")?,
        )?;
    }
    Ok(())
}

pub(super) fn recover<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store<'_, '_, S>,
    state: &Directory,
    journal: Funded<Document>,
    profile_id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<PendingRepublish>> {
    let render = |key| {
        let id = journal
            .get(journal.root, key)
            .expect("verified journal keys");
        definitions::render_python_str(op, budget, &journal, id)
    };
    let reservation = render("reservation_id")?;
    let Some(row) = incomplete_reservations::read_pending(
        op,
        store,
        text(&reservation),
        incomplete_reservations::BINDING_FIELDS,
    )?
    else {
        return Err(raw(
            APP_DENIED,
            "pending authority reservation is unavailable",
        ));
    };
    let activation = render("activation_id")?;
    let fence = journal
        .get(journal.root, "fencing_token")
        .expect("verified journal keys");
    let expected_fence = pending_fence(op, journal.node(fence))?;
    let [activation_col, fence_col, profile_col, state_col] = &row.value;
    let [activation_key, fence_key, profile_key, state_key] =
        incomplete_reservations::BINDING_FIELDS;
    let actual_activation = column_text(activation_col, activation_key)?;
    let actual_fence = column_fence(fence_col, fence_key)?;
    let actual_profile = column_text(profile_col, profile_key)?;
    if actual_activation != text(&activation)
        || expected_fence.decimal().parse::<i64>() != Ok(actual_fence)
        || actual_profile != profile_id
    {
        return Err(raw(
            APP_DENIED,
            "pending activation authority binding changed",
        ));
    }
    let envelope = render("envelope_path")?;
    let name = text(&envelope);
    // Match Path(name).name first. The secure directory still validates the
    // relative path independently, including a bare '..'.
    if name.is_empty() || name == "." || name.contains('/') {
        return Err(raw(
            APP_DENIED,
            "pending activation envelope path is invalid",
        ));
    }
    match column_text(state_col, state_key)? {
        "active" => {
            let (document, _) = with_envelope_path(op, name, |path| {
                check_committed_envelope(op, state, path, &journal, budget)
            })?;
            committed::revalidate(
                op,
                store,
                &document,
                committed::Binding {
                    reservation_id: text(&reservation),
                    activation_id: text(&activation),
                    profile_id,
                    fencing_token: actual_fence,
                },
            )?;
            let profile = committed_profile(op, &document, budget)?;
            Ok(Some(PendingRepublish {
                journal,
                envelope,
                reservation,
                activation,
                fencing_token: actual_fence,
                profile,
            }))
        }
        status @ ("prepared" | "ready_without_authority" | "committing" | "aborted") => {
            if status != "aborted" {
                abort_transaction::abort(store, text(&reservation), status)?;
            }
            unlink(op, state, Path::new("pending.json"))?;
            with_envelope_path(op, name, |path| unlink(op, state, path))?;
            Ok(None)
        }
        _ => Err(raw(
            APP_DENIED,
            "pending activation has an invalid authority state",
        )),
    }
}

fn with_envelope_path<S: OriginalAuthorityOwner, T>(
    op: &Operation<'_, S>,
    name: &str,
    use_path: impl FnOnce(&Path) -> Result<T>,
) -> Result<T> {
    let _path = op.reserve(STAGE, name.len().saturating_add(64))?;
    let mut path = PathBuf::with_capacity(name.len().saturating_add(16));
    path.push("activations");
    path.push(name);
    use_path(&path)
}

fn check_committed_envelope<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &Directory,
    path: &Path,
    journal: &Document,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<(Funded<Document>, Digest)> {
    let envelope = pending::read_state(op, state, path, "committed activation envelope")?;
    let changed = || raw(APP_DENIED, "committed activation envelope changed");
    if envelope.entries(envelope.root).is_none() {
        return Err(changed());
    }
    let bytes = json::canonical_subtree(&envelope, envelope.root, None, op, budget)?;
    let digest = Digest::hash(&bytes);
    let expected = journal
        .get(journal.root, "envelope_digest")
        .expect("verified journal keys");
    if !journal.string_is(expected, digest.as_str())
        || !envelope
            .get(envelope.root, "activation")
            .and_then(|id| envelope.get(id, "state"))
            .is_some_and(|id| envelope.string_is(id, "active"))
    {
        return Err(changed());
    }
    Ok((envelope, digest))
}

pub(super) struct PendingRepublish {
    journal: Funded<Document>,
    envelope: CoreBuffer,
    reservation: CoreBuffer,
    activation: CoreBuffer,
    fencing_token: i64,
    pub(super) profile: CoreValue,
}

impl PendingRepublish {
    /// Reacquire the same lock. The caller has already verified bytes unlocked.
    pub(super) fn publish<S: OriginalAuthorityOwner>(
        self,
        op: &Operation<'_, S>,
        store: &Store<'_, '_, S>,
        workspace: &RecoveryWorkspace,
        profile_id: &str,
        budget: &Arc<dyn AllocationBudget>,
        require_artifact: impl FnOnce(&CoreValue) -> Result<()>,
    ) -> Result<()> {
        workspace.republish(op, |state, workspace_digest| {
            let path = Path::new(pending::NAME);
            if !state.exists(op, pending::NAME)? {
                return Ok(());
            }
            // Digest before interpreting fields. A different pending record
            // belongs to the next recovery iteration, including other shapes.
            let current = pending::read_state(op, state, path, "pending activation journal")?;
            let digest = |doc: &Document| {
                json::canonical_subtree(doc, doc.root, None, op, budget)
                    .map(|bytes| Digest::hash(&bytes))
            };
            if digest(&current)? != digest(&self.journal)? {
                return Ok(());
            }
            let name = text(&self.envelope);
            let (envelope, envelope_digest) = with_envelope_path(op, name, |path| {
                check_committed_envelope(op, state, path, &current, budget)
            })?;
            committed::revalidate(
                op,
                store,
                &envelope,
                committed::Binding {
                    reservation_id: text(&self.reservation),
                    activation_id: text(&self.activation),
                    profile_id,
                    fencing_token: self.fencing_token,
                },
            )?;
            require_artifact(&committed_profile(op, &envelope, budget)?)?;
            let activation = text(&self.activation);
            let _write_capacity = op.reserve(
                STAGE,
                activation
                    .len()
                    .saturating_add(name.len())
                    .saturating_mul(16)
                    .saturating_add(8192),
            )?;
            let payload = object([
                (
                    "schema",
                    CanonValue::Str(workspace_active::POINTER_SCHEMA.into()),
                ),
                ("activation_id", CanonValue::Str(activation.into())),
                ("envelope_path", CanonValue::Str(name.into())),
                (
                    "envelope_digest",
                    CanonValue::Str(envelope_digest.as_str().into()),
                ),
                (
                    "workspace_digest",
                    CanonValue::Str(workspace_digest.as_str().into()),
                ),
            ]);
            let mut bytes = tobkiri_canon::encode(&payload, Variant::StrictV1)
                .map_err(|_| OriginalAuthorityError::unavailable())?;
            bytes.push(b'\n');
            state
                .replace_activation_pointer(op, &bytes)
                .map_err(persistence_error)?;
            unlink(op, state, path)
        })
    }
}

fn committed_profile<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    envelope: &Document,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let profile = subscript(envelope, envelope.root, "profile")?;
    if envelope.entries(profile).is_none() {
        return Err(raw(APP_DENIED, "committed activation profile is invalid"));
    }
    let bytes = json::canonical_subtree(envelope, profile, None, op, budget)?;
    CoreValue::parse_guest(&bytes, budget).map_err(|_| OriginalAuthorityError::capacity(STAGE))
}

fn text(buffer: &CoreBuffer) -> &str {
    buffer.as_str().expect("Python str rendered as valid UTF-8")
}

fn column<'a>(column: &'a Option<Value>, key: &str) -> Result<&'a Value> {
    column
        .as_ref()
        .ok_or_else(|| raw("builtins.KeyError", format!("'{key}'")))
}

fn column_text<'a>(field: &'a Option<Value>, key: &str) -> Result<&'a str> {
    if let Value::Text(text) = column(field, key)? {
        Ok(text)
    } else {
        Err(OriginalAuthorityError::unsupported(
            STAGE,
            "non-text Authority reservation column",
        ))
    }
}

fn column_fence(field: &Option<Value>, key: &str) -> Result<i64> {
    if let Value::Integer(value) = column(field, key)? {
        Ok(*value)
    } else {
        Err(OriginalAuthorityError::unsupported(
            STAGE,
            "non-integer Authority fencing token",
        ))
    }
}

fn pending_fence<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    node: &Node,
) -> Result<Funded<OriginalAuthorityEpoch>> {
    let size = match node {
        Node::String(value) => value.0.len().saturating_mul(16),
        _ => 0,
    };
    let capacity = op.reserve(STAGE, size.saturating_add(256))?;
    let value = match node {
        Node::Scalar(CanonValue::Int(value)) => OriginalAuthorityEpoch(value.to_string()),
        Node::Scalar(CanonValue::Bool(value)) => {
            OriginalAuthorityEpoch(if *value { "1" } else { "0" }.into())
        }
        Node::String(value) => integer(&value.utf8()?, STAGE)?,
        other => {
            let kind = match other {
                Node::Scalar(CanonValue::Null) => "NoneType",
                Node::Array(_) => "list",
                Node::Object(_) => "dict",
                _ => unreachable!("verified strict pending JSON"),
            };
            return Err(raw(
                "builtins.TypeError",
                format!(
                    "int() argument must be a string, a bytes-like object or a real number, not '{kind}'"
                ),
            ));
        }
    };
    Ok(Funded {
        value,
        _capacity: capacity,
    })
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
            raw(APP_DENIED, "activation persistence is unavailable"),
            error,
        )
    } else {
        error
    }
}
