//! The fixed old ActiveProfileStore read and verified recovery CAS.
//! Snapshot integrity is candidate DATA, never an Authority or approval check.
use super::*;
use crate::original_authority::lease::model_text;
use model::{Pointer, equal_atom, pointer_error, validate_pointer};
use tobkiri_canon::{AllocationBudget, CoreBuffer, CoreValue};

const ACTIVE: &str = "active.json";
const SCHEMA: &str = "io.tobkiri.active-profile-pointer.v1";
const REQUEST_FIELDS: [&str; 8] = [
    "profile_id",
    "profile_revision",
    "activation_id",
    "plan_digest",
    "lock_digest",
    "activation_snapshot_path",
    "activation_snapshot_digest",
    "catalog_revision",
];

pub(super) struct LoadedPointer {
    pub(super) doc: Funded<Document>,
    pub(super) pointer: Pointer,
}

impl LoadedPointer {
    pub(super) fn profile_id(&self) -> &Text {
        match self.doc.node(self.pointer.field("profile_id")) {
            Node::String(value) => value,
            _ => unreachable!("verified active Profile ID"),
        }
    }
}

fn missing(error: &OriginalAuthorityError) -> bool {
    error.class == "builtins.FileNotFoundError"
        || (error.class == RAW_IO
            && matches!(&error.cause, Some(OriginalAuthorityCause::Io(error)) if error.kind == std::io::ErrorKind::NotFound))
}

fn read_error(error: OriginalAuthorityError, message: &'static str) -> OriginalAuthorityError {
    if persistence_failure(&error) {
        caused(pointer_error(message), error)
    } else {
        error
    }
}

fn persistence_failure(error: &OriginalAuthorityError) -> bool {
    matches!(
        error.class,
        RAW_IO
            | "builtins.FileNotFoundError"
            | "builtins.ValueError"
            | "tobkiri_protocol.errors.CanonicalizationError"
            | SECURE_PERSISTENCE_ERROR
    )
}

pub(super) fn load<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    requested: &Path,
) -> Result<Option<LoadedPointer>> {
    load_with_decoder(op, requested, None)
}

/// Verify the predecessor in recovery without the legacy wide JSON reservation.
pub(super) fn load_verified<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    requested: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<LoadedPointer>> {
    load_with_decoder(op, requested, Some(budget))
}

fn load_with_decoder<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    requested: &Path,
    budget: Option<&Arc<dyn AllocationBudget>>,
) -> Result<Option<LoadedPointer>> {
    let (directory, user_directory) = directories(op, requested)?;
    let current = load_pointer(op, &directory)?;
    if let Some(LoadedPointer { doc, pointer }) = &current {
        verify_snapshot(op, &user_directory, doc, pointer, budget, false)?;
    }
    Ok(current)
}

fn directories<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    requested: &Path,
) -> Result<(Directory, Directory)> {
    let _paths = op.reserve(
        OriginalAuthorityStage::Path,
        requested
            .as_os_str()
            .len()
            .saturating_mul(16)
            .saturating_add(8192),
    )?;
    op.check(OriginalAuthorityStage::Path)?;
    if requested.is_symlink() {
        return Err(pointer_error("Host state root must not be a symlink"));
    }
    let requested = std::path::absolute(requested).map_err(|error| raw_io(error, None))?;
    let (user_data, profiles) = if requested.file_name().is_some_and(|name| name == "profiles") {
        (
            requested
                .parent()
                .expect("absolute profiles parent")
                .to_owned(),
            requested,
        )
    } else {
        let profiles = requested.join("profiles");
        (requested, profiles)
    };
    op.check(OriginalAuthorityStage::Path)?;
    if profiles.is_symlink() {
        return Err(pointer_error(
            "Profile authority directory must not be a symlink",
        ));
    }
    // Both actual constructors precede load, including the missing-pointer branch.
    let directory = Directory::capture(op, &profiles, true)?;
    let user_directory = Directory::capture(op, &user_data, true)?;
    Ok((directory, user_directory))
}

fn load_pointer<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    directory: &Directory,
) -> Result<Option<LoadedPointer>> {
    let read = (|| {
        if !directory.exists(op, ACTIVE)? {
            return Ok(None);
        }
        let bytes = directory.read(op, ACTIVE)?;
        json::decode_strict(&bytes, op).map(Some)
    })();
    let Some(mut doc) = (match read {
        Err(error) if missing(&error) => return Ok(None),
        result => {
            result.map_err(|error| read_error(error, "active Profile pointer is unavailable"))?
        }
    }) else {
        return Ok(None);
    };
    if !matches!(doc.node(doc.root), Node::Object(_)) {
        return Err(pointer_error("active Profile pointer is not an object"));
    }
    // All temporary model strings are prepaid before the actual parser. Its
    // funded arena remains live until the same recovery scope finishes.
    let _model_capacity = op.reserve(
        OriginalAuthorityStage::Source,
        doc.source_bytes().saturating_mul(4).saturating_add(4096),
    )?;
    let root = doc.root;
    let pointer = validate_pointer(op, &mut doc, root)?;
    Ok(Some(LoadedPointer { doc, pointer }))
}

/// Commit only a previously loaded activation envelope which still has the
/// same canonical digest and exact identity while holding the Host CAS lock.
pub(super) fn commit_verified<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    request: &CoreValue,
    expected: Option<(&Document, &Pointer)>,
    clock: impl FnOnce() -> Result<i64>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let (directory, user_directory) = directories(op, root)?;
    let request = REQUEST_FIELDS.map(|name| {
        request
            .get_key(name)
            .filter(|value| value.as_str().is_some())
    });
    if request.iter().any(Option::is_none)
        || request[..5]
            .iter()
            .flatten()
            .any(|value| value.as_str().is_some_and(str::is_empty))
    {
        return Err(store_error("complete active Profile identity is required"));
    }
    let request = request.map(|value| value.expect("required request field"));
    // Validate the requested identity before locking, as the original
    // constructor does. Generation and timestamp acquire meaning under CAS.
    let draft = encode_pointer(&request, 1, 0, budget)?;
    let mut draft = json::decode_strict(draft.as_bytes(), op)?;
    let root = draft.root;
    let _model_capacity = op.reserve(
        OriginalAuthorityStage::Source,
        draft.source_bytes().saturating_mul(4).saturating_add(4096),
    )?;
    let pointer = validate_pointer(op, &mut draft, root)?;
    let name = LockName::HostSelection;
    let mut guard = PublicationLock::acquire_named(op, &directory, name)?;
    let result = (|| {
        let current = load_pointer(op, &directory)?;
        match (&current, expected) {
            (Some(_), None) => {
                return Err(conflict(
                    "active Profile replacement requires its current predecessor",
                ));
            }
            (Some(current), Some((doc, pointer)))
                if current.pointer.matches(&current.doc, pointer, doc) => {}
            (_, Some(_)) => return Err(conflict("active Profile predecessor is stale")),
            (None, None) => {}
        }
        // The digest came from the caller's full canonical envelope. The
        // second, pinned read under this lock must match it before publication.
        verify_snapshot(op, &user_directory, &draft, &pointer, Some(budget), true)?;
        let generation = current.as_ref().map_or(1, |current| {
            current.pointer.integer(&current.doc, "generation") + 1
        });
        let now = clock()?.max(0);
        let write = (|| {
            let json = encode_pointer(&request, generation, now, budget)?;
            let mut line = CoreBuffer::builder(budget, json.len() + 1);
            line.append(json.as_bytes()).map_err(capacity)?;
            line.append(b"\n").map_err(capacity)?;
            let line = line.finish().map_err(capacity)?;
            directory.replace_activation_pointer(op, line.as_bytes())
        })();
        write.map_err(|error| {
            if persistence_failure(&error) {
                caused(
                    store_error("active Profile pointer could not be committed"),
                    error,
                )
            } else {
                error
            }
        })?;
        directory.validate_file(op, name.text(), guard.file.as_ref().expect("Host CAS lock"))
    })();
    finish_scope(result, guard.close())
}

fn store_error(message: &'static str) -> OriginalAuthorityError {
    raw(
        "core_runtime.active_profile_store_v4.ActiveProfileStoreError",
        message,
    )
}

fn conflict(message: &'static str) -> OriginalAuthorityError {
    raw(
        "core_runtime.active_profile_store_v4.ActiveProfileStoreConflict",
        message,
    )
}

fn capacity(_: tobkiri_canon::AllocationError) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}

fn encode_pointer(
    request: &[CoreValue; 8],
    generation: i64,
    now: i64,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
    if [generation, now]
        .into_iter()
        .any(|value| value.unsigned_abs() > tobkiri_canon::MAX_SAFE_INTEGER)
    {
        return Err(json::strict_error(
            "integer exceeds I-JSON safe range; use a canonical decimal string",
        ));
    }
    let metadata = [
        (
            "schema",
            CoreValue::string(SCHEMA, budget).map_err(capacity)?,
        ),
        (
            "generation",
            CoreValue::int(generation, budget).map_err(capacity)?,
        ),
        ("updated_at", CoreValue::int(now, budget).map_err(capacity)?),
    ];
    let field = |index: usize| {
        if let Some((key, value)) = metadata.get(index) {
            (*key, value.share())
        } else {
            let index = index - metadata.len();
            (REQUEST_FIELDS[index], request[index].share())
        }
    };
    let fields = metadata.len() + REQUEST_FIELDS.len();
    let payload = CoreValue::object_shared((0..fields).map(|index| Ok(field(index))), budget)
        .map_err(capacity)?;
    let digest = payload.digest_guest(budget).map_err(capacity)?;
    let digest = CoreValue::string(digest.as_str(), budget).map_err(capacity)?;
    let signed = CoreValue::object_shared(
        (0..fields + 1).map(|index| {
            Ok(if index == fields {
                ("pointer_digest", digest.share())
            } else {
                field(index)
            })
        }),
        budget,
    )
    .map_err(capacity)?;
    signed.encode_guest(budget).map_err(capacity)
}

fn verify_snapshot<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    user_directory: &Directory,
    pointer_doc: &Document,
    pointer: &Pointer,
    budget: Option<&Arc<dyn AllocationBudget>>,
    publication: bool,
) -> Result<()> {
    // from_mapping already validates the exact relative binding. The repeated
    // _relative_to_root checks operate on the same immutable pointer fields.
    let _path_capacity = op.reserve(OriginalAuthorityStage::Path, 4096)?;
    let relative = pointer.text(pointer_doc, "activation_snapshot_path")?;
    let bytes = user_directory
        .read_relative(op, Path::new(&relative))
        .map_err(|error| read_error(error, "active Profile activation snapshot is unavailable"))?;
    let snapshot = match budget {
        Some(budget) => json::decode_read_only_strict(&bytes, op, budget),
        None => json::decode_strict(&bytes, op),
    }
    .map_err(|error| {
        read_error(
            error,
            if publication {
                "active Profile activation snapshot is invalid"
            } else {
                "active Profile activation snapshot is unavailable"
            },
        )
    })?;
    drop(bytes);
    if !matches!(snapshot.node(snapshot.root), Node::Object(_)) {
        return Err(pointer_error("activation snapshot is not an object"));
    }
    // Hash the WHOLE decoded snapshot before examining its envelope or any
    // identity. Wrapper metadata remains part of the pinned digest.
    let canonical = match budget {
        Some(budget) => json::canonical_subtree(&snapshot, snapshot.root, None, op, budget)?,
        None => json::canonical(&snapshot, None, op)?,
    };
    if !pointer_doc.string_is(
        pointer.field("activation_snapshot_digest"),
        Digest::hash(&canonical).as_str(),
    ) {
        return Err(pointer_error(if publication {
            "activation snapshot differs from its on-disk envelope"
        } else {
            "activation snapshot digest does not match active pointer"
        }));
    }
    drop(canonical);
    let envelope = snapshot
        .get(snapshot.root, "envelope")
        .filter(|&id| matches!(snapshot.node(id), Node::Object(_)))
        .unwrap_or(snapshot.root);
    let records = ["profile", "lock", "plan", "activation"].map(|name| {
        snapshot
            .get(envelope, name)
            .filter(|&id| matches!(snapshot.node(id), Node::Object(_)))
    });
    let [Some(profile), Some(lock), Some(plan), Some(activation)] = records else {
        return Err(pointer_error("activation snapshot records are incomplete"));
    };
    let fields = &REQUEST_FIELDS[..5];
    let actual = [
        snapshot.get(profile, "profile_id"),
        snapshot.get(plan, "profile_revision"),
        snapshot.get(activation, "activation_id"),
        snapshot.get(plan, "plan_digest"),
        snapshot.get(lock, "lock_digest"),
    ];
    let matches_identity = if actual
        .iter()
        .all(|id| id.is_none_or(|id| matches!(snapshot.node(id), Node::String(_))))
    {
        // The pointer strings are nonempty. str(value or '') for a string
        // or missing field needs no rendering of the decoded envelope.
        fields.iter().zip(actual).all(|(field, id)| {
            id.is_some_and(|id| {
                equal_atom(snapshot.node(id), pointer_doc.node(pointer.field(field)))
            })
        })
    } else {
        // Preserve Python's eager coercion of all five values for old
        // snapshots that contain numbers or containers in identity fields.
        let _tuple_capacity = op.reserve(
            OriginalAuthorityStage::Source,
            snapshot
                .source_bytes()
                .saturating_mul(12)
                .saturating_add(snapshot.node_count().saturating_mul(64))
                .saturating_add(4096),
        )?;
        let expected = fields
            .iter()
            .map(|field| pointer.text(pointer_doc, field))
            .collect::<Result<Vec<_>>>()?;
        let actual = actual
            .into_iter()
            .map(|id| model_text(&snapshot, id, true))
            .collect::<Result<Vec<_>>>()?;
        actual == expected
    };
    if !matches_identity {
        return Err(pointer_error(
            "activation snapshot identity does not match active Profile pointer",
        ));
    }
    if [
        "profile_id",
        "profile_revision",
        "plan_digest",
        "lock_digest",
    ]
    .into_iter()
    .any(|name| {
        !snapshot
            .get(activation, name)
            .is_some_and(|id| equal_atom(snapshot.node(id), pointer_doc.node(pointer.field(name))))
    }) || !snapshot
        .get(activation, "state")
        .is_some_and(|id| snapshot.string_is(id, "active"))
    {
        return Err(pointer_error(
            "ActivationRecord is stale or does not match active Profile pointer",
        ));
    }
    let catalog = pointer.field("catalog_revision");
    if !matches!(pointer_doc.node(catalog), Node::Scalar(CanonValue::Null))
        && !snapshot
            .get(plan, "catalog_revision")
            .is_some_and(|id| equal_atom(snapshot.node(id), pointer_doc.node(catalog)))
    {
        return Err(pointer_error(
            "activation snapshot catalog revision does not match active pointer",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::super::definitions::tests::{lock_free, operation, with_source};
    use super::*;
    use serde_json::{Value, json};
    use std::sync::Barrier;

    const RELATIVE: &str = "workspaces/defaults/activation/activations/defaults-selection.json";
    const CONFLICT: &str = "core_runtime.active_profile_store_v4.ActiveProfileStoreConflict";

    fn envelope(padding: usize) -> (Value, Value) {
        let profile = "defaults";
        let activation = "activation:defaults-selection";
        let [revision, plan, lock, catalog] =
            [b"rev".as_slice(), b"plan", b"lock", b"catalog"].map(Digest::hash);
        let snapshot = json!({
            "envelope": {
                "profile": {"profile_id": profile},
                "lock": {"lock_digest": lock.as_str()},
                "plan": {"profile_revision": revision.as_str(), "plan_digest": plan.as_str(),
                         "catalog_revision": catalog.as_str()},
                "activation": {"profile_id": profile, "profile_revision": revision.as_str(),
                               "activation_id": activation, "plan_digest": plan.as_str(),
                               "lock_digest": lock.as_str(), "state": "active"},
            },
            "metadata": "x".repeat(padding)
        });
        let request = json!({
            "profile_id": profile,
            "profile_revision": revision.as_str(),
            "activation_id": activation,
            "plan_digest": plan.as_str(),
            "lock_digest": lock.as_str(),
            "activation_snapshot_path": RELATIVE,
            "activation_snapshot_digest": digest(&snapshot),
            "catalog_revision": catalog.as_str(),
        });
        (request, snapshot)
    }

    fn canonical(value: &Value) -> Vec<u8> {
        let variant = tobkiri_canon::Variant::StrictV1;
        let val = tobkiri_canon::parse(&serde_json::to_vec(value).unwrap(), variant).unwrap();
        tobkiri_canon::encode(&val, variant).unwrap()
    }

    fn digest(value: &Value) -> String {
        Digest::hash(&canonical(value)).as_str().to_owned()
    }

    #[tokio::test(flavor = "current_thread")]
    async fn host_pointer_commit_serializes_competing_predecessors() {
        with_source(|source, read, cancel, budget| {
            let op = operation(read, source, cancel);
            let root = &source.root;
            let (request_data, snapshot) = envelope(750 * 1024);
            let path = root.join(RELATIVE);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, serde_json::to_vec_pretty(&snapshot).unwrap()).unwrap();
            let request = CoreValue::parse_guest(&canonical(&request_data), budget).unwrap();
            let lock = root.join("profiles/.active-profile.lock");
            let active = root.join("profiles").join(ACTIVE);
            commit_verified(
                &op,
                root,
                &request,
                None,
                || {
                    assert!(!lock_free(&lock));
                    Ok(-1)
                },
                budget,
            )
            .unwrap();
            let current = load_verified(&op, root, budget).unwrap().unwrap();
            let expected = Some((&*current.doc, &current.pointer));
            assert_eq!(current.pointer.integer(&current.doc, "generation"), 1);
            assert_eq!(current.pointer.integer(&current.doc, "updated_at"), 0);
            let err =
                commit_verified(&op, root, &request, None, || unreachable!(), budget).unwrap_err();
            assert_eq!(err.class, CONFLICT);
            assert_eq!(
                err.message,
                "active Profile replacement requires its current predecessor",
            );
            let start = Barrier::new(2);
            let [left, right] = std::thread::scope(|threads| {
                let attempt = |now| {
                    start.wait();
                    commit_verified(&op, root, &request, expected, || Ok(now), budget)
                };
                let left = threads.spawn(move || attempt(24));
                let right = threads.spawn(move || attempt(25));
                [left.join().unwrap(), right.join().unwrap()]
            });
            let (stale, now) = match (left, right) {
                (Ok(()), Err(stale)) => (stale, 24),
                (Err(stale), Ok(())) => (stale, 25),
                results => panic!("expected one commit and one stale predecessor: {results:?}"),
            };
            assert_eq!(stale.class, CONFLICT);
            assert_eq!(stale.message, "active Profile predecessor is stale");
            let mut committed = request_data;
            committed["schema"] = json!(SCHEMA);
            committed["generation"] = json!(2);
            committed["updated_at"] = json!(now);
            committed["pointer_digest"] = json!(digest(&committed));
            let mut line = canonical(&committed);
            line.push(b'\n');
            assert_eq!(fs::read(active).unwrap(), line);
            assert!(lock_free(&lock));
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn host_pointer_commit_rejects_changed_snapshot_and_preserves_predecessor() {
        with_source(|source, read, cancel, budget| {
            let op = operation(read, source, cancel);
            let root = &source.root;
            let (mut request_data, mut snapshot) = envelope(0);
            let snapshot_path = root.join(RELATIVE);
            fs::create_dir_all(snapshot_path.parent().unwrap()).unwrap();
            fs::write(&snapshot_path, canonical(&snapshot)).unwrap();
            let request = CoreValue::parse_guest(&canonical(&request_data), budget).unwrap();
            commit_verified(&op, root, &request, None, || Ok(30), budget).unwrap();
            let current = load_verified(&op, root, budget).unwrap().unwrap();
            let expected = Some((&*current.doc, &current.pointer));
            let profiles = root.join("profiles");
            let pointer_path = profiles.join(ACTIVE);
            let pointer_bytes = fs::read(&pointer_path).unwrap();
            snapshot["envelope"]["activation"]["state"] = json!("inactive");
            fs::write(&snapshot_path, canonical(&snapshot)).unwrap();
            for (request, message) in [
                (
                    request,
                    "activation snapshot differs from its on-disk envelope",
                ),
                (
                    {
                        request_data["activation_snapshot_digest"] = json!(digest(&snapshot));
                        CoreValue::parse_guest(&canonical(&request_data), budget).unwrap()
                    },
                    "ActivationRecord is stale or does not match active Profile pointer",
                ),
            ] {
                let error =
                    commit_verified(&op, root, &request, expected, || unreachable!(), budget)
                        .unwrap_err();
                assert_eq!(error.class, pointer_error("").class);
                assert_eq!(error.message, message);
                assert_eq!(fs::read(&pointer_path).unwrap(), pointer_bytes);
                assert!(lock_free(&profiles.join(".active-profile.lock")));
                assert_eq!(fs::read_dir(&profiles).unwrap().count(), 2);
            }
        });
    }
}
