//! Adapter-only fixtures. Original getter/catch edges and actual invocation
//! resource custody are separate evidence, not established by these meters.
use super::*;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use tobkiri_canon::{AllocationError, AllocationRequest};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum MeterError {
    Exhausted,
    Cancelled,
}

struct Meter {
    remaining: u64,
    reject_check: Option<MeterError>,
}
impl Default for Meter {
    fn default() -> Self {
        Self {
            remaining: u64::MAX,
            reject_check: None,
        }
    }
}
impl UnicodeWork for Meter {
    type Error = MeterError;
    fn charge_work(&mut self, amount: u64) -> std::result::Result<(), Self::Error> {
        if amount == 0 {
            if let Some(error) = self.reject_check {
                return Err(error);
            }
        }
        self.remaining = self
            .remaining
            .checked_sub(amount)
            .ok_or(MeterError::Exhausted)?;
        Ok(())
    }
}

struct Budget {
    bytes: AtomicU64,
    refused: AtomicBool,
    calls: AtomicU64,
    refuse_after: AtomicU64,
}
impl Default for Budget {
    fn default() -> Self {
        Self {
            bytes: AtomicU64::new(0),
            refused: AtomicBool::new(false),
            calls: AtomicU64::new(0),
            refuse_after: AtomicU64::new(u64::MAX),
        }
    }
}
impl AllocationBudget for Budget {
    fn reserve(&self, request: AllocationRequest) -> std::result::Result<(), AllocationError> {
        let call = self.calls.fetch_add(1, Ordering::SeqCst);
        if self.refused.load(Ordering::SeqCst) || call >= self.refuse_after.load(Ordering::SeqCst) {
            return Err(AllocationError::Limit);
        }
        self.bytes.fetch_add(request.bytes, Ordering::SeqCst);
        Ok(())
    }
    fn release(&self, bytes: u64) {
        self.bytes.fetch_sub(bytes, Ordering::SeqCst);
    }
}

fn string_input(codepoints: &[u32]) -> BootstrapIdentityInput<'_> {
    BootstrapIdentityInput::String {
        length: codepoints.len(),
        codepoints,
    }
}

fn rejection(input: BootstrapIdentityInput<'_>, expected: &str) {
    let budget = Arc::new(Budget::default());
    let erased: Arc<dyn AllocationBudget> = budget.clone();
    let mut scratch = [0; 512];
    let error = validate_bootstrap_identity(input, &mut scratch, &erased, &mut Meter::default())
        .unwrap()
        .unwrap_err();
    assert_eq!(
        BootstrapProtocolError::CLASS,
        "tobkiri_protocol.errors.ProtocolError"
    );
    assert_eq!(BootstrapProtocolError::CODE, "protocol_error");
    assert_eq!(error.message.as_bytes(), expected.as_bytes());
    assert!(budget.bytes.load(Ordering::SeqCst) > 0);
    drop(error);
    assert_eq!(budget.bytes.load(Ordering::SeqCst), 0);
}

fn text_rejection(text: &str, expected: &str) {
    let points: Vec<u32> = text.chars().map(u32::from).collect();
    rejection(string_input(&points), expected);
}

#[test]
fn canonical_success_borrows_the_exact_input_without_diagnostic_allocation() {
    let budget = Arc::new(Budget::default());
    let erased: Arc<dyn AllocationBudget> = budget.clone();
    let mut scratch = [0; 512];
    for text in ["defaults", "a", "a0129", "a.b-c_d", "legacy", "rumi-a"] {
        let original: Vec<u32> = text.chars().map(u32::from).collect();
        let accepted = validate_bootstrap_identity(
            string_input(&original),
            &mut scratch,
            &erased,
            &mut Meter::default(),
        )
        .unwrap()
        .unwrap();
        assert!(std::ptr::eq(accepted, original.as_slice()));
    }
    let longest = [0x61; 128];
    assert!(std::ptr::eq(
        validate_bootstrap_identity(
            string_input(&longest),
            &mut scratch,
            &erased,
            &mut Meter::default(),
        )
        .unwrap()
        .unwrap(),
        longest.as_slice()
    ));
    assert_eq!(budget.calls.load(Ordering::SeqCst), 0);
    assert_eq!(budget.bytes.load(Ordering::SeqCst), 0);
}

#[test]
fn type_empty_and_length_rejections_do_not_require_unicode_scratch_or_repr() {
    let budget: Arc<dyn AllocationBudget> = Arc::new(Budget::default());
    for (input, expected) in [
        (
            BootstrapIdentityInput::NonString,
            "bootstrap_profile_id must be a non-empty string",
        ),
        (
            string_input(&[]),
            "bootstrap_profile_id must be a non-empty string",
        ),
        (
            BootstrapIdentityInput::String {
                length: 129,
                codepoints: &[],
            },
            "bootstrap_profile_id exceeds 128 characters",
        ),
        (
            BootstrapIdentityInput::String {
                length: usize::MAX,
                codepoints: &[],
            },
            "bootstrap_profile_id exceeds 128 characters",
        ),
    ] {
        let error = validate_bootstrap_identity(input, &mut [], &budget, &mut Meter::default())
            .unwrap()
            .unwrap_err();
        assert_eq!(error.message.as_bytes(), expected.as_bytes());
    }
}

#[test]
fn nfc_precedes_lower_prefix_and_syntax_while_lower_and_prefix_precede_syntax() {
    for text in ["A\u{30a}", "legacy.a\u{301}", "a\u{315}\u{300}"] {
        text_rejection(text, "bootstrap_profile_id is not Unicode-normalized");
    }
    text_rejection(
        "Å",
        "bootstrap_profile_id is not a canonical v4 identifier: 'Å'",
    );
    text_rejection(
        "A/b",
        "bootstrap_profile_id is not a canonical v4 identifier: 'A/b'",
    );
    text_rejection(
        "legacy./bad",
        "bootstrap_profile_id is not a canonical v4 identifier: 'legacy./bad'",
    );
    text_rejection(
        "ος",
        "bootstrap_profile_id has invalid canonical ID syntax: 'ος'",
    );
    text_rejection(
        "İ",
        "bootstrap_profile_id is not a canonical v4 identifier: 'İ'",
    );
}

#[test]
fn legacy_prefixes_and_ascii_fullmatch_keep_the_original_rules() {
    for (text, expected) in [
        (
            "rumi.a",
            "bootstrap_profile_id is not a canonical v4 identifier: 'rumi.a'",
        ),
        (
            "rumiai.a",
            "bootstrap_profile_id is not a canonical v4 identifier: 'rumiai.a'",
        ),
        (
            "viewer.a",
            "bootstrap_profile_id is not a canonical v4 identifier: 'viewer.a'",
        ),
        (
            "legacy.a",
            "bootstrap_profile_id is not a canonical v4 identifier: 'legacy.a'",
        ),
        (
            "1a",
            "bootstrap_profile_id has invalid canonical ID syntax: '1a'",
        ),
        (
            ".a",
            "bootstrap_profile_id has invalid canonical ID syntax: '.a'",
        ),
        (
            "a.",
            "bootstrap_profile_id has invalid canonical ID syntax: 'a.'",
        ),
        (
            "a.-b",
            "bootstrap_profile_id has invalid canonical ID syntax: 'a.-b'",
        ),
        (
            "a b",
            "bootstrap_profile_id has invalid canonical ID syntax: 'a b'",
        ),
        (
            "a\n",
            r"bootstrap_profile_id has invalid canonical ID syntax: 'a\n'",
        ),
    ] {
        text_rejection(text, expected);
    }
}

#[test]
fn diagnostics_use_shared_python_repr_for_quotes_controls_and_lone_surrogates() {
    text_rejection(
        "a'b",
        r#"bootstrap_profile_id has invalid canonical ID syntax: "a'b""#,
    );
    text_rejection(
        "a'\"b",
        "bootstrap_profile_id has invalid canonical ID syntax: 'a\\'\"b'",
    );
    text_rejection(
        "a\\\0\tb",
        r"bootstrap_profile_id has invalid canonical ID syntax: 'a\\\x00\tb'",
    );
    rejection(
        string_input(&[0x61, 0xd800]),
        r"bootstrap_profile_id has invalid canonical ID syntax: 'a\ud800'",
    );
    rejection(
        string_input(&[0xd83e, 0xdded]),
        r"bootstrap_profile_id has invalid canonical ID syntax: '\ud83e\udded'",
    );
    text_rejection(
        "a🧭",
        "bootstrap_profile_id has invalid canonical ID syntax: 'a🧭'",
    );
}

#[test]
fn malformed_transport_invalid_codepoints_and_unfunded_scratch_are_outer_failures() {
    let budget = Arc::new(Budget::default());
    let erased: Arc<dyn AllocationBudget> = budget.clone();
    let mut scratch = [0; 512];
    for input in [
        BootstrapIdentityInput::String {
            length: 0,
            codepoints: &[0x61],
        },
        BootstrapIdentityInput::String {
            length: 1,
            codepoints: &[],
        },
        BootstrapIdentityInput::String {
            length: 129,
            codepoints: &[0x61],
        },
    ] {
        assert!(matches!(
            validate_bootstrap_identity(input, &mut scratch, &erased, &mut Meter::default()),
            Err(BootstrapIdentityFailure::InvalidTransport)
        ));
    }
    assert!(matches!(
        validate_bootstrap_identity(
            string_input(&[0x41, 0x110000]),
            &mut scratch,
            &erased,
            &mut Meter::default(),
        ),
        Err(BootstrapIdentityFailure::Processing(
            UnicodeError::InvalidCodePoint
        ))
    ));
    assert!(matches!(
        validate_bootstrap_identity(
            string_input(&[0x61]),
            &mut [],
            &erased,
            &mut Meter::default(),
        ),
        Err(BootstrapIdentityFailure::Processing(
            UnicodeError::ScratchTooSmall
        ))
    ));
    assert_eq!(budget.calls.load(Ordering::SeqCst), 0);
    assert_eq!(budget.bytes.load(Ordering::SeqCst), 0);
}

#[test]
fn work_cancellation_and_allocation_failures_do_not_become_protocol_errors() {
    let budget = Arc::new(Budget::default());
    let erased: Arc<dyn AllocationBudget> = budget.clone();
    assert!(matches!(
        validate_bootstrap_identity(
            BootstrapIdentityInput::NonString,
            &mut [],
            &erased,
            &mut Meter {
                remaining: 0,
                ..Meter::default()
            },
        ),
        Err(BootstrapIdentityFailure::Processing(UnicodeError::Work(
            MeterError::Exhausted
        )))
    ));
    assert_eq!(budget.calls.load(Ordering::SeqCst), 0);
    // Cancellation at the final check drops even the already finished message.
    assert!(matches!(
        validate_bootstrap_identity(
            BootstrapIdentityInput::NonString,
            &mut [],
            &erased,
            &mut Meter {
                reject_check: Some(MeterError::Cancelled),
                ..Meter::default()
            },
        ),
        Err(BootstrapIdentityFailure::Processing(UnicodeError::Work(
            MeterError::Cancelled
        )))
    ));
    assert!(budget.calls.load(Ordering::SeqCst) > 0);
    assert_eq!(budget.bytes.load(Ordering::SeqCst), 0);
    budget.refused.store(true, Ordering::SeqCst);
    assert!(matches!(
        validate_bootstrap_identity(
            BootstrapIdentityInput::NonString,
            &mut [],
            &erased,
            &mut Meter::default(),
        ),
        Err(BootstrapIdentityFailure::Processing(
            UnicodeError::Allocation(AllocationError::Limit)
        ))
    ));
    assert_eq!(budget.bytes.load(Ordering::SeqCst), 0);
    let partial = Arc::new(Budget::default());
    partial.refuse_after.store(2, Ordering::SeqCst);
    let erased_partial: Arc<dyn AllocationBudget> = partial.clone();
    assert!(matches!(
        validate_bootstrap_identity(
            string_input(&[0x61, 0xd800]),
            &mut [0; 512],
            &erased_partial,
            &mut Meter::default(),
        ),
        Err(BootstrapIdentityFailure::Processing(
            UnicodeError::Allocation(AllocationError::Limit)
        ))
    ));
    assert!(partial.calls.load(Ordering::SeqCst) > 2);
    assert_eq!(partial.bytes.load(Ordering::SeqCst), 0);
}

#[test]
fn a_shared_protocol_message_retains_funding_through_its_last_owner() {
    let budget = Arc::new(Budget::default());
    let erased: Arc<dyn AllocationBudget> = budget.clone();
    let error = validate_bootstrap_identity(
        BootstrapIdentityInput::NonString,
        &mut [],
        &erased,
        &mut Meter::default(),
    )
    .unwrap()
    .unwrap_err();
    let retained = error.message.share();
    drop(error);
    assert!(budget.bytes.load(Ordering::SeqCst) > 0);
    assert_eq!(
        retained.as_bytes(),
        b"bootstrap_profile_id must be a non-empty string"
    );
    drop(retained);
    assert_eq!(budget.bytes.load(Ordering::SeqCst), 0);
}

#[path = "app_bootstrap_identity_ci.rs"]
mod original_reference_ci;
