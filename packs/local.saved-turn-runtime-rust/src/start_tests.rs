use super::*;

const PROFILE: &str = "Office_A";
const TURN: &str = "test-turn";
const CONVERSATION: &str = "conversation";
const INPUT: &str = "sha256:3365f0d333c6fafb2ee509d24a0deacffd312f9ea88f322aa84b7c82749b92ef";
const REQUEST: &str = "saved-turn.9e5cffb6e4d73790ec37abf773e81515d18d819fb0ffa8b9bf7f1a2bf7d8c91d";

// Bytes captured from DurableTurnRuntime and its original SQLite store for a
// saved input with content "こんにちは\nowner"; only the owner clocks were replaced.
const QUEUED: &str = concat!(
    r#"{"id":"test-turn","request_id":"saved-turn.9e5cffb6e4d73790ec37abf773e81515d18d819fb0ffa8b9bf7f1a2bf7d8c91d","profile_id":"Office_A","#,
    r#""conversation_id":"conversation","conversation_revision":7,"status":"queued","revision":1,"created_at":10,"updated_at":10,"guidance":[],"handoff":null,"result_reference":null,"error":null,"#,
    r#""events":[{"sequence":0,"name":"turn.queued","at":11,"details":{}}],"input_digest":"sha256:3365f0d333c6fafb2ee509d24a0deacffd312f9ea88f322aa84b7c82749b92ef"}"#,
);
const RUNNING: &str = concat!(
    r#"{"id":"test-turn","request_id":"saved-turn.9e5cffb6e4d73790ec37abf773e81515d18d819fb0ffa8b9bf7f1a2bf7d8c91d","profile_id":"Office_A","#,
    r#""conversation_id":"conversation","conversation_revision":7,"status":"running","revision":4,"created_at":10,"updated_at":22,"guidance":[{"id":"z","status":"queued","value":{"auto_send":true,"text":"あとで"}}],"handoff":null,"result_reference":null,"error":null,"#,
    r#""events":[{"sequence":0,"name":"turn.queued","at":11,"details":{}},{"sequence":1,"name":"turn.running","at":23,"details":{"phase":"saved_execution_claimed","user_message_id":"message:f252926839e00670d67c5dd465ff6cb413f73ba04d3c6b9c47c919f7b3e318dd","assistant_message_id":"message:8273d569849d21a6ab4dc72f118e21400dbdc20862eeb2237c2ee451e8aa5a26"}}],"#,
    r#""input_digest":"sha256:3365f0d333c6fafb2ee509d24a0deacffd312f9ea88f322aa84b7c82749b92ef","extra":{"z":123456789012345678901234567890,"a":1e-05,"text":"未知"}}"#,
);

fn request() -> LegacySavedTurnStartRequest<'static> {
    LegacySavedTurnStartRequest {
        turn_id: TURN,
        conversation_id: CONVERSATION,
        conversation_revision: 7,
        input_digest: INPUT,
    }
}

fn with_extra(record: &str, extra: &str) -> String {
    format!("{},{extra}}}", record.strip_suffix('}').unwrap())
}

#[test]
fn begin_matches_the_original_owner_row() {
    assert_eq!(request().request_id(PROFILE).unwrap(), REQUEST);
    let mut clock = [10, 11].into_iter();
    let Transition::Commit { updated_at, body } = request()
        .begin(PROFILE, &mut || Ok(clock.next().unwrap()))
        .unwrap()
    else {
        panic!("a new turn must produce a queued row");
    };
    assert!(clock.next().is_none());
    assert_eq!(updated_at, 10);
    assert_eq!(body, QUEUED.as_bytes());
}

#[test]
fn claim_matches_the_original_bytes_and_retains_guidance_and_unknown_fields() {
    let updated = QUEUED.replace(r#""revision":1"#, r#""revision":3"#).replace(
        r#""guidance":[]"#,
        r#""guidance":[{"id":"z","status":"queued","value":{"auto_send":true,"text":"あとで"}}]"#,
    );
    let updated = with_extra(
        &updated,
        r#""extra":{"z":123456789012345678901234567890,"a":1.0E-5,"text":"未知"}"#,
    );
    assert_eq!(
        request().claimable(PROFILE, TURN, REQUEST, updated.as_bytes()),
        Ok(true)
    );
    let mut clock = [22, 23].into_iter();
    let Transition::Commit { updated_at, body } = request()
        .claim(PROFILE, TURN, REQUEST, updated.as_bytes(), &mut || {
            Ok(clock.next().unwrap())
        })
        .unwrap()
    else {
        panic!("an admitted queued turn must be claimable");
    };
    assert!(clock.next().is_none());
    assert_eq!(updated_at, 22);
    assert_eq!(body, RUNNING.as_bytes());
}

#[test]
fn a_claim_that_would_outgrow_the_row_codec_requests_compatibility() {
    let baseline = with_extra(QUEUED, r#""padding":[0]"#);
    let baseline_marks = baseline
        .bytes()
        .filter(|byte| matches!(byte, b'{' | b'[' | b':' | b','))
        .count();
    let padding = "0,".repeat(MAX_STRUCTURAL_MARKS - baseline_marks);
    let row = with_extra(QUEUED, &format!(r#""padding":[{padding}0]"#));
    assert_eq!(
        request().claimable(PROFILE, TURN, REQUEST, row.as_bytes()),
        Ok(true)
    );
    assert!(matches!(
        request().claim(PROFILE, TURN, REQUEST, row.as_bytes(), &mut || Ok(22)),
        Err(Error::RequiresLegacyCodec)
    ));
}

#[test]
fn a_repeat_or_cancellation_request_replays_without_a_clock() {
    let stopped = QUEUED.replace(
        r#""details":{}}],"input_digest""#,
        r#""details":{}},{"name":"turn.cancellation_requested","opaque":true}],"input_digest""#,
    );
    for row in [
        stopped,
        RUNNING.to_owned(),
        RUNNING.replace(r#""status":"running""#, r#""status":"waiting""#),
        RUNNING.replace(r#""status":"running""#, r#""status":"completed""#),
        RUNNING.replace(r#""status":"running""#, r#""status":"failed""#),
        RUNNING.replace(r#""status":"running""#, r#""status":"cancelled""#),
    ] {
        assert_eq!(
            request().validate_replay(PROFILE, TURN, REQUEST, row.as_bytes()),
            Ok(())
        );
        assert_eq!(
            request().claimable(PROFILE, TURN, REQUEST, row.as_bytes()),
            Ok(false)
        );
        assert!(matches!(
            request().claim(PROFILE, TURN, REQUEST, row.as_bytes(), &mut || panic!(
                "a lost claim response or cancellation may not grant execution again"
            )),
            Ok(Transition::Replay)
        ));
    }
}

#[test]
fn replay_distinguishes_invalid_identity_rebinding_legacy_codec_and_limits() {
    let check = |row: &str| request().validate_replay(PROFILE, TURN, REQUEST, row.as_bytes());
    for row in [
        QUEUED.replace(
            r#""conversation_revision":7"#,
            r#""conversation_revision":8"#,
        ),
        QUEUED.replace(
            r#""conversation_id":"conversation""#,
            r#""conversation_id":"other""#,
        ),
        QUEUED.replace(INPUT, Digest::hash(b"different input").as_str()),
    ] {
        assert_eq!(check(&row), Err(Error::Conflict));
    }
    assert_eq!(
        check(&QUEUED.replace(r#""revision":1"#, r#""revision":true"#)),
        Err(Error::InvalidRecord)
    );
    assert_eq!(
        check(&with_extra(QUEUED, r#""status":"queued""#)),
        Err(Error::RequiresLegacyCodec)
    );
    assert_eq!(
        check(&with_extra(QUEUED, r#""escaped":"\ud800""#)),
        Err(Error::RequiresLegacyCodec)
    );
    let padded = format!("{}{}", QUEUED, " ".repeat(RECORD_LIMIT - QUEUED.len()));
    assert_eq!(check(&padded), Ok(()));
    assert_eq!(check(&(padded + " ")), Err(Error::Capacity));

    let other = QUEUED.replace(REQUEST, "different-request");
    assert_eq!(
        request().validate_replay(PROFILE, TURN, "different-request", other.as_bytes()),
        Err(Error::Conflict)
    );
    assert_eq!(
        request().validate_replay(PROFILE, TURN, REQUEST, other.as_bytes()),
        Err(Error::InvalidRecord)
    );
}

#[test]
fn a_claim_at_the_record_limit_checks_its_output_size() {
    let empty = with_extra(QUEUED, r#""extra":"""#);
    let full = with_extra(
        QUEUED,
        &format!(r#""extra":"{}""#, "x".repeat(RECORD_LIMIT - empty.len())),
    );
    assert_eq!(full.len(), RECORD_LIMIT);
    assert_eq!(
        request().claimable(PROFILE, TURN, REQUEST, full.as_bytes()),
        Ok(true)
    );
    assert!(matches!(
        request().claim(PROFILE, TURN, REQUEST, full.as_bytes(), &mut || Ok(22)),
        Err(Error::Capacity)
    ));
}

#[test]
fn validates_identity_before_clocks_and_preserves_legacy_revision_fallback() {
    for invalid in [
        LegacySavedTurnStartRequest {
            conversation_revision: 0,
            ..request()
        },
        LegacySavedTurnStartRequest {
            conversation_revision: tobkiri_canon::MAX_SAFE_INTEGER + 1,
            ..request()
        },
        LegacySavedTurnStartRequest {
            input_digest: "untrusted",
            ..request()
        },
        LegacySavedTurnStartRequest {
            turn_id: "not a turn",
            ..request()
        },
    ] {
        assert!(matches!(
            invalid.begin(PROFILE, &mut || panic!("invalid identity used a clock")),
            Err(Error::InvalidRequest)
        ));
    }
    let revision = QUEUED.replace(r#""revision":1"#, &format!(r#""revision":{}"#, u64::MAX));
    assert!(matches!(
        request().claim(PROFILE, TURN, REQUEST, revision.as_bytes(), &mut || panic!(
            "unrepresentable revision used a clock"
        )),
        Err(Error::RequiresLegacyCodec)
    ));
}

#[test]
fn conversation_fence_validates_every_row_before_testing_activity() {
    let active = |row: &str, conversation, except| {
        active_turn_in_conversation(PROFILE, TURN, REQUEST, row.as_bytes(), conversation, except)
    };
    for status in [
        "queued",
        "running",
        "waiting",
        "completed",
        "failed",
        "cancelled",
    ] {
        let row = QUEUED.replace(r#""status":"queued""#, &format!(r#""status":"{status}""#));
        assert_eq!(
            active(&row, CONVERSATION, "other"),
            Ok(matches!(status, "queued" | "running" | "waiting"))
        );
        assert_eq!(active(&row, CONVERSATION, TURN), Ok(false));
        assert_eq!(active(&row, "other", "other"), Ok(false));
    }
    let invalid = QUEUED.replace(r#""profile_id":"Office_A""#, r#""profile_id":"foreign""#);
    assert_eq!(
        active(&invalid, CONVERSATION, TURN),
        Err(Error::InvalidRecord)
    );
    assert_eq!(
        active(&invalid, "other", "other"),
        Err(Error::InvalidRecord)
    );
}
