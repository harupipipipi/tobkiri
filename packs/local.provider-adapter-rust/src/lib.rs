//! Effect-free preparation and projection of original local Provider chat.

mod projection;
mod request;
pub use projection::project_local_completion;
pub use request::compile_local_chat_request;

#[cfg(feature = "component")]
mod component;
