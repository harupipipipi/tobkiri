//! Fixed, copy-only samples of one actual native Setup Store.

use serde_json::{Value, json};
use std::sync::OnceLock;
use tobkiri_gate::NativeSetupInvocation;
use tobkiri_wasm::NativeDataObservation;

use crate::init_trace::STAGES;

#[derive(Clone, Copy)]
pub(super) struct Boundary {
    fuel_remaining: Option<u64>,
    elapsed_micros: u128,
}

impl Boundary {
    pub(super) fn sample(invocation: &NativeSetupInvocation, fuel_remaining: Option<u64>) -> Self {
        Self {
            fuel_remaining,
            elapsed_micros: invocation.started_at().elapsed().as_micros(),
        }
    }

    fn value(&self) -> Value {
        json!({"fuel_remaining": self.fuel_remaining, "elapsed_micros": self.elapsed_micros})
    }
}

pub(super) struct CallReturn {
    boundary: Boundary,
    post_return: bool,
    store_retired: bool,
    deadline_exceeded: bool,
}

impl CallReturn {
    pub(super) fn sample(
        invocation: &NativeSetupInvocation,
        observation: NativeDataObservation,
    ) -> Self {
        Self {
            boundary: Boundary::sample(invocation, observation.fuel_remaining),
            post_return: observation.post_return,
            store_retired: observation.store_retired,
            deadline_exceeded: observation.deadline_exceeded,
        }
    }

    fn value(&self) -> Value {
        json!({"fuel_remaining": self.boundary.fuel_remaining,
            "elapsed_micros": self.boundary.elapsed_micros,
            "post_return": self.post_return, "store_retired": self.store_retired,
            "deadline_exceeded": self.deadline_exceeded})
    }
}

#[derive(Default)]
pub(super) struct Samples {
    pub(super) before_instantiation: OnceLock<Boundary>,
    pub(super) after_instantiation: OnceLock<Boundary>,
    pub(super) inventory_import_entry: OnceLock<Boundary>,
    pub(super) init_import_entries: [OnceLock<Boundary>; STAGES.len()],
    pub(super) after_runtime_init: OnceLock<CallReturn>,
    pub(super) before_first_catalog_export: OnceLock<Boundary>,
    pub(super) after_first_catalog_export: OnceLock<CallReturn>,
}

impl Samples {
    pub(super) fn initialization_observed(&self, remaining: u64) -> bool {
        let (Some(before), Some(after), Some(inventory), Some(init)) = (
            self.before_instantiation.get(),
            self.after_instantiation.get(),
            self.inventory_import_entry.get(),
            self.after_runtime_init.get(),
        ) else {
            return false;
        };
        let ordered = |left: &Boundary, right: &Boundary| {
            left.elapsed_micros <= right.elapsed_micros
                && left
                    .fuel_remaining
                    .zip(right.fuel_remaining)
                    .is_some_and(|(left, right)| left >= right)
        };
        let mut previous = after;
        for checkpoint in &self.init_import_entries {
            let Some(current) = checkpoint.get().filter(|sample| ordered(previous, sample)) else {
                return false;
            };
            previous = current;
        }
        before.fuel_remaining == Some(2_000_000_000)
            && ordered(before, after)
            && ordered(after, inventory)
            && ordered(inventory, &init.boundary)
            && ordered(previous, &init.boundary)
            && init.boundary.fuel_remaining == Some(remaining)
            && init.post_return
            && !init.store_retired
            && !init.deadline_exceeded
    }

    pub(super) fn receipt(&self) -> Value {
        let init_import_entries: [Value; STAGES.len()] = std::array::from_fn(
            |index| json!({"stage": STAGES[index], "sample": self.init_import_entries[index].get().map(Boundary::value)}),
        );
        json!({
            "format": "io.tobkiri.native-setup-fuel-checkpoints.v1",
            "before_instantiation": self.before_instantiation.get().map(Boundary::value),
            "after_instantiation": self.after_instantiation.get().map(Boundary::value),
            "inventory_import_entry": self.inventory_import_entry.get().map(Boundary::value),
            "init_import_entries": init_import_entries,
            "after_runtime_init": self.after_runtime_init.get().map(CallReturn::value),
            "before_first_catalog_export": self.before_first_catalog_export.get().map(Boundary::value),
            "after_first_catalog_export": self.after_first_catalog_export.get().map(CallReturn::value),
        })
    }
}
