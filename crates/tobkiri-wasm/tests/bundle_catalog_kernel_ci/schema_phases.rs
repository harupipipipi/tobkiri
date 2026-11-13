//! Fixed host fuel checkpoints for one actual, unsplit cold App schema-store call.
use super::*;
use serde::ser::{Serialize, SerializeSeq, Serializer};
use std::sync::Mutex;
use tobkiri_canon::ControlHandle;
use tobkiri_gate::{ControlKind, ControlReservation, NativeSetupInvocation};
use tobkiri_wasm::{
    NativeDataIo, NativeDataIoError, NativeImportBinding, NativeImportContext, NativeImportInput,
    NativeImportOperation, NativeImportState,
};

const INTERFACE: &str = "tobkiri-native:app-schema-phases/bindings@1.0.0";
const SCHEMAS: usize = 42;
const NAME_BYTES: usize = 128;
const MAX_FUEL: u64 = 2_000_000_000;
const MAX_MEMORY: u64 = 64 * 1024 * 1024;

#[derive(Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "kebab-case")]
enum Phase {
    Setup,
    InitialReference,
    Evaluation,
    Return,
}

impl Phase {
    fn parse(text: &str) -> Result<Self> {
        match text {
            "setup" => Ok(Self::Setup),
            "initial-reference" => Ok(Self::InitialReference),
            "evaluation" => Ok(Self::Evaluation),
            "return" => Ok(Self::Return),
            _ => anyhow::bail!("unknown App schema phase"),
        }
    }
}

const MAX_ROWS: usize = SCHEMAS * 4;

#[derive(Clone, Copy)]
struct SchemaName {
    bytes: [u8; NAME_BYTES],
    len: usize,
}

impl SchemaName {
    const EMPTY: Self = Self {
        bytes: [0; NAME_BYTES],
        len: 0,
    };

    fn from_resource(path: &str) -> Result<Self> {
        let name = path
            .strip_prefix("tobkiri_protocol/schemas/")
            .context("actual original schema path required")?;
        ensure!(
            name.len() > ".schema.json".len()
                && name.len() <= NAME_BYTES
                && name.ends_with(".schema.json")
                && name
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"-_.".contains(&byte)),
            "closed original schema file name required"
        );
        let mut output = Self::EMPTY;
        output.len = name.len();
        output.bytes[..name.len()].copy_from_slice(name.as_bytes());
        Ok(output)
    }

    fn as_str(&self) -> &str {
        std::str::from_utf8(&self.bytes[..self.len]).expect("checked ASCII schema name")
    }
}

#[derive(Clone, Copy)]
struct Row {
    schema: usize,
    phase: Phase,
    fuel_remaining: u64,
    fuel_since_previous_sample: u64,
}

struct Trace {
    names: [SchemaName; SCHEMAS],
    rows: [Option<Row>; MAX_ROWS],
    len: usize,
    returned: usize,
    fuel_after_prepare: Option<u64>,
}

impl Trace {
    fn new(names: [SchemaName; SCHEMAS]) -> Self {
        Self {
            names,
            rows: [None; MAX_ROWS],
            len: 0,
            returned: 0,
            fuel_after_prepare: None,
        }
    }

    fn last(&self) -> Option<Row> {
        self.len.checked_sub(1).and_then(|index| self.rows[index])
    }

    fn record(&mut self, name: &str, phase: Phase, fuel_remaining: u64) -> Result<()> {
        let prepared = self
            .fuel_after_prepare
            .context("cold schema operation has not started")?;
        let schema = self
            .names
            .binary_search_by(|known| known.as_str().cmp(name))
            .map_err(|_| anyhow::anyhow!("marker is outside the original schema inventory"))?;
        let previous = self.last();
        let valid = match (previous, phase) {
            (None, Phase::Setup) => true,
            (Some(row), Phase::Setup) => row.phase == Phase::Return && schema > row.schema,
            (Some(row), Phase::InitialReference) => {
                row.phase == Phase::Setup && schema == row.schema
            }
            (Some(row), Phase::Evaluation) => {
                row.phase == Phase::InitialReference && schema == row.schema
            }
            // A real error in the shared implementation may return before the evaluator.
            (Some(row), Phase::Return) => row.phase != Phase::Return && schema == row.schema,
            _ => false,
        };
        ensure!(
            valid && self.len < MAX_ROWS,
            "out-of-order or excess App schema phase"
        );
        let fuel_since_previous_sample = previous
            .map_or(prepared, |row| row.fuel_remaining)
            .checked_sub(fuel_remaining)
            .context("App schema phase fuel increased")?;
        self.rows[self.len] = Some(Row {
            schema,
            phase,
            fuel_remaining,
            fuel_since_previous_sample,
        });
        self.len += 1;
        if phase == Phase::Return {
            self.returned += 1;
        }
        Ok(())
    }
}

struct Checkpoints<'a>(&'a Trace);

impl Serialize for Checkpoints<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        #[derive(serde::Serialize)]
        struct Item<'a> {
            schema: &'a str,
            phase: Phase,
            fuel_remaining: u64,
            fuel_since_previous_sample: u64,
        }
        let trace = self.0;
        let mut seq = serializer.serialize_seq(Some(trace.len))?;
        for row in trace.rows[..trace.len].iter().flatten() {
            seq.serialize_element(&Item {
                schema: trace.names[row.schema].as_str(),
                phase: row.phase,
                fuel_remaining: row.fuel_remaining,
                fuel_since_previous_sample: row.fuel_since_previous_sample,
            })?;
        }
        seq.end()
    }
}

type HeldTrace = ControlHandle<Mutex<Trace>>;

struct State {
    invocation: NativeSetupInvocation,
    trace: HeldTrace,
}

impl NativeImportState for State {
    fn native_import_invocation(&self) -> &NativeSetupInvocation {
        &self.invocation
    }
}

fn checkpoint(context: NativeImportContext<'_, State>) -> Result<NativeImportOperation> {
    let state = context.state();
    ensure!(
        context.same_invocation(&state.invocation),
        "foreign phase observation"
    );
    let fuel = context
        .ci_fuel_at_import_entry()
        .context("App phase import-entry fuel unavailable")?;
    let payload = context
        .input()
        .context("missing App schema phase")?
        .as_str()?;
    ensure!(
        payload.len() <= "initial-reference:".len() + NAME_BYTES,
        "oversized App schema phase"
    );
    let (phase, name) = payload
        .split_once(':')
        .context("invalid App schema phase")?;
    // Observe in the fixed import factory, using the fuel captured before any
    // host parsing or worker admission. Do not retain caller-owned input.
    state
        .trace
        .get()
        .try_lock()
        .map_err(|_| NativeDataIoError::Unavailable)?
        .record(name, Phase::parse(phase)?, fuel)?;
    // This binding can only acknowledge a marker. It never exposes fuel or IO.
    context.inspect(|output| output.append(b"{}"))
}

static IMPORTS: [NativeImportBinding<State>; 1] = [NativeImportBinding {
    interface: INTERFACE,
    function: "checkpoint",
    input: NativeImportInput::String,
    prepare: checkpoint,
}];

#[tokio::test]
#[ignore = "actual App schema phases, one CI-only instrumented artifact and Store"]
async fn actual_app_schema_cold_store_phases() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_BUNDLE_APP_BACKEND").as_deref() == Ok("1")
            && std::env::var("TOBKIRI_BUNDLE_SCHEMA_PROGRESS").as_deref() == Ok("0")
            && std::env::var("TOBKIRI_BUNDLE_SCHEMA_PHASES").as_deref() == Ok("1"),
        "explicit CI-only App phase diagnostic required"
    );
    let original: Value = serde_json::from_slice(&input("CASES", 4 * 1024 * 1024)?)?;
    ensure!(
        original["format"] == "tobkiri.bundle-catalog-comparison/1"
            && original["oracle_commit"] == "54e7da008997a3eb45876cbe18459dba9902cc9d"
            && original["entries"] == 305
            && original["read_occurrences"] == 453
            && original["schema_store_cold_selfcheck_calls"].as_u64() == Some(SCHEMAS as u64)
            && original["schema_store_expected_bytes"]
                .as_u64()
                .is_some_and(|n| n <= 4 * 1024 * 1024)
            && original["schema_store_expected_sha256"]
                .as_str()
                .is_some_and(|s| s.len() == 64),
        "pinned original cold schema-store oracle required"
    );
    let mut names = [SchemaName::EMPTY; SCHEMAS];
    let resources = original["schema_resources"]
        .as_object()
        .context("missing original resources")?;
    ensure!(
        resources.len() == SCHEMAS,
        "complete original42 resource inventory required"
    );
    for (slot, path) in names.iter_mut().zip(resources.keys()) {
        *slot = SchemaName::from_resource(path)?;
    }
    names.sort_unstable_by(|left, right| left.as_str().cmp(right.as_str()));

    let bytes = input("COMPONENT", 32 * 1024 * 1024)?;
    let identity = Digest::hash(&bytes);
    let control = CoreControl::default();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(AllocationOnlyFixture)))?;
    let engine = WasmEngine::new_with_control(&control)?;
    let component = Component::new(engine.engine(), &bytes)?;
    drop(bytes);
    ensure!(
        component
            .component_type()
            .imports(engine.engine())
            .map(|(name, _)| name)
            .eq([INTERFACE]),
        "diagnostic must import exactly the closed App phase binding"
    );
    // Pay the fixed trace/mutex and streaming serialization allowance before
    // the Store starts. The observer has no extra DATA account or guest budget.
    let trace_funds = control.reserve(
        ControlKind::Frame,
        u64::try_from(HeldTrace::storage_bytes::<ControlReservation>())?
            + (size_of::<std::os::raw::c_long>() + 56) as u64
            + 16 * 1024,
    )?;
    let state = Mutex::new(Trace::new(names));
    drop(
        state
            .try_lock()
            .map_err(|_| NativeDataIoError::Unavailable)?,
    );
    let trace = HeldTrace::try_new_precharged(state, trace_funds)?;
    let mut report = json!({
        "format":"tobkiri.app-schema-cold-store-phases/1", "component_sha256":identity.as_str(),
        "operation":"cold-schema-store", "diagnostic_only":true,
        "qualification":false, "production_enabled":false, "whole_catalog_acceptance":false,
        "cold_schema_acceptance":false, "stores":1, "budget_resets":0, "export_calls_started":0,
        "fuel_limit":MAX_FUEL, "memory_limit_bytes":MAX_MEMORY, "wall_seconds":30,
        "fuel_sampling":"fixed-import-entry", "includes_diagnostic_overhead":true,
        "phase_deltas_are_boundary_intervals":true, "per_function_fuel_attribution":false,
        "phase_capacity":MAX_ROWS, "stage":"prepare",
    });
    let mut io: Option<NativeDataIo> = None;
    let started = Instant::now();
    let outcome: Result<()> = async {
        let invocation = account.begin()?;
        let deadline = invocation.deadline()?;
        let state = State {
            invocation: invocation.clone(),
            trace: trace.clone(),
        };
        let store = engine.native_setup_store_with_imports(invocation, state, &IMPORTS)?;
        io = Some(
            store
                .native_data_io()
                .context("original phase inspection IO missing")?
                .clone(),
        );
        let owners = store.allocation_owners().clone();
        let mut program = store
            .prepare_data_program(&component, &[CATALOG_EXPORT])
            .await?;
        let prepared = program.fuel_remaining()?;
        ensure!(
            prepared <= MAX_FUEL,
            "prepared fuel exceeds original budget"
        );
        report["fuel_after_prepare"] = json!(prepared);
        let payload = CoreBuffer::copy_from(COLD_SCHEMA_STORE_PAYLOAD, owners.graph())?;
        trace
            .get()
            .try_lock()
            .map_err(|_| NativeDataIoError::Unavailable)?
            .fuel_after_prepare = Some(prepared);
        report["stage"] = json!("invoke-cold-schema-store");
        report["export_calls_started"] = json!(1);
        let reply = program.invoke(0, Some(&payload)).await;
        let observation = program.observation();
        report["observation"] = serde_json::to_value(observation)?;
        report["original_deadline_preserved"] = json!(program.original_deadline() == deadline);
        ensure!(
            program.original_deadline() == deadline,
            "original deadline changed"
        );
        let fuel_stop = reply.as_ref().err().is_some_and(|error| {
            error.downcast_ref::<wasmtime::Trap>() == Some(&wasmtime::Trap::OutOfFuel)
                || error.chain().any(|cause| {
                    cause.downcast_ref::<wasmtime::Trap>() == Some(&wasmtime::Trap::OutOfFuel)
                })
        });
        if fuel_stop {
            ensure!(
                observation.fuel_remaining == Some(0)
                    && observation.store_retired
                    && !observation.deadline_exceeded
                    && !observation.unreachable_trap
                    && !observation.post_return
                    && !observation.post_return_failed,
                "unexpected original fuel-stop observation"
            );
            report["stage"] = json!("original-fuel-stop");
            report["store_retired_after_operation"] = json!(observation.store_retired);
            return Ok(());
        }
        let Ok(NativeDataValue::String(actual)) = reply? else {
            anyhow::bail!("cold schema phase call returned an unexpected DATA result");
        };
        ensure!(
            observation.post_return
                && !observation.store_retired
                && !observation.deadline_exceeded
                && !observation.post_return_failed,
            "actual post-return or original Store lifetime differs"
        );
        let output_digest = Digest::hash(actual.as_bytes());
        report["schema_store_output_bytes"] = json!(actual.as_bytes().len());
        report["schema_store_output_sha256"] = json!(output_digest.hex());
        ensure!(
            original["schema_store_expected_bytes"].as_u64()
                == Some(actual.as_bytes().len() as u64)
                && original["schema_store_expected_sha256"] == output_digest.hex(),
            "actual original cold schema store/cache outcome differs"
        );
        drop((actual, payload, owners));
        program.retire();
        report["store_retired_after_operation"] = json!(program.observation().store_retired);
        report["stage"] = json!("returned-original-cold-schema-store");
        Ok(())
    }
    .await;
    let io_cleanup = match io {
        Some(io) => io.retire_and_wait().await.is_ok(),
        None => false,
    };
    let reaped = tobkiri_wasm::reap_allocation_owners().is_ok();
    let snapshot = account.snapshot().ok();
    let trace = trace
        .get()
        .try_lock()
        .map_err(|_| NativeDataIoError::Unavailable)?;
    let cleanup_ok = io_cleanup
        && reaped
        && report["store_retired_after_operation"] == true
        && snapshot.is_some_and(|s| {
            s.limit_bytes == MAX_MEMORY
                && s.peak_charged_bytes <= MAX_MEMORY
                && s.charged_bytes == 0
                && s.reservations == 0
        });
    let reached_expected_boundary = if report["stage"] == "returned-original-cold-schema-store" {
        trace.returned == SCHEMAS
    } else {
        trace.len > 0 && report["stage"] == "original-fuel-stop"
    };
    let tail_fuel = trace.last().and_then(|row| {
        let end = report["observation"]["fuel_remaining"].as_u64()?;
        row.fuel_remaining.checked_sub(end)
    });
    let valid = outcome.is_ok() && cleanup_ok && reached_expected_boundary && tail_fuel.is_some();
    report["diagnostic_observation_valid"] = json!(valid);
    report["elapsed_seconds"] = json!(started.elapsed().as_secs_f64());
    report["phase_rows"] = json!(trace.len);
    report["native_returns_observed"] = json!(trace.returned);
    report["fuel_after_last_checkpoint"] = json!(tail_fuel);
    report["io_cleanup_success"] = json!(io_cleanup);
    report["allocation_reap_success"] = json!(reaped);
    report["peak_charged_bytes"] = json!(snapshot.map(|s| s.peak_charged_bytes));
    report["payload_after_physical_drop"] = json!(snapshot.map(|s| s.charged_bytes));
    report["reservations_after_reap"] = json!(snapshot.map(|s| s.reservations));
    #[derive(serde::Serialize)]
    struct Receipt<'a> {
        #[serde(flatten)]
        summary: &'a Value,
        checkpoints: Checkpoints<'a>,
    }
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(PathBuf::from(std::env::var("TOBKIRI_BUNDLE_REPORT")?))?;
    serde_json::to_writer_pretty(
        file,
        &Receipt {
            summary: &report,
            checkpoints: Checkpoints(&trace),
        },
    )?;
    outcome?;
    ensure!(
        valid,
        "cold schema diagnostic lacks valid original phase or cleanup evidence"
    );
    Ok(())
}
