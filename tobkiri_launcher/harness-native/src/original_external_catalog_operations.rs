//! Operation joins for the pinned direct CAS compiler, over its validated documents.
use super::{canonical_digest, capacity, charge, compiler::invalid_artifact};
use crate::original_authority::{
    Operation, OriginalAuthorityOwner, OriginalAuthorityStage, Result,
    json::{Document, Node, Text},
};
use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};
use tobkiri_canon::{AllocationBudget, CanonValue};
use tobkiri_gate::NativeSetupReservation;

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const INVENTORY: &str = "executable variant Operation inventory mismatch";
const SCHEMAS: &str = "executable Operation schema digest mismatch";
const SCHEMA_FIELDS: [(&str, &str); 3] = [
    ("input_schema", "input_schema_digest"),
    ("output_schema", "output_schema_digest"),
    ("error_schema", "error_schema_digest"),
];

/// Borrowed identity indexes, retained for the entire executable catalog walk.
pub(super) struct OperationIndex<'a> {
    contracts: &'a Document,
    executable: &'a Document,
    contract_documents: HashMap<&'a [u32], usize>,
    routes: HashSet<(&'a [u32], &'a [u32])>,
    _tables: NativeSetupReservation,
}

impl<'a> OperationIndex<'a> {
    /// Index Contracts and reserve the maximum number of route keys.
    pub(super) fn new<S: OriginalAuthorityOwner>(
        contracts: &'a Document,
        executable: &'a Document,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self> {
        let documents = items(contracts, contracts.root, "contracts");
        let mut route_count = 0usize;
        for &variant in items(executable, executable.root, "variants") {
            charge(op, budget, 1)?;
            route_count = route_count
                .checked_add(items(executable, variant, "operations").len())
                .ok_or_else(|| capacity(()))?;
        }
        let entries = documents
            .len()
            .checked_add(route_count)
            .ok_or_else(|| capacity(()))?;
        charge(op, budget, entries)?;
        let _tables = fund_tables(op, entries)?;
        let mut contract_documents = HashMap::new();
        contract_documents
            .try_reserve(documents.len())
            .map_err(capacity)?;
        for &contract in documents {
            let id = text(contracts, contract, "contract_id");
            charge(op, budget, id.len() + 1)?;
            // A dict comprehension uses the last value for an equal key.
            contract_documents.insert(id, contract);
        }
        let mut routes = HashSet::new();
        routes.try_reserve(route_count).map_err(capacity)?;
        Ok(Self {
            contracts,
            executable,
            contract_documents,
            routes,
            _tables,
        })
    }

    /// Match the Function inventory, then check and retain routes in executable order.
    pub(super) fn validate_variant<S: OriginalAuthorityOwner>(
        &mut self,
        manifest: &Document,
        function: usize,
        variant: usize,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<&'a [usize]> {
        let executable = self.executable;
        let operations = items(executable, variant, "operations");
        let declared = items(manifest, function, "operations");
        check_inventory(manifest, declared, executable, operations, op, budget)?;

        for &operation in operations {
            let contract_id = text(executable, operation, "contract_id");
            let operation_id = text(executable, operation, "operation_id");
            charge(op, budget, contract_id.len() + 1)?;
            let contract = self
                .contract_documents
                .get(contract_id)
                .copied()
                .ok_or_else(|| invalid_artifact("executable Contract revision mismatch"))?;
            if !equal_text(
                text(self.contracts, contract, "revision_digest"),
                text(executable, operation, "revision_digest"),
                op,
                budget,
            )? {
                return Err(invalid_artifact("executable Contract revision mismatch"));
            }
            let source = declared_operation(self.contracts, contract, operation_id, op, budget)?;
            check_schemas(
                self.contracts,
                contract,
                source,
                executable,
                operation,
                op,
                budget,
            )?;

            // The other constructor checks are already enforced by the strict
            // document schemas and semantic identity validation.
            let default = integer(executable, operation, "timeout_default_ms");
            let maximum = integer(executable, operation, "timeout_hard_max_ms");
            if maximum < default {
                return Err(invalid_artifact("hard timeout cannot be below default"));
            }

            charge(op, budget, contract_id.len() + operation_id.len() + 1)?;
            if !self.routes.insert((contract_id, operation_id)) {
                return Err(invalid_artifact(
                    "executable Operation mapping is duplicated or unqualified",
                ));
            }
        }
        Ok(operations)
    }
}

fn check_inventory<S: OriginalAuthorityOwner>(
    manifest: &Document,
    declared: &[usize],
    executable: &Document,
    operations: &[usize],
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let _table = fund_tables(op, declared.len())?;
    let mut inventory = HashMap::new();
    inventory.try_reserve(declared.len()).map_err(capacity)?;
    for &node in declared {
        let Node::String(id) = manifest.node(node) else {
            unreachable!("checked manifest Operation identifier");
        };
        charge(op, budget, 1 + id.0.len())?;
        inventory.insert(id.0.as_slice(), false);
    }
    for &operation in operations {
        let id = text(executable, operation, "operation_id");
        charge(op, budget, 1 + id.len())?;
        let matched = inventory
            .get_mut(id)
            .ok_or_else(|| invalid_artifact(INVENTORY))?;
        *matched = true;
    }
    charge(op, budget, inventory.capacity())?;
    if inventory.values().all(|matched| *matched) {
        Ok(())
    } else {
        Err(invalid_artifact(INVENTORY))
    }
}

fn declared_operation<S: OriginalAuthorityOwner>(
    contracts: &Document,
    contract: usize,
    operation_id: &[u32],
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<usize> {
    let undeclared = || invalid_artifact("executable Operation is not declared exactly once");
    let mut source = None;
    for &candidate in items(contracts, contract, "operations") {
        if equal_text(
            text(contracts, candidate, "operation_id"),
            operation_id,
            op,
            budget,
        )? && source.replace(candidate).is_some()
        {
            return Err(undeclared());
        }
    }
    source.ok_or_else(undeclared)
}

fn check_schemas<S: OriginalAuthorityOwner>(
    contracts: &Document,
    contract: usize,
    source: usize,
    executable: &Document,
    operation: usize,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let fields = SCHEMA_FIELDS.map(|(schema, digest)| {
        (
            field(executable, operation, schema),
            text(contracts, source, digest),
        )
    });
    // The original checks all three executable digests before consulting the
    // Contract schema catalog. Dict equality can equate true/false with 1/0.
    for (schema, digest) in fields {
        let actual = canonical_digest(executable, schema, None, op, budget)?;
        if !digest
            .iter()
            .copied()
            .eq(actual.as_str().bytes().map(u32::from))
        {
            return Err(invalid_artifact(SCHEMAS));
        }
    }
    let catalog = contracts
        .entries(field(contracts, contract, "schema_catalog"))
        .expect("checked Contract schema catalog");
    for (schema, digest) in fields {
        let Some(expected) = entry(catalog, digest, op, budget)? else {
            return Err(invalid_artifact(SCHEMAS));
        };
        if !python_equal(contracts, expected, executable, schema, op, budget)? {
            return Err(invalid_artifact(SCHEMAS));
        }
    }
    Ok(())
}

pub(super) fn python_equal<S: OriginalAuthorityOwner>(
    left_doc: &Document,
    left: usize,
    right_doc: &Document,
    right: usize,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    // Both inputs have already passed strict JSON validation, including the
    // depth bound and rejection of floats and integers beyond the safe range.
    charge(op, budget, 1)?;
    Ok(match (left_doc.node(left), right_doc.node(right)) {
        (Node::String(a), Node::String(b)) => equal_text(&a.0, &b.0, op, budget)?,
        (Node::Scalar(CanonValue::Int(a)), Node::Scalar(CanonValue::Bool(b)))
        | (Node::Scalar(CanonValue::Bool(b)), Node::Scalar(CanonValue::Int(a))) => {
            *a == i64::from(*b)
        }
        (Node::Scalar(a), Node::Scalar(b)) => a == b,
        (Node::Array(a), Node::Array(b)) if a.len() == b.len() => {
            for (&a, &b) in a.iter().zip(b) {
                if !python_equal(left_doc, a, right_doc, b, op, budget)? {
                    return Ok(false);
                }
            }
            true
        }
        (Node::Object(a), Node::Object(b)) if a.len() == b.len() => {
            for (key, left) in a {
                let Some(right) = entry(b, &key.0, op, budget)? else {
                    return Ok(false);
                };
                if !python_equal(left_doc, *left, right_doc, right, op, budget)? {
                    return Ok(false);
                }
            }
            true
        }
        _ => false,
    })
}

fn entry<S: OriginalAuthorityOwner>(
    entries: &[(Text, usize)],
    key: &[u32],
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<usize>> {
    for (name, value) in entries {
        if equal_text(&name.0, key, op, budget)? {
            return Ok(Some(*value));
        }
    }
    Ok(None)
}

fn equal_text<S: OriginalAuthorityOwner>(
    left: &[u32],
    right: &[u32],
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    charge(op, budget, 1 + left.len().min(right.len()))?;
    Ok(left == right)
}

fn fund_tables<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    entries: usize,
) -> Result<NativeSetupReservation> {
    // The conservative bound covers bucket rounding, load factor and metadata
    // for a table ranging from one borrowed key up to a pair of borrowed keys.
    let bytes = entries
        .checked_mul(256)
        .and_then(|bytes| bytes.checked_add(512))
        .ok_or_else(|| capacity(()))?;
    op.reserve(STAGE, bytes)
}

fn field(doc: &Document, parent: usize, key: &str) -> usize {
    doc.get(parent, key).expect("checked compiler input")
}

fn items<'a>(doc: &'a Document, parent: usize, key: &str) -> &'a [usize] {
    let Node::Array(items) = doc.node(field(doc, parent, key)) else {
        unreachable!("checked compiler array");
    };
    items
}

fn text<'a>(doc: &'a Document, parent: usize, key: &str) -> &'a [u32] {
    let Node::String(text) = doc.node(field(doc, parent, key)) else {
        unreachable!("checked compiler string");
    };
    &text.0
}

fn integer(doc: &Document, parent: usize, key: &str) -> i64 {
    let Node::Scalar(CanonValue::Int(value)) = doc.node(field(doc, parent, key)) else {
        unreachable!("checked compiler integer");
    };
    *value
}

#[cfg(test)]
mod tests {
    use super::super::super::definitions::tests::{operation, with_source};
    use super::*;
    use crate::original_authority::{OriginalAuthorityCause, json};
    use serde_json::{Value, json};
    use tobkiri_canon::Digest;

    #[tokio::test(flavor = "current_thread")]
    async fn joins_qualified_operations_and_preserves_executable_order() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let decode = |value: Value| {
                json::decode_catalog_lock(&serde_json::to_vec(&value).unwrap(), &op, budget)
                    .unwrap()
            };
            let digest = Digest::hash(br#"{"additionalProperties":false,"enum":[true]}"#);
            let manifest = decode(json!({"functions": [{"operations": ["first", "second"]}]}));
            let contract_operation = |id| {
                json!({
                    "operation_id": id,
                    "input_schema_digest": digest.as_str(),
                    "output_schema_digest": digest.as_str(),
                    "error_schema_digest": digest.as_str(),
                })
            };
            let contracts = |catalog_schema: Value, declarations: Vec<Value>| {
                decode(json!({"contracts": [{
                    "contract_id": "sample.echo.v1",
                    "version": "1.0.0",
                    "revision_digest": digest.as_str(),
                    "operations": declarations,
                    "schema_catalog": {(digest.as_str()): catalog_schema},
                }]}))
            };
            let equivalent = || json!({"enum": [1], "additionalProperties": 0});
            let declarations = || vec![contract_operation("first"), contract_operation("second")];
            let executable_operation = |id| {
                let schema = json!({"enum": [true], "additionalProperties": false});
                json!({
                    "contract_id": "sample.echo.v1",
                    "contract_version": "1.1.0",
                    "revision_digest": digest.as_str(),
                    "operation_id": id,
                    "input_schema": schema,
                    "output_schema": schema,
                    "error_schema": schema,
                    "timeout_default_ms": 4,
                    "timeout_hard_max_ms": 8,
                })
            };
            let function = items(&manifest, manifest.root, "functions")[0];
            let first = executable_operation("first");
            let second = executable_operation("second");
            let executable = decode(json!({"variants": [
                {"operations": [second.clone(), first.clone()]},
                {"operations": [first.clone(), second.clone()]},
            ]}));
            let original_contracts = contracts(equivalent(), declarations());
            let variants = items(&executable, executable.root, "variants");
            let mut index =
                OperationIndex::new(&original_contracts, &executable, &op, budget).unwrap();
            let result = index
                .validate_variant(&manifest, function, variants[0], &op, budget)
                .unwrap();
            assert!(executable.string_is(field(&executable, result[0], "operation_id"), "second"));
            assert!(executable.string_is(field(&executable, result[1], "operation_id"), "first"));
            assert!(
                executable.string_is(field(&executable, result[1], "contract_version"), "1.1.0")
            );
            let error = index
                .validate_variant(&manifest, function, variants[1], &op, budget)
                .unwrap_err();
            assert_compiler_error(
                error,
                "executable Operation mapping is duplicated or unqualified",
            );

            let run_case = |contract_doc: &Document, variant_ops: Vec<Value>, message| {
                let exec = decode(json!({"variants": [{"operations": variant_ops}]}));
                let mut index = OperationIndex::new(contract_doc, &exec, &op, budget).unwrap();
                let variant = items(&exec, exec.root, "variants")[0];
                let error = index
                    .validate_variant(&manifest, function, variant, &op, budget)
                    .unwrap_err();
                assert_compiler_error(error, message);
            };
            run_case(&original_contracts, vec![first.clone()], INVENTORY);
            let mut changed = second.clone();
            changed["revision_digest"] = json!(Digest::hash(b"new revision").as_str());
            run_case(
                &original_contracts,
                vec![changed, first.clone()],
                "executable Contract revision mismatch",
            );
            let missing = contracts(equivalent(), vec![contract_operation("first")]);
            run_case(
                &missing,
                vec![second.clone(), first.clone()],
                "executable Operation is not declared exactly once",
            );
            let incorrect = contracts(
                json!({"enum": [2], "additionalProperties": 0}),
                declarations(),
            );
            run_case(&incorrect, vec![second.clone(), first.clone()], SCHEMAS);
            let mut changed = second.clone();
            changed["error_schema"] = json!({"enum": [false], "additionalProperties": false});
            run_case(&original_contracts, vec![changed, first.clone()], SCHEMAS);
            let mut short = second;
            short["timeout_hard_max_ms"] = json!(3);
            run_case(
                &original_contracts,
                vec![short, first],
                "hard timeout cannot be below default",
            );
        });
    }

    fn assert_compiler_error(
        error: crate::original_authority::OriginalAuthorityError,
        expected: &str,
    ) {
        assert_eq!(
            error.message,
            "external Pack artifact digest verification failed",
        );
        assert!(error.suppressed);
        assert!(matches!(
            error.cause,
            Some(OriginalAuthorityCause::Python {
                class: "tobkiri_host.errors.InvalidArtifactError",
                ref message,
                ..
            }) if message == expected
        ));
    }
}
