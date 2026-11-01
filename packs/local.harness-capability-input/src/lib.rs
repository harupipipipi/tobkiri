//! Thin request port that starts the Profile Flow for capability projection.

wit_bindgen::generate!({
    path: "wit",
    world: "pack",
    generate_all,
});

use exports::local::harness_capability_input::requests::Guest as Requests;
use exports::tobkiri::core::lifecycle::Guest as Lifecycle;
use tobkiri::core::{
    flow,
    types::{Error, ErrorCode, StreamHandle},
};

const MAX_FRAME_BYTES: usize = 4 * 1024 * 1024;
const PROJECTION_FLOW: &str = "local.harness-capability-input.projection-flow@1.0";

struct Pack;

impl Lifecycle for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl Requests for Pack {
    fn submit(payload: String) -> Result<StreamHandle, Error> {
        if payload.len() > MAX_FRAME_BYTES {
            return Err(Error {
                code: ErrorCode::LimitExceeded,
                message: "capability projection input exceeds four MiB".into(),
            });
        }

        let request = serde_json::json!({"payload": payload});
        let frame = serde_json::to_string(&request).map_err(|_| Error {
            code: ErrorCode::Internal,
            message: "capability projection request serialization failed".into(),
        })?;
        if frame.len() > MAX_FRAME_BYTES {
            return Err(Error {
                code: ErrorCode::LimitExceeded,
                message: "capability projection request frame exceeds four MiB".into(),
            });
        }

        flow::run_flow(PROJECTION_FLOW, &frame)
    }
}

export!(Pack);
