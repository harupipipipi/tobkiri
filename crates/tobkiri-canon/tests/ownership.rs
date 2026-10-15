use std::sync::{
    Arc,
    atomic::{AtomicU64, AtomicUsize, Ordering},
};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, CanonValue, ControlHandle, CoreBuffer,
    CoreValue, LeasedShared, Variant,
};

struct Budget {
    limit: u64,
    used: AtomicU64,
    calls: AtomicUsize,
    fail: usize,
}
impl Budget {
    fn new(limit: u64, fail: usize) -> Arc<Self> {
        Arc::new(Self {
            limit,
            used: AtomicU64::new(0),
            calls: AtomicUsize::new(0),
            fail,
        })
    }
    fn bytes(&self) -> u64 {
        self.used.load(Ordering::SeqCst)
    }
}
impl AllocationBudget for Budget {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        let call = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
        if call == self.fail
            || request.depth > 64
            || request.elements > 1_000_000
            || request.work > 1_000_000
        {
            return Err(AllocationError::Limit);
        }
        self.used
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |old| {
                old.checked_add(request.bytes)
                    .filter(|new| *new <= self.limit)
            })
            .map_err(|_| AllocationError::Limit)?;
        Ok(())
    }
    fn release(&self, bytes: u64) {
        let old = self.used.fetch_sub(bytes, Ordering::SeqCst);
        assert!(old >= bytes, "double or premature reservation release");
    }
}
fn owner(budget: &Arc<Budget>) -> Arc<dyn AllocationBudget> {
    budget.clone()
}

#[test]
fn erased_control_projection_retains_original_drop_plan_and_clone_allocates_nothing() {
    use std::any::Any;
    trait Read: Send + Sync {
        fn value(&self) -> u64;
    }
    struct Payload {
        value: u64,
        dropped: Arc<AtomicUsize>,
    }
    impl Read for Payload {
        fn value(&self) -> u64 {
            self.value
        }
    }
    impl Drop for Payload {
        fn drop(&mut self) {
            assert_eq!(self.dropped.fetch_add(1, Ordering::SeqCst), 0);
        }
    }
    struct Release {
        budget: Arc<Budget>,
        bytes: u64,
        dropped: Arc<AtomicUsize>,
    }
    impl Drop for Release {
        fn drop(&mut self) {
            assert_eq!(self.dropped.load(Ordering::SeqCst), 1);
            self.budget.release(self.bytes);
        }
    }
    let budget = Budget::new(4096, usize::MAX);
    let dropped = Arc::new(AtomicUsize::new(0));
    let bytes = ControlHandle::<Payload>::storage_bytes::<Release>() as u64;
    budget
        .reserve(AllocationRequest {
            kind: tobkiri_canon::AllocationKind::Graph,
            bytes,
            elements: 1,
            depth: 0,
            work: 1,
        })
        .unwrap();
    let concrete = ControlHandle::try_new_precharged(
        Payload {
            value: 17,
            dropped: dropped.clone(),
        },
        Release {
            budget: budget.clone(),
            bytes,
            dropped: dropped.clone(),
        },
    )
    .unwrap();
    let before = budget.calls.load(Ordering::SeqCst);
    let erased: ControlHandle<dyn Any + Send + Sync> =
        concrete.project(|v| v as &(dyn Any + Send + Sync));
    let trait_object: ControlHandle<dyn Read> = concrete.project(|v| v as &dyn Read);
    let child: ControlHandle<u64> = concrete.project(|v| &v.value);
    let denied: Result<ControlHandle<u64>, ()> = concrete.try_project(|_| Err(()));
    assert!(denied.is_err());
    let shared = erased.clone();
    assert!(ControlHandle::ptr_eq(&shared, &erased));
    assert_eq!(shared.downcast_ref::<Payload>().unwrap().value, 17);
    assert!(shared.downcast_ref::<u64>().is_none());
    assert_eq!(budget.calls.load(Ordering::SeqCst), before);
    drop(concrete);
    drop(erased);
    drop(shared);
    assert_eq!(trait_object.value(), 17);
    drop(trait_object);
    assert_eq!(*child, 17);
    assert_eq!(dropped.load(Ordering::SeqCst), 0);
    assert_eq!(budget.bytes(), bytes);
    drop(child);
    assert_eq!(dropped.load(Ordering::SeqCst), 1);
    assert_eq!(budget.bytes(), 0);
}

#[test]
fn streaming_guest_digest_matches_all_frozen_strict_oracle_bytes_and_failures() {
    use base64::{Engine as _, engine::general_purpose::STANDARD};
    let path =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../vectors/canon/StrictV1.jsonl");
    let corpus = std::fs::read_to_string(path).unwrap();
    let mut checked = 0;
    for line in corpus.lines() {
        let case: serde_json::Value = serde_json::from_str(line).unwrap();
        let input = STANDARD
            .decode(case["input_b64"].as_str().unwrap())
            .unwrap();
        let source = Budget::new(64 * 1024, usize::MAX);
        let creator = Budget::new(0, usize::MAX);
        let parsed = CoreValue::parse_guest(&input, &owner(&source));
        if case["expect"] == "error" {
            assert!(parsed.is_err(), "oracle case {}", case["id"]);
        } else {
            let value = parsed.unwrap_or_else(|e| panic!("oracle {}: {e}", case["id"]));
            let held = source.bytes();
            let digest = value.digest_guest(&owner(&creator)).unwrap();
            assert_eq!(digest.as_str(), case["digest"].as_str().unwrap());
            assert_eq!(source.bytes(), held);
            assert_eq!(creator.bytes(), 0, "streaming digest allocates no payload");
            drop(value);
        }
        assert_eq!(source.bytes(), 0);
        checked += 1;
    }
    assert_eq!(checked, 885);
}

#[test]
fn guest_digest_and_equality_fund_complete_work_without_changing_source_owners() {
    let origin = Budget::new(8192, usize::MAX);
    let creator = Budget::new(0, usize::MAX);
    let value =
        CoreValue::parse_guest(br#"{"z":[1,true,null],"a":"borrowed"}"#, &owner(&origin)).unwrap();
    let equal =
        CoreValue::parse_guest(br#"{"a":"borrowed","z":[1,true,null]}"#, &owner(&origin)).unwrap();
    let different =
        CoreValue::parse_guest(br#"{"a":"borrowed","z":[1,false,null]}"#, &owner(&origin)).unwrap();
    let held = origin.bytes();
    let expected = value.digest_guest(&owner(&creator)).unwrap();
    assert_eq!(expected, equal.digest_guest(&owner(&creator)).unwrap());
    assert!(value.eq_guest(&equal, &owner(&creator)).unwrap());
    assert!(!value.eq_guest(&different, &owner(&creator)).unwrap());
    assert_eq!(origin.bytes(), held);
    assert_eq!(creator.bytes(), 0);

    for equal_operation in [false, true] {
        let successful = Budget::new(0, usize::MAX);
        if equal_operation {
            value.eq_guest(&equal, &owner(&successful)).unwrap();
        } else {
            value.digest_guest(&owner(&successful)).unwrap();
        }
        for failure in 1..=successful.calls.load(Ordering::SeqCst) {
            let denied = Budget::new(0, failure);
            let result = if equal_operation {
                value.eq_guest(&equal, &owner(&denied)).map(|_| ())
            } else {
                value.digest_guest(&owner(&denied)).map(|_| ())
            };
            assert_eq!(result, Err(AllocationError::Limit));
            assert_eq!(denied.bytes(), 0);
            assert_eq!(origin.bytes(), held);
        }
    }
    drop(value);
    drop(equal);
    drop(different);
    assert_eq!(origin.bytes(), 0);
}

#[test]
fn guest_digest_and_equality_reject_all_legacy_scalars_before_prefix_comparison() {
    let origin = Budget::new(8192, usize::MAX);
    let creator = Budget::new(0, usize::MAX);
    let strict = CoreValue::parse_guest(b"[false,0]", &owner(&origin)).unwrap();
    for scalar in [
        CanonValue::Float(0.0),
        CanonValue::Float(f64::NAN),
        CanonValue::Float(f64::INFINITY),
        CanonValue::BigInt("1".into()),
        CanonValue::BigInt("9007199254740992".into()),
    ] {
        // Trusted metadata may retain legacy scalars; no guest hash/compare
        // operation silently tags, narrows, or normalizes them into StrictV1.
        let legacy = CoreValue::try_from_borrowed(
            &CanonValue::Arr(vec![CanonValue::Bool(true), scalar]),
            &owner(&origin),
        )
        .unwrap();
        assert_eq!(
            legacy.digest_guest(&owner(&creator)),
            Err(AllocationError::Invalid)
        );
        assert_eq!(
            strict.eq_guest(&legacy, &owner(&creator)),
            Err(AllocationError::Invalid)
        );
        assert_eq!(
            legacy.eq_guest(&strict, &owner(&creator)),
            Err(AllocationError::Invalid)
        );
    }
    let scientific = CoreValue::parse_guest(br#""-0e0""#, &owner(&origin)).unwrap();
    let plain_zero = CoreValue::parse_guest(b"0", &owner(&origin)).unwrap();
    assert!(!scientific.eq_guest(&plain_zero, &owner(&creator)).unwrap());
    assert_ne!(
        scientific.digest_guest(&owner(&creator)).unwrap(),
        plain_zero.digest_guest(&owner(&creator)).unwrap()
    );
}

#[test]
fn producer_unwind_rewinds_uncommitted_tail_and_owned_literal_ints_are_safe() {
    let budget = Budget::new(8192, usize::MAX);
    let mut builder = CoreBuffer::builder(&owner(&budget), 64);
    builder.append(b"prefix").unwrap();
    let failed = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _ = builder.write_chunk(8, |tail| {
            tail[0] = b'X';
            panic!("test producer unwind")
        });
    }));
    assert!(failed.is_err());
    assert_eq!(builder.len(), 6);
    let bytes = builder.finish().unwrap();
    assert_eq!(bytes.as_bytes(), b"prefix");
    drop(bytes);
    assert_eq!(budget.bytes(), 0);
    for value in [
        i64::MIN,
        i64::MAX,
        -9_007_199_254_740_992,
        9_007_199_254_740_992,
    ] {
        assert!(matches!(
            CoreValue::try_from_borrowed(&CanonValue::Int(value), &owner(&budget)),
            Err(AllocationError::Invalid)
        ));
        assert_eq!(budget.bytes(), 0);
    }
    let value = CanonValue::Obj(std::collections::BTreeMap::from([
        ("z".into(), CanonValue::Int(2)),
        ("a".into(), CanonValue::Int(1)),
        ("\u{10000}".into(), CanonValue::Int(3)),
    ]));
    let graph = CoreValue::try_from_borrowed(&value, &owner(&budget)).unwrap();
    assert_eq!(graph.get_key("a").unwrap().as_i64(), Some(1));
    assert_eq!(graph.get_key("z").unwrap().as_i64(), Some(2));
    assert_eq!(graph.get_key("\u{10000}").unwrap().as_i64(), Some(3));
}
#[test]
fn leased_shared_last_reference_destroys_payload_before_release_plan() {
    struct Payload(Arc<AtomicUsize>);
    impl Drop for Payload {
        fn drop(&mut self) {
            assert_eq!(self.0.fetch_add(1, Ordering::SeqCst), 0);
        }
    }
    struct Plan {
        state: Arc<AtomicUsize>,
        budget: Arc<Budget>,
        bytes: u64,
    }
    impl Drop for Plan {
        fn drop(&mut self) {
            assert_eq!(self.state.fetch_add(1, Ordering::SeqCst), 1);
            self.budget.release(self.bytes);
        }
    }
    let state = Arc::new(AtomicUsize::new(0));
    let budget = Budget::new(4096, usize::MAX);
    let bytes = LeasedShared::<Payload, Plan>::storage_bytes() as u64;
    budget
        .reserve(AllocationRequest {
            kind: tobkiri_canon::AllocationKind::Graph,
            bytes,
            elements: 1,
            depth: 0,
            work: 1,
        })
        .unwrap();
    let value = LeasedShared::try_new_precharged(
        Payload(state.clone()),
        Plan {
            state: state.clone(),
            budget: budget.clone(),
            bytes,
        },
    )
    .unwrap();
    let other = value.clone();
    drop(value);
    assert_eq!(state.load(Ordering::SeqCst), 0);
    assert_eq!(budget.bytes(), bytes);
    drop(other);
    assert_eq!(state.load(Ordering::SeqCst), 2);
    assert_eq!(budget.bytes(), 0);
}
#[test]
fn detached_values_and_buffers_retain_storage_after_parent_owner_is_dropped() {
    let state = Budget::new(8192, usize::MAX);
    let budget = owner(&state);
    let root = CoreValue::parse_guest(br#"{"a":["retained",true]}"#, &budget).unwrap();
    let child = root.get_key("a").unwrap().get_index(0).unwrap();
    let held = state.bytes();
    drop(root);
    drop(budget);
    assert_eq!(state.bytes(), held);
    assert_eq!(child.as_str(), Some("retained"));
    let output = child.encode_guest(&owner(&state)).unwrap();
    let copy = output.share();
    drop(output);
    drop(child);
    assert_eq!(copy.as_bytes(), br#""retained""#);
    assert!(state.bytes() > 0);
    drop(copy);
    assert_eq!(state.bytes(), 0);
}
#[test]
fn shared_container_funds_only_new_metadata_and_keeps_each_original_owner() {
    let origin = Budget::new(4096, usize::MAX);
    let creator = Budget::new(4096, usize::MAX);
    let leaf = CoreValue::string("original-owner", &owner(&origin)).unwrap();
    let original = origin.bytes();
    let composite = CoreValue::array_shared(
        [Ok(leaf.share()), Ok(leaf.share())].into_iter(),
        &owner(&creator),
    )
    .unwrap();
    assert_eq!(origin.bytes(), original);
    assert!(creator.bytes() > 0);
    drop(leaf);
    let detached = composite.get_index(1).unwrap();
    drop(composite);
    assert_eq!(creator.bytes(), 0);
    assert_eq!(origin.bytes(), original);
    assert_eq!(detached.as_str(), Some("original-owner"));
    drop(detached);
    assert_eq!(origin.bytes(), 0);
}
#[test]
fn producer_builders_precharge_capacity_and_parallel_slots_keep_source_order() {
    let producer = Budget::new(8192, usize::MAX);
    let child_owner = Budget::new(8192, usize::MAX);
    let leaf = CoreValue::string("held", &owner(&child_owner)).unwrap();
    let held = child_owner.bytes();
    let mut append = CoreValue::array_builder(&owner(&producer), 2).unwrap();
    assert!(producer.bytes() > 0);
    append.push(leaf.share()).unwrap();
    append.push(leaf.share()).unwrap();
    assert!(append.push(leaf.share()).is_err());
    let values = append.finish().unwrap();
    assert_eq!(values.array().unwrap().len(), 2);
    assert_eq!(child_owner.bytes(), held);
    drop(values);
    assert_eq!(producer.bytes(), 0);
    let mut slots = CoreValue::array_builder_slots(&owner(&producer), 2).unwrap();
    slots.set(1, leaf.share()).unwrap();
    assert!(slots.set(1, leaf.share()).is_err());
    assert!(slots.set(2, leaf.share()).is_err());
    assert!(slots.finish().is_err());
    assert_eq!(producer.bytes(), 0);
    let mut slots = CoreValue::array_builder_slots(&owner(&producer), 2).unwrap();
    slots.set(1, leaf.share()).unwrap();
    slots
        .set(0, CoreValue::int(7, &owner(&child_owner)).unwrap())
        .unwrap();
    let values = slots.finish().unwrap();
    assert_eq!(values.get_index(0).unwrap().as_i64(), Some(7));
    assert_eq!(values.get_index(1).unwrap().as_str(), Some("held"));
    drop(values);
    drop(leaf);
    assert_eq!(producer.bytes(), 0);
    assert_eq!(child_owner.bytes(), 0);
    let denied = Budget::new(1, usize::MAX);
    assert!(CoreValue::array_builder(&owner(&denied), 2).is_err());
    assert!(CoreValue::array_builder_slots(&owner(&denied), 2).is_err());
    assert_eq!(denied.bytes(), 0);
}

#[test]
fn incremental_array_funds_small_start_and_growth_without_copying_child_owners() {
    let producer = Budget::new(8192, usize::MAX);
    let child_owner = Budget::new(8192, usize::MAX);
    let leaf = CoreValue::int(7, &owner(&child_owner)).unwrap();
    let held = child_owner.bytes();
    let mut append =
        CoreValue::array_builder_incremental(&owner(&producer), 4 * 1024 * 1024).unwrap();
    let initial = producer.bytes();
    assert!(initial > 0 && initial < 8192);
    for _ in 0..8 {
        append.push(leaf.share()).unwrap();
        assert_eq!(producer.bytes(), initial);
    }
    append.push(leaf.share()).unwrap();
    assert!(
        producer.bytes() > initial * 2,
        "old and new capacity remain funded"
    );
    assert_eq!(child_owner.bytes(), held);
    drop(leaf);
    let array = append.finish().unwrap();
    let detached = array.get_index(8).unwrap();
    drop(array);
    assert_eq!(producer.bytes(), 0);
    assert_eq!(child_owner.bytes(), held);
    assert_eq!(detached.as_i64(), Some(7));
    drop(detached);
    assert_eq!(child_owner.bytes(), 0);
}

#[test]
fn incremental_array_denies_growth_before_releasing_existing_storage_or_children() {
    let probe = Budget::new(8192, usize::MAX);
    let builder = CoreValue::array_builder_incremental(&owner(&probe), 1024).unwrap();
    let initial = probe.bytes();
    drop(builder);
    assert_eq!(probe.bytes(), 0);
    let producer = Budget::new(initial * 2, usize::MAX);
    let child_owner = Budget::new(8192, usize::MAX);
    let leaf = CoreValue::int(7, &owner(&child_owner)).unwrap();
    let held = child_owner.bytes();
    let mut append = CoreValue::array_builder_incremental(&owner(&producer), 1024).unwrap();
    for _ in 0..8 {
        append.push(leaf.share()).unwrap();
    }
    assert_eq!(append.push(leaf.share()), Err(AllocationError::Limit));
    assert_eq!(append.len(), 8);
    assert_eq!(producer.bytes(), initial);
    drop(leaf);
    assert_eq!(child_owner.bytes(), held);
    drop(append);
    assert_eq!(producer.bytes(), 0);
    assert_eq!(child_owner.bytes(), 0);
}

#[test]
fn incremental_array_bounds_complete_encoding_and_minimum_item_size() {
    let producer = Budget::new(8192, usize::MAX);
    let child_owner = Budget::new(8192, usize::MAX);
    let escaped = CoreValue::string("\n", &owner(&child_owner)).unwrap();
    let mut append = CoreValue::array_builder_incremental(&owner(&producer), 10).unwrap();
    append.push(escaped.share()).unwrap();
    // Each encoded child is four bytes, but two need eleven with [] and comma.
    assert_eq!(append.push(escaped.share()), Err(AllocationError::Limit));
    assert_eq!(append.len(), 1);
    let array = append.finish().unwrap();
    assert_eq!(
        array.encode_guest(&owner(&producer)).unwrap().as_bytes(),
        br#"["\n"]"#
    );
    drop(array);
    let mut append = CoreValue::array_builder_incremental(&owner(&producer), 3).unwrap();
    let zero = CoreValue::int(0, &owner(&child_owner)).unwrap();
    append.push(zero.share()).unwrap();
    assert_eq!(append.push(zero), Err(AllocationError::Limit));
    drop(append);
    assert!(matches!(
        CoreValue::array_builder_incremental(&owner(&producer), 1),
        Err(AllocationError::Limit)
    ));
    assert_eq!(producer.bytes(), 0);
}

#[test]
fn transfer_copy_reserves_destination_and_keeps_source_refs_unchanged() {
    let origin = Budget::new(4096, usize::MAX);
    let denied = Budget::new(1, usize::MAX);
    let destination = Budget::new(4096, usize::MAX);
    let source = CoreValue::string("source-data", &owner(&origin)).unwrap();
    let original = origin.bytes();
    assert!(source.try_copy_to(&owner(&denied)).is_err());
    assert_eq!(denied.bytes(), 0);
    assert_eq!(origin.bytes(), original);
    let copy = source.try_copy_to(&owner(&destination)).unwrap();
    assert!(destination.bytes() > 0);
    assert_eq!(origin.bytes(), original);
    drop(source);
    assert_eq!(origin.bytes(), 0);
    assert_eq!(copy.as_str(), Some("source-data"));
    drop(copy);
    assert_eq!(destination.bytes(), 0);
}
#[test]
fn every_parse_allocation_and_work_failure_rolls_back_only_after_partial_payload_drop() {
    let input = br#"{"z":["a","b",{"nested":"value"}],"a":true,"emoji":"\ud83d\ude80"}"#;
    let baseline = Budget::new(16384, usize::MAX);
    let parsed = CoreValue::parse_guest(input, &owner(&baseline)).unwrap();
    let points = baseline.calls.load(Ordering::SeqCst);
    drop(parsed);
    assert_eq!(baseline.bytes(), 0);
    for fail in 1..=points {
        let state = Budget::new(16384, fail);
        assert!(
            CoreValue::parse_guest(input, &owner(&state)).is_err(),
            "point {fail}"
        );
        assert_eq!(state.bytes(), 0, "partial payload leak at {fail}");
    }
}
#[test]
fn strict_guest_codec_matches_protocol_and_rejects_ambiguity_and_numeric_floats() {
    let state = Budget::new(32768, usize::MAX);
    let budget = owner(&state);
    for input in [
        br#"{"b":1,"a":"\u0000\ud83d\ude80"}"#.as_slice(),
        br#"[null,true,false,-9007199254740991]"#,
        br#"{"\ud83d\ude80":2,"\ue000":1}"#,
    ] {
        let expected = tobkiri_canon::encode(
            &tobkiri_canon::parse(input, Variant::StrictV1).unwrap(),
            Variant::StrictV1,
        )
        .unwrap();
        let value = CoreValue::parse_guest(input, &budget).unwrap();
        let encoded = value.encode_guest(&budget).unwrap();
        assert_eq!(encoded.as_bytes(), expected);
        drop(encoded);
        drop(value);
    }
    for input in [
        br#"{"a":1,"a":2}"#.as_slice(),
        br#"1.0"#,
        br#"1e0"#,
        br#"9007199254740992"#,
        br#""\ud800""#,
        br#"[1,]"#,
        br#"{"a":1,}"#,
    ] {
        assert!(CoreValue::parse_guest(input, &budget).is_err());
    }
    assert_eq!(state.bytes(), 0);
}
#[test]
fn controlled_writer_and_append_builder_enforce_limits_without_owned_escape() {
    let state = Budget::new(8192, usize::MAX);
    let budget = owner(&state);
    let mut builder = CoreBuffer::builder(&budget, 5);
    builder.append(b"ab").unwrap();
    builder.append(b"cd").unwrap();
    assert!(builder.append(b"ef").is_err());
    let buffer = builder.finish().unwrap();
    assert_eq!(buffer.as_bytes(), b"abcd");
    let value = CoreValue::string_from_utf8(&buffer, &budget).unwrap();
    assert_eq!(value.as_str(), Some("abcd"));
    drop(value);
    drop(buffer);
    let failed = CoreBuffer::write_exact(4, &budget, |bytes| {
        bytes.copy_from_slice(b"test");
        Err(AllocationError::Invalid)
    });
    assert!(failed.is_err());
    assert_eq!(state.bytes(), 0);
    assert!(CoreBuffer::write_exact(usize::MAX, &budget, |_| Ok(())).is_err());
    assert_eq!(state.bytes(), 0);
}
#[test]
fn controlled_read_is_funded_before_callback_and_checks_written_length() {
    let state = Budget::new(8192, usize::MAX);
    let budget = owner(&state);
    let mut builder = CoreBuffer::builder(&budget, 8);
    assert_eq!(
        builder
            .write_chunk(4, |tail| {
                assert!(state.bytes() >= 4);
                assert_eq!(tail.len(), 4);
                tail[..2].copy_from_slice(b"ab");
                Ok(2)
            })
            .unwrap(),
        2
    );
    assert!(builder.write_chunk(3, |_| Ok(4)).is_err());
    assert_eq!(builder.len(), 2);
    assert!(
        builder
            .write_chunk(3, |_| Err(AllocationError::Invalid))
            .is_err()
    );
    assert_eq!(builder.len(), 2);
    let called = std::cell::Cell::new(false);
    assert!(
        builder
            .write_chunk(7, |_| {
                called.set(true);
                Ok(0)
            })
            .is_err()
    );
    assert!(!called.get());
    let out = builder.finish().unwrap();
    assert_eq!(out.as_bytes(), b"ab");
    drop(out);
    assert_eq!(state.bytes(), 0);
    let denied = Budget::new(1, usize::MAX);
    let mut builder = CoreBuffer::builder(&owner(&denied), 8);
    assert!(
        builder
            .write_chunk(8, |_| {
                called.set(true);
                Ok(0)
            })
            .is_err()
    );
    assert!(!called.get());
    drop(builder);
    assert_eq!(denied.bytes(), 0);
}
#[test]
fn hostile_repeated_owned_copies_and_shared_depth_use_safe_kib_limits() {
    let state = Budget::new(2048, usize::MAX);
    let budget = owner(&state);
    let source = CoreValue::string(&"a".repeat(512), &budget).unwrap();
    let held = state.bytes();
    let result = CoreValue::array_from((0..8).map(|_| Ok(source.share())), &budget);
    assert!(result.is_err());
    assert_eq!(state.bytes(), held);
    drop(source);
    assert_eq!(state.bytes(), 0);
    let deep = CanonValue::Arr(vec![CanonValue::Arr(vec![CanonValue::Null])]);
    let value = CoreValue::try_from_borrowed(&deep, &budget).unwrap();
    drop(value);
    assert_eq!(state.bytes(), 0);
    let mut value = CoreValue::null(&owner(&Budget::new(32768, usize::MAX))).unwrap();
    let depth_state = Budget::new(32768, usize::MAX);
    let depth_budget = owner(&depth_state);
    for _ in 0..64 {
        value = CoreValue::array_shared([Ok(value)].into_iter(), &depth_budget).unwrap();
    }
    assert!(CoreValue::array_shared([Ok(value.share())].into_iter(), &depth_budget).is_err());
    drop(value);
    assert_eq!(depth_state.bytes(), 0);
}
