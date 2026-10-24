//! Closed borrowed runtime diagnostics, without formatting private causes.
use wasmtime::component::HostAllocationError;

pub(crate) fn failure_kind(error: &anyhow::Error) -> &'static str {
    let allocation = error
        .chain()
        .find_map(|cause| cause.downcast_ref::<HostAllocationError>());
    match allocation {
        Some(HostAllocationError::Unavailable) => "allocation-unavailable",
        Some(HostAllocationError::Limit) => "allocation-limit",
        Some(HostAllocationError::Overflow) => "allocation-overflow",
        Some(HostAllocationError::Depth) => "allocation-depth",
        Some(HostAllocationError::Allocator) => "allocator-failure",
        Some(HostAllocationError::Invalid) => "invalid-allocation",
        None => match error
            .chain()
            .find_map(|cause| cause.downcast_ref::<wasmtime::Trap>())
        {
            Some(wasmtime::Trap::OutOfFuel) => "fuel-exhausted",
            Some(wasmtime::Trap::Interrupt) => "execution-interrupted",
            _ => "execution-error",
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };

    #[test]
    fn typed_fuel_and_interrupt_causes_survive_private_context_without_string_matching() {
        for (cause, expected) in [
            (wasmtime::Trap::OutOfFuel, "fuel-exhausted"),
            (wasmtime::Trap::Interrupt, "execution-interrupted"),
            (wasmtime::Trap::UnreachableCodeReached, "execution-error"),
        ] {
            let error = anyhow::Error::new(cause).context("PRIVATE guest context");
            assert_eq!(failure_kind(&error), expected);
        }
        assert_eq!(
            failure_kind(&anyhow::anyhow!("out of fuel")),
            "execution-error"
        );
        let allocation =
            anyhow::Error::new(wasmtime::Trap::OutOfFuel).context(HostAllocationError::Limit);
        assert_eq!(failure_kind(&allocation), "allocation-limit");
    }

    #[test]
    fn private_diagnostics_are_never_formatted_or_copied() {
        struct Private(Arc<AtomicUsize>);
        impl std::fmt::Display for Private {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                self.0.fetch_add(1, Ordering::Relaxed);
                f.write_str("PRIVATE diagnostic")
            }
        }
        impl std::fmt::Debug for Private {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                self.0.fetch_add(1, Ordering::Relaxed);
                f.write_str("PRIVATE diagnostic")
            }
        }
        impl std::error::Error for Private {}
        let formats = Arc::new(AtomicUsize::new(0));
        let error = anyhow::Error::new(Private(formats.clone())).context("PRIVATE outer context");
        assert_eq!(failure_kind(&error), "execution-error");
        assert_eq!(formats.load(Ordering::Relaxed), 0);
    }
}
