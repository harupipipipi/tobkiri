use super::*;
use std::collections::VecDeque;

struct Fake {
    batches: VecDeque<Result<Vec<String>, Error>>,
    pulls: usize,
    cancels: usize,
}

impl Fake {
    fn new(batches: impl IntoIterator<Item = Result<Vec<String>, Error>>) -> Self {
        Self {
            batches: batches.into_iter().collect(),
            pulls: 0,
            cancels: 0,
        }
    }
}
impl Source for Fake {
    fn pull(&mut self) -> Result<Vec<String>, Error> {
        self.pulls += 1;
        self.batches.pop_front().expect("no extra pull or restart")
    }
    fn cancel(&mut self) {
        self.cancels += 1;
    }
}

fn frame(sequence: u64, kind: Value) -> String {
    json!({"request":"req_fixture", "plan_digest":format!("sha256:{}", "1".repeat(64)),
        "sequence":sequence,"kind":kind})
    .to_string()
}
fn batch(sequence: u64, kind: Value) -> Result<Vec<String>, Error> {
    Ok(vec![frame(sequence, kind)])
}
fn started() -> Result<Vec<String>, Error> {
    batch(1, json!({"kind":"started"}))
}
fn completed(value: Value) -> Result<Vec<String>, Error> {
    batch(2, json!({"kind":"completed","cost_units":0,"value":value}))
}
fn result(value: Value) -> Value {
    json!({"op":"status","result":value})
}
fn eof() -> Result<Vec<String>, Error> {
    Ok(vec![])
}

#[test]
fn only_completed_and_real_eof_return_the_actual_json_value_once() {
    for value in [
        json!({"run_id":"immutable","usage":{"total_tokens":30}}),
        json!(null),
        json!(42),
        json!("quoted public text"),
        json!([1, true]),
    ] {
        let mut source = Fake::new([started(), completed(result(value.clone())), eof()]);
        assert_eq!(drain(&mut source, "status").unwrap(), value.to_string());
        assert_eq!((source.pulls, source.cancels), (3, 0));
    }
}

#[test]
fn completed_is_not_success_when_core_cleanup_is_unknown() {
    let mut source = Fake::new([
        started(),
        completed(result(json!({}))),
        Err(error(ErrorCode::Internal, "storage-sql-commit-unknown")),
    ]);
    let returned = drain(&mut source, "status").unwrap_err();
    assert_eq!(returned.code, ErrorCode::Internal);
    assert_eq!(returned.message, "storage-sql-commit-unknown");
    assert_eq!((source.pulls, source.cancels), (3, 0));
}

#[test]
fn failed_terminal_preserves_all_nominal_core_errors_and_fixed_unknown_metadata() {
    for code in [
        ErrorCode::Denied,
        ErrorCode::ApprovalRequired,
        ErrorCode::Cancelled,
        ErrorCode::NotFound,
        ErrorCode::Invalid,
        ErrorCode::LimitExceeded,
        ErrorCode::Conflict,
        ErrorCode::Unavailable,
        ErrorCode::Internal,
    ] {
        for message in [
            "original bounded Core diagnostic",
            "filesystem-commit-unknown",
            "storage-sql-commit-unknown",
            "storage-pending-data-move",
        ] {
            let mut source = Fake::new([
                started(),
                batch(
                    2,
                    json!({"kind":"failed",
                "error":{"code":"unavailable","message":"public frame is not authority"}}),
                ),
                Err(error(code, message)),
            ]);
            let returned = drain(&mut source, "status").unwrap_err();
            assert_eq!((returned.code, returned.message), (code, message.into()));
            assert_eq!((source.pulls, source.cancels), (3, 0));
        }
    }
}

#[test]
fn failed_terminal_with_eof_still_cannot_return_a_result() {
    let mut source = Fake::new([
        started(),
        batch(
            2,
            json!({"kind":"failed",
        "error":{"code":"unavailable","message":"failure"}}),
        ),
        eof(),
    ]);
    assert_eq!(
        drain(&mut source, "status").unwrap_err().code,
        ErrorCode::Internal
    );
    assert_eq!((source.pulls, source.cancels), (3, 0));
}

#[test]
fn cancellation_waits_for_core_receipt_and_preserves_its_stronger_error() {
    let cancelled = || {
        batch(
            2,
            json!({"kind":"cancelled","reason":"operation cancelled"}),
        )
    };
    let mut source = Fake::new([started(), cancelled(), eof()]);
    assert_eq!(
        drain(&mut source, "status").unwrap_err().code,
        ErrorCode::Cancelled
    );
    assert_eq!(source.cancels, 0);
    let mut source = Fake::new([
        started(),
        cancelled(),
        Err(error(ErrorCode::Internal, "cleanup unknown")),
    ]);
    assert_eq!(
        drain(&mut source, "status").unwrap_err().message,
        "cleanup unknown"
    );
    assert_eq!(source.cancels, 0);
}

#[test]
fn empty_or_missing_terminal_refuses_after_eof_without_a_second_cancel() {
    for mut source in [Fake::new([eof()]), Fake::new([started(), eof()])] {
        assert_eq!(
            drain(&mut source, "status").unwrap_err().code,
            ErrorCode::Invalid
        );
        assert_eq!(source.cancels, 0);
    }
}

#[test]
fn duplicate_terminal_or_frame_after_terminal_cancels_once_without_restart() {
    for last in [
        json!({"kind":"completed","value":result(json!(null)),"cost_units":0}),
        json!({"kind":"step_started","step":"late"}),
    ] {
        let mut source = Fake::new([started(), completed(result(json!(null))), batch(3, last)]);
        assert_eq!(
            drain(&mut source, "status").unwrap_err().code,
            ErrorCode::Invalid
        );
        assert_eq!((source.pulls, source.cancels), (3, 1));
    }
}

#[test]
fn sequence_gaps_duplicates_and_cross_run_or_plan_are_local_failures() {
    for (field, value) in [
        ("sequence", json!(1)),
        ("sequence", json!(3)),
        ("request", json!("req_other")),
        ("plan_digest", json!(format!("sha256:{}", "2".repeat(64)))),
    ] {
        let mut last: Value =
            serde_json::from_str(&frame(2, json!({"kind":"step_started","step":"work"}))).unwrap();
        last[field] = value;
        let mut source = Fake::new([started(), Ok(vec![last.to_string()])]);
        assert_eq!(
            drain(&mut source, "status").unwrap_err().code,
            ErrorCode::Invalid
        );
        assert_eq!(source.cancels, 1);
    }
}

#[test]
fn first_frame_must_start_and_a_second_started_frame_is_invalid() {
    let mut source = Fake::new([completed(result(json!(null)))]);
    assert_eq!(
        drain(&mut source, "status").unwrap_err().code,
        ErrorCode::Invalid
    );
    assert_eq!(source.cancels, 1);
    let mut source = Fake::new([started(), batch(2, json!({"kind":"started"}))]);
    assert_eq!(
        drain(&mut source, "status").unwrap_err().code,
        ErrorCode::Invalid
    );
    assert_eq!(source.cancels, 1);
}

#[test]
fn malformed_or_open_envelopes_cannot_be_treated_as_progress() {
    let mut extra: Value = serde_json::from_str(&frame(1, json!({"kind":"started"}))).unwrap();
    extra["authority"] = json!("fake");
    for text in [
        "invalid JSON".into(),
        extra.to_string(),
        frame(1, json!({"kind":"started","extra":true})),
        frame(1, json!({"kind":"new_unknown_event"})),
        frame(1, json!({"kind":"step_started"})),
        frame(1, json!({"kind":"step_value","step":"missing value"})),
        frame(1, json!({"kind":"emitted","shape":"local.test.output@1.0"})),
        frame(1, json!({"kind":"completed","cost_units":0})),
    ] {
        let mut source = Fake::new([Ok(vec![text])]);
        assert_eq!(
            drain(&mut source, "status").unwrap_err().code,
            ErrorCode::Invalid
        );
        assert_eq!((source.pulls, source.cancels), (1, 1));
    }
}

#[test]
fn completed_envelope_is_checked_after_eof_without_double_encoding_or_domain_policy() {
    for value in [
        json!({"op":"other","result":{}}),
        json!({"op":"status"}),
        json!({"op":"status","result":{},"extra":true}),
        json!("stringified object"),
    ] {
        let mut source = Fake::new([started(), completed(value), eof()]);
        assert_eq!(
            drain(&mut source, "status").unwrap_err().code,
            ErrorCode::Invalid
        );
        assert_eq!((source.pulls, source.cancels), (3, 0));
    }
}

#[test]
fn frame_size_is_an_explicit_refusal_without_truncation() {
    let oversized = frame(
        2,
        json!({"kind":"step_value","step":"oversize","value":"あ".repeat(MAX_FRAME_BYTES/3)}),
    );
    assert!(oversized.len() > MAX_FRAME_BYTES);
    let mut source = Fake::new([started(), Ok(vec![oversized])]);
    assert_eq!(
        drain(&mut source, "status").unwrap_err().code,
        ErrorCode::LimitExceeded
    );
    assert_eq!(source.cancels, 1);
}

#[test]
fn cumulative_byte_and_frame_limits_cannot_be_extended_by_progress() {
    let mut state = Drain {
        identity: None,
        next: 1,
        bytes: MAX_TOTAL_BYTES,
        frames: 0,
        terminal: None,
    };
    assert_eq!(
        state
            .accept(&frame(1, json!({"kind":"started"})))
            .unwrap_err()
            .code,
        ErrorCode::LimitExceeded
    );
    let mut batches = vec![started()];
    batches.extend(
        (2..=MAX_FRAMES as u64 + 1).map(|n| batch(n, json!({"kind":"step_started","step":"loop"}))),
    );
    let mut source = Fake::new(batches);
    assert_eq!(
        drain(&mut source, "status").unwrap_err().code,
        ErrorCode::LimitExceeded
    );
    assert_eq!((source.pulls, source.cancels), (MAX_FRAMES + 1, 1));
}

#[test]
fn one_frame_pull_cannot_accept_an_oversized_batch() {
    let mut source = Fake::new([Ok(vec![
        frame(1, json!({"kind":"started"})),
        frame(
            2,
            json!({"kind":"completed","value":result(json!({})),"cost_units":0}),
        ),
    ])]);
    assert_eq!(
        drain(&mut source, "status").unwrap_err().code,
        ErrorCode::Invalid
    );
    assert_eq!((source.pulls, source.cancels), (1, 1));
}

#[test]
fn streaming_events_keep_unicode_tokens_and_allow_actual_core_error_on_next_pull() {
    let chat = chat_event(&frame(
        1,
        json!({"kind":"emitted","shape":"local.harness-chat.assistant-output@1.0",
        "value":{"kind":"delta","text":"公開本文","prompt-tokens":20,"completion-tokens":10}}),
    ))
    .unwrap();
    assert_eq!(
        (
            chat.kind,
            chat.text,
            chat.prompt_tokens,
            chat.completion_tokens
        ),
        ("delta".into(), Some("公開本文".into()), Some(20), Some(10))
    );
    let failed = chat_event(&frame(
        1,
        json!({"kind":"failed","error":{"code":"unavailable","message":"failed"}}),
    ))
    .unwrap();
    assert_eq!(failed.kind, "failed");
}

#[test]
fn malformed_streaming_tokens_and_text_are_explicit_errors() {
    for value in [
        json!({"kind":"delta","text":3}),
        json!({"kind":"usage","prompt-tokens":-1}),
        json!({"kind":"usage","completion-tokens":4294967296_u64}),
        json!({"text":"no kind"}),
    ] {
        assert_eq!(chat_event(&frame(1,json!({"kind":"emitted","shape":"local.harness-chat.assistant-output@1.0","value":value}))).unwrap_err().code,ErrorCode::Invalid);
    }
}
