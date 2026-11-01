//! Test-only epoch fuel/time observations; no backtrace or function attribution.
//!
//! The caller binds this handle to the actual Store and supplies its fuel and
//! deadline. This helper grants no authority and cannot inspect or call a guest.

use anyhow::{Context, Result};
use serde::ser::{Serialize, SerializeSeq, SerializeStruct, Serializer};
use std::{
    sync::{Mutex, MutexGuard},
    time::Instant,
};
use tobkiri_canon::LeasedShared;
use tobkiri_gate::{ControlKind, ControlReservation, CoreControl};

const MAX_RECORDS: usize = 128;
const SERIALIZATION_ALLOWANCE: u64 = 32 * 1024;

#[derive(Clone, Copy, serde::Serialize)]
struct Row {
    time_since_binding_ns: u64,
    fuel_remaining: Option<u64>,
}

struct State {
    rows: [Option<Row>; MAX_RECORDS],
    recorded: usize,
    seen: u64,
    last_fuel: Option<u64>,
    overflow: bool,
    fuel_missing: bool,
    fuel_increased: bool,
    deadline_changed: bool,
    poisoned: bool,
}

struct Inner {
    state: Mutex<State>,
    binding: Instant,
    original_deadline: Instant,
}

impl Inner {
    fn lock_state(&self) -> MutexGuard<'_, State> {
        match self.state.lock() {
            Ok(state) => state,
            Err(error) => {
                let mut state = error.into_inner();
                state.poisoned = true;
                state
            }
        }
    }
}

type Held = LeasedShared<Inner, ControlReservation>;

/// Fixed numerical diagnostic DATA funded until the last retained handle drops.
#[derive(Clone)]
pub(crate) struct EpochObservation(Held);

fn charged_bytes() -> Result<u64> {
    // Same conservative Darwin PAL mutex allowance as native_import_trace.
    // It is requested-storage accounting, not a whole-process RSS guarantee.
    u64::try_from(Held::storage_bytes())?
        .checked_add((size_of::<std::os::raw::c_long>() + 56) as u64)
        .and_then(|bytes| bytes.checked_add(SERIALIZATION_ALLOWANCE))
        .context("epoch observation storage overflow")
}

impl EpochObservation {
    /// Prepay all fixed storage and bounded serialization before allocation.
    pub(crate) fn new(control: &CoreControl, original_deadline: Instant) -> Result<Self> {
        let reservation = control.reserve(ControlKind::Frame, charged_bytes()?)?;
        let inner = Inner {
            state: Mutex::new(State {
                rows: [None; MAX_RECORDS],
                recorded: 0,
                seen: 0,
                last_fuel: None,
                overflow: false,
                fuel_missing: false,
                fuel_increased: false,
                deadline_changed: false,
                poisoned: false,
            }),
            binding: Instant::now(),
            original_deadline,
        };
        // Initialize any platform mutex storage outside the epoch callback.
        drop(
            inner
                .state
                .try_lock()
                .map_err(|_| anyhow::anyhow!("epoch observation mutex unavailable"))?,
        );
        Ok(Self(Held::try_new_precharged(inner, reservation)?))
    }

    /// Record only fixed numerical state. No allocation, IO, guest call or
    /// fallible operation is introduced into the caller's epoch callback.
    pub(crate) fn record(&self, fuel: Option<u64>, actual_deadline: Instant) {
        let inner = self.0.get();
        // Only record mutates State and it cannot unwind while holding this
        // lock. A serializer panic can poison the lock without changing State;
        // recovering that guard keeps this observation path non-fallible.
        let mut state = inner.lock_state();
        state.seen = state.seen.saturating_add(1);
        state.deadline_changed |= actual_deadline != inner.original_deadline;
        if let Some(fuel) = fuel {
            state.fuel_increased |= state.last_fuel.is_some_and(|previous| fuel > previous);
            state.last_fuel = Some(fuel);
        } else {
            state.fuel_missing = true;
        }
        // Continue checking fuel/deadline after the fixed row table fills.
        if state.recorded == MAX_RECORDS {
            state.overflow = true;
            return;
        }
        let index = state.recorded;
        state.rows[index] = Some(Row {
            time_since_binding_ns: inner.binding.elapsed().as_nanos().min(u64::MAX as u128) as u64,
            fuel_remaining: fuel,
        });
        state.recorded += 1;
    }
}

impl Serialize for EpochObservation {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        // Borrow the funded table for the complete serialization. No owned
        // snapshot escapes the handle or acquires fresh callback funding.
        let state = self.0.get().lock_state();
        let mut out = serializer.serialize_struct("EpochObservation", 14)?;
        out.serialize_field("format", "tobkiri.native-epoch-observation/1")?;
        out.serialize_field("diagnostic_only", &true)?;
        out.serialize_field("qualification", &false)?;
        out.serialize_field("captures_backtrace", &false)?;
        out.serialize_field("function_costs_classified", &false)?;
        out.serialize_field("max_records", &MAX_RECORDS)?;
        out.serialize_field("seen", &state.seen)?;
        out.serialize_field("recorded", &state.recorded)?;
        out.serialize_field("overflow", &state.overflow)?;
        out.serialize_field("fuel_missing", &state.fuel_missing)?;
        out.serialize_field("fuel_increased", &state.fuel_increased)?;
        out.serialize_field("deadline_changed", &state.deadline_changed)?;
        out.serialize_field("poisoned", &state.poisoned)?;
        out.serialize_field("records", &Rows(&state.rows[..state.recorded]))?;
        out.end()
    }
}

struct Rows<'a>(&'a [Option<Row>]);

impl Serialize for Rows<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        let mut out = serializer.serialize_seq(Some(self.0.len()))?;
        for row in self.0 {
            out.serialize_element(row.as_ref().expect("contiguous epoch observation rows"))?;
        }
        out.end()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{io::Cursor, time::Duration};
    use tobkiri_gate::CoreControlLimits;

    #[test]
    fn fixed_table_keeps_checking_fuel_and_deadline_after_overflow() {
        let control = CoreControl::default();
        let deadline = Instant::now() + Duration::from_secs(30);
        let observation = EpochObservation::new(&control, deadline).unwrap();
        for index in 0..MAX_RECORDS {
            observation.record(Some(1000 - index as u64), deadline);
        }
        observation.record(Some(872), deadline);
        observation.record(None, deadline);
        observation.record(Some(873), deadline + Duration::from_millis(1));
        let state = observation.0.get().state.lock().unwrap();
        assert_eq!(state.seen, 131);
        assert_eq!(state.recorded, MAX_RECORDS);
        assert_eq!(state.rows[127].unwrap().fuel_remaining, Some(873));
        assert_eq!(state.last_fuel, Some(873));
        assert!(state.overflow && state.fuel_missing);
        assert!(state.fuel_increased && state.deadline_changed);
    }

    #[test]
    fn equal_deadlines_and_nonincreasing_fuel_preserve_clear_flags() {
        let control = CoreControl::default();
        let deadline = Instant::now() + Duration::from_secs(30);
        let observation = EpochObservation::new(&control, deadline).unwrap();
        for fuel in [2, 1, 1, 0] {
            observation.record(Some(fuel), deadline);
        }
        let report = serde_json::to_value(&observation).unwrap();
        assert_eq!(report["recorded"], 4);
        assert_eq!(report["seen"], 4);
        for name in [
            "qualification",
            "captures_backtrace",
            "function_costs_classified",
            "overflow",
            "fuel_missing",
            "fuel_increased",
            "deadline_changed",
            "poisoned",
        ] {
            assert_eq!(report[name], false);
        }
        let rows = report["records"].as_array().unwrap();
        assert!(rows.windows(2).all(|pair| {
            pair[0]["time_since_binding_ns"].as_u64().unwrap()
                <= pair[1]["time_since_binding_ns"].as_u64().unwrap()
        }));
    }

    #[test]
    fn seen_count_saturates_without_losing_overflow_checks() {
        let control = CoreControl::default();
        let deadline = Instant::now();
        let observation = EpochObservation::new(&control, deadline).unwrap();
        for _ in 0..MAX_RECORDS {
            observation.record(Some(1), deadline);
        }
        observation.0.get().state.lock().unwrap().seen = u64::MAX;
        observation.record(Some(0), deadline);
        observation.record(Some(1), deadline);
        let state = observation.0.get().state.lock().unwrap();
        assert_eq!(state.seen, u64::MAX);
        assert_eq!(state.recorded, MAX_RECORDS);
        assert!(state.overflow && state.fuel_increased);
    }

    #[test]
    fn the_last_clone_keeps_storage_and_serialization_funded_until_drop() {
        let control = CoreControl::default();
        let before = control.usage();
        let deadline = Instant::now();
        let observation = EpochObservation::new(&control, deadline).unwrap();
        for _ in 0..MAX_RECORDS {
            observation.record(Some(u64::MAX), deadline);
        }
        let retained = observation.clone();
        let admitted = control.usage();
        assert_eq!(admitted.bytes - before.bytes, charged_bytes().unwrap());
        assert_eq!(admitted.count(ControlKind::Frame), 1);
        drop(observation);
        assert_eq!(control.usage(), admitted);
        {
            let mut bytes = [0u8; SERIALIZATION_ALLOWANCE as usize];
            let mut output = Cursor::new(bytes.as_mut_slice());
            serde_json::to_writer(&mut output, &retained).unwrap();
            assert!(output.position() > 0 && output.position() <= SERIALIZATION_ALLOWANCE);
        }
        assert_eq!(control.usage(), admitted);
        drop(retained);
        assert_eq!(control.usage(), before);
    }

    #[test]
    fn insufficient_control_capacity_denies_before_a_live_record_is_created() {
        let control = CoreControl::new(CoreControlLimits {
            total_bytes: charged_bytes().unwrap() - 1,
            ..CoreControlLimits::default()
        })
        .unwrap();
        assert!(EpochObservation::new(&control, Instant::now()).is_err());
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Frame), 0);
    }

    #[test]
    fn poisoned_serialization_lock_remains_explicit_and_recording_is_nonfallible() {
        let control = CoreControl::default();
        let deadline = Instant::now();
        let observation = EpochObservation::new(&control, deadline).unwrap();
        let failure = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _guard = observation.0.get().state.lock().unwrap();
            panic!("simulate a serializer panic without mutating numerical state");
        }));
        assert!(failure.is_err());
        observation.record(None, deadline);
        let report = serde_json::to_value(&observation).unwrap();
        assert_eq!(report["poisoned"], true);
        assert_eq!(report["fuel_missing"], true);
        assert_eq!(report["seen"], 1);
        assert_eq!(report["recorded"], 1);
    }
}
