//! Migration-only application policy for the original bootstrap identity.
//!
//! This is the `bootstrap_profile_id` invocation of the original
//! tobkiri_protocol.ids.validate_canonical_id, after the raw getter returned.
//! Raw getter exceptions remain outside this adapter, as in profile_capture's
//! _bootstrap_profile_id. Its caller projects an inner ProtocolError through
//! the existing application-owned ProfileResolutionDenied factory. Processing
//! failures are outer errors and must never become that domain rejection.
//!
//! The caller retains original invocation custody, supplies its original meter
//! and allocation budget, and prepays/verifies the actual scratch capacity.
//! No account, deadline, cache, owning input string or normalized copy is made.

use crate::unicode15_1::{self, UnicodeError, UnicodeWork};
use std::sync::Arc;
use tobkiri_canon::{AllocationBudget, CoreBuffer, CoreBufferBuilder};

#[cfg(test)]
#[path = "app_bootstrap_identity_tests.rs"]
mod tests;

const MAX_ID_CODEPOINTS: usize = 128;
const NON_EMPTY_STRING: &[u8] = b"bootstrap_profile_id must be a non-empty string";
const TOO_LONG: &[u8] = b"bootstrap_profile_id exceeds 128 characters";
const NOT_NORMALIZED: &[u8] = b"bootstrap_profile_id is not Unicode-normalized";
const NOT_CANONICAL_PREFIX: &[u8] = b"bootstrap_profile_id is not a canonical v4 identifier: ";
const INVALID_SYNTAX_PREFIX: &[u8] = b"bootstrap_profile_id has invalid canonical ID syntax: ";
const LONGEST_PREFIX: usize = if NOT_CANONICAL_PREFIX.len() > INVALID_SYNTAX_PREFIX.len() {
    NOT_CANONICAL_PREFIX.len()
} else {
    INVALID_SYNTAX_PREFIX.len()
};
// A Python repr code point needs at most ten UTF-8 bytes (a \Uxxxxxxxx
// escape), plus its two quotes. This is an output bound, not another budget.
const MAX_MESSAGE_BYTES: usize = LONGEST_PREFIX + 2 + MAX_ID_CODEPOINTS * 10;

/// Borrowed observation of an exact builtin kind from the actual raw getter.
/// Non-string payloads must not be inspected or rendered. For strings of at
/// most 128 code points, the complete unmodified u32 sequence is mandatory.
/// Longer strings carry no code points; 129 can stand for every longer length.
#[derive(Clone, Copy, Debug)]
pub(crate) enum BootstrapIdentityInput<'a> {
    NonString,
    String {
        length: usize,
        codepoints: &'a [u32],
    },
}

/// The exact original ProtocolError argument. Its allocation remains funded
/// through destruction. No replacement exception graph or class is invented.
#[derive(Debug)]
pub(crate) struct BootstrapProtocolError {
    pub(crate) message: CoreBuffer,
}

impl BootstrapProtocolError {
    #[cfg(test)]
    pub(crate) const CLASS: &'static str = "tobkiri_protocol.errors.ProtocolError";
    #[cfg(test)]
    pub(crate) const CODE: &'static str = "protocol_error";
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum BootstrapIdentityFailure<E> {
    InvalidTransport,
    Processing(UnicodeError<E>),
}

type Result<T, E> = std::result::Result<T, BootstrapIdentityFailure<E>>;

/// Validate without changing the original identity. Inner success is the very
/// same input slice; inner failure is only a funded original ProtocolError.
/// Meter/cancellation, allocation, invalid transport/code point and insufficient
/// scratch errors remain outer failures. This never catches raw getter errors.
pub(crate) fn validate_bootstrap_identity<'a, M: UnicodeWork + ?Sized>(
    input: BootstrapIdentityInput<'a>,
    scratch: &mut [u32],
    budget: &Arc<dyn AllocationBudget>,
    work: &mut M,
) -> Result<std::result::Result<&'a [u32], BootstrapProtocolError>, M::Error> {
    charge(work, 1)?;
    let BootstrapIdentityInput::String { length, codepoints } = input else {
        return Ok(Err(protocol_error(Reason::NonEmptyString, budget, work)?));
    };

    // These are transport invariants, not alternate canonical-ID rules. A long
    // string needs no repr or Unicode scan at the original length reachpoint.
    charge(work, 1)?;
    if length > MAX_ID_CODEPOINTS {
        if !codepoints.is_empty() {
            return Err(BootstrapIdentityFailure::InvalidTransport);
        }
        return Ok(Err(protocol_error(Reason::TooLong, budget, work)?));
    }
    if codepoints.len() != length {
        return Err(BootstrapIdentityFailure::InvalidTransport);
    }
    if length == 0 {
        return Ok(Err(protocol_error(Reason::NonEmptyString, budget, work)?));
    }

    if !unicode15_1::is_nfc(codepoints, scratch, work)
        .map_err(BootstrapIdentityFailure::Processing)?
    {
        return Ok(Err(protocol_error(Reason::NotNormalized, budget, work)?));
    }
    if unicode15_1::lower_changes(codepoints, work).map_err(BootstrapIdentityFailure::Processing)?
        || has_legacy_prefix(codepoints, work)?
    {
        return Ok(Err(protocol_error(
            Reason::NotCanonical(codepoints),
            budget,
            work,
        )?));
    }
    if !canonical_ascii_syntax(codepoints, work)? {
        return Ok(Err(protocol_error(
            Reason::InvalidSyntax(codepoints),
            budget,
            work,
        )?));
    }
    charge(work, 0)?;
    Ok(Ok(codepoints))
}

enum Reason<'a> {
    NonEmptyString,
    TooLong,
    NotNormalized,
    NotCanonical(&'a [u32]),
    InvalidSyntax(&'a [u32]),
}

fn protocol_error<M: UnicodeWork + ?Sized>(
    reason: Reason<'_>,
    budget: &Arc<dyn AllocationBudget>,
    work: &mut M,
) -> Result<BootstrapProtocolError, M::Error> {
    charge(work, 1)?;
    let mut message = CoreBuffer::builder(budget, MAX_MESSAGE_BYTES);
    let (prefix, repr) = match reason {
        Reason::NonEmptyString => (NON_EMPTY_STRING, None),
        Reason::TooLong => (TOO_LONG, None),
        Reason::NotNormalized => (NOT_NORMALIZED, None),
        Reason::NotCanonical(input) => (NOT_CANONICAL_PREFIX, Some(input)),
        Reason::InvalidSyntax(input) => (INVALID_SYNTAX_PREFIX, Some(input)),
    };
    append(&mut message, prefix, work)?;
    if let Some(input) = repr {
        unicode15_1::write_python_repr(input, &mut message, work)
            .map_err(BootstrapIdentityFailure::Processing)?;
    }
    charge(work, 1)?;
    let message = message
        .finish()
        .map_err(|error| BootstrapIdentityFailure::Processing(UnicodeError::Allocation(error)))?;
    charge(work, 0)?;
    Ok(BootstrapProtocolError { message })
}

fn has_legacy_prefix<M: UnicodeWork + ?Sized>(
    input: &[u32],
    work: &mut M,
) -> Result<bool, M::Error> {
    for prefix in [
        b"rumi.".as_slice(),
        b"rumiai.".as_slice(),
        b"viewer.".as_slice(),
        b"legacy.".as_slice(),
    ] {
        charge(work, 1)?;
        if input.len() < prefix.len() {
            continue;
        }
        let mut matches = true;
        for (&actual, &expected) in input.iter().zip(prefix) {
            charge(work, 1)?;
            if actual != u32::from(expected) {
                matches = false;
                break;
            }
        }
        if matches {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Equivalent to fullmatch of ^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$.
/// In particular a trailing newline, adjacent separators and a leading digit
/// all fail. Code points are tested directly, without lossy UTF-8 conversion.
fn canonical_ascii_syntax<M: UnicodeWork + ?Sized>(
    input: &[u32],
    work: &mut M,
) -> Result<bool, M::Error> {
    charge(work, 1)?;
    let Some((&first, rest)) = input.split_first() else {
        return Ok(false);
    };
    if !(0x61..=0x7a).contains(&first) {
        return Ok(false);
    }
    let mut separator = false;
    for &cp in rest {
        charge(work, 1)?;
        if (0x61..=0x7a).contains(&cp) || (0x30..=0x39).contains(&cp) {
            separator = false;
        } else if matches!(cp, 0x2e | 0x5f | 0x2d) && !separator {
            separator = true;
        } else {
            return Ok(false);
        }
    }
    charge(work, 1)?;
    Ok(!separator)
}

fn append<M: UnicodeWork + ?Sized>(
    output: &mut CoreBufferBuilder,
    bytes: &[u8],
    work: &mut M,
) -> Result<(), M::Error> {
    charge(work, bytes.len() as u64)?;
    output
        .append(bytes)
        .map_err(|error| BootstrapIdentityFailure::Processing(UnicodeError::Allocation(error)))
}

fn charge<M: UnicodeWork + ?Sized>(work: &mut M, amount: u64) -> Result<(), M::Error> {
    work.charge_work(amount)
        .map_err(|error| BootstrapIdentityFailure::Processing(UnicodeError::Work(error)))
}
