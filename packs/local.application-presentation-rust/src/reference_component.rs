//! Import-free ABI used by the sealed Python comparison.

wit_bindgen::generate!({ path: "wit", world: "pack" });

struct Presentation;

impl Guest for Presentation {
    fn invoke(operation_id: String, payload_json: String) -> Result<String, String> {
        crate::invoke(&operation_id, &payload_json).map_err(str::to_owned)
    }
}

export!(Presentation);
