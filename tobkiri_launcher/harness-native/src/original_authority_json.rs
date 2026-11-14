//! The fixed old AuthorityStore's decoded-DATA boundary, not a general JSON API.
//! CPython 3.13.16 default JSON syntax/strings; existing canon scalar numbers.
//! Nodes/frames are flat so valid structural depth is not a Rust call-stack
//! limit. No Python threadstate or invented RecursionError is imported here.
use super::*;
use tobkiri_canon::{Variant, encode, parse};

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct Text(pub Vec<u32>);
impl Text {
    pub fn from_str(value: &str) -> Self {
        Self(value.chars().map(u32::from).collect())
    }
    pub fn is(&self, value: &str) -> bool {
        self.0.iter().copied().eq(value.chars().map(u32::from))
    }
    pub fn utf8(&self) -> Result<String> {
        self.0
            .iter()
            .map(|value| {
                char::from_u32(*value).ok_or_else(|| {
                    OriginalAuthorityError::unsupported(
                        OriginalAuthorityStage::Migration,
                        "uncanonicalized Authority string",
                    )
                })
            })
            .collect()
    }
}
pub(super) enum Node {
    Scalar(CanonValue), // Only null/bool/integer/float, never recursive values.
    String(Text),
    Array(Vec<usize>),
    Object(Vec<(Text, usize)>), // First-key position survives replacement.
}
pub(super) struct Document {
    pub nodes: Vec<Node>,
    pub root: usize,
    bytes: usize,
    allocation_bound: usize,
}
impl Document {
    pub fn storage_bound(&self) -> usize {
        self.allocation_bound
    }
    pub fn actual_storage_bytes(&self) -> usize {
        let mut total = self.nodes.capacity() * std::mem::size_of::<Node>();
        for node in &self.nodes {
            total += match node {
                Node::String(text) => text.0.capacity() * std::mem::size_of::<u32>(),
                Node::Array(items) => items.capacity() * std::mem::size_of::<usize>(),
                Node::Object(items) => {
                    items.capacity() * std::mem::size_of::<(Text, usize)>()
                        + items
                            .iter()
                            .map(|(key, _)| key.0.capacity() * std::mem::size_of::<u32>())
                            .sum::<usize>()
                }
                Node::Scalar(CanonValue::BigInt(value)) => value.capacity(),
                _ => 0,
            };
        }
        total
    }
    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }
    pub fn source_bytes(&self) -> usize {
        self.bytes
    }
    pub fn node(&self, id: usize) -> &Node {
        &self.nodes[id]
    }
    pub fn entries(&self, id: usize) -> Option<&[(Text, usize)]> {
        if let Node::Object(items) = self.node(id) {
            Some(items)
        } else {
            None
        }
    }
    pub fn get(&self, id: usize, key: &str) -> Option<usize> {
        self.entries(id)?
            .iter()
            .find(|(name, _)| name.is(key))
            .map(|(_, value)| *value)
    }
    pub fn string_is(&self, id: usize, expected: &str) -> bool {
        matches!(self.node(id), Node::String(text) if text.is(expected))
    }
    pub fn insert_string(&mut self, key: &str, value: &str) {
        let id = self.nodes.len();
        self.nodes.push(Node::String(Text::from_str(value)));
        let Node::Object(items) = &mut self.nodes[self.root] else {
            unreachable!("decoded object");
        };
        if let Some((_, old)) = items.iter_mut().find(|(name, _)| name.is(key)) {
            *old = id;
        } else {
            items.push((Text::from_str(key), id));
        }
    }
}

enum State {
    ArrayValue(bool),
    ArrayAfter,
    ObjectKey(bool),
    ObjectValue(Text),
    ObjectAfter,
}
struct Frame {
    id: usize,
    state: State,
}
struct Decoder<'a, 'op, 'inv, S> {
    source: &'a str,
    at: usize,
    doc: Document,
    frames: Vec<Frame>,
    ticks: usize,
    strict: bool,
    operation: &'op Operation<'inv, S>,
    catalog_work: Option<std::sync::Arc<dyn tobkiri_canon::AllocationBudget>>,
}
pub(super) fn decode<S: OriginalAuthorityOwner>(
    input: &[u8],
    op: &Operation<'_, S>,
) -> Result<Funded<Document>> {
    decode_inner(input, op, false, None)
}

/// Publication uses strict_loads; legacy Authority JSON keeps its overwrite rule.
pub(super) fn decode_strict<S: OriginalAuthorityOwner>(
    input: &[u8],
    op: &Operation<'_, S>,
) -> Result<Funded<Document>> {
    if input.len() > tobkiri_canon::MAX_BYTES {
        return Err(strict_error("json input exceeds size limit"));
    }
    let doc = decode_inner(input, op, true, None).map_err(strict_parse_error)?;
    validate_strict(&doc, op)?;
    Ok(doc)
}

/// Reuse the metered non-strict decoder after the actual catalog json.loads.
pub(super) fn decode_catalog_manifest<S: OriginalAuthorityOwner>(
    input: &[u8],
    op: &Operation<'_, S>,
    budget: &std::sync::Arc<dyn tobkiri_canon::AllocationBudget>,
) -> Result<Funded<Document>> {
    decode_metered_legacy(input, op, budget)
}

/// A handover journal uses json.loads before canonicalizing its signed payload.
#[cfg(unix)]
pub(super) fn decode_handover_journal<S: OriginalAuthorityOwner>(
    input: &[u8],
    op: &Operation<'_, S>,
    budget: &std::sync::Arc<dyn tobkiri_canon::AllocationBudget>,
) -> Result<Funded<Document>> {
    let input = input.strip_prefix(b"\xef\xbb\xbf").unwrap_or(input);
    if matches!(input.first(), Some(0 | 0xfe | 0xff)) || input.get(1).is_some_and(|byte| *byte == 0)
    {
        return Err(OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Audit,
            "original handover journal byte encoding",
        ));
    }
    decode_metered_legacy(input, op, budget).map_err(|mut error| {
        if let Some(OriginalAuthorityCause::Python { class, .. }) = error.cause.as_ref()
            && matches!(
                *class,
                "json.decoder.JSONDecodeError" | "builtins.UnicodeDecodeError"
            )
            && let Some(OriginalAuthorityCause::Python {
                class,
                message,
                cause,
                context,
                suppressed,
            }) = error.cause.take()
        {
            let mut original = raw_error(class, message);
            original.cause = cause.map(|cause| *cause);
            original.suppressed = suppressed;
            if let Some(context) = context
                && let OriginalAuthorityCause::Python { class, message, .. } = *context
            {
                original.context = Some(Box::new(raw_error(class, message)));
            }
            original
        } else {
            error
        }
    })
}

fn decode_metered_legacy<S: OriginalAuthorityOwner>(
    input: &[u8],
    op: &Operation<'_, S>,
    budget: &std::sync::Arc<dyn tobkiri_canon::AllocationBudget>,
) -> Result<Funded<Document>> {
    budget
        .reserve(tobkiri_canon::AllocationRequest {
            kind: tobkiri_canon::AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: (input.len() as u64)
                .checked_mul(8)
                .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?,
        })
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    let bound = catalog_allocation_bound(input)
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    decode_inner(input, op, false, Some((bound, budget.clone())))
}

/// The fixed catalog resource index keeps the OLD strict allocation bound;
/// only its otherwise quadratic duplicate checks gain the same actual meter.
pub(super) fn decode_catalog_lock<S: OriginalAuthorityOwner>(
    input: &[u8],
    op: &Operation<'_, S>,
    budget: &std::sync::Arc<dyn tobkiri_canon::AllocationBudget>,
) -> Result<Funded<Document>> {
    decode_metered_strict(input, op, budget)
}

/// Only the fixed registry constructor/first-list caller uses this bound.
/// Unlike the catalog manifest it has no prior Guest parse: the census bounds
/// every reached allocation, including invalid/unfinished JSON prefixes. It
/// never validates, skips a value or changes the strict decoder's error order.
pub(super) fn decode_profile_definitions<S: OriginalAuthorityOwner>(
    input: &[u8],
    op: &Operation<'_, S>,
    budget: &std::sync::Arc<dyn tobkiri_canon::AllocationBudget>,
) -> Result<Funded<Document>> {
    if input.len() > tobkiri_canon::MAX_BYTES {
        return Err(strict_error("json input exceeds size limit"));
    }
    budget
        .reserve(tobkiri_canon::AllocationRequest {
            kind: tobkiri_canon::AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: input.len() as u64 * 8,
        })
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    let bound = definition_allocation_bound(input)
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    let mut doc =
        decode_inner(input, op, true, Some((bound, budget.clone()))).map_err(strict_parse_error)?;
    validate_strict(&doc, op)?;
    // Registry validation uses small temporary metadata nodes and may infer
    // bootstrap state. Keep enough funded space for either growth, but release
    // the parser frames, duplicate diagnostics and numeric scratch provision
    // before canonical hashing and later authenticated reads.
    let arena_growth = doc
        .nodes
        .capacity()
        .checked_mul(2)
        .and_then(|nodes| nodes.checked_mul(std::mem::size_of::<Node>()))
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    let retained = doc
        .actual_storage_bytes()
        .checked_add(arena_growth)
        .and_then(|bytes| bytes.checked_add(8192))
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?
        .min(bound);
    doc._capacity
        .resize(retained as u64)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    doc.allocation_bound = retained;
    Ok(doc)
}

fn decode_metered_strict<S: OriginalAuthorityOwner>(
    input: &[u8],
    op: &Operation<'_, S>,
    budget: &std::sync::Arc<dyn tobkiri_canon::AllocationBudget>,
) -> Result<Funded<Document>> {
    if input.len() > tobkiri_canon::MAX_BYTES {
        return Err(strict_error("json input exceeds size limit"));
    }
    budget
        .reserve(tobkiri_canon::AllocationRequest {
            kind: tobkiri_canon::AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: input.len() as u64 * 8,
        })
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    let bound = input
        .len()
        .checked_add(1024)
        .and_then(|n| n.checked_mul(legacy_allocation_unit()))
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    let doc =
        decode_inner(input, op, true, Some((bound, budget.clone()))).map_err(strict_parse_error)?;
    validate_strict(&doc, op)?;
    Ok(doc)
}

fn catalog_key_equal(
    budget: &std::sync::Arc<dyn tobkiri_canon::AllocationBudget>,
    left: &Text,
    right: &Text,
) -> Result<bool> {
    // Prepay each reached comparison (including the length test), and the
    // largest u32 comparison extent BEFORE invoking Vec equality. No n^2
    // reservation or second work counter: actual loops debit this same Store.
    let work = 1_u64
        .checked_add(if left.0.len() == right.0.len() {
            left.0.len() as u64
        } else {
            0
        })
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    budget
        .reserve(tobkiri_canon::AllocationRequest {
            kind: tobkiri_canon::AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work,
        })
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    Ok(left == right)
}

// A lexical density scan, not a parser or validation fallback. Count ALL
// quoted strings, including keys/duplicates, and ALL scalar/container starts.
// The total bounds unreachable duplicate values too. Raw string bytes bound
// decoded u32 points (including every escape/surrogate); numeric bytes cover
// the existing scalar decoder's token/BigInt temporary copies.
fn catalog_allocation_bound(input: &[u8]) -> Option<usize> {
    let (mut atoms, mut containers, mut strings, mut text, mut numeric) =
        (0_usize, 0_usize, 0_usize, 0_usize, 0_usize);
    let (mut quoted, mut escaped, mut scalar) = (false, false, false);
    for &byte in input {
        if quoted {
            if !escaped && byte == b'"' {
                quoted = false;
            } else {
                text = text.checked_add(1)?;
                if escaped {
                    escaped = false;
                } else if byte == b'\\' {
                    escaped = true;
                }
            }
        } else {
            match byte {
                b'"' => {
                    strings = strings.checked_add(1)?;
                    atoms = atoms.checked_add(1)?;
                    quoted = true;
                    scalar = false;
                }
                b'{' | b'[' => {
                    containers = containers.checked_add(1)?;
                    atoms = atoms.checked_add(1)?;
                    scalar = false;
                }
                b'}' | b']' | b':' | b',' | b' ' | b'\t' | b'\r' | b'\n' => scalar = false,
                _ => {
                    numeric = numeric.checked_add(1)?;
                    if !scalar {
                        atoms = atoms.checked_add(1)?;
                        scalar = true;
                    }
                }
            }
        }
    }
    if quoted || escaped || atoms == 0 {
        return None;
    }
    // Vec<T> here has nonzero T, min capacity 4 and doubling growth. For one
    // arena/stack old+new capacity <= 3*n+4. Across per-container edge Vecs,
    // retained + one growing old allocation <= 3*edges+4*containers. Across
    // all Text Vec<u32>, <= 3*points+4*quoted-strings. Keys count as atoms too,
    // so atoms+strings safely bounds every array edge/object pair together.
    let arena = atoms
        .checked_mul(3)?
        .checked_add(4)?
        .checked_mul(std::mem::size_of::<Node>())?;
    let frames = containers
        .checked_mul(3)?
        .checked_add(4)?
        .checked_mul(std::mem::size_of::<Frame>())?;
    let edges = atoms
        .checked_add(strings)?
        .checked_mul(3)?
        .checked_add(containers.checked_mul(4)?)?
        .checked_mul(std::mem::size_of::<(Text, usize)>())?;
    let points = text
        .checked_mul(3)?
        .checked_add(strings.checked_mul(4)?)?
        .checked_mul(std::mem::size_of::<u32>())?;
    // Reuse the ORIGINAL scalar per-byte provision unchanged, including std
    // numeric conversion scratch; no new small float/BigInt multiplier is
    // assumed by this slice. Only strings/structure receive a density bound.
    arena
        .checked_add(frames)?
        .checked_add(edges)?
        .checked_add(points)?
        .checked_add(numeric.checked_mul(legacy_allocation_unit())?)?
        .checked_add(8192)
}

fn definition_allocation_bound(input: &[u8]) -> Option<usize> {
    let (mut atoms, mut containers, mut strings, mut text, mut numeric) =
        (0_usize, 0_usize, 0_usize, 0_usize, 0_usize);
    let (mut quoted, mut escaped, mut scalar) = (false, false, false);
    let mut longest_text = 0_usize;
    let mut current_text = 0_usize;
    for &byte in input {
        if quoted {
            if !escaped && byte == b'"' {
                quoted = false;
            } else {
                text = text.checked_add(1)?;
                current_text = current_text.checked_add(1)?;
                longest_text = longest_text.max(current_text);
                if escaped {
                    escaped = false;
                } else if byte == b'\\' {
                    escaped = true;
                }
            }
        } else {
            match byte {
                b'"' => {
                    strings = strings.checked_add(1)?;
                    atoms = atoms.checked_add(1)?;
                    quoted = true;
                    current_text = 0;
                    scalar = false;
                }
                b'{' | b'[' => {
                    containers = containers.checked_add(1)?;
                    atoms = atoms.checked_add(1)?;
                    scalar = false;
                }
                b'}' | b']' | b':' | b',' | b' ' | b'\t' | b'\r' | b'\n' => scalar = false,
                _ => {
                    numeric = numeric.checked_add(1)?;
                    if !scalar {
                        atoms = atoms.checked_add(1)?;
                        scalar = true;
                    }
                }
            }
        }
    }
    // No completion/syntax check here. A final partial Text is live in text()
    // even though it never becomes a Node, and an ObjectValue frame can own a
    // key before its value parses. Count every quoted key/value and raw byte,
    // including escaped quotes, invalid escapes and lone surrogate spellings.
    // Each decoded u32 consumes >=1 of these bytes. Every reached Node/frame
    // and edge likewise consumes a counted atom/container/string start.
    //
    // Post-decode registry edits are also prepaid: missing bootstrap adds
    // three nodes/one object/three pairs; tombstone hashing temporarily adds
    // two nodes/one object/three pairs. set_digest adds one 71-point Text.
    // First-list fallback adds at most one object/pair/"profile_id" Text per
    // ORIGINAL node (using atoms avoids any early schema-dependent discount).
    let fallback = atoms;
    let nodes = atoms.checked_add(fallback)?.checked_add(5)?;
    let objects = containers.checked_add(fallback)?.checked_add(2)?;
    let edges = atoms
        .checked_add(strings)?
        .checked_add(fallback)?
        .checked_add(6)?;
    let texts = strings.checked_add(fallback)?.checked_add(9)?;
    let fixed_points = "not_required".len()
        + "state".len()
        + "template_profile_revision".len()
        + "bootstrap".len()
        + "sha256:".len()
        + 64
        + "io.tobkiri.profile-tombstone.v1".len()
        + "schema".len()
        + "profile_id".len()
        + "parent_revision".len();
    let points = text
        .checked_add(fallback.checked_mul("profile_id".len())?)?
        .checked_add(fixed_points)?;
    // All these non-byte Vecs have minimum capacity four and geometric
    // doubling. 3*n+4 covers old+new allocation at a growth; summing per-object
    // and per-Text minima gives 4*objects/4*texts. Source key Text moves from
    // the temporary/frame to its pair, never clones. Arrays use the larger
    // object-pair element size here. Frames never exceed source containers.
    let arena = nodes
        .checked_mul(3)?
        .checked_add(4)?
        .checked_mul(std::mem::size_of::<Node>())?;
    let frames = containers
        .checked_mul(3)?
        .checked_add(4)?
        .checked_mul(std::mem::size_of::<Frame>())?;
    let pairs = edges
        .checked_mul(3)?
        .checked_add(objects.checked_mul(4)?)?
        .checked_mul(std::mem::size_of::<(Text, usize)>())?;
    let points = points
        .checked_mul(3)?
        .checked_add(texts.checked_mul(4)?)?
        .checked_mul(std::mem::size_of::<u32>())?;
    // Only one duplicate-key error can coexist with the arena. Bound its
    // repr by the longest reached quoted token, including an unfinished one.
    // repr_text emits at most 10 bytes/u32 plus quotes. Its retained String
    // uses <=2*r+8 bytes; format!'s old+new String uses <=3*(r+prefix)+8. Their simultaneous bound
    // is 5*r+3*prefix+16, including diagnostic growth, without reading args or
    // changing the original duplicate-before-strict-scalar validation order.
    let repr_bytes = longest_text.checked_mul(10)?.checked_add(2)?;
    let duplicate_scratch = repr_bytes
        .checked_mul(5)?
        .checked_add("duplicate JSON object key: ".len().checked_mul(3)?)?
        .checked_add(16)?;
    arena
        .checked_add(frames)?
        .checked_add(pairs)?
        .checked_add(points)?
        .checked_add(duplicate_scratch)?
        // Scalar conversion keeps the ORIGINAL per-byte numeric provision;
        // the old fixed 1024 units are retained for fixed scratch/insertions.
        .checked_add(
            numeric
                .checked_add(1024)?
                .checked_mul(legacy_allocation_unit())?,
        )
}

const fn legacy_allocation_unit() -> usize {
    std::mem::size_of::<Node>() * 3
        + std::mem::size_of::<Frame>() * 3
        + std::mem::size_of::<(Text, usize)>() * 6
        + std::mem::size_of::<usize>() * 6
        + std::mem::size_of::<u32>() * 6
        + 24
}

#[cfg(test)]
#[path = "original_catalog_json_tests.rs"]
mod catalog_funding_tests;

#[cfg(test)]
#[path = "original_profile_definitions_json_tests.rs"]
mod definitions_funding_tests;

fn decode_inner<S: OriginalAuthorityOwner>(
    input: &[u8],
    op: &Operation<'_, S>,
    strict: bool,
    catalog_bound: Option<(usize, std::sync::Arc<dyn tobkiri_canon::AllocationBudget>)>,
) -> Result<Funded<Document>> {
    let source = std::str::from_utf8(input).map_err(|error| utf8_error(input, error))?;
    if source.starts_with('\u{feff}') {
        return Err(syntax(
            source,
            0,
            "Unexpected UTF-8 BOM (decode using utf-8-sig)",
            false,
        ));
    }
    // Every node, edge, frame and codepoint consumes at least one input byte.
    // Vec's minimum nonzero capacity is four for these element sizes. Six
    // slots per input unit covers that minimum and old+new allocations during
    // geometric growth. Arena/frame storage has one aggregate minimum, not
    // one per node. Numeric conversion's token/BigInt temporaries have their
    // own byte allowance. The fixed extra units cover the three migration
    // insertions (request/activation/plan), never arbitrary caller updates.
    let unit = legacy_allocation_unit();
    let (bound, catalog_work) = match catalog_bound {
        Some((bound, work)) => (bound, Some(work)),
        None => (
            input
                .len()
                .checked_add(1024)
                .and_then(|n| n.checked_mul(unit))
                .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?,
            None,
        ),
    };
    let capacity = op.reserve(OriginalAuthorityStage::Audit, bound)?;
    let mut decoder = Decoder {
        source,
        at: 0,
        doc: Document {
            nodes: Vec::new(),
            root: 0,
            bytes: input.len(),
            allocation_bound: bound,
        },
        frames: Vec::new(),
        ticks: 0,
        strict,
        operation: op,
        catalog_work,
    };
    decoder.space();
    let (root, frame) = decoder.start()?;
    decoder.doc.root = root;
    if let Some(frame) = frame {
        decoder.frames.push(frame);
    }
    while let Some(Frame { id, state }) = decoder.frames.pop() {
        decoder.tick()?;
        match state {
            State::ArrayValue(first) => {
                decoder.space();
                if first && decoder.byte() == Some(b']') {
                    decoder.at += 1;
                    continue;
                }
                let (child, frame) = decoder.start()?;
                let Node::Array(items) = &mut decoder.doc.nodes[id] else {
                    unreachable!("array frame");
                };
                items.push(child);
                decoder.frames.push(Frame {
                    id,
                    state: State::ArrayAfter,
                });
                if let Some(frame) = frame {
                    decoder.frames.push(frame);
                }
            }
            State::ArrayAfter => {
                decoder.space();
                if decoder.byte() == Some(b']') {
                    decoder.at += 1;
                    continue;
                }
                if decoder.byte() != Some(b',') {
                    return Err(decoder.error("Expecting ',' delimiter", false));
                }
                let comma = decoder.at;
                decoder.at += 1;
                decoder.space();
                if decoder.byte() == Some(b']') {
                    return Err(syntax(
                        source,
                        comma,
                        "Illegal trailing comma before end of array",
                        false,
                    ));
                }
                decoder.frames.push(Frame {
                    id,
                    state: State::ArrayValue(false),
                });
            }
            State::ObjectKey(first) => {
                decoder.space();
                if first && decoder.byte() == Some(b'}') {
                    decoder.finish_object(id)?;
                    decoder.at += 1;
                    continue;
                }
                if decoder.byte() != Some(b'"') {
                    return Err(
                        decoder.error("Expecting property name enclosed in double quotes", false)
                    );
                }
                let name = decoder.text()?;
                decoder.space();
                if decoder.byte() != Some(b':') {
                    return Err(decoder.error("Expecting ':' delimiter", false));
                }
                decoder.at += 1;
                decoder.space();
                decoder.frames.push(Frame {
                    id,
                    state: State::ObjectValue(name),
                });
            }
            State::ObjectValue(name) => {
                let (child, frame) = decoder.start()?;
                let Node::Object(items) = &mut decoder.doc.nodes[id] else {
                    unreachable!("object frame");
                };
                if strict {
                    items.push((name, child));
                } else if let Some(work) = &decoder.catalog_work {
                    let mut found = false;
                    for (key, old) in items.iter_mut() {
                        if catalog_key_equal(work, key, &name)? {
                            *old = child;
                            found = true;
                            break;
                        }
                    }
                    if !found {
                        items.push((name, child));
                    }
                } else if let Some((_, old)) = items.iter_mut().find(|(key, _)| *key == name) {
                    *old = child;
                } else {
                    items.push((name, child));
                }
                decoder.frames.push(Frame {
                    id,
                    state: State::ObjectAfter,
                });
                if let Some(frame) = frame {
                    decoder.frames.push(frame);
                }
            }
            State::ObjectAfter => {
                decoder.space();
                if decoder.byte() == Some(b'}') {
                    decoder.finish_object(id)?;
                    decoder.at += 1;
                    continue;
                }
                if decoder.byte() != Some(b',') {
                    return Err(decoder.error("Expecting ',' delimiter", false));
                }
                let comma = decoder.at;
                decoder.at += 1;
                decoder.space();
                if decoder.byte() == Some(b'}') {
                    return Err(syntax(
                        source,
                        comma,
                        "Illegal trailing comma before end of object",
                        false,
                    ));
                }
                decoder.frames.push(Frame {
                    id,
                    state: State::ObjectKey(false),
                });
            }
        }
    }
    decoder.space();
    if decoder.at != source.len() {
        return Err(decoder.error("Extra data", false));
    }
    Ok(Funded {
        value: decoder.doc,
        _capacity: capacity,
    })
}
impl<S: OriginalAuthorityOwner> Decoder<'_, '_, '_, S> {
    fn finish_object(&self, id: usize) -> Result<()> {
        if self.strict {
            let pairs = self.doc.entries(id).expect("object frame");
            for (index, (key, _)) in pairs.iter().enumerate() {
                self.operation.check(OriginalAuthorityStage::Audit)?;
                let duplicate = if let Some(work) = &self.catalog_work {
                    let mut found = false;
                    for (previous, _) in &pairs[..index] {
                        if catalog_key_equal(work, previous, key)? {
                            found = true;
                            break;
                        }
                    }
                    found
                } else {
                    pairs[..index].iter().any(|(previous, _)| previous == key)
                };
                if duplicate {
                    return Err(strict_error(format!(
                        "duplicate JSON object key: {}",
                        repr_text(key)
                    )));
                }
            }
        }
        Ok(())
    }
    fn byte(&self) -> Option<u8> {
        self.source.as_bytes().get(self.at).copied()
    }
    fn space(&mut self) {
        while self.byte().is_some_and(|value| b" \t\n\r".contains(&value)) {
            self.at += 1;
        }
    }
    fn error(&self, message: &'static str, stop: bool) -> OriginalAuthorityError {
        syntax(self.source, self.at, message, stop)
    }
    fn tick(&mut self) -> Result<()> {
        self.ticks += 1;
        if self.ticks.is_multiple_of(1024) {
            self.operation.check(OriginalAuthorityStage::Audit)?;
        }
        Ok(())
    }
    fn push(&mut self, node: Node) -> usize {
        let id = self.doc.nodes.len();
        self.doc.nodes.push(node);
        id
    }
    fn start(&mut self) -> Result<(usize, Option<Frame>)> {
        self.tick()?;
        match self.byte() {
            Some(b'{') => {
                self.at += 1;
                let id = self.push(Node::Object(Vec::new()));
                Ok((
                    id,
                    Some(Frame {
                        id,
                        state: State::ObjectKey(true),
                    }),
                ))
            }
            Some(b'[') => {
                self.at += 1;
                let id = self.push(Node::Array(Vec::new()));
                Ok((
                    id,
                    Some(Frame {
                        id,
                        state: State::ArrayValue(true),
                    }),
                ))
            }
            Some(b'"') => {
                let text = self.text()?;
                let id = self.push(Node::String(text));
                Ok((id, None))
            }
            Some(b'n') if self.source[self.at..].starts_with("null") => {
                self.at += 4;
                let id = self.push(Node::Scalar(CanonValue::Null));
                Ok((id, None))
            }
            Some(b't') if self.source[self.at..].starts_with("true") => {
                self.at += 4;
                let id = self.push(Node::Scalar(CanonValue::Bool(true)));
                Ok((id, None))
            }
            Some(b'f') if self.source[self.at..].starts_with("false") => {
                self.at += 5;
                let id = self.push(Node::Scalar(CanonValue::Bool(false)));
                Ok((id, None))
            }
            Some(b'N') if self.source[self.at..].starts_with("NaN") => {
                if self.strict {
                    return Err(strict_error("non-finite JSON number: NaN"));
                }
                self.at += 3;
                let id = self.push(Node::Scalar(CanonValue::Float(f64::NAN)));
                Ok((id, None))
            }
            Some(b'I') if self.source[self.at..].starts_with("Infinity") => {
                if self.strict {
                    return Err(strict_error("non-finite JSON number: Infinity"));
                }
                self.at += 8;
                let id = self.push(Node::Scalar(CanonValue::Float(f64::INFINITY)));
                Ok((id, None))
            }
            Some(b'-') if self.source[self.at..].starts_with("-Infinity") => {
                if self.strict {
                    return Err(strict_error("non-finite JSON number: -Infinity"));
                }
                self.at += 9;
                let id = self.push(Node::Scalar(CanonValue::Float(f64::NEG_INFINITY)));
                Ok((id, None))
            }
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(self.error("Expecting value", true)),
        }
    }
    fn number(&mut self) -> Result<(usize, Option<Frame>)> {
        let start = self.at;
        if self.byte() == Some(b'-') {
            self.at += 1;
        }
        match self.byte() {
            Some(b'0') => self.at += 1,
            Some(b'1'..=b'9') => {
                while self.byte().is_some_and(|value| value.is_ascii_digit()) {
                    self.at += 1;
                }
            }
            _ => {
                self.at = start;
                return Err(self.error("Expecting value", true));
            }
        }
        let integer_end = self.at;
        let mut floating = false;
        if self.byte() == Some(b'.')
            && self
                .source
                .as_bytes()
                .get(self.at + 1)
                .is_some_and(u8::is_ascii_digit)
        {
            floating = true;
            self.at += 1;
            while self.byte().is_some_and(|value| value.is_ascii_digit()) {
                self.at += 1;
            }
        }
        if self.byte().is_some_and(|value| b"eE".contains(&value)) {
            let exponent = self.at;
            self.at += 1;
            if self.byte().is_some_and(|value| b"+-".contains(&value)) {
                self.at += 1;
            }
            let first = self.at;
            while self.byte().is_some_and(|value| value.is_ascii_digit()) {
                self.at += 1;
            }
            if first == self.at {
                self.at = exponent;
            } else {
                floating = true;
            }
        }
        let digits = integer_end - start - usize::from(self.source.as_bytes()[start] == b'-');
        if !floating && digits > 4300 {
            return Err(raw_error(
                "builtins.ValueError",
                format!(
                    "Exceeds the limit (4300 digits) for integer string conversion: value has {digits} digits; use sys.set_int_max_str_digits() to increase the limit"
                ),
            ));
        }
        // The source grammar and integer boundary are already selected. This
        // is scalar conversion, never a parser-failure fallback/retry.
        let value = parse(
            &self.source.as_bytes()[start..self.at],
            Variant::LegacyFloat,
        )
        .map_err(|_| {
            OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Audit,
                "Authority JSON numeric scalar conversion",
            )
        })?;
        let id = self.push(Node::Scalar(value));
        Ok((id, None))
    }
    fn hex(&self, start: usize, u_at: usize) -> Result<u32> {
        let Some(bytes) = self.source.as_bytes().get(start..start + 4) else {
            return Err(syntax(self.source, u_at, "Invalid \\uXXXX escape", false));
        };
        let mut value = 0;
        for byte in bytes {
            value = value * 16
                + match byte {
                    b'0'..=b'9' => u32::from(byte - b'0'),
                    b'a'..=b'f' => u32::from(byte - b'a' + 10),
                    b'A'..=b'F' => u32::from(byte - b'A' + 10),
                    _ => return Err(syntax(self.source, u_at, "Invalid \\uXXXX escape", false)),
                };
        }
        Ok(value)
    }
    fn text(&mut self) -> Result<Text> {
        let begin = self.at;
        self.at += 1;
        let mut output = Vec::new();
        loop {
            self.tick()?;
            let Some(byte) = self.byte() else {
                return Err(syntax(
                    self.source,
                    begin,
                    "Unterminated string starting at",
                    false,
                ));
            };
            if byte == b'"' {
                self.at += 1;
                return Ok(Text(output));
            }
            if byte < 32 {
                return Err(self.error("Invalid control character at", false));
            }
            if byte != b'\\' {
                let ch = self.source[self.at..]
                    .chars()
                    .next()
                    .expect("validated UTF8");
                output.push(u32::from(ch));
                self.at += ch.len_utf8();
                continue;
            }
            let slash = self.at;
            self.at += 1;
            let Some(escaped) = self.byte() else {
                return Err(syntax(
                    self.source,
                    begin,
                    "Unterminated string starting at",
                    false,
                ));
            };
            let value = match escaped {
                b'"' | b'\\' | b'/' => {
                    self.at += 1;
                    u32::from(escaped)
                }
                b'b' => {
                    self.at += 1;
                    8
                }
                b'f' => {
                    self.at += 1;
                    12
                }
                b'n' => {
                    self.at += 1;
                    10
                }
                b'r' => {
                    self.at += 1;
                    13
                }
                b't' => {
                    self.at += 1;
                    9
                }
                b'u' => {
                    let first = self.hex(self.at + 1, self.at)?;
                    self.at += 5;
                    if (0xd800..=0xdbff).contains(&first)
                        && self.source.as_bytes().get(self.at..self.at + 2) == Some(b"\\u")
                    {
                        let second = self.hex(self.at + 2, self.at + 1)?;
                        if (0xdc00..=0xdfff).contains(&second) {
                            self.at += 6;
                            0x10000 + ((first - 0xd800) << 10) + second - 0xdc00
                        } else {
                            first
                        }
                    } else {
                        first
                    }
                }
                _ => return Err(syntax(self.source, slash, "Invalid \\escape", false)),
            };
            output.push(value);
        }
    }
}

fn syntax(source: &str, byte: usize, message: &'static str, stop: bool) -> OriginalAuthorityError {
    let prefix = &source[..byte];
    let position = prefix.chars().count();
    let line = prefix.bytes().filter(|value| *value == b'\n').count() + 1;
    let column = prefix
        .chars()
        .rev()
        .take_while(|value| *value != '\n')
        .count()
        + 1;
    OriginalAuthorityError::new(
        OriginalAuthorityStage::Audit,
        "authority record authentication failed",
        Some(OriginalAuthorityCause::Python {
            class: "json.decoder.JSONDecodeError",
            message: format!("{message}: line {line} column {column} (char {position})"),
            cause: None,
            context: stop
                .then(|| Box::new(python_error("builtins.StopIteration", position.to_string()))),
            suppressed: stop,
        }),
    )
}
fn utf8_error(input: &[u8], error: std::str::Utf8Error) -> OriginalAuthorityError {
    let start = error.valid_up_to();
    let length = error.error_len().unwrap_or(input.len() - start);
    let reason = if error.error_len().is_none() {
        "unexpected end of data"
    } else if (0xc2..=0xf4).contains(&input[start]) {
        "invalid continuation byte"
    } else {
        "invalid start byte"
    };
    let message = if length == 1 {
        format!(
            "'utf-8' codec can't decode byte 0x{:02x} in position {start}: {reason}",
            input[start]
        )
    } else {
        format!(
            "'utf-8' codec can't decode bytes in position {start}-{}: {reason}",
            start + length - 1
        )
    };
    OriginalAuthorityError::new(
        OriginalAuthorityStage::Audit,
        "authority record authentication failed",
        Some(python_error("builtins.UnicodeDecodeError", message)),
    )
}
pub(super) fn raw_error(class: &'static str, message: impl Into<String>) -> OriginalAuthorityError {
    OriginalAuthorityError {
        stage: OriginalAuthorityStage::Audit,
        class,
        message: message.into(),
        cause: None,
        context: None,
        suppressed: false,
    }
}

pub(super) fn strict_error(message: impl Into<String>) -> OriginalAuthorityError {
    raw_error("tobkiri_protocol.errors.CanonicalizationError", message)
}

fn strict_parse_error(mut error: OriginalAuthorityError) -> OriginalAuthorityError {
    let Some(OriginalAuthorityCause::Python { class, message, .. }) = error.cause.as_ref() else {
        return error;
    };
    let text = match *class {
        "builtins.UnicodeDecodeError" => "json input is not valid UTF-8".into(),
        "json.decoder.JSONDecodeError" => format!("invalid JSON: {message}"),
        _ => return error,
    };
    let cause = error.cause.take();
    let mut outer = strict_error(text);
    outer.cause = cause;
    outer.suppressed = true;
    outer
}

pub(super) fn repr_text(text: &Text) -> String {
    let quote = if text.0.contains(&39) && !text.0.contains(&34) {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for &point in &text.0 {
        use std::fmt::Write;
        match point {
            92 => out.push_str("\\\\"),
            10 => out.push_str("\\n"),
            13 => out.push_str("\\r"),
            9 => out.push_str("\\t"),
            value if value == u32::from(quote) => {
                out.push('\\');
                out.push(quote);
            }
            0..=31 | 127..=159 => {
                let _ = write!(out, "\\x{point:02x}");
            }
            0xd800..=0xdfff => {
                let _ = write!(out, "\\u{point:04x}");
            }
            value
                if char::from_u32(value)
                    .is_some_and(|ch| ch.is_control() || (python_space(ch) && ch != ' ')) =>
            {
                if value < 256 {
                    let _ = write!(out, "\\x{value:02x}");
                } else if value < 65536 {
                    let _ = write!(out, "\\u{value:04x}");
                } else {
                    let _ = write!(out, "\\U{value:08x}");
                }
            }
            value => out.push(char::from_u32(value).expect("decoded codepoint")),
        }
    }
    out.push(quote);
    out
}

fn strict_text(text: &Text) -> Result<()> {
    if let Some(start) = text
        .0
        .iter()
        .position(|point| (0xd800..=0xdfff).contains(point))
    {
        let mut end = start + 1;
        while text
            .0
            .get(end)
            .is_some_and(|point| (0xd800..=0xdfff).contains(point))
        {
            end += 1;
        }
        let point = text.0[start];
        let message = if end == start + 1 {
            format!(
                "'utf-8' codec can't encode character '\\u{point:04x}' in position {start}: surrogates not allowed"
            )
        } else {
            format!(
                "'utf-8' codec can't encode characters in position {start}-{}: surrogates not allowed",
                end - 1
            )
        };
        let mut error = strict_error("json string contains invalid Unicode");
        error.cause = Some(python_error("builtins.UnicodeEncodeError", message));
        error.suppressed = true;
        return Err(error);
    }
    Ok(())
}

pub(super) fn validate_strict<S: OriginalAuthorityOwner>(
    doc: &Document,
    op: &Operation<'_, S>,
) -> Result<()> {
    enum Check<'a> {
        Value(usize, usize),
        Key(&'a Text, usize),
    }
    let count = doc
        .node_count()
        .checked_mul(2)
        .and_then(|n| n.checked_add(1))
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    let _capacity = op.reserve(
        OriginalAuthorityStage::Audit,
        count
            .checked_mul(std::mem::size_of::<Check<'_>>())
            .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?,
    )?;
    let mut work = Vec::new();
    work.try_reserve_exact(count)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    work.push(Check::Value(doc.root, 0));
    while let Some(check) = work.pop() {
        op.check(OriginalAuthorityStage::Audit)?;
        let (id, depth) = match check {
            Check::Key(text, depth) => {
                if depth > 64 {
                    return Err(strict_error("json nesting exceeds depth limit"));
                }
                strict_text(text)?;
                continue;
            }
            Check::Value(id, depth) => (id, depth),
        };
        if depth > 64 {
            return Err(strict_error("json nesting exceeds depth limit"));
        }
        match doc.node(id) {
            Node::String(text) => strict_text(text)?,
            Node::Scalar(CanonValue::Float(_)) => {
                return Err(strict_error(
                    "floating point values are not permitted; use an exact string or integer",
                ));
            }
            Node::Scalar(CanonValue::BigInt(_)) => {
                return Err(strict_error(
                    "integer exceeds I-JSON safe range; use a canonical decimal string",
                ));
            }
            Node::Scalar(CanonValue::Int(value))
                if value.unsigned_abs() > tobkiri_canon::MAX_SAFE_INTEGER =>
            {
                return Err(strict_error(
                    "integer exceeds I-JSON safe range; use a canonical decimal string",
                ));
            }
            Node::Array(items) => {
                for &id in items.iter().rev() {
                    work.push(Check::Value(id, depth + 1));
                }
            }
            Node::Object(items) => {
                for (key, id) in items.iter().rev() {
                    work.push(Check::Value(*id, depth + 1));
                    work.push(Check::Key(key, depth));
                }
            }
            Node::Scalar(_) => {}
        }
    }
    Ok(())
}

pub(super) struct Audit<'a> {
    pub id: &'a str,
    pub kind: &'a str,
    pub state: &'a str,
    pub previous: &'a str,
    pub created: &'a CanonValue,
}
enum Action<'a> {
    Node(usize),
    Text(&'a Text),
    NativeText(&'a str),
    Scalar(&'a CanonValue),
    Literal(&'static str),
}
struct Writer {
    bytes: Vec<u8>,
    characters: usize,
    bad: Option<(usize, usize, u32)>,
}
impl Writer {
    fn ascii(&mut self, text: &str) {
        self.bytes.extend_from_slice(text.as_bytes());
        self.characters += text.len();
    }
    fn point(&mut self, value: u32) {
        if (0xd800..=0xdfff).contains(&value) {
            if let Some((_, end, _)) = self.bad.as_mut() {
                if *end == self.characters {
                    *end += 1;
                }
            } else {
                self.bad = Some((self.characters, self.characters + 1, value));
            }
        } else {
            let ch = char::from_u32(value).expect("decoded Unicode codepoint");
            let mut bytes = [0; 4];
            self.bytes
                .extend_from_slice(ch.encode_utf8(&mut bytes).as_bytes());
        }
        self.characters += 1;
    }
    fn text(&mut self, text: impl Iterator<Item = u32>) {
        self.ascii("\"");
        for ch in text {
            match ch {
                34 => self.ascii("\\\""),
                92 => self.ascii("\\\\"),
                8 => self.ascii("\\b"),
                12 => self.ascii("\\f"),
                10 => self.ascii("\\n"),
                13 => self.ascii("\\r"),
                9 => self.ascii("\\t"),
                0..=31 => self.ascii(&format!("\\u{ch:04x}")),
                _ => self.point(ch),
            }
        }
        self.ascii("\"");
    }
    fn scalar(&mut self, value: &CanonValue) -> Result<()> {
        if let CanonValue::Float(value) = value
            && !value.is_finite()
        {
            return Err(raw_error(
                "builtins.ValueError",
                format!(
                    "Out of range float values are not JSON compliant: {}",
                    if value.is_nan() {
                        "nan"
                    } else if value.is_sign_negative() {
                        "-inf"
                    } else {
                        "inf"
                    }
                ),
            ));
        }
        let bytes = encode(value, Variant::LegacyFloat).map_err(|_| {
            OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Audit,
                "Authority canonical scalar conversion",
            )
        })?;
        self.characters += bytes.len();
        self.bytes.extend_from_slice(&bytes);
        Ok(())
    }
    fn finish(self) -> Result<Vec<u8>> {
        if let Some((start, end, point)) = self.bad {
            return Err(raw_error(
                "builtins.UnicodeEncodeError",
                if end == start + 1 {
                    format!(
                        "'utf-8' codec can't encode character '\\u{point:04x}' in position {start}: surrogates not allowed"
                    )
                } else {
                    format!(
                        "'utf-8' codec can't encode characters in position {start}-{}: surrogates not allowed",
                        end - 1
                    )
                },
            ));
        }
        Ok(self.bytes)
    }
}
pub(super) fn canonical<S: OriginalAuthorityOwner>(
    doc: &Document,
    audit: Option<Audit<'_>>,
    op: &Operation<'_, S>,
) -> Result<Funded<Vec<u8>>> {
    canonical_view(doc, doc.root, None, audit, op)
}

#[cfg(unix)]
pub(super) fn canonical_subtree<S: OriginalAuthorityOwner>(
    doc: &Document,
    root: usize,
    omit: Option<&str>,
    op: &Operation<'_, S>,
) -> Result<Funded<Vec<u8>>> {
    canonical_view(doc, root, omit, None, op)
}

fn canonical_view<S: OriginalAuthorityOwner>(
    doc: &Document,
    root: usize,
    omit: Option<&str>,
    audit: Option<Audit<'_>>,
    op: &Operation<'_, S>,
) -> Result<Funded<Vec<u8>>> {
    let extra = audit
        .as_ref()
        .map(|value| value.id.len() + value.kind.len() + value.state.len() + value.previous.len())
        .unwrap_or(0);
    let bound = doc
        .bytes
        .checked_mul(12)
        .and_then(|n| n.checked_add(extra.checked_mul(6)?))
        .and_then(|n| n.checked_add(1024))
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    // Reserve the actual maximum action capacity before allocating it. Each
    // object edge contributes four actions, each array edge at most two;
    // the additional sixteen cover the audit wrapper. A single object-sort
    // vector can coexist, with one borrowed entry pointer per arena node.
    let action_count = doc
        .nodes
        .len()
        .checked_mul(4)
        .and_then(|n| n.checked_add(16))
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    let work = action_count
        .checked_mul(std::mem::size_of::<Action<'_>>())
        .and_then(|n| {
            n.checked_add(
                doc.nodes
                    .len()
                    .checked_mul(std::mem::size_of::<&(Text, usize)>())?,
            )
        })
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    let capacity = op.reserve(
        OriginalAuthorityStage::Audit,
        bound
            .checked_add(work)
            .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?,
    )?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(bound)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    let mut writer = Writer {
        bytes: output,
        characters: 0,
        bad: None,
    };
    let mut actions = Vec::new();
    actions
        .try_reserve_exact(action_count)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
    if let Some(value) = &audit {
        actions.extend([
            Action::Literal("}"),
            Action::NativeText(value.previous),
            Action::Literal(",\"previous_digest\":"),
            Action::Node(root),
            Action::Literal(",\"payload\":"),
            Action::NativeText(value.kind),
            Action::Literal(",\"event_type\":"),
            Action::NativeText(value.state),
            Action::Literal(",\"event_state\":"),
            Action::NativeText(value.id),
            Action::Literal(",\"event_id\":"),
            Action::Scalar(value.created),
            Action::Literal("{\"created_at\":"),
        ]);
    } else {
        actions.push(Action::Node(root));
    }
    while let Some(action) = actions.pop() {
        op.check(OriginalAuthorityStage::Audit)?;
        match action {
            Action::Literal(value) => writer.ascii(value),
            Action::Text(value) => writer.text(value.0.iter().copied()),
            Action::NativeText(value) => writer.text(value.chars().map(u32::from)),
            Action::Scalar(value) => writer.scalar(value)?,
            Action::Node(id) => match doc.node(id) {
                Node::Scalar(value) => writer.scalar(value)?,
                Node::String(text) => writer.text(text.0.iter().copied()),
                Node::Array(values) => {
                    actions.push(Action::Literal("]"));
                    for (index, value) in values.iter().enumerate().rev() {
                        actions.push(Action::Node(*value));
                        if index != 0 {
                            actions.push(Action::Literal(","));
                        }
                    }
                    actions.push(Action::Literal("["));
                }
                Node::Object(values) => {
                    let mut order = Vec::new();
                    order.try_reserve_exact(values.len()).map_err(|_| {
                        OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit)
                    })?;
                    order.extend(values.iter().filter(|(key, _)| {
                        id != root || omit.is_none_or(|omitted| !key.is(omitted))
                    }));
                    order.sort_unstable_by(|one, two| one.0.cmp(&two.0));
                    actions.push(Action::Literal("}"));
                    for (index, (key, value)) in order.into_iter().enumerate().rev() {
                        actions.push(Action::Node(*value));
                        actions.push(Action::Literal(":"));
                        actions.push(Action::Text(key));
                        if index != 0 {
                            actions.push(Action::Literal(","));
                        }
                    }
                    actions.push(Action::Literal("{"));
                }
            },
        }
    }
    Ok(Funded {
        value: writer.finish()?,
        _capacity: capacity,
    })
}
