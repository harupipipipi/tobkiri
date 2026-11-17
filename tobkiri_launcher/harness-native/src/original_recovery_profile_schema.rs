//! Structural predicates in the bound persisted Profile and activation schemas.
use super::*;
use tobkiri_canon::{AllocationKind, AllocationRequest};

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const DIALECT: &str = "https://json-schema.org/draft/2020-12/schema";
const PROFILE_V4: &str = "https://schemas.tobkiri.local/io.tobkiri/profile.v4.schema.json";
const PROFILE_V5: &str = "https://schemas.tobkiri.local/io.tobkiri/profile.v5.schema.json";
const LOCK_V4: &str = "https://schemas.tobkiri.local/io.tobkiri/profile-lock.v4.schema.json";
const LOCK_V5: &str = "https://schemas.tobkiri.local/io.tobkiri/profile-lock.v5.schema.json";
const PLAN_V1: &str = "https://schemas.tobkiri.local/io.tobkiri/resolved-plan.v1.schema.json";
const PLAN_V2: &str = "https://schemas.tobkiri.local/io.tobkiri/resolved-plan.v2.schema.json";
const ACTIVATION_V1: &str =
    "https://schemas.tobkiri.local/io.tobkiri/activation-record.v1.schema.json";
const ACTIVATION_V2: &str =
    "https://schemas.tobkiri.local/io.tobkiri/activation-record.v2.schema.json";
const COMMON: &str = "https://schemas.tobkiri.local/io.tobkiri/common.v1.schema.json";
const PROJECTION: &str =
    "https://schemas.tobkiri.local/io.tobkiri/profile-content-projection.v1.schema.json";
const FUNCTION_PRINCIPAL: &str =
    "https://schemas.tobkiri.local/io.tobkiri/function-principal.v1.schema.json";
const DEFS: &str = "#/$defs/";
const MAX_SCHEMA_DEPTH: usize = 32;
const MAX_EVALUATION_DEPTH: usize = 48;

fn unsupported() -> OriginalAuthorityError {
    OriginalAuthorityError::unsupported(
        STAGE,
        "original persisted record structural schema closure",
    )
}

fn capacity() -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(STAGE)
}

#[derive(Clone, Copy)]
struct Schema {
    document: usize,
    id: usize,
}

impl Schema {
    fn at(self, id: usize) -> Self {
        Self { id, ..self }
    }
}

#[derive(Clone, Copy)]
enum Instance<'a> {
    Value(usize),
    PropertyName(&'a Text),
}

struct Validator<'a, 'inv, S> {
    op: &'a Operation<'inv, S>,
    budget: &'a Arc<dyn AllocationBudget>,
    schemas: &'a [&'a Document],
    record: &'a Document,
}

/// Apply the supported structural assertions before any semantic diagnostics.
pub(super) fn matches<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    schemas: &[&Document],
    record: &Document,
    record_id: usize,
) -> Result<bool> {
    if !matches!(schemas.len(), 3 | 4) {
        return Err(unsupported());
    }
    let validator = Validator {
        op,
        budget,
        schemas,
        record,
    };
    for (index, doc) in schemas.iter().enumerate() {
        if doc.source_bytes() > 64 * 1024 || doc.node_count() > 8192 {
            return Err(unsupported());
        }
        let Some(id) = doc.get(doc.root, "$id") else {
            return Err(unsupported());
        };
        let expected: &[_] = match index {
            0 => &[
                PROFILE_V4,
                PROFILE_V5,
                LOCK_V4,
                LOCK_V5,
                PLAN_V1,
                PLAN_V2,
                ACTIVATION_V1,
                ACTIVATION_V2,
            ],
            1 => &[COMMON],
            2 => &[PROJECTION],
            _ => &[FUNCTION_PRINCIPAL],
        };
        if !expected.iter().any(|value| doc.string_is(id, value))
            || !doc
                .get(doc.root, "$schema")
                .is_some_and(|id| doc.string_is(id, DIALECT))
        {
            return Err(unsupported());
        }
        validator.check_schema(
            Schema {
                document: index,
                id: doc.root,
            },
            0,
        )?;
    }
    validator.evaluate(
        Schema {
            document: 0,
            id: schemas[0].root,
        },
        Instance::Value(record_id),
        0,
    )
}

impl<S: OriginalAuthorityOwner> Validator<'_, '_, S> {
    fn work(&self, units: usize) -> Result<()> {
        self.op.check(STAGE)?;
        self.budget
            .reserve(AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes: 0,
                depth: 0,
                elements: 0,
                work: units as u64,
            })
            .map_err(|_| capacity())
    }

    fn resolve(&self, schema: Schema, reference: &Text) -> Result<Schema> {
        let mut buffer = [0; 256];
        let reference = ascii(reference, &mut buffer).ok_or_else(unsupported)?;
        if reference == FUNCTION_PRINCIPAL {
            let document = 3;
            let doc = self.schemas.get(document).ok_or_else(unsupported)?;
            return Ok(Schema {
                document,
                id: doc.root,
            });
        }
        let (document, fragment) = if reference.starts_with(DEFS) {
            (schema.document, reference)
        } else if let Some(fragment) = reference.strip_prefix(COMMON) {
            (1, fragment)
        } else if let Some(fragment) = reference.strip_prefix(PROJECTION) {
            (2, fragment)
        } else {
            return Err(unsupported());
        };
        let name = fragment.strip_prefix(DEFS).ok_or_else(unsupported)?;
        if name.is_empty() || !name.bytes().all(|byte| byte.is_ascii_alphanumeric()) {
            return Err(unsupported());
        }
        let doc = self.schemas[document];
        let id = doc
            .get(doc.root, "$defs")
            .and_then(|defs| doc.get(defs, name))
            .ok_or_else(unsupported)?;
        Ok(Schema { document, id })
    }

    fn check_schema(&self, schema: Schema, depth: usize) -> Result<()> {
        if depth > MAX_SCHEMA_DEPTH {
            return Err(unsupported());
        }
        self.work(1)?;
        let doc = self.schemas[schema.document];
        if matches!(doc.node(schema.id), Node::Scalar(CanonValue::Bool(_))) {
            return Ok(());
        }
        let mut keyword_buffer = [0; 24];
        for (name, id) in doc.entries(schema.id).ok_or_else(unsupported)? {
            self.work(name.0.len() + 1)?;
            let id = *id;
            let keyword = ascii(name, &mut keyword_buffer).ok_or_else(unsupported)?;
            let node = doc.node(id);
            match keyword {
                "$id" | "$schema" if schema.id == doc.root => {}
                "title" => {
                    string(node)?;
                }
                "properties" | "$defs" if keyword == "properties" || schema.id == doc.root => {
                    for (name, child) in doc.entries(id).ok_or_else(unsupported)? {
                        self.work(name.0.len() + 1)?;
                        self.check_schema(schema.at(*child), depth + 1)?;
                    }
                }
                "$ref" => {
                    self.resolve(schema, string(node)?)?;
                }
                "type" => match node {
                    Node::String(name) => {
                        ValueType::read(name)?;
                    }
                    Node::Array(types) if !types.is_empty() && types.len() <= 6 => {
                        let mut seen = 0u8;
                        for id in types {
                            let bit = 1 << ValueType::read(string(doc.node(*id))?)? as u8;
                            if seen & bit != 0 {
                                return Err(unsupported());
                            }
                            seen |= bit;
                        }
                    }
                    _ => return Err(unsupported()),
                },
                "const" => match node {
                    Node::String(_) | Node::Scalar(CanonValue::Bool(_) | CanonValue::Null) => {}
                    _ => return Err(unsupported()),
                },
                "enum" | "required" => {
                    let values = array(node)?;
                    if keyword == "enum" && values.is_empty() {
                        return Err(unsupported());
                    }
                    for id in values {
                        self.work(1)?;
                        string(doc.node(*id))?;
                    }
                }
                "minLength" | "maxLength" | "minItems" | "maxProperties" => {
                    limit(node)?;
                }
                "minimum" => {
                    if !matches!(node, Node::Scalar(CanonValue::Int(0 | 1))) {
                        return Err(unsupported());
                    }
                }
                "uniqueItems" => {
                    boolean(node)?;
                }
                "pattern" => {
                    Pattern::read(string(node)?)?;
                }
                // The pinned Python distribution loads FormatChecker without
                // the optional rfc3339_validator package. Its date-time format
                // is unregistered, so the original checks only the string type.
                "format" if doc.string_is(id, "date-time") => {}
                "items" | "propertyNames" | "additionalProperties" | "if" | "then" | "not" => {
                    self.check_schema(schema.at(id), depth + 1)?;
                }
                "allOf" | "anyOf" | "oneOf" => {
                    let branches = array(node)?;
                    if branches.is_empty() || branches.len() > 16 {
                        return Err(unsupported());
                    }
                    for child in branches {
                        self.check_schema(schema.at(*child), depth + 1)?;
                    }
                }
                _ => return Err(unsupported()),
            }
        }
        Ok(())
    }

    fn evaluate(&self, schema: Schema, instance: Instance<'_>, depth: usize) -> Result<bool> {
        if depth > MAX_EVALUATION_DEPTH {
            return Err(unsupported());
        }
        self.work(1)?;
        let doc = self.schemas[schema.document];
        let Node::Object(keywords) = doc.node(schema.id) else {
            return boolean(doc.node(schema.id));
        };
        let value = match instance {
            Instance::Value(id) => Some(self.record.node(id)),
            Instance::PropertyName(_) => None,
        };
        let text = match (instance, value) {
            (Instance::PropertyName(name), _) | (_, Some(Node::String(name))) => Some(name),
            _ => None,
        };
        let recurse = |id, instance| self.evaluate(schema.at(id), instance, depth + 1);
        let fields = value.and_then(|node| match node {
            Node::Object(fields) => Some(fields.as_slice()),
            _ => None,
        });
        let items = value.and_then(|node| match node {
            Node::Array(items) => Some(items.as_slice()),
            _ => None,
        });
        let mut keyword_buffer = [0; 24];
        for (name, id) in keywords {
            self.work(1)?;
            let id = *id;
            let keyword = ascii(name, &mut keyword_buffer).ok_or_else(unsupported)?;
            let node = doc.node(id);
            let matched = match keyword {
                "$id" | "$schema" | "$defs" | "title" | "then" | "format" => true,
                "$ref" => {
                    self.evaluate(self.resolve(schema, string(node)?)?, instance, depth + 1)?
                }
                "type" => match node {
                    Node::String(name) => ValueType::read(name)?.accepts(value),
                    _ => {
                        let mut accepted = false;
                        for id in array(node)? {
                            accepted |= ValueType::read(string(doc.node(*id))?)?.accepts(value);
                        }
                        accepted
                    }
                },
                "const" => match (node, value) {
                    (Node::String(expected), _) => self.text_matches(text, expected)?,
                    (Node::Scalar(expected), Some(Node::Scalar(actual))) => actual == expected,
                    _ => false,
                },
                "enum" => {
                    let mut accepted = false;
                    for id in array(node)? {
                        if self.text_matches(text, string(doc.node(*id))?)? {
                            accepted = true;
                            break;
                        }
                    }
                    accepted
                }
                "minLength" => {
                    text.is_none_or(|name| name.0.len() >= limit(node).expect("checked length"))
                }
                "maxLength" => {
                    text.is_none_or(|name| name.0.len() <= limit(node).expect("checked length"))
                }
                "minItems" => {
                    items.is_none_or(|items| items.len() >= limit(node).expect("checked length"))
                }
                "maxProperties" => {
                    fields.is_none_or(|fields| fields.len() <= limit(node).expect("checked length"))
                }
                "minimum" => match (node, value) {
                    (
                        Node::Scalar(CanonValue::Int(minimum)),
                        Some(Node::Scalar(CanonValue::Int(integer))),
                    ) => integer >= minimum,
                    _ => true,
                },
                "pattern" => {
                    if let Some(text) = text {
                        self.work(text.0.len() + 1)?;
                        Pattern::read(string(node)?)?.accepts(text)
                    } else {
                        true
                    }
                }
                "required" => {
                    if let Some(fields) = fields {
                        for id in array(node)? {
                            if self.find(fields, string(doc.node(*id))?)?.is_none() {
                                return Ok(false);
                            }
                        }
                    }
                    true
                }
                "properties" => {
                    if let Some(fields) = fields {
                        for (name, id) in doc.entries(id).ok_or_else(unsupported)? {
                            if let Some(value) = self.find(fields, name)?
                                && !recurse(*id, Instance::Value(value))?
                            {
                                return Ok(false);
                            }
                        }
                    }
                    true
                }
                "additionalProperties" => {
                    if let Some(fields) = fields {
                        let known = doc
                            .get(schema.id, "properties")
                            .and_then(|id| doc.entries(id))
                            .unwrap_or_default();
                        for (name, value) in fields {
                            if self.find(known, name)?.is_none()
                                && !recurse(id, Instance::Value(*value))?
                            {
                                return Ok(false);
                            }
                        }
                    }
                    true
                }
                "propertyNames" => {
                    if let Some(fields) = fields {
                        for (name, _) in fields {
                            if !recurse(id, Instance::PropertyName(name))? {
                                return Ok(false);
                            }
                        }
                    }
                    true
                }
                "items" => {
                    if let Some(items) = items {
                        for value in items {
                            if !recurse(id, Instance::Value(*value))? {
                                return Ok(false);
                            }
                        }
                    }
                    true
                }
                "uniqueItems" => {
                    if let Some(items) = items.filter(|_| boolean(node).expect("checked flag")) {
                        self.unique(items)?
                    } else {
                        true
                    }
                }
                "if" => {
                    if let Some(then) = doc.get(schema.id, "then")
                        && recurse(id, instance)?
                    {
                        recurse(then, instance)?
                    } else {
                        true
                    }
                }
                "not" => !recurse(id, instance)?,
                "allOf" => {
                    for id in array(node)? {
                        if !recurse(*id, instance)? {
                            return Ok(false);
                        }
                    }
                    true
                }
                "oneOf" | "anyOf" => {
                    let mut passed = false;
                    for id in array(node)? {
                        if recurse(*id, instance)? {
                            if passed {
                                return Ok(false);
                            }
                            passed = true;
                            if keyword == "anyOf" {
                                break;
                            }
                        }
                    }
                    passed
                }
                _ => return Err(unsupported()),
            };
            if !matched {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn text_matches(&self, text: Option<&Text>, expected: &Text) -> Result<bool> {
        let Some(text) = text.filter(|text| text.0.len() == expected.0.len()) else {
            return Ok(false);
        };
        self.work(text.0.len() + 1)?;
        Ok(text == expected)
    }

    fn find(&self, fields: &[(Text, usize)], name: &Text) -> Result<Option<usize>> {
        self.work(fields.len() + 1)?;
        for (field, id) in fields {
            if self.text_matches(Some(field), name)? {
                return Ok(Some(*id));
            }
        }
        Ok(None)
    }

    fn unique(&self, items: &[usize]) -> Result<bool> {
        if items.len() < 2 {
            return Ok(true);
        }
        let bytes = items
            .len()
            .checked_mul(std::mem::size_of::<(Digest, usize)>())
            .ok_or_else(capacity)?;
        let levels = usize::BITS - items.len().leading_zeros();
        self.work(
            bytes
                .checked_mul(levels as usize + 1)
                .ok_or_else(capacity)?,
        )?;
        let _scratch = self.op.reserve(STAGE, bytes)?;
        let mut by_digest = Vec::new();
        by_digest
            .try_reserve_exact(items.len())
            .map_err(|_| capacity())?;
        if by_digest.capacity() > items.len() {
            return Err(capacity());
        }
        for id in items {
            let bytes = json::canonical_subtree(self.record, *id, None, self.op, self.budget)?;
            self.work(bytes.len() + 1)?;
            by_digest.push((Digest::hash(&bytes), *id));
        }
        // These strict-JSON values have a unique canonical representation.
        // A digest only groups candidates. The bytes decide their equality.
        by_digest.sort_unstable();
        for (index, (digest, id)) in by_digest.iter().enumerate() {
            let collisions = &by_digest[index + 1..];
            if collisions.first().is_none_or(|(next, _)| digest != next) {
                continue;
            }
            let left = json::canonical_subtree(self.record, *id, None, self.op, self.budget)?;
            for (_, right) in collisions.iter().take_while(|(next, _)| digest == next) {
                let right =
                    json::canonical_subtree(self.record, *right, None, self.op, self.budget)?;
                self.work(left.len().min(right.len()) + 1)?;
                if left.as_slice() == right.as_slice() {
                    return Ok(false);
                }
            }
        }
        Ok(true)
    }
}

fn ascii<'a>(text: &Text, buffer: &'a mut [u8]) -> Option<&'a str> {
    let bytes = buffer.get_mut(..text.0.len())?;
    for (byte, point) in bytes.iter_mut().zip(&text.0) {
        *byte = u8::try_from(*point).ok()?;
    }
    if !bytes.is_ascii() {
        return None;
    }
    std::str::from_utf8(bytes).ok()
}

fn string(node: &Node) -> Result<&Text> {
    match node {
        Node::String(text) => Ok(text),
        _ => Err(unsupported()),
    }
}

fn array(node: &Node) -> Result<&[usize]> {
    match node {
        Node::Array(items) => Ok(items),
        _ => Err(unsupported()),
    }
}

fn boolean(node: &Node) -> Result<bool> {
    match node {
        Node::Scalar(CanonValue::Bool(value)) => Ok(*value),
        _ => Err(unsupported()),
    }
}

fn limit(node: &Node) -> Result<usize> {
    match node {
        Node::Scalar(CanonValue::Int(value)) => usize::try_from(*value).map_err(|_| unsupported()),
        _ => Err(unsupported()),
    }
}

#[derive(Clone, Copy)]
enum ValueType {
    Array,
    Boolean,
    Integer,
    Null,
    Object,
    String,
}

impl ValueType {
    fn read(text: &Text) -> Result<Self> {
        let mut buffer = [0; 7];
        match ascii(text, &mut buffer) {
            Some("array") => Ok(Self::Array),
            Some("boolean") => Ok(Self::Boolean),
            Some("integer") => Ok(Self::Integer),
            Some("null") => Ok(Self::Null),
            Some("object") => Ok(Self::Object),
            Some("string") => Ok(Self::String),
            _ => Err(unsupported()),
        }
    }

    fn accepts(self, value: Option<&Node>) -> bool {
        matches!(
            (self, value),
            (Self::Array, Some(Node::Array(_)))
                | (Self::Boolean, Some(Node::Scalar(CanonValue::Bool(_))))
                | (Self::Integer, Some(Node::Scalar(CanonValue::Int(_))))
                | (Self::Null, Some(Node::Scalar(CanonValue::Null)))
                | (Self::Object, Some(Node::Object(_)))
                | (Self::String, Some(Node::String(_)) | None)
        )
    }
}

const ID: &str = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$";
const CONTRACT: &str = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\.v[1-9][0-9]*$";
const SEMVER: &str = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$";

enum Pattern {
    Id,
    Contract,
    Digest,
    AuthorityReference,
    ActivationReference,
    OpaqueReference,
    Commit,
    Tree,
    Semver,
    ArtifactRoot,
    ProjectionRoot,
}

impl Pattern {
    fn read(text: &Text) -> Result<Self> {
        let mut buffer = [0; 160];
        match ascii(text, &mut buffer) {
            Some(ID) => Ok(Self::Id),
            Some(CONTRACT) => Ok(Self::Contract),
            Some(r"^sha256:[0-9a-f]{64}$") => Ok(Self::Digest),
            Some(r"^authority-ref:[a-z0-9][a-z0-9._-]{7,127}$") => Ok(Self::AuthorityReference),
            Some(r"^activation:[a-z0-9][a-z0-9._-]{7,127}$") => Ok(Self::ActivationReference),
            Some(r"^(?:authority-ref|lease|handle):[a-z0-9][a-z0-9._-]{7,127}$") => {
                Ok(Self::OpaqueReference)
            }
            Some(r"^(working-tree|[0-9a-f]{40})$") => Ok(Self::Commit),
            Some(r"^[0-9a-f]{64}$") => Ok(Self::Tree),
            Some(SEMVER) => Ok(Self::Semver),
            Some(r"^[a-z0-9][a-z0-9._/-]*$") => Ok(Self::ArtifactRoot),
            Some(r"^profile_projections/[a-z0-9][a-z0-9._/-]*$") => Ok(Self::ProjectionRoot),
            _ => Err(unsupported()),
        }
    }

    fn accepts(self, text: &Text) -> bool {
        // Python's non-multiline $ also matches just before a terminal LF.
        let text = text.0.strip_suffix(&[10]).unwrap_or(&text.0);
        match self {
            Self::Id => canonical_id(text),
            Self::Contract => text
                .iter()
                .rposition(|point| *point == u32::from(b'.'))
                .is_some_and(|dot| {
                    canonical_id(&text[..dot])
                        && ascii_prefix(&text[dot + 1..], "v").is_some_and(nonzero_integer)
                }),
            Self::Digest => ascii_prefix(text, "sha256:").is_some_and(|text| hex(text, 64)),
            Self::AuthorityReference => {
                ascii_prefix(text, "authority-ref:").is_some_and(reference_body)
            }
            Self::ActivationReference => {
                ascii_prefix(text, "activation:").is_some_and(reference_body)
            }
            Self::OpaqueReference => ["authority-ref:", "lease:", "handle:"]
                .iter()
                .any(|prefix| ascii_prefix(text, prefix).is_some_and(reference_body)),
            Self::Commit => {
                ascii_prefix(text, "working-tree").is_some_and(|tail| tail.is_empty())
                    || hex(text, 40)
            }
            Self::Tree => hex(text, 64),
            Self::Semver => semver(text),
            Self::ArtifactRoot => artifact_root(text),
            Self::ProjectionRoot => {
                ascii_prefix(text, "profile_projections/").is_some_and(artifact_root)
            }
        }
    }
}

fn ascii_prefix<'a>(text: &'a [u32], prefix: &str) -> Option<&'a [u32]> {
    let (head, tail) = text.split_at_checked(prefix.len())?;
    head.iter()
        .copied()
        .eq(prefix.bytes().map(u32::from))
        .then_some(tail)
}

fn lower(point: u32) -> bool {
    (u32::from(b'a')..=u32::from(b'z')).contains(&point)
}

fn digit(point: u32) -> bool {
    (u32::from(b'0')..=u32::from(b'9')).contains(&point)
}

fn alphanumeric(point: u32) -> bool {
    lower(point) || digit(point)
}

fn separators(point: u32) -> bool {
    point == u32::from(b'.') || point == u32::from(b'_') || point == u32::from(b'-')
}

fn canonical_id(text: &[u32]) -> bool {
    text.first().is_some_and(|point| lower(*point))
        && text
            .split(|point| separators(*point))
            .all(|section| !section.is_empty() && section.iter().copied().all(alphanumeric))
}

fn hex(text: &[u32], len: usize) -> bool {
    text.len() == len
        && text
            .iter()
            .copied()
            .all(|point| digit(point) || (u32::from(b'a')..=u32::from(b'f')).contains(&point))
}

fn reference_body(text: &[u32]) -> bool {
    (8..=128).contains(&text.len())
        && text.first().is_some_and(|point| alphanumeric(*point))
        && text
            .iter()
            .copied()
            .all(|point| alphanumeric(point) || separators(point))
}

fn artifact_root(text: &[u32]) -> bool {
    text.first().is_some_and(|point| alphanumeric(*point))
        && text
            .iter()
            .copied()
            .all(|point| alphanumeric(point) || separators(point) || point == u32::from(b'/'))
}

fn nonzero_integer(text: &[u32]) -> bool {
    text.first().is_some_and(|point| *point != u32::from(b'0')) && text.iter().copied().all(digit)
}

fn semver_identifiers(text: &[u32]) -> bool {
    text.split(|point| *point == u32::from(b'.')).all(|part| {
        !part.is_empty()
            && part.iter().copied().all(|point| {
                alphanumeric(point)
                    || (u32::from(b'A')..=u32::from(b'Z')).contains(&point)
                    || point == u32::from(b'-')
            })
    })
}

fn semver(text: &[u32]) -> bool {
    let mut build = text.splitn(2, |point| *point == u32::from(b'+'));
    let core = build.next().expect("split includes a first part");
    if build
        .next()
        .is_some_and(|identifiers| !semver_identifiers(identifiers))
    {
        return false;
    }
    let mut prerelease = core.splitn(2, |point| *point == u32::from(b'-'));
    let core = prerelease.next().expect("split includes a first part");
    if prerelease
        .next()
        .is_some_and(|identifiers| !semver_identifiers(identifiers))
    {
        return false;
    }
    let mut fields = core.split(|point| *point == u32::from(b'.'));
    (0..3).all(|_| {
        fields
            .next()
            .is_some_and(|part| part == [u32::from(b'0')] || nonzero_integer(part))
    }) && fields.next().is_none()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{operation, with_source};
    use serde_json::{Value, json};

    const COMMON_BYTES: &[u8] =
        include_bytes!("../../../tobkiri_runtime/tobkiri_protocol/schemas/common_v1.schema.json");
    const PROJECTION_BYTES: &[u8] = include_bytes!(
        "../../../tobkiri_runtime/tobkiri_protocol/schemas/profile_content_projection_v1.schema.json"
    );
    const V4_BYTES: &[u8] =
        include_bytes!("../../../tobkiri_runtime/tobkiri_protocol/schemas/profile_v4.schema.json");
    const V5_BYTES: &[u8] =
        include_bytes!("../../../tobkiri_runtime/tobkiri_protocol/schemas/profile_v5.schema.json");
    const V4_PROFILE_BYTES: &[u8] = include_bytes!(
        "../../../tobkiri_runtime/tests/fixtures/profile_v4/pre-e853-activation.json"
    );
    const V5_PROFILE_BYTES: &[u8] = include_bytes!(
        "../../../tobkiri_runtime/ecosystem/defaultspack/v4/defaults.profile.v5.json"
    );

    #[tokio::test(flavor = "current_thread")]
    async fn applies_both_shipped_schemas_and_the_current_scope_conditions() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let common = json::decode_strict(COMMON_BYTES, &op).unwrap();
            let projection = json::decode_strict(PROJECTION_BYTES, &op).unwrap();
            let v4 = json::decode_strict(V4_BYTES, &op).unwrap();
            let v5 = json::decode_strict(V5_BYTES, &op).unwrap();
            let mut old: Value = serde_json::from_slice(V4_PROFILE_BYTES).unwrap();
            let mut current: Value = serde_json::from_slice(V5_PROFILE_BYTES).unwrap();
            current["requested_edges"]
                .as_array_mut()
                .unwrap()
                .truncate(1);
            old["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .truncate(1);
            let check = |schema, profile: &Value| {
                let profile =
                    json::decode_strict(&serde_json::to_vec(profile).unwrap(), &op).unwrap();
                matches(
                    &op,
                    budget,
                    &[schema, &common, &projection],
                    &profile,
                    profile.root,
                )
                .unwrap()
            };

            assert!(check(&v4, &old["profile"]));
            assert!(check(&v5, &current));
            assert!(!check(&v4, &current));
            assert!(!check(&v5, &old["profile"]));

            let scope = &current["requested_edges"][0]["requested_scope_template"];
            let mut invalid = current.clone();
            invalid["requested_edges"][0]["requested_scope_template"]["dimensions"] =
                json!({"Uppercase": ["member"]});
            assert!(!check(&v5, &invalid));
            invalid["requested_edges"][0]["requested_scope_template"]["dimensions"] =
                json!({"dimension": ["*"]});
            assert!(!check(&v5, &invalid));
            invalid["requested_edges"][0]["requested_scope_template"]["dimensions"] =
                json!({"dimension": ["member", "member"]});
            assert!(!check(&v5, &invalid));
            invalid["requested_edges"][0]["requested_scope_template"] = scope.clone();
            invalid["requested_edges"][0]["requested_scope_template"]["quotas"] =
                json!({"items": true});
            assert!(!check(&v5, &invalid));
            invalid["requested_edges"][0]["requested_scope_template"]["quotas"] =
                json!({"items": -1});
            assert!(!check(&v5, &invalid));
            invalid["requested_edges"][0]["requested_scope_template"]["quotas"] =
                json!({"items": 0});
            assert!(check(&v5, &invalid));

            invalid = current.clone();
            invalid["state"] = json!("resolved");
            invalid["catalog_revision"] = Value::Null;
            assert!(!check(&v5, &invalid));
            invalid = current.clone();
            invalid["mode"] = json!("headless");
            assert!(!check(&v5, &invalid));
            invalid["shell"] = Value::Null;
            assert!(check(&v5, &invalid));
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn resolves_projection_and_checks_reordered_object_uniqueness() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let common = json::decode_strict(COMMON_BYTES, &op).unwrap();
            let projection = json::decode_strict(PROJECTION_BYTES, &op).unwrap();
            let schema = json::decode_strict(
                br#"{
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "https://schemas.tobkiri.local/io.tobkiri/profile.v5.schema.json",
                    "type": "array",
                    "uniqueItems": true,
                    "items": {
                        "$ref": "https://schemas.tobkiri.local/io.tobkiri/profile-content-projection.v1.schema.json#/$defs/resolvedProjection",
                        "properties": {"kind": {"const": "profile_content"}}
                    }
                }"#,
                &op,
            )
            .unwrap();
            let one = r#"{
                "projection_id":"p","kind":"profile_content","artifact_root":"profile_projections/x",
                "content_digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000",
                "file_count":1
            }"#;
            let reordered = r#"{
                "file_count":1,
                "content_digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000",
                "artifact_root":"profile_projections/x","kind":"profile_content","projection_id":"p"
            }"#;
            let check = |input: &str| {
                let value = json::decode_strict(input.as_bytes(), &op).unwrap();
                matches(
                    &op,
                    budget,
                    &[&schema, &common, &projection],
                    &value,
                    value.root,
                )
                .unwrap()
            };
            assert!(check(&format!("[{one}]")));
            assert!(!check(&format!("[{one},{reordered}]")));
            assert!(check(&format!(
                "[{one},{}]",
                reordered.replace("\"p\"", "\"other\"")
            )));
            assert!(!check(&format!(
                "[{}]",
                one.replace("profile_content", "ui_projection")
            )));
            assert!(!check(&format!(
                "[{}]",
                one.replace("profile_projections/x", "other/x")
            )));
            assert!(!check(&format!(
                "[{}]",
                one.replace("\"file_count\":1", "\"file_count\":true")
            )));
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn rejects_unsupported_schemas_before_using_branches() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let common = json::decode_strict(COMMON_BYTES, &op).unwrap();
            let projection = json::decode_strict(PROJECTION_BYTES, &op).unwrap();
            let profile = json::decode_strict(b"true", &op).unwrap();
            for rule in [
                json!({"if": false, "then": {"format": "email"}}),
                json!({"$ref": "https://example.invalid/schema#/$defs/id"}),
                json!({"pattern": "^custom$"}),
                json!({"$ref": "#/$defs/loop", "$defs": {"loop": {"$ref": "#/$defs/loop"}}}),
            ] {
                let mut schema = rule;
                schema["$schema"] = json!(DIALECT);
                schema["$id"] = json!(PROFILE_V5);
                let schema =
                    json::decode_strict(&serde_json::to_vec(&schema).unwrap(), &op).unwrap();
                let error = matches(
                    &op,
                    budget,
                    &[&schema, &common, &projection],
                    &profile,
                    profile.root,
                )
                .unwrap_err();
                assert!(matches!(
                    error.cause,
                    Some(OriginalAuthorityCause::Unsupported(_))
                ));
            }
        });
    }

    #[test]
    fn accepts_only_the_terminal_lf_permitted_by_python_patterns() {
        for (value, expected) in [
            ("abc-v2.p", true),
            ("abc-v2.p\n", true),
            ("abc-v2.p\n\n", false),
            ("abc-v2.p\r\n", false),
            ("abc..p", false),
            ("abc_9", true),
            ("abcé", false),
        ] {
            assert_eq!(Pattern::Id.accepts(&Text::from_str(value)), expected);
        }
        for (value, expected) in [
            ("0.12.1", true),
            ("1.2.3-dev.00+TEST-x.2\n", true),
            ("01.2.3", false),
            ("1.2.3-x..z", false),
            ("1.2.3-x+z+", false),
            ("1.2.3-", false),
        ] {
            assert_eq!(Pattern::Semver.accepts(&Text::from_str(value)), expected);
        }
    }
}
