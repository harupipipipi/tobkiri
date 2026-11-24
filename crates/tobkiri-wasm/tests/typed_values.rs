use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CoreValue,
};
use tobkiri_manifest::WitType;
use tobkiri_wasm::{AllocationOwners, core_to_val, val_to_core};
use wasmtime::component::{
    BudgetedVec, HostAllocationBudget, HostAllocationError, HostAllocationRequest, Val,
};

struct Meter(AtomicU64);
impl AllocationBudget for Meter {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                live.checked_add(request.bytes)
                    .filter(|next| *next <= 4 * 1024 * 1024)
            })
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }
    fn release(&self, bytes: u64) {
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                live.checked_sub(bytes)
            })
            .expect("released actual charged payload");
    }
}
impl HostAllocationBudget for Meter {
    fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError> {
        AllocationBudget::reserve(
            self,
            AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes: request.bytes,
                elements: request.elements,
                depth: request.depth,
                work: request.work,
            },
        )
        .map_err(|_| HostAllocationError::Limit)
    }
    fn release(&self, bytes: u64) {
        AllocationBudget::release(self, bytes)
    }
}
fn owners() -> (Arc<Meter>, AllocationOwners) {
    let meter = Arc::new(Meter(AtomicU64::new(0)));
    (
        meter.clone(),
        AllocationOwners::from_budgets(meter.clone(), meter),
    )
}

#[test]
fn finite_floats_preserve_negative_zero_and_extreme_width_bits() {
    let (meter, owners) = owners();
    for value in [
        -0.0_f32,
        f32::MIN,
        f32::MAX,
        f32::MIN_POSITIVE,
        f32::from_bits(1),
    ] {
        let graph = val_to_core(&Val::Float32(value), &WitType::F32, &owners).unwrap();
        let Val::Float32(round) = core_to_val(&graph, &WitType::F32, &owners).unwrap() else {
            panic!("wrong scalar")
        };
        assert_eq!(round.to_bits(), value.to_bits());
    }
    for value in [
        -0.0_f64,
        f64::MIN,
        f64::MAX,
        f64::MIN_POSITIVE,
        f64::from_bits(1),
    ] {
        let graph = val_to_core(&Val::Float64(value), &WitType::F64, &owners).unwrap();
        let Val::Float64(round) = core_to_val(&graph, &WitType::F64, &owners).unwrap() else {
            panic!("wrong scalar")
        };
        assert_eq!(round.to_bits(), value.to_bits());
    }
    assert_eq!(
        val_to_core(&Val::Float64(-0.0), &WitType::F64, &owners)
            .unwrap()
            .as_str(),
        Some("-0e0")
    );
    assert!(val_to_core(&Val::Float32(f32::NAN), &WitType::F32, &owners).is_err());
    assert!(val_to_core(&Val::Float64(f64::INFINITY), &WitType::F64, &owners).is_err());
    let invalid = CoreValue::string("-0.0", owners.graph()).unwrap();
    assert!(core_to_val(&invalid, &WitType::F64, &owners).is_err());
    drop(invalid);
    assert_eq!(meter.0.load(Ordering::Acquire), 0);
}

#[test]
fn byte_and_large_integer_mapping_is_lossless() {
    let (meter, owners) = owners();
    let bytes = Val::List(
        BudgetedVec::from_iter(
            [Val::U8(0), Val::U8(255), Val::U8(128)].into_iter(),
            owners.component(),
            0,
        )
        .unwrap(),
    );
    let ty = WitType::List(Box::new(WitType::U8));
    let graph = val_to_core(&bytes, &ty, &owners).unwrap();
    assert_eq!(core_to_val(&graph, &ty, &owners).unwrap(), bytes);
    let integer = val_to_core(&Val::U64(u64::MAX), &WitType::U64, &owners).unwrap();
    assert_eq!(integer.as_str(), Some("18446744073709551615"));
    assert_eq!(
        core_to_val(&integer, &WitType::U64, &owners).unwrap(),
        Val::U64(u64::MAX)
    );
    let invalid = CoreValue::int(256, owners.graph()).unwrap();
    assert!(core_to_val(&invalid, &WitType::U8, &owners).is_err());
    drop((bytes, graph, integer, invalid));
    assert_eq!(meter.0.load(Ordering::Acquire), 0);
}

#[test]
fn core_json_alias_carries_objects_without_changing_regular_strings() {
    let (meter, owners) = owners();
    let graph = CoreValue::parse_guest(br#"{"value":7}"#, owners.graph()).unwrap();
    let value = core_to_val(&graph, &WitType::Json, &owners).unwrap();
    let result = val_to_core(&value, &WitType::Json, &owners).unwrap();
    assert!(graph.eq_guest(&result, owners.graph()).unwrap());
    assert_eq!(
        val_to_core(&value, &WitType::String, &owners)
            .unwrap()
            .as_str(),
        Some("{\"value\":7}")
    );
    drop((graph, value, result));
    assert_eq!(meter.0.load(Ordering::Acquire), 0);
}
