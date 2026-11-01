// Include the build script so its path-contract tests run under the normal
// Rust test harness without duplicating build-only helpers.
#[allow(dead_code)]
#[path = "../build.rs"]
mod build_script;
