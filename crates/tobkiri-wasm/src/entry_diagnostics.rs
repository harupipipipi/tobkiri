//! Closed component failure observations. These labels never grant authority,
//! change execution limits, make an uncertain effect replayable or expose a
//! guest/host error body. The original typed error remains the cause.

use anyhow::Error;
use std::time::Instant;
use tobkiri_gate::CallCx;
use wasmtime::component::{CanonicalCallStage, HostAllocationError};

/// Observe the failed call's still-owned Store without refuelling or invoking
/// the component. A typed canonical-call stage proves that call_owned reached
/// dispatch after setting this call's fuel. Early admission/lookup failures must
/// not accidentally report the previous call's remaining fuel from a warm Store.
pub(crate) fn remaining_fuel<T>(error: &Error, store: &wasmtime::Store<T>) -> Option<u64> {
    error.downcast_ref::<CanonicalCallStage>()?;
    store.get_fuel().ok()
}

fn failure_kind(error: &Error) -> &'static str {
    let allocation = error.downcast_ref::<HostAllocationError>().or_else(|| {
        error
            .chain()
            .find_map(|cause| cause.downcast_ref::<HostAllocationError>())
    });
    if let Some(allocation) = allocation {
        return match allocation {
            HostAllocationError::Unavailable => "allocation-unavailable",
            HostAllocationError::Limit => "allocation-limit",
            HostAllocationError::Overflow => "allocation-overflow",
            HostAllocationError::Depth => "allocation-depth",
            HostAllocationError::Allocator => "allocator-failure",
            HostAllocationError::Invalid => "invalid-allocation",
        };
    }
    // Sealed native uncertainty retains its existing classification and the
    // higher-priority step_error metadata. A trap context cannot replace it.
    if crate::core_imports::storage_failure_metadata(error).is_some() {
        return "execution-error";
    }
    match error.downcast_ref::<wasmtime::Trap>() {
        Some(wasmtime::Trap::OutOfFuel) => "wasm-fuel-exhausted",
        Some(wasmtime::Trap::Interrupt) => "wasm-interrupted",
        Some(_) => "wasm-trap",
        None => "execution-error",
    }
}

struct Snapshot {
    phase: &'static str,
    active: &'static str,
    wall_remaining_ms: u128,
    loop_remaining_ms: Option<u128>,
    declared_wall_ms: u64,
    owner_failure: &'static str,
    fuel_remaining: Option<u64>,
}

fn annotate_snapshot(error: Error, snapshot: Snapshot) -> Error {
    let kind = failure_kind(&error);
    let abi_stage = error
        .downcast_ref::<CanonicalCallStage>()
        .or_else(|| {
            error
                .chain()
                .find_map(|cause| cause.downcast_ref::<CanonicalCallStage>())
        })
        .map_or("unknown", |stage| stage.as_str());
    let Snapshot {
        phase,
        active,
        wall_remaining_ms,
        loop_remaining_ms,
        declared_wall_ms,
        owner_failure,
        fuel_remaining,
    } = snapshot;
    // Only closed host labels and numeric observations enter this context.
    // Do not interpolate Display/Debug of the underlying error or its chain.
    error.context(format!(
        "component phase={phase} abi_stage={abi_stage} failure={kind} context={active} wall_remaining_ms={wall_remaining_ms} loop_remaining_ms={loop_remaining_ms:?} declared_wall_ms={declared_wall_ms} owner_failure={owner_failure} fuel_remaining={fuel_remaining:?}",
    ))
}

/// Add a closed diagnostic while preserving every original typed cause.
pub(crate) fn annotate(
    error: Error,
    phase: &'static str,
    cx: &CallCx,
    owners: &crate::owned_values::ValueOwners,
    fuel_remaining: Option<u64>,
) -> Error {
    let now = Instant::now();
    annotate_snapshot(
        error,
        Snapshot {
            phase,
            active: cx
                .check_active()
                .err()
                .map_or("active", |error| error.code()),
            wall_remaining_ms: cx.deadline().saturating_duration_since(now).as_millis(),
            loop_remaining_ms: cx
                .budget()
                .map(|budget| budget.deadline().saturating_duration_since(now).as_millis()),
            declared_wall_ms: cx.limits().wall_ms,
            owner_failure: owners.allocation_failure_code(),
            fuel_remaining,
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot(fuel_remaining: Option<u64>) -> Snapshot {
        Snapshot {
            phase: "export",
            active: "active",
            wall_remaining_ms: 18_112,
            loop_remaining_ms: Some(18_112),
            declared_wall_ms: 300_000,
            owner_failure: "none",
            fuel_remaining,
        }
    }

    #[test]
    fn wrapped_typed_traps_have_closed_distinct_kinds_and_keep_the_original_cause() {
        for (trap, expected) in [
            (wasmtime::Trap::OutOfFuel, "wasm-fuel-exhausted"),
            (wasmtime::Trap::Interrupt, "wasm-interrupted"),
            (wasmtime::Trap::UnreachableCodeReached, "wasm-trap"),
        ] {
            let error = Error::new(trap)
                .context("PRIVATE GUEST OR PROVIDER BODY")
                .context(CanonicalCallStage::Guest);
            let error = annotate_snapshot(error, snapshot(Some(0)));
            let text = error.to_string();
            assert!(text.contains(&format!("failure={expected} ")));
            assert!(text.contains("abi_stage=guest"));
            assert!(text.contains("fuel_remaining=Some(0)"));
            assert!(!text.contains("PRIVATE"));
            assert_eq!(error.downcast_ref::<wasmtime::Trap>(), Some(&trap));
            assert_eq!(
                error
                    .downcast_ref::<CanonicalCallStage>()
                    .map(|stage| stage.as_str()),
                Some("guest")
            );
        }
    }

    #[test]
    fn allocation_classification_keeps_priority_over_a_trap_context() {
        for (allocation, expected) in [
            (HostAllocationError::Unavailable, "allocation-unavailable"),
            (HostAllocationError::Limit, "allocation-limit"),
            (HostAllocationError::Overflow, "allocation-overflow"),
            (HostAllocationError::Depth, "allocation-depth"),
            (HostAllocationError::Allocator, "allocator-failure"),
            (HostAllocationError::Invalid, "invalid-allocation"),
        ] {
            let error = Error::new(allocation).context(wasmtime::Trap::OutOfFuel);
            let error = annotate_snapshot(error, snapshot(Some(0)));
            assert!(error.to_string().contains(&format!("failure={expected} ")));
            assert!(!error.to_string().contains("failure=wasm-fuel-exhausted"));
            assert_eq!(
                error.downcast_ref::<HostAllocationError>(),
                Some(&allocation)
            );
        }
    }

    #[test]
    fn sealed_native_unknown_keeps_step_error_priority_and_does_not_leak_its_context() {
        for (error, expected) in [
            (
                Error::new(tobkiri_store::StoreError::CommitUnknown),
                "storage-sql-commit-unknown",
            ),
            (
                Error::new(tobkiri_store::StoreError::PendingDataMove),
                "storage-pending-data-move",
            ),
            (
                Error::new(crate::filesystem::FsError::CommitUnknown),
                "filesystem-commit-unknown",
            ),
        ] {
            let error = error
                .context("PRIVATE SQL PATH AND PARAMETERS")
                .context(wasmtime::Trap::OutOfFuel)
                .context(CanonicalCallStage::Guest);
            let error = annotate_snapshot(error, snapshot(Some(0)));
            assert!(error.to_string().contains("failure=execution-error "));
            assert_eq!(
                crate::core_imports::storage_failure_metadata(&error).map(|(code, _)| code),
                Some(expected)
            );
            let step = crate::host::step_error(error);
            assert_eq!(step.code, expected);
            assert!(!step.message.contains("PRIVATE"));
            assert!(step.message.contains("do not replay"));
        }
    }

    #[test]
    fn generic_text_cannot_impersonate_a_typed_trap_or_escape_the_closed_message() {
        let error = Error::msg("PRIVATE SQL Trap::OutOfFuel wasm-fuel-exhausted")
            .context(CanonicalCallStage::Guest);
        let error = annotate_snapshot(error, snapshot(None));
        assert!(error.to_string().contains("failure=execution-error "));
        assert!(error.to_string().contains("fuel_remaining=None"));
        assert!(!error.to_string().contains("PRIVATE"));
        assert!(error.downcast_ref::<wasmtime::Trap>().is_none());
        assert!(error.root_cause().to_string().contains("PRIVATE"));
    }

    #[test]
    fn numeric_fuel_observation_does_not_determine_failure_kind_or_change_the_limit() {
        for fuel in [None, Some(0), Some(12_345), Some(u64::MAX)] {
            let error = annotate_snapshot(Error::msg("PRIVATE"), snapshot(fuel));
            let text = error.to_string();
            assert!(text.contains("failure=execution-error "));
            assert!(text.contains(&format!("fuel_remaining={fuel:?}")));
            assert!(text.contains("declared_wall_ms=300000"));
            assert!(!text.contains("PRIVATE"));
        }
    }

    #[test]
    fn observing_fuel_is_read_only_and_uninstrumented_stores_report_none() {
        let mut config = wasmtime::Config::new();
        config.consume_fuel(true);
        let engine = wasmtime::Engine::new(&config).unwrap();
        let mut store = wasmtime::Store::new(&engine, ());
        store.set_fuel(12_345).unwrap();
        let error = Error::msg("PRIVATE").context(CanonicalCallStage::Guest);
        assert_eq!(remaining_fuel(&error, &store), Some(12_345));
        assert_eq!(remaining_fuel(&Error::msg("early admission"), &store), None);
        assert_eq!(store.get_fuel().unwrap(), 12_345);
        let engine = wasmtime::Engine::default();
        let store = wasmtime::Store::new(&engine, ());
        assert_eq!(remaining_fuel(&error, &store), None);
    }
}
