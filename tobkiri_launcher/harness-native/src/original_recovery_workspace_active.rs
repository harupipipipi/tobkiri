//! Read the original workspace activation pointer and its selected envelope.
use super::*;
use crate::bootstrap_metadata::BoundProfileSchemas;
use recovery_workspace::{APP_DENIED, pending};
use tobkiri_canon::{CoreBuffer, CoreValue};

pub(super) const POINTER_SCHEMA: &str = "io.tobkiri.defaultspack-active-pointer.v1";
const ENVELOPE_SCHEMA: &str = "io.tobkiri.defaultspack-activation-envelope.v1";
const CANONICALIZATION_ERROR: &str = "tobkiri_protocol.errors.CanonicalizationError";
const PROFILE_SCHEMAS: [(&str, &str); 2] = [
    ("io.tobkiri.profile.v4", "profile_v4.schema.json"),
    ("io.tobkiri.profile.v5", "profile_v5.schema.json"),
];
const POINTER_KEYS: [&str; 5] = [
    "schema",
    "activation_id",
    "envelope_path",
    "envelope_digest",
    "workspace_digest",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct ActiveSnapshotPrefix {
    pub(super) profile_schema: &'static str,
    pub(super) legacy: bool,
}

/// Read the first record up to the original versioned App schema load.
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

    let profile = record(&envelope, "profile", "activation profile record is invalid")?;
    let lock = record(&envelope, "lock", "activation lock record is invalid")?;
    let plan = record(&envelope, "plan", "activation plan record is invalid")?;
    let activation = record(
        &envelope,
        "activation",
        "activation envelope records are invalid",
    )?;
    let legacy = [lock, plan, activation]
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
        ));
    // The replacement catalog is held by the caller. Legacy migration then
    // requires a raw mapping before the first ordinary validate_document call.
    if legacy && envelope.entries(profile).is_none() {
        return Err(raw(APP_DENIED, "legacy activation profile is invalid"));
    }
    let profile_schema = select_profile_schema(op, budget, &envelope, profile)?;
    Ok(ActiveSnapshotPrefix {
        profile_schema,
        legacy,
    })
}

fn field_is(doc: &Document, id: usize, key: &str, expected: &str) -> bool {
    doc.get(id, key)
        .is_some_and(|id| doc.string_is(id, expected))
}

/// Load the already selected component's finite resource under the workspace lock.
pub(super) fn load_profile_schema<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    schemas: BoundProfileSchemas,
    name: &str,
) -> Result<()> {
    let source = || {
        OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Source,
            "selected Setup component Profile schema resource",
        )
    };
    let bytes = schemas.get(name).ok_or_else(source)?;
    let data = json::decode_strict(bytes, op)?;
    if data.entries(data.root).is_none() {
        return Err(source());
    }
    Ok(())
}

fn record(doc: &Document, key: &str, invalid: &'static str) -> Result<usize> {
    doc.get(doc.root, key)
        .filter(|&id| matches!(doc.node(id), Node::Object(_) | Node::String(_)))
        .ok_or_else(|| raw(APP_DENIED, invalid))
}

fn select_profile_schema<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    envelope: &Document,
    profile: usize,
) -> Result<&'static str> {
    // The canonical envelope already proves an object child's canonical JSON
    // eligibility. Preserve its ordered arena. Serialized text requires an
    // independent strict parse before the version lookup.
    let decoded = if matches!(envelope.node(profile), Node::String(_)) {
        let text = definitions::render_python_str(op, budget, envelope, profile)?;
        Some(match json::decode_strict(text.as_bytes(), op) {
            Ok(document) => document,
            Err(error) if error.class == CANONICALIZATION_ERROR => {
                return Err(schema_failure(budget, Err(error))?);
            }
            Err(error) => return Err(error),
        })
    } else {
        None
    };
    let (doc, profile) = decoded
        .as_deref()
        .map_or((envelope, profile), |doc| (doc, doc.root));
    let message = if doc.entries(profile).is_some() {
        for (version, schema) in PROFILE_SCHEMAS {
            if field_is(doc, profile, "profile_api_version", version) {
                return Ok(schema);
            }
        }
        "unsupported profile API version"
    } else {
        "serialized protocol document must be an object"
    };
    Err(schema_failure(budget, Ok(message))?)
}

fn schema_failure(
    budget: &Arc<dyn AllocationBudget>,
    failure: std::result::Result<&'static str, OriginalAuthorityError>,
) -> Result<OriginalAuthorityError> {
    let capacity = || OriginalAuthorityError::capacity(OriginalAuthorityStage::Source);
    let mut argument = CoreBuffer::builder(budget, tobkiri_canon::MAX_BYTES);
    let (diagnostic, cause) = match failure {
        Ok(message) => {
            argument
                .append(message.as_bytes())
                .map_err(|_| capacity())?;
            (None, None)
        }
        Err(error) => {
            for bytes in [error.message.as_bytes(), b": ", error.message.as_bytes()] {
                argument.append(bytes).map_err(|_| capacity())?;
            }
            let diagnostic = CoreValue::string(&error.message, budget).map_err(|_| capacity())?;
            (Some(diagnostic), Some(Box::new(into_cause(error))))
        }
    };
    let diagnostics =
        CoreValue::array_shared(diagnostic.into_iter().map(Ok), budget).map_err(|_| capacity())?;
    let argument = argument.finish().map_err(|_| capacity())?;
    let mut error = raw(
        crate::bootstrap_review::DENIED,
        "Profile publication recovery failed closed",
    );
    error.cause = Some(OriginalAuthorityCause::SchemaValidation {
        argument,
        diagnostics,
        cause,
    });
    error.suppressed = true;
    Ok(error)
}

#[cfg(test)]
pub(super) mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{operation, with_source};
    use serde_json::{Value, json};

    const NAME: &str = "selected.json";

    static SCHEMAS: crate::BootstrapProfileSchemas = crate::BootstrapProfileSchemas {
        component_sha256: "sha256:df236ea3735c6e7102f4284c4cd6af041c31b6edea151558769c21200fe2d787",
        v4: include_bytes!(
            "../../../tobkiri_runtime/tobkiri_protocol/schemas/profile_v4.schema.json"
        ),
        v5: include_bytes!(
            "../../../tobkiri_runtime/tobkiri_protocol/schemas/profile_v5.schema.json"
        ),
    };

    pub(in crate::original_authority::io::publication::recovery_authority) fn bound_fixture_schemas()
    -> BoundProfileSchemas {
        SCHEMAS
            .bind(&Digest::parse(SCHEMAS.component_sha256).unwrap())
            .unwrap()
    }

    #[tokio::test(flavor = "current_thread")]
    async fn loads_only_the_original_profile_pair_bound_to_the_compiled_component() {
        assert!(SCHEMAS.bind(&Digest::hash(b"foreign component")).is_err());
        let schemas = bound_fixture_schemas();
        with_source(|source, read, cancellation, _| {
            let op = operation(read, source, cancellation);
            for (_, name) in PROFILE_SCHEMAS {
                load_profile_schema(&op, schemas, name).unwrap();
            }
            assert!(load_profile_schema(&op, schemas, "profile_lock_v5.schema.json").is_err());
        });
    }

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
                "profile": {"profile_api_version": "io.tobkiri.profile.v5"},
                "lock": {}, "plan": {}, "activation": {},
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
                    json!(r#"{"profile_api_version":"io.tobkiri.profile.v4"}"#),
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
                ActiveSnapshotPrefix {
                    profile_schema: "profile_v4.schema.json",
                    legacy: false
                }
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
                    ActiveSnapshotPrefix {
                        profile_schema: "profile_v5.schema.json",
                        legacy: true
                    }
                );
                fixture.envelope[record] = json!({});
            }
            fixture.envelope["lock"]["lock_api_version"] = json!("io.tobkiri.profile-lock.v4");
            fixture.envelope["plan"] = json!("serialized plan");
            fixture.write();
            assert_eq!(
                fixture.read(&op, budget).unwrap(),
                ActiveSnapshotPrefix {
                    profile_schema: "profile_v5.schema.json",
                    legacy: false
                }
            );
            fixture.envelope["plan"] = json!({});
            fixture.envelope["profile"] =
                json!(r#"{"profile_api_version":"io.tobkiri.profile.v4"}"#);
            fixture.write();
            let error = fixture.read(&op, budget).unwrap_err();
            assert_eq!(error.class, APP_DENIED);
            assert_eq!(error.message, "legacy activation profile is invalid");
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn first_profile_record_matches_the_original_decode_and_version_failures() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let mut fixture = Fixture::new(source.root.join("activation"));
            // These are admissible envelope fields. Their own content cannot
            // be checked before the first profile validates completely.
            for name in ["lock", "plan", "activation"] {
                fixture.envelope[name] = json!("invalid JSON");
            }
            let duplicate = "duplicate JSON object key: 'profile_api_version'";
            for (profile, message, has_cause) in [
                (
                    json!("[]"),
                    "serialized protocol document must be an object",
                    false,
                ),
                (
                    json!({"profile_api_version": false}),
                    "unsupported profile API version",
                    false,
                ),
                (
                    json!({"profile_api_version": "io.tobkiri.profile.v999"}),
                    "unsupported profile API version",
                    false,
                ),
                (
                    json!(r#"{"profile_api_version":0,"profile_api_version":1}"#),
                    duplicate,
                    true,
                ),
            ] {
                fixture.envelope["profile"] = profile;
                fixture.write();
                let error = fixture.read(&op, budget).unwrap_err();
                assert_eq!(error.class, crate::bootstrap_review::DENIED);
                assert_eq!(error.message, "Profile publication recovery failed closed");
                assert!(error.suppressed);
                let Some(OriginalAuthorityCause::SchemaValidation {
                    argument,
                    diagnostics,
                    cause,
                }) = error.cause
                else {
                    panic!("structured SchemaValidationError expected");
                };
                let diagnostics = diagnostics.array().unwrap();
                if has_cause {
                    assert_eq!(argument.as_str().unwrap(), format!("{message}: {message}"));
                    assert_eq!(diagnostics.len(), 1);
                    assert_eq!(diagnostics.get(0).unwrap().as_str(), Some(message));
                    assert!(
                        matches!(cause.as_deref(), Some(OriginalAuthorityCause::Python { class: CANONICALIZATION_ERROR, message: actual, .. }) if actual == message)
                    );
                } else {
                    assert_eq!(argument.as_str().unwrap(), message);
                    assert_eq!(diagnostics.len(), 0);
                    assert!(cause.is_none());
                }
            }
            fixture.envelope["profile"] = json!({"profile_api_version": "io.tobkiri.profile.v5"});
            fixture.write();
            assert_eq!(
                fixture.read(&op, budget).unwrap().profile_schema,
                "profile_v5.schema.json"
            );
        });
    }
}
