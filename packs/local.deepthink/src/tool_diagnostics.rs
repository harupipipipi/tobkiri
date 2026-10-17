//! Bounded commitments to stored model-selected tool receipts, never tool calls.
use crate::run_repository::{Digest, Head};
use crate::workflow::{Failure, Run, ToolReceipt, digest, invalid};
use serde_json::{Value, json};

pub const MAX_PAGE_ITEMS: u32 = 16;
pub const MAX_PAGE_BYTES: usize = 16 * 1024;

fn limit() -> Failure {
    Failure {
        code: "limit-exceeded".into(),
        message: "Tool receipt diagnostic bound exceeded.".into(),
    }
}

fn commitment(run: &Run, key: &str, receipt: &ToolReceipt, ordinal: u32) -> Result<Value, Failure> {
    Digest::parse(&receipt.request_digest)
        .map_err(|_| invalid("invalid stored tool request digest"))?;
    let outcome_digest = match (receipt.status.as_str(), receipt.outcome.as_ref()) {
        ("pending", None) => None,
        ("completed", Some(outcome)) if outcome.get("ok") == Some(&Value::Bool(true)) => {
            Some(digest(outcome))
        }
        _ => {
            return Err(invalid(
                "stored tool receipt has an unresolved or invalid state",
            ));
        }
    };
    // Exactly the production dispatch-id formula. The private position and
    // actual outcome body never enter the public result.
    Ok(
        json!({"ordinal":ordinal,"dispatch_id":digest(&json!({"run":run.id,"position":key})),
        "request_digest":receipt.request_digest,"status":receipt.status,"outcome_digest":outcome_digest}),
    )
}

fn checked_envelope(page: Value) -> Result<String, Failure> {
    let envelope = json!({"op":"tool-receipts","result":page}).to_string();
    if envelope.len() > MAX_PAGE_BYTES {
        return Err(limit());
    }
    Ok(envelope)
}

/// Return a single immutable snapshot page. The caller has already selected
/// and authorized that exact SQL Head/configuration through ordinary ports.
pub fn page(run: &Run, head: &Head, offset: u32, count: u32) -> Result<String, Failure> {
    if !(1..=MAX_PAGE_ITEMS).contains(&count) {
        return Err(invalid("tool receipt page limit must be 1..16"));
    }
    if run.id != head.run_id || run.request_digest != head.run_request_digest.as_str() {
        return Err(invalid("tool receipt snapshot identity changed"));
    }
    let total = u32::try_from(run.tool_receipts.len()).map_err(|_| limit())?;
    if offset > total {
        return Err(invalid("tool receipt offset exceeds the selected snapshot"));
    }
    let end = offset
        .checked_add(count.min(total - offset))
        .ok_or_else(limit)?;
    let mut items = Vec::with_capacity(usize::try_from(end - offset).map_err(|_| limit())?);
    let start = usize::try_from(offset).map_err(|_| limit())?;
    let length = usize::try_from(end - offset).map_err(|_| limit())?;
    for (ordinal, (key, receipt)) in run
        .tool_receipts
        .iter()
        .enumerate()
        .skip(start)
        .take(length)
    {
        items.push(commitment(
            run,
            key,
            receipt,
            u32::try_from(ordinal).map_err(|_| limit())?,
        )?);
    }
    checked_envelope(
        json!({"version":"deepthink.tool-receipts.v1","run_id":run.id,
        "repository":{"format":"deepthink.run-ledger.v1","run_id":head.run_id,
            "run_request_digest":head.run_request_digest.as_str(),"revision":head.revision.to_string(),
            "snapshot_digest":head.snapshot_digest.as_str()},
        "offset":offset,"total":total,"next_offset":if end < total {Some(end)} else {None},"items":items}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::run_selection::fixture;

    fn insert(run: &mut Run, key: &str, value: i64) -> (Value, Value) {
        let call = json!({"id":digest(&json!({"run":run.id,"position":key})),
            "name":"calculator","arguments":{"operation":"multiply","operands":[23,17]}});
        let outcome = json!({"ok":true,"result":{"value":value}});
        run.tool_receipts.insert(
            key.into(),
            ToolReceipt {
                request_digest: digest(&call),
                status: "completed".into(),
                outcome: Some(outcome.clone()),
            },
        );
        (call, outcome)
    }
    fn result(run: &Run, head: &Head, offset: u32, count: u32) -> Value {
        let text = page(run, head, offset, count).unwrap();
        assert!(text.len() <= MAX_PAGE_BYTES);
        let value: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(value["op"], "tool-receipts");
        value["result"].clone()
    }
    #[test]
    fn pages_cover_zero_three_and_seventeen_receipts_without_repeating_positions() {
        let (mut run, head, _) = fixture();
        assert_eq!(result(&run, &head, 0, 16)["items"], json!([]));
        for n in 0..17 {
            insert(&mut run, &format!("section@0:0:{n:02}"), 391);
        }
        let first = result(&run, &head, 0, 16);
        let last = result(&run, &head, 16, 16);
        assert_eq!(first["total"], 17);
        assert_eq!(first["next_offset"], 16);
        assert_eq!(first["items"].as_array().unwrap().len(), 16);
        assert_eq!(last["items"][0]["ordinal"], 16);
        assert!(last["next_offset"].is_null());
        assert_eq!(result(&run, &head, 17, 1)["items"], json!([]));
        assert_eq!(
            result(&run, &head, 0, 3)["items"].as_array().unwrap().len(),
            3
        );
        assert!(page(&run, &head, 18, 1).is_err());
        for count in [0, 17, u32::MAX] {
            assert!(page(&run, &head, 0, count).is_err());
        }
    }
    #[test]
    fn known_exact_request_and_outcome_preimages_can_be_independently_checked() {
        let (mut run, head, _) = fixture();
        let (mut expected, outcome) = insert(&mut run, "section@0:0:0", 391);
        insert(&mut run, "section@0:0:1", 391);
        let data = result(&run, &head, 0, 2);
        assert_eq!(data["items"][0]["request_digest"], digest(&expected));
        assert_eq!(data["items"][0]["outcome_digest"], digest(&outcome));
        assert_ne!(
            data["items"][0]["dispatch_id"],
            data["items"][1]["dispatch_id"]
        );
        expected["arguments"]["operands"] = json!([23, 18]);
        assert_ne!(data["items"][0]["request_digest"], digest(&expected));
        assert_ne!(
            data["items"][0]["outcome_digest"],
            digest(&json!({"ok":true,"result":{"value":392}}))
        );
    }
    #[test]
    fn production_digest_matches_the_independent_no_float_calculator_vectors() {
        let request = json!({"id":"1".repeat(64),"name":"calculator", "arguments":{"operation":"multiply","operands":[23,17]}});
        assert_eq!(
            digest(&request),
            "5fbde0722b0df634eb003be6724a88ef874bd53e86902ec06bfed953482fecd2"
        );
        assert_eq!(
            digest(&json!({"ok":true,"result":{"value":391}})),
            "2e6d8693ed7b35caa812c54133e44c176cda331e9ef26575b3336b429dd548cf"
        );
    }
    #[test]
    fn pending_is_unknown_and_malformed_states_are_not_normalized_to_success() {
        let (mut run, head, _) = fixture();
        run.tool_receipts.insert(
            "pending".into(),
            ToolReceipt {
                request_digest: "a".repeat(64),
                status: "pending".into(),
                outcome: None,
            },
        );
        let data = result(&run, &head, 0, 1);
        assert_eq!(data["items"][0]["status"], "pending");
        assert!(data["items"][0]["outcome_digest"].is_null());
        for (status, outcome) in [
            ("completed", None),
            ("pending", Some(json!({"ok":true}))),
            ("completed", Some(json!({"ok":false}))),
            ("unknown", None),
        ] {
            let receipt = run.tool_receipts.get_mut("pending").unwrap();
            receipt.status = status.into();
            receipt.outcome = outcome;
            assert!(page(&run, &head, 0, 1).is_err());
        }
        let receipt = run.tool_receipts.get_mut("pending").unwrap();
        receipt.status = "pending".into();
        receipt.outcome = None;
        receipt.request_digest = "A".repeat(64);
        assert!(page(&run, &head, 0, 1).is_err());
    }
    #[test]
    fn private_keys_and_large_outcomes_do_not_expand_or_mutate_the_projection() {
        let (mut run, head, _) = fixture();
        let key = "private-key-猫".repeat(1000);
        insert(&mut run, &key, 391);
        run.tool_receipts.get_mut(&key).unwrap().outcome =
            Some(json!({"ok":true,"private":"private-body-猫".repeat(10000)}));
        let before = serde_json::to_vec(&run).unwrap();
        let text = page(&run, &head, 0, 16).unwrap();
        assert!(!text.contains("private-key") && !text.contains("private-body"));
        assert_eq!(serde_json::to_vec(&run).unwrap(), before);
        assert_eq!(
            checked_envelope(json!({"unexpected":"x".repeat(MAX_PAGE_BYTES)}))
                .unwrap_err()
                .code,
            "limit-exceeded"
        );
        let mut different = head.clone();
        different.run_request_digest = Digest::hash(b"other");
        assert!(page(&run, &different, 0, 1).is_err());
    }
}
