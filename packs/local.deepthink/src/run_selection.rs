//! Read-only selection for new ordinary Pack operations; never grants dispatch.
use crate::run_repository::{Digest, Head};
use crate::workflow::{Failure, Run, Settings, digest, invalid};
use serde::{Deserialize, Serialize};
use serde_json::json;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Selection {
    pub run_id: String,
    pub conversation: String,
    pub expected_revision: String,
    pub expected_snapshot_digest: String,
}

impl Selection {
    /// Reject malformed selection before opening a private Store.
    pub fn validate_request(&self) -> Result<(), Failure> {
        if self.run_id.len() != 64
            || !self
                .run_id
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err(invalid("invalid selected Run reference"));
        }
        let revision = self
            .expected_revision
            .parse::<i64>()
            .map_err(|_| invalid("invalid selected Run revision"))?;
        if revision < 0 || revision.to_string() != self.expected_revision {
            return Err(invalid("invalid selected Run revision"));
        }
        Digest::parse(&self.expected_snapshot_digest)
            .map_err(|_| invalid("invalid selected Run digest"))?;
        Ok(())
    }

    /// The caller supplies a Head and decoded bytes from one verified snapshot.
    /// This checks selection/configuration, not permission or effect completion.
    pub fn validate(&self, run: &Run, head: &Head, current: &Settings) -> Result<(), Failure> {
        self.validate_request()?;
        if head.run_id != run.id || head.run_request_digest.as_str() != run.request_digest {
            return Err(invalid(
                "selected SQL snapshot changed its original identity",
            ));
        }
        if self.run_id != head.run_id
            || self.expected_revision != head.revision.to_string()
            || self.expected_snapshot_digest != head.snapshot_digest.as_str()
            || self.conversation != run.conversation
        {
            return Err(Failure {
                code: "conflict".into(),
                message: "Selected Run head or conversation changed.".into(),
            });
        }
        if digest(&json!(current)) != digest(&json!(run.settings)) {
            return Err(invalid(
                "selected Run changed its exact captured configuration",
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
pub(crate) fn fixture() -> (Run, Head, Settings) {
    let settings = Settings {
        max_review_rounds: 2,
        max_tool_rounds: 3,
        max_contract_retries: 0,
        structured_provider: false,
        tools: vec![],
        skills: vec![],
        profile_checks: vec![],
        environment: json!({"execution_mode":"verified_wasm"}),
        capability_gaps: vec![],
    };
    let run = Run::new(
        "diagnostic-fixture".into(),
        "fixture-model".into(),
        vec![json!({"role":"user","content":"public fixture"})],
        settings.clone(),
    )
    .unwrap();
    let head = Head {
        run_id: run.id.clone(),
        run_request_digest: Digest::parse(&run.request_digest).unwrap(),
        revision: 7,
        snapshot_digest: Digest::hash(&serde_json::to_vec(&run).unwrap()),
    };
    (run, head, settings)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn selection(run: &Run, head: &Head) -> Selection {
        Selection {
            run_id: run.id.clone(),
            conversation: run.conversation.clone(),
            expected_revision: head.revision.to_string(),
            expected_snapshot_digest: head.snapshot_digest.as_str().into(),
        }
    }

    #[test]
    fn exact_selection_is_read_only_and_checks_the_captured_configuration() {
        let (run, head, settings) = fixture();
        let before = serde_json::to_vec(&run).unwrap();
        let query = selection(&run, &head);
        query.validate(&run, &head, &settings).unwrap();
        assert_eq!(serde_json::to_vec(&run).unwrap(), before);
        let mut changed = settings.clone();
        changed.environment["extra"] = json!(true);
        assert_eq!(
            query.validate(&run, &head, &changed).unwrap_err().code,
            "invalid"
        );
    }

    #[test]
    fn wrong_head_conversation_and_original_identity_are_rejected() {
        let (run, head, settings) = fixture();
        let original = selection(&run, &head);
        for part in 0..4 {
            let mut query = original.clone();
            match part {
                0 => query.run_id = "0".repeat(64),
                1 => query.conversation.push('x'),
                2 => query.expected_revision = "8".into(),
                _ => query.expected_snapshot_digest = "0".repeat(64),
            }
            assert_eq!(
                query.validate(&run, &head, &settings).unwrap_err().code,
                "conflict"
            );
        }
        let mut changed = head.clone();
        changed.run_request_digest = Digest::hash(b"other");
        assert_eq!(
            original
                .validate(&run, &changed, &settings)
                .unwrap_err()
                .code,
            "invalid"
        );
    }

    #[test]
    fn malformed_selection_is_rejected_without_loading_a_run() {
        let (run, head, _) = fixture();
        for revision in ["-1", "+7", "07", "7.0", "9223372036854775808"] {
            let mut query = selection(&run, &head);
            query.expected_revision = revision.into();
            assert!(query.validate_request().is_err());
        }
        for value in ["A".repeat(64), "x".repeat(64), "0".repeat(63)] {
            let mut query = selection(&run, &head);
            query.run_id = value;
            assert!(query.validate_request().is_err());
        }
        let mut query = selection(&run, &head);
        query.expected_snapshot_digest = "A".repeat(64);
        assert!(query.validate_request().is_err());
    }
}
