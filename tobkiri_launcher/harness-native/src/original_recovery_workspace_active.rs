//! Read the original workspace activation pointer and its selected envelope.
use super::*;
use recovery_workspace::{APP_DENIED, pending};

pub(super) const POINTER_SCHEMA: &str = "io.tobkiri.defaultspack-active-pointer.v1";
const ENVELOPE_SCHEMA: &str = "io.tobkiri.defaultspack-activation-envelope.v1";
const POINTER_KEYS: [&str; 5] = [
    "schema",
    "activation_id",
    "envelope_path",
    "envelope_digest",
    "workspace_digest",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum ActiveSnapshotPrefix {
    ReadyForRecordValidation,
    LegacyMigration,
}

/// Read the locked snapshot up to record validation or legacy migration.
pub(super) fn read<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &Directory,
    workspace_digest: &Digest,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<ActiveSnapshotPrefix> {
    let pointer = pending::read_state(op, state, Path::new("active.json"), "active pointer")?;
    if !exact(&pointer, pointer.root, &POINTER_KEYS) {
        return Err(raw(APP_DENIED, "active pointer is invalid"));
    }
    let pointer_field = |key| {
        pointer
            .get(pointer.root, key)
            .expect("verified pointer keys")
    };
    if !pointer.string_is(pointer_field("schema"), POINTER_SCHEMA) {
        return Err(raw(APP_DENIED, "active pointer schema is unsupported"));
    }
    if !pointer.string_is(pointer_field("workspace_digest"), workspace_digest.as_str()) {
        return Err(raw(
            APP_DENIED,
            "active Profile belongs to another workspace",
        ));
    }

    let name = pointer_field("envelope_path");
    if !matches!(pointer.node(name), Node::String(_)) {
        return Err(raw(APP_DENIED, "active envelope path is invalid"));
    }
    let rendered = definitions::render_python_str(op, budget, &pointer, name)?;
    let name = rendered.as_str().expect("strict JSON Unicode string");
    // POSIX Path(name).name permits a bare parent component. SecureDirectory
    // still refuses that component when reading the resulting relative path.
    if name.is_empty() || name == "." || name.contains('/') {
        return Err(raw(APP_DENIED, "active envelope path is invalid"));
    }
    let path_bytes = "activations/".len().saturating_add(name.len());
    let stage = OriginalAuthorityStage::Path;
    let _path = op.reserve(stage, path_bytes)?;
    let mut path = PathBuf::new();
    path.try_reserve_exact(path_bytes)
        .map_err(|_| OriginalAuthorityError::capacity(stage))?;
    if path.capacity() > path_bytes {
        return Err(OriginalAuthorityError::capacity(stage));
    }
    path.push("activations");
    path.push(name);
    let envelope = pending::read_state(op, state, &path, "activation envelope")?;

    let changed = || raw(APP_DENIED, "activation envelope digest changed");
    if envelope.entries(envelope.root).is_none() {
        return Err(changed());
    }
    let canonical = json::canonical_subtree(&envelope, envelope.root, None, op, budget)?;
    let digest = Digest::hash(&canonical);
    drop(canonical);
    if !pointer.string_is(pointer_field("envelope_digest"), digest.as_str()) {
        return Err(changed());
    }
    if !field_is(&envelope, envelope.root, "schema", ENVELOPE_SCHEMA) {
        return Err(raw(APP_DENIED, "activation envelope schema is unsupported"));
    }
    if !field_is(
        &envelope,
        envelope.root,
        "workspace_digest",
        workspace_digest.as_str(),
    ) {
        return Err(raw(
            APP_DENIED,
            "activation envelope belongs to another workspace",
        ));
    }

    record(&envelope, "profile", "activation profile record is invalid")?;
    let lock = record(&envelope, "lock", "activation lock record is invalid")?;
    let plan = record(&envelope, "plan", "activation plan record is invalid")?;
    let activation = record(
        &envelope,
        "activation",
        "activation envelope records are invalid",
    )?;
    if [lock, plan, activation]
        .iter()
        .all(|id| envelope.entries(*id).is_some())
        && (field_is(
            &envelope,
            lock,
            "lock_api_version",
            "io.tobkiri.profile-lock.v4",
        ) || field_is(
            &envelope,
            plan,
            "plan_api_version",
            "io.tobkiri.resolved-plan.v1",
        ) || field_is(
            &envelope,
            activation,
            "activation_api_version",
            "io.tobkiri.activation-record.v1",
        ))
    {
        return Ok(ActiveSnapshotPrefix::LegacyMigration);
    }
    Ok(ActiveSnapshotPrefix::ReadyForRecordValidation)
}

fn field_is(doc: &Document, id: usize, key: &str, expected: &str) -> bool {
    doc.get(id, key)
        .is_some_and(|id| doc.string_is(id, expected))
}

fn record(doc: &Document, key: &str, invalid: &'static str) -> Result<usize> {
    doc.get(doc.root, key)
        .filter(|&id| matches!(doc.node(id), Node::Object(_) | Node::String(_)))
        .ok_or_else(|| raw(APP_DENIED, invalid))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{operation, with_source};
    use serde_json::{Value, json};

    const NAME: &str = "selected.json";

    struct Fixture {
        directory: PathBuf,
        workspace: Digest,
        pointer: Value,
        envelope: Value,
    }

    impl Fixture {
        fn new(directory: PathBuf) -> Self {
            fs::create_dir_all(directory.join("activations")).unwrap();
            let workspace = Digest::hash(b"workspace");
            let envelope = json!({
                "schema": ENVELOPE_SCHEMA,
                "workspace_digest": workspace.as_str(),
                "profile": {}, "lock": {}, "plan": {}, "activation": {},
                "additional_envelope_field": true,
            });
            let pointer = json!({
                "schema": POINTER_SCHEMA,
                "workspace_digest": workspace.as_str(),
                "activation_id": null,
                "envelope_path": NAME,
                "envelope_digest": null,
            });
            Self {
                directory,
                workspace,
                pointer,
                envelope,
            }
        }

        fn write(&mut self) {
            let compact = serde_json::to_vec(&self.envelope).unwrap();
            let pretty = serde_json::to_vec_pretty(&self.envelope).unwrap();
            self.pointer["envelope_digest"] = json!(Digest::hash(&compact).as_str());
            fs::write(self.directory.join("activations").join(NAME), pretty).unwrap();
            self.write_pointer();
        }

        fn write_pointer(&self) {
            fs::write(
                self.directory.join("active.json"),
                serde_json::to_vec(&self.pointer).unwrap(),
            )
            .unwrap();
        }

        fn read<S: OriginalAuthorityOwner>(
            &self,
            op: &Operation<'_, S>,
            budget: &Arc<dyn AllocationBudget>,
        ) -> Result<ActiveSnapshotPrefix> {
            let state = Directory::capture(op, &self.directory, false)?;
            read(op, &state, &self.workspace, budget)
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn checks_exact_pointer_shape_before_reading_the_selected_envelope() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let mut fixture = Fixture::new(source.root.join("activation"));
            let missing = fixture.read(&op, budget).unwrap_err();
            assert_eq!(missing.class, APP_DENIED);
            assert_eq!(
                missing.message,
                "active pointer is unavailable: active.json"
            );
            assert!(missing.cause.is_some());

            fixture.pointer["schema"] = json!(false);
            fixture.pointer["workspace_digest"] = json!(null);
            fixture.pointer["envelope_path"] = json!(false);
            fixture.pointer["extra"] = json!(true);
            for (message, key, next_value) in [
                ("active pointer is invalid", "extra", None),
                (
                    "active pointer schema is unsupported",
                    "schema",
                    Some(json!(POINTER_SCHEMA)),
                ),
                (
                    "active Profile belongs to another workspace",
                    "workspace_digest",
                    Some(json!(fixture.workspace.as_str())),
                ),
                (
                    "active envelope path is invalid",
                    "envelope_path",
                    Some(json!(NAME)),
                ),
            ] {
                fixture.write_pointer();
                let error = fixture.read(&op, budget).unwrap_err();
                assert_eq!(error.class, APP_DENIED);
                assert_eq!(error.message, message);
                assert!(error.cause.is_none());
                if let Some(next_value) = next_value {
                    fixture.pointer[key] = next_value;
                } else {
                    fixture.pointer.as_object_mut().unwrap().remove(key);
                }
            }
            fixture.write_pointer();
            let missing = fixture.read(&op, budget).unwrap_err();
            assert_eq!(missing.class, APP_DENIED);
            assert_eq!(
                missing.message,
                "activation envelope is unavailable: selected.json"
            );
            assert!(missing.cause.is_some());

            fixture.pointer["envelope_path"] = json!("invalid\0leaf.json");
            fixture.write_pointer();
            let invalid_leaf = fixture.read(&op, budget).unwrap_err();
            assert_eq!(invalid_leaf.class, "builtins.ValueError");
            assert_eq!(
                invalid_leaf.message,
                "stat: embedded null character in path"
            );
            fs::remove_dir(fixture.directory.join("activations")).unwrap();
            let missing_parent = fixture.read(&op, budget).unwrap_err();
            assert_eq!(missing_parent.class, APP_DENIED);
            assert_eq!(
                missing_parent.message,
                "activation envelope is unavailable: persistence child directory is unsafe"
            );
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn checks_canonical_envelope_digest_before_schema_workspace_and_records() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let mut fixture = Fixture::new(source.root.join("activation"));
            fixture.envelope["schema"] = json!(false);
            fixture.envelope["workspace_digest"] = json!(null);
            fixture.envelope["profile"] = json!(null);
            fixture.envelope["lock"] = json!([]);
            fixture.envelope["plan"] = json!(false);
            fixture.envelope["activation"] = json!(0);
            fixture.write();
            let raw = fs::read(fixture.directory.join("activations").join(NAME)).unwrap();
            fixture.pointer["envelope_digest"] = json!(Digest::hash(&raw).as_str());
            fixture.write_pointer();
            let error = fixture.read(&op, budget).unwrap_err();
            assert_eq!(error.class, APP_DENIED);
            assert_eq!(error.message, "activation envelope digest changed");

            for (message, key, next_value) in [
                (
                    "activation envelope schema is unsupported",
                    "schema",
                    json!(ENVELOPE_SCHEMA),
                ),
                (
                    "activation envelope belongs to another workspace",
                    "workspace_digest",
                    json!(fixture.workspace.as_str()),
                ),
                (
                    "activation profile record is invalid",
                    "profile",
                    json!("serialized profile"),
                ),
                ("activation lock record is invalid", "lock", json!({})),
                ("activation plan record is invalid", "plan", json!({})),
                (
                    "activation envelope records are invalid",
                    "activation",
                    json!({}),
                ),
            ] {
                fixture.write();
                let error = fixture.read(&op, budget).unwrap_err();
                assert_eq!(error.class, APP_DENIED);
                assert_eq!(error.message, message);
                assert!(error.cause.is_none());
                fixture.envelope[key] = next_value;
            }
            fixture.write();
            assert_eq!(
                fixture.read(&op, budget).unwrap(),
                ActiveSnapshotPrefix::ReadyForRecordValidation
            );
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn selects_legacy_migration_only_when_all_three_records_are_objects() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let mut fixture = Fixture::new(source.root.join("activation"));
            for (record, field, version) in [
                ("lock", "lock_api_version", "io.tobkiri.profile-lock.v4"),
                ("plan", "plan_api_version", "io.tobkiri.resolved-plan.v1"),
                (
                    "activation",
                    "activation_api_version",
                    "io.tobkiri.activation-record.v1",
                ),
            ] {
                fixture.envelope[record][field] = json!(version);
                fixture.write();
                assert_eq!(
                    fixture.read(&op, budget).unwrap(),
                    ActiveSnapshotPrefix::LegacyMigration
                );
                fixture.envelope[record] = json!({});
            }
            fixture.envelope["lock"]["lock_api_version"] = json!("io.tobkiri.profile-lock.v4");
            fixture.envelope["plan"] = json!("serialized plan");
            fixture.write();
            assert_eq!(
                fixture.read(&op, budget).unwrap(),
                ActiveSnapshotPrefix::ReadyForRecordValidation
            );
        });
    }
}
