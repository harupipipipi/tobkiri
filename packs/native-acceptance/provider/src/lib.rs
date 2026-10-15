//! Opt-in acceptance guest. No storage operation occurs during initialization.
wit_bindgen::generate!({ path: "wit", world: "pack", generate_all });

use exports::local::native_acceptance_provider::review::Guest;
use exports::tobkiri::core::lifecycle;
use tobkiri::core::{storage, types::Error};

struct Pack;

impl lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl Guest for Pack {
    fn ping() -> String {
        "native-acceptance-provider-ready".into()
    }

    fn open_global() -> Result<String, Error> {
        // The actual generic import produces UserGlobalStorage/Open, whose
        // Dangerous risk and fresh approval are assigned by Gate. No guest
        // flag can choose the trusted warning or answer the native sheet.
        let store = storage::open_user_global("native-acceptance-probe")?;
        drop(store);
        // Opening may create this dedicated database and its schema. This
        // fixture never reads, writes, lists or deletes any application value.
        Ok("native-acceptance-global-opened-without-value-access".into())
    }
}

export!(Pack);
