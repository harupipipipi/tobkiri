//! Unicode 15.1 primitives over Python's code-point strings, including surrogates.
//!
//! Tables are generated from pinned CPython 3.13.16, never the host's Unicode
//! implementation. There are no identifier, namespace or diagnostic policies
//! here. The caller retains its original invocation, prepays and verifies the
//! actual scratch allocation, and supplies its original work/deadline meter.
//! No heap allocation or owning normalized/repr string is produced here.

use crate::unicode15_1_data::{
    CANONICAL_DECOMP_INDEX, CCC, COMPOSITION, DECOMP_DATA, LOWER_CHANGED_RANGES,
    MAX_CANONICAL_EXPANSION, PRINTABLE_RANGES,
};
use std::cmp::Ordering;
use tobkiri_canon::{AllocationError, CoreBufferBuilder};

#[cfg(test)]
#[path = "unicode15_1_tests.rs"]
mod tests;

/// Metering only: implement with the existing operation's original host-work,
/// cancellation and absolute-deadline checks. This must not create a budget.
pub(crate) trait UnicodeWork {
    type Error;
    fn charge_work(&mut self, work: u64) -> std::result::Result<(), Self::Error>;
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum UnicodeError<E> {
    Work(E),
    Allocation(AllocationError),
    InvalidCodePoint,
    ScratchTooSmall,
    Overflow,
    InvalidData,
}

type Result<T, E> = std::result::Result<T, UnicodeError<E>>;

const MAX_CODE_POINT: u32 = 0x10ffff;
const S_BASE: u32 = 0xac00;
const L_BASE: u32 = 0x1100;
const V_BASE: u32 = 0x1161;
const T_BASE: u32 = 0x11a7;
const L_COUNT: u32 = 19;
const V_COUNT: u32 = 21;
const T_COUNT: u32 = 28;
const N_COUNT: u32 = V_COUNT * T_COUNT;
const S_COUNT: u32 = L_COUNT * N_COUNT;

/// Number of u32 elements (not bytes) sufficient for the complete NFD scratch.
/// Reserve before allocating, then verify the actual Vec capacity and retain
/// that reservation until its storage is physically freed. Pass only a slice
/// of that same invocation-funded allocation to `is_nfc`.
pub(crate) fn nfc_scratch_len(input_len: usize) -> Option<usize> {
    input_len.checked_mul(MAX_CANONICAL_EXPANSION)
}

/// Whether full canonical decomposition, ordering and composition are identical
/// to the input. Surrogates remain individual unchanged code points.
pub(crate) fn is_nfc<M: UnicodeWork + ?Sized>(
    input: &[u32],
    scratch: &mut [u32],
    work: &mut M,
) -> Result<bool, M::Error> {
    let normalized = normalize_nfc(input, scratch, work)?;
    charge(work, 1)?;
    if normalized.len() != input.len() {
        return Ok(false);
    }
    for (actual, original) in normalized.iter().zip(input) {
        charge(work, 1)?;
        if actual != original {
            return Ok(false);
        }
    }
    charge(work, 0)?;
    Ok(true)
}

fn normalize_nfc<'a, M: UnicodeWork + ?Sized>(
    input: &[u32],
    scratch: &'a mut [u32],
    work: &mut M,
) -> Result<&'a [u32], M::Error> {
    charge(work, 1)?;
    let required = nfc_scratch_len(input.len()).ok_or(UnicodeError::Overflow)?;
    if scratch.len() < required {
        return Err(UnicodeError::ScratchTooSmall);
    }
    let mut length = 0;
    for &cp in input {
        charge(work, 1)?;
        validate(cp)?;
        if (S_BASE..S_BASE + S_COUNT).contains(&cp) {
            charge(work, 1)?;
            let index = cp - S_BASE;
            push_ordered(scratch, &mut length, L_BASE + index / N_COUNT, work)?;
            push_ordered(
                scratch,
                &mut length,
                V_BASE + (index % N_COUNT) / T_COUNT,
                work,
            )?;
            let trailing = index % T_COUNT;
            if trailing != 0 {
                push_ordered(scratch, &mut length, T_BASE + trailing, work)?;
            }
        } else if let Some(decomposition) = canonical_decomposition(cp, work)? {
            // The generated entry is the complete recursive NFD, rather than
            // just UnicodeData's immediate decomposition. Concatenated entries
            // still require canonical ordering across their boundaries.
            for &part in decomposition {
                push_ordered(scratch, &mut length, part, work)?;
            }
        } else {
            push_ordered(scratch, &mut length, cp, work)?;
        }
    }

    // In-place canonical composition. `last_ccc` describes the previous
    // surviving character: a successfully consumed mark does not block a later
    // composition. Equal nonzero classes do block; class zero allows Hangul.
    let mut starter = None;
    let mut last_ccc = 0;
    let mut written = 0;
    for read in 0..length {
        charge(work, 1)?;
        let cp = scratch[read];
        let ccc = combining_class(cp, work)?;
        if let Some(position) = starter {
            if last_ccc == 0 || last_ccc < ccc {
                if let Some(composite) = composition(scratch[position], cp, work)? {
                    charge(work, 1)?;
                    scratch[position] = composite;
                    continue;
                }
            }
        }
        charge(work, 1)?;
        scratch[written] = cp;
        if ccc == 0 {
            starter = Some(written);
        }
        last_ccc = ccc;
        written += 1;
    }
    charge(work, 0)?;
    Ok(&scratch[..written])
}

fn push_ordered<M: UnicodeWork + ?Sized>(
    scratch: &mut [u32],
    length: &mut usize,
    cp: u32,
    work: &mut M,
) -> Result<(), M::Error> {
    charge(work, 1)?;
    let slot = scratch
        .get_mut(*length)
        .ok_or(UnicodeError::ScratchTooSmall)?;
    *slot = cp;
    let mut position = *length;
    *length += 1;
    let ccc = combining_class(cp, work)?;
    if ccc != 0 {
        while position != 0 {
            charge(work, 1)?;
            if combining_class(scratch[position - 1], work)? <= ccc {
                break;
            }
            charge(work, 1)?;
            scratch.swap(position - 1, position);
            position -= 1;
        }
    }
    Ok(())
}

fn canonical_decomposition<M: UnicodeWork + ?Sized>(
    cp: u32,
    work: &mut M,
) -> Result<Option<&'static [u32]>, M::Error> {
    let mut lo = 0;
    let mut hi = CANONICAL_DECOMP_INDEX.len();
    while lo < hi {
        charge(work, 1)?;
        let mid = lo + (hi - lo) / 2;
        let (key, start, count) = CANONICAL_DECOMP_INDEX[mid];
        match key.cmp(&cp) {
            Ordering::Less => lo = mid + 1,
            Ordering::Greater => hi = mid,
            Ordering::Equal => {
                charge(work, 1)?;
                let start = usize::try_from(start).map_err(|_| UnicodeError::InvalidData)?;
                let count = usize::from(count);
                if count == 0 || count > MAX_CANONICAL_EXPANSION {
                    return Err(UnicodeError::InvalidData);
                }
                let end = start.checked_add(count).ok_or(UnicodeError::InvalidData)?;
                return DECOMP_DATA
                    .get(start..end)
                    .map(Some)
                    .ok_or(UnicodeError::InvalidData);
            }
        }
    }
    Ok(None)
}

fn combining_class<M: UnicodeWork + ?Sized>(cp: u32, work: &mut M) -> Result<u8, M::Error> {
    let mut lo = 0;
    let mut hi = CCC.len();
    while lo < hi {
        charge(work, 1)?;
        let mid = lo + (hi - lo) / 2;
        let (key, class) = CCC[mid];
        match key.cmp(&cp) {
            Ordering::Less => lo = mid + 1,
            Ordering::Greater => hi = mid,
            Ordering::Equal => return Ok(class),
        }
    }
    Ok(0)
}

fn composition<M: UnicodeWork + ?Sized>(
    first: u32,
    second: u32,
    work: &mut M,
) -> Result<Option<u32>, M::Error> {
    charge(work, 1)?;
    if (L_BASE..L_BASE + L_COUNT).contains(&first) && (V_BASE..V_BASE + V_COUNT).contains(&second) {
        return Ok(Some(
            S_BASE + ((first - L_BASE) * V_COUNT + second - V_BASE) * T_COUNT,
        ));
    }
    if (S_BASE..S_BASE + S_COUNT).contains(&first)
        && (first - S_BASE) % T_COUNT == 0
        && (T_BASE + 1..T_BASE + T_COUNT).contains(&second)
    {
        return Ok(Some(first + second - T_BASE));
    }
    let pair = (first, second);
    let mut lo = 0;
    let mut hi = COMPOSITION.len();
    while lo < hi {
        charge(work, 1)?;
        let mid = lo + (hi - lo) / 2;
        let (a, b, composite) = COMPOSITION[mid];
        match (a, b).cmp(&pair) {
            Ordering::Less => lo = mid + 1,
            Ordering::Greater => hi = mid,
            Ordering::Equal => return Ok(Some(composite)),
        }
    }
    Ok(None)
}

/// Whether CPython's entire `str.lower()` would differ, without constructing it.
///
/// CPython 3.13.16's `lower_ucs4` has only one contextual branch: U+03A3 maps to
/// U+03C2 or U+03C3, both different from U+03A3. All other full lower mappings
/// are context independent. The generator verifies that none is empty. Thus a
/// multi-code-point mapping strictly increases total length; otherwise equality
/// is positionwise. The generated changed-character set is sufficient even for
/// final-sigma contexts; this is not an assumption from singleton tests alone.
/// Source: python/cpython v3.13.16, Objects/unicodeobject.c, lower_ucs4 and
/// handle_capital_sigma; Unicode 15.1 SpecialCasing.txt, Final_Sigma.
pub(crate) fn lower_changes<M: UnicodeWork + ?Sized>(
    input: &[u32],
    work: &mut M,
) -> Result<bool, M::Error> {
    charge(work, 0)?;
    let mut changed = false;
    // Validate every code point even if an earlier one already changes.
    for &cp in input {
        charge(work, 1)?;
        validate(cp)?;
        changed |= in_ranges(cp, LOWER_CHANGED_RANGES, work)?;
    }
    charge(work, 0)?;
    Ok(changed)
}

/// Append Python `str.__repr__` as UTF-8 into an already funded writer.
/// Quote choice, escaping and printability follow pinned CPython 3.13.16's
/// unicode_repr, not Rust Debug, JSON escaping, or the host Unicode version.
/// Lone surrogates use literal ASCII `\u` escapes and are never replaced.
pub(crate) fn write_python_repr<M: UnicodeWork + ?Sized>(
    input: &[u32],
    output: &mut CoreBufferBuilder,
    work: &mut M,
) -> Result<(), M::Error> {
    charge(work, 0)?;
    let mut single_quote = false;
    let mut double_quote = false;
    for &cp in input {
        charge(work, 1)?;
        validate(cp)?;
        single_quote |= cp == u32::from(b'\'');
        double_quote |= cp == u32::from(b'"');
    }
    let quote = if single_quote && !double_quote {
        b'"'
    } else {
        b'\''
    };
    append(output, &[quote], work)?;
    for &cp in input {
        charge(work, 1)?;
        if cp == u32::from(quote) || cp == u32::from(b'\\') {
            append(output, &[b'\\', cp as u8], work)?;
        } else if cp == 9 || cp == 10 || cp == 13 {
            let escaped = match cp {
                9 => b't',
                10 => b'n',
                _ => b'r',
            };
            append(output, &[b'\\', escaped], work)?;
        } else if (0x20..0x7f).contains(&cp)
            || (cp >= 0xa0 && in_ranges(cp, PRINTABLE_RANGES, work)?)
        {
            // Python printability excludes all surrogates. A corrupt generated
            // table fails instead of silently writing a replacement character.
            let ch = char::from_u32(cp).ok_or(UnicodeError::InvalidData)?;
            let mut encoded = [0; 4];
            append(output, ch.encode_utf8(&mut encoded).as_bytes(), work)?;
        } else {
            let mut escaped = [0; 10];
            escaped[0] = b'\\';
            let digits = if cp < 0x100 {
                escaped[1] = b'x';
                2
            } else if cp < 0x10000 {
                escaped[1] = b'u';
                4
            } else {
                escaped[1] = b'U';
                8
            };
            for index in 0..digits {
                charge(work, 1)?;
                let shift = (digits - index - 1) * 4;
                escaped[index + 2] = b"0123456789abcdef"[((cp >> shift) & 0xf) as usize];
            }
            append(output, &escaped[..digits + 2], work)?;
        }
    }
    append(output, &[quote], work)?;
    charge(work, 0)
}

fn in_ranges<M: UnicodeWork + ?Sized>(
    cp: u32,
    ranges: &[(u32, u32)],
    work: &mut M,
) -> Result<bool, M::Error> {
    let mut lo = 0;
    let mut hi = ranges.len();
    while lo < hi {
        charge(work, 1)?;
        let mid = lo + (hi - lo) / 2;
        let (first, last) = ranges[mid];
        if cp < first {
            hi = mid;
        } else if cp > last {
            lo = mid + 1;
        } else {
            return Ok(true);
        }
    }
    Ok(false)
}

fn validate<E>(cp: u32) -> Result<(), E> {
    if cp > MAX_CODE_POINT {
        Err(UnicodeError::InvalidCodePoint)
    } else {
        Ok(())
    }
}

fn append<M: UnicodeWork + ?Sized>(
    output: &mut CoreBufferBuilder,
    bytes: &[u8],
    work: &mut M,
) -> Result<(), M::Error> {
    charge(work, bytes.len() as u64)?;
    output.append(bytes).map_err(UnicodeError::Allocation)
}

fn charge<M: UnicodeWork + ?Sized>(work: &mut M, amount: u64) -> Result<(), M::Error> {
    work.charge_work(amount).map_err(UnicodeError::Work)
}
