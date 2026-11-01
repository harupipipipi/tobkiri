//! Accounting tests only: controlled DATA and an injected portable root.
//! They do not substitute for actual Guest parser/AppHandle/catalog proof.
use super::*;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use tobkiri_canon::{AllocationBudget, Digest};
use tobkiri_gate::{
    CapacityLedger, ControlKind, CoreControl, GateError, NATIVE_SETUP_MEMORY_BYTES,
    NativeSetupLease,
};

struct Portable;
impl NativeSetupLease for Portable {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"catalog-json-accounting-test-not-source-authority")
    }
}
struct Source;
impl OriginalAuthorityOwner for Source {
    fn with_original_user_data<R>(
        &self,
        operation: impl FnOnce(&std::path::Path) -> R,
    ) -> Result<R> {
        Ok(operation(std::path::Path::new("/fixture-only")))
    }
    fn check_original_owner(&self) -> Result<()> {
        Ok(())
    }
}
struct Meter {
    inner: Arc<dyn AllocationBudget>,
    work: AtomicU64,
}
impl AllocationBudget for Meter {
    fn reserve(
        &self,
        request: tobkiri_canon::AllocationRequest,
    ) -> std::result::Result<(), tobkiri_canon::AllocationError> {
        let work = request.work;
        self.inner.reserve(request)?;
        self.work.fetch_add(work, Ordering::SeqCst);
        Ok(())
    }
    fn release(&self, bytes: u64) {
        self.inner.release(bytes);
    }
}
fn with_operation(
    run: impl FnOnce(&Operation<'_, Source>, &Arc<dyn AllocationBudget>, &AtomicU64),
) {
    let control = CoreControl::default();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(Portable)))
        .unwrap();
    let invocation = account.begin().unwrap();
    let engine = tobkiri_wasm::WasmEngine::new_with_control(&control).unwrap();
    let store = engine.native_setup_store(invocation.clone(), ()).unwrap();
    let meter = Arc::new(Meter {
        inner: store.allocation_owners().graph().clone(),
        work: AtomicU64::new(0),
    });
    let budget: Arc<dyn AllocationBudget> = meter.clone();
    let cancellation = store.cancellation().clone();
    let cleanup = Arc::new(super::super::io::CleanupState::new(
        control.reserve(ControlKind::Context, 4096).unwrap(),
    ));
    let source = Source;
    let op = Operation {
        owner: &source,
        invocation: &invocation,
        cancellation: &cancellation,
        deadline: invocation.deadline().unwrap(),
        pid: std::process::id(),
        cleanup,
        fixture: None,
    };
    run(&op, &budget, &meter.work);
    drop(op);
    drop((budget, meter, store, cancellation, invocation, engine));
    tobkiri_wasm::reap_allocation_owners().unwrap();
    let remaining = account.snapshot().unwrap();
    assert_eq!((remaining.charged_bytes, remaining.reservations), (0, 0));
}
fn actual_retained_capacity(doc: &Document) -> usize {
    let mut total = doc.nodes.capacity() * std::mem::size_of::<Node>();
    for node in &doc.nodes {
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
fn same_data(left: &Document, right: &Document) {
    assert_eq!(
        (left.root, left.nodes.len()),
        (right.root, right.nodes.len())
    );
    for (left, right) in left.nodes.iter().zip(&right.nodes) {
        match (left, right) {
            (Node::String(left), Node::String(right)) => assert!(left == right),
            (Node::Array(left), Node::Array(right)) => assert_eq!(left, right),
            (Node::Object(left), Node::Object(right)) => assert!(left == right),
            (Node::Scalar(CanonValue::Float(left)), Node::Scalar(CanonValue::Float(right))) => {
                assert_eq!(left.to_bits(), right.to_bits())
            }
            (Node::Scalar(left), Node::Scalar(right)) => assert_eq!(left, right),
            _ => panic!("accounting path changed the actual decoded node type"),
        }
    }
}

#[test]
fn catalog_density_covers_actual_vec_capacities_and_preserves_legacy_data() {
    with_operation(|op, budget, work| {
        let cases = vec![
            "{}".to_owned(),
            "[null,true,false,0,-0,NaN,Infinity,-Infinity,1.25,1e400]".to_owned(),
            r#"{"duplicate":"first","duplicate":"second","q":"\\\"\ud800\udc00\n","empty":""}"#
                .to_owned(),
            r#"{"surrogate":"\ud800","pair":"\ud800\udc00"}"#.to_owned(),
            format!("[{}]", vec!["\"\""; 1024].join(",")),
            format!("[{}]", vec!["[]"; 1024].join(",")),
            format!("{}0{}", "[".repeat(48), "]".repeat(48)),
            format!("{{\"n\":{}}}", "9".repeat(4200)),
        ];
        for input in cases {
            let before_work = work.load(Ordering::SeqCst);
            let old = decode(input.as_bytes(), op).unwrap();
            let current = decode_catalog_manifest(input.as_bytes(), op, budget).unwrap();
            same_data(&old.value, &current.value);
            assert!(actual_retained_capacity(&current.value) <= current.value.storage_bound());
            assert!(work.load(Ordering::SeqCst) > before_work);
            // Historical entry keeps exactly its original reservation; only
            // the new fixed caller uses the independent density accounting.
            assert_eq!(
                old.value.storage_bound(),
                (input.len() + 1024) * legacy_allocation_unit()
            );
        }
        assert!(catalog_allocation_bound(b"").is_none());
        assert!(catalog_allocation_bound(b" \r\n\t").is_none());
        assert!(catalog_allocation_bound(b"\"unfinished\\").is_none());
    });
}

#[test]
fn long_string_density_is_funded_and_insufficient_capacity_refuses_before_decode() {
    with_operation(|op, budget, work| {
        let input = format!("\"{}\"", "x".repeat(250_000));
        assert!(decode(input.as_bytes(), op).is_err());
        let current = decode_catalog_manifest(input.as_bytes(), op, budget).unwrap();
        assert!(actual_retained_capacity(&current.value) <= current.value.storage_bound());
        assert!(current.value.storage_bound() < 16 * 1024 * 1024);
        drop(current);
        let before = op.invocation.capacity_snapshot().unwrap();
        let blocker = op
            .invocation
            .reserve(
                CapacityKind::VfsBuffer,
                NATIVE_SETUP_MEMORY_BYTES - before.charged_bytes - 1024,
            )
            .unwrap();
        let charged = op.invocation.capacity_snapshot().unwrap().charged_bytes;
        let failure = decode_catalog_manifest(input.as_bytes(), op, budget)
            .err()
            .unwrap();
        assert!(matches!(
            failure.cause,
            Some(OriginalAuthorityCause::Capacity)
        ));
        assert_eq!(
            op.invocation.capacity_snapshot().unwrap().charged_bytes,
            charged
        );
        drop(blocker);
        let consumed = work.load(Ordering::SeqCst);
        assert!(consumed > 0);
        assert!(
            budget
                .reserve(tobkiri_canon::AllocationRequest {
                    kind: tobkiri_canon::AllocationKind::Temporary,
                    bytes: 0,
                    elements: 0,
                    depth: 0,
                    work: 2_000_000_001 - consumed
                })
                .is_err(),
            "the actual delegated native work budget was reset"
        );
    });
}

#[test]
fn catalog_long_key_comparisons_debit_the_same_work_budget_before_equality() {
    with_operation(|op, budget, work| {
        let prefix = "shared".repeat(32);
        let input = format!(
            "{{{}}}",
            (0..256)
                .map(|index| format!("\"{prefix}{index:04}\":{index}"))
                .collect::<Vec<_>>()
                .join(",")
        );
        let old = decode(input.as_bytes(), op).unwrap();
        let current = decode_catalog_manifest(input.as_bytes(), op, budget).unwrap();
        same_data(&old.value, &current.value);
        assert!(
            work.load(Ordering::SeqCst) > input.len() as u64 * 16,
            "quadratic key comparison work was hidden in the linear scan charge"
        );
        drop((old, current));
        let consumed = work.load(Ordering::SeqCst);
        let available = input.len() as u64 * 8 + 1000;
        budget
            .reserve(tobkiri_canon::AllocationRequest {
                kind: tobkiri_canon::AllocationKind::Temporary,
                bytes: 0,
                elements: 0,
                depth: 0,
                work: 2_000_000_000 - consumed - available,
            })
            .unwrap();
        let before = op.invocation.capacity_snapshot().unwrap().charged_bytes;
        let failed = decode_catalog_manifest(input.as_bytes(), op, budget)
            .err()
            .unwrap();
        assert!(matches!(
            failed.cause,
            Some(OriginalAuthorityCause::Capacity)
        ));
        assert_eq!(
            op.invocation.capacity_snapshot().unwrap().charged_bytes,
            before
        );
        assert!(work.load(Ordering::SeqCst) <= 2_000_000_000);
    });
}
