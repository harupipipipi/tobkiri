use super::*;

#[test]
fn prepay_denial_and_retained_spans_keep_actual_capacity() {
    let small = CoreControl::new(crate::CoreControlLimits {
        total_bytes: Observation::reserved_bytes() - 1,
        ..Default::default()
    })
    .unwrap();
    assert!(Observation::try_new(&small).is_err());
    assert_eq!(small.usage().bytes, 0);
    let control = CoreControl::default();
    let observation = Observation::try_new(&control).unwrap();
    let span = observation.span(Metric::RequestWall);
    drop(observation);
    assert_eq!(control.usage().bytes, Observation::reserved_bytes());
    drop(span);
    assert_eq!(control.usage().bytes, 0);
}

#[test]
fn dropped_is_not_returned_and_unobserved_is_not_zero_evidence() {
    let observation = Observation::try_new(&CoreControl::default()).unwrap();
    let pending = observation.span(Metric::RequestWall);
    let active = observation.snapshot();
    assert_eq!(active.slots[0].started, 1);
    assert_eq!(active.slots[0].returned + active.slots[0].dropped, 0);
    drop(pending);
    let snap = observation.snapshot();
    assert_eq!(snap.slots[0].returned, 0);
    assert_eq!(snap.slots[0].dropped, 1);
    let mut output = [0u8; ENCODER_BYTES];
    let mut writer = std::io::Cursor::new(output.as_mut_slice());
    snap.write_json(1, false, &mut writer).unwrap();
    let n = writer.position() as usize;
    let value: serde_json::Value = serde_json::from_slice(&output[..n]).unwrap();
    assert!(value["slots"][4].is_null());
    assert_eq!(value["request_ok"], false);
}

#[test]
fn sync_tls_restores_on_nested_none_and_panic_without_cross_request_leak() {
    let control = CoreControl::default();
    let first = Observation::try_new(&control).unwrap();
    let second = Observation::try_new(&control).unwrap();
    let outer = enter_sync(Some(&first));
    current_span(Metric::GuardFaultSelect).finish();
    let _ = std::panic::catch_unwind(|| {
        let _inner = enter_sync(Some(&second));
        current_span(Metric::GuardEpochSelect).finish();
        panic!("fixed fixture panic");
    });
    {
        let _none = enter_sync(None);
        current_span(Metric::GuardFaultSelect).finish();
    }
    current_span(Metric::GuardFaultSelect).finish();
    std::thread::scope(|scope| {
        scope
            .spawn(|| current_span(Metric::GuardFaultSelect).finish())
            .join()
            .unwrap();
    });
    drop(outer);
    current_span(Metric::GuardFaultSelect).finish();
    assert_eq!(first.snapshot().slots[5].started, 2);
    assert_eq!(second.snapshot().slots[6].started, 1);
    assert_eq!(first.snapshot().slots[6].started, 0);
}

#[test]
fn overflow_is_explicit_and_encoder_never_reports_wrapped_values_as_valid() {
    let observation = Observation::try_new(&CoreControl::default()).unwrap();
    let timer = observation.span(Metric::AuditImageScan);
    timer.add_units(u64::MAX, 4);
    timer.add_units(1, 0);
    timer.finish();
    let snapshot = observation.snapshot();
    assert!(snapshot.overflow);
    let mut bytes = [0u8; ENCODER_BYTES];
    let mut output = std::io::Cursor::new(bytes.as_mut_slice());
    snapshot.write_json(2, true, &mut output).unwrap();
    let n = output.position() as usize;
    let value: serde_json::Value = serde_json::from_slice(&bytes[..n]).unwrap();
    assert!(
        value["slots"]
            .as_array()
            .unwrap()
            .iter()
            .all(|slot| slot.is_null())
    );
}

#[test]
fn cx_descendants_share_bank_and_different_control_cannot_adopt_it() {
    use crate::{CallCx, CoreCancellation, Principal, ResourceLimits};
    use tobkiri_canon::ids::{ProfileId, RequestId};
    let control = CoreControl::default();
    let observation = Observation::try_new(&control).unwrap();
    let cx = CallCx::try_root_with_control(
        ProfileId::parse("prof_metrics").unwrap(),
        Principal::User,
        RequestId::parse("req_metrics").unwrap(),
        CoreCancellation::new_with_control(&control).unwrap(),
        Instant::now() + std::time::Duration::from_secs(60),
        ResourceLimits::default(),
        control,
    )
    .unwrap()
    .with_observation(&observation)
    .unwrap();
    let child = cx
        .try_callee(Principal::User, ResourceLimits::default())
        .unwrap();
    child
        .observation()
        .unwrap()
        .span(Metric::AllocationMetered)
        .finish();
    assert_eq!(observation.snapshot().slots[2].returned, 1);
    let other = Observation::try_new(&CoreControl::default()).unwrap();
    assert!(cx.with_observation(&other).is_err());
}

#[test]
fn elapsed_is_monotonic_numeric_and_worst_case_snapshot_fits_fixed_scratch() {
    let observation = Observation::try_new(&CoreControl::default()).unwrap();
    observation.span(Metric::ApprovalWait).finish();
    observation.span(Metric::ApprovalWait).finish();
    let slot = observation.snapshot().slots[Metric::ApprovalWait as usize];
    assert_eq!(slot.returned, 2);
    assert!(slot.max_ns <= slot.elapsed_ns);
    let snapshot = Snapshot {
        observed_mask: (1 << SLOT_COUNT) - 1,
        overflow: false,
        slots: [SlotSnapshot {
            started: u64::MAX,
            returned: u64::MAX,
            dropped: u64::MAX,
            elapsed_ns: u64::MAX,
            max_ns: u64::MAX,
            units0: u64::MAX,
            units1: u64::MAX,
        }; SLOT_COUNT],
    };
    let mut bytes = [0u8; ENCODER_BYTES];
    let mut output = std::io::Cursor::new(bytes.as_mut_slice());
    snapshot
        .write_json(9_007_199_254_740_991, false, &mut output)
        .unwrap();
    assert!(output.position() < ENCODER_BYTES as u64);
}
