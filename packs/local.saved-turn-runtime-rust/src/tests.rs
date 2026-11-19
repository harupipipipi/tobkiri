use super::*;
use serde_json::{Value, json};

const INPUT: &str = "sha256:1111111111111111111111111111111111111111111111111111111111111111";
const OUTCOME: &str = "sha256:2222222222222222222222222222222222222222222222222222222222222222";
const STOP: &str = r#"{"name":"turn.cancellation_requested","sequence":1.0,"details":{"phase":"nested_cancellation_requested"}}"#;
const QUEUED_EVENT: &str = r#"{"name":"turn.queued","sequence":0}"#;

fn reference(revision: &str) -> String {
    let id = |role: &str| {
        let bytes = serde_json::to_vec(&["conversation", "test-turn", role]).unwrap();
        format!("message:{}", Digest::hash(&bytes).hex())
    };
    format!(
        r#"{{"outcome_digest":"{OUTCOME}","conversation_revision":{revision},"assistant_message_id":"{}","conversation_id":"conversation","user_message_id":"{}"}}"#,
        id("assistant"),
        id("user")
    )
}

fn record(status: &str, events: &str, guidance: &str, reference: &str) -> String {
    format!(
        r#"{{"profile_id":"defaults","id":"test-turn","request_id":"saved-turn.test","conversation_revision":7,"conversation_id":"conversation","revision":3,"created_at":10,"updated_at":11,"status":"{status}","guidance":[{guidance}],"events":[{events}],"error":"opaque","result_reference":{reference}}}"#
    )
}

fn apply(body: &str, reference: &str, expected_revision: Option<u64>) -> (Result<Transition>, i64) {
    let mut time = 100;
    let result = settle(
        "defaults",
        "test-turn",
        "saved-turn.test",
        body.as_bytes(),
        &LegacySavedTurnSettlementRequest {
            turn_id: "test-turn",
            input_digest: INPUT,
            expected_revision,
            result_reference: reference.as_bytes(),
        },
        &mut || {
            time += 1;
            Ok(time)
        },
    );
    (result, time - 100)
}

fn saved_record(status: &str, events: &str, guidance: &str, reference: &str) -> String {
    let mut body = record(status, events, guidance, reference);
    body.pop();
    body.push_str(&format!(r#","input_digest":"{INPUT}"}}"#));
    body
}

#[test]
fn waiting_uses_separate_turn_and_event_clocks_and_preserves_legacy_values() {
    let body = saved_record("waiting", QUEUED_EVENT, "", "null");
    let body = body.strip_suffix('}').unwrap();
    let body = format!(
        r#"{body},"opaque":{{"z":1234567890123456789012345678901234567890,"a":1.0E-5,"unicode":"日本語"}}}}"#
    );
    let reference = reference("9");
    let (result, calls) = apply(&body, &reference, Some(3));
    let Transition::Commit { updated_at, body } = result.unwrap() else {
        panic!("fresh receipt should commit");
    };
    assert_eq!(calls, 2);
    assert_eq!(updated_at, 101);
    let encoded = std::str::from_utf8(&body).unwrap();
    assert!(encoded.contains(&format!(r#""result_reference":{reference}"#)));
    assert!(encoded.contains(
        r#""opaque":{"z":1234567890123456789012345678901234567890,"a":1e-05,"unicode":"日本語"}"#
    ));
    let result: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(result["status"], "completed");
    assert_eq!(result["revision"], 4);
    assert_eq!(result["error"], "opaque");
    assert_eq!(result["events"][1]["at"], 102);
}

#[test]
fn stop_fences_only_queued_boolean_auto_send_in_guidance_order() {
    let body = saved_record(
        "running",
        &format!("{QUEUED_EVENT},{STOP}"),
        concat!(
            r#"{"status":"queued","id":"z","value":{"auto_send":true}},"#,
            r#"{"status":"queued","id":"numeric","value":{"auto_send":1}},"#,
            r#"{"status":"consumed","id":"consumed","value":{"auto_send":true}},"#,
            r#"{"status":"queued","id":"a","value":{"auto_send":true}}"#,
        ),
        "null",
    );
    let (result, calls) = apply(&body, &reference("9"), None);
    let Transition::Commit { updated_at, body } = result.unwrap() else {
        panic!("the exact receipt should win a stop request");
    };
    assert_eq!(calls, 3);
    assert_eq!(updated_at, 102);
    let result: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(result["revision"], 5);
    assert_eq!(result["guidance"][0]["status"], "failed");
    assert_eq!(result["guidance"][0]["updated_at"], 101);
    assert_eq!(result["guidance"][1]["status"], "queued");
    assert_eq!(result["guidance"][2]["status"], "consumed");
    assert_eq!(
        result["guidance"][3]["failure_reason"],
        "cancelled_before_dispatch"
    );
    assert_eq!(
        result["events"][2],
        json!({
            "name": "turn.guidance_cancelled",
            "sequence": 2,
            "at": 101,
            "details": {"guidance_ids": ["z", "a"]},
        })
    );
    assert_eq!(result["events"][3]["at"], 103);
}

#[test]
fn completed_replay_precedes_stale_revision_and_cancellation_checks() {
    let reference = reference("9");
    let body = saved_record("completed", STOP, "", &reference);
    let (result, calls) = apply(&body, &reference, Some(1));
    assert!(matches!(result, Ok(Transition::Replay)));
    assert_eq!(calls, 0);
    let body = saved_record("running", QUEUED_EVENT, "", "null");
    let (result, calls) = apply(&body, &reference, Some(1));
    assert_eq!(result.unwrap_err(), Error::Conflict);
    assert_eq!(calls, 0);
}

#[test]
fn malformed_stop_and_nonfinite_legacy_values_take_no_clock_samples() {
    let reference = reference("9");
    for events in [
        format!("{STOP},{QUEUED_EVENT}"),
        format!(r#"{QUEUED_EVENT},{STOP},{{"name":"turn.cancelled"}}"#),
        format!("{STOP},{STOP}"),
        format!("{QUEUED_EVENT},{}", STOP.replace("1.0", "2")),
        format!(
            "{QUEUED_EVENT},{}",
            STOP.replace("nested_cancellation_requested", "other")
        ),
    ] {
        let body = saved_record("waiting", &events, "", "null");
        let (result, calls) = apply(&body, &reference, None);
        assert_eq!(result.unwrap_err(), Error::Conflict);
        assert_eq!(calls, 0);
    }
    let body = saved_record("running", QUEUED_EVENT, "", "null").replace(r#""opaque""#, "NaN");
    let (result, calls) = apply(&body, &reference, None);
    assert_eq!(result.unwrap_err(), Error::RequiresLegacyCodec);
    assert_eq!(calls, 0);
}
