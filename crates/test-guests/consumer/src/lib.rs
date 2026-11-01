wit_bindgen::generate!({path:"../wit",world:"consumer",generate_all});

struct Consumer;
impl exports::tobkiri::core::lifecycle::Guest for Consumer {
    fn init(_: String) -> Result<(), tobkiri::core::types::Error> {
        Ok(())
    }
    fn shutdown() {}
}
impl exports::local::proxy_test::entry::Guest for Consumer {
    fn run(value: u32) -> u32 {
        local::proxy_test::arithmetic::increment(value) * 2
    }
    fn run_flow(
        implements: String,
        input: String,
    ) -> Result<tobkiri::core::types::StreamHandle, tobkiri::core::types::Error> {
        tobkiri::core::flow::run_flow(&implements, &input)
    }
    fn run_callback(value: u32) -> Result<u32, tobkiri::core::types::Error> {
        local::proxy_test::arithmetic::with_callback(
            tobkiri::core::types::CallbackHandle { id: 1 },
            value,
        )
    }
    fn open_callback() -> Result<tobkiri::core::types::CallbackHandle, tobkiri::core::types::Error>
    {
        Ok(tobkiri::core::types::CallbackHandle { id: 1 })
    }
}
impl exports::tobkiri::core::callback_sink::Guest for Consumer {
    fn invoke(id: u64, args: String) -> Result<String, tobkiri::core::types::Error> {
        if id != 1 {
            return Err(tobkiri::core::types::Error {
                code: tobkiri::core::types::ErrorCode::NotFound,
                message: "callback fixture absent".into(),
            });
        }
        args.parse::<u32>()
            .map(|v| (v * 3).to_string())
            .map_err(|_| tobkiri::core::types::Error {
                code: tobkiri::core::types::ErrorCode::Invalid,
                message: "callback fixture integer required".into(),
            })
    }
}
export!(Consumer);
