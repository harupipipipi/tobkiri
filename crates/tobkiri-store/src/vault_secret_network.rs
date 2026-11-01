//! The actual opaque-secret → HTTP header boundary. The original network
//! permit remains authority; caller-owned allocation funding is accounting.

use super::SecretRef;
use crate::StoreError;
use std::{sync::Arc, time::Instant};
use tobkiri_canon::{AllocationBudget, AllocationError, AllocationKind, AllocationRequest};
use tobkiri_gate::{CallCx, DispatchPermit, EffectRequest, GateError};
use zeroize::Zeroizing;

struct ScratchLease<'a> {
    budget: &'a dyn AllocationBudget,
    bytes: u64,
}
impl Drop for ScratchLease<'_> {
    fn drop(&mut self) {
        self.budget.release(self.bytes);
    }
}

fn allocation_error(error: AllocationError) -> StoreError {
    match error {
        AllocationError::Limit | AllocationError::Depth | AllocationError::Overflow => {
            StoreError::Quota
        }
        AllocationError::Invalid => StoreError::Invalid("secret header allocation".into()),
        AllocationError::Unavailable | AllocationError::Allocator => {
            StoreError::Unavailable("secret header allocation owner".into())
        }
    }
}

fn check_live(cx: &CallCx, permit: &DispatchPermit) -> Result<(), StoreError> {
    cx.check_active()?;
    if Instant::now() >= permit.deadline() {
        return Err(GateError::Deadline.into());
    }
    Ok(())
}

impl SecretRef {
    /// The existing per-value native HTTP header bound, before secret copying.
    pub const MAX_NETWORK_HEADER_BYTES: usize = 16 * 1024;

    /// Borrow a secret for the SAME live network permit. The temporary joined
    /// header is admitted to the caller's payload owner before allocation and
    /// zeroized/freed before its lease is refunded, including callback unwind.
    /// The callback must fund and retain any distinct output copy itself.
    pub fn with_network_value<R>(
        &self,
        cx: &CallCx,
        permit: &DispatchPermit,
        prefix: &str,
        budget: &Arc<dyn AllocationBudget>,
        use_value: impl FnOnce(&[u8]) -> R,
    ) -> Result<R, StoreError> {
        if self.profile != *cx.profile()
            || self.principal != *cx.principal()
            || !permit.matches_context(cx)
            || !matches!(permit.effect(), EffectRequest::Network { .. })
        {
            return Err(StoreError::Denied(
                "secret reference context/network mismatch".into(),
            ));
        }
        check_live(cx, permit)?;
        let bytes = prefix
            .len()
            .checked_add(self.bytes.0.len())
            .filter(|bytes| *bytes <= Self::MAX_NETWORK_HEADER_BYTES)
            .ok_or(StoreError::Quota)?;
        if prefix.contains(['\r', '\n'])
            || self.bytes.0.contains(&b'\r')
            || self.bytes.0.contains(&b'\n')
        {
            return Err(StoreError::Invalid("secret header contains newline".into()));
        }
        let charge = u64::try_from(bytes).map_err(|_| StoreError::Quota)?;
        budget
            .reserve(AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes: charge,
                elements: charge,
                depth: 0,
                work: charge,
            })
            .map_err(allocation_error)?;
        let _lease = ScratchLease {
            budget: budget.as_ref(),
            bytes: charge,
        };
        // Declared AFTER the lease: destruction zeroizes/frees this Vec before
        // refund on every return/unwind. No boxed owner or background cleanup.
        let mut header = Zeroizing::new(Vec::new());
        header
            .try_reserve_exact(bytes)
            .map_err(|_| allocation_error(AllocationError::Allocator))?;
        if header.capacity() != bytes {
            return Err(allocation_error(AllocationError::Unavailable));
        }
        header.extend_from_slice(prefix.as_bytes());
        header.extend_from_slice(&self.bytes.0);
        check_live(cx, permit)?;
        Ok(use_value(&header))
    }
}
