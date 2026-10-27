wit_bindgen::generate!({path:"../wit",world:"provider",generate_all});

struct Provider;
use std::{cell::RefCell, collections::BTreeMap};
use tobkiri::core::types::{CallbackHandle, Error, ErrorCode, StreamHandle};
thread_local! {static STREAMS:RefCell<BTreeMap<u64,(u32,u32)>>=const{RefCell::new(BTreeMap::new())};}

impl exports::tobkiri::core::lifecycle::Guest for Provider {
    fn init(_: String) -> Result<(), tobkiri::core::types::Error> {
        Ok(())
    }
    fn shutdown() {}
}
impl exports::local::proxy_test::arithmetic::Guest for Provider {
    fn increment(value: u32) -> u32 {
        value.saturating_add(1)
    }
    fn floats(single: f32, double: f64) -> (f32, f64) {
        (single, double)
    }
    fn nan() -> f64 {
        f64::NAN
    }
    fn spin() {
        loop {
            std::hint::spin_loop()
        }
    }
    fn memory_bomb() -> u32 {
        let mut memory = vec![0u8; 128 * 1024 * 1024];
        for index in (0..memory.len()).step_by(4096) {
            memory[index] = (index % 251) as u8;
        }
        std::hint::black_box(memory.len() as u32)
    }
    fn trap() {
        panic!("intentional guest trap")
    }
    fn wait() -> Result<(), Error> {
        tobkiri::core::host_io::terminal_read_line("fixture wait")?;
        Ok(())
    }
    fn request_ui(slot: String) -> Result<(), Error> {
        tobkiri::core::host_io::request_ui_embed(&slot)
    }
    fn count_stream(count: u32) -> Result<StreamHandle, Error> {
        STREAMS.with_borrow_mut(|s| {
            let id = s.keys().last().copied().unwrap_or(0) + 1;
            s.insert(id, (0, count));
            Ok(StreamHandle { id })
        })
    }
    fn with_callback(callback: CallbackHandle, value: u32) -> Result<u32, Error> {
        let result = tobkiri::core::flow::invoke_callback(callback, &value.to_string())?;
        result.parse::<u32>().map(|v| v + 1).map_err(|_| Error {
            code: ErrorCode::Invalid,
            message: "callback fixture returned invalid integer".into(),
        })
    }
}
impl exports::tobkiri::core::stream_source::Guest for Provider {
    fn pull(id: u64, max: u32) -> Result<Vec<String>, Error> {
        STREAMS.with_borrow_mut(|streams| {
            let (state, count) = streams.get_mut(&id).ok_or_else(|| Error {
                code: ErrorCode::NotFound,
                message: "stream fixture absent".into(),
            })?;
            let mut values = Vec::new();
            for _ in 0..max {
                if *state >= *count {
                    break;
                }
                values.push(format!("{{\"index\":{state}}}"));
                *state += 1;
            }
            Ok(values)
        })
    }
    fn cancel(id: u64) {
        STREAMS.with_borrow_mut(|s| {
            s.remove(&id);
        });
    }
}
export!(Provider);
