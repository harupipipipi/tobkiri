//! Prepare a receipt settlement using the live Core execution Profile.

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:saved-turn-runtime/pack@1.0.0",
    generate_all,
});

use crate::{LegacySavedTurnSettlementError, LegacySavedTurnSettlementRequest, Transition, settle};
use exports::local::saved_turn_runtime::legacy_settlement::{
    Guest, Replacement, SettlementError, SettlementOutcome,
};
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
        let decision = settle(
            &profile,
            &turn_id,
            &request_id,
            body.as_bytes(),
            &request,
            &mut || {
                i64::try_from(clock::now_ms()).map_err(|_| LegacySavedTurnSettlementError::Storage)
            },
        );
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
