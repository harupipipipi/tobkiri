//! Isolated diagnostic of the actual pinned interpreter, not product acceptance.
use crate::{BootstrapInputOwner, BootstrapRoots, NativeComponentPreparation};
use anyhow::{Context, Result, ensure};
use std::{
    fmt::Write as _,
    fs::{File, OpenOptions},
    future::{Future, poll_fn},
    io::{Read, Write},
    os::unix::fs::OpenOptionsExt,
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{
        Condvar, Mutex,
        atomic::{AtomicU8, Ordering},
    },
    task::Poll,
    time::{Duration, Instant},
};
use tobkiri_canon::{ControlHandle, CoreValue, Digest};
use tobkiri_core::RuntimeOwner;
use tobkiri_gate::{
    CapacityKind, ControlKind, ControlReservation, NativeSetupInvocation, NativeSetupReservation,
};
use tobkiri_wasm::{
    NativeDataBinding, NativeDataExport, NativeDataIoError, NativeDataValue, NativeImportBinding,
    NativeImportContext, NativeImportInput, NativeImportOperation, NativeImportState,
};

const CHILD: &str = "thread_capabilities_actual_ci::isolated_thread_capabilities_child";
const PREFIX: &str = "THREAD_CAPABILITIES_CHILD ";
const INTERFACE: &str = "tobkiri-ui:thread-capabilities/probe@1.0.0";
const STAGES: [&str; 15] = [
    "starting",
    "import-contextvars",
    "contextvars-imported",
    "import-struct",
    "struct-imported",
    "import-sys",
    "sys-imported",
    "import-threading",
    "threading-imported",
    "local-context",
    "thread-start",
    "thread-coordination",
    "worker-waiting",
    "thread-join",
    "completed",
];
const WORKER_WAITING_STAGE: u8 = 13;

// This owns only actual diagnostic code. It cannot provide original Host roots
// or authorize any filesystem, Profile, approval or native domain operation.
struct CodeSource(File);
impl BootstrapInputOwner for CodeSource {
    fn with_roots<R>(
        &self,
        _: impl for<'a> FnOnce(BootstrapRoots<'a>) -> R,
    ) -> std::result::Result<R, NativeDataIoError> {
        Err(NativeDataIoError::Unavailable)
    }
    fn check_live(&self) -> std::result::Result<(), NativeDataIoError> {
        match self.0.metadata() {
            Ok(metadata) if metadata.is_file() => Ok(()),
            _ => Err(NativeDataIoError::Retired),
        }
    }
}
struct Funding {
    _native: NativeSetupReservation,
    _control: ControlReservation,
}
struct Work {
    invocation: NativeSetupInvocation,
    _source: CodeSource,
    stage: AtomicU8,
    hold_waiter: bool,
    release: Mutex<bool>,
    wake: Condvar,
    changed: tokio::sync::Notify,
}
struct State(ControlHandle<Work>);
impl NativeImportState for State {
    fn native_import_invocation(&self) -> &NativeSetupInvocation {
        &self.0.get().invocation
    }
}
const IMPORTS: [NativeImportBinding<State>; 1] = [NativeImportBinding {
    interface: "tobkiri-native:thread-capabilities/bindings@1.0.0",
    function: "inspect",
    input: NativeImportInput::String,
    prepare: checkpoint,
}];
fn checkpoint(context: NativeImportContext<'_, State>) -> Result<NativeImportOperation> {
    let work = context.state().0.clone();
    ensure!(
        context.same_invocation(&work.get().invocation),
        "foreign diagnostic invocation"
    );
    let input = context.input().context("checkpoint missing")?;
    ensure!(input.len() <= 512, "checkpoint exceeds closed bound");
    let value = CoreValue::parse_guest(input.as_bytes(), context.allocation_owners().graph())?;
    ensure!(
        crate::source_metadata::exact(&value, &["format", "stage"]).is_some()
            && value
                .get_key("format")
                .is_some_and(|v| v.as_str() == Some("tobkiri.thread-capabilities.checkpoint.v1")),
        "invalid checkpoint frame"
    );
    let stage_value = value.get_key("stage").context("stage missing")?;
    let name = stage_value.as_str().context("stage is not a string")?;
    let stage = u8::try_from(
        STAGES
            .iter()
            .position(|s| *s == name)
            .context("unknown stage")?
            + 1,
    )?;
    let custody = work.project(|v| v as &(dyn std::any::Any + Send + Sync));
    context.inspect_retaining(custody, move |output| {
        let state = work.get();
        state._source.check_live()?;
        if !state.invocation.same_invocation(output.invocation())
            || state
                .stage
                .compare_exchange(stage - 1, stage, Ordering::AcqRel, Ordering::Acquire)
                .is_err()
        {
            return Err(NativeDataIoError::Unavailable);
        }
        state.changed.notify_one();
        if stage == WORKER_WAITING_STAGE && state.hold_waiter {
            let release = state
                .release
                .lock()
                .map_err(|_| NativeDataIoError::CompletionUnknown)?;
            let remaining = state
                .invocation
                .deadline()
                .map_err(|_| NativeDataIoError::Retired)?
                .checked_duration_since(Instant::now())
                .ok_or(NativeDataIoError::Deadline)?;
            let (released, timeout) = state
                .wake
                .wait_timeout_while(release, remaining, |v| !*v)
                .map_err(|_| NativeDataIoError::CompletionUnknown)?;
            if timeout.timed_out() || !*released {
                return Err(NativeDataIoError::Deadline);
            }
        }
        output.append(b"{}")
    })
}

// Error formatting cannot allocate an unbounded backtrace into the receipt.
struct ErrorText {
    bytes: [u8; 1024],
    length: usize,
    truncated: bool,
}

// A parent-side failure must still reap its actual child before fixture removal.
struct ChildGuard {
    child: Child,
    reaped: bool,
}
impl Drop for ChildGuard {
    fn drop(&mut self) {
        if !self.reaped {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}
impl std::fmt::Write for ErrorText {
    fn write_str(&mut self, text: &str) -> std::fmt::Result {
        if self.truncated {
            return Ok(());
        }
        for c in text.chars() {
            let mut encoded = [0; 4];
            let encoded = c.encode_utf8(&mut encoded).as_bytes();
            if self.length + encoded.len() > self.bytes.len() {
                self.truncated = true;
                break;
            }
            self.bytes[self.length..self.length + encoded.len()].copy_from_slice(encoded);
            self.length += encoded.len();
        }
        Ok(())
    }
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "runs only inside the parent's separate bounded diagnostic process"]
async fn isolated_thread_capabilities_child() -> Result<()> {
    if let Err(original) = child_operation().await {
        let mut text = ErrorText {
            bytes: [0; 1024],
            length: 0,
            truncated: false,
        };
        let _ = write!(text, "{original}");
        let _ = std::io::stderr().write_all(&text.bytes[..text.length]);
        // child_operation deliberately does not drop a possibly-live program
        // on its own error paths. This is an abnormal diagnostic process exit.
        std::process::exit(86);
    }
    std::process::exit(0);
}

async fn child_operation() -> Result<()> {
    ensure!(
        std::env::var("TOBKIRI_TEST_THREAD_CAPABILITIES_CI").as_deref() == Ok("1"),
        "explicit diagnostic CI required"
    );
    let scenario = std::env::var("TOBKIRI_THREAD_SCENARIO")?;
    ensure!(
        matches!(scenario.as_str(), "normal" | "waiter-drop"),
        "unknown diagnostic case"
    );
    let artifact = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_THREAD_COMPONENT").context("component missing")?,
    );
    ensure!(
        artifact.is_absolute() && artifact.canonicalize()? == artifact,
        "physical component required"
    );
    let expected = Digest::parse(&format!(
        "sha256:{}",
        std::env::var("TOBKIRI_TEST_THREAD_COMPONENT_SHA256")?
    ))?;
    let directory =
        PathBuf::from(std::env::var_os("TOBKIRI_THREAD_OWNER").context("isolated owner missing")?);
    let owner = RuntimeOwner::open(directory.join("data"))?;
    let invocation = owner.begin_native_setup()?;
    let original_deadline = invocation.deadline()?;
    let prepared =
        NativeComponentPreparation::new(invocation.clone(), CodeSource(File::open(&artifact)?))?
            .compile(File::open(&artifact)?, &expected)?;
    let preparation = prepared.observation();
    let (engine, component, invocation, source, cancellation) = prepared.into_diagnostic_parts()?;
    let size = u64::try_from(ControlHandle::<Work>::storage_bytes::<Funding>())?;
    let work = ControlHandle::try_new_precharged(
        Work {
            invocation: invocation.clone(),
            _source: source,
            stage: AtomicU8::new(0),
            hold_waiter: scenario == "waiter-drop",
            release: Mutex::new(false),
            wake: Condvar::new(),
            changed: tokio::sync::Notify::new(),
        },
        Funding {
            _native: invocation.reserve(CapacityKind::VfsBuffer, size)?,
            _control: invocation.control().reserve(ControlKind::Frame, size)?,
        },
    )?;
    let store = engine.native_setup_store_with_imports_and_cancellation_parent(
        invocation.clone(),
        State(work.clone()),
        &IMPORTS,
        &cancellation,
    )?;
    let io = store.native_data_io().context("actual IO missing")?.clone();
    let budget = store.allocation_owners().graph().clone();
    let bindings = [
        NativeDataBinding {
            export: NativeDataExport::new(Some(INTERFACE), "run"),
            result: None,
        },
        NativeDataBinding {
            export: NativeDataExport::new(Some(INTERFACE), "ping"),
            result: None,
        },
    ];
    let program = store.prepare_data_program(&component, &bindings).await?;
    // The kernel's own failure retirement remains unchanged. Additional caller
    // errors must not introduce a second ordinary Drop of an unconfirmed VM.
    let mut program = std::mem::ManuallyDrop::new(program);
    let reporting_bytes = u64::try_from(16 * 1024 + std::mem::size_of::<ErrorText>())?;
    let _record_native = invocation.reserve(CapacityKind::VfsBuffer, reporting_bytes)?;
    let _record_control = invocation
        .control()
        .reserve(ControlKind::Frame, reporting_bytes)?;
    let mut guest_record = [0_u8; 16 * 1024];
    let mut guest_length = 0;
    let mut waiter_dropped = false;
    let result = if scenario == "waiter-drop" {
        let mut running = std::pin::pin!(program.invoke(0, None));
        let reached = async {
            loop {
                let notified = work.get().changed.notified();
                if work.get().stage.load(Ordering::Acquire) >= WORKER_WAITING_STAGE {
                    break;
                }
                notified.await;
            }
        };
        tokio::select! {
            result = &mut running => result,
            _ = reached => {
                let pending = {
                    let mut waiter = std::pin::pin!(async { running.as_mut().await });
                    poll_fn(|cx| Poll::Ready(matches!(waiter.as_mut().poll(cx), Poll::Pending))).await
                };
                ensure!(pending, "owned call completed before waiter observation");
                waiter_dropped = true;
                *work.get().release.lock().map_err(|_| anyhow::anyhow!("release poisoned"))? = true;
                work.get().wake.notify_one();
                running.await
            }
        }
    } else {
        program.invoke(0, None).await
    };
    let observation = program.observation();
    let fuel = program.fuel_remaining().ok().or(observation.fuel_remaining);
    let mut error = ErrorText {
        bytes: [0; 1024],
        length: 0,
        truncated: false,
    };
    let guest = match &result {
        _ if observation.store_retired => {
            if let Err(original) = &result {
                write!(error, "{original:#}")?;
            }
            None
        }
        Ok(Ok(NativeDataValue::String(data))) => Some(data.as_bytes()),
        Ok(_) => {
            write!(error, "unexpected diagnostic result")?;
            None
        }
        Err(original) => {
            write!(error, "{original:#}")?;
            None
        }
    };
    let mut confirmed = false;
    let mut classification = "runtime-failure-unclassified";
    if let Some(bytes) = guest {
        ensure!(
            bytes.len() <= 16 * 1024,
            "diagnostic result exceeds fixed bound"
        );
        let value = CoreValue::parse_guest(bytes, &budget)?;
        ensure!(
            value
                .get_key("format")
                .is_some_and(|v| v.as_str() == Some("tobkiri.thread-capabilities.observation.v1")),
            "wrong observation frame"
        );
        confirmed = value
            .get_key("termination_confirmed")
            .and_then(|v| v.as_bool())
            == Some(true);
        classification = if confirmed {
            "worker-join-confirmed"
        } else if value
            .get_key("failure")
            .and_then(|v| v.get_key("stage"))
            .is_some_and(|v| v.as_str() == Some("thread-start"))
        {
            "start-refused-termination-unconfirmed"
        } else {
            "termination-unconfirmed"
        };
        // Freeze the returned DATA into independently funded host storage
        // before any explicit Store retirement or physical cleanup.
        guest_record[..bytes.len()].copy_from_slice(bytes);
        guest_length = bytes.len();
    }
    let physical_cleanup = if confirmed {
        program.retire();
        io.retire_and_wait().await.is_ok()
    } else {
        false
    };
    let snapshot = invocation.capacity_snapshot()?;
    let mut stdout = std::io::stdout().lock();
    write!(
        stdout,
        "{PREFIX}{{\"format\":\"tobkiri.thread-capabilities.native.v1\",\"diagnostic_only\":true,\"whole_component_parity\":false,\"individual_task_cancellation_proven\":false,\"same_store_independent_entry_tested\":false,\"guest_json\":"
    )?;
    let guest_text = if guest_length == 0 {
        None
    } else {
        Some(std::str::from_utf8(&guest_record[..guest_length])?)
    };
    serde_json::to_writer(&mut stdout, &guest_text)?;
    write!(stdout, ",\"execution_error\":")?;
    serde_json::to_writer(
        &mut stdout,
        std::str::from_utf8(&error.bytes[..error.length])?,
    )?;
    write!(stdout, ",\"execution_error_truncated\":{}", error.truncated)?;
    write!(stdout, ",\"classification\":")?;
    serde_json::to_writer(&mut stdout, classification)?;
    write!(
        stdout,
        ",\"store_retired_at_invoke_return\":{},\"store_retired_for_confirmed_cleanup\":{confirmed},\"native_deadline_exceeded\":{},\"unreachable_trap\":{}",
        observation.store_retired, observation.deadline_exceeded, observation.unreachable_trap
    )?;
    write!(
        stdout,
        ",\"stage\":{},\"waiter_drop_exercised\":{waiter_dropped},\"termination_confirmed\":{confirmed},\"physical_cleanup\":{physical_cleanup},\"elapsed_us\":{},\"preparation_us\":{},\"peak_charged_bytes\":{},\"fuel_remaining\":",
        work.get().stage.load(Ordering::Acquire),
        invocation.started_at().elapsed().as_micros(),
        preparation.elapsed_micros,
        snapshot.peak_charged_bytes
    )?;
    serde_json::to_writer(&mut stdout, &fuel)?;
    writeln!(
        stdout,
        ",\"deadline_unchanged\":{}}}",
        invocation.deadline()? == original_deadline
    )?;
    stdout.flush()?;
    // Unconfirmed real workers must not be followed by dropping/reusing their
    // Store. The parent owns this process and its independent wall-time bound.
    // Process exit is deliberately NOT evidence of individual task cancellation.
    std::process::exit(0);
}

#[test]
#[ignore = "requires authenticated CI-built actual CPython component"]
fn actual_thread_capabilities_in_isolated_processes() -> Result<()> {
    ensure!(
        std::env::var("TOBKIRI_TEST_THREAD_CAPABILITIES_CI").as_deref() == Ok("1"),
        "explicit CI required"
    );
    let evidence = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_THREAD_EVIDENCE").context("evidence missing")?,
    );
    let mut records = Vec::new();
    for scenario in ["normal", "waiter-drop"] {
        let root = tempfile::tempdir()?;
        let mut command = Command::new(std::env::current_exe()?);
        command
            .env_clear()
            .args(["--ignored", "--exact", CHILD, "--nocapture"])
            .env("TOBKIRI_TEST_THREAD_CAPABILITIES_CI", "1")
            .env("TOBKIRI_THREAD_SCENARIO", scenario)
            .env("TOBKIRI_THREAD_OWNER", root.path())
            .env("TMPDIR", root.path())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for name in [
            "TOBKIRI_TEST_THREAD_COMPONENT",
            "TOBKIRI_TEST_THREAD_COMPONENT_SHA256",
            "PATH",
            "LANG",
            "LC_ALL",
            "DYLD_LIBRARY_PATH",
        ] {
            if let Some(value) = std::env::var_os(name) {
                command.env(name, value);
            }
        }
        let mut child = ChildGuard {
            child: command.spawn()?,
            reaped: false,
        };
        let out = child.child.stdout.take().context("stdout missing")?;
        let err = child.child.stderr.take().context("stderr missing")?;
        let read = |mut pipe: Box<dyn Read + Send>| -> Result<Vec<u8>> {
            let mut bytes = Vec::new();
            pipe.by_ref()
                .take(4 * 1024 * 1024 + 1)
                .read_to_end(&mut bytes)?;
            ensure!(
                bytes.len() <= 4 * 1024 * 1024,
                "isolated diagnostics exceeded envelope"
            );
            Ok(bytes)
        };
        let stdout = std::thread::spawn(move || read(Box::new(out)));
        let stderr = std::thread::spawn(move || read(Box::new(err)));
        let deadline = Instant::now() + Duration::from_secs(40);
        let mut process_timeout = false;
        let status = loop {
            if let Some(status) = child.child.try_wait()? {
                child.reaped = true;
                break status;
            }
            if Instant::now() >= deadline {
                process_timeout = true;
                child.child.kill()?;
                let status = child.child.wait()?;
                child.reaped = true;
                break status;
            }
            std::thread::sleep(Duration::from_millis(25));
        };
        let stdout = stdout
            .join()
            .map_err(|_| anyhow::anyhow!("stdout reader failed"))??;
        let stderr = stderr
            .join()
            .map_err(|_| anyhow::anyhow!("stderr reader failed"))??;
        let text = std::str::from_utf8(&stdout)?;
        let record = if process_timeout {
            serde_json::json!({"diagnostic_only":true,"scenario":scenario,"runner_timeout":true,"individual_task_cancellation_proven":false,"process_exit_confirmed":true,"thread_termination_confirmed":false,"classification":"forced-process-exit-unknown-thread"})
        } else {
            ensure!(
                status.success(),
                "isolated diagnostic failed: {}",
                String::from_utf8_lossy(&stderr[..stderr.len().min(2048)])
            );
            ensure!(
                text.matches(PREFIX).count() == 1,
                "child observation missing or repeated"
            );
            let raw = text
                .split_once(PREFIX)
                .context("child observation missing")?
                .1
                .lines()
                .next()
                .context("empty child observation")?;
            let record: serde_json::Value = serde_json::from_str(raw)?;
            ensure!(
                record["diagnostic_only"] == true
                    && record["whole_component_parity"] == false
                    && record["individual_task_cancellation_proven"] == false
                    && record["deadline_unchanged"] == true,
                "diagnostic scope changed"
            );
            ensure!(
                record["peak_charged_bytes"]
                    .as_u64()
                    .is_some_and(|v| v <= tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES),
                "original payload bound changed"
            );
            ensure!(
                record["fuel_remaining"].is_null()
                    || record["fuel_remaining"]
                        .as_u64()
                        .is_some_and(|v| v <= 2_000_000_000),
                "original fuel bound changed"
            );
            record
        };
        println!("THREAD_CAPABILITIES_OBSERVATION scenario={scenario} {record}");
        records.push(serde_json::json!({"scenario":scenario,"record":record}));
    }
    ensure!(
        evidence.is_absolute()
            && evidence
                .parent()
                .context("evidence parent")?
                .canonicalize()?
                == evidence.parent().unwrap(),
        "physical evidence parent required"
    );
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(evidence)?;
    serde_json::to_writer_pretty(
        file,
        &serde_json::json!({"diagnostic_only":true,"product_acceptance":false,"scenarios":records}),
    )?;
    Ok(())
}
