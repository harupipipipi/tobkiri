//! One live native interaction, preserving each original permission request.
use super::*;

struct PendingReviewArray<'a> {
    values: Vec<PendingPermissionReview<'a>>,
    // Keep the actual incoming array funded through guard drop/deallocation.
    _array: ControlReservation,
}

/// Exact completed effects and their original bounded array custody. No wire,
/// clone, extraction or repeated commit API exists.
#[must_use = "Completed review must be committed or dropped"]
pub struct PreparedPermissionReviewBatch<'a> {
    approvals: Vec<PreparedPermissionApproval<'a>>,
    _array: ControlReservation,
}
impl PreparedPermissionReviewBatch<'_> {
    /// Commit all completed effects through the existing atomic batch protocol.
    pub fn commit_all(self) -> Result<(), GateError> {
        PreparedPermissionApproval::commit_all(self.approvals)
    }
    /// Hand the exact move-only batch to the existing durable coordinator.
    /// Callback failure keeps the original commit/uncertainty semantics.
    pub fn commit_all_with<E: From<GateError>>(
        self,
        commit: impl FnOnce(&PermissionApprovalBatch<'_, '_>) -> Result<(), E>,
    ) -> Result<(), E> {
        PreparedPermissionApproval::commit_all_with(self.approvals, commit)
    }
}

impl<'a> PendingPermissionReview<'a> {
    /// Keep the same native requests, validate one complete native response
    /// and finish every preparation together. This never installs authority.
    /// The caller retains exact candidate/source/view custody throughout await.
    /// Unsupported native surfaces fail closed without individual-prompt fallback.
    pub async fn request_all(
        reviews: Vec<Self>,
        content: &[u8],
    ) -> Result<PreparedPermissionReviewBatch<'a>, GateError> {
        if reviews.capacity() > 4096 {
            return Err(GateError::Limit(
                "native review array exceeds existing member bound".into(),
            ));
        }
        let first = reviews
            .first()
            .ok_or_else(|| GateError::Invalid("empty native review".into()))?;
        let gate = first.gate;
        let cx = first.cx;
        for pending in &reviews {
            if !std::ptr::eq(pending.gate, gate) || !std::ptr::eq(pending.cx, cx) {
                return Err(GateError::Denied(
                    "native review has different original custody".into(),
                ));
            }
        }
        // Native callers may supply spare Vec capacity. Admit its actual
        // retention at this asynchronous boundary, before native UI await.
        // Core also prepays construction; neither stage creates a new account.
        let bytes = reviews
            .capacity()
            .checked_mul(std::mem::size_of::<PendingPermissionReview<'_>>())
            .and_then(|n| n.checked_add(std::mem::size_of::<PendingReviewArray<'_>>()))
            .and_then(|n| u64::try_from(n).ok())
            .ok_or(GateError::Unavailable)?;
        let array = gate.control().reserve(ControlKind::Queue, bytes)?;
        let reviews = PendingReviewArray {
            values: reviews,
            _array: array,
        };
        cx.check_active()?;
        if gate.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        let count = reviews
            .values
            .iter()
            .filter(|pending| pending.prompt.is_some())
            .count();
        let mut batch = ApprovalReviewBatch::new(
            content,
            count,
            reviews
                .values
                .iter()
                .filter_map(|pending| pending.prompt.as_ref()),
            gate.control(),
        )?;
        gate.obtain_approval_review(cx, &mut batch).await?;
        let mut index = 0;
        for pending in &reviews.values {
            if let Some(prompt) = &pending.prompt {
                let answer = batch.take_answer(index, prompt)?;
                gate.accept_approval_answer(cx, prompt, prompt.snapshot_digest()?, answer)?;
                index += 1;
            }
        }
        cx.check_active()?;
        if gate.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        drop(batch);
        let count = reviews.values.len();
        let bytes = count
            .checked_mul(std::mem::size_of::<PreparedPermissionApproval<'_>>())
            .and_then(|n| n.checked_add(std::mem::size_of::<PreparedPermissionReviewBatch<'_>>()))
            .and_then(|n| u64::try_from(n).ok())
            .ok_or(GateError::Unavailable)?;
        let mut array = gate.control().reserve(ControlKind::Queue, bytes)?;
        let mut approvals = Vec::new();
        approvals
            .try_reserve_exact(count)
            .map_err(|_| GateError::Unavailable)?;
        if approvals.capacity() > count {
            array.try_grow(
                u64::try_from(
                    (approvals.capacity() - count)
                        .checked_mul(std::mem::size_of::<PreparedPermissionApproval<'_>>())
                        .ok_or(GateError::Unavailable)?,
                )
                .map_err(|_| GateError::Unavailable)?,
            )?;
        }
        for pending in reviews.values {
            approvals.push(PreparedPermissionApproval::new(
                pending.gate,
                pending.cx,
                pending.reservation,
                pending.target,
                pending.permissions,
                pending._pending,
                pending._retained,
            ));
        }
        Ok(PreparedPermissionReviewBatch {
            approvals,
            _array: array,
        })
    }
}
