//! Original external Normal Pack catalog DATA at 1cf025f1e.
//! The installed registry seal is separate from the install-event hash chain
//! and from later verification of each visible Pack in the quarantine CAS.
use crate::original_authority::{
    Funded, Operation, OriginalAuthorityCause, OriginalAuthorityError, OriginalAuthorityOwner,
    OriginalAuthorityStage, Result,
    json::{self, Document, Node, Text},
};
use std::{
    collections::{BTreeMap, HashSet},
    sync::Arc,
};
use tobkiri_canon::{
    AllocationBudget, AllocationKind, AllocationRequest, CanonValue, Digest, Variant,
};

pub(super) const MAX_CATALOG_BYTES: usize = 16 * 1024 * 1024;
const VERSION: &str = "io.tobkiri.external-normal-pack-catalog.v4";
const ENTRY_VERSION: &str = "io.tobkiri.external-normal-pack-catalog-entry.v4";
const EVENT: &str = "external_normal_pack_admitted";
const SCHEMA_ERROR: &str = "external Pack catalog schema validation failed";
const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;

pub(super) fn denied(message: &'static str) -> OriginalAuthorityError {
    let mut error = json::raw_error(
        "core_runtime.external_pack_catalog_v4.ExternalPackCatalogDenied",
        message,
    );
    error.stage = STAGE;
    error
}

fn schema_error() -> OriginalAuthorityError {
    let mut error = denied(SCHEMA_ERROR);
    error.cause = Some(crate::original_authority::python_error(
        "tobkiri_protocol.errors.SchemaValidationError",
        "external_pack_catalog validation failed",
    ));
    error.suppressed = true;
    error
}

fn capacity() -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(STAGE)
}

fn charge<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    work: u64,
) -> Result<()> {
    op.check(STAGE)?;
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work,
        })
        .map_err(|_| capacity())
}

fn decode_failure(mut error: OriginalAuthorityError) -> OriginalAuthorityError {
    match error.cause.as_ref() {
        Some(OriginalAuthorityCause::Python {
            class: "json.decoder.JSONDecodeError",
            ..
        }) => {
            let mut outer = denied("Host JSON file is invalid");
            outer.cause = error.cause.take();
            outer.suppressed = true;
            outer
        }
        Some(OriginalAuthorityCause::Python { class, .. }) => {
            // json.loads(bytes) only wraps JSONDecodeError in the original.
            let class = *class;
            let Some(OriginalAuthorityCause::Python {
                message,
                cause,
                suppressed,
                ..
            }) = error.cause.take()
            else {
                unreachable!("Python decoder error")
            };
            let mut original = json::raw_error(class, message);
            original.cause = cause.map(|cause| *cause);
            original.suppressed = suppressed;
            original
        }
        _ => error,
    }
}

pub(super) fn decode_host_json<S: OriginalAuthorityOwner>(
    bytes: &[u8],
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Funded<Document>> {
    if bytes.len() > MAX_CATALOG_BYTES {
        return Err(denied("Host JSON file changed while reading"));
    }
    let bytes = bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes);
    if matches!(bytes.first(), Some(0 | 0xfe | 0xff)) || bytes.get(1).is_some_and(|byte| *byte == 0)
    {
        return Err(OriginalAuthorityError::unsupported(
            STAGE,
            "original external Pack Host JSON byte encoding",
        ));
    }
    // The catalog decoder's fast allocation census assumes a complete token
    // stream. Small malformed input takes the funded legacy error path instead.
    let parsed = if bytes.len() <= 1024 {
        charge(op, budget, (bytes.len() as u64 + 1) * 1024)?;
        json::decode(bytes, op)
    } else {
        json::decode_catalog_manifest(bytes, op, budget)
    };
    parsed.map_err(decode_failure)
}

/// Parse raw legacy JSON, then validate the exact external catalog shape.
/// Schema validation appears before loading the signing key in the old Host.
pub(super) fn parse_envelope<S: OriginalAuthorityOwner>(
    bytes: &[u8],
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Funded<Document>> {
    let doc = decode_host_json(bytes, op, budget)?;
    let root = fixed_fields(
        &doc,
        doc.root,
        ["version", "entries", "journal", "signature"],
    )
    .ok_or_else(|| denied("external Normal Pack catalog fields are invalid"))?;
    if let Err(error) = json::validate_strict(&doc, op) {
        return Err(canonical_schema_failure(error));
    }
    let canonical = canonical(&doc, op, budget).map_err(canonical_schema_failure)?;
    drop(canonical);
    let [version, entries, journal, signature] = root;
    if !doc.string_is(version, VERSION) || !lower_hex_text(text(&doc, signature), 64, true) {
        return Err(schema_error());
    }
    let entries = doc.entries(entries).ok_or_else(schema_error)?;
    for (key, entry) in entries {
        charge(op, budget, key.0.len() as u64 + 1)?;
        let valid = match split_first(strip_schema_newline(&key.0), 64) {
            Some((id, digest)) => id_syntax(id) && digest_syntax(digest),
            None => id_syntax(strip_schema_newline(&key.0)),
        };
        if !valid || !entry_schema(&doc, *entry, true, op, budget)? {
            return Err(schema_error());
        }
    }
    let journal = array(&doc, journal).ok_or_else(schema_error)?;
    let _seen_capacity = reserve_set(op, journal.len())?;
    let mut seen = HashSet::new();
    seen.try_reserve(journal.len()).map_err(|_| capacity())?;
    for &event in journal {
        charge(op, budget, 1000)?;
        let Some(
            [
                sequence,
                event_type,
                admission,
                pack,
                artifact,
                content,
                previous,
                digest,
            ],
        ) = fixed_fields(&doc, event, EVENT_FIELDS)
        else {
            return Err(schema_error());
        };
        if !integer(&doc, sequence, 1)
            || !doc.string_is(event_type, EVENT)
            || !lower_hex_text(text(&doc, admission), 64, true)
            || !text(&doc, pack).is_some_and(|value| canonical_id(&value.0))
            || [artifact, content, previous, digest]
                .into_iter()
                .any(|id| !text(&doc, id).is_some_and(|value| digest_syntax(&value.0)))
        {
            return Err(schema_error());
        }
        let pack = &text(&doc, pack).expect("schema checked").0;
        let artifact = &text(&doc, artifact).expect("schema checked").0;
        if !seen.insert((pack.as_slice(), artifact.as_slice())) {
            return Err(schema_error());
        }
    }
    drop(seen);
    drop(_seen_capacity);
    validate_semantics(&doc, doc.root, SemanticPath::Root, op, budget)?;
    Ok(doc)
}

fn canonical_schema_failure(error: OriginalAuthorityError) -> OriginalAuthorityError {
    if error.class != "tobkiri_protocol.errors.CanonicalizationError" {
        return error;
    }
    let mut outer = denied(SCHEMA_ERROR);
    outer.cause = Some(OriginalAuthorityCause::Python {
        class: "tobkiri_protocol.errors.SchemaValidationError",
        message: error.message.clone(),
        cause: Some(Box::new(OriginalAuthorityCause::Python {
            class: error.class,
            message: error.message,
            cause: error.cause.map(Box::new),
            context: None,
            suppressed: error.suppressed,
        })),
        context: None,
        suppressed: true,
    });
    outer.suppressed = true;
    outer
}

fn canonical<S: OriginalAuthorityOwner>(
    doc: &Document,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Funded<Vec<u8>>> {
    let levels = u64::from(usize::BITS - doc.node_count().max(1).leading_zeros());
    let work = (doc.source_bytes() as u64)
        .checked_mul((levels + 2) * 8)
        .ok_or_else(capacity)?;
    charge(op, budget, work)?;
    let bytes = json::canonical(doc, None, op)?;
    if bytes.len() > tobkiri_canon::MAX_BYTES {
        return Err(json::strict_error("canonical JSON exceeds size limit"));
    }
    Ok(bytes)
}

fn fixed_fields<const N: usize>(doc: &Document, id: usize, names: [&str; N]) -> Option<[usize; N]> {
    let fields = doc.entries(id)?;
    if fields.len() != N {
        return None;
    }
    let mut values = [0; N];
    for (index, name) in names.into_iter().enumerate() {
        values[index] = doc.get(id, name)?;
    }
    Some(values)
}

fn text(doc: &Document, id: usize) -> Option<&Text> {
    if let Node::String(value) = doc.node(id) {
        Some(value)
    } else {
        None
    }
}

fn array(doc: &Document, id: usize) -> Option<&[usize]> {
    if let Node::Array(value) = doc.node(id) {
        Some(value)
    } else {
        None
    }
}

fn integer(doc: &Document, id: usize, min: i64) -> bool {
    matches!(doc.node(id), Node::Scalar(CanonValue::Int(value)) if *value >= min)
}

fn strip_schema_newline(value: &[u32]) -> &[u32] {
    // JSON Schema patterns use search with Python's `$`, not fullmatch.
    value.strip_suffix(&[10]).unwrap_or(value)
}

fn split_first(value: &[u32], separator: u32) -> Option<(&[u32], &[u32])> {
    let index = value.iter().position(|&point| point == separator)?;
    Some((&value[..index], &value[index + 1..]))
}

fn lower_hex(value: &[u32], count: usize) -> bool {
    value.len() == count
        && value
            .iter()
            .all(|&point| matches!(point, 48..=57 | 97..=102))
}

fn lower_hex_text(value: Option<&Text>, count: usize, schema_pattern: bool) -> bool {
    value.is_some_and(|text| {
        let value = if schema_pattern {
            strip_schema_newline(&text.0)
        } else {
            &text.0
        };
        lower_hex(value, count)
    })
}

pub(super) fn digest_syntax(value: &[u32]) -> bool {
    value
        .strip_prefix(&[115, 104, 97, 50, 53, 54, 58])
        .is_some_and(|hex| lower_hex(hex, 64))
}

pub(super) fn id_syntax(value: &[u32]) -> bool {
    if !matches!(value.first(), Some(97..=122)) {
        return false;
    }
    let mut separator = false;
    for &point in value {
        match point {
            97..=122 | 48..=57 => separator = false,
            46 | 95 | 45 if !separator => separator = true,
            _ => return false,
        }
    }
    !separator
}

fn starts_with(value: &[u32], prefix: &str) -> bool {
    value.len() >= prefix.len()
        && value[..prefix.len()]
            .iter()
            .copied()
            .eq(prefix.bytes().map(u32::from))
}

fn canonical_id(value: &[u32]) -> bool {
    value.len() <= 128
        && id_syntax(value)
        && !["rumi.", "rumiai.", "viewer.", "legacy."]
            .into_iter()
            .any(|prefix| starts_with(value, prefix))
}

fn reserve_set<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    len: usize,
) -> Result<tobkiri_gate::NativeSetupReservation> {
    op.reserve(
        STAGE,
        len.checked_mul(256)
            .and_then(|n| n.checked_add(256))
            .ok_or_else(capacity)?,
    )
}

const EVENT_FIELDS: [&str; 8] = [
    "sequence",
    "event_type",
    "admission_id",
    "pack_id",
    "artifact_digest",
    "content_digest",
    "previous_event_digest",
    "event_digest",
];
const ENTRY_COMMON: [&str; 10] = [
    "version",
    "state",
    "pack_id",
    "version_string",
    "artifact_digest",
    "content_digest",
    "publisher_id",
    "key_id",
    "catalog_record",
    "store_token",
];

fn entry_schema<S: OriginalAuthorityOwner>(
    doc: &Document,
    id: usize,
    allow_prepared: bool,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    charge(op, budget, 1000)?;
    let Some(fields) = doc.entries(id) else {
        return Ok(false);
    };
    let mut values = [0; ENTRY_COMMON.len()];
    for (index, field) in ENTRY_COMMON.into_iter().enumerate() {
        let Some(id) = doc.get(id, field) else {
            return Ok(false);
        };
        values[index] = id;
    }
    let [
        version,
        state,
        pack,
        version_string,
        artifact,
        content,
        publisher,
        key,
        record,
        token,
    ] = values;
    let token_valid = if let Some(token) = text(doc, token) {
        charge(op, budget, token.0.len() as u64 + 1)?;
        split_first(strip_schema_newline(&token.0), 47)
            .is_some_and(|(hex, pack)| lower_hex(hex, 64) && id_syntax(pack))
    } else {
        false
    };
    if !doc.string_is(version, ENTRY_VERSION)
        || !text(doc, pack).is_some_and(|text| canonical_id(&text.0))
        || [version_string, publisher, key]
            .into_iter()
            .any(|field| !nonempty(doc, field))
        || [artifact, content]
            .into_iter()
            .any(|field| !text(doc, field).is_some_and(|text| digest_syntax(&text.0)))
        || !token_valid
        || !record_schema(doc, record, op, budget)?
    {
        return Ok(false);
    }
    if doc.string_is(state, "committed") {
        Ok(fields.len() == 13
            && lower_hex_text(
                doc.get(id, "admission_id").and_then(|id| text(doc, id)),
                64,
                true,
            )
            && ["root_device", "root_inode"].into_iter().all(|name| {
                doc.get(id, name)
                    .is_some_and(|field| integer(doc, field, 0))
            }))
    } else if allow_prepared && doc.string_is(state, "prepared") {
        if fields.len() != 12
            || !lower_hex_text(
                doc.get(id, "transaction_nonce")
                    .and_then(|id| text(doc, id)),
                64,
                true,
            )
        {
            return Ok(false);
        }
        let Some(previous) = doc.get(id, "previous") else {
            return Ok(false);
        };
        if matches!(doc.node(previous), Node::Scalar(CanonValue::Null)) {
            Ok(true)
        } else {
            entry_schema(doc, previous, false, op, budget)
        }
    } else {
        Ok(false)
    }
}

fn nonempty(doc: &Document, id: usize) -> bool {
    text(doc, id).is_some_and(|value| !value.0.is_empty())
}

fn record_schema<S: OriginalAuthorityOwner>(
    doc: &Document,
    id: usize,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    let Some(
        [
            pack,
            name,
            description,
            version,
            kind,
            digest,
            authority,
            approval,
            caps,
            dependencies,
            provided,
            required,
            runtime,
            execution,
            workspace,
            network,
            secrets,
            legacy_ids,
            legacy_operations,
            provenance,
        ],
    ) = fixed_fields(
        doc,
        id,
        [
            "pack_id",
            "display_name",
            "description",
            "version",
            "kind",
            "artifact_digest",
            "authority",
            "approval_policy",
            "capabilities",
            "dependencies",
            "provided_contracts",
            "required_contracts",
            "runtime_artifacts",
            "execution_boundary",
            "workspace_boundary",
            "network",
            "secrets",
            "legacy_ids",
            "legacy_operations",
            "source_provenance",
        ],
    )
    else {
        return Ok(false);
    };
    if !text(doc, pack).is_some_and(|value| canonical_id(&value.0))
        || !nonempty(doc, name)
        || text(doc, description).is_none()
        || !nonempty(doc, version)
        || !text(doc, digest).is_some_and(|value| digest_syntax(&value.0))
        || !doc.string_is(approval, "explicit")
        || doc.entries(dependencies).is_none()
        || doc.entries(network).is_none()
        || text(doc, workspace).is_none()
        || array(doc, secrets).is_none()
        || ![legacy_ids, legacy_operations]
            .into_iter()
            .all(|id| array(doc, id).is_some_and(<[usize]>::is_empty))
    {
        return Ok(false);
    }
    if doc.string_is(kind, "host_extension") {
        if !doc.string_is(authority, "host-signed-external-host-extension-v4")
            || !doc.string_is(execution, "host_brokered")
            || !doc.string_is(workspace, "host_brokered")
        {
            return Ok(false);
        }
    } else if !["normal_sandbox", "application"]
        .into_iter()
        .any(|value| doc.string_is(kind, value))
        || !doc.string_is(authority, "host-signed-external-normal-v4")
        || !["sandbox", "declarative_only"]
            .into_iter()
            .any(|value| doc.string_is(execution, value))
    {
        return Ok(false);
    }
    for id in [provided, required, runtime] {
        let Some(values) = array(doc, id) else {
            return Ok(false);
        };
        charge(op, budget, values.len() as u64)?;
        if !values.iter().all(|&value| doc.entries(value).is_some()) {
            return Ok(false);
        }
    }
    let Some([mode, publisher, key]) =
        fixed_fields(doc, provenance, ["mode", "publisher_id", "key_id"])
    else {
        return Ok(false);
    };
    if !doc.string_is(mode, "host-signed-external-normal-catalog")
        || !nonempty(doc, publisher)
        || !nonempty(doc, key)
    {
        return Ok(false);
    }
    let Some(caps) = array(doc, caps) else {
        return Ok(false);
    };
    let _seen_capacity = reserve_set(op, caps.len())?;
    let mut seen = HashSet::new();
    seen.try_reserve(caps.len()).map_err(|_| capacity())?;
    for &id in caps {
        let Some(capability) = text(doc, id) else {
            return Ok(false);
        };
        charge(op, budget, capability.0.len() as u64 + 1)?;
        if !seen.insert(capability.0.as_slice()) {
            return Ok(false);
        }
    }
    Ok(true)
}

const IDS: [&str; 10] = [
    "id",
    "pack_id",
    "profile_id",
    "function_id",
    "operation_id",
    "provider_id",
    "provider_instance_id",
    "distribution_id",
    "caller_function_id",
    "target_provider_id",
];
const REFERENCES: [&str; 3] = [
    "authority_reference",
    "lease_reference",
    "resource_reference",
];

fn point_eq(text: &[u32], value: &str) -> bool {
    text.iter().copied().eq(value.bytes().map(u32::from))
}

fn suffix(text: &[u32], value: &str) -> bool {
    text.len() >= value.len() && point_eq(&text[text.len() - value.len()..], value)
}

fn contract_id(value: &[u32]) -> bool {
    canonical_id(value)
        && value.iter().rposition(|&point| point == 46).is_some_and(|dot| {
            let version = &value[dot + 1..];
            matches!(version, [118, 49..=57, rest @ ..] if rest.iter().all(|point| matches!(point, 48..=57)))
        })
}

fn opaque_reference(value: &[u32]) -> bool {
    let Some((namespace, value)) = split_first(value, 58) else {
        return false;
    };
    ["authority-ref", "lease", "handle"]
        .into_iter()
        .any(|name| point_eq(namespace, name))
        && (8..=128).contains(&value.len())
        && matches!(value.first(), Some(97..=122 | 48..=57))
        && value
            .iter()
            .all(|&point| matches!(point, 97..=122 | 48..=57 | 46 | 95 | 45))
}

#[derive(Clone, Copy)]
pub(super) enum SemanticPath {
    Root,
    Journal,
    Bindings,
    Operations,
    Other,
}

impl SemanticPath {
    fn child(self, key: &Text) -> Self {
        if matches!(self, Self::Root) && key.is("journal") {
            Self::Journal
        } else if key.is("bindings") || suffix(&key.0, ".bindings") {
            Self::Bindings
        } else if key.is("operations")
            || suffix(&key.0, ".operations")
            || matches!(self, Self::Root) && key.is("operation_catalog")
        {
            Self::Operations
        } else {
            Self::Other
        }
    }
}

pub(super) fn validate_semantics<S: OriginalAuthorityOwner>(
    doc: &Document,
    id: usize,
    path: SemanticPath,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    // validate_strict has already established depth <= 64 and excluded
    // non-canonical numeric or Unicode representations.
    charge(op, budget, 1)?;
    match doc.node(id) {
        Node::Object(fields) => {
            for (key, child) in fields {
                charge(op, budget, (key.0.len() as u64 + 1) * 4)?;
                if let Some(value) = text(doc, *child) {
                    let value = &value.0;
                    if IDS.into_iter().any(|name| key.is(name)) && !canonical_id(value)
                        || key.is("contract_id") && !contract_id(value)
                        || suffix(&key.0, "_digest")
                            && !key.is("tree_digest")
                            && !digest_syntax(value)
                        || REFERENCES.into_iter().any(|name| key.is(name))
                            && !opaque_reference(value)
                    {
                        return Err(schema_error());
                    }
                }
                validate_semantics(doc, *child, path.child(key), op, budget)?;
            }
        }
        Node::Array(items) => {
            match path {
                SemanticPath::Journal => (), // revision identity checked by the fixed schema
                SemanticPath::Bindings => {
                    let _capacity = reserve_set(op, items.len())?;
                    let mut seen = HashSet::new();
                    seen.try_reserve(items.len()).map_err(|_| capacity())?;
                    for &id in items {
                        if doc.entries(id).is_none() {
                            continue;
                        }
                        charge(op, budget, 32)?;
                        let principal = doc
                            .get(id, "function_principal")
                            .filter(|&value| doc.entries(value).is_some());
                        let target = if let Some(principal) = principal {
                            doc.get(principal, "function_id")
                        } else {
                            doc.get(id, "target_provider_id")
                        };
                        let binding = [
                            doc.get(id, "caller_function_id"),
                            target,
                            doc.get(id, "contract_id"),
                            doc.get(id, "operation_id"),
                        ]
                        .map(|field| binding_part(doc, field));
                        let [a, b, c, d] = binding;
                        let binding = [a?, b?, c?, d?];
                        for part in &binding {
                            charge(op, budget, part.work())?;
                        }
                        if !seen.insert(binding) {
                            return Err(schema_error());
                        }
                    }
                }
                SemanticPath::Operations | SemanticPath::Other | SemanticPath::Root => {
                    let fields = if matches!(path, SemanticPath::Operations) {
                        &["operation_id"][..]
                    } else {
                        &[
                            "id",
                            "pack_id",
                            "profile_id",
                            "function_id",
                            "operation_id",
                            "provider_id",
                            "provider_instance_id",
                            "distribution_id",
                            "caller_function_id",
                            "target_provider_id",
                            "contract_id",
                        ][..]
                    };
                    let _capacity = reserve_set(
                        op,
                        items.len().checked_mul(fields.len()).ok_or_else(capacity)?,
                    )?;
                    let mut seen = HashSet::new();
                    seen.try_reserve(items.len().checked_mul(fields.len()).ok_or_else(capacity)?)
                        .map_err(|_| capacity())?;
                    for &id in items {
                        let Some(object) = doc.entries(id) else {
                            continue;
                        };
                        for (key, child) in object {
                            if let Some(value) = text(doc, *child)
                                && let Some(field) = fields.iter().position(|name| key.is(name))
                            {
                                charge(op, budget, value.0.len() as u64 + 1)?;
                                if !seen.insert((field, value.0.as_slice())) {
                                    return Err(schema_error());
                                }
                            }
                        }
                    }
                }
            }
            for &id in items {
                validate_semantics(doc, id, SemanticPath::Other, op, budget)?;
            }
        }
        _ => (),
    }
    Ok(())
}

#[derive(Eq, Hash, PartialEq)]
enum BindingPart<'a> {
    Text(&'a [u32]),
    Number(i64),
    True,
}
impl BindingPart<'_> {
    fn work(&self) -> u64 {
        match self {
            Self::Text(value) => value.len() as u64 + 1,
            _ => 16,
        }
    }
}

fn binding_part(doc: &Document, id: Option<usize>) -> Result<BindingPart<'_>> {
    let value = match id.map(|id| doc.node(id)) {
        None
        | Some(Node::Scalar(CanonValue::Null | CanonValue::Bool(false) | CanonValue::Int(0))) => {
            BindingPart::Text(&[])
        }
        Some(Node::Scalar(CanonValue::Bool(true))) => BindingPart::True,
        Some(Node::Scalar(CanonValue::Int(value))) => BindingPart::Number(*value),
        Some(Node::String(value)) => BindingPart::Text(&value.0),
        Some(Node::Array(items)) if items.is_empty() => BindingPart::Text(&[]),
        Some(Node::Object(items)) if items.is_empty() => BindingPart::Text(&[]),
        _ => {
            return Err(OriginalAuthorityError::unsupported(
                STAGE,
                "external Pack catalog nested binding identity coercion",
            ));
        }
    };
    Ok(value)
}

/// Authenticate with the bytes of the key returned by the original Host key
/// loader, then check install-event chain integrity. No key is created here.
pub(super) fn authenticate_and_validate_journal<S: OriginalAuthorityOwner>(
    payload: &mut Document,
    key: &[u8],
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let Node::Object(fields) = &mut payload.nodes[payload.root] else {
        return Err(denied("external Normal Pack catalog fields are invalid"));
    };
    let index = fields
        .iter()
        .position(|(name, _)| name.is("signature"))
        .ok_or_else(|| denied("external Normal Pack catalog fields are invalid"))?;
    let signature = fields.remove(index);
    let result = (|| {
        let signature = text(payload, signature.1);
        let bytes = canonical(payload, op, budget)?;
        charge(op, budget, bytes.len() as u64 + key.len() as u64)?;
        let mut hex = [b'0'; 64];
        let valid = lower_hex_text(signature, 64, false);
        if let Some(signature) = signature.filter(|_| valid) {
            for (byte, &point) in hex.iter_mut().zip(&signature.0) {
                *byte = point as u8;
            }
        }
        let matched = tobkiri_canon::verify_seal_hex(
            &bytes,
            std::str::from_utf8(&hex).expect("checked ASCII seal"),
            key,
        )
        .is_ok();
        if !valid || !matched {
            return Err(denied("external Normal Pack catalog authentication failed"));
        }
        drop(bytes);
        validate_journal(payload, op, budget)
    })();
    if result.is_err() {
        let Node::Object(fields) = &mut payload.nodes[payload.root] else {
            unreachable!("fixed catalog envelope");
        };
        fields.insert(index, signature);
    }
    result
}

const INITIAL_DIGEST: &str =
    "sha256:0000000000000000000000000000000000000000000000000000000000000000";

fn validate_journal<S: OriginalAuthorityOwner>(
    doc: &Document,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let events = doc
        .get(doc.root, "journal")
        .and_then(|id| array(doc, id))
        .ok_or_else(|| denied("external Normal Pack catalog payload is invalid"))?;
    let _seen_capacity = reserve_set(op, events.len())?;
    let mut seen = HashSet::new();
    seen.try_reserve(events.len()).map_err(|_| capacity())?;
    let mut previous_hash: Option<Digest> = None;
    for (index, &id) in events.iter().enumerate() {
        charge(op, budget, 1024)?;
        if doc.entries(id).is_none() {
            return Err(denied("external Pack journal event is invalid"));
        }
        let [
            sequence,
            kind,
            admission,
            pack,
            artifact,
            content,
            predecessor,
            event,
        ] = fixed_fields(doc, id, EVENT_FIELDS)
            .ok_or_else(|| denied("external Pack journal fields are invalid"))?;
        if !matches!(doc.node(sequence), Node::Scalar(CanonValue::Int(value)) if *value == index as i64 + 1)
            || !doc.string_is(kind, EVENT)
            || !doc.string_is(
                predecessor,
                previous_hash
                    .as_ref()
                    .map_or(INITIAL_DIGEST, Digest::as_str),
            )
        {
            return Err(denied("external Pack journal chain is invalid"));
        }
        let _capacity = op.reserve(STAGE, 8192)?;
        let mut unsigned = BTreeMap::new();
        for (name, id) in EVENT_FIELDS.into_iter().take(7).zip([
            sequence,
            kind,
            admission,
            pack,
            artifact,
            content,
            predecessor,
        ]) {
            let value = match doc.node(id) {
                Node::Scalar(CanonValue::Int(value)) => CanonValue::Int(*value),
                Node::String(value) => CanonValue::Str(value.utf8()?),
                _ => return Err(denied("external Pack journal chain is invalid")),
            };
            unsigned.insert(name.to_owned(), value);
        }
        let bytes =
            tobkiri_canon::encode(&CanonValue::Obj(unsigned), Variant::StrictV1).map_err(|_| {
                OriginalAuthorityError::unsupported(STAGE, "external Pack journal event encoding")
            })?;
        let digest = Digest::hash(&bytes);
        let admission = text(doc, admission)
            .ok_or_else(|| denied("external Pack journal admission is invalid"))?;
        if !doc.string_is(event, digest.as_str()) || seen.contains(admission.0.as_slice()) {
            return Err(denied("external Pack journal chain is invalid"));
        }
        if [artifact, content, event].into_iter().any(|id| {
            !text(doc, id)
                .is_some_and(|value| value.0.len() == 71 && starts_with(&value.0, "sha256:"))
        }) {
            return Err(denied("external Pack journal digest is invalid"));
        }
        if !lower_hex(&admission.0, 64) {
            return Err(denied("external Pack journal admission is invalid"));
        }
        seen.insert(admission.0.as_slice());
        previous_hash = Some(digest);
    }
    Ok(())
}

/// A prepared row exposes only its signed predecessor, if present.
pub(super) fn visible_entry(doc: &Document, id: usize) -> Result<Option<usize>> {
    if doc.entries(id).is_none() {
        return Err(denied("external Pack catalog entry is malformed"));
    }
    let state = doc.get(id, "state");
    if state.is_some_and(|state| doc.string_is(state, "committed")) {
        return Ok(Some(id));
    }
    if !state.is_some_and(|state| doc.string_is(state, "prepared")) {
        return Err(denied(
            "external Normal Pack catalog transaction state is invalid",
        ));
    }
    let Some(previous) = doc.get(id, "previous") else {
        return Ok(None);
    };
    if matches!(doc.node(previous), Node::Scalar(CanonValue::Null)) {
        return Ok(None);
    }
    if !doc
        .get(previous, "state")
        .is_some_and(|state| doc.string_is(state, "committed"))
    {
        return Err(denied(
            "external Normal Pack catalog rollback entry is invalid",
        ));
    }
    Ok(Some(previous))
}

/// Bind the original catalog key to a visible entry before its CAS read.
/// Root location and device/inode checks still belong to the physical reader.
pub(super) fn validate_visible_entry(doc: &Document, key: &Text, id: usize) -> Result<()> {
    let pack_id = doc.get(id, "pack_id").and_then(|field| text(doc, field));
    let artifact = doc
        .get(id, "artifact_digest")
        .and_then(|field| text(doc, field));
    let key_matches = pack_id.is_some_and(|pack| {
        let pack = pack.0.as_slice();
        canonical_id(pack)
            && artifact.is_some_and(|artifact| digest_syntax(&artifact.0))
            && (pack == key.0
                || split_first(&key.0, 64).is_some_and(|(left, right)| {
                    left == pack && artifact.is_some_and(|artifact| right == artifact.0)
                }))
    });
    if !key_matches {
        let mut error = denied("Pack revision catalog key is invalid");
        error.cause = Some(crate::original_authority::python_error(
            "builtins.ValueError",
            "Pack revision catalog key is inconsistent",
        ));
        error.suppressed = true;
        return Err(error);
    }
    let [
        version,
        state,
        pack,
        _,
        artifact,
        content,
        _,
        _,
        _,
        _,
        admission,
        device,
        inode,
    ] = fixed_fields(
        doc,
        id,
        [
            "version",
            "state",
            "pack_id",
            "version_string",
            "artifact_digest",
            "content_digest",
            "publisher_id",
            "key_id",
            "catalog_record",
            "store_token",
            "admission_id",
            "root_device",
            "root_inode",
        ],
    )
    .filter(|fields| doc.string_is(fields[0], ENTRY_VERSION))
    .ok_or_else(|| denied("external Normal Pack catalog entry fields are invalid"))?;
    debug_assert!(doc.string_is(version, ENTRY_VERSION));
    if !doc.string_is(state, "committed") || text(doc, pack) != pack_id {
        return Err(denied(
            "external Normal Pack catalog entry state is invalid",
        ));
    }
    if !pack_id.is_some_and(|value| id_syntax(&value.0)) {
        return Err(denied("external Normal Pack catalog Pack ID is invalid"));
    }
    if [artifact, content].into_iter().any(|field| {
        !text(doc, field)
            .is_some_and(|value| value.0.len() == 71 && starts_with(&value.0, "sha256:"))
    }) {
        return Err(denied("external Normal Pack catalog digest is invalid"));
    }
    if !integer(doc, device, 0) || !integer(doc, inode, 0) {
        return Err(denied("external Pack root identity is invalid"));
    }
    if !lower_hex_text(text(doc, admission), 64, false) {
        return Err(denied("external Pack admission identity is invalid"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::io::publication::definitions::tests::{operation, with_source};
    use serde_json::{Value, json};

    const KEY: &[u8] = b"original-external-catalog-fixture-key";

    fn resign(payload: &mut Value) {
        payload.as_object_mut().unwrap().remove("signature");
        let bytes = serde_json::to_vec(payload).unwrap();
        let parsed = tobkiri_canon::parse(&bytes, Variant::StrictV1).unwrap();
        payload["signature"] = tobkiri_canon::seal(&parsed, Variant::StrictV1, KEY)
            .unwrap()
            .into();
    }

    fn revision(byte: char) -> String {
        format!("sha256:{}", byte.to_string().repeat(64))
    }

    fn event(sequence: u64, admission: char, artifact: char, previous: &str) -> Value {
        let mut row = json!({
            "sequence": sequence,
            "event_type": EVENT,
            "admission_id": admission.to_string().repeat(64),
            "pack_id": "local.fixture",
            "artifact_digest": revision(artifact),
            "content_digest": revision('c'),
            "previous_event_digest": previous,
        });
        let parsed =
            tobkiri_canon::parse(&serde_json::to_vec(&row).unwrap(), Variant::StrictV1).unwrap();
        row["event_digest"] = tobkiri_canon::digest(&parsed, Variant::StrictV1)
            .unwrap()
            .as_str()
            .into();
        row
    }

    fn with_journal(journal: Value) -> Value {
        json!({"version": VERSION, "entries": {}, "journal": journal})
    }

    #[tokio::test(flavor = "current_thread")]
    async fn external_catalog_model_checks_python_seal_and_event_hashes_over_parsed_json() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let previous =
                "sha256:8a6f55538d43421629be27631f132333d0530b42da5a2274597ab3e6cb07765e";
            let second = "sha256:918e461c355335c719f2b746a8b65cce0c6bd67574fa5f11941e96aabf6724e0";
            let first_row = event(1, '1', 'a', INITIAL_DIGEST);
            let second_row = event(2, '2', 'b', previous);
            assert_eq!(first_row["event_digest"], previous);
            assert_eq!(second_row["event_digest"], second);
            let mut data = with_journal(json!([first_row, second_row]));
            // CPython dumps(sort_keys=True, ensure_ascii=False, separators=...)
            // over the payload without the envelope signature.
            data["signature"] =
                "6c48f342408efc150d791c700b4d41b011edfa8c1320423852a24e5285aab448".into();
            let pretty = serde_json::to_string_pretty(&data).unwrap();
            // json.loads(bytes) accepts a UTF-8 BOM and last duplicate value.
            let bytes = format!("\u{feff}{{\"version\":null,{}", &pretty[1..]);
            let mut doc = parse_envelope(bytes.as_bytes(), &op, budget).unwrap();
            authenticate_and_validate_journal(&mut doc, KEY, &op, budget).unwrap();
            assert!(doc.get(doc.root, "signature").is_none());
            let journal = array(&doc, doc.get(doc.root, "journal").unwrap()).unwrap();
            assert_eq!(journal.len(), 2);
            assert!(doc.string_is(doc.get(journal[1], "event_digest").unwrap(), second));
            assert!(
                doc.entries(doc.get(doc.root, "entries").unwrap())
                    .unwrap()
                    .is_empty()
            );
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn external_catalog_model_preserves_decode_shape_schema_and_signature_order() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            for (bytes, expected) in [
                (b"{".as_slice(), "Host JSON file is invalid"),
                (b"", "Host JSON file is invalid"),
                (b"{}", "external Normal Pack catalog fields are invalid"),
                (
                    br#"{"version":"bad","entries":{},"journal":[],"signature":""}"#,
                    SCHEMA_ERROR,
                ),
            ] {
                let error = parse_envelope(bytes, &op, budget).err().unwrap();
                assert_eq!(error.message, expected);
            }
            let mut data = with_journal(json!([]));
            data["signature"] =
                "7214e6427d31615618c5409993db1b96fa51228a018dc03e2b433123af88ee42\n".into();
            let bytes = serde_json::to_vec(&data).unwrap();
            let mut doc = parse_envelope(&bytes, &op, budget).unwrap();
            let error = authenticate_and_validate_journal(&mut doc, KEY, &op, budget).unwrap_err();
            assert_eq!(
                error.message,
                "external Normal Pack catalog authentication failed"
            );
            assert!(doc.get(doc.root, "signature").is_some());
            resign(&mut data);
            let bytes = serde_json::to_vec(&data).unwrap();
            let mut doc = parse_envelope(&bytes, &op, budget).unwrap();
            let error = authenticate_and_validate_journal(
                &mut doc,
                b"wrong catalogue key over thirty two bytes",
                &op,
                budget,
            )
            .unwrap_err();
            assert_eq!(
                error.message,
                "external Normal Pack catalog authentication failed"
            );
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn external_catalog_model_rejects_journal_revisions_and_admissions_at_distinct_boundaries()
     {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let first = event(1, '1', 'a', INITIAL_DIGEST);
            let previous = first["event_digest"].as_str().unwrap();
            let mut duplicate_revision = with_journal(json!([first, event(2, '2', 'a', previous)]));
            resign(&mut duplicate_revision);
            let bytes = serde_json::to_vec(&duplicate_revision).unwrap();
            assert_eq!(
                parse_envelope(&bytes, &op, budget).err().unwrap().message,
                SCHEMA_ERROR
            );
            for latter in [
                event(2, '1', 'b', previous),
                event(2, '2', 'b', INITIAL_DIGEST),
                event(3, '2', 'b', previous),
            ] {
                let mut data = with_journal(json!([first, latter]));
                resign(&mut data);
                let mut doc =
                    parse_envelope(&serde_json::to_vec(&data).unwrap(), &op, budget).unwrap();
                let error =
                    authenticate_and_validate_journal(&mut doc, KEY, &op, budget).unwrap_err();
                assert_eq!(error.message, "external Pack journal chain is invalid");
            }
        });
    }

    fn committed(pack: &str) -> Value {
        json!({
            "version": ENTRY_VERSION,
            "state": "committed",
            "pack_id": pack,
            "version_string": "1.2.3",
            "artifact_digest": revision('a'),
            "content_digest": revision('b'),
            "publisher_id": "publisher",
            "key_id": "key",
            "store_token": format!("{}/local.separately-verified-cas", "f".repeat(64)),
            "admission_id": "c".repeat(64),
            "root_device": 1,
            "root_inode": 2,
            "catalog_record": {
                "pack_id": pack,
                "display_name": "Fixture 日本語",
                "description": "",
                "version": "1.2.3",
                "kind": "normal_sandbox",
                "artifact_digest": revision('a'),
                "authority": "host-signed-external-normal-v4",
                "approval_policy": "explicit",
                "capabilities": [],
                "dependencies": {},
                "provided_contracts": [],
                "required_contracts": [],
                "runtime_artifacts": [],
                "execution_boundary": "sandbox",
                "workspace_boundary": "workspace",
                "network": {},
                "secrets": [],
                "legacy_ids": [],
                "legacy_operations": [],
                "source_provenance": {
                    "mode": "host-signed-external-normal-catalog",
                    "publisher_id": "publisher",
                    "key_id": "key",
                },
            }
        })
    }

    #[tokio::test(flavor = "current_thread")]
    async fn external_catalog_model_exposes_only_prepared_predecessors_before_a_separate_cas_read()
    {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let committed = committed("local.fixture");
            assert_eq!(
                committed["catalog_record"]["display_name"],
                "Fixture 日本語"
            );
            let mut prepared = committed.clone();
            for key in ["admission_id", "root_device", "root_inode"] {
                prepared.as_object_mut().unwrap().remove(key);
            }
            prepared["state"] = "prepared".into();
            prepared["transaction_nonce"] = "e".repeat(64).into();
            prepared["artifact_digest"] = revision('d').into();
            prepared["previous"] = committed.clone();
            let mut absent = prepared.clone();
            absent["previous"] = Value::Null;
            let mut data = with_journal(json!([]));
            data["entries"] = json!({"local.fixture": prepared, "local.unrelated": absent});
            resign(&mut data);
            let bytes = serde_json::to_vec(&data).unwrap();
            let mut doc = parse_envelope(&bytes, &op, budget).unwrap();
            authenticate_and_validate_journal(&mut doc, KEY, &op, budget).unwrap();
            let entries = doc.entries(doc.get(doc.root, "entries").unwrap()).unwrap();
            let (key, id) = &entries[0];
            let predecessor = visible_entry(&doc, *id).unwrap().unwrap();
            validate_visible_entry(&doc, key, predecessor).unwrap();
            assert_ne!(*id, predecessor);
            assert!(doc.string_is(
                doc.get(predecessor, "artifact_digest").unwrap(),
                &revision('a')
            ));
            assert_eq!(visible_entry(&doc, entries[1].1).unwrap(), None);
            assert_eq!(
                validate_visible_entry(&doc, &entries[1].0, predecessor)
                    .unwrap_err()
                    .message,
                "Pack revision catalog key is invalid",
            );
        });
    }
}
