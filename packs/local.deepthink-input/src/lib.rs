//! Ordinary User input adapter. DeepThink owns all domain work and storage;
//! each input operation enters a Profile-selected Flow before worker access.

mod control_flow;

wit_bindgen::generate!({path:"wit",world:"pack",generate_all});

use exports::local::deepthink::continuation_input::{self, Guest, RecoverySelection};
use exports::tobkiri::core::lifecycle;
use serde_json::{Value, json};
use tobkiri::core::{
    flow,
    types::{Error, ErrorCode, StreamHandle},
};

const CONTROL: &str = "local.deepthink.control-flow@1.0";
const CONTINUATION: &str = "local.deepthink.continuation-flow@1.0";
const REGENERATION: &str = "local.deepthink.regeneration-flow@1.0";

struct Pack;

fn error(code: ErrorCode, message: &str) -> Error {
    Error {
        code,
        message: message.into(),
    }
}

struct ControlSource(StreamHandle);
impl control_flow::Source for ControlSource {
    fn pull(&mut self) -> Result<Vec<String>, Error> {
        // The Core event allocation is shared. One frame per read avoids
        // compounding that bound; it does not increase the 1MiB transport cap.
        flow::pull(self.0, 1)
    }
    fn cancel(&mut self) {
        flow::cancel_stream(self.0);
    }
}

fn control(operation: &str, input: Value) -> Result<String, Error> {
    let stream = flow::run_flow(CONTROL, &input.to_string())?;
    control_flow::drain(&mut ControlSource(stream), operation)
}

fn selection_value(selection: RecoverySelection) -> Value {
    json!({"run-id":selection.run_id, "conversation":selection.conversation,
        "attempt-id":selection.attempt_id, "expected-revision":selection.expected_revision,
        "expected-snapshot-digest":selection.expected_snapshot_digest})
}

impl lifecycle::Guest for Pack {
    fn init(config: String) -> Result<(), Error> {
        let value: Value = serde_json::from_str(&config).map_err(|_| {
            error(
                ErrorCode::Invalid,
                "input adapter config must be an empty object",
            )
        })?;
        if value.as_object().is_none_or(|fields| !fields.is_empty()) {
            return Err(error(
                ErrorCode::Invalid,
                "input adapter config must be an empty object",
            ));
        }
        Ok(())
    }
    fn shutdown() {}
}

impl Guest for Pack {
    fn status(run_id: String) -> Result<String, Error> {
        control("status", json!({"op":"status", "run-id":run_id}))
    }

    fn submit(
        run_id: String,
        conversation: String,
        answers: Option<String>,
    ) -> Result<StreamHandle, Error> {
        // The first Flow step performs existing private history/preflight work.
        // This adapter has no Store capability and never loads a Run itself.
        flow::run_flow(
            CONTINUATION,
            &json!({"run-id":run_id,
            "conversation":conversation,"answers":answers})
            .to_string(),
        )
    }

    fn reconcile_generation(selection: RecoverySelection) -> Result<String, Error> {
        control(
            "reconcile-generation",
            json!({"op":"reconcile-generation",
            "selection":selection_value(selection)}),
        )
    }

    fn regenerate_phase(
        selection: RecoverySelection,
        resolution_digest: String,
    ) -> Result<StreamHandle, Error> {
        flow::run_flow(
            REGENERATION,
            &json!({"selection":selection_value(selection),
            "resolution-digest":resolution_digest})
            .to_string(),
        )
    }

    fn pull(stream: StreamHandle, max: u32) -> Result<Vec<continuation_input::ChatEvent>, Error> {
        if max == 0 || max > 64 {
            return Err(error(ErrorCode::Invalid, "pull batch must be 1..64"));
        }
        let frames = flow::pull(stream, max)?;
        let mut events = Vec::with_capacity(frames.len());
        for frame in frames {
            match control_flow::chat_event(&frame) {
                Ok(event) => events.push(event),
                Err(local_error) => {
                    // Only local parse/bound failure still owns an open source.
                    // A Core pull error or EOF above never issues a second cancel.
                    flow::cancel_stream(stream);
                    return Err(local_error);
                }
            }
        }
        Ok(events)
    }

    fn cancel(stream: StreamHandle) {
        flow::cancel_stream(stream);
    }
}

export!(Pack);
