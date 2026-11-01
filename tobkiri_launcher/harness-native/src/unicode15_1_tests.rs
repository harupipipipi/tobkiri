//! Portable algorithm fixtures. These do not establish original invocation
//! custody or replace the pinned CPython/Unicode conformance comparison in CI.
use super::*;
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering as AtomicOrdering},
};
use tobkiri_canon::{AllocationBudget, AllocationRequest, CoreBuffer};

struct Meter {
    remaining: u64,
}
impl Default for Meter {
    fn default() -> Self {
        Self {
            remaining: u64::MAX,
        }
    }
}
impl UnicodeWork for Meter {
    type Error = ();
    fn charge_work(&mut self, amount: u64) -> std::result::Result<(), Self::Error> {
        self.remaining = self.remaining.checked_sub(amount).ok_or(())?;
        Ok(())
    }
}

#[derive(Default)]
struct Budget {
    bytes: AtomicU64,
    refused: AtomicBool,
}
impl AllocationBudget for Budget {
    fn reserve(&self, request: AllocationRequest) -> std::result::Result<(), AllocationError> {
        if self.refused.load(AtomicOrdering::SeqCst) {
            return Err(AllocationError::Limit);
        }
        self.bytes.fetch_add(request.bytes, AtomicOrdering::SeqCst);
        Ok(())
    }
    fn release(&self, bytes: u64) {
        self.bytes.fetch_sub(bytes, AtomicOrdering::SeqCst);
    }
}

fn nfc(input: &[u32], expected: &[u32]) {
    let mut scratch = [0; 512];
    let mut work = Meter::default();
    assert_eq!(
        normalize_nfc(input, &mut scratch, &mut work).unwrap(),
        expected
    );
    assert_eq!(
        is_nfc(input, &mut scratch, &mut work).unwrap(),
        input == expected
    );
    assert!(is_nfc(expected, &mut scratch, &mut work).unwrap());
}

fn repr(input: &[u32], expected: &str) {
    let budget = Arc::new(Budget::default());
    let erased: Arc<dyn AllocationBudget> = budget.clone();
    let mut output = CoreBuffer::builder(&erased, 4096);
    write_python_repr(input, &mut output, &mut Meter::default()).unwrap();
    let output = output.finish().unwrap();
    assert_eq!(output.as_bytes(), expected.as_bytes());
    assert!(budget.bytes.load(AtomicOrdering::SeqCst) > 0);
    drop(output);
    assert_eq!(budget.bytes.load(AtomicOrdering::SeqCst), 0);
}

#[test]
fn nfc_uses_recursive_canonical_decomposition_and_composition_exclusions() {
    nfc(&[], &[]);
    nfc(&[0x65, 0x301], &[0xe9]);
    nfc(&[0xe9], &[0xe9]);
    nfc(&[0x212b], &[0xc5]);
    nfc(&[0x1e0a, 0x323], &[0x1e0c, 0x307]);
    nfc(&[0x340], &[0x300]);
    nfc(&[0x344], &[0x308, 0x301]);
    nfc(&[0x958], &[0x915, 0x93c]);
    // Compatibility decompositions are not canonical decompositions.
    nfc(&[0xfb01], &[0xfb01]);
}

#[test]
fn canonical_ordering_is_stable_and_composition_observes_blocking() {
    nfc(&[0x301, 0x323], &[0x323, 0x301]);
    nfc(&[0x41, 0x305, 0x301], &[0x41, 0x305, 0x301]);
    nfc(&[0x41, 0x316, 0x30a], &[0xc5, 0x316]);
    nfc(&[0x41, 0x30a, 0x301], &[0x1fa]);
    nfc(&[0x41, 0x30a, 0x305, 0x301], &[0xc5, 0x305, 0x301]);
}

#[test]
fn algorithmic_hangul_composition_handles_boundary_trailing_jamo_and_blockers() {
    nfc(&[0x1100, 0x1161], &[0xac00]);
    nfc(&[0x1100, 0x1161, 0x11a8], &[0xac01]);
    nfc(&[0xac00, 0x11a8], &[0xac01]);
    nfc(&[0xac01], &[0xac01]);
    nfc(&[0x1112, 0x1175, 0x11c2], &[0xd7a3]);
    nfc(&[0xd7a3], &[0xd7a3]);
    nfc(&[0x1100, 0x1161, 0x11a7], &[0xac00, 0x11a7]);
    nfc(&[0x1100, 0x301, 0x1161], &[0x1100, 0x301, 0x1161]);
}

#[test]
fn nfc_preserves_surrogates_and_noncharacters_as_code_points() {
    nfc(&[0xd800, 0xdc00], &[0xd800, 0xdc00]);
    nfc(&[0x65, 0xd800, 0x301], &[0x65, 0xd800, 0x301]);
    nfc(&[0xffff, 0x10ffff], &[0xffff, 0x10ffff]);
}

#[test]
fn lower_identity_handles_contextual_sigma_without_case_folding_or_locale_rules() {
    for input in [
        &[0x3a3][..],
        &[0x3bf, 0x3a3],
        &[0x3bf, 0x3a3, 0x3b1],
        &[0x3bf, 0x3a3, 0x301],
        &[0x3bf, 0x3a3, 0x301, 0x3b1],
        &[0xd800, 0x3bf, 0x3a3, 0xdc00],
        &[0x130],
        &[0x212a],
        &[0x1c5],
        &[0x1e9e],
        &[0x10400],
        &[0x49, 0x307],
    ] {
        assert!(lower_changes(input, &mut Meter::default()).unwrap());
    }
    for input in [
        &[][..],
        &[0x3c3],
        &[0x3c2],
        &[0x3bf, 0x3c3],
        &[0x3bf, 0x3c2, 0x3b1],
        &[0xdf],
        &[0x131],
        &[0x10428],
        &[0xd800, 0xdc00, 0x10ffff],
    ] {
        assert!(!lower_changes(input, &mut Meter::default()).unwrap());
    }
}

#[test]
fn repr_chooses_python_quotes_and_only_escapes_the_selected_quote() {
    repr(&[], "''");
    repr(&[0x27], "\"'\"");
    repr(&[0x22], "'\"'");
    repr(&[0x27, 0x22], "'\\'\"'");
    repr(&[0x27, 0x27, 0x22], "'\\'\\'\"'");
    repr(&[0x5c], "'\\\\'");
}

#[test]
fn repr_matches_control_surrogate_printable_and_escape_width_rules() {
    repr(
        &[0, 9, 10, 13, 8, 11, 12, 0x1b, 0x7f, 0x85, 0xa0],
        r"'\x00\t\n\r\x08\x0b\x0c\x1b\x7f\x85\xa0'",
    );
    repr(
        &[0x200b, 0x2028, 0x378, 0xe000, 0xd800, 0xdc00, 0x10ffff],
        r"'\u200b\u2028\u0378\ue000\ud800\udc00\U0010ffff'",
    );
    repr(&[0x20, 0xe9, 0x301, 0x65e5, 0x1f600], "' é\u{301}日😀'");
    // Unicode 15.1 additions: never use the build host's printability tables.
    repr(&[0x2ebf0], "'\u{2ebf0}'");
    repr(&[0x1fae9], r"'\U0001fae9'");
}

#[test]
fn invalid_code_points_are_refused_without_replacement_or_partial_repr() {
    let mut scratch = [0; 16];
    assert_eq!(
        is_nfc(&[0x110000], &mut scratch, &mut Meter::default()),
        Err(UnicodeError::InvalidCodePoint)
    );
    assert_eq!(
        lower_changes(&[0x41, 0x110000], &mut Meter::default()),
        Err(UnicodeError::InvalidCodePoint)
    );
    let budget: Arc<dyn AllocationBudget> = Arc::new(Budget::default());
    let mut output = CoreBuffer::builder(&budget, 4096);
    assert_eq!(
        write_python_repr(&[0x41, 0x110000], &mut output, &mut Meter::default()),
        Err(UnicodeError::InvalidCodePoint)
    );
    assert!(output.is_empty());
}

#[test]
fn scratch_and_work_exhaustion_fail_before_the_unfunded_step() {
    assert_eq!(nfc_scratch_len(usize::MAX), None);
    let mut untouched = [0xaabbccdd; 8];
    assert_eq!(
        is_nfc(&[0x65], &mut untouched, &mut Meter { remaining: 0 }),
        Err(UnicodeError::Work(()))
    );
    assert_eq!(untouched, [0xaabbccdd; 8]);
    assert_eq!(
        is_nfc(&[0xe9], &mut [], &mut Meter::default()),
        Err(UnicodeError::ScratchTooSmall)
    );
    assert_eq!(
        lower_changes(&[0x41], &mut Meter { remaining: 0 }),
        Err(UnicodeError::Work(()))
    );
    let budget: Arc<dyn AllocationBudget> = Arc::new(Budget::default());
    let mut output = CoreBuffer::builder(&budget, 4096);
    assert_eq!(
        write_python_repr(&[0x41], &mut output, &mut Meter { remaining: 0 }),
        Err(UnicodeError::Work(()))
    );
    assert!(output.is_empty());
}

#[test]
fn repr_never_escapes_writer_funding_or_capacity_failures() {
    let budget = Arc::new(Budget::default());
    let erased: Arc<dyn AllocationBudget> = budget.clone();
    let mut output = CoreBuffer::builder(&erased, 4096);
    budget.refused.store(true, AtomicOrdering::SeqCst);
    assert_eq!(
        write_python_repr(&[], &mut output, &mut Meter::default()),
        Err(UnicodeError::Allocation(AllocationError::Limit))
    );
    assert!(output.is_empty());
    assert_eq!(budget.bytes.load(AtomicOrdering::SeqCst), 0);
    budget.refused.store(false, AtomicOrdering::SeqCst);
    let mut bounded = CoreBuffer::builder(&erased, 1);
    assert_eq!(
        write_python_repr(&[], &mut bounded, &mut Meter::default()),
        Err(UnicodeError::Allocation(AllocationError::Limit))
    );
    drop((output, bounded));
    assert_eq!(budget.bytes.load(AtomicOrdering::SeqCst), 0);
}

#[test]
fn generated_tables_are_pinned_sorted_and_scratch_bound_covers_hangul() {
    assert_eq!(crate::unicode15_1_data::UNICODE_VERSION, "15.1.0");
    assert_eq!(crate::unicode15_1_data::PYTHON_VERSION, "3.13.16");
    assert!(MAX_CANONICAL_EXPANSION >= 3);
    assert!(CCC.windows(2).all(|w| w[0].0 < w[1].0));
    assert!(
        CCC.iter()
            .all(|&(cp, ccc)| cp <= MAX_CODE_POINT && ccc != 0)
    );
    assert!(CANONICAL_DECOMP_INDEX.windows(2).all(|w| w[0].0 < w[1].0));
    for &(cp, start, count) in CANONICAL_DECOMP_INDEX {
        assert!(cp <= MAX_CODE_POINT);
        assert!(count != 0 && usize::from(count) <= MAX_CANONICAL_EXPANSION);
        let entry = &DECOMP_DATA[start as usize..start as usize + usize::from(count)];
        assert!(entry.iter().all(|&cp| cp <= MAX_CODE_POINT));
    }
    assert!(
        COMPOSITION
            .windows(2)
            .all(|w| (w[0].0, w[0].1) < (w[1].0, w[1].1))
    );
    for table in [LOWER_CHANGED_RANGES, PRINTABLE_RANGES] {
        assert!(table.windows(2).all(|w| w[0].1 < w[1].0));
        assert!(
            table
                .iter()
                .all(|&(lo, hi)| lo <= hi && hi <= MAX_CODE_POINT)
        );
    }
}

#[path = "unicode15_1_ci.rs"]
mod original_reference_ci;
