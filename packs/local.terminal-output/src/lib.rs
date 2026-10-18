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
        } else if event.kind == "usage" {
            let value = event
                .text
                .as_deref()
                .and_then(|text| serde_json::from_str(text).ok())
                .unwrap_or(serde_json::Value::Null);
            host_io::terminal_write(&format!(
                "\n[usage {}]\n",
                tobkiri_guest_sdk::accounting::format_usage(
                    &value,
                    event.prompt_tokens,
                    event.completion_tokens
                )
            ))?;
        } else if event.kind == "progress" {
            if let Some(value) = event
                .text
                .as_deref()
                .and_then(|text| serde_json::from_str::<serde_json::Value>(text).ok())
            {
                let phase = value["phase"].as_str().unwrap_or("workflow");
                let status = value["status"].as_str().unwrap_or("running");
                let next = value
                    .pointer("/summary/next_action")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                host_io::terminal_write(&format!("\n[DeepThink {phase} {status}] {next}\n"))?;
                if let Some(run_id) = value["run_id"].as_str() {
                    host_io::terminal_write(&format!("run_id={run_id}\n"))?;
                }
                if let Some(usage) = value.get("usage") {
                    host_io::terminal_write(&format!(
                        "[usage {}]\n",
                        tobkiri_guest_sdk::accounting::format_usage(usage, None, None)
                    ))?;
                }
            }
        }
        Ok(())
    }
}
export!(Pack);
