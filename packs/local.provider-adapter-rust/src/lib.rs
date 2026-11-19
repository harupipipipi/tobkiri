//! Effect-free projection of finite chat completions from the original local Provider.

mod projection;
pub use projection::project_local_completion;

#[cfg(feature = "component")]
mod component;
