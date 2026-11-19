use crate::{
    Error, ObjectFields, Result, Transition, append_event, check_structure_bound, commit,
    exact_u64, identifier, integer, load_record, object, saved_message_id, string,
};
use ordered_python_compat::{PyValue, dumps, pystrip};
use tobkiri_canon::{Digest, MAX_SAFE_INTEGER};

/// Identity of an ordinary saved input validated by the captured owner.
pub struct LegacySavedTurnStartRequest<'a> {
    /// Stable turn identifier in the captured Profile.
    pub turn_id: &'a str,
    /// Conversation to which the original input was bound.
    pub conversation_id: &'a str,
    /// Conversation revision admitted with the original input.
    pub conversation_revision: u64,
    /// Canonical digest of the full validated saved input.
    pub input_digest: &'a str,
}

impl LegacySavedTurnStartRequest<'_> {
    /// Derive the original owner request key for this captured Profile and turn.
    pub fn request_id(&self, profile: &str) -> Result<String> {
        if profile.is_empty()
            || pystrip(profile) != profile
            || profile.contains(['/', '\\'])
            || profile.contains("..")
            || !identifier(self.turn_id)
            || !identifier(self.conversation_id)
            || !(1..=MAX_SAFE_INTEGER).contains(&self.conversation_revision)
            || Digest::parse(self.input_digest).is_err()
        {
            return Err(Error::InvalidRequest);
        }
        // The original canonical object has these keys in sorted order.
        let identity = dumps(&object([
            ("profile_id", string(profile)),
            ("turn_id", string(self.turn_id)),
        ]))
        .map_err(|_| Error::InvalidRequest)?;
        Ok(format!(
            "saved-turn.{}",
            Digest::hash(identity.as_bytes()).hex()
        ))
    }

    /// Prepare the first queued row after the owner checks admission.
    pub fn begin(
        &self,
        profile: &str,
        clock: &mut impl FnMut() -> Result<i64>,
    ) -> Result<Transition> {
        let request_id = self.request_id(profile)?;
        let updated_at = clock()?;
        let event_at = clock()?;
        let mut turn = object([
            ("id", string(self.turn_id)),
            ("request_id", string(&request_id)),
            ("profile_id", string(profile)),
            ("conversation_id", string(self.conversation_id)),
            ("conversation_revision", integer(self.conversation_revision)),
            ("status", string("queued")),
            ("revision", integer(1)),
            ("created_at", integer(updated_at)),
            ("updated_at", integer(updated_at)),
            ("guidance", PyValue::Array(Vec::new())),
            ("handoff", PyValue::Null),
            ("result_reference", PyValue::Null),
            ("error", PyValue::Null),
            ("events", PyValue::Array(Vec::new())),
            ("input_digest", string(self.input_digest)),
        ]);
        append_event(&mut turn, "turn.queued", event_at, object([]))?;
        commit(&turn, updated_at)
    }

    /// Validate a retry against the observed row and the original saved input.
    pub fn validate_replay(
        &self,
        profile: &str,
        turn_id: &str,
        request_id: &str,
        body: &[u8],
    ) -> Result<()> {
        self.load_bound_turn(profile, turn_id, request_id, body)
            .map(drop)
    }

    /// Check whether the observed row can proceed to the conversation fence.
    pub fn claimable(
        &self,
        profile: &str,
        turn_id: &str,
        request_id: &str,
        body: &[u8],
    ) -> Result<bool> {
        let turn = self.load_bound_turn(profile, turn_id, request_id, body)?;
        claim_available(&turn)
    }

    /// Prepare a running row or return a retry without rewriting the row.
    ///
    /// The owner must check the original conversation fence before a claim
    /// and apply the transition under the same write lock.
    pub fn claim(
        &self,
        profile: &str,
        turn_id: &str,
        request_id: &str,
        body: &[u8],
        clock: &mut impl FnMut() -> Result<i64>,
    ) -> Result<Transition> {
        let mut turn = self.load_bound_turn(profile, turn_id, request_id, body)?;
        if !claim_available(&turn)? {
            return Ok(Transition::Replay);
        }
        let revision = turn
            .get("revision")
            .and_then(exact_u64)
            .and_then(|value| value.checked_add(1))
            .ok_or(Error::RequiresLegacyCodec)?;
        let user_message_id = saved_message_id(self.conversation_id, self.turn_id, "user")?;
        let assistant_message_id =
            saved_message_id(self.conversation_id, self.turn_id, "assistant")?;
        let updated_at = clock()?;
        let event_at = clock()?;
        turn.set("status", string("running"))?;
        turn.set("revision", integer(revision))?;
        turn.set("updated_at", integer(updated_at))?;
        append_event(
            &mut turn,
            "turn.running",
            event_at,
            object([
                ("phase", string("saved_execution_claimed")),
                ("user_message_id", string(&user_message_id)),
                ("assistant_message_id", string(&assistant_message_id)),
            ]),
        )?;
        let transition = commit(&turn, updated_at)?;
        if let Transition::Commit { body, .. } = &transition {
            // Keep the replacement readable by the bounded owner codec.
            check_structure_bound(body)?;
        }
        Ok(transition)
    }

    /// Verify a Pack-produced record against the original owner's intended write.
    pub fn validate_publication(
        &self,
        profile: &str,
        request_id: &str,
        body: &[u8],
        updated_at: i64,
        claimed: bool,
    ) -> Result<()> {
        let updated_at = u64::try_from(updated_at).map_err(|_| Error::InvalidRecord)?;
        let turn = self
            .load_bound_turn(profile, self.turn_id, request_id, body)
            .map_err(|_| Error::InvalidRecord)?;
        let status = if claimed { "running" } else { "queued" };
        if turn.string("status") != Some(status)
            || turn.get("updated_at").and_then(exact_u64) != Some(updated_at)
        {
            return Err(Error::InvalidRecord);
        }
        Ok(())
    }

    fn load_bound_turn(
        &self,
        profile: &str,
        turn_id: &str,
        request_id: &str,
        body: &[u8],
    ) -> Result<PyValue> {
        let expected_request_id = self.request_id(profile)?;
        let turn = load_record(profile, turn_id, request_id, body)?;
        if turn_id != self.turn_id
            || request_id != expected_request_id
            || turn.string("conversation_id") != Some(self.conversation_id)
            || turn.get("conversation_revision").and_then(exact_u64)
                != Some(self.conversation_revision)
            || turn.string("input_digest") != Some(self.input_digest)
        {
            return Err(Error::Conflict);
        }
        Ok(turn)
    }
}

/// Validate one row from the original physical owner scan and test for activity.
///
/// Every physical row must be checked before the caller resolves the fence,
/// including rows from other conversations and the excluded turn itself.
pub fn active_turn_in_conversation(
    profile: &str,
    turn_id: &str,
    request_id: &str,
    body: &[u8],
    conversation_id: &str,
    except_turn_id: &str,
) -> Result<bool> {
    let turn = load_record(profile, turn_id, request_id, body)?;
    Ok(turn_id != except_turn_id
        && turn.string("conversation_id") == Some(conversation_id)
        && matches!(
            turn.string("status"),
            Some("queued" | "running" | "waiting")
        ))
}

fn claim_available(turn: &PyValue) -> Result<bool> {
    let events = turn.array("events").ok_or(Error::InvalidRecord)?;
    Ok(turn.string("status") == Some("queued")
        && !events
            .iter()
            .any(|event| event.string("name") == Some("turn.cancellation_requested")))
}
