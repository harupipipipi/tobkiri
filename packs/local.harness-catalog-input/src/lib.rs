//! Thin typed request port that starts the installed-defaults catalog Flow.

wit_bindgen::generate!({
    path: "wit",
    world: "pack",
    generate_all,
});

use exports::local::harness_catalog_input::requests::Guest as Requests;
use exports::tobkiri::core::lifecycle::Guest as Lifecycle;
use tobkiri::core::{
    flow,
    types::{Error, StreamHandle},
};

const CATALOG_FLOW: &str = "local.harness-catalog-input.catalog-flow@1.0";

struct Pack;

impl Lifecycle for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl Requests for Pack {
    fn submit() -> Result<StreamHandle, Error> {
        flow::run_flow(CATALOG_FLOW, "{}")
    }
}

export!(Pack);
