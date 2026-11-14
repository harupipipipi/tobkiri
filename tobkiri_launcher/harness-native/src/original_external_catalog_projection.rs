//! Ordinary signed Pack projection in the fixed original executable loader.
//! This pure comparison confers no admission and does not replace any read.
use super::{canonical_digest, capacity, charge, model};
use crate::original_authority::{
    Funded, Operation, OriginalAuthorityError, OriginalAuthorityOwner, OriginalAuthorityStage,
    Result,
    json::{self, Document, Node, Text},
};
use sha2::{Digest as _, Sha256};
use std::{mem::size_of, sync::Arc};
use tobkiri_canon::{AllocationBudget, Digest, MAX_BYTES};

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;

#[derive(Clone, Copy)]
pub(super) struct NormalProjection<'a> {
    // These three documents have passed their individual Rust schema checks.
    pub(super) manifest: &'a Document,
    pub(super) contracts: &'a Document,
    pub(super) executable: &'a Document,
    pub(super) physical_contracts_digest: &'a Digest,
    pub(super) signed: &'a Document,
}

#[derive(Clone, Copy)]
struct Export<'a> {
    contract: &'a Text,
    operation: &'a Text,
    provider: &'a Text,
    implementation: &'a Text,
    // Python sorts stably by operation ID, including across variants.
    position: usize,
}

/// Compare the complete ordinary projection to the Host-authenticated record.
pub(super) fn verify<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    input: NormalProjection<'_>,
    registry: &Document,
    entry: usize,
) -> Result<()> {
    let projected = project_digest(op, budget, input)?;
    let record = field(registry, entry, "catalog_record");
    let admitted = canonical_digest(registry, record, None, op, budget)?;
    if projected != admitted {
        return Err(model::denied("external Pack compatibility record changed"));
    }
    Ok(())
}

fn project_digest<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    input: NormalProjection<'_>,
) -> Result<Digest> {
    let NormalProjection {
        manifest,
        contracts,
        executable,
        physical_contracts_digest,
        signed,
    } = input;
    let pack = field(manifest, manifest.root, "pack");
    let integrity = field(manifest, manifest.root, "integrity");
    let requirements = field(manifest, manifest.root, "requirements");
    let pack_id = text(manifest, pack, "id");

    if !text(manifest, integrity, "contract_catalog_digest").is(physical_contracts_digest.as_str())
        || !same_text(
            pack_id,
            text(contracts, contracts.root, "pack_id"),
            op,
            budget,
        )?
        || !same_text(
            text(manifest, integrity, "source_identity"),
            text(contracts, contracts.root, "source_identity"),
            op,
            budget,
        )?
    {
        return Err(model::denied(
            "signed Pack Contract catalog identity is inconsistent",
        ));
    }
    if manifest.string_is(field(manifest, pack, "kind"), "host_extension") {
        // The explicit bundled baseline uses the separate captured Host
        // Extension helper and never produces this Normal projection.
        return Err(OriginalAuthorityError::unsupported(
            STAGE,
            "external Host Extension compatibility projection",
        ));
    }
    let variants = items(executable, executable.root, "variants");
    if executable
        .get(executable.root, "materialization_catalog_digest")
        .is_some()
    {
        return Err(model::denied(
            "external Pack cannot replace its executable catalog identity",
        ));
    }
    for &variant in variants {
        charge(op, budget, 96)?;
        let kind = field(executable, variant, "execution_kind");
        if !executable.string_is(kind, "wasm") && !executable.string_is(kind, "pack_vm") {
            return Err(model::denied(
                "external admission accepts only Normal Sandbox backends",
            ));
        }
    }
    let kind = field(manifest, pack, "kind");
    let boundary = field(manifest, requirements, "execution_boundary");
    if !(manifest.string_is(kind, "normal_sandbox") || manifest.string_is(kind, "application"))
        || !(manifest.string_is(boundary, "sandbox")
            || manifest.string_is(boundary, "declarative_only"))
    {
        return Err(model::denied(
            "external admission accepts only Normal Sandbox Packs",
        ));
    }
    if !signed_identity(signed, "pack_id", pack_id, op, budget)?
        || !signed_identity(
            signed,
            "version",
            text(manifest, pack, "version"),
            op,
            budget,
        )?
    {
        return Err(model::denied("signed Pack and v4 manifest disagree"));
    }

    let count = variants.iter().try_fold(0usize, |count, &variant| {
        charge(op, budget, 1)?;
        count
            .checked_add(items(executable, variant, "operations").len())
            .ok_or_else(|| capacity(()))
    })?;
    let mut exports = reserve_list::<Export<'_>, _>(count, op)?;
    let mut longest_contract = 0;
    let mut longest_operation = 0;
    for &variant in variants {
        let provider = text(executable, variant, "function_id");
        let implementation = text(executable, variant, "implementation_digest");
        for &row in items(executable, variant, "operations") {
            charge(op, budget, 1)?;
            let contract = text(executable, row, "contract_id");
            let operation = text(executable, row, "operation_id");
            longest_contract = longest_contract.max(contract.0.len());
            longest_operation = longest_operation.max(operation.0.len());
            let position = exports.len();
            exports.push(Export {
                contract,
                operation,
                provider,
                implementation,
                position,
            });
        }
    }
    charge(
        op,
        budget,
        sort_work(
            count,
            longest_contract
                .saturating_add(longest_operation)
                .saturating_add(1),
        ),
    )?;
    exports.sort_unstable_by(|a, b| {
        (a.contract, a.operation, a.position).cmp(&(b.contract, b.operation, b.position))
    });
    for pair in exports.windows(2) {
        if same_text(pair[0].contract, pair[1].contract, op, budget)?
            && !same_text(pair[0].provider, pair[1].provider, op, budget)?
        {
            return Err(model::denied("external Contract provider is ambiguous"));
        }
    }

    let declared = items(manifest, manifest.root, "contracts");
    let mut provided = reserve_list::<(&Text, &[Export<'_>]), _>(declared.len(), op)?;
    for &row in declared {
        let id = text(manifest, row, "contract_id");
        longest_contract = longest_contract.max(id.0.len());
        charge(op, budget, sort_work(1, longest_contract + 1))?;
        let first = exports.partition_point(|export| export.contract < id);
        let rest = &exports[first..];
        let end = rest.partition_point(|export| export.contract == id);
        if end == 0 {
            return Err(model::denied("external v4 Contract executable is missing"));
        }
        provided.push((id, &rest[..end]));
    }
    charge(op, budget, sort_work(provided.len(), longest_contract + 1))?;
    provided.sort_unstable_by_key(|&(id, _)| id);

    // The admitted signer emits strings here. A differently typed value
    // observed on a later read is outside this portable projection path;
    // the original loader's fresh re-verification is a separate step.
    let publisher = signed_text(signed, signed.root, "publisher_id", op, budget)?;
    let signature = signed_field(signed, signed.root, "signature", op, budget)?;
    let key = signed_text(signed, signature, "key_id", op, budget)?;

    let caps = items(manifest, requirements, "capabilities");
    let mut capabilities = reserve_list::<&[u32], _>(caps.len(), op)?;
    let mut longest_capability = 0;
    for &cap in caps {
        charge(op, budget, 1)?;
        let Node::String(cap) = manifest.node(cap) else {
            unreachable!("checked capability string");
        };
        longest_capability = longest_capability.max(cap.0.len());
        capabilities.push(&cap.0);
    }
    charge(op, budget, sort_work(caps.len(), longest_capability + 1))?;
    capabilities.sort_unstable();

    // Fields and fixed maps below are in canonical key order. Values copied
    // unchanged from the manifest keep array order and canonical object order.
    let mut out = Output {
        hash: Sha256::new(),
        bytes: 0,
        op,
        budget,
    };
    out.write(b"{")?;
    out.field("approval_policy")?;
    out.write(br#""explicit","#)?;
    out.field("artifact_digest")?;
    out.node(manifest, field(manifest, pack, "artifact_digest"))?;
    out.write(br#","authority":"host-signed-external-normal-v4","capabilities":"#)?;
    out.strings(&capabilities)?;
    out.write(b",")?;
    out.field("dependencies")?;
    out.node(manifest, field(manifest, requirements, "pack_dependencies"))?;
    out.write(br#","description":"Signed external Tobkiri v4 Pack.","display_name":"#)?;
    out.node(manifest, field(manifest, pack, "display_name"))?;
    out.write(b",")?;
    out.field("execution_boundary")?;
    out.node(manifest, boundary)?;
    out.write(b",")?;
    out.field("kind")?;
    out.node(manifest, kind)?;
    out.write(br#","legacy_ids":[],"legacy_operations":[],"network":"#)?;
    out.node(manifest, field(manifest, requirements, "network"))?;
    out.write(b",")?;
    out.field("pack_id")?;
    out.quoted(&pack_id.0)?;
    out.write(br#","provided_contracts":["#)?;
    if !provided.is_empty() {
        const PREFIX: &[u32] = &[99, 97, 112, 97, 98, 105, 108, 105, 116, 121, 58]; // capability:
        charge(op, budget, caps.len().saturating_mul(PREFIX.len()))?;
        for cap in capabilities.iter_mut() {
            *cap = cap.strip_prefix(PREFIX).unwrap_or(cap);
        }
        charge(op, budget, sort_work(caps.len(), longest_capability + 1))?;
        capabilities.sort_unstable();
    }
    for (index, &(id, operations)) in provided.iter().enumerate() {
        if index != 0 {
            out.write(b",")?;
        }
        out.write(br#"{"cardinality":"one","contract_id":"#)?;
        out.quoted(&id.0)?;
        out.write(br#","failure":"fail_closed","isolation":"process","operations":["#)?;
        for (index, row) in operations.iter().enumerate() {
            if index != 0 {
                out.write(b",")?;
            }
            out.write(br#"{"entrypoint_id":"#)?;
            out.quoted(&row.operation.0)?;
            out.write(br#","id":"#)?;
            out.quoted(&row.operation.0)?;
            out.write(br#","implementation_digest":"#)?;
            out.quoted(&row.implementation.0)?;
            out.write(b"}")?;
        }
        out.write(br#"],"owner":"#)?;
        out.quoted(&pack_id.0)?;
        out.write(br#","provider_id":"#)?;
        out.quoted(&operations[0].provider.0)?;
        out.write(br#","required_capabilities":"#)?;
        out.strings(&capabilities)?;
        out.write(br#","version":"1.0.0"}"#)?;
    }
    out.write(br#"],"required_contracts":"#)?;
    out.node(
        manifest,
        field(manifest, requirements, "contract_dependencies"),
    )?;
    out.write(b",")?;
    out.field("runtime_artifacts")?;
    out.node(manifest, field(manifest, manifest.root, "artifacts"))?;
    out.write(b",")?;
    out.field("secrets")?;
    out.node(manifest, field(manifest, requirements, "secrets"))?;
    out.write(br#","source_provenance":{"key_id":"#)?;
    out.quoted(&key.0)?;
    out.write(br#","mode":"host-signed-external-normal-catalog","publisher_id":"#)?;
    out.quoted(&publisher.0)?;
    out.write(br#"},"version":"#)?;
    out.node(manifest, field(manifest, pack, "version"))?;
    out.write(b",")?;
    out.field("workspace_boundary")?;
    out.node(
        manifest,
        field(manifest, requirements, "workspace_boundary"),
    )?;
    out.write(b"}")?;
    Ok(Digest::from_sha256(out.hash.finalize().into()))
}

struct Output<'a, 'op, S> {
    hash: Sha256,
    bytes: usize,
    op: &'a Operation<'op, S>,
    budget: &'a Arc<dyn AllocationBudget>,
}

impl<S: OriginalAuthorityOwner> Output<'_, '_, S> {
    fn write(&mut self, bytes: &[u8]) -> Result<()> {
        self.bytes = self.bytes.saturating_add(bytes.len());
        if self.bytes > MAX_BYTES {
            return Err(json::strict_error("canonical JSON exceeds size limit"));
        }
        for chunk in bytes.chunks(64 * 1024) {
            charge(self.op, self.budget, chunk.len())?;
            self.hash.update(chunk);
        }
        Ok(())
    }

    fn node(&mut self, doc: &Document, id: usize) -> Result<()> {
        // The manifest was strictly parsed. Each copied field moves up one
        // level or stays at the same depth in the projection.
        self.write(&json::canonical_subtree(
            doc,
            id,
            None,
            self.op,
            self.budget,
        )?)
    }

    fn field(&mut self, key: &str) -> Result<()> {
        self.write(b"\"")?;
        self.write(key.as_bytes())?;
        self.write(b"\":")
    }

    fn quoted(&mut self, points: &[u32]) -> Result<()> {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        self.write(b"\"")?;
        for batch in points.chunks(256) {
            charge(self.op, self.budget, batch.len())?;
            let mut buf = [0; 256 * 6];
            let mut len = 0;
            for &point in batch {
                let mut unicode = [0; 4];
                let mut escaped = *b"\\u0000";
                let bytes: &[u8] = match point {
                    34 => br#"\""#,
                    92 => br"\\",
                    8 => br"\b",
                    12 => br"\f",
                    10 => br"\n",
                    13 => br"\r",
                    9 => br"\t",
                    0..=31 => {
                        escaped[4] = HEX[(point >> 4) as usize];
                        escaped[5] = HEX[(point & 15) as usize];
                        &escaped
                    }
                    _ => char::from_u32(point)
                        .ok_or_else(|| json::strict_error("json string contains invalid Unicode"))?
                        .encode_utf8(&mut unicode)
                        .as_bytes(),
                };
                buf[len..len + bytes.len()].copy_from_slice(bytes);
                len += bytes.len();
            }
            self.write(&buf[..len])?;
        }
        self.write(b"\"")
    }

    fn strings(&mut self, values: &[&[u32]]) -> Result<()> {
        self.write(b"[")?;
        for (index, value) in values.iter().enumerate() {
            if index != 0 {
                self.write(b",")?;
            }
            self.quoted(value)?;
        }
        self.write(b"]")
    }
}

fn reserve_list<T, S: OriginalAuthorityOwner>(
    count: usize,
    op: &Operation<'_, S>,
) -> Result<Funded<Vec<T>>> {
    let funding = op.reserve(
        STAGE,
        count
            .checked_mul(size_of::<T>())
            .ok_or_else(|| capacity(()))?,
    )?;
    let mut value = Vec::new();
    value.try_reserve_exact(count).map_err(capacity)?;
    if value.capacity() != count {
        return Err(capacity(()));
    }
    Ok(Funded {
        value,
        _capacity: funding,
    })
}

fn sort_work(count: usize, width: usize) -> usize {
    count
        .saturating_mul(width)
        .saturating_mul(usize::BITS as usize)
}

fn field(doc: &Document, parent: usize, name: &str) -> usize {
    doc.get(parent, name).expect("checked document field")
}

fn text<'a>(doc: &'a Document, parent: usize, name: &str) -> &'a Text {
    let Node::String(value) = doc.node(field(doc, parent, name)) else {
        unreachable!("checked document string");
    };
    value
}

fn items<'a>(doc: &'a Document, parent: usize, name: &str) -> &'a [usize] {
    let Node::Array(value) = doc.node(field(doc, parent, name)) else {
        unreachable!("checked document array");
    };
    value
}

fn same_text<S: OriginalAuthorityOwner>(
    a: &Text,
    b: &Text,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    charge(op, budget, a.0.len().min(b.0.len()).saturating_add(1))?;
    Ok(a == b)
}

fn signed_lookup<S: OriginalAuthorityOwner>(
    doc: &Document,
    object: usize,
    name: &str,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<usize>> {
    let count = doc.entries(object).map_or(0, <[_]>::len);
    charge(op, budget, count.saturating_mul(name.len() + 1))?;
    Ok(doc.get(object, name))
}

fn signed_identity<S: OriginalAuthorityOwner>(
    doc: &Document,
    name: &str,
    expected: &Text,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    match signed_lookup(doc, doc.root, name, op, budget)?.map(|id| doc.node(id)) {
        Some(Node::String(value)) => same_text(expected, value, op, budget),
        _ => Ok(false),
    }
}

fn signed_field<S: OriginalAuthorityOwner>(
    doc: &Document,
    object: usize,
    name: &'static str,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<usize> {
    signed_lookup(doc, object, name, op, budget)?.ok_or_else(|| {
        if doc.entries(object).is_some() {
            let message = match name {
                "publisher_id" => "'publisher_id'",
                "signature" => "'signature'",
                "key_id" => "'key_id'",
                _ => unreachable!("known signed field"),
            };
            json::raw_error("builtins.KeyError", message)
        } else {
            OriginalAuthorityError::unsupported(STAGE, "external Pack signed provenance type")
        }
    })
}

fn signed_text<'a, S: OriginalAuthorityOwner>(
    doc: &'a Document,
    object: usize,
    name: &'static str,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<&'a Text> {
    match doc.node(signed_field(doc, object, name, op, budget)?) {
        Node::String(value) => Ok(value),
        _ => Err(OriginalAuthorityError::unsupported(
            STAGE,
            "external Pack signed provenance type",
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::super::super::definitions::tests::{operation, with_source};
    use super::super::{
        Parent, compiler, external_catalog_pinned, read_artifact_definition, read_signed_manifest,
    };
    use super::*;
    use serde_json::Value;
    use std::{fs, path::Path};

    #[tokio::test(flavor = "current_thread")]
    async fn reprojects_original_signed_packs_and_rejects_different_records() {
        let Some(fixture) = std::env::var_os("TOBKIRI_EXTERNAL_CATALOG_FIXTURE") else {
            return;
        };
        let bytes = fs::read(fixture).unwrap();
        assert!(bytes.len() <= 2 * 1024 * 1024);
        let fixture: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            fixture["format"],
            "io.tobkiri.original-external-pack-catalog-reference.v1"
        );
        let mut matched = 0;
        for case in fixture["cases"].as_array().unwrap() {
            if !matches!(case["name"].as_str(), Some("admitted" | "retained-version")) {
                continue;
            }
            let runtime = Path::new(case["runtime_dir"].as_str().unwrap());
            let user_data = Path::new(case["native_user_data"].as_str().unwrap());
            let entries = case["expected"]["value"]["entries"].as_object().unwrap();
            for row in entries.values() {
                let id = row["pack_id"].as_str().unwrap();
                let digest = Digest::parse(row["artifact_digest"].as_str().unwrap()).unwrap();
                with_source(|source, read, cancellation, budget| {
                    let op = operation(read, source, cancellation);
                    let selection =
                        external_catalog_pinned::read(&op, runtime, user_data, budget, id, &digest)
                            .unwrap();
                    let external_catalog_pinned::PinnedPackSelection::Normal { registry, pack } =
                        selection
                    else {
                        panic!("ordinary signed fixture did not produce its exact pinned version");
                    };
                    let root = &pack._root;
                    let directory = Parent::walk(&root.path, &op.cleanup).unwrap();
                    let mut physical_contracts = None;
                    let definitions = compiler::read_direct_definitions(
                        |name| {
                            let raw = read_artifact_definition(
                                &op, &directory, &root.path, name, budget,
                            )?;
                            if name == c"contracts.v4.json" {
                                charge(&op, budget, raw.len())?;
                                physical_contracts = Some(Digest::hash(&raw));
                            }
                            Ok(raw)
                        },
                        &op,
                        budget,
                    )
                    .unwrap();
                    let raw_signed =
                        read_signed_manifest(&op, budget, &directory, &root.path).unwrap();
                    let signed = model::decode_host_json(&raw_signed, &op, budget).unwrap();
                    drop(raw_signed);
                    directory.close().unwrap();
                    assert_eq!(read.receipt().open_files, 0);

                    let physical_contracts = physical_contracts.unwrap();
                    let input = NormalProjection {
                        manifest: &definitions.manifest,
                        contracts: &definitions.contracts,
                        executable: &definitions.executable,
                        physical_contracts_digest: &physical_contracts,
                        signed: &signed,
                    };
                    let record = field(&registry, root.entry, "catalog_record");
                    let canonical =
                        json::canonical_subtree(&registry, record, None, &op, budget).unwrap();
                    assert_eq!(
                        serde_json::from_slice::<Value>(&canonical).unwrap(),
                        row["catalog_record"],
                    );
                    drop(canonical);
                    verify(&op, budget, input, &registry, root.entry).unwrap();

                    let mismatched_record = br#"{"catalog_record":{}}"#;
                    let different =
                        model::decode_host_json(mismatched_record, &op, budget).unwrap();
                    let error = verify(&op, budget, input, &different, different.root).unwrap_err();
                    assert_eq!(error.message, "external Pack compatibility record changed");

                    let wrong_digest = Digest::hash(b"different contract catalog bytes");
                    let error = verify(
                        &op,
                        budget,
                        NormalProjection {
                            physical_contracts_digest: &wrong_digest,
                            ..input
                        },
                        &registry,
                        root.entry,
                    )
                    .unwrap_err();
                    assert_eq!(
                        error.message,
                        "signed Pack Contract catalog identity is inconsistent"
                    );
                });
                matched += 1;
            }
        }
        assert_eq!(
            matched, 3,
            "expected a fresh admission and both retained versions"
        );
    }
}
