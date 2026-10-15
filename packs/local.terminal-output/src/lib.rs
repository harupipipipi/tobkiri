//! Output is a separate Pack so Flow stream pulls never reenter the input guest.
wit_bindgen::generate!({path:"wit",world:"pack",generate_all});
use exports::local::harness_chat::assistant_output::{ChatEvent, Guest};
use exports::tobkiri::core::lifecycle;
use tobkiri::core::{host_io, types::Error};
struct Pack;
impl lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }
    fn shutdown() {}
}
impl Guest for Pack {
    fn emit(event: ChatEvent) -> Result<(), Error> {
        if event.kind == "delta" {
            if let Some(text) = event.text {
                host_io::terminal_write(&text)?;
            }
        } else if event.kind == "done" {
            host_io::terminal_write("\n")?;
        }
        Ok(())
    }
}
export!(Pack);
