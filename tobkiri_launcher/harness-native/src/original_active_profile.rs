//! The fixed old ActiveProfileStore read reached inside publication recovery.
//! Snapshot integrity is candidate DATA, never an Authority or approval check.
use super::*;
use crate::original_authority::lease::model_text;
use model::{Pointer, equal_atom, pointer_error, validate_pointer};

pub(super) struct LoadedPointer {
    pub(super) doc: Funded<Document>,
    pub(super) pointer: Pointer,
}

fn missing(error: &OriginalAuthorityError) -> bool {
    error.class == "builtins.FileNotFoundError"
        || (error.class == RAW_IO
            && matches!(&error.cause, Some(OriginalAuthorityCause::Io(error)) if error.kind == std::io::ErrorKind::NotFound))
}

fn read_error(error: OriginalAuthorityError, message: &'static str) -> OriginalAuthorityError {
    if error.class == RAW_IO
        || matches!(
            error.class,
            "builtins.FileNotFoundError"
                | "builtins.ValueError"
                | "tobkiri_protocol.errors.CanonicalizationError"
                | "tobkiri_protocol.secure_persistence.SecurePersistenceError"
        )
    {
        caused(pointer_error(message), error)
    } else {
        error
    }
}

pub(super) fn load<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    requested: &Path,
) -> Result<Option<LoadedPointer>> {
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
    // Both actual constructors precede load, including the missing-pointer
    // branch. load takes no active-profile lock and never rereads active.json.
    let directory = Directory::capture(op, &profiles, true)?;
    let user_directory = Directory::capture(op, &user_data, true)?;
    let read = (|| {
        if !directory.exists(op, "active.json")? {
            return Ok(None);
        }
        let bytes = directory.read(op, "active.json")?;
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
    verify_snapshot(op, &user_directory, &doc, &pointer)?;
    Ok(Some(LoadedPointer { doc, pointer }))
}

fn verify_snapshot<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    user_directory: &Directory,
    pointer_doc: &Document,
    pointer: &Pointer,
) -> Result<()> {
    // from_mapping already validates the exact relative binding. The repeated
    // _relative_to_root checks operate on the same immutable pointer fields.
    let relative = pointer.text(pointer_doc, "activation_snapshot_path")?;
    let read = (|| {
        let bytes = user_directory.read_relative(op, Path::new(&relative))?;
        json::decode_strict(&bytes, op)
    })();
    let snapshot = read
        .map_err(|error| read_error(error, "active Profile activation snapshot is unavailable"))?;
    if !matches!(snapshot.node(snapshot.root), Node::Object(_)) {
        return Err(pointer_error("activation snapshot is not an object"));
    }
    // Hash the WHOLE decoded snapshot before examining its envelope or any
    // identity. Wrapper metadata remains part of the pinned digest.
    let canonical = json::canonical(&snapshot, None, op)?;
    if !pointer_doc.string_is(
        pointer.field("activation_snapshot_digest"),
        Digest::hash(&canonical).as_str(),
    ) {
        return Err(pointer_error(
            "activation snapshot digest does not match active pointer",
        ));
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
    // Python str(value or '') may coerce integers and containers here. It is
    // not a string-schema check. Construct all five values eagerly, after the
    // expected tuple, before comparing. The arena and builtin repr scratch
    // have one bounded, same-invocation reservation; no callback is involved.
    let _tuple_capacity = op.reserve(
        OriginalAuthorityStage::Source,
        snapshot
            .source_bytes()
            .saturating_mul(12)
            .saturating_add(snapshot.node_count().saturating_mul(64))
            .saturating_add(4096),
    )?;
    let fields = [
        "profile_id",
        "profile_revision",
        "activation_id",
        "plan_digest",
        "lock_digest",
    ];
    let expected = [
        pointer.text(pointer_doc, fields[0])?,
        pointer.text(pointer_doc, fields[1])?,
        pointer.text(pointer_doc, fields[2])?,
        pointer.text(pointer_doc, fields[3])?,
        pointer.text(pointer_doc, fields[4])?,
    ];
    let actual = [
        model_text(&snapshot, snapshot.get(profile, "profile_id"), true)?,
        model_text(&snapshot, snapshot.get(plan, "profile_revision"), true)?,
        model_text(&snapshot, snapshot.get(activation, "activation_id"), true)?,
        model_text(&snapshot, snapshot.get(plan, "plan_digest"), true)?,
        model_text(&snapshot, snapshot.get(lock, "lock_digest"), true)?,
    ];
    if actual != expected {
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
