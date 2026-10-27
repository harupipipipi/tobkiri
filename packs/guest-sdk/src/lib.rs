//! Pure guest helpers shared by independently identified Packs.

pub mod sse;

/// Convert a finite WIT f64 to the exact StrictV1 string representation.
pub fn canon_f64(value: f64) -> Result<String, &'static str> {
    if value.is_finite() { Ok(format!("{value:e}")) } else { Err("non-finite WIT f64") }
}

/// Convert a finite WIT f32 at its declared width, preserving negative zero.
pub fn canon_f32(value: f32) -> Result<String, &'static str> {
    if value.is_finite() { Ok(format!("{value:e}")) } else { Err("non-finite WIT f32") }
}

/// A text delta in the Pack-owned stream event convention.
pub fn delta_event(text: &str) -> String {
    serde_json::json!({"kind":"delta", "text":text,"prompt-tokens":null,"completion-tokens":null}).to_string()
}

/// A completed text generation, collected by Flow before persistence.
pub fn done_event(text: &str) -> String {
    serde_json::json!({"kind":"done", "text":text,"prompt-tokens":null,"completion-tokens":null}).to_string()
}
