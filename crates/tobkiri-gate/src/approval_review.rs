//! Gate-owned, bounded native review and exact per-item answer slots.
use crate::{
    ApprovalAnswer, ApprovalPrompt, ControlKind, ControlReservation, CoreControl, GateError,
};
use std::mem::size_of;
use tobkiri_canon::Digest;

/// Native correspondence within this live review, never a consent token.
/// Only Gate constructs it; it has no wire or guest representation.
#[derive(Clone, PartialEq, Eq)]
pub struct ApprovalReviewItemId {
    index: usize,
    snapshot: Digest,
}

/// Borrowed immutable prompt and native-only correspondence for one item.
pub struct ApprovalReviewItem<'a> {
    id: &'a ApprovalReviewItemId,
    prompt: &'a ApprovalPrompt,
}
impl ApprovalReviewItem<'_> {
    pub fn id(&self) -> &ApprovalReviewItemId {
        self.id
    }
    pub fn prompt(&self) -> &ApprovalPrompt {
        self.prompt
    }
}

struct AnswerSlot<'a> {
    id: ApprovalReviewItemId,
    prompt: &'a ApprovalPrompt,
    answer: Option<ApprovalAnswer>,
}

/// A trusted native surface borrows this original bounded review. It can read
/// descriptive content and answer exact items once; it cannot replace prompts,
/// create IDs, clear an invalid reply, or extract an approval capability.
/// The caller retains the native candidate and this content throughout await.
pub struct ApprovalReviewBatch<'a> {
    content: &'a [u8],
    slots: Vec<AnswerSlot<'a>>,
    invalid: bool,
    // Retain through answer destruction and actual Vec deallocation.
    storage: ControlReservation,
}
impl<'a> ApprovalReviewBatch<'a> {
    pub(crate) fn new(
        content: &'a [u8],
        count: usize,
        prompts: impl Iterator<Item = &'a ApprovalPrompt>,
        control: &CoreControl,
    ) -> Result<Self, GateError> {
        if count > 4096 || content.len() > tobkiri_canon::MAX_BYTES {
            return Err(GateError::Limit(
                "native review exceeds existing bounds".into(),
            ));
        }
        let bytes = count
            .checked_mul(size_of::<AnswerSlot<'_>>())
            .and_then(|bytes| bytes.checked_add(size_of::<Self>()))
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or(GateError::Unavailable)?;
        let mut storage = control.reserve(ControlKind::Queue, bytes)?;
        let mut slots = Vec::new();
        slots
            .try_reserve_exact(count)
            .map_err(|_| GateError::Unavailable)?;
        if slots.capacity() > count {
            storage.try_grow(
                u64::try_from(
                    (slots.capacity() - count)
                        .checked_mul(size_of::<AnswerSlot<'_>>())
                        .ok_or(GateError::Unavailable)?,
                )
                .map_err(|_| GateError::Unavailable)?,
            )?;
        }
        for (index, prompt) in prompts.enumerate() {
            if index >= count {
                return Err(GateError::Invalid(
                    "native review item count differs".into(),
                ));
            }
            slots.push(AnswerSlot {
                id: ApprovalReviewItemId {
                    index,
                    snapshot: prompt.snapshot_digest()?,
                },
                prompt,
                answer: None,
            });
        }
        if slots.len() != count {
            return Err(GateError::Invalid(
                "native review item count differs".into(),
            ));
        }
        Ok(Self {
            content,
            slots,
            invalid: false,
            storage,
        })
    }

    /// Core-captured descriptive content, not client approval or an authority token.
    pub fn content(&self) -> &[u8] {
        self.content
    }

    pub fn items(&self) -> impl ExactSizeIterator<Item = ApprovalReviewItem<'_>> {
        self.slots.iter().map(|slot| ApprovalReviewItem {
            id: &slot.id,
            prompt: slot.prompt,
        })
    }

    pub fn item(&self, index: usize) -> Option<ApprovalReviewItem<'_>> {
        self.slots.get(index).map(|slot| ApprovalReviewItem {
            id: &slot.id,
            prompt: slot.prompt,
        })
    }

    /// Record one native decision for its exact item. Any duplicate, foreign
    /// or invalid response permanently rejects this whole live review, even
    /// when a buggy native caller ignores the returned error and retries.
    pub fn answer(
        &mut self,
        id: &ApprovalReviewItemId,
        answer: ApprovalAnswer,
    ) -> Result<(), GateError> {
        let matches = self
            .slots
            .get(id.index)
            .is_some_and(|slot| slot.id == *id && slot.answer.is_none());
        if self.invalid || !matches {
            self.invalid = true;
            return Err(GateError::Denied("native review response mismatch".into()));
        }
        if let ApprovalAnswer::Attestation(attestation) = &answer {
            // Fixed allowance covers the closed bounded native identity fields;
            // variable device/nonce/signature storage uses actual capacities.
            let extra = attestation
                .device
                .capacity()
                .checked_add(attestation.nonce.capacity())
                .and_then(|n| n.checked_add(attestation.signature.capacity()))
                .and_then(|n| n.checked_add(4096))
                .and_then(|n| u64::try_from(n).ok())
                .ok_or(GateError::Unavailable);
            let charged = extra.and_then(|bytes| self.storage.try_grow(bytes).map_err(Into::into));
            if let Err(error) = charged {
                self.invalid = true;
                return Err(error);
            }
        }
        self.slots[id.index].answer = Some(answer);
        Ok(())
    }

    pub(crate) fn ensure_complete(&self) -> Result<(), GateError> {
        if self.invalid || self.slots.iter().any(|slot| slot.answer.is_none()) {
            return Err(GateError::Denied(
                "native review responses incomplete or invalid".into(),
            ));
        }
        Ok(())
    }

    pub(crate) fn take_answer(
        &mut self,
        index: usize,
        prompt: &ApprovalPrompt,
    ) -> Result<ApprovalAnswer, GateError> {
        if self.invalid {
            return Err(GateError::Denied("invalid native review".into()));
        }
        let slot = self.slots.get_mut(index).ok_or(GateError::Unavailable)?;
        if slot.id.snapshot != prompt.snapshot_digest()? {
            self.invalid = true;
            return Err(GateError::Denied("native review snapshot changed".into()));
        }
        slot.answer
            .take()
            .ok_or_else(|| GateError::Denied("missing native review answer".into()))
    }
}
