//! CI-only observer of the real native DATA runner; never native OS-owner proof.
use super::*;
use serde::Serialize;
use std::fmt::Write;
use std::fs::OpenOptions;
use std::path::PathBuf;
use std::sync::Arc;
use tobkiri_canon::Digest;
use tobkiri_gate::{CapacityLedger, CoreControl, GateError, NativeSetupLease};

struct PortableOwner;
impl NativeSetupLease for PortableOwner {
    fn validate(&self) -> Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"ci-native-schema-portable-owner-not-an-os-proof")
    }
}

// Diagnostics are bounded even if a trap contains many Wasm frames. This is
// observer metadata, outside the guest/ABI/output reservation being measured.
struct Trace(String);
impl std::fmt::Write for Trace {
    fn write_str(&mut self, text: &str) -> std::fmt::Result {
        let mut end = text.len().min(4096 - self.0.len());
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        self.0.push_str(&text[..end]);
        Ok(())
    }
}

#[derive(Serialize)]
struct Report<'a> {
    format: &'static str,
    component_sha256: &'a str,
    operation: &'a str,
    value: Option<&'a str>,
    error: Option<&'a str>,
    genuine_unreachable: bool,
    observation: NativeDataObservation,
    original_deadline_preserved: bool,
    elapsed_seconds: f64,
    memory_limit_bytes: u64,
    fuel_limit: u64,
    wall_seconds: u64,
    portable_owner_fixture: bool,
    actual_os_owner_proof: bool,
    production_enabled: bool,
}

#[tokio::test]
#[ignore = "external real component; Linux CI diagnostic only"]
async fn native_schema_component() -> Result<()> {
    if std::env::var("TOBKIRI_NATIVE_DATA_CI").as_deref() != Ok("1") {
        bail!("native schema observation requires the explicit CI test harness");
    }
    let path =
        PathBuf::from(std::env::var_os("TOBKIRI_NATIVE_COMPONENT").context("component path")?);
    let expected = std::env::var("TOBKIRI_NATIVE_COMPONENT_SHA256")?;
    let operation = std::env::var("TOBKIRI_NATIVE_OPERATION")?;
    if !matches!(
        operation.as_str(),
        "run" | "probe-clock" | "probe-monotonic" | "probe-denied-io"
    ) {
        bail!("unknown schema observation operation");
    }
    let argument = std::env::var("TOBKIRI_NATIVE_ARGUMENT").ok();
    if (operation == "probe-denied-io") != argument.is_some()
        || argument
            .as_deref()
            .is_some_and(|value| !matches!(value, "filesystem" | "random" | "subscribe" | "poll"))
    {
        bail!("schema observation arguments differ from the original four denials");
    }
    let output_path =
        PathBuf::from(std::env::var_os("TOBKIRI_NATIVE_REPORT").context("report path")?);
    let metadata = std::fs::symlink_metadata(&path)?;
    if !metadata.is_file() || metadata.len() > 32 * 1024 * 1024 {
        bail!("component exceeds the original artifact boundary");
    }
    let bytes = std::fs::read(&path)?;
    let identity = Digest::hash(&bytes);
    if identity.as_str() != expected || bytes.len() as u64 != metadata.len() {
        bail!("actual component identity changed");
    }

    let control = CoreControl::default();
    let ledger = CapacityLedger::new();
    let account = ledger.native_setup_account(&control, || Ok(Arc::new(PortableOwner)))?;
    let engine = crate::WasmEngine::new_with_control(&control)?;
    // As in the original observer, compilation precedes the invocation lease.
    let component = Component::new(engine.engine(), &bytes)?;
    drop(bytes);
    let started = Instant::now();
    let invocation = account.begin()?;
    let deadline = invocation.deadline()?;
    let store = engine.native_setup_store(invocation, ())?;
    let input = argument
        .as_ref()
        .map(|text| CoreBuffer::copy_from(text.as_bytes(), store.allocation_owners().graph()))
        .transpose()?;
    let mut entry = store
        .prepare_data_entry(
            &component,
            NativeDataExport::new(Some("tobkiri-ui:schema-probe/probe@1.0.0"), &operation),
        )
        .await?;
    let result = entry.invoke(input.as_ref()).await;
    let deadline_preserved = entry.original_deadline() == deadline;
    let elapsed = started.elapsed().as_secs_f64();
    let (output, failure) = match result {
        Ok(output) => (Some(output), None),
        Err(error) => (None, Some(error)),
    };
    let mut trace = Trace(String::with_capacity(4096));
    if let Some(error) = &failure {
        write!(&mut trace, "{error:?}")?;
    }
    entry.retire();
    let observation = entry.observation();
    let report = Report {
        format: "tobkiri.native-data-schema-observation/1",
        component_sha256: identity.as_str(),
        operation: &operation,
        value: output.as_ref().map(CoreBuffer::as_str).transpose()?,
        error: failure.as_ref().map(|_| trace.0.as_str()),
        genuine_unreachable: observation.unreachable_trap,
        observation,
        original_deadline_preserved: deadline_preserved,
        elapsed_seconds: elapsed,
        memory_limit_bytes: tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES,
        fuel_limit: super::super::FUEL,
        wall_seconds: 30,
        portable_owner_fixture: true,
        actual_os_owner_proof: false,
        production_enabled: false,
    };
    // Stream a borrowed view of the still-funded CoreBuffer; do not extract an
    // unowned guest String/Vec. The test harness is not a product IO API.
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output_path)?;
    serde_json::to_writer(&mut file, &report)?;
    std::io::Write::write_all(&mut file, b"\n")?;
    drop(output);
    drop(input);
    drop(entry);
    crate::reap_allocation_owners()?;
    assert_eq!(account.snapshot()?.charged_bytes, 0);
    Ok(())
}
