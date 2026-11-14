//! Ordered compiler joins from the authenticated CAS metadata.
use super::{external_catalog_operations::OperationIndex, *};

type NodeId = usize;

/// Stable indices into the funded direct compiler inputs, in executable order.
pub(super) struct VerifiedVariants {
    _ordered: Funded<Vec<VerifiedVariant>>,
}

struct VerifiedVariant {
    _executable: NodeId,
    _function: NodeId,
}

pub(super) fn verify<S: OriginalAuthorityOwner>(
    docs: &compiler::DirectDefinitions,
    mut hash: impl FnMut(&Text) -> Result<tobkiri_canon::Digest>,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<VerifiedVariants> {
    let invalid = compiler::invalid_artifact;
    let manifest = &docs.manifest;
    let index = &docs.index;
    let executable = &docs.executable;
    let pack = field(manifest, manifest.root, "pack");
    let requirements = field(manifest, manifest.root, "requirements");
    let host_extension = is(manifest, pack, "kind", "host_extension");
    let remote = is(manifest, requirements, "execution_boundary", "remote");
    let functions = array(manifest, manifest.root, "functions");
    let variants = array(executable, executable.root, "variants");
    let index_rows = array(index, index.root, "artifacts");
    let funding = op.reserve(
        OriginalAuthorityStage::Source,
        variants.len().saturating_mul(size_of::<VerifiedVariant>()),
    )?;
    let scratch = op.reserve(
        OriginalAuthorityStage::Source,
        (index_rows.len() + functions.len()).saturating_mul(size_of::<(&Text, usize)>())
            + functions.len().saturating_mul(size_of::<bool>())
            + variants.len().saturating_mul(size_of::<&Text>()),
    )?;
    let mut seen_ids = funded_vec(variants.len())?;
    let mut ordered = funded_vec(variants.len())?;
    let mut seen_functions = funded_vec(functions.len())?;
    seen_functions.resize(functions.len(), false);
    let mut function_by_id = funded_vec(functions.len())?;
    for &id in functions {
        function_by_id.push((text(manifest, id, "id"), id));
    }
    sort_index(&mut function_by_id, op, budget)?;
    let mut runtime_by_path = funded_vec(index_rows.len())?;
    for &id in index_rows {
        if is(index, id, "role", "runtime") {
            runtime_by_path.push((text(index, id, "path"), id));
        }
    }
    sort_index(&mut runtime_by_path, op, budget)?;
    let mut operations = OperationIndex::new(&docs.contracts, executable, op, budget)?;

    for &variant in variants {
        let function_id = text(executable, variant, "function_id");
        let variant_id = text(executable, variant, "variant_id");
        let function_slot = find(&function_by_id, function_id, op, budget)?;
        let Some(function_slot) = function_slot else {
            return Err(invalid(
                "executable variant has unknown or duplicate Function",
            ));
        };
        charge(
            op,
            budget,
            (variant_id.0.len() + 1).saturating_mul(seen_ids.len() + 1),
        )?;
        if seen_functions[function_slot] || seen_ids.contains(&variant_id) {
            return Err(invalid(
                "executable variant has unknown or duplicate Function",
            ));
        }
        seen_functions[function_slot] = true;
        seen_ids.push(variant_id);
        let function = function_by_id[function_slot].1;
        let expected_kind = if host_extension {
            "host_extension"
        } else if optional_is(manifest, function, "isolation", "wasm_component") {
            "wasm"
        } else if remote || optional_is(manifest, function, "isolation", "remote") {
            "remote"
        } else {
            "pack_vm"
        };
        if !is(executable, variant, "execution_kind", expected_kind) {
            return Err(invalid("executable execution kind does not match Pack"));
        }
        let relative = text(executable, variant, "implementation_path");
        let digest = hash(relative)?;
        if !is(
            executable,
            variant,
            "implementation_digest",
            digest.as_str(),
        ) || !is(manifest, function, "implementation_digest", digest.as_str())
            || !find(&runtime_by_path, relative, op, budget)?
                .is_some_and(|slot| is(index, runtime_by_path[slot].1, "digest", digest.as_str()))
        {
            return Err(invalid("executable implementation digest mismatch"));
        }
        operations.validate_variant(manifest, function, variant, op, budget)?;
        ordered.push(VerifiedVariant {
            _executable: variant,
            _function: function,
        });
    }
    if seen_functions.iter().filter(|&&seen| seen).count() != function_by_id.len() {
        return Err(invalid(
            "not every declared Function has an executable variant",
        ));
    }
    drop((function_by_id, runtime_by_path, seen_ids, seen_functions));
    drop(scratch);
    Ok(VerifiedVariants {
        _ordered: Funded {
            value: ordered,
            _capacity: funding,
        },
    })
}

fn funded_vec<T>(size: usize) -> Result<Vec<T>> {
    let mut items = Vec::new();
    items.try_reserve_exact(size).map_err(capacity)?;
    if items.capacity() != size {
        return Err(capacity(()));
    }
    Ok(items)
}

fn sort_index<S: OriginalAuthorityOwner>(
    items: &mut Vec<(&Text, NodeId)>,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let longest = items.iter().map(|(key, _)| key.0.len()).max().unwrap_or(0);
    let levels = usize::BITS - items.len().max(1).leading_zeros();
    charge(
        op,
        budget,
        items
            .len()
            .saturating_mul(longest + 1)
            .saturating_mul(levels as usize + 1),
    )?;
    // Last occurrence is the value retained by the original dict comprehension.
    items.sort_unstable_by(|left, right| left.0.cmp(right.0).then_with(|| right.1.cmp(&left.1)));
    items.dedup_by(|right, left| right.0 == left.0);
    Ok(())
}

fn find<S: OriginalAuthorityOwner>(
    items: &[(&Text, NodeId)],
    key: &Text,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<usize>> {
    let levels = usize::BITS - items.len().max(1).leading_zeros();
    charge(
        op,
        budget,
        (key.0.len() + 1).saturating_mul(levels as usize + 1),
    )?;
    Ok(items.binary_search_by(|item| item.0.cmp(key)).ok())
}

fn field(doc: &Document, parent: NodeId, name: &str) -> NodeId {
    doc.get(parent, name).expect("checked compiler field")
}

fn array<'a>(doc: &'a Document, parent: NodeId, name: &str) -> &'a [NodeId] {
    let Node::Array(items) = doc.node(field(doc, parent, name)) else {
        unreachable!("checked compiler array");
    };
    items
}

fn text<'a>(doc: &'a Document, parent: NodeId, name: &str) -> &'a Text {
    let Node::String(text) = doc.node(field(doc, parent, name)) else {
        unreachable!("checked compiler string");
    };
    text
}

fn is(doc: &Document, parent: NodeId, name: &str, expected: &str) -> bool {
    doc.string_is(field(doc, parent, name), expected)
}

fn optional_is(doc: &Document, parent: NodeId, name: &str, expected: &str) -> bool {
    doc.get(parent, name)
        .is_some_and(|id| doc.string_is(id, expected))
}
