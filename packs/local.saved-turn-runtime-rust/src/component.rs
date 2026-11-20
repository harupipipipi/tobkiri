//! Project the original Turn lifecycle using the live Core Profile.

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:saved-turn-runtime/pack@1.0.0",
    generate_all,
});

use crate::{
    LegacySavedTurnSettlementError, LegacySavedTurnSettlementRequest, LegacySavedTurnStartRequest,
    Transition, prepare_saved_input, replay_saved_input, saved_input_source, settle,
    validate_legacy_saved_turn,
};
use exports::local::saved_turn_runtime::legacy_input;
use exports::local::saved_turn_runtime::legacy_read::{self, ReadOutcome};
use exports::local::saved_turn_runtime::legacy_settlement::{
    Guest, Replacement, SettlementError, SettlementOutcome,
};
use exports::local::saved_turn_runtime::legacy_start;
use tobkiri::core::{
    clock, flow,
    types::{Error, ErrorCode},
};

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl legacy_read::Guest for Pack {
    fn validate(turn_id: String, request_id: String, body: String) -> Result<ReadOutcome, Error> {
        let profile = flow::current_profile()?;
        let outcome =
            match validate_legacy_saved_turn(&profile, &turn_id, &request_id, body.as_bytes()) {
                Ok(()) => ReadOutcome::Valid,
                Err(LegacySavedTurnSettlementError::RequiresLegacyCodec) => {
                    ReadOutcome::RequiresLegacyCodec
                }
                Err(LegacySavedTurnSettlementError::Capacity) => ReadOutcome::Capacity,
                Err(LegacySavedTurnSettlementError::InvalidRecord) => ReadOutcome::InvalidRecord,
                Err(_) => {
                    return Err(Error {
                        code: ErrorCode::Internal,
                        message: "unexpected Turn row validation result".into(),
                    });
                }
            };
        Ok(outcome)
    }
}

impl Guest for Pack {
    fn prepare(
        turn_id: String,
        request_id: String,
        body: String,
        input_digest: String,
        expected_revision: Option<u64>,
        result_reference: String,
    ) -> Result<SettlementOutcome, Error> {
        let profile = flow::current_profile()?;
        let request = LegacySavedTurnSettlementRequest {
            turn_id: &turn_id,
            input_digest: &input_digest,
            expected_revision,
            result_reference: result_reference.as_bytes(),
        };
        encode_transition(settle(
            &profile,
            &turn_id,
            &request_id,
            body.as_bytes(),
            &request,
            &mut now_ms,
        ))
    }
}

impl legacy_start::Guest for Pack {
    fn begin(
        turn_id: String,
        conversation_id: String,
        conversation_revision: u64,
        input_digest: String,
    ) -> Result<SettlementOutcome, Error> {
        let profile = flow::current_profile()?;
        let request = LegacySavedTurnStartRequest {
            turn_id: &turn_id,
            conversation_id: &conversation_id,
            conversation_revision,
            input_digest: &input_digest,
        };
        encode_transition(request.begin(&profile, &mut now_ms))
    }

    fn claim(
        turn_id: String,
        request_id: String,
        body: String,
        conversation_id: String,
        conversation_revision: u64,
        input_digest: String,
    ) -> Result<SettlementOutcome, Error> {
        let profile = flow::current_profile()?;
        let request = LegacySavedTurnStartRequest {
            turn_id: &turn_id,
            conversation_id: &conversation_id,
            conversation_revision,
            input_digest: &input_digest,
        };
        encode_transition(request.claim(
            &profile,
            &turn_id,
            &request_id,
            body.as_bytes(),
            &mut now_ms,
        ))
    }
}

impl legacy_input::Guest for Pack {
    fn identify(
        source: String,
    ) -> Result<Result<legacy_input::SourceIdentity, SettlementError>, Error> {
        Ok(saved_input_source(source.as_bytes())
            .map(|identity| legacy_input::SourceIdentity {
                turn_id: identity.turn_id,
                source_digest: identity.source_digest,
            })
            .map_err(domain_error))
    }

    fn prepare(
        source: String,
        accepted: String,
    ) -> Result<Result<legacy_input::InputProjection, SettlementError>, Error> {
        let projection = match prepare_saved_input(source.as_bytes(), accepted.as_bytes()) {
            Ok(projection) => projection,
            Err(error) => return Ok(Err(domain_error(error))),
        };
        Ok(Ok(legacy_input::InputProjection {
            identity: legacy_input::SourceIdentity {
                turn_id: projection.turn_id,
                source_digest: projection.source_digest,
            },
            accepted_digest: projection.accepted_digest,
            context: input_json(projection.context)?,
        }))
    }

    fn recover(
        source: String,
        source_digest: String,
        accepted_digest: String,
        context: String,
    ) -> Result<Result<String, SettlementError>, Error> {
        match replay_saved_input(
            source.as_bytes(),
            &source_digest,
            &accepted_digest,
            context.as_bytes(),
        ) {
            Ok(restored) => Ok(Ok(input_json(restored)?)),
            Err(error) => Ok(Err(domain_error(error))),
        }
    }
}

fn input_json(bytes: Vec<u8>) -> Result<String, Error> {
    String::from_utf8(bytes).map_err(|_| Error {
        code: ErrorCode::Internal,
        message: "saved input serialization failed".into(),
    })
}

fn now_ms() -> crate::Result<i64> {
    i64::try_from(clock::now_ms()).map_err(|_| LegacySavedTurnSettlementError::Storage)
}

fn encode_transition(decision: crate::Result<Transition>) -> Result<SettlementOutcome, Error> {
    match decision {
        Ok(Transition::Replay) => Ok(SettlementOutcome::Replay),
        Ok(Transition::Commit { updated_at, body }) => {
            let body = String::from_utf8(body).map_err(|_| Error {
                code: ErrorCode::Internal,
                message: "saved turn update serialization failed".into(),
            })?;
            Ok(SettlementOutcome::Update(Replacement { updated_at, body }))
        }
        Err(error) => Ok(SettlementOutcome::Rejected(domain_error(error))),
    }
}

fn domain_error(error: LegacySavedTurnSettlementError) -> SettlementError {
    use LegacySavedTurnSettlementError as Source;
    match error {
        Source::InvalidRequest => SettlementError::InvalidRequest,
        Source::NotFound => SettlementError::NotFound,
        Source::InvalidRecord => SettlementError::InvalidRecord,
        Source::InvalidReference => SettlementError::InvalidReference,
        Source::Conflict => SettlementError::Conflict,
        Source::RequiresLegacyCodec => SettlementError::RequiresLegacyCodec,
        Source::Capacity => SettlementError::Capacity,
        Source::Revoked => SettlementError::Revoked,
        Source::Storage => SettlementError::Storage,
        Source::CommitUnconfirmed => SettlementError::CommitUnconfirmed,
    }
}

export!(Pack);
