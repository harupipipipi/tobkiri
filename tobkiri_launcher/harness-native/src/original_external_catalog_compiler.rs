//! Old CAS Pack v4 definition checks and the opening steps of the artifact compiler.
use super::charge;
use super::external_catalog_model::{
    self as model, SemanticPath, digest_syntax, id_syntax, validate_semantics,
};
use crate::original_authority::{
    Funded, Operation, OriginalAuthorityCause, OriginalAuthorityError, OriginalAuthorityOwner,
    OriginalAuthorityStage, Result,
    json::{self, Document, Node, Text},
};
use std::{ffi::CStr, sync::Arc};
use tobkiri_canon::{AllocationBudget, CanonValue};
use zeroize::Zeroizing;

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const SCHEMA_ERROR: &str = "tobkiri_protocol.errors.SchemaValidationError";
const INVALID_ARTIFACT: &str = "tobkiri_host.errors.InvalidArtifactError";

/// Executable catalog retained under the read's capacity reservation.
pub(super) struct ExecutableCatalog {
    doc: Funded<Document>,
    variants: usize,
}

/// Pack kind at the original pre-compiler boundary.
pub(super) struct PackManifest {
    _doc: Funded<Document>,
    host_extension: bool,
}

/// Compiler definitions held under the operation's capacity reservation.
pub(super) struct DirectDefinitions {
    pub(super) manifest: Funded<Document>,
    pub(super) contracts: Funded<Document>,
    pub(super) index: Funded<Document>,
    pub(super) executable: Funded<Document>,
}

/// Read and validate the direct compiler's four inputs, then check their identities.
pub(super) fn read_direct_definitions<S: OriginalAuthorityOwner>(
    mut read: impl FnMut(&'static CStr) -> Result<Funded<Zeroizing<Vec<u8>>>>,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<DirectDefinitions> {
    let mut validated = |file: &'static CStr, name: &'static str, fields: &'static [Field]| {
        op.check(STAGE)?;
        let bytes = read(file)?;
        read_document(bytes.as_slice(), Rule::Closed(fields), name, op, budget)
    };
    let manifest = validated(c"pack.v4.json", "pack", MANIFEST)?;
    let contracts = validated(
        c"contracts.v4.json",
        "pack_contract_catalog",
        CONTRACT_CATALOG,
    )?;
    let index = validated(
        c"artifact-index.v4.json",
        "pack_artifact_index",
        ARTIFACT_INDEX,
    )?;
    let executable = validated(c"executables.v4.json", "executable_catalog", EXECUTABLE)?;

    let sidecars = [&*contracts, &*index, &*executable];
    for (section, manifest_field, sidecar_field, message) in [
        (
            "pack",
            "id",
            "pack_id",
            "v4 artifact documents disagree on Pack identity",
        ),
        (
            "integrity",
            "source_identity",
            "source_identity",
            "v4 artifact documents disagree on source identity",
        ),
    ] {
        let section = manifest
            .get(manifest.root, section)
            .expect("checked manifest section");
        let expected = required_text(&manifest, section, manifest_field);
        for sidecar in sidecars {
            let actual = required_text(sidecar, sidecar.root, sidecar_field);
            charge(op, budget, expected.0.len().min(actual.0.len()) + 1)?;
            if actual != expected {
                return Err(invalid_artifact(message));
            }
        }
    }
    Ok(DirectDefinitions {
        manifest,
        contracts,
        index,
        executable,
    })
}

fn required_text<'a>(doc: &'a Document, parent: usize, key: &str) -> &'a Text {
    let id = doc.get(parent, key).expect("checked compiler field");
    let Node::String(text) = doc.node(id) else {
        unreachable!("checked compiler string");
    };
    text
}

/// Decode and check the first document reached by the original CAS verifier.
pub(super) fn read_executable<S: OriginalAuthorityOwner>(
    bytes: &[u8],
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<ExecutableCatalog> {
    let doc = read_document(
        bytes,
        Rule::Closed(EXECUTABLE),
        "executable_catalog",
        op,
        budget,
    )?;
    let variants = doc
        .get(doc.root, "variants")
        .expect("checked executable catalog");
    Ok(ExecutableCatalog { doc, variants })
}

/// Decode and check the manifest after the executable catalog passes.
pub(super) fn read_manifest<S: OriginalAuthorityOwner>(
    bytes: &[u8],
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<PackManifest> {
    let doc = read_document(bytes, Rule::Closed(MANIFEST), "pack", op, budget)?;
    let pack = doc.get(doc.root, "pack").expect("checked Pack manifest");
    let kind = doc.get(pack, "kind").expect("checked Pack kind");
    let host_extension = doc.string_is(kind, "host_extension");
    Ok(PackManifest {
        _doc: doc,
        host_extension,
    })
}

/// Check the receipt's Host Extension authority before alias and backend checks.
pub(super) fn require_external_catalog_identity<S: OriginalAuthorityOwner>(
    executable: &ExecutableCatalog,
    manifest: &PackManifest,
    authority_is_host_extension: bool,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    op.check(STAGE)?;
    let deny = |message| artifact_failure(python_cause(model::denied(message)));
    let host_extension = manifest.host_extension;
    if host_extension && !authority_is_host_extension {
        return Err(deny("Host Extension admission identity is missing"));
    }
    let doc = &executable.doc;
    if doc
        .get(doc.root, "materialization_catalog_digest")
        .is_some()
    {
        return Err(deny(
            "external Pack cannot replace its executable catalog identity",
        ));
    }
    let Node::Array(variants) = doc.node(executable.variants) else {
        unreachable!("checked executable variants");
    };
    for &variant in variants {
        charge(op, budget, 96)?;
        let execution_kind = doc
            .get(variant, "execution_kind")
            .expect("checked execution kind");
        let allowed = if host_extension {
            doc.string_is(execution_kind, "host_extension")
        } else {
            doc.string_is(execution_kind, "wasm") || doc.string_is(execution_kind, "pack_vm")
        };
        if !allowed {
            return Err(deny(
                "external admission accepts only Normal Sandbox backends",
            ));
        }
    }
    Ok(host_extension)
}

fn read_document<S: OriginalAuthorityOwner>(
    bytes: &[u8],
    rule: Rule,
    name: &'static str,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Funded<Document>> {
    let doc = json::decode_catalog_lock(bytes, op, budget).map_err(json_failure)?;
    if doc.entries(doc.root).is_none() {
        return Err(artifact_failure(python_cause(json::raw_error(
            SCHEMA_ERROR,
            "serialized protocol document must be an object",
        ))));
    }
    let validation = Validation {
        doc: &doc,
        op,
        budget,
    };
    if !validation.check(doc.root, rule)?
        || !validation.check_semantic_fields(doc.root, name == "pack")?
    {
        return Err(schema_diagnostics_unavailable(name));
    }
    validate_semantics(&doc, doc.root, SemanticPath::Root, op, budget).map_err(|error| {
        if error.class == "core_runtime.external_pack_catalog_v4.ExternalPackCatalogDenied" {
            schema_diagnostics_unavailable(name)
        } else {
            error
        }
    })?;
    Ok(doc)
}

fn schema_diagnostics_unavailable(name: &'static str) -> OriginalAuthorityError {
    // The original includes all sorted JSON Schema and semantic error messages.
    // Keep an explicit integration boundary until that projection is available.
    let feature = match name {
        "pack" => "original CAS Pack manifest schema diagnostics",
        "pack_contract_catalog" => "original CAS Pack Contract catalog schema diagnostics",
        "pack_artifact_index" => "original CAS Pack artifact index schema diagnostics",
        _ => "original CAS executable catalog schema diagnostics",
    };
    OriginalAuthorityError::unsupported(STAGE, feature)
}

fn artifact_failure(cause: OriginalAuthorityCause) -> OriginalAuthorityError {
    let mut error = model::denied("external Pack artifact digest verification failed");
    error.cause = Some(cause);
    error.suppressed = true;
    error
}

/// Wrap a compiler inconsistency in the external CAS verifier's denial.
pub(super) fn invalid_artifact(message: &'static str) -> OriginalAuthorityError {
    artifact_failure(python_cause(json::raw_error(INVALID_ARTIFACT, message)))
}

fn json_failure(error: OriginalAuthorityError) -> OriginalAuthorityError {
    if error.class != "tobkiri_protocol.errors.CanonicalizationError" {
        return error;
    }
    let mut schema = json::raw_error(SCHEMA_ERROR, format!("{0}: {0}", error.message));
    schema.cause = Some(python_cause(error));
    schema.suppressed = true;
    artifact_failure(python_cause(schema))
}

fn python_cause(error: OriginalAuthorityError) -> OriginalAuthorityCause {
    OriginalAuthorityCause::Python {
        class: error.class,
        message: error.message,
        cause: error.cause.map(Box::new),
        context: error.context.map(|error| Box::new(python_cause(*error))),
        suppressed: error.suppressed,
    }
}

#[derive(Clone, Copy)]
enum Rule {
    Any,
    String,
    Nonempty,
    Bool,
    False,
    True,
    Integer(i64),
    OneOf(&'static [&'static str]),
    Id,
    CommonId,
    ContractId,
    CommonContractId,
    Digest,
    Semver,
    Date,
    ImplementationPath,
    Commit,
    RepositoryTree,
    Object,
    Map(&'static Rule),
    SchemaCatalog,
    Closed(&'static [Field]),
    Array {
        items: &'static Rule,
        min: usize,
        unique: bool,
    },
    Provenance,
}

use Rule::*;

const fn array(items: &'static Rule, min: usize, unique: bool) -> Rule {
    Array { items, min, unique }
}

struct Field {
    name: &'static str,
    rule: Rule,
    required: bool,
}

const fn required(name: &'static str, rule: Rule) -> Field {
    Field {
        name,
        rule,
        required: true,
    }
}
const fn optional(name: &'static str, rule: Rule) -> Field {
    Field {
        name,
        rule,
        required: false,
    }
}

const EXECUTABLE: &[Field] = &[
    required(
        "catalog_api_version",
        OneOf(&["io.tobkiri.executable-catalog.v4"]),
    ),
    required("pack_id", CommonId),
    required("source_identity", Digest),
    optional("materialization_catalog_digest", Digest),
    required("variants", array(&Closed(VARIANT), 0, false)),
    required("catalog_digest", Digest),
];

const VARIANT: &[Field] = &[
    required("variant_id", CommonId),
    required("function_id", CommonId),
    required("implementation_path", ImplementationPath),
    required("implementation_digest", Digest),
    required(
        "execution_kind",
        OneOf(&["wasm", "pack_vm", "host_extension", "remote"]),
    ),
    required("platform", Nonempty),
    required("architecture", Nonempty),
    required("runtime_abi", Nonempty),
    required("backend", CommonId),
    required(
        "materialization_mode",
        OneOf(&["eager", "continuous", "on_demand", "event_wake"]),
    ),
    required("execution_domain_profile", CommonId),
    required("operations", array(&Closed(EXECUTABLE_OPERATION), 1, false)),
];

const EXECUTABLE_OPERATION: &[Field] = &[
    required("contract_id", CommonContractId),
    required("contract_version", Nonempty),
    required("revision_digest", Digest),
    required("operation_id", CommonId),
    required("input_schema", Object),
    required("output_schema", Object),
    required("error_schema", Object),
    required(
        "effect_class",
        OneOf(&["pure", "read", "write", "external_effect", "privileged"]),
    ),
    required("timeout_default_ms", Integer(1)),
    required("timeout_hard_max_ms", Integer(1)),
    required("idempotency", OneOf(&["none", "keyed", "replayable"])),
];

const CONTRACT_CATALOG: &[Field] = &[
    required(
        "catalog_api_version",
        OneOf(&["io.tobkiri.pack-contract-catalog.v4"]),
    ),
    required("pack_id", Id),
    required("source_identity", Digest),
    required("contracts", array(&Closed(CONTRACT_DOCUMENT), 0, false)),
];

const CONTRACT_DOCUMENT: &[Field] = &[
    required("contract_api_version", OneOf(&["io.tobkiri.contract.v4"])),
    required("contract_id", ContractId),
    required("version", Semver),
    required("revision_digest", Digest),
    required("owner", Id),
    required(
        "status",
        OneOf(&["draft", "accepted", "deprecated", "sunset"]),
    ),
    required("operations", array(&Closed(CONTRACT_OPERATION), 1, false)),
    required("schema_catalog", SchemaCatalog),
    required("provider_semantics", Closed(PROVIDER_SEMANTICS)),
    required("provenance", Provenance),
];

const CONTRACT_OPERATION: &[Field] = &[
    required("operation_id", Id),
    required("input_schema_digest", Digest),
    required("output_schema_digest", Digest),
    required("error_schema_digest", Digest),
    optional("progress_schema_digest", Digest),
    required("effect_ceiling", array(&Nonempty, 0, true)),
    optional("scope_semantics_digest", Digest),
    optional(
        "scope_semantics",
        OneOf(&["declarative", "host_broker", "opaque", "unknown"]),
    ),
    optional("idempotency", Closed(IDEMPOTENCY)),
    optional("timeout_default_ms", Integer(1)),
    optional("timeout_hard_max_ms", Integer(1)),
];

const IDEMPOTENCY: &[Field] = &[
    required("mode", OneOf(&["none", "keyed", "replayable"])),
    optional("key_namespace", String),
    optional("reconcile_operation_id", Id),
    optional("retention_seconds", Integer(0)),
];

const PROVIDER_SEMANTICS: &[Field] = &[
    required("provider_id", Id),
    required(
        "cardinality",
        OneOf(&["one", "many", "keyed", "chain", "fanout", "optional"]),
    ),
    required(
        "security",
        OneOf(&["public", "internal", "sensitive", "restricted"]),
    ),
    required(
        "failure",
        OneOf(&["fail_closed", "isolate", "continue_chain", "best_effort"]),
    ),
    required(
        "isolation",
        OneOf(&["in_process", "process", "sandbox", "remote", "declarative"]),
    ),
    required("required_capabilities", array(&Nonempty, 0, true)),
    required("lifecycle", Object),
    optional("routing_keys", array(&Nonempty, 0, true)),
    optional("instance_key", Nonempty),
    optional("priority", Integer(i64::MIN)),
    optional("before", array(&String, 0, true)),
    optional("after", array(&String, 0, true)),
];

const ARTIFACT_INDEX: &[Field] = &[
    required(
        "index_api_version",
        OneOf(&["io.tobkiri.pack-artifact-index.v4"]),
    ),
    required("pack_id", Id),
    required("source_identity", Digest),
    required("artifacts", array(&Closed(INDEX_ENTRY), 2, false)),
    required("artifact_set_digest", Digest),
    required("integrity_seal", Closed(INDEX_SEAL)),
];

const INDEX_ENTRY: &[Field] = &[
    required("path", Nonempty),
    required("digest", Digest),
    required(
        "role",
        OneOf(&[
            "canonical_manifest",
            "contract_catalog",
            "runtime",
            "asset",
            "schema",
            "sidecar",
            "host_contract_contribution",
            "host_contract_update_metadata",
        ]),
    ),
];

const INDEX_SEAL: &[Field] = &[
    required("algorithm", OneOf(&["sha256-canonical-v1"])),
    required("signed_digest", Digest),
];

const MANIFEST: &[Field] = &[
    required("pack_api_version", OneOf(&["io.tobkiri.pack.v4"])),
    required("pack", Closed(PACK)),
    required("functions", array(&Closed(FUNCTION), 0, false)),
    required("contracts", array(&Closed(CONTRACT), 0, false)),
    required("artifacts", array(&Closed(ARTIFACT), 0, false)),
    required("requirements", Closed(REQUIREMENTS)),
    required("operation_catalog", array(&Closed(OPERATION), 0, false)),
    required("provider_catalog", array(&Closed(PROVIDER), 0, false)),
    required("integrity", Closed(INTEGRITY)),
    required("provenance", Provenance),
    required("migration", Closed(MIGRATION)),
];

const PACK: &[Field] = &[
    required("id", Id),
    required("version", Semver),
    required(
        "kind",
        OneOf(&[
            "normal_sandbox",
            "host_extension",
            "base",
            "shell",
            "application",
        ]),
    ),
    required("artifact_digest", Digest),
    required("display_name", Nonempty),
    optional("description", String),
    optional("publisher_id", Id),
    optional("signing_lineage_digest", Digest),
];

const FUNCTION: &[Field] = &[
    required("id", Id),
    required("implementation_digest", Digest),
    required("contract_revision_digest", Digest),
    required("operations", array(&Id, 1, true)),
    required(
        "role",
        OneOf(&[
            "pure",
            "vendor_semantic",
            "brokered",
            "host_capability_provider",
        ]),
    ),
    optional(
        "isolation",
        OneOf(&["wasm_component", "pack_vm", "dedicated_process", "remote"]),
    ),
];

const CONTRACT: &[Field] = &[
    required("contract_id", ContractId),
    required("revision_digest", Digest),
    required("operations", array(&Id, 1, true)),
];

const ARTIFACT: &[Field] = &[
    required("path", Nonempty),
    required("digest", Digest),
    required(
        "kind",
        OneOf(&[
            "executable",
            "schema",
            "asset",
            "variant",
            "sidecar",
            "ui.contribution",
        ]),
    ),
    optional("platform", String),
    optional("entrypoint", Nonempty),
    optional("entrypoint_digest", Digest),
    optional("argv", array(&String, 0, false)),
];

const REQUIREMENTS: &[Field] = &[
    required("pack_dependencies", Map(&Nonempty)),
    required(
        "contract_dependencies",
        array(&Closed(DEPENDENCY), 0, false),
    ),
    required("capabilities", array(&Nonempty, 0, true)),
    required("network", Object),
    required("secrets", array(&Any, 0, true)),
    required(
        "execution_boundary",
        OneOf(&["sandbox", "host_brokered", "declarative_only", "remote"]),
    ),
    required(
        "approval_policy",
        OneOf(&["none", "capability_gated", "always"]),
    ),
    required(
        "workspace_boundary",
        OneOf(&["pack_local", "workspace_brokered", "host_brokered", "none"]),
    ),
];

const DEPENDENCY: &[Field] = &[
    required("contract_id", ContractId),
    required("version_range", Nonempty),
    required(
        "cardinality",
        OneOf(&["one", "many", "keyed", "chain", "fanout", "optional"]),
    ),
    required("optional", Bool),
    optional("instance_key", Nonempty),
    optional("operations", array(&Id, 1, true)),
];

const OPERATION: &[Field] = &[
    required("operation_id", Id),
    required("owner", Id),
    optional("contract_reference", ContractId),
    optional("provider_id", Id),
    required(
        "source_kind",
        OneOf(&[
            "canonical_v4_contract",
            "v3_contract",
            "legacy_component",
            "legacy_connectivity",
        ]),
    ),
    required("effect_ceiling", array(&Nonempty, 0, true)),
];

const PROVIDER: &[Field] = &[
    required("provider_id", Id),
    required("owner", Id),
    required("contract_reference", ContractId),
    required("operations", array(&Id, 1, true)),
];

const INTEGRITY: &[Field] = &[
    required("source_identity", Digest),
    required("artifact_set_digest", Digest),
    required("contract_catalog_digest", Digest),
];

const MIGRATION: &[Field] = &[
    required("compatibility", OneOf(&["none", "read_only"])),
    required("legacy_ids", array(&Nonempty, 0, true)),
    required("removal_wave", Integer(0)),
    required("sunset_at", Date),
];

const PROVENANCE_V1: &[Field] = &[
    required("schema", OneOf(&["io.tobkiri.provenance.v1"])),
    required(
        "source_kind",
        OneOf(&["repository", "migration", "generated", "external"]),
    ),
    required("source_path", Nonempty),
    required("source_digest", Digest),
    required("repository_commit", Commit),
    required("repository_tree", RepositoryTree),
    required("generator", Nonempty),
    required("generator_version", Semver),
    required("normative", Bool),
    required("evidence", array(&Closed(EVIDENCE_V1), 0, true)),
];

const PROVENANCE_V2: &[Field] = &[
    required("schema", OneOf(&["io.tobkiri.provenance.v2"])),
    required("source_kind", OneOf(&["generated"])),
    required("source_path", Nonempty),
    required("source_digest", Digest),
    required("repository_commit", Commit),
    required("repository_commit_trusted", False),
    required("content_root_digest", Digest),
    required("generator", Nonempty),
    required("generator_version", Semver),
    required("generator_path", Nonempty),
    required("generator_digest", Digest),
    required("input_inventory_digest", Digest),
    required("normative", True),
    required("evidence", array(&Closed(EVIDENCE_V2), 2, true)),
];

const EVIDENCE_V1: &[Field] = &[
    required("path", Nonempty),
    required("rule_id", Nonempty),
    required("digest", Digest),
    optional("line", Integer(1)),
];

const EVIDENCE_V2: &[Field] = &[
    required("path", Nonempty),
    required(
        "rule_id",
        OneOf(&["normative-generator-bytes", "normative-input-bytes"]),
    ),
    required("digest", Digest),
    optional("line", Integer(1)),
];

struct Validation<'a, 'inv, S> {
    doc: &'a Document,
    op: &'a Operation<'inv, S>,
    budget: &'a Arc<dyn AllocationBudget>,
}

impl<S: OriginalAuthorityOwner> Validation<'_, '_, S> {
    fn check(&self, id: usize, rule: Rule) -> Result<bool> {
        let node = self.doc.node(id);
        let text = match node {
            Node::String(text) => Some(text.0.as_slice()),
            _ => None,
        };
        charge(self.op, self.budget, 32 + text.map_or(0, |text| text.len()))?;
        let value = text.unwrap_or_default();
        let regex_value = value.strip_suffix(&[10]).unwrap_or(value);
        let valid = match rule {
            Any => true,
            String => text.is_some(),
            Nonempty => !value.is_empty(),
            Bool => matches!(node, Node::Scalar(CanonValue::Bool(_))),
            False => matches!(node, Node::Scalar(CanonValue::Bool(false))),
            True => matches!(node, Node::Scalar(CanonValue::Bool(true))),
            Integer(min) => matches!(node, Node::Scalar(CanonValue::Int(n)) if *n >= min),
            OneOf(options) => options.iter().any(|option| self.doc.string_is(id, option)),
            Id => id_syntax(regex_value),
            CommonId => value.len() <= 128 && id_syntax(regex_value),
            ContractId => contract_syntax(regex_value),
            CommonContractId => value.len() <= 128 && contract_syntax(regex_value),
            Digest => digest_syntax(regex_value),
            Semver => semver(regex_value),
            Date => date(value),
            ImplementationPath => {
                !regex_value.is_empty()
                    && regex_value.first() != Some(&47)
                    && !regex_value.contains(&10)
                    && !regex_value
                        .split(|point| *point == 47)
                        .any(|part| part == [46, 46])
            }
            Commit => lower_hex(regex_value, 40) || point_eq(regex_value, "working-tree"),
            RepositoryTree => lower_hex(regex_value, 64),
            Object => self.doc.entries(id).is_some(),
            Map(rule) => return self.check_fields(id, &[], Some(*rule)),
            SchemaCatalog => {
                let Some(entries) = self.doc.entries(id) else {
                    return Ok(false);
                };
                for (key, value) in entries {
                    charge(self.op, self.budget, 16 + key.0.len())?;
                    if !digest_syntax(key.0.strip_suffix(&[10]).unwrap_or(&key.0))
                        || self.doc.entries(*value).is_none()
                    {
                        return Ok(false);
                    }
                }
                true
            }
            Closed(fields) => return self.check_fields(id, fields, None),
            Array { items, min, unique } => {
                return self.check_array(node, *items, min, unique);
            }
            Provenance => {
                let version = self.doc.get(id, "schema");
                let fields = match version {
                    Some(version) if self.doc.string_is(version, "io.tobkiri.provenance.v1") => {
                        PROVENANCE_V1
                    }
                    Some(version) if self.doc.string_is(version, "io.tobkiri.provenance.v2") => {
                        PROVENANCE_V2
                    }
                    _ => return Ok(false),
                };
                return self.check_fields(id, fields, None);
            }
        };
        Ok(valid)
    }

    fn check_fields(&self, id: usize, shape: &[Field], others: Option<Rule>) -> Result<bool> {
        let Some(object) = self.doc.entries(id) else {
            return Ok(false);
        };
        if others.is_none() && object.len() > shape.len() {
            return Ok(false);
        }
        for field in shape {
            match self.doc.get(id, field.name) {
                Some(child) if self.check(child, field.rule)? => (),
                None if !field.required => (),
                _ => return Ok(false),
            }
        }
        for (name, child) in object {
            charge(self.op, self.budget, 16 + name.0.len())?;
            if let Some(rule) = others {
                if !self.check(*child, rule)? {
                    return Ok(false);
                }
            } else if !shape.iter().any(|field| name.is(field.name)) {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn check_array(&self, node: &Node, rule: Rule, min: usize, unique: bool) -> Result<bool> {
        let Node::Array(ids) = node else {
            return Ok(false);
        };
        if ids.len() < min {
            return Ok(false);
        }
        for (index, &id) in ids.iter().enumerate() {
            if !self.check(id, rule)? {
                return Ok(false);
            }
            if unique {
                for &previous in &ids[..index] {
                    if self.equals(previous, id)? {
                        return Ok(false);
                    }
                }
            }
        }
        Ok(true)
    }

    // Only strict JSON reaches this walk, so bools and integers have distinct
    // JSON Schema identities and comparisons use the validated depth bound.
    fn equals(&self, left: usize, right: usize) -> Result<bool> {
        charge(self.op, self.budget, 16)?;
        match (self.doc.node(left), self.doc.node(right)) {
            (Node::String(left), Node::String(right)) => {
                charge(self.op, self.budget, left.0.len().min(right.0.len()))?;
                Ok(left == right)
            }
            (Node::Scalar(left), Node::Scalar(right)) => Ok(left == right),
            (Node::Array(left), Node::Array(right)) if left.len() == right.len() => {
                for (&left, &right) in left.iter().zip(right) {
                    if !self.equals(left, right)? {
                        return Ok(false);
                    }
                }
                Ok(true)
            }
            (Node::Object(left), Node::Object(right)) if left.len() == right.len() => {
                for (name, left_value) in left {
                    let mut right_value = None;
                    for (right_name, value) in right {
                        charge(
                            self.op,
                            self.budget,
                            1 + name.0.len().min(right_name.0.len()),
                        )?;
                        if name == right_name {
                            right_value = Some(*value);
                            break;
                        }
                    }
                    let Some(right_value) = right_value else {
                        return Ok(false);
                    };
                    if !self.equals(*left_value, right_value)? {
                        return Ok(false);
                    }
                }
                Ok(true)
            }
            _ => Ok(false),
        }
    }

    fn check_semantic_fields(&self, id: usize, is_pack: bool) -> Result<bool> {
        charge(self.op, self.budget, 1)?;
        match self.doc.node(id) {
            Node::Object(fields) => {
                for (key, child) in fields {
                    charge(self.op, self.budget, 16 + key.0.len())?;
                    if is_pack && forbidden_authority_field(key) {
                        return Ok(false);
                    }
                    // The legacy check coerces binding identities with Python str().
                    if let Node::Array(items) = self.doc.node(*child)
                        && !items.is_empty()
                        && (key.is("bindings") || ends_with(&key.0, ".bindings"))
                    {
                        return Err(OriginalAuthorityError::unsupported(
                            STAGE,
                            "original CAS nested binding identity",
                        ));
                    }
                    if !self.check_semantic_fields(*child, is_pack)? {
                        return Ok(false);
                    }
                }
            }
            Node::Array(items) => {
                for &id in items {
                    if !self.check_semantic_fields(id, is_pack)? {
                        return Ok(false);
                    }
                }
            }
            _ => (),
        }
        Ok(true)
    }
}

fn forbidden_authority_field(key: &Text) -> bool {
    [
        "approved",
        "approval",
        "approvals",
        "grant",
        "grants",
        "invocation_lease",
        "provider_authority",
        "provider_authority_record",
        "secret_material",
        "raw_secret",
        "host_execution",
        "rumi_allow_host_execution",
    ]
    .into_iter()
    .any(|name| key.is(name))
}

fn ends_with(points: &[u32], value: &str) -> bool {
    points
        .len()
        .checked_sub(value.len())
        .is_some_and(|start| point_eq(&points[start..], value))
}

fn point_eq(points: &[u32], value: &str) -> bool {
    points.iter().copied().eq(value.bytes().map(u32::from))
}

fn lower_hex(value: &[u32], length: usize) -> bool {
    value.len() == length
        && value
            .iter()
            .all(|point| matches!(point, 48..=57 | 97..=102))
}

fn contract_syntax(value: &[u32]) -> bool {
    id_syntax(value) && value.iter().rposition(|&point| point == 46).is_some_and(|dot| {
        matches!(&value[dot + 1..], [118, 49..=57, rest @ ..] if rest.iter().all(|point| matches!(point, 48..=57)))
    })
}

fn digit(point: &u32) -> bool {
    matches!(point, 48..=57)
}

fn semver(value: &[u32]) -> bool {
    fn split(value: &[u32], separator: u32) -> (&[u32], Option<&[u32]>) {
        match value.iter().position(|&point| point == separator) {
            Some(at) => (&value[..at], Some(&value[at + 1..])),
            None => (value, None),
        }
    }
    let (version, build) = split(value, 43);
    let (numeric, prerelease) = split(version, 45);
    let mut parts = numeric.split(|point| *point == 46);
    let valid_number = |component: &[u32]| {
        matches!(component, [48] | [49..=57, ..]) && component.iter().all(digit)
    };
    if !(0..3).all(|_| parts.next().is_some_and(valid_number)) || parts.next().is_some() {
        return false;
    }
    [prerelease, build].into_iter().flatten().all(|suffix| {
        suffix.split(|point| *point == 46).all(|part| {
            !part.is_empty()
                && part
                    .iter()
                    .all(|point| matches!(point, 48..=57 | 65..=90 | 97..=122 | 45))
        })
    })
}

fn date(value: &[u32]) -> bool {
    let [_, _, _, _, 45, _, _, 45, _, _] = value else {
        return false;
    };
    let number = |digits: &[u32]| -> Option<u32> {
        digits.iter().try_fold(0, |value, point| {
            digit(point).then(|| value * 10 + point - 48)
        })
    };
    let (Some(year), Some(month), Some(day)) = (
        number(&value[..4]),
        number(&value[5..7]),
        number(&value[8..]),
    ) else {
        return false;
    };
    let max_day = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400)) => {
            29
        }
        2 => 28,
        _ => return false,
    };
    year > 0 && (1..=max_day).contains(&day)
}

#[cfg(test)]
mod tests {
    use super::super::super::definitions::tests::{operation, with_source};
    use super::*;
    use serde_json::Value;

    const EXECUTABLE_BYTES: &[u8] = include_bytes!(
        "../../../tobkiri_runtime/tests/fixtures/conformance_minimal_echo_pack/executables.v4.json"
    );
    const MANIFEST_BYTES: &[u8] = include_bytes!(
        "../../../tobkiri_runtime/tests/fixtures/conformance_minimal_echo_pack/pack.v4.json"
    );
    const CONTRACTS_BYTES: &[u8] = include_bytes!(
        "../../../tobkiri_runtime/tests/fixtures/conformance_minimal_echo_pack/contracts.v4.json"
    );
    const INDEX_BYTES: &[u8] = include_bytes!(
        "../../../tobkiri_runtime/tests/fixtures/conformance_minimal_echo_pack/artifact-index.v4.json"
    );

    fn assert_denial(error: OriginalAuthorityError, expected: &str) {
        assert_eq!(
            error.class,
            "core_runtime.external_pack_catalog_v4.ExternalPackCatalogDenied"
        );
        assert_eq!(
            error.message,
            "external Pack artifact digest verification failed"
        );
        assert!(error.suppressed);
        let Some(OriginalAuthorityCause::Python { message, .. }) = error.cause else {
            panic!("original exception cause missing");
        };
        assert_eq!(message, expected);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn direct_compiler_reads_validated_documents_in_order_before_identity_checks() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let mut contracts: Value = serde_json::from_slice(CONTRACTS_BYTES).unwrap();
            let source_identity = contracts["source_identity"].clone();
            let other_source_identity = Value::from(format!("sha256:{}", "0".repeat(64)));
            let cases = [
                ("conformance.minimal.echo", source_identity, None),
                (
                    "another.pack",
                    other_source_identity.clone(),
                    Some("v4 artifact documents disagree on Pack identity"),
                ),
                (
                    "conformance.minimal.echo",
                    other_source_identity,
                    Some("v4 artifact documents disagree on source identity"),
                ),
            ];
            for (pack_id, source_identity, expected) in cases {
                contracts["pack_id"] = Value::from(pack_id);
                contracts["source_identity"] = source_identity;
                let contracts = serde_json::to_vec(&contracts).unwrap();
                let mut paths = [
                    c"pack.v4.json",
                    c"contracts.v4.json",
                    c"artifact-index.v4.json",
                    c"executables.v4.json",
                ]
                .into_iter();
                let result = read_direct_definitions(
                    |name| {
                        assert_eq!(Some(name), paths.next());
                        let bytes = match name.to_bytes() {
                            b"pack.v4.json" => MANIFEST_BYTES,
                            b"contracts.v4.json" => contracts.as_slice(),
                            b"artifact-index.v4.json" => INDEX_BYTES,
                            b"executables.v4.json" => EXECUTABLE_BYTES,
                            _ => panic!("unexpected compiler file"),
                        };
                        let capacity = op.reserve(STAGE, bytes.len())?;
                        Ok(Funded {
                            value: Zeroizing::new(bytes.to_vec()),
                            _capacity: capacity,
                        })
                    },
                    &op,
                    budget,
                );
                assert!(paths.next().is_none());
                if let Some(expected) = expected {
                    let error = result
                        .err()
                        .expect("compiler identity mismatch was accepted");
                    assert!(matches!(
                        error.cause.as_ref(),
                        Some(OriginalAuthorityCause::Python { class, .. }) if *class == INVALID_ARTIFACT
                    ));
                    assert_denial(error, expected);
                } else {
                    assert!(result.is_ok());
                }
            }
            let mut paths = [c"pack.v4.json", c"contracts.v4.json"].into_iter();
            let error = read_direct_definitions(
                |name| {
                    assert_eq!(Some(name), paths.next());
                    let bytes = if name == c"pack.v4.json" {
                        MANIFEST_BYTES
                    } else {
                        b"[]"
                    };
                    let capacity = op.reserve(STAGE, bytes.len())?;
                    Ok(Funded {
                        value: Zeroizing::new(bytes.to_vec()),
                        _capacity: capacity,
                    })
                },
                &op,
                budget,
            )
            .err()
            .expect("non-object Contract catalog was accepted");
            assert!(paths.next().is_none());
            assert_denial(error, "serialized protocol document must be an object");
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn reads_original_echo_documents_and_checks_external_execution_kind() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let manifest = read_manifest(MANIFEST_BYTES, &op, budget).unwrap();
            let mut input: Value = serde_json::from_slice(EXECUTABLE_BYTES).unwrap();
            for kind in ["wasm", "pack_vm", "remote"] {
                input["variants"][0]["execution_kind"] = Value::from(kind);
                let data = serde_json::to_vec(&input).unwrap();
                let executable = read_executable(&data, &op, budget).unwrap();
                let result =
                    require_external_catalog_identity(&executable, &manifest, false, &op, budget);
                if kind == "remote" {
                    assert_denial(
                        result.unwrap_err(),
                        "external admission accepts only Normal Sandbox backends",
                    );
                } else {
                    assert!(!result.unwrap());
                }
            }
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn checks_host_extension_receipt_before_executable_identity_alias() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let mut manifest: Value = serde_json::from_slice(MANIFEST_BYTES).unwrap();
            manifest["pack"]["kind"] = Value::from("host_extension");
            let raw = serde_json::to_vec(&manifest).unwrap();
            let manifest = read_manifest(&raw, &op, budget).unwrap();

            let mut input: Value = serde_json::from_slice(EXECUTABLE_BYTES).unwrap();
            input["variants"][0]["execution_kind"] = Value::from("host_extension");
            let data = serde_json::to_vec(&input).unwrap();
            let executable = read_executable(&data, &op, budget).unwrap();
            assert!(
                require_external_catalog_identity(&executable, &manifest, true, &op, budget)
                    .unwrap()
            );
            input["materialization_catalog_digest"] = input["catalog_digest"].clone();
            let data = serde_json::to_vec(&input).unwrap();
            let executable = read_executable(&data, &op, budget).unwrap();
            for (is_host_extension, expected) in [
                (false, "Host Extension admission identity is missing"),
                (
                    true,
                    "external Pack cannot replace its executable catalog identity",
                ),
            ] {
                assert_denial(
                    require_external_catalog_identity(
                        &executable,
                        &manifest,
                        is_host_extension,
                        &op,
                        budget,
                    )
                    .unwrap_err(),
                    expected,
                );
            }
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn separates_original_strict_json_failure_from_unported_schema_diagnostics() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let non_object = read_executable(b"[]", &op, budget).err().unwrap();
            assert_denial(non_object, "serialized protocol document must be an object");

            let malformed = read_executable(b"{", &op, budget).err().unwrap();
            assert_denial(
                malformed,
                "invalid JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1): invalid JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
            );
            let schema = read_executable(b"{}", &op, budget).err().unwrap();
            assert!(matches!(
                schema.cause,
                Some(OriginalAuthorityCause::Unsupported(
                    "original CAS executable catalog schema diagnostics"
                ))
            ));
        });
    }
}
