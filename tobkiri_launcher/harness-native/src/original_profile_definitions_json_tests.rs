//! Private registry accounting on a real portable NativeStore/account.
//! Original13 separately proves packaged state, locks and exception parity.
use super::catalog_funding_tests::{actual_retained_capacity, same_data, with_operation};
use super::*;
use std::sync::atomic::Ordering;
use tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES;

#[tokio::test(flavor = "current_thread")]
async fn definition_census_covers_actual_capacities_and_preserves_strict_data() {
    with_operation(|op, budget, work| {
        let cases = [
            "{}".to_owned(),
            " \r\n [null,true,false,0,-1,9007199254740991] \t".to_owned(),
            r#"{"escaped":"\\\"\n\t","unicode":"日本語😀","pair":"\ud800\udc00"}"#.to_owned(),
            format!("[{}]", vec!["\"\""; 1025].join(",")),
            format!("[{}]", vec!["{\"\":[]}"; 1025].join(",")),
            format!("{}0{}", "[".repeat(64), "]".repeat(64)),
            format!(
                "{{{}}}",
                (0..257)
                    .map(|index| format!("\"key{index}\":\"value{index}\""))
                    .collect::<Vec<_>>()
                    .join(",")
            ),
        ];
        for input in cases {
            let before_work = work.load(Ordering::SeqCst);
            let old = decode_strict(input.as_bytes(), op).unwrap();
            let current = decode_profile_definitions(input.as_bytes(), op, budget).unwrap();
            same_data(&old.value, &current.value);
            assert!(actual_retained_capacity(&current.value) <= current.value.storage_bound());
            assert!(work.load(Ordering::SeqCst) > before_work);
            assert_eq!(
                old.value.storage_bound(),
                (input.len() + 1024) * legacy_allocation_unit()
            );
            // The other private strict caller still keeps its old reservation.
            let catalog = decode_catalog_lock(input.as_bytes(), op, budget).unwrap();
            same_data(&old.value, &catalog.value);
            assert_eq!(catalog.value.storage_bound(), old.value.storage_bound());
        }
    });
}

#[tokio::test(flavor = "current_thread")]
async fn definition_census_covers_partial_allocations_and_keeps_error_priority() {
    with_operation(|op, budget, _| {
        let complete = format!("{{\"partial\":\"{}\"}}", "日本\\n\\\"".repeat(1025));
        let partial = &complete[..complete.len() - 2];
        let complete_doc = decode_profile_definitions(complete.as_bytes(), op, budget).unwrap();
        // Closing quote/brace add no string allocation. The unfinished text()
        // buffer and pending key must already be funded before either closes.
        assert!(
            definition_allocation_bound(partial.as_bytes()).unwrap()
                >= actual_retained_capacity(&complete_doc.value)
        );
        drop(complete_doc);
        let cases = [
            b"".to_vec(),
            b" \r\n\t".to_vec(),
            partial.as_bytes().to_vec(),
            b"{\"key\":\"unfinished\\".to_vec(),
            br#"{"key":"prefix\q"}"#.to_vec(),
            br#"{"key":1,"key":2}"#.to_vec(),
            br#"{"key":9007199254740992,"key":2}"#.to_vec(),
            br#"{"key":1,"key":NaN}"#.to_vec(),
            br#"{"key":"\ud800","key":2}"#.to_vec(),
            br#"{"key":"\ud800"}"#.to_vec(),
            b"1.25".to_vec(),
            b"1e400".to_vec(),
            "9".repeat(4200).into_bytes(),
            "9".repeat(4301).into_bytes(),
            format!("{}0{}", "[".repeat(65), "]".repeat(65)).into_bytes(),
            format!("{}0", "[".repeat(65)).into_bytes(),
            vec![b'"', 0xff, b'"'],
            "\u{feff}{}".as_bytes().to_vec(),
        ];
        for input in cases {
            let before = op.invocation.capacity_snapshot().unwrap().charged_bytes;
            let old = decode_strict(&input, op).err().unwrap();
            let current = decode_profile_definitions(&input, op, budget)
                .err()
                .unwrap();
            let (mut old_id, mut current_id) = (0, 0);
            assert_eq!(
                super::super::tests::graph(&current, &mut current_id),
                super::super::tests::graph(&old, &mut old_id)
            );
            assert_eq!(
                op.invocation.capacity_snapshot().unwrap().charged_bytes,
                before,
                "failed or unfinished decode leaked its actual reservation"
            );
        }
    });
}

#[tokio::test(flavor = "current_thread")]
async fn definition_large_text_is_funded_and_capacity_refuses_before_decode() {
    with_operation(|op, budget, work| {
        let input = format!("{{\"profile\":\"{}\"}}", "x".repeat(189_777));
        // This is the reached old over-reservation, not an increased limit.
        let old = decode_strict(input.as_bytes(), op).err().unwrap();
        assert!(matches!(old.cause, Some(OriginalAuthorityCause::Capacity)));
        let before = op.invocation.capacity_snapshot().unwrap().charged_bytes;
        let current = decode_profile_definitions(input.as_bytes(), op, budget).unwrap();
        assert!(actual_retained_capacity(&current.value) <= current.value.storage_bound());
        assert!(
            op.invocation.capacity_snapshot().unwrap().charged_bytes - before
                >= current.value.storage_bound() as u64
        );
        assert!(work.load(Ordering::SeqCst) >= input.len() as u64 * 8);
        drop(current);
        assert_eq!(
            op.invocation.capacity_snapshot().unwrap().charged_bytes,
            before
        );
        let blocker = op
            .invocation
            .reserve(
                CapacityKind::VfsBuffer,
                NATIVE_SETUP_MEMORY_BYTES - before - 1024,
            )
            .unwrap();
        let charged = op.invocation.capacity_snapshot().unwrap().charged_bytes;
        let failure = decode_profile_definitions(input.as_bytes(), op, budget)
            .err()
            .unwrap();
        assert!(matches!(
            failure.cause,
            Some(OriginalAuthorityCause::Capacity)
        ));
        assert_eq!(failure.stage, OriginalAuthorityStage::Audit);
        assert_eq!(
            op.invocation.capacity_snapshot().unwrap().charged_bytes,
            charged
        );
        drop(blocker);
    });
}

#[tokio::test(flavor = "current_thread")]
async fn definition_key_comparisons_exhaust_the_same_work_without_leaking_capacity() {
    with_operation(|op, budget, work| {
        let prefix = "shared".repeat(32);
        let input = format!(
            "{{{}}}",
            (0..256)
                .map(|index| format!("\"{prefix}{index:04}\":{index}"))
                .collect::<Vec<_>>()
                .join(",")
        );
        let current = decode_profile_definitions(input.as_bytes(), op, budget).unwrap();
        assert!(actual_retained_capacity(&current.value) <= current.value.storage_bound());
        assert!(work.load(Ordering::SeqCst) > input.len() as u64 * 16);
        drop(current);
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
        let failure = decode_profile_definitions(input.as_bytes(), op, budget)
            .err()
            .unwrap();
        assert!(matches!(
            failure.cause,
            Some(OriginalAuthorityCause::Capacity)
        ));
        assert_eq!(
            op.invocation.capacity_snapshot().unwrap().charged_bytes,
            before
        );
        assert!(work.load(Ordering::SeqCst) <= 2_000_000_000);
        assert!(
            budget
                .reserve(tobkiri_canon::AllocationRequest {
                    kind: tobkiri_canon::AllocationKind::Temporary,
                    bytes: 0,
                    elements: 0,
                    depth: 0,
                    work: 1001,
                })
                .is_err(),
            "the actual native work budget was reset"
        );
    });
}
