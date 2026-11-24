//! Original-account custody for the one native save operation's review data.
use crate::{CoreError, ProfileSavePrecondition, profile_review};
use serde::Serialize;
use std::{io::Write, mem::size_of};
use tobkiri_canon::Digest;
use tobkiri_gate::{ControlKind, ControlReservation, CoreControl, GateError};
use tobkiri_manifest::VerifiedPack;
use tobkiri_profile::Profile;

/// Drop the real array and all its original guards before its metadata charge.
pub(crate) struct FundedArray<T> {
    pub(crate) values: Vec<T>,
    _array: Option<ControlReservation>,
}
impl<T> FundedArray<T> {
    pub(crate) fn new(control: &CoreControl, count: usize) -> Result<Self, GateError> {
        if count == 0 {
            return Ok(Self {
                values: Vec::new(),
                _array: None,
            });
        }
        let bytes = count
            .checked_mul(size_of::<T>())
            .and_then(|n| n.checked_add(size_of::<Self>()))
            .and_then(|n| u64::try_from(n).ok())
            .ok_or(GateError::Unavailable)?;
        let mut array = control.reserve(ControlKind::Queue, bytes)?;
        let mut values = Vec::new();
        values
            .try_reserve_exact(count)
            .map_err(|_| GateError::Unavailable)?;
        if values.capacity() > count {
            array.try_grow(
                u64::try_from(
                    (values.capacity() - count)
                        .checked_mul(size_of::<T>())
                        .ok_or(GateError::Unavailable)?,
                )
                .map_err(|_| GateError::Unavailable)?,
            )?;
        }
        Ok(Self {
            values,
            _array: Some(array),
        })
    }
}

/// Internal native rendering bytes, never a client-provided consent envelope.
pub(crate) struct NativeProfileReviewContent {
    bytes: Vec<u8>,
    _retained: ControlReservation,
}
impl NativeProfileReviewContent {
    pub(crate) fn as_bytes(&self) -> &[u8] {
        &self.bytes
    }
}

#[derive(Serialize)]
struct Review<'a> {
    format: &'static str,
    definition: &'a Profile,
    structural: crate::ProfileReview,
    precondition: &'a ProfileSavePrecondition,
    plan_digest: &'a Digest,
}

fn review_error(error: crate::ProfileReviewError) -> CoreError {
    match error {
        crate::ProfileReviewError::Core(error) => error,
        crate::ProfileReviewError::Capacity(_) => {
            GateError::Limit("complete native review cannot fit its bounded representation".into())
                .into()
        }
    }
}

struct FixedWriter<'a> {
    bytes: &'a mut Vec<u8>,
    limit: usize,
}
impl Write for FixedWriter<'_> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if self
            .bytes
            .len()
            .checked_add(bytes.len())
            .is_none_or(|n| n > self.limit)
        {
            return Err(std::io::Error::other(
                "native review changed during capture",
            ));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

pub(crate) fn capture(
    profile: &Profile,
    packs: &[VerifiedPack],
    document: &Digest,
    plan: &Digest,
    precondition: &ProfileSavePrecondition,
    control: &CoreControl,
) -> Result<NativeProfileReviewContent, CoreError> {
    // Prepay the established bounded structural representation before building
    // it. Full definition is borrowed; no second candidate/source catalog is made.
    let mut retained = control.reserve(
        ControlKind::Queue,
        u64::try_from(
            profile_review::MAX_REVIEW_BYTES
                + size_of::<Review<'_>>()
                + size_of::<NativeProfileReviewContent>(),
        )
        .map_err(|_| GateError::Unavailable)?,
    )?;
    let structural =
        profile_review::review_verified_candidate(profile, packs, &[], document.clone())
            .map_err(review_error)?;
    let review = Review {
        format: "tobkiri.native-profile-review/1",
        definition: profile,
        structural,
        precondition,
        plan_digest: plan,
    };
    let count = profile_review::review_wire_size(&review).map_err(review_error)?;
    retained.try_grow(u64::try_from(count).map_err(|_| GateError::Unavailable)?)?;
    let mut bytes = Vec::new();
    bytes
        .try_reserve_exact(count)
        .map_err(|_| GateError::Unavailable)?;
    if bytes.capacity() > count {
        retained.try_grow(
            u64::try_from(bytes.capacity() - count).map_err(|_| GateError::Unavailable)?,
        )?;
    }
    serde_json::to_writer(
        FixedWriter {
            bytes: &mut bytes,
            limit: count,
        },
        &review,
    )
    .map_err(|_| CoreError::Invalid("native review capture changed".into()))?;
    if bytes.len() != count {
        return Err(CoreError::Invalid("native review capture changed".into()));
    }
    Ok(NativeProfileReviewContent {
        bytes,
        _retained: retained,
    })
}
