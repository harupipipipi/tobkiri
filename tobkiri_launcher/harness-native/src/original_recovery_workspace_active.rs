//! Read the original workspace activation pointer and its selected envelope.
use super::*;
use crate::bootstrap_metadata::BoundProfileSchemas;
use recovery_workspace::{APP_DENIED, pending};
use tobkiri_canon::{CoreBuffer, CoreValue};

#[path = "original_recovery_profile_schema.rs"]
mod profile_schema;

pub(super) const POINTER_SCHEMA: &str = "io.tobkiri.defaultspack-active-pointer.v1";
const ENVELOPE_SCHEMA: &str = "io.tobkiri.defaultspack-activation-envelope.v1";
const CANONICALIZATION_ERROR: &str = "tobkiri_protocol.errors.CanonicalizationError";
const PROFILE_SCHEMAS: [(&str, &str); 2] = [
    ("io.tobkiri.profile.v4", "profile_v4.schema.json"),
    ("io.tobkiri.profile.v5", "profile_v5.schema.json"),
];
const LOCK_SCHEMAS: [(&str, &str); 2] = [
    ("io.tobkiri.profile-lock.v4", "profile_lock_v4.schema.json"),
    ("io.tobkiri.profile-lock.v5", "profile_lock_v5.schema.json"),
];
const PLAN_SCHEMAS: [(&str, &str); 2] = [
    (
        "io.tobkiri.resolved-plan.v1",
        "resolved_plan_v1.schema.json",
    ),
    (
        "io.tobkiri.resolved-plan.v2",
        "resolved_plan_v2.schema.json",
    ),
];
const ACTIVATION_SCHEMAS: [(&str, &str); 2] = [
    (
        "io.tobkiri.activation-record.v1",
        "activation_record_v1.schema.json",
    ),
    (
        "io.tobkiri.activation-record.v2",
        "activation_record_v2.schema.json",
    ),
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
    pub(super) check: ProfileCheck,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum ProfileCheck {
    SourceUnavailable,
    ReferencesUnavailable,
    Validated,
    RecordsValidated,
}

/// Read the selected records in the original order with the bound schema closure.
pub(super) fn read<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &Directory,
    workspace_digest: &Digest,
    budget: &Arc<dyn AllocationBudget>,
    schemas: Option<BoundProfileSchemas>,
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
    let envelope = pending::read_state_with(op, state, &path, "activation envelope", |bytes| {
        json::decode_read_only_strict(bytes, op, budget)
    })?;

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
    let profile = select_record(op, budget, &envelope, profile, RecordKind::Profile)?;
    let check = check_snapshot_records(
        op,
        budget,
        schemas,
        &profile,
        &envelope,
        [lock, plan, activation],
        legacy,
    )?;
    Ok(ActiveSnapshotPrefix {
        profile_schema: profile.name,
        legacy,
        check,
    })
}

fn field_is(doc: &Document, id: usize, key: &str, expected: &str) -> bool {
    doc.get(id, key)
        .is_some_and(|id| doc.string_is(id, expected))
}

/// Load the already selected component's finite resource under the workspace lock.
fn load_record_schema<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    schemas: BoundProfileSchemas,
    name: &str,
) -> Result<Funded<Document>> {
    let source = || {
        OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Source,
            "selected Setup component record schema resource",
        )
    };
    let bytes = schemas.get(name).ok_or_else(source)?;
    let data = json::decode_read_only_strict(bytes, op, budget)?;
    if data.entries(data.root).is_none() {
        return Err(source());
    }
    Ok(data)
}

fn check_snapshot_records<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    schemas: Option<BoundProfileSchemas>,
    profile: &Record<'_>,
    envelope: &Document,
    pending: [usize; 3],
    legacy: bool,
) -> Result<ProfileCheck> {
    let Some(schemas) = schemas else {
        return Ok(ProfileCheck::SourceUnavailable);
    };
    let root = load_record_schema(op, budget, schemas, profile.name)?;
    let Some([common, projection]) = schemas.references() else {
        return Ok(ProfileCheck::ReferencesUnavailable);
    };
    let common = json::decode_read_only_strict(common, op, budget)?;
    let projection = json::decode_read_only_strict(projection, op, budget)?;
    validate_record(op, budget, profile, &[&root, &common, &projection])?;
    drop(root);
    if schemas.get(LOCK_SCHEMAS[0].1).is_none() {
        return Ok(ProfileCheck::Validated);
    }
    let mut all_legacy_versions = true;
    for (id, kind) in
        pending
            .into_iter()
            .zip([RecordKind::Lock, RecordKind::Plan, RecordKind::Activation])
    {
        let record = select_record(op, budget, envelope, id, kind)?;
        let root = load_record_schema(op, budget, schemas, record.name)?;
        if matches!(kind, RecordKind::Plan) {
            let principal =
                load_record_schema(op, budget, schemas, "function_principal_v1.schema.json")?;
            validate_record(
                op,
                budget,
                &record,
                &[&root, &common, &projection, &principal],
            )?;
        } else {
            validate_record(op, budget, &record, &[&root, &common, &projection])?;
        }
        all_legacy_versions &= record.legacy_version;
    }
    if legacy && !all_legacy_versions {
        return Err(raw(
            APP_DENIED,
            "legacy activation versions do not form one record set",
        ));
    }
    Ok(ProfileCheck::RecordsValidated)
}

fn validate_record<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    record: &Record<'_>,
    schemas: &[&Document],
) -> Result<()> {
    let (doc, id) = record.parts();
    let mismatch =
        |message| OriginalAuthorityError::unsupported(OriginalAuthorityStage::Source, message);
    let profile = matches!(record.kind, RecordKind::Profile);
    if !profile_schema::matches(op, budget, schemas, doc, id)? {
        return Err(mismatch(if profile {
            "original Profile JSON Schema mismatch diagnostics"
        } else {
            "original activation record JSON Schema mismatch diagnostics"
        }));
    }
    let valid = if profile {
        external_catalog::validate_profile_semantics(doc, id, op, budget)?
    } else {
        external_catalog::validate_recovery_record_semantics(doc, id, op, budget)?
    };
    if !valid {
        return Err(mismatch(if profile {
            "original Profile semantic mismatch diagnostics"
        } else {
            "original activation record semantic mismatch diagnostics"
        }));
    }
    Ok(())
}

fn record(doc: &Document, key: &str, invalid: &'static str) -> Result<usize> {
    doc.get(doc.root, key)
        .filter(|&id| matches!(doc.node(id), Node::Object(_) | Node::String(_)))
        .ok_or_else(|| raw(APP_DENIED, invalid))
}

#[derive(Clone, Copy)]
enum RecordKind {
    Profile,
    Lock,
    Plan,
    Activation,
}

impl RecordKind {
    fn versioning(self) -> (&'static str, VersionedSchemas, &'static str) {
        match self {
            Self::Profile => (
                "profile_api_version",
                PROFILE_SCHEMAS,
                "unsupported profile API version",
            ),
            Self::Lock => (
                "lock_api_version",
                LOCK_SCHEMAS,
                "unsupported profile_lock API version",
            ),
            Self::Plan => (
                "plan_api_version",
                PLAN_SCHEMAS,
                "unsupported resolved_plan API version",
            ),
            Self::Activation => (
                "activation_api_version",
                ACTIVATION_SCHEMAS,
                "unsupported activation API version",
            ),
        }
    }
}

type VersionedSchemas = [(&'static str, &'static str); 2];

struct Record<'a> {
    decoded: Option<Funded<Document>>,
    envelope: &'a Document,
    id: usize,
    kind: RecordKind,
    name: &'static str,
    legacy_version: bool,
}

impl Record<'_> {
    fn parts(&self) -> (&Document, usize) {
        self.decoded
            .as_deref()
            .map_or((self.envelope, self.id), |doc| (doc, doc.root))
    }
}

fn select_record<'a, S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    envelope: &'a Document,
    id: usize,
    kind: RecordKind,
) -> Result<Record<'a>> {
    // The canonical envelope already proves an object child's canonical JSON
    // eligibility. Preserve its ordered arena. Serialized text requires an
    // independent strict parse before the version lookup.
    let decoded = if matches!(envelope.node(id), Node::String(_)) {
        let text = definitions::render_python_str(op, budget, envelope, id)?;
        Some(
            match json::decode_read_only_strict(text.as_bytes(), op, budget) {
                Ok(document) => document,
                Err(error) if error.class == CANONICALIZATION_ERROR => {
                    return Err(schema_failure(budget, Err(error))?);
                }
                Err(error) => return Err(error),
            },
        )
    } else {
        None
    };
    let (doc, value) = decoded
        .as_deref()
        .map_or((envelope, id), |doc| (doc, doc.root));
    let (version_field, choices, unsupported_version) = kind.versioning();
    let message = if doc.entries(value).is_some() {
        for (index, (version, name)) in choices.into_iter().enumerate() {
            if field_is(doc, value, version_field, version) {
                return Ok(Record {
                    decoded,
                    envelope,
                    id,
                    kind,
                    name,
                    legacy_version: index == 0,
                });
            }
        }
        unsupported_version
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

    const PROFILE_ROOTS: crate::BootstrapProfileSchemas = crate::BootstrapProfileSchemas {
        component_sha256: "sha256:df236ea3735c6e7102f4284c4cd6af041c31b6edea151558769c21200fe2d787",
        v4: include_bytes!(
            "../../../tobkiri_runtime/tobkiri_protocol/schemas/profile_v4.schema.json"
        ),
        v5: include_bytes!(
            "../../../tobkiri_runtime/tobkiri_protocol/schemas/profile_v5.schema.json"
        ),
        references: None,
        recovery: None,
    };
    static SCHEMAS: crate::BootstrapProfileSchemas = PROFILE_ROOTS;
    static COMPLETE_SCHEMAS: crate::BootstrapProfileSchemas = crate::BootstrapProfileSchemas {
        references: Some([
            include_bytes!(
                "../../../tobkiri_runtime/tobkiri_protocol/schemas/common_v1.schema.json"
            ),
            include_bytes!(
                "../../../tobkiri_runtime/tobkiri_protocol/schemas/profile_content_projection_v1.schema.json"
            ),
        ]),
        ..PROFILE_ROOTS
    };
    static RECOVERY_SCHEMAS: crate::BootstrapProfileSchemas = crate::BootstrapProfileSchemas {
        recovery: Some([
            include_bytes!(
                "../../../tobkiri_runtime/tobkiri_protocol/schemas/profile_lock_v4.schema.json"
            ),
            include_bytes!(
                "../../../tobkiri_runtime/tobkiri_protocol/schemas/profile_lock_v5.schema.json"
            ),
            include_bytes!(
                "../../../tobkiri_runtime/tobkiri_protocol/schemas/resolved_plan_v1.schema.json"
            ),
            include_bytes!(
                "../../../tobkiri_runtime/tobkiri_protocol/schemas/resolved_plan_v2.schema.json"
            ),
            include_bytes!(
                "../../../tobkiri_runtime/tobkiri_protocol/schemas/activation_record_v1.schema.json"
            ),
            include_bytes!(
                "../../../tobkiri_runtime/tobkiri_protocol/schemas/activation_record_v2.schema.json"
            ),
            include_bytes!(
                "../../../tobkiri_runtime/tobkiri_protocol/schemas/function_principal_v1.schema.json"
            ),
        ]),
        ..COMPLETE_SCHEMAS
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
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            for (_, name) in PROFILE_SCHEMAS {
                load_record_schema(&op, budget, schemas, name).unwrap();
            }
            assert!(
                load_record_schema(&op, budget, schemas, "profile_lock_v5.schema.json").is_err()
            );
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
            self.read_with_schemas(op, budget, None)
        }

        fn read_with_schemas<S: OriginalAuthorityOwner>(
            &self,
            op: &Operation<'_, S>,
            budget: &Arc<dyn AllocationBudget>,
            schemas: Option<BoundProfileSchemas>,
        ) -> Result<ActiveSnapshotPrefix> {
            let state = Directory::capture(op, &self.directory, false)?;
            read(op, &state, &self.workspace, budget, schemas)
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
                    legacy: false,
                    check: ProfileCheck::SourceUnavailable,
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
                        legacy: true,
                        check: ProfileCheck::SourceUnavailable,
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
                    legacy: false,
                    check: ProfileCheck::SourceUnavailable,
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

    #[tokio::test(flavor = "current_thread")]
    async fn shipped_profile_shape_and_semantics_are_checked_before_later_records() {
        let schemas = COMPLETE_SCHEMAS
            .bind(&Digest::parse(COMPLETE_SCHEMAS.component_sha256).unwrap())
            .unwrap();
        let v4: Value = serde_json::from_slice(include_bytes!(
            "../../../tobkiri_runtime/tests/fixtures/profile_v4/pre-e853-activation.json"
        ))
        .unwrap();
        let v5: Value = serde_json::from_slice(include_bytes!(
            "../../../tobkiri_runtime/ecosystem/defaultspack/v4/defaults.profile.v5.json"
        ))
        .unwrap();
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let mut fixture = Fixture::new(source.root.join("activation"));
            fixture.envelope["plan"] = json!("invalid later record");
            for (name, profile) in [
                ("profile_v4.schema.json", v4["profile"].clone()),
                (
                    "profile_v5.schema.json",
                    json!(serde_json::to_string(&v5).unwrap()),
                ),
            ] {
                fixture.envelope["profile"] = profile;
                fixture.write();
                assert_eq!(
                    fixture
                        .read_with_schemas(&op, budget, Some(schemas))
                        .unwrap(),
                    ActiveSnapshotPrefix {
                        profile_schema: name,
                        legacy: false,
                        check: ProfileCheck::Validated,
                    }
                );
            }
            for field in ["profile", "lock", "plan", "activation"] {
                fixture.envelope[field] = v4[field].clone();
            }
            fixture.write();
            assert_eq!(
                fixture
                    .read_with_schemas(&op, budget, Some(schemas))
                    .unwrap(),
                ActiveSnapshotPrefix {
                    profile_schema: "profile_v4.schema.json",
                    legacy: true,
                    check: ProfileCheck::Validated,
                }
            );
            fixture.envelope["lock"] = json!({});
            for (path, value, feature) in [
                (
                    "base",
                    json!(null),
                    "original Profile JSON Schema mismatch diagnostics",
                ),
                (
                    "provenance",
                    json!({}),
                    "original Profile JSON Schema mismatch diagnostics",
                ),
                (
                    "authority_references",
                    json!(["authority-ref:fixture-1"]),
                    "original Profile semantic mismatch diagnostics",
                ),
            ] {
                let mut invalid = v5.clone();
                invalid[path] = value;
                fixture.envelope["profile"] = invalid;
                fixture.write();
                let error = fixture
                    .read_with_schemas(&op, budget, Some(schemas))
                    .unwrap_err();
                assert!(
                    matches!(error.cause, Some(OriginalAuthorityCause::Unsupported(name)) if name == feature),
                    "{error:?}"
                );
            }
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn validates_bound_recovery_records_in_original_order() {
        let schemas = RECOVERY_SCHEMAS
            .bind(&Digest::parse(RECOVERY_SCHEMAS.component_sha256).unwrap())
            .unwrap();
        let v4: Value = serde_json::from_slice(include_bytes!(
            "../../../tobkiri_runtime/tests/fixtures/profile_v4/pre-e853-activation.json"
        ))
        .unwrap();
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let mut fixture = Fixture::new(source.root.join("activation"));
            for field in ["profile", "lock", "plan", "activation"] {
                fixture.envelope[field] = v4[field].clone();
            }
            for legacy in [true, false] {
                if !legacy {
                    for field in ["lock", "plan", "activation"] {
                        fixture.envelope[field] = json!(serde_json::to_string(&v4[field]).unwrap());
                    }
                }
                fixture.write();
                assert_eq!(
                    fixture
                        .read_with_schemas(&op, budget, Some(schemas))
                        .unwrap(),
                    ActiveSnapshotPrefix {
                        profile_schema: "profile_v4.schema.json",
                        legacy,
                        check: ProfileCheck::RecordsValidated,
                    }
                );
            }
            fixture.envelope["lock"] = v4["lock"].clone();
            fixture.envelope["plan"] = v4["plan"].clone();
            fixture.envelope["activation"] = json!("broken later record");
            fixture.envelope["lock"]["lock_api_version"] = json!("wrong-version");
            fixture.envelope["plan"]["plan_api_version"] = json!("wrong-version");
            for (field, expected) in [
                ("lock", "unsupported profile_lock API version"),
                ("plan", "unsupported resolved_plan API version"),
            ] {
                fixture.write();
                let error = fixture
                    .read_with_schemas(&op, budget, Some(schemas))
                    .unwrap_err();
                assert!(
                    matches!(
                        error.cause,
                        Some(OriginalAuthorityCause::SchemaValidation { ref argument, .. })
                        if argument.as_str().ok() == Some(expected)
                    ),
                    "{field}: {error:?}"
                );
                fixture.envelope[field] = v4[field].clone();
            }

            fixture.envelope["plan"]["bindings"][0]["function_principal"]["function_id"] =
                json!("BAD ID");
            fixture.write();
            let error = fixture
                .read_with_schemas(&op, budget, Some(schemas))
                .unwrap_err();
            assert!(
                matches!(
                    error.cause,
                    Some(OriginalAuthorityCause::Unsupported(
                        "original activation record JSON Schema mismatch diagnostics"
                    ))
                ),
                "{error:?}"
            );
        });
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "requires current records from the original packaged Profile resolver"]
    async fn validates_original_packaged_profile_records_within_store_budget() {
        let path = std::env::var_os("TOBKIRI_ORIGINAL_CURRENT_ACTIVATION_FIXTURE")
            .expect("path to the original packaged Profile, Lock, Plan and Activation records");
        let records: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        for (field, id) in [
            ("profile_api_version", "io.tobkiri.profile.v5"),
            ("lock_api_version", "io.tobkiri.profile-lock.v5"),
            ("plan_api_version", "io.tobkiri.resolved-plan.v2"),
            ("activation_api_version", "io.tobkiri.activation-record.v2"),
        ] {
            let record = field.strip_suffix("_api_version").unwrap();
            assert_eq!(records[record][field], id);
        }
        let schemas = RECOVERY_SCHEMAS
            .bind(&Digest::parse(RECOVERY_SCHEMAS.component_sha256).unwrap())
            .unwrap();
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let mut fixture = Fixture::new(source.root.join("activation"));
            for field in ["profile", "lock", "plan", "activation"] {
                fixture.envelope[field] = records[field].clone();
            }
            fixture.write();
            assert_eq!(
                fixture
                    .read_with_schemas(&op, budget, Some(schemas))
                    .unwrap(),
                ActiveSnapshotPrefix {
                    profile_schema: "profile_v5.schema.json",
                    legacy: false,
                    check: ProfileCheck::RecordsValidated,
                }
            );
        });
    }
}
