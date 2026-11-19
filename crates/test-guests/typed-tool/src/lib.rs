wit_bindgen::generate!({
    path: ["../../tobkiri-wit/wit/core/core.wit", "wit"],
    world: "local:acceptance-typed-tool/typed-tool@1.0.0",
    generate_all,
});

use tobkiri::core::{audit, types::Error};

struct TypedTool;

impl exports::tobkiri::core::lifecycle::Guest for TypedTool {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl exports::local::acceptance_typed_tool::transform::Guest for TypedTool {
    fn rewrite(text: String) -> Result<String, Error> {
        audit::append("typed-tool-rewrite", "{}")?;
        Ok(format!("tool-{text}"))
    }
}

export!(TypedTool);
