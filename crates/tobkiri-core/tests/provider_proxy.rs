//! Actual provider integration through Core's private execution factory.
//!
//! These fixtures are independently verified Pack directories. The contracts
//! Pack owns the authoritative WIT/sidecars; runtime Packs only provide or
//! consume those shapes. Every invocation starts with the public Core facade,
//! so neither a synthetic ProviderEntryBridge nor a test frame can admit it.
//!
//! Exact migration inventory, in original `tobkiri-wasm/tests/port_proxy.rs`
//! order. "Same name" means the test in this file; all originals are retained.
//! - P01 `rust_consumer_calls_rust_provider_through_real_gate_and_audit`
//!   -> `rust_consumer_calls_rust_provider_through_real_core_gate_and_audit`.
//! - P02 `declared_typed_import_still_cannot_call_without_effective_port_permission`
//!   -> same name.
//! - P03 `input_principal_cannot_select_another_pack_with_the_same_provided_shape`
//!   -> same name plus `actual_native_input_receipt_reaches_its_manifest_flow`.
//! - P04 `actual_float_guest_preserves_bits_and_nonfinite_output_is_rejected`
//!   -> same name.
//! - P05 `guest_stream_opens_once_and_finalizes_once_without_per_pull_audit`
//!   -> same name.
//! - P06 `guest_ui_embed_is_unavailable_even_for_startup_slot_and_is_audited`
//!   -> same name, including a physical outcome-INSERT failure. Its exact
//!   caller/provider WIT pins both use the authoritative Core error, so the
//!   public facade preserves the closed `unavailable` code with fixed text.
//! - P07 `explicit_close_and_cancel_then_pull_surface_audit_sink_failure`
//!   -> `actual_component_close_and_cancelled_pull_surface_physical_audit_failure`
//!   plus raw Core `tracked_stream_tests::{explicit_close_surfaces_audit_failure,
//!   failed_audit_is_visible_after_cancel_and_on_repeated_close,
//!   cancel_then_pull_failure_can_retry_only_its_audit}`.
//! - P08 `expired_stream_and_callback_never_claim_successful_cancellation`
//!   -> same name.
//! - P09 `acknowledged_cleanup_retries_the_same_immutable_cancel_winner_after_deadline`
//!   -> `actual_component_cancel_winner_survives_physical_audit_retry_after_deadline`
//!   plus raw Core `tracked_stream_tests::cancelled_terminal_retry_after_deadline_preserves_original_winner`.
//! - P10 `cancel_after_owner_runtime_shutdown_returns_error_and_durable_expiry_recovers`
//!   -> same name.
//! - P11 `cancellation_from_plain_thread_uses_owning_runtime_and_records_outcome`
//!   -> same name.
//! - P12 `drop_from_plain_thread_completes_cleanup_on_owning_runtime`
//!   -> same name.
//! - P13 `callback_reentry_returns_conflict_instead_of_waiting_for_ancestor_lock`
//!   -> same name.
//! - P14 `guest_run_flow_uses_weak_bridge_and_real_flow_engine_typed_port`
//!   -> `guest_run_flow_uses_actual_core_bridge_and_manifest_flow_typed_port`.
//! - P15 `actual_guest_trap_fuel_and_memory_errors_do_not_poison_following_call`
//!   -> same name.
//! - P16 `missing_hash_bound_execution_budget_is_audited_before_instantiation`
//!   -> same name.
//! - P17 `core_interface_name_with_forged_function_signature_is_rejected_before_init`
//!   -> same name.
//! - P18 `epoch_revocation_closes_live_stream_and_next_fresh_context_recovers`
//!   -> `epoch_revocation_closes_live_stream_and_next_fresh_session_recovers`.
//! - P19 `cancelling_stream_retires_locked_instance_and_drops_pending_host_future`
//!   -> same name.
//! - P20 `stream_uses_own_entry_mediator_budget_across_multiple_pulls`
//!   -> retained, independent entry-Mediator replacement pending. The additional
//!   `stream_preserves_its_source_output_budget_across_multiple_pulls` covers
//!   the producing Pack's cumulative output budget only.
//! - P21 `json_ui_cannot_mint_a_guest_local_callback_pointer` -> same name.
//! - P22 `old_handle_cannot_read_or_cancel_a_recreated_instance_local_id`
//!   -> same name.
//!
//! Stream calls use received UI entries and producer-owned CoreBuffers; User
//! utility calls return values only. Retain all 22 original cases until their
//! complete replacements actually execute, including the pending P20 coverage.

use async_trait::async_trait;
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    future::Future,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    task::{Context, Poll, Wake, Waker},
    time::{Duration, Instant},
};
use tobkiri_canon::{CanonValue, CoreBuffer, Digest, Variant, ids::*};
use tobkiri_core::{
    Core, CoreConfig, CoreError, NativeInputRouteStatus, NativeInputStatus, Session, UiAttachment,
};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, CallCx, CoreCancellation, EffectRequest, Gate,
    GateEngine, HostError, HostIo, LoopLimits, MediatorChain, Outcome, PermissionSet, Principal,
    ResourceLimits, TrustedPrompt, UiEmbed,
};
use tobkiri_manifest::{
    FileEntry, InputDeclaration, PackManifest, PortDeclaration, Ports, RuntimeSpec,
    ShapeDefinition, ShapeKind, ShapeSidecar, UiDefinition, VerifiedPack,
};
use tobkiri_profile::{
    ErrorPolicy, FlowDocument, InputEntry, OverlayAccess, PackPin, PortCall, Profile, Startup,
    Step, StepKind, Trigger,
};
use tobkiri_store::{Approvals, AuditLog, AuditQuota, AuditRow, DataRoot};

type TestResult<T = ()> = Result<T, Box<dyn std::error::Error + Send + Sync>>;

fn note_stage_failure(stage: &'static str, started: Instant, thread: &std::thread::Thread) {
    // Keep the original error, including direct Box downcasts, untouched.
    // Diagnostic output failure must never replace the actual operation error.
    let _ = std::io::Write::write_fmt(
        &mut std::io::stderr(),
        format_args!(
            "FIXTURE_STAGE file={} thread={} stage={} elapsed_us={}\n",
            file!(),
            thread.name().unwrap_or("<unnamed>"),
            stage,
            started.elapsed().as_micros(),
        ),
    );
}

async fn fixture_stage<T, E>(
    stage: &'static str,
    operation: impl Future<Output = Result<T, E>>,
) -> Result<T, E> {
    let started = Instant::now();
    let thread = std::thread::current();
    operation
        .await
        .inspect_err(|_| note_stage_failure(stage, started, &thread))
}

fn fixture_sync_stage<T, E>(
    stage: &'static str,
    operation: impl FnOnce() -> Result<T, E>,
) -> Result<T, E> {
    let started = Instant::now();
    let thread = std::thread::current();
    operation().inspect_err(|_| note_stage_failure(stage, started, &thread))
}

#[tokio::test]
async fn stage_diagnostics_preserve_original_error_identity() {
    let original: Box<dyn std::error::Error + Send + Sync> =
        Box::new(CoreError::Gate(tobkiri_gate::GateError::Deadline));
    let identity = std::ptr::from_ref(original.as_ref());
    let observed: TestResult<()> = fixture_stage(
        "diagnostic-test: async error",
        std::future::ready(Err(original)),
    )
    .await;
    let error = observed.unwrap_err();
    assert!(std::ptr::eq(identity, std::ptr::from_ref(error.as_ref())));
    assert!(matches!(
        error.downcast_ref::<CoreError>(),
        Some(CoreError::Gate(tobkiri_gate::GateError::Deadline))
    ));
    assert!(matches!(
        fixture_sync_stage("diagnostic-test: sync error", || {
            Err::<(), _>(tobkiri_gate::GateError::Deadline)
        }),
        Err(tobkiri_gate::GateError::Deadline)
    ));
}

const PROVIDER: &str = "local.test-provider";
const CONSUMER: &str = "local.test-consumer";
const SUBSTITUTE: &str = "local.test-substitute";
const UI: &str = "local.test-ui";
const UI_GENERATION: u64 = 7;
const PRIVATE_ERROR_MESSAGE: &str = "guest-secret-message-must-never-cross-facade";

fn shape(name: &str) -> ShapeRef {
    ShapeRef::parse(&format!("local.proxy-test.{name}@1.0")).unwrap()
}

fn op(name: &str) -> OpName {
    OpName::parse(name).unwrap()
}

fn pack_id(name: &str) -> PackId {
    PackId::parse(name).unwrap()
}

fn limits() -> ResourceLimits {
    ResourceLimits {
        fuel_per_call: 10_000_000,
        memory_mib: 16,
        wall_ms: 5000,
        table_elements: 10_000,
        output_bytes: 1024 * 1024,
    }
}

fn loops() -> LoopLimits {
    LoopLimits {
        max_iterations: 100,
        max_wall_ms: 5000,
        max_cost_units: 10_000,
    }
}

fn permissions(ports: &[&str]) -> PermissionSet {
    PermissionSet {
        ports: ports.iter().map(|name| shape(name).to_string()).collect(),
        wasm: limits(),
        loops: loops(),
        ..Default::default()
    }
}

fn canonical_bytes(value: &impl serde::Serialize) -> TestResult<Vec<u8>> {
    let value = tobkiri_canon::parse(&serde_json::to_vec(value)?, Variant::StrictV1)?;
    Ok(tobkiri_canon::encode(&value, Variant::StrictV1)?)
}

/// Trusted tests inspect producer output while its original lease stays live.
fn parse_output(buffer: &CoreBuffer) -> TestResult<CanonValue> {
    assert!(buffer.len() <= tobkiri_canon::MAX_BYTES);
    Ok(tobkiri_canon::parse(buffer.as_bytes(), Variant::StrictV1)?)
}

fn args(entries: &[(&str, CanonValue)]) -> CanonValue {
    CanonValue::Obj(
        entries
            .iter()
            .map(|(name, value)| ((*name).into(), value.clone()))
            .collect(),
    )
}

fn error_code(error: &CoreError) -> &str {
    match error {
        CoreError::Step(error) => &error.code,
        CoreError::Gate(error) => error.code(),
        CoreError::Cleanup { original, .. } => error_code(original),
        CoreError::Invalid(_) => "invalid",
        CoreError::Runtime(_) => "runtime",
        _ => "other",
    }
}

#[derive(Default)]
struct Host {
    reviewed_packs: Mutex<BTreeSet<(PackId, PackHash)>>,
    reviewed_users: Mutex<BTreeSet<(Digest, Digest, u64)>>,
    prompts: AtomicUsize,
    ui_embeds: AtomicUsize,
    reads: AtomicUsize,
    lines: Mutex<VecDeque<String>>,
    block_input: AtomicBool,
    read_started: tokio::sync::Notify,
    read_dropped: AtomicUsize,
}

struct PendingRead<'a>(&'a AtomicUsize);
impl Drop for PendingRead<'_> {
    fn drop(&mut self) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}

#[async_trait]
impl TrustedPrompt for Host {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        self.prompts.fetch_add(1, Ordering::SeqCst);
        let reviewed = match prompt.effect {
            EffectRequest::Permissions { pack, hash } => {
                self.reviewed_packs.lock().unwrap().contains(&(pack, hash))
            }
            EffectRequest::UserPermissions {
                profile_hash,
                revision,
                epoch,
            } if matches!(prompt.principal, Principal::User) => self
                .reviewed_users
                .lock()
                .unwrap()
                .contains(&(profile_hash, revision, epoch)),
            EffectRequest::PortCall {
                shape: view,
                op: operation,
            } if matches!(prompt.principal, Principal::User) => {
                (view == shape("arithmetic") || view == shape("entry"))
                    && matches!(
                        operation.as_str(),
                        "run"
                            | "floats"
                            | "nan"
                            | "increment"
                            | "trap"
                            | "spin"
                            | "memory-bomb"
                            | "wait"
                            | "request-ui"
                            | "run-callback"
                            | "open-callback"
                    )
            }
            // The explicit User fixture declaration includes this read-only
            // inspection surface; it grants no provider execution authority.
            EffectRequest::AuditRead if matches!(prompt.principal, Principal::User) => true,
            _ => false,
        };
        if reviewed {
            ApprovalAnswer::Approve
        } else {
            ApprovalAnswer::Deny
        }
    }
}

#[async_trait]
impl HostIo for Host {
    fn display(&self, _: &str, _: &CanonValue) {}

    async fn terminal_read_line_budgeted(
        &self,
        _: &str,
        max_bytes: usize,
        budget: Arc<dyn tobkiri_canon::AllocationBudget>,
    ) -> Result<CoreBuffer, HostError> {
        // Reserve the actual producer storage before observing scripted input
        // or notifying the pending-read test. Never wrap a legacy String read.
        let blocked = self.block_input.load(Ordering::SeqCst);
        let mut buffer = CoreBuffer::builder(&budget, max_bytes);
        buffer
            .write_chunk(max_bytes.min(4096), |destination| {
                self.reads.fetch_add(1, Ordering::SeqCst);
                if blocked {
                    return Ok(0);
                }
                let mut lines = self.lines.lock().unwrap();
                let line = lines
                    .front()
                    .ok_or(tobkiri_canon::AllocationError::Invalid)?;
                if line.len() > destination.len() {
                    return Err(tobkiri_canon::AllocationError::Limit);
                }
                let length = line.len();
                destination[..length].copy_from_slice(line.as_bytes());
                lines.pop_front();
                Ok(length)
            })
            .map_err(|error| HostError(error.to_string()))?;
        if blocked {
            let _pending = PendingRead(&self.read_dropped);
            self.read_started.notify_one();
            std::future::pending::<()>().await;
        }
        buffer
            .finish()
            .map_err(|error| HostError(error.to_string()))
    }

    fn terminal_write_budgeted(&self, text: &CoreBuffer) -> Result<(), HostError> {
        text.as_str()
            .map_err(|error| HostError(error.to_string()))?;
        Ok(())
    }

    async fn terminal_read_line(&self, _: &str) -> Result<String, HostError> {
        self.reads.fetch_add(1, Ordering::SeqCst);
        if self.block_input.load(Ordering::SeqCst) {
            let _pending = PendingRead(&self.read_dropped);
            self.read_started.notify_one();
            return std::future::pending().await;
        }
        self.lines
            .lock()
            .unwrap()
            .pop_front()
            .ok_or_else(|| HostError("scripted proxy input exhausted".into()))
    }

    fn terminal_write(&self, _: &str) {}

    fn embed_ui(&self, embed: UiEmbed) -> Result<UiSessionId, HostError> {
        self.ui_embeds.fetch_add(1, Ordering::SeqCst);
        assert_eq!(embed.pack, pack_id(UI));
        assert_eq!(embed.slot, "startup");
        Ok(UiSessionId::parse("proxy-host-ui").unwrap())
    }

    async fn pick_directory(&self, _: &str) -> Option<PathBuf> {
        None
    }
}

fn artifact(binary: &str) -> TestResult<Vec<u8>> {
    let fallback = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../.work/cargo-target")
        .join("wasm32-wasip2/debug")
        .join(binary);
    let configured = std::env::var_os("CARGO_TARGET_DIR")
        .map(PathBuf::from)
        .map(|root| root.join("wasm32-wasip2/debug").join(binary));
    let path = configured.filter(|path| path.is_file()).unwrap_or(fallback);
    Ok(std::fs::read(path)?)
}

fn port(name: &str) -> PortDeclaration {
    let reference = shape(name);
    PortDeclaration {
        shape: reference.name,
        version: reference.version,
        optional: false,
    }
}

fn manifest(id: &str, runtime: RuntimeSpec, permissions: PermissionSet) -> PackManifest {
    PackManifest {
        format: "tobkiri.pack/1".into(),
        id: pack_id(id),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime,
        shapes: Vec::new(),
        ports: Ports::default(),
        inputs: Vec::new(),
        flows: Vec::new(),
        overlays: Vec::new(),
        stores: Vec::new(),
        permissions,
        ui: None,
        files: Vec::new(),
    }
}

fn write_pack(
    root: &Path,
    mut manifest: PackManifest,
    files: BTreeMap<String, Vec<u8>>,
) -> TestResult<(PackPin, VerifiedPack)> {
    let directory = root.join("packs").join(manifest.id.as_str());
    std::fs::create_dir_all(&directory)?;
    manifest.files = files
        .iter()
        .map(|(path, bytes)| FileEntry {
            path: path.clone(),
            sha256: Digest::hash(bytes),
            size: bytes.len() as u64,
        })
        .collect();
    for (path, bytes) in files {
        let destination = directory.join(path);
        std::fs::create_dir_all(destination.parent().unwrap())?;
        std::fs::write(destination, bytes)?;
    }
    std::fs::write(
        directory.join("pack.json"),
        tobkiri_manifest::canonical_manifest(&manifest)?,
    )?;
    let verified = tobkiri_manifest::verify_directory(&directory, &BTreeMap::new())?;
    let pin = PackPin {
        id: verified.manifest.id.clone(),
        version: verified.manifest.version.clone(),
        sha256: verified.hash.clone(),
        source: format!("file:{}", directory.display()),
    };
    Ok((pin, verified))
}

#[derive(Clone)]
struct Options {
    consumer_allowed: bool,
    substitute_input: bool,
    bind_substitute_input: bool,
    native_input: bool,
    ui_wall_ms: u64,
    provider_output_bytes: u64,
    omit_provider_limit: bool,
    forged_core_import: bool,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            consumer_allowed: true,
            substitute_input: false,
            bind_substitute_input: false,
            native_input: false,
            ui_wall_ms: limits().wall_ms,
            provider_output_bytes: limits().output_bytes,
            omit_provider_limit: false,
            forged_core_import: false,
        }
    }
}

struct Fixture {
    _directory: tempfile::TempDir,
    data_root: PathBuf,
    host: Arc<Host>,
    core: Core,
    profile: Profile,
    verified: Vec<VerifiedPack>,
}

/// Fail only physical terminal INSERTs. Dispatch still reaches the actual
/// guest/import path; removing the trigger permits retrying durable delivery.
/// The production Core receives its ordinary AuditLog, never a fake sink.
struct OutcomeAuditFault {
    connection: rusqlite::Connection,
    active: bool,
}

impl OutcomeAuditFault {
    fn install(fixture: &Fixture) -> TestResult<Self> {
        let path = fixture.data_root.join(DataRoot::profile_relative(
            &fixture.profile.id,
            "audit.sqlite",
        )?);
        assert!(path.is_file(), "Core has not created its actual audit DB");
        let connection = rusqlite::Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
        )?;
        connection.execute_batch(
            "CREATE TRIGGER provider_proxy_outcome_fault BEFORE INSERT ON audit
             WHEN instr(CAST(NEW.body AS TEXT), '\"kind\":\"outcome\"') > 0
             BEGIN SELECT RAISE(FAIL, 'provider proxy outcome sink failure'); END;",
        )?;
        Ok(Self {
            connection,
            active: true,
        })
    }

    fn install_denial(fixture: &Fixture) -> TestResult<Self> {
        let path = fixture.data_root.join(DataRoot::profile_relative(
            &fixture.profile.id,
            "audit.sqlite",
        )?);
        let connection = rusqlite::Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
        )?;
        connection.execute_batch(
            "CREATE TRIGGER provider_proxy_outcome_fault BEFORE INSERT ON audit
             WHEN instr(CAST(NEW.body AS TEXT), '\"kind\":\"denied\"') > 0
             BEGIN SELECT RAISE(FAIL, 'provider proxy denial sink failure'); END;",
        )?;
        Ok(Self {
            connection,
            active: true,
        })
    }

    fn clear(&mut self) -> TestResult {
        self.connection
            .execute_batch("DROP TRIGGER provider_proxy_outcome_fault;")?;
        self.active = false;
        Ok(())
    }
}

impl Drop for OutcomeAuditFault {
    fn drop(&mut self) {
        if self.active {
            let _ = self
                .connection
                .execute_batch("DROP TRIGGER IF EXISTS provider_proxy_outcome_fault;");
        }
    }
}

/// A real generic Core caller, returning the received closed error discriminant
/// as an ordinary scalar. Its public export cannot reinterpret a typed error.
fn error_projection_probe_component() -> TestResult<Vec<u8>> {
    Ok(wat::parse_str(
        r#"(component
            (type $core-types (instance
                (type $code-definition (enum "denied" "approval-required" "cancelled" "not-found" "invalid" "limit-exceeded" "conflict" "unavailable" "internal"))
                (export "error-code" (type $named-code (eq $code-definition)))
                (type $error-definition (record (field "code" $named-code) (field "message" string)))
                (export "error" (type (eq $error-definition)))))
            (import "tobkiri:core/types@1.0.0" (instance $types-import (type $core-types)))
            (alias export $types-import "error-code" (type $code))
            (alias export $types-import "error" (type $error))
            (type $call-result (result string (error $error)))
            (type $init-result (result (error $error)))
            (type $flow (instance
                (export "call-port" (func
                    (param "shape" string) (param "op" string) (param "args" string)
                    (result $call-result)))))
            (import "tobkiri:core/flow@1.0.0" (instance $flow-import (type $flow)))
            (alias export $flow-import "call-port" (func $call-port))
            (core module $storage
                (memory (export "memory") 1 1)
                (func (export "realloc") (param i32 i32 i32 i32) (result i32)
                    i32.const 4096))
            (core instance $storage-instance (instantiate $storage))
            (alias core export $storage-instance "memory" (core memory $memory))
            (alias core export $storage-instance "realloc" (core func $realloc))
            (core func $call-lowered
                (canon lower (func $call-port) (memory $memory) (realloc $realloc)))
            (core module $probe
                (import "host" "memory" (memory 1 1))
                (import "host" "call" (func $call (param i32 i32 i32 i32 i32 i32 i32)))
                (data (i32.const 128) "local.proxy-test.entry@1.0")
                (data (i32.const 192) "run")
                (data (i32.const 208) "{}")
                (func (export "init") (param i32 i32) (result i32)
                    i32.const 64 i32.const 0 i32.store i32.const 64)
                (func (export "shutdown"))
                (func (export "probe") (param i32) (result i32)
                    i32.const 128 i32.const 26
                    i32.const 192 i32.const 3
                    i32.const 208 i32.const 2
                    i32.const 0 call $call
                    i32.const 0 i32.load
                    if (result i32)
                        i32.const 4 i32.load
                    else
                        i32.const 99
                    end))
            (core instance $probe-instance (instantiate $probe
                (with "host" (instance
                    (export "memory" (memory $memory))
                    (export "call" (func $call-lowered))))))
            (alias core export $probe-instance "init" (core func $init))
            (alias core export $probe-instance "shutdown" (core func $shutdown))
            (alias core export $probe-instance "probe" (core func $probe-run))
            (func $init-value (param "config" string) (result $init-result)
                (canon lift (core func $init) (memory $memory) (realloc $realloc)))
            (func $shutdown-value (canon lift (core func $shutdown)))
            (func $probe-value (param "value" u32) (result u32)
                (canon lift (core func $probe-run)))
            (instance $types
                (export "error-code" (type $code))
                (export "error" (type $error)))
            (export "tobkiri:core/types@1.0.0" (instance $types))
            (instance $lifecycle
                (export "init" (func $init-value))
                (export "shutdown" (func $shutdown-value)))
            (export "tobkiri:core/lifecycle@1.0.0" (instance $lifecycle))
            (instance $arithmetic (export "increment" (func $probe-value)))
            (export "local:proxy-test/arithmetic@1.0.0" (instance $arithmetic)))"#,
    )?)
}

impl Fixture {
    /// A small verified provider whose identical ABI is declared by two exact
    /// minor views. Nominality is determined by each original WIT document,
    /// independently of structural compatibility and the returned field names.
    fn error_projection(caller_nominal: bool, provider_nominal: bool) -> TestResult<Self> {
        Self::error_projection_bound(caller_nominal, provider_nominal, None)
    }

    fn error_projection_bound(
        caller_nominal: bool,
        provider_nominal: bool,
        same_ref_provider_dependency: Option<bool>,
    ) -> TestResult<Self> {
        Self::error_projection_with_probe(
            caller_nominal,
            provider_nominal,
            same_ref_provider_dependency,
            None,
        )
    }

    fn error_projection_with_probe(
        caller_nominal: bool,
        provider_nominal: bool,
        same_ref_provider_dependency: Option<bool>,
        probe_dependency: Option<bool>,
    ) -> TestResult<Self> {
        fn document(version: &str, nominal: bool, caller: bool, probe: bool) -> String {
            // Both views retain the same named type dictionary. Selecting the
            // result alias changes only nominal provenance, so the compiler can
            // honestly admit the structurally compatible minor binding.
            let error_type = if nominal {
                "final-error"
            } else {
                "local-error"
            };
            format!(
                "package local:proxy-test@{version};
                 interface entry {{
                     use tobkiri:core/types@1.0.0.{{error, error-code}};
                     record local-error {{ code: error-code, message: string }}
                     type first-error = error;
                     type final-error = first-error;
                     run: func() -> result<_, {error_type}>;
                 }}\n{}",
                if caller && probe {
                    "interface arithmetic { increment: func(value: u32) -> u32; }"
                } else if caller {
                    "interface arithmetic { unused: func(); }"
                } else {
                    ""
                },
            )
        }

        let directory = tempfile::tempdir()?;
        let root = directory.path().canonicalize()?;
        let host = Arc::new(Host::default());
        let caller_wit =
            document("1.0.0", caller_nominal, true, probe_dependency.is_some()).into_bytes();
        let provider_version = if same_ref_provider_dependency.is_some() {
            "1.0.0"
        } else {
            "1.1.0"
        };
        let provider_wit = document(
            provider_version,
            same_ref_provider_dependency.unwrap_or(provider_nominal),
            false,
            false,
        )
        .into_bytes();
        let provided = ShapeRef::parse(if same_ref_provider_dependency.is_some() {
            "local.proxy-test.entry@1.0"
        } else {
            "local.proxy-test.entry@1.1"
        })?;
        let mut contracts = manifest("local.proxy-test", RuntimeSpec::Data, permissions(&[]));
        let mut definitions = vec![
            (shape("entry"), "wit/v1/views.wit"),
            (shape("arithmetic"), "wit/v1/views.wit"),
        ];
        if same_ref_provider_dependency.is_none() {
            definitions.push((provided.clone(), "wit/v2/views.wit"));
        }
        contracts.shapes = definitions
            .into_iter()
            .map(|(reference, wit)| ShapeDefinition {
                name: reference.name,
                version: reference.version,
                kind: ShapeKind::Port,
                wit: wit.into(),
            })
            .collect();
        let sidecar = canonical_bytes(&ShapeSidecar {
            cardinality: tobkiri_manifest::Cardinality::One,
            streams: BTreeMap::new(),
            callbacks: BTreeMap::new(),
            conformance: None,
        })?;
        let mut contract_files = BTreeMap::from([
            ("wit/v1/views.wit".into(), caller_wit),
            ("wit/entry.shape.json".into(), sidecar.clone()),
            ("wit/arithmetic.shape.json".into(), sidecar),
        ]);
        if same_ref_provider_dependency.is_none() {
            contract_files.insert("wit/v2/views.wit".into(), provider_wit.clone());
        }
        let contracts = write_pack(&root, contracts, contract_files)?;
        let component = wat::parse_str(format!(
            r#"(component
                (type $code (enum "denied" "approval-required" "cancelled" "not-found" "invalid" "limit-exceeded" "conflict" "unavailable" "internal"))
                (type $error (record (field "code" $code) (field "message" string)))
                (type $result (result (error $error)))
                (core module $m
                    (memory (export "memory") 1 1)
                    (data (i32.const 128) "{PRIVATE_ERROR_MESSAGE}")
                    (func (export "realloc") (param i32 i32 i32 i32) (result i32) i32.const 1024)
                    (func (export "init") (param i32 i32) (result i32)
                        i32.const 32 i32.const 0 i32.store i32.const 32)
                    (func (export "shutdown"))
                    (func (export "run") (result i32)
                        i32.const 0 i32.const 1 i32.store
                        i32.const 4 i32.const 4 i32.store
                        i32.const 8 i32.const 128 i32.store
                        i32.const 12 i32.const {message_len} i32.store
                        i32.const 0))
                (core instance $i (instantiate $m))
                (alias core export $i "memory" (core memory $memory))
                (alias core export $i "realloc" (core func $realloc))
                (alias core export $i "init" (core func $init))
                (alias core export $i "shutdown" (core func $shutdown))
                (alias core export $i "run" (core func $run))
                (func $init-value (param "config" string) (result $result)
                    (canon lift (core func $init) (memory $memory) (realloc $realloc)))
                (func $shutdown-value (canon lift (core func $shutdown)))
                (func $run-value (result $result)
                    (canon lift (core func $run) (memory $memory) (realloc $realloc)))
                (instance $types
                    (export "error-code" (type $code))
                    (export "error" (type $error)))
                (export "tobkiri:core/types@1.0.0" (instance $types))
                (instance $lifecycle
                    (export "init" (func $init-value))
                    (export "shutdown" (func $shutdown-value)))
                (export "tobkiri:core/lifecycle@1.0.0" (instance $lifecycle))
                (instance $entry (export "run" (func $run-value)))
                (export "local:proxy-test/entry@{provider_version}" (instance $entry)))"#,
            message_len = PRIVATE_ERROR_MESSAGE.len(),
        ))?;
        let mut provider = manifest(
            PROVIDER,
            RuntimeSpec::Wasm {
                component: "component.wasm".into(),
                toolchain: "wat-fixture".into(),
                config: None,
            },
            permissions(&[]),
        );
        provider.ports.provides = vec![PortDeclaration {
            shape: provided.name,
            version: provided.version,
            optional: false,
        }];
        let provider = write_pack(
            &root,
            provider,
            BTreeMap::from([
                ("component.wasm".into(), component),
                ("wit/provider.wit".into(), provider_wit),
            ]),
        )?;
        let mut packs = vec![contracts, provider];
        if let Some(nominal) = probe_dependency {
            let mut consumer = manifest(
                CONSUMER,
                RuntimeSpec::Wasm {
                    component: "component.wasm".into(),
                    toolchain: "wat-fixture".into(),
                    config: None,
                },
                permissions(&["entry"]),
            );
            consumer.ports.provides = vec![port("arithmetic")];
            consumer.ports.consumes = vec![port("entry")];
            packs.push(write_pack(
                &root,
                consumer,
                BTreeMap::from([
                    ("component.wasm".into(), error_projection_probe_component()?),
                    (
                        "wit/consumer.wit".into(),
                        document("1.0.0", nominal, true, true).into_bytes(),
                    ),
                ]),
            )?);
        }
        let mut user = permissions(&["arithmetic", "entry"]);
        user.audit_read = true;
        let profile = Profile {
            format: "tobkiri.profile/1".into(),
            id: ProfileId::parse("prof_error_projection")?,
            name: "Verified nominal error projection".into(),
            packs: packs.iter().map(|(pin, _)| pin.clone()).collect(),
            publishers: BTreeMap::new(),
            ceiling: user.clone(),
            user_permissions: Some(user),
            pack_limits: packs
                .iter()
                .map(|(_, pack)| (pack.manifest.id.clone(), pack.manifest.permissions.clone()))
                .collect(),
            bindings: BTreeMap::new(),
            overlay_order: Vec::new(),
            user_overlays: Vec::new(),
            startup: Startup {
                inputs: Vec::new(),
                ui: None,
            },
        };
        let verified: Vec<_> = packs.into_iter().map(|(_, pack)| pack).collect();
        *host.reviewed_packs.lock().unwrap() = verified
            .iter()
            .map(|pack| (pack.manifest.id.clone(), pack.hash.clone()))
            .collect();
        let data_root = root.join("data");
        let core = Core::open(
            CoreConfig::for_test_data_root(data_root.clone()),
            host.clone(),
        )?;
        let fixture = Self {
            _directory: directory,
            data_root,
            host,
            core,
            profile,
            verified,
        };
        fixture.review_user(1)?;
        Ok(fixture)
    }

    fn new(options: Options) -> TestResult<Self> {
        let directory = tempfile::tempdir()?;
        let root = directory.path().canonicalize()?;
        let host = Arc::new(Host::default());
        let mut packs = Vec::new();

        // The old fixture attached invented ShapeDef values to unrelated
        // runtime Packs. Here local.proxy-test actually owns its WIT bytes.
        let wit = format!(
            "{}\ninterface increment-flow {{ run: func(); }}\ninterface input-flow {{ run: func(); }}\n",
            include_str!("../../test-guests/wit/fixtures.wit").replace(
                "{stream-handle, callback-handle, error}",
                "{stream-handle, callback-handle, error, json}"
            )
        )
        .into_bytes();
        let mut contracts = manifest("local.proxy-test", RuntimeSpec::Data, permissions(&[]));
        contracts.shapes = [
            ("arithmetic", ShapeKind::Port),
            ("entry", ShapeKind::Port),
            ("increment-flow", ShapeKind::Flow),
            ("input-flow", ShapeKind::Flow),
        ]
        .into_iter()
        .map(|(name, kind)| {
            let reference = shape(name);
            ShapeDefinition {
                name: reference.name,
                version: reference.version,
                kind,
                wit: "wit/fixtures.wit".into(),
            }
        })
        .collect();
        let mut contract_files = BTreeMap::from([("wit/fixtures.wit".into(), wit.clone())]);
        for (name, stream) in [
            ("arithmetic", Some("count-stream")),
            ("entry", Some("run-flow")),
            ("increment-flow", None),
            ("input-flow", None),
        ] {
            let sidecar = ShapeSidecar {
                cardinality: tobkiri_manifest::Cardinality::One,
                streams: stream
                    .map(|operation| BTreeMap::from([(operation.into(), "json".into())]))
                    .unwrap_or_default(),
                callbacks: BTreeMap::new(),
                conformance: None,
            };
            contract_files.insert(format!("wit/{name}.shape.json"), canonical_bytes(&sidecar)?);
        }
        packs.push(write_pack(&root, contracts, contract_files)?);

        let mut provider_permissions = permissions(&["input-flow"]);
        provider_permissions.host_io =
            BTreeSet::from(["terminal-read-line".into(), "request-ui-embed".into()]);
        provider_permissions.wasm.output_bytes = options.provider_output_bytes;
        let mut provider = manifest(
            PROVIDER,
            RuntimeSpec::Wasm {
                component: "component.wasm".into(),
                toolchain: "rust-1.94.0/wasm32-wasip2".into(),
                config: None,
            },
            provider_permissions.clone(),
        );
        provider.ports.provides = vec![port("arithmetic")];
        if options.native_input {
            provider.inputs = vec![InputDeclaration {
                exact_input_view: shape("arithmetic"),
                operation: op("wait"),
            }];
        }
        let provider_bytes = if options.forged_core_import {
            // The import has a closed Core name with a forged signature. Its
            // manifest is still verified; Core startup must reject the ABI.
            provider.ports.provides.clear();
            wat::parse_str(
                r#"(component
                    (type $clock (instance
                        (export "now-ms" (func (param "forged" u32) (result u64)))))
                    (import "tobkiri:core/clock@1.0.0" (instance (type $clock))))"#,
            )?
        } else {
            artifact("tobkiri_test_provider.wasm")?
        };
        packs.push(write_pack(
            &root,
            provider,
            BTreeMap::from([
                ("component.wasm".into(), provider_bytes.clone()),
                ("wit/fixtures.wit".into(), wit.clone()),
            ]),
        )?);

        let consumer_ports = if options.consumer_allowed {
            vec!["arithmetic", "increment-flow"]
        } else {
            vec!["increment-flow"]
        };
        let consumer_permissions = permissions(&consumer_ports);
        let mut consumer = manifest(
            CONSUMER,
            RuntimeSpec::Wasm {
                component: "component.wasm".into(),
                toolchain: "rust-1.94.0/wasm32-wasip2".into(),
                config: None,
            },
            consumer_permissions,
        );
        consumer.ports.provides = vec![port("entry")];
        // The binary has this typed import even in the denied case. A typed
        // declaration must not turn it into effective port permission.
        consumer.ports.consumes = vec![port("arithmetic")];
        if options.consumer_allowed {
            // Dynamic Core Flow entry requires the exact view to be declared,
            // in addition to its independently approved effective permission.
            // Its implementation is a manifest Flow, so it has no required
            // physical port binding; the real Flow runner selects that body.
            let mut flow = port("increment-flow");
            flow.optional = true;
            consumer.ports.consumes.push(flow);
        }
        let increment = FlowDocument {
            format: "tobkiri.flow/1".into(),
            id: "fixture.increment".into(),
            implements: Some(shape("increment-flow")),
            trigger: Trigger {
                input: shape("entry"),
            },
            loop_limits: "profile-default".into(),
            steps: vec![Step {
                id: Some("increment".into()),
                role: None,
                overlay: OverlayAccess::Closed,
                kind: StepKind::Call(PortCall {
                    shape: shape("arithmetic"),
                    op: op("increment"),
                    args: args(&[("value", CanonValue::Str("$input.value".into()))]),
                }),
                out: Some("answer".into()),
                stream: None,
            }],
            on_error: ErrorPolicy::default(),
        };
        let native = FlowDocument {
            format: "tobkiri.flow/1".into(),
            id: "fixture.native-read".into(),
            implements: Some(shape("input-flow")),
            trigger: Trigger {
                input: shape("arithmetic"),
            },
            loop_limits: "profile-default".into(),
            steps: vec![Step {
                id: Some("physical-input-result".into()),
                role: None,
                overlay: OverlayAccess::Closed,
                kind: StepKind::Call(PortCall {
                    shape: shape("arithmetic"),
                    op: op("increment"),
                    args: args(&[("value", CanonValue::Int(41))]),
                }),
                out: Some("answer".into()),
                stream: None,
            }],
            on_error: ErrorPolicy::default(),
        };
        // Dependency WIT is hash-verified in every declaring runtime Pack.
        // Only the contracts Pack declares ShapeDefinitions/owns the views;
        // carrying these import types grants the consumer no port permission.
        let mut consumer_files = BTreeMap::from([
            (
                "component.wasm".into(),
                artifact("tobkiri_test_consumer.wasm")?,
            ),
            ("wit/fixtures.wit".into(), wit.clone()),
        ]);
        // A Flow owner must actually declare the call permission at compile
        // time. The denied typed-import fixture therefore has no such Flow.
        if options.consumer_allowed {
            consumer.flows.push("flows/increment.json".into());
            consumer_files.insert("flows/increment.json".into(), canonical_bytes(&increment)?);
        }
        if options.native_input {
            consumer.flows.push("flows/native.json".into());
            consumer_files.insert("flows/native.json".into(), canonical_bytes(&native)?);
        }
        packs.push(write_pack(&root, consumer, consumer_files)?);

        if options.substitute_input {
            let mut substitute = manifest(
                SUBSTITUTE,
                RuntimeSpec::Wasm {
                    component: "component.wasm".into(),
                    toolchain: "rust-1.94.0/wasm32-wasip2".into(),
                    config: None,
                },
                provider_permissions,
            );
            substitute.ports.provides = vec![port("arithmetic")];
            packs.push(write_pack(
                &root,
                substitute,
                BTreeMap::from([
                    ("component.wasm".into(), provider_bytes),
                    ("wit/fixtures.wit".into(), wit.clone()),
                ]),
            )?);
        }

        let mut ui_permissions = permissions(&["arithmetic", "entry"]);
        ui_permissions.wasm.wall_ms = options.ui_wall_ms;
        let mut ui = manifest(UI, RuntimeSpec::Data, ui_permissions);
        ui.ports.consumes = vec![port("arithmetic"), port("entry")];
        ui.ui = Some(UiDefinition {
            entry: "index.html".into(),
            ports_used: vec![shape("arithmetic"), shape("entry")],
        });
        packs.push(write_pack(
            &root,
            ui,
            BTreeMap::from([
                ("index.html".into(), b"<p>Proxy test UI</p>".to_vec()),
                ("wit/fixtures.wit".into(), wit),
            ]),
        )?);

        let mut ceiling = permissions(&["arithmetic", "entry", "increment-flow", "input-flow"]);
        ceiling.audit_read = true;
        ceiling.host_io = BTreeSet::from(["terminal-read-line".into(), "request-ui-embed".into()]);
        // Literal reviewed User authority: utility ports plus read-only audit,
        // with its own memory budget. No Pack declaration is borrowed.
        let mut user = permissions(&["arithmetic", "entry"]);
        user.audit_read = true;
        let mut profile = Profile {
            format: "tobkiri.profile/1".into(),
            id: ProfileId::parse("prof_proxy")?,
            name: "Verified provider integration".into(),
            packs: packs.iter().map(|(pin, _)| pin.clone()).collect(),
            publishers: BTreeMap::new(),
            ceiling,
            user_permissions: Some(user),
            pack_limits: packs
                .iter()
                .map(|(_, pack)| (pack.manifest.id.clone(), pack.manifest.permissions.clone()))
                .collect(),
            bindings: if options.substitute_input {
                BTreeMap::from([(
                    "local.proxy-test.arithmetic@1".into(),
                    pack_id(if options.bind_substitute_input {
                        SUBSTITUTE
                    } else {
                        PROVIDER
                    }),
                )])
            } else {
                BTreeMap::new()
            },
            overlay_order: Vec::new(),
            user_overlays: Vec::new(),
            startup: Startup {
                inputs: if options.native_input {
                    vec![InputEntry {
                        source_pack: pack_id(PROVIDER),
                        exact_input_view: shape("arithmetic"),
                        operation: op("wait"),
                    }]
                } else {
                    Vec::new()
                },
                ui: Some(pack_id(UI)),
            },
        };
        if options.omit_provider_limit {
            profile.pack_limits.remove(&pack_id(PROVIDER));
        }
        if options.forged_core_import {
            // No imported physical interface may mask the forged Core ABI
            // failure with a missing required port/profile conflict.
            let consumer = profile
                .packs
                .iter()
                .position(|pin| pin.id == pack_id(CONSUMER))
                .unwrap();
            profile.packs.remove(consumer);
            profile.pack_limits.remove(&pack_id(CONSUMER));
            let ui = profile
                .packs
                .iter()
                .position(|pin| pin.id == pack_id(UI))
                .unwrap();
            profile.packs.remove(ui);
            profile.pack_limits.remove(&pack_id(UI));
            profile.startup.ui = None;
            packs.retain(|(_, pack)| {
                pack.manifest.id != pack_id(CONSUMER) && pack.manifest.id != pack_id(UI)
            });
        }
        let verified: Vec<_> = packs.into_iter().map(|(_, pack)| pack).collect();
        *host.reviewed_packs.lock().unwrap() = verified
            .iter()
            .map(|pack| (pack.manifest.id.clone(), pack.hash.clone()))
            .collect();
        let data_root = root.join("data");
        let core = Core::open(
            CoreConfig::for_test_data_root(data_root.clone()),
            host.clone(),
        )?;
        let fixture = Self {
            _directory: directory,
            data_root,
            host,
            core,
            profile,
            verified,
        };
        fixture.review_user(1)?;
        Ok(fixture)
    }

    fn review_user(&self, epoch: u64) -> TestResult<()> {
        let user = self.profile.user_permissions.as_ref().unwrap();
        assert_eq!(
            user,
            &PermissionSet {
                audit_read: true,
                ..permissions(&["arithmetic", "entry"])
            }
        );
        assert!(user.is_subset_of(&self.profile.ceiling));
        let compiled = tobkiri_profile::compile(&self.profile, &self.verified)
            .map_err(|errors| format!("fixture profile conflicts: {errors:?}"))?;
        self.host.reviewed_users.lock().unwrap().insert((
            Digest::hash(&canonical_bytes(&self.profile)?),
            compiled.plan_digest,
            epoch,
        ));
        Ok(())
    }

    async fn start(&self) -> TestResult<Session> {
        fixture_stage(
            "fixture.start: Core::save_profile",
            self.core.save_profile(self.profile.clone(), None),
        )
        .await?;
        Ok(fixture_stage(
            "fixture.start: Core::start",
            self.core.start(&self.profile.id),
        )
        .await?)
    }

    async fn ui(&self, session: &Session) -> TestResult<UiAttachment> {
        session.bind_ui_generation(UI_GENERATION)?;
        let mut attachments = session.ui_attachments()?;
        assert_eq!(attachments.len(), 1);
        let attachment = attachments.remove(0);
        assert_eq!(attachment.pack, pack_id(UI));
        assert_eq!(attachment.generation, Some(UI_GENERATION));
        Ok(attachment)
    }

    async fn call(
        &self,
        session: &Session,
        interface: &str,
        operation: &str,
        value: &CanonValue,
    ) -> TestResult<CanonValue> {
        let buffer = fixture_stage(
            "fixture.call: Session::call_user_port",
            session.call_user_port(shape(interface), op(operation), value),
        )
        .await?;
        parse_output(&buffer)
    }

    async fn ui_call(
        &self,
        session: &Session,
        ui: &UiAttachment,
        interface: &str,
        operation: &str,
        value: &CanonValue,
    ) -> Result<CoreBuffer, CoreError> {
        fixture_stage(
            "fixture.ui_call: Session::ui_invoke_received",
            session.ui_invoke_received(
                &ui.session,
                &ui.host_session,
                UI_GENERATION,
                &ui.receiver,
                shape(interface),
                op(operation),
                value,
            ),
        )
        .await
    }

    async fn count_stream(
        &self,
        session: &Session,
        ui: &UiAttachment,
        count: i64,
    ) -> TestResult<u64> {
        self.count_stream_with_cancel(
            session,
            ui,
            count,
            tokio_util::sync::CancellationToken::new(),
        )
        .await
    }

    async fn count_stream_with_cancel(
        &self,
        session: &Session,
        ui: &UiAttachment,
        count: i64,
        cancel: tokio_util::sync::CancellationToken,
    ) -> TestResult<u64> {
        let buffer = fixture_stage(
            "fixture.count_stream: Session::ui_invoke_received_with_cancel",
            session.ui_invoke_received_with_cancel(
                &ui.session,
                &ui.host_session,
                UI_GENERATION,
                &ui.receiver,
                shape("arithmetic"),
                op("count-stream"),
                &args(&[("count", CanonValue::Int(count))]),
                cancel,
            ),
        )
        .await?;
        let value = parse_output(&buffer)?;
        assert_eq!(
            value.get("kind").and_then(CanonValue::as_str),
            Some("stream")
        );
        Ok(u64::try_from(
            value
                .get("handle")
                .and_then(CanonValue::as_i64)
                .ok_or("missing stream handle")?,
        )?)
    }

    async fn pull(
        &self,
        session: &Session,
        ui: &UiAttachment,
        handle: u64,
        max: u32,
    ) -> TestResult<CanonValue> {
        let buffer = fixture_stage(
            "fixture.pull: Session::ui_stream_pull_buffer",
            session.ui_stream_pull_buffer(&ui.session, handle, max),
        )
        .await?;
        parse_output(&buffer)
    }

    async fn audit_rows(&self, session: &Session) -> TestResult<Vec<AuditRow>> {
        self.audit_rows_for_plan(session.plan_digest()).await
    }

    async fn flush_outbox(&self) -> TestResult {
        let root = DataRoot::open(&self.data_root)?;
        let gate = GateEngine::new(
            Arc::new(Approvals::open(&root, &self.profile.id)?),
            Arc::new(AuditLog::open(
                &root,
                &self.profile.id,
                AuditQuota::default(),
            )?),
            self.host.clone(),
            MediatorChain::default(),
        );
        fixture_stage(
            "fixture.flush_outbox: Gate::flush_outbox",
            gate.flush_outbox(&self.profile.id),
        )
        .await?;
        Ok(())
    }

    fn reservations(&self) -> TestResult<Vec<tobkiri_gate::Reservation>> {
        let path = self.data_root.join(DataRoot::profile_relative(
            &self.profile.id,
            "approvals.sqlite",
        )?);
        let connection = rusqlite::Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )?;
        let mut statement = connection.prepare("SELECT body FROM reservations LIMIT 1001")?;
        let mut rows = statement.query([])?;
        let mut reservations = Vec::new();
        while let Some(row) = rows.next()? {
            assert!(
                reservations.len() < 1000,
                "fixture authority inventory exceeded"
            );
            let bytes: Vec<u8> = row.get(0)?;
            assert!(
                bytes.len() <= 64 * 1024,
                "fixture reservation body exceeded"
            );
            reservations.push(serde_json::from_slice(&bytes)?);
        }
        Ok(reservations)
    }

    async fn audit_rows_for_plan(&self, revision: &Digest) -> TestResult<Vec<AuditRow>> {
        let root = DataRoot::open(&self.data_root)?;
        let approvals = Arc::new(Approvals::open(&root, &self.profile.id)?);
        let audit = Arc::new(AuditLog::open(
            &root,
            &self.profile.id,
            AuditQuota::default(),
        )?);
        let gate = GateEngine::new(
            approvals.clone(),
            audit.clone(),
            self.host.clone(),
            MediatorChain::default(),
        );
        let (profile, document) = self.core.profile(&self.profile.id)?;
        let epoch = approvals.security_epoch(&profile.id)?;
        let compiled = tobkiri_profile::compile(&profile, &self.verified)
            .map_err(|errors| format!("audit reader profile conflicts: {errors:?}"))?;
        assert_eq!(
            &compiled.plan_digest, revision,
            "audit reader must bind the actual current compiled revision"
        );
        gate.register_operation_views(
            &profile.id,
            revision,
            compiled
                .views
                .iter()
                .map(|(reference, view)| {
                    (reference.clone(), view.functions.keys().cloned().collect())
                })
                .collect(),
        )?;
        gate.register_revision(&profile.id, revision, BTreeMap::new())?;
        gate.register_user_policy_for_revision(
            &profile.id,
            &document,
            revision,
            epoch,
            profile.user_permissions.clone().unwrap(),
            profile.ceiling.clone(),
        )?;
        let cx = CallCx::root(
            profile.id.clone(),
            Principal::User,
            RequestId::parse("proxy-audit-read")?,
            CoreCancellation::new(),
            Instant::now() + Duration::from_secs(5),
            profile.ceiling.wasm,
        )
        .with_security_epoch(epoch)
        .with_revision(revision.clone());
        let grant = fixture_stage(
            "audit-reader: Gate::authorize(AuditRead)",
            gate.authorize(
                &cx,
                EffectRequest::AuditRead,
                AuditLog::read_digest(0, 1000)?,
            ),
        )
        .await?;
        let id = grant.id().clone();
        let rows = fixture_sync_stage("audit-reader: AuditLog::read", || {
            audit.read(&cx, 0, 1000, grant)
        })?;
        fixture_stage(
            "audit-reader: Gate::record_outcome",
            gate.record_outcome(id, Outcome::Succeeded),
        )
        .await?;
        Ok(rows)
    }
}

fn has_pack_event(rows: &[AuditRow], kind: &str, pack: &str) -> bool {
    rows.iter().any(|row| {
        row.event.kind == kind
            && row
                .event
                .principal
                .pack()
                .is_some_and(|(id, _)| id.as_str() == pack)
    })
}

fn effect_rows<'a>(
    rows: &'a [AuditRow],
    effect: &EffectRequest,
    kind: &str,
) -> TestResult<Vec<&'a AuditRow>> {
    let digest = CanonValue::Str(effect.digest()?.to_string());
    Ok(rows
        .iter()
        .filter(|row| {
            row.event.kind == kind && row.event.payload.get("effect_digest") == Some(&digest)
        })
        .collect())
}

fn cancelled_outcomes(rows: &[AuditRow]) -> usize {
    rows.iter()
        .filter(|row| {
            row.event.kind == "outcome"
                && row
                    .event
                    .payload
                    .get("outcome")
                    .and_then(|outcome| outcome.get("status"))
                    .and_then(CanonValue::as_str)
                    == Some("cancelled")
        })
        .count()
}

fn assert_cancelled_outcomes(rows: &[AuditRow], expected: usize) {
    let terminals: Vec<_> = rows
        .iter()
        .filter(|row| row.event.kind == "outcome")
        .map(|row| &row.event)
        .collect();
    assert_eq!(
        cancelled_outcomes(rows),
        expected,
        "actual terminal events: {terminals:#?}"
    );
}

async fn assert_public_error_projection(
    caller_nominal: bool,
    provider_nominal: bool,
    expected_code: &str,
    expected_message: &str,
) -> TestResult {
    let fixture = Fixture::error_projection(caller_nominal, provider_nominal)?;
    // The User requests the exact 1.0 view, while the only actual provider
    // declares 1.1. Structural minor compatibility must not merge their WIT
    // type identities when Core chooses the public error representation.
    assert_public_error_fixture(fixture, expected_code, expected_message).await
}

async fn assert_public_error_fixture(
    fixture: Fixture,
    expected_code: &str,
    expected_message: &str,
) -> TestResult {
    // Startup must accept the verified, structurally compatible declaration.
    // Error projection is tested only after actual physical provider dispatch.
    let session = fixture.start().await?;
    let error = session
        .call_user_port(shape("entry"), op("run"), &args(&[]))
        .await
        .unwrap_err();
    assert_eq!(error_code(&error), expected_code);
    assert!(matches!(&error, CoreError::Step(error) if error.message == expected_message));
    assert!(!error.to_string().contains(PRIVATE_ERROR_MESSAGE));
    let rows = fixture.audit_rows(&session).await?;
    assert!(has_pack_event(&rows, "dispatch", PROVIDER));
    let audit_bytes = canonical_bytes(&rows)?;
    assert!(!std::str::from_utf8(&audit_bytes)?.contains(PRIVATE_ERROR_MESSAGE));
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn public_core_error_alias_chain_preserves_only_closed_code_and_fixed_message() -> TestResult
{
    assert_public_error_projection(true, true, "invalid", "provider reported invalid input").await
}

#[tokio::test]
async fn public_local_error_record_with_core_enum_does_not_gain_core_identity() -> TestResult {
    assert_public_error_projection(
        false,
        false,
        "provider-error",
        "provider returned a typed error",
    )
    .await
}

#[tokio::test]
async fn public_minor_binding_requires_both_caller_and_provider_nominal_error_identity()
-> TestResult {
    for (caller, provider) in [(true, false), (false, true)] {
        assert_public_error_projection(
            caller,
            provider,
            "provider-error",
            "provider returned a typed error",
        )
        .await?;
    }
    Ok(())
}

#[tokio::test]
async fn same_ref_provider_dependency_lookalike_loads_but_does_not_gain_catalog_core_identity()
-> TestResult {
    let fixture = Fixture::error_projection_bound(true, true, Some(false))?;
    let provider = fixture
        .verified
        .iter()
        .find(|pack| pack.manifest.id == pack_id(PROVIDER))
        .unwrap();
    assert!(provider.manifest.shapes.is_empty());
    assert!(provider.shapes.is_empty());
    assert_eq!(
        provider.manifest.ports.provides[0].shape,
        shape("entry").name
    );
    assert_eq!(
        provider.manifest.ports.provides[0].version,
        shape("entry").version
    );
    let dependency = std::str::from_utf8(&provider.files["wit/provider.wit"])?;
    assert!(dependency.contains("run: func() -> result<_, local-error>"));
    let catalog = fixture
        .verified
        .iter()
        .flat_map(|pack| &pack.shapes)
        .find(|definition| definition.reference == shape("entry"))
        .unwrap();
    assert!(tobkiri_manifest::core_error_result_operations(catalog)?.contains(&op("run")));
    assert_public_error_fixture(fixture, "provider-error", "provider returned a typed error").await
}

#[tokio::test]
async fn same_ref_matching_provider_dependency_preserves_nominal_core_error() -> TestResult {
    let fixture = Fixture::error_projection_bound(true, true, Some(true))?;
    assert_public_error_fixture(fixture, "invalid", "provider reported invalid input").await
}

#[tokio::test]
async fn generic_guest_caller_uses_its_own_same_ref_dependency_error_identity() -> TestResult {
    // Core's generic API returns its frozen error enum, whose internal variant
    // is 8 and invalid variant is 4. A nonnominal provider-error is encoded as
    // internal by that API; a genuine nominal invalid remains invalid. The
    // guest returns this observed scalar successfully through its own export.
    for (caller_dependency_nominal, expected_code) in [(false, 8), (true, 4)] {
        let fixture = Fixture::error_projection_with_probe(
            true,
            true,
            Some(true),
            Some(caller_dependency_nominal),
        )?;
        let consumer = fixture
            .verified
            .iter()
            .find(|pack| pack.manifest.id == pack_id(CONSUMER))
            .unwrap();
        assert!(consumer.manifest.shapes.is_empty());
        assert!(consumer.shapes.is_empty());
        assert_eq!(
            consumer.manifest.ports.consumes[0].shape,
            shape("entry").name
        );
        assert_eq!(
            consumer.manifest.ports.consumes[0].version,
            shape("entry").version
        );
        let dependency = std::str::from_utf8(&consumer.files["wit/consumer.wit"])?;
        assert!(dependency.contains(if caller_dependency_nominal {
            "run: func() -> result<_, final-error>"
        } else {
            "run: func() -> result<_, local-error>"
        }));
        let session = fixture.start().await?;
        let observed = fixture
            .call(
                &session,
                "arithmetic",
                "increment",
                &args(&[("value", CanonValue::Int(0))]),
            )
            .await?;
        assert_eq!(observed, CanonValue::Int(expected_code));
        let rows = fixture.audit_rows(&session).await?;
        assert!(has_pack_event(&rows, "dispatch", CONSUMER));
        assert!(has_pack_event(&rows, "dispatch", PROVIDER));
        let public_bytes = canonical_bytes(&observed)?;
        let audit_bytes = canonical_bytes(&rows)?;
        assert!(!std::str::from_utf8(&public_bytes)?.contains(PRIVATE_ERROR_MESSAGE));
        assert!(!std::str::from_utf8(&audit_bytes)?.contains(PRIVATE_ERROR_MESSAGE));
        session.close().await?;
    }
    Ok(())
}

/// Poll a public close future from a genuinely runtime-free native thread.
/// The future's actual guest cleanup must execute on the captured runtime.
fn wait_on_plain_thread<T>(future: impl Future<Output = T>) -> T {
    struct WakeThread(std::thread::Thread);
    impl Wake for WakeThread {
        fn wake(self: Arc<Self>) {
            self.0.unpark();
        }
        fn wake_by_ref(self: &Arc<Self>) {
            self.0.unpark();
        }
    }
    assert!(tokio::runtime::Handle::try_current().is_err());
    let waker = Waker::from(Arc::new(WakeThread(std::thread::current())));
    let mut context = Context::from_waker(&waker);
    let mut future = std::pin::pin!(future);
    loop {
        match future.as_mut().poll(&mut context) {
            Poll::Ready(value) => return value,
            Poll::Pending => std::thread::park(),
        }
    }
}

#[tokio::test]
async fn rust_consumer_calls_rust_provider_through_real_core_gate_and_audit() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    assert_eq!(
        fixture
            .call(
                &session,
                "entry",
                "run",
                &args(&[("value", CanonValue::Int(40))])
            )
            .await?,
        CanonValue::Int(82)
    );
    let rows = fixture.audit_rows(&session).await?;
    assert!(has_pack_event(&rows, "dispatch", CONSUMER));
    assert!(has_pack_event(&rows, "outcome", CONSUMER));
    assert!(has_pack_event(&rows, "dispatch", PROVIDER));
    assert!(has_pack_event(&rows, "outcome", PROVIDER));
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn declared_typed_import_still_cannot_call_without_effective_port_permission() -> TestResult {
    let fixture = Fixture::new(Options {
        consumer_allowed: false,
        ..Options::default()
    })?;
    let session = fixture.start().await?;
    assert!(
        session
            .call_user_port(
                shape("entry"),
                op("run"),
                &args(&[("value", CanonValue::Int(40))])
            )
            .await
            .is_err()
    );
    assert!(has_pack_event(
        &fixture.audit_rows(&session).await?,
        "denied",
        CONSUMER
    ));
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn input_principal_cannot_select_another_pack_with_the_same_provided_shape() -> TestResult {
    let fixture = Fixture::new(Options {
        substitute_input: true,
        bind_substitute_input: true,
        native_input: true,
        ..Options::default()
    })?;
    fixture_stage(
        "test.setup: Core::save_profile",
        fixture.core.save_profile(fixture.profile.clone(), None),
    )
    .await?;
    let before = fixture.host.prompts.load(Ordering::SeqCst);
    assert!(
        fixture.core.start(&fixture.profile.id).await.is_err(),
        "a substituted physical source binding started"
    );
    assert_eq!(fixture.host.prompts.load(Ordering::SeqCst), before);
    assert_eq!(fixture.host.reads.load(Ordering::SeqCst), 0);
    Ok(())
}

#[tokio::test]
async fn actual_native_input_receipt_reaches_its_manifest_flow() -> TestResult {
    let fixture = Fixture::new(Options {
        substitute_input: true,
        native_input: true,
        ..Options::default()
    })?;
    fixture
        .host
        .lines
        .lock()
        .unwrap()
        .push_back("physical source read".into());
    let session = fixture.start().await?;
    let before = fixture.audit_rows(&session).await?.len();
    assert_eq!(
        session.input_sources(),
        vec![InputSourceId::parse(PROVIDER)?]
    );
    // A second verified runtime provides the identical physical interface,
    // but is absent from the frozen Input record. Its public read request
    // cannot reach either provider or manufacture a substitute receipt.
    assert!(
        session
            .read_input(
                InputSourceId::parse(SUBSTITUTE)?,
                shape("arithmetic"),
                op("wait"),
            )
            .await
            .is_err()
    );
    assert_eq!(fixture.host.reads.load(Ordering::SeqCst), 0);
    let input = session
        .read_input(
            InputSourceId::parse(PROVIDER)?,
            shape("arithmetic"),
            op("wait"),
        )
        .await?;
    let mut run = session.submit_native_input(input).await?;
    assert_eq!(run.routes().len(), 1);
    let target = run.routes()[0].target.clone();
    assert_eq!(target.flow_key, "local.test-consumer/fixture.native-read");
    let mut completed = false;
    while let Some(tagged) = run.events.recv().await {
        assert_eq!(tagged.route_index, 0);
        assert_eq!(tagged.event.plan_digest, target.plan_digest);
        if let tobkiri_flow::RunEventKind::Completed { value, .. } = tagged.event.kind {
            assert_eq!(value.as_i64(), Some(42));
            completed = true;
        }
    }
    let completion = run.result.await?;
    assert!(completed);
    assert_eq!(completion.status, NativeInputStatus::Succeeded);
    assert_eq!(completion.routes.len(), 1);
    assert_eq!(
        completion.routes[0].status,
        NativeInputRouteStatus::Succeeded
    );
    let encoded = parse_output(&completion.buffer)?;
    assert_eq!(
        encoded.get("status").and_then(CanonValue::as_str),
        Some("succeeded")
    );
    assert_eq!(fixture.host.reads.load(Ordering::SeqCst), 1);
    let rows = fixture.audit_rows(&session).await?;
    assert!(has_pack_event(&rows[before..], "dispatch", PROVIDER));
    assert!(!has_pack_event(&rows[before..], "dispatch", SUBSTITUTE));
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn actual_float_guest_preserves_bits_and_nonfinite_output_is_rejected() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    for value in [-0.0_f64, f64::MAX, f64::MIN_POSITIVE] {
        let single = tobkiri_manifest::encode_f32(-0.0)?;
        let double = tobkiri_manifest::encode_f64(value)?;
        assert_eq!(
            fixture
                .call(
                    &session,
                    "arithmetic",
                    "floats",
                    &args(&[("single", single.clone()), ("double", double.clone())])
                )
                .await?,
            CanonValue::Arr(vec![single, double])
        );
    }
    assert!(
        session
            .call_user_port(shape("arithmetic"), op("nan"), &args(&[]))
            .await
            .is_err()
    );
    assert_eq!(
        fixture
            .call(
                &session,
                "arithmetic",
                "increment",
                &args(&[("value", CanonValue::Int(6))])
            )
            .await?,
        CanonValue::Int(7)
    );
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn guest_stream_opens_once_and_finalizes_once_without_per_pull_audit() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    let ui = fixture.ui(&session).await?;
    let handle = fixture.count_stream(&session, &ui, 3).await?;
    let before = fixture.audit_rows(&session).await?;
    assert_eq!(
        fixture.pull(&session, &ui, handle, 1).await?,
        CanonValue::Arr(vec![args(&[("index", CanonValue::Int(0))])])
    );
    assert_eq!(
        fixture
            .pull(&session, &ui, handle, 2)
            .await?
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(
        fixture.pull(&session, &ui, handle, 2).await?,
        CanonValue::Arr(Vec::new())
    );
    let after = fixture.audit_rows(&session).await?;
    // Each audit inspection adds dispatch/outcome. Pulling the guest stream
    // adds only the source-open terminal; it does not authorize per item.
    assert_eq!(after.len() - before.len(), 3);
    assert!(has_pack_event(&after[before.len()..], "outcome", PROVIDER));
    assert!(
        session
            .ui_stream_pull_buffer(&ui.session, handle, 1)
            .await
            .is_err()
    );
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn guest_ui_embed_is_unavailable_even_for_startup_slot_and_is_audited() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    assert_eq!(fixture.host.ui_embeds.load(Ordering::SeqCst), 1);
    for slot in ["startup", "guest-panel"] {
        let error = session
            .call_user_port(
                shape("arithmetic"),
                op("request-ui"),
                &args(&[("slot", CanonValue::Str(slot.into()))]),
            )
            .await
            .unwrap_err();
        assert_eq!(error_code(&error), "unavailable");
    }
    assert_eq!(fixture.host.ui_embeds.load(Ordering::SeqCst), 1);
    let rows = fixture.audit_rows(&session).await?;
    let effect = EffectRequest::HostIo {
        operation: "request-ui-embed".into(),
    };
    let dispatches = effect_rows(&rows, &effect, "dispatch")?;
    let outcomes = effect_rows(&rows, &effect, "outcome")?;
    assert_eq!(dispatches.len(), 2);
    assert_eq!(outcomes.len(), 2);
    let ids: BTreeSet<_> = dispatches
        .iter()
        .map(|row| {
            row.event
                .payload
                .get("grant")
                .and_then(CanonValue::as_str)
                .unwrap()
        })
        .collect();
    for row in outcomes {
        assert!(
            ids.contains(
                row.event
                    .payload
                    .get("grant")
                    .and_then(CanonValue::as_str)
                    .unwrap()
            )
        );
        assert_eq!(
            row.event.payload.get("outcome"),
            Some(&args(&[
                ("status", CanonValue::Str("failed".into())),
                ("code", CanonValue::Str("host-effect".into()))
            ]))
        );
    }
    // A real audit write fault must replace the guest's ordinary unavailable
    // result. It cannot invoke the native UI host or acknowledge success.
    let mut fault = OutcomeAuditFault::install(&fixture)?;
    let error = session
        .call_user_port(
            shape("arithmetic"),
            op("request-ui"),
            &args(&[("slot", CanonValue::Str("startup".into()))]),
        )
        .await
        .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("provider proxy outcome sink failure"),
        "the physical audit failure was hidden: {error}"
    );
    assert_eq!(fixture.host.ui_embeds.load(Ordering::SeqCst), 1);
    fault.clear()?;
    // Retry only the already-selected terminal, then inspect the same chain.
    fixture.flush_outbox().await?;
    let rows = fixture.audit_rows(&session).await?;
    assert_eq!(effect_rows(&rows, &effect, "dispatch")?.len(), 3);
    let outcomes = effect_rows(&rows, &effect, "outcome")?;
    assert_eq!(outcomes.len(), 3);
    for row in outcomes {
        assert_eq!(
            row.event.payload.get("outcome"),
            Some(&args(&[
                ("status", CanonValue::Str("failed".into())),
                ("code", CanonValue::Str("host-effect".into()))
            ]))
        );
    }
    assert_eq!(fixture.host.ui_embeds.load(Ordering::SeqCst), 1);
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn actual_component_close_and_cancelled_pull_surface_physical_audit_failure() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    let ui = fixture.ui(&session).await?;
    let first = fixture.count_stream(&session, &ui, 10).await?;
    let mut fault = OutcomeAuditFault::install(&fixture)?;
    let error = tokio::time::timeout(
        Duration::from_secs(3),
        session.ui_stream_cancel(&ui.session, first),
    )
    .await?
    .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("provider proxy outcome sink failure"),
        "actual component close hid its audit failure: {error}"
    );
    fault.clear()?;
    fixture.flush_outbox().await?;
    assert_cancelled_outcomes(&fixture.audit_rows(&session).await?, 1);

    let external = tokio_util::sync::CancellationToken::new();
    let second = fixture
        .count_stream_with_cancel(&session, &ui, 10, external.clone())
        .await?;
    let mut fault = OutcomeAuditFault::install(&fixture)?;
    external.cancel();
    let error = tokio::time::timeout(
        Duration::from_secs(3),
        session.ui_stream_pull_buffer(&ui.session, second, 1),
    )
    .await?
    .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("provider proxy outcome sink failure"),
        "cancelled actual component pull hid its audit failure: {error}"
    );
    fault.clear()?;
    fixture.flush_outbox().await?;
    assert_cancelled_outcomes(&fixture.audit_rows(&session).await?, 2);
    assert!(
        session
            .ui_stream_pull_buffer(&ui.session, second, 1)
            .await
            .is_err()
    );
    fixture_stage(
        "audit recovery: close after two cancelled streams",
        session.close(),
    )
    .await?;
    Ok(())
}

#[tokio::test]
async fn actual_component_cancel_winner_survives_physical_audit_retry_after_deadline() -> TestResult
{
    let fixture = Fixture::new(Options {
        ui_wall_ms: 500,
        ..Options::default()
    })?;
    let session = fixture.start().await?;
    let ui = fixture.ui(&session).await?;
    // Warm the actual instance before opening the intentionally short source.
    let warm = fixture.count_stream(&session, &ui, 1).await?;
    session.ui_stream_cancel(&ui.session, warm).await?;
    let before = fixture.audit_rows(&session).await?.len();
    let stream = fixture.count_stream(&session, &ui, 10).await?;
    let mut fault = OutcomeAuditFault::install(&fixture)?;
    let error = session
        .ui_stream_cancel(&ui.session, stream)
        .await
        .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("provider proxy outcome sink failure"),
        "actual component cancellation did not reach the physical sink: {error}"
    );
    tokio::time::sleep(Duration::from_millis(750)).await;
    fault.clear()?;
    fixture.flush_outbox().await?;
    fixture.flush_outbox().await?;
    let rows = fixture.audit_rows(&session).await?;
    let terminals: Vec<_> = rows[before..]
        .iter()
        .filter(|row| {
            row.event.kind == "outcome"
                && row
                    .event
                    .payload
                    .get("outcome")
                    .and_then(|outcome| outcome.get("status"))
                    .and_then(CanonValue::as_str)
                    == Some("cancelled")
        })
        .collect();
    assert_eq!(
        terminals.len(),
        1,
        "audit retry changed or repeated the winner: {rows:#?}"
    );
    assert_eq!(
        terminals[0]
            .event
            .payload
            .get("disposition")
            .and_then(CanonValue::as_str),
        Some("completed")
    );
    assert!(!rows[before..].iter().any(|row| {
        row.event
            .payload
            .get("outcome")
            .and_then(|outcome| outcome.get("code"))
            .and_then(CanonValue::as_str)
            == Some("deadline-failure-unknown")
    }));
    assert!(
        session
            .ui_stream_pull_buffer(&ui.session, stream, 1)
            .await
            .is_err()
    );
    fixture_stage(
        "audit recovery: close after original winner acknowledgement",
        session.close(),
    )
    .await?;
    Ok(())
}

#[tokio::test]
async fn expired_stream_and_callback_never_claim_successful_cancellation() -> TestResult {
    let fixture = Fixture::new(Options {
        // The actual UI source owns this narrower bound. Provider lifecycle
        // initialization retains its independently approved five-second cap.
        ui_wall_ms: 1000,
        ..Options::default()
    })?;
    let session = fixture.start().await?;
    let ui = fixture.ui(&session).await?;
    let before = fixture.audit_rows(&session).await?.len();
    let stream = fixture.count_stream(&session, &ui, 10).await?;
    let callback_buffer = fixture
        .ui_call(&session, &ui, "entry", "open-callback", &args(&[]))
        .await?;
    let callback = parse_output(&callback_buffer)?;
    assert_eq!(
        callback.get("kind").and_then(CanonValue::as_str),
        Some("value")
    );
    assert!(
        callback
            .get("value")
            .and_then(|value| value.get("id"))
            .and_then(CanonValue::as_i64)
            .is_some()
    );
    tokio::time::sleep(Duration::from_millis(1500)).await;
    let rows = fixture.audit_rows(&session).await?;
    let expired: Vec<_> = rows[before..]
        .iter()
        .filter(|row| {
            row.event.kind == "outcome"
                && row
                    .event
                    .payload
                    .get("outcome")
                    .and_then(|outcome| outcome.get("code"))
                    .and_then(CanonValue::as_str)
                    == Some("deadline-failure-unknown")
        })
        .collect();
    assert_eq!(
        expired.len(),
        2,
        "both auxiliary grants must expire honestly"
    );
    for row in expired {
        assert_eq!(
            row.event
                .payload
                .get("disposition")
                .and_then(CanonValue::as_str),
            Some("expired-cleanup-unknown")
        );
    }
    assert!(
        session
            .ui_stream_pull_buffer(&ui.session, stream, 1)
            .await
            .is_err()
    );
    session.close().await?;
    Ok(())
}

#[test]
fn cancel_after_owner_runtime_shutdown_returns_error_and_durable_expiry_recovers() -> TestResult {
    let runtime = tokio::runtime::Runtime::new()?;
    let (fixture, session, ui, stream) = runtime.block_on(async {
        let fixture = Fixture::new(Options::default())?;
        let session = fixture.start().await?;
        let ui = fixture.ui(&session).await?;
        let stream = fixture.count_stream(&session, &ui, 10).await?;
        Ok::<_, Box<dyn std::error::Error + Send + Sync>>((fixture, session, ui, stream))
    })?;
    drop(runtime);
    let recovery = tokio::runtime::Runtime::new()?;
    let error = recovery.block_on(async {
        tokio::time::timeout(
            Duration::from_secs(3),
            session.ui_stream_cancel(&ui.session, stream),
        )
        .await
        .expect("dead-runtime cleanup hung")
        .unwrap_err()
    });
    assert!(
        error.to_string().contains("owning runtime"),
        "runtime loss was reported as {error}"
    );
    let root = DataRoot::open(&fixture.data_root)?;
    let approvals = Arc::new(Approvals::open(&root, &fixture.profile.id)?);
    let audit = Arc::new(AuditLog::open(
        &root,
        &fixture.profile.id,
        AuditQuota::default(),
    )?);
    let gate = GateEngine::new(
        approvals,
        audit,
        fixture.host.clone(),
        MediatorChain::default(),
    );
    let now = u64::try_from(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_millis(),
    )?;
    gate.recover_profile_at(&fixture.profile.id, now + 60_000)?;
    recovery.block_on(gate.flush_outbox(&fixture.profile.id))?;
    let rows = recovery.block_on(fixture.audit_rows(&session))?;
    assert_eq!(cancelled_outcomes(&rows), 0);
    assert!(rows.iter().any(|row| {
        row.event
            .payload
            .get("disposition")
            .and_then(CanonValue::as_str)
            == Some("expired-cleanup-unknown")
    }));
    Ok(())
}

#[tokio::test]
async fn cancellation_from_plain_thread_uses_owning_runtime_and_records_outcome() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    let ui = fixture.ui(&session).await?;
    let stream = fixture.count_stream(&session, &ui, 10).await?;
    let native_session = session.clone();
    let token = ui.session.clone();
    let (finished, result) = tokio::sync::oneshot::channel();
    let native = std::thread::spawn(move || {
        let result = wait_on_plain_thread(native_session.ui_stream_cancel(&token, stream));
        let _ = finished.send(result);
    });
    tokio::time::timeout(Duration::from_secs(3), result).await???;
    native.join().expect("native cancellation thread panicked");
    assert!(
        session
            .ui_stream_pull_buffer(&ui.session, stream, 1)
            .await
            .is_err()
    );
    assert_cancelled_outcomes(&fixture.audit_rows(&session).await?, 1);
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn drop_from_plain_thread_completes_cleanup_on_owning_runtime() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    let ui = fixture.ui(&session).await?;
    let _stream = fixture.count_stream(&session, &ui, 10).await?;
    let revision = session.plan_digest().clone();
    // The last Session owns its registered UI streams. Its drop schedules the
    // same real guest source cleanup while keeping the captured runtime.
    std::thread::spawn(move || {
        assert!(tokio::runtime::Handle::try_current().is_err());
        drop(session);
    })
    .join()
    .expect("native drop thread panicked");
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            let rows = fixture.audit_rows_for_plan(&revision).await?;
            if cancelled_outcomes(&rows) == 1 {
                return Ok::<_, Box<dyn std::error::Error + Send + Sync>>(());
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await??;
    Ok(())
}

#[tokio::test]
async fn callback_reentry_returns_conflict_instead_of_waiting_for_ancestor_lock() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    let started = Instant::now();
    let error = session
        .call_user_port(
            shape("entry"),
            op("run-callback"),
            &args(&[("value", CanonValue::Int(7))]),
        )
        .await
        .unwrap_err();
    assert_eq!(
        error_code(&error),
        "conflict",
        "callback error: {error}; actual audit: {:#?}",
        fixture.audit_rows(&session).await?
    );
    assert!(started.elapsed() < Duration::from_secs(2));
    assert_eq!(
        fixture
            .call(
                &session,
                "entry",
                "run",
                &args(&[("value", CanonValue::Int(5))])
            )
            .await?,
        CanonValue::Int(12)
    );
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn guest_run_flow_uses_actual_core_bridge_and_manifest_flow_typed_port() -> TestResult {
    let measured = Instant::now();
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    let ui = fixture.ui(&session).await?;
    eprintln!(
        "FLOW_BRIDGE_PHASE test=core_run_flow stage=ui_ready elapsed_ms={}",
        measured.elapsed().as_millis()
    );
    let buffer = fixture
        .ui_call(
            &session,
            &ui,
            "entry",
            "run-flow",
            &args(&[
                (
                    "implements",
                    CanonValue::Str(shape("increment-flow").to_string()),
                ),
                ("input", CanonValue::Str("{\"value\":41}".into())),
            ]),
        )
        .await?;
    let opened = parse_output(&buffer)?;
    eprintln!(
        "FLOW_BRIDGE_PHASE test=core_run_flow stage=opened elapsed_ms={}",
        measured.elapsed().as_millis()
    );
    assert_eq!(
        opened.get("kind").and_then(CanonValue::as_str),
        Some("stream")
    );
    let handle = u64::try_from(
        opened
            .get("handle")
            .and_then(CanonValue::as_i64)
            .ok_or("missing Flow stream handle")?,
    )?;
    let mut completed = false;
    let mut observations = 0_u8;
    loop {
        if observations < 4 {
            observations += 1;
            eprintln!(
                "FLOW_BRIDGE_PHASE test=core_run_flow stage=pull completed={completed} elapsed_ms={}",
                measured.elapsed().as_millis()
            );
        }
        let batch = fixture.pull(&session, &ui, handle, 8).await?;
        let frames = batch.as_array().ok_or("Flow batch is not an array")?;
        if frames.is_empty() {
            break;
        }
        for event in frames {
            let kind = event.get("kind").ok_or("Flow frame kind missing")?;
            if kind.get("kind").and_then(CanonValue::as_str) == Some("completed") {
                assert_eq!(kind.get("value"), Some(&CanonValue::Int(42)));
                completed = true;
            }
        }
    }
    assert!(completed);
    let rows = fixture.audit_rows(&session).await?;
    assert!(has_pack_event(&rows, "dispatch", CONSUMER));
    assert!(has_pack_event(&rows, "dispatch", PROVIDER));
    eprintln!(
        "FLOW_BRIDGE_PHASE test=core_run_flow stage=session_close elapsed_ms={}",
        measured.elapsed().as_millis()
    );
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn actual_guest_trap_fuel_and_memory_errors_do_not_poison_following_call() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    for operation in ["trap", "spin", "memory-bomb"] {
        let started = Instant::now();
        assert!(
            session
                .call_user_port(shape("arithmetic"), op(operation), &args(&[]))
                .await
                .is_err(),
            "{operation} unexpectedly succeeded"
        );
        assert!(
            started.elapsed() < Duration::from_secs(3),
            "{operation} exceeded its execution bound"
        );
        assert_eq!(
            fixture
                .call(
                    &session,
                    "arithmetic",
                    "increment",
                    &args(&[("value", CanonValue::Int(8))])
                )
                .await?,
            CanonValue::Int(9)
        );
    }
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn missing_hash_bound_execution_budget_is_audited_before_instantiation() -> TestResult {
    let fixture = Fixture::new(Options {
        omit_provider_limit: true,
        ..Options::default()
    })?;
    fixture_stage(
        "test.setup: Core::save_profile",
        fixture.core.save_profile(fixture.profile.clone(), None),
    )
    .await?;
    // Startup compiles the verified catalog; component initialization is lazy.
    // A missing explicit Pack cap yields zero effective execution capacity and
    // must deny the actual entry before it can initialize or read host input.
    let session = fixture_stage(
        "test.setup: Core::start",
        fixture.core.start(&fixture.profile.id),
    )
    .await?;
    let before = fixture.audit_rows(&session).await?.len();
    let reservations_before = fixture.reservations()?.len();
    let error = session
        .call_user_port(shape("arithmetic"), op("wait"), &args(&[]))
        .await
        .unwrap_err();
    assert!(matches!(
        error,
        CoreError::Gate(tobkiri_gate::GateError::Unavailable)
    ));
    assert_eq!(fixture.host.reads.load(Ordering::SeqCst), 0);
    let reservations = fixture.reservations()?;
    assert_eq!(
        reservations.len(),
        reservations_before,
        "pre-entry rejection reserved authority"
    );
    let rows = fixture.audit_rows(&session).await?;
    let denials: Vec<_> = rows[before..]
        .iter()
        .filter(|row| row.event.kind == "denied")
        .collect();
    assert_eq!(denials.len(), 1, "actual audit: {rows:#?}");
    let denial = denials[0];
    assert!(matches!(denial.event.principal, Principal::User));
    assert_eq!(denial.event.profile, fixture.profile.id);
    assert!(denial.event.request.as_str().starts_with("req_"));
    assert_ne!(denial.event.request.as_str(), "proxy-audit-read");
    let root = DataRoot::open(&fixture.data_root)?;
    let approvals = Approvals::open(&root, &fixture.profile.id)?;
    let epoch = approvals.security_epoch(&fixture.profile.id)?;
    assert_eq!(
        denial.event.payload,
        CanonValue::Arr(vec![
            CanonValue::Str("port-call".into()),
            CanonValue::Str("unavailable".into()),
            CanonValue::Int(epoch.try_into()?),
        ])
    );
    assert!(denial.event_id.is_none());
    assert_eq!(
        rows.iter()
            .filter(|row| row.event.request == denial.event.request)
            .count(),
        1
    );
    assert!(
        !reservations
            .iter()
            .any(|reservation| reservation.request == denial.event.request)
    );
    assert!(!has_pack_event(&rows, "dispatch", PROVIDER));
    assert!(!has_pack_event(&rows, "denied", PROVIDER));
    assert!(
        !rows[before..]
            .iter()
            .any(|row| row.event.kind.starts_with("run-") || row.event.kind.starts_with("step-"))
    );
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn missing_provider_capacity_preserves_original_rejection_when_physical_denial_audit_fails()
-> TestResult {
    let fixture = Fixture::new(Options {
        omit_provider_limit: true,
        ..Options::default()
    })?;
    let session = fixture.start().await?;
    let before = fixture.audit_rows(&session).await?.len();
    let reservations_before = fixture.reservations()?.len();
    let mut fault = OutcomeAuditFault::install_denial(&fixture)?;
    let error = session
        .call_user_port(shape("arithmetic"), op("wait"), &args(&[]))
        .await
        .unwrap_err();
    match error {
        CoreError::Gate(tobkiri_gate::GateError::AuditFailure { denial, audit }) => {
            assert!(matches!(*denial, tobkiri_gate::GateError::Unavailable));
            assert!(
                matches!(*audit, tobkiri_gate::GateError::Persistence(ref message)
                if message.contains("provider proxy denial sink failure"))
            );
        }
        other => panic!("pre-entry failure lost its denial or sink error: {other}"),
    }
    assert_eq!(fixture.host.reads.load(Ordering::SeqCst), 0);
    assert_eq!(fixture.reservations()?.len(), reservations_before);
    fault.clear()?;
    fixture.flush_outbox().await?;
    let rows = fixture.audit_rows(&session).await?;
    assert!(!rows[before..].iter().any(|row| row.event.kind == "denied"));
    assert!(!has_pack_event(&rows, "dispatch", PROVIDER));
    assert!(
        !rows[before..]
            .iter()
            .any(|row| row.event.kind.starts_with("run-") || row.event.kind.starts_with("step-"))
    );
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn core_interface_name_with_forged_function_signature_is_rejected_before_init() -> TestResult
{
    let fixture = Fixture::new(Options {
        forged_core_import: true,
        ..Options::default()
    })?;
    fixture_stage(
        "test.setup: Core::save_profile",
        fixture.core.save_profile(fixture.profile.clone(), None),
    )
    .await?;
    let prompts = fixture.host.prompts.load(Ordering::SeqCst);
    assert!(fixture.core.start(&fixture.profile.id).await.is_err());
    assert_eq!(fixture.host.prompts.load(Ordering::SeqCst), prompts);
    assert_eq!(fixture.host.reads.load(Ordering::SeqCst), 0);
    Ok(())
}

#[tokio::test]
async fn epoch_revocation_closes_live_stream_and_next_fresh_session_recovers() -> TestResult {
    let mut fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    let ui = fixture.ui(&session).await?;
    let handle = fixture.count_stream(&session, &ui, 3).await?;
    assert_eq!(
        fixture
            .pull(&session, &ui, handle, 1)
            .await?
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let (_, head) = fixture.core.profile(&fixture.profile.id)?;
    fixture.profile.name = "Revised verified provider integration".into();
    fixture.review_user(2)?;
    fixture_stage(
        "test.setup: Core::save_profile",
        fixture
            .core
            .save_profile(fixture.profile.clone(), Some(head)),
    )
    .await?;
    assert!(
        session
            .ui_stream_pull_buffer(&ui.session, handle, 1)
            .await
            .is_err(),
        "revoked stream kept executing"
    );
    let fresh = fixture_stage(
        "test.setup: Core::start",
        fixture.core.start(&fixture.profile.id),
    )
    .await?;
    assert_eq!(
        fixture
            .call(
                &fresh,
                "arithmetic",
                "increment",
                &args(&[("value", CanonValue::Int(10))])
            )
            .await?,
        CanonValue::Int(11)
    );
    assert_cancelled_outcomes(&fixture.audit_rows(&fresh).await?, 1);
    session.close().await?;
    fresh.close().await?;
    Ok(())
}

#[tokio::test]
async fn cancelling_stream_retires_locked_instance_and_drops_pending_host_future() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    let ui = fixture.ui(&session).await?;
    let handle = fixture.count_stream(&session, &ui, 3).await?;
    fixture.host.block_input.store(true, Ordering::SeqCst);
    let pending_session = session.clone();
    let pending = tokio::spawn(async move {
        pending_session
            .call_user_port(shape("arithmetic"), op("wait"), &args(&[]))
            .await
    });
    tokio::time::timeout(Duration::from_secs(2), fixture.host.read_started.notified()).await?;
    session.ui_stream_cancel(&ui.session, handle).await?;
    let result = tokio::time::timeout(Duration::from_millis(2400), pending).await??;
    assert!(result.is_err());
    assert_eq!(fixture.host.read_dropped.load(Ordering::SeqCst), 1);
    assert_eq!(
        fixture
            .call(
                &session,
                "arithmetic",
                "increment",
                &args(&[("value", CanonValue::Int(11))])
            )
            .await?,
        CanonValue::Int(12)
    );
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn session_close_cancels_guest_waiting_in_imported_host_effect() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    let reads_before = fixture.host.reads.load(Ordering::SeqCst);
    fixture.host.block_input.store(true, Ordering::SeqCst);
    let pending_session = session.clone();
    let pending = tokio::spawn(async move {
        pending_session
            .call_user_port(shape("arithmetic"), op("wait"), &args(&[]))
            .await
    });

    // This notification is emitted inside the real terminal-read HostIo import,
    // immediately before its future remains pending. Closing before it fires
    // would only test admission refusal, not cancellation of entered guest work.
    tokio::time::timeout(Duration::from_secs(2), fixture.host.read_started.notified()).await?;
    assert_eq!(fixture.host.reads.load(Ordering::SeqCst), reads_before + 1);
    assert!(
        !pending.is_finished(),
        "guest call finished before Session::close"
    );

    let close = tokio::time::timeout(Duration::from_secs(3), session.close()).await;
    let pending_result = tokio::time::timeout(Duration::from_secs(3), pending).await;
    close??;
    let call_result = pending_result??;
    let call_error = call_result.expect_err("cancelled guest call unexpectedly succeeded");
    let observed_code = error_code(&call_error);
    assert!(
        matches!(&call_error, CoreError::Step(error) if error.message.contains("context=cancelled")),
        "Session::close must return a Core error with cancellation context, not a fuel or pre-entry error; observed code={observed_code}, error={call_error}"
    );
    assert_eq!(fixture.host.read_dropped.load(Ordering::SeqCst), 1);

    let rows = fixture.audit_rows(&session).await?;
    let port_call = EffectRequest::PortCall {
        shape: shape("arithmetic"),
        op: op("wait"),
    };
    let host_read = EffectRequest::HostIo {
        operation: "terminal-read-line".into(),
    };
    for effect in [&port_call, &host_read] {
        let dispatches = effect_rows(&rows, effect, "dispatch")?;
        let outcomes = effect_rows(&rows, effect, "outcome")?;
        assert_eq!(
            dispatches.len(),
            1,
            "expected one actual dispatch for {effect:?}"
        );
        assert_eq!(outcomes.len(), 1, "expected one terminal for {effect:?}");
        assert_eq!(
            outcomes[0].event.payload.get("outcome"),
            Some(&args(&[("status", CanonValue::Str("cancelled".into()))])),
            "Session::close must select the cancelled terminal for {effect:?}"
        );
    }
    assert!(matches!(
        &effect_rows(&rows, &port_call, "dispatch")?[0]
            .event
            .principal,
        Principal::User
    ));
    assert!(matches!(
        &effect_rows(&rows, &host_read, "dispatch")?[0].event.principal,
        Principal::Pack { pack, .. } if pack.as_str() == PROVIDER
    ));
    Ok(())
}

#[tokio::test]
async fn session_close_surfaces_host_io_terminal_audit_failure() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    let reads_before = fixture.host.reads.load(Ordering::SeqCst);
    fixture.host.block_input.store(true, Ordering::SeqCst);
    let pending_session = session.clone();
    let pending = tokio::spawn(async move {
        pending_session
            .call_user_port(shape("arithmetic"), op("wait"), &args(&[]))
            .await
    });

    // Observe the actual imported HostIo read before faulting terminal writes;
    // this excludes admission refusal and pre-entry cancellation.
    tokio::time::timeout(Duration::from_secs(2), fixture.host.read_started.notified()).await?;
    assert_eq!(fixture.host.reads.load(Ordering::SeqCst), reads_before + 1);
    assert!(
        !pending.is_finished(),
        "guest call finished before Session::close"
    );
    let host_read = EffectRequest::HostIo {
        operation: "terminal-read-line".into(),
    };
    let before = fixture.audit_rows(&session).await?;
    let dispatches = effect_rows(&before, &host_read, "dispatch")?;
    assert_eq!(dispatches.len(), 1, "expected one actual HostIo grant");
    let original_grant = dispatches[0]
        .event
        .payload
        .get("grant")
        .and_then(CanonValue::as_str)
        .expect("HostIo dispatch must carry its original grant ID")
        .to_owned();
    let original_grant_id: tobkiri_gate::GrantId =
        serde_json::from_value(serde_json::Value::String(original_grant.clone()))?;

    // Reuse the real SQLite terminal-INSERT fault. Dispatch and guest entry
    // have already happened; the fault targets terminal delivery only.
    let mut fault = OutcomeAuditFault::install(&fixture)?;
    let close_result = tokio::time::timeout(Duration::from_secs(3), session.close()).await;
    let pending_result = tokio::time::timeout(Duration::from_secs(3), pending).await;
    fault.clear()?;

    let close_error = close_result?.err().map(|error| error.to_string());
    let call_result = pending_result??;
    let call_error = call_result.expect_err(
        "Session::close plus failed HostIo terminal persistence must not report caller success",
    );
    let call_message = call_error.to_string();
    assert_eq!(fixture.host.read_dropped.load(Ordering::SeqCst), 1);

    // Gate records the immutable terminal winner before attempting the
    // physical audit insert. Before retrying delivery, this exact original
    // grant must remain unacknowledged and its cancellation must not vanish.
    let root = DataRoot::open(&fixture.data_root)?;
    let approvals = Approvals::open(&root, &fixture.profile.id)?;
    let failed_delivery = approvals
        .terminal_delivery(&original_grant_id)?
        .expect("the consumed HostIo grant must retain its failed terminal receipt");
    assert_eq!(
        failed_delivery.decision.reservation.id.as_str(),
        original_grant
    );
    assert_eq!(failed_delivery.decision.outcome, Outcome::Cancelled);
    assert!(
        !failed_delivery.acknowledged,
        "a rejected physical outcome INSERT must not be acknowledged"
    );

    // Cleanup must expose either the real SQLite failure or its conservative
    // completion-unknown result; cancellation alone is not a cleanup proof.
    assert!(
        call_message.contains("provider proxy outcome sink failure")
            || call_message.contains("unknown")
            || close_error.as_deref().is_some_and(|message| {
                message.contains("provider proxy outcome sink failure")
                    || message.contains("unknown")
            }),
        "HostIo terminal persistence failure was hidden; caller={call_message}, close={close_error:?}"
    );

    // Removing the fault allows only the already-selected receipt to be
    // delivered. Verify the persisted outcome still names the original grant.
    fixture.flush_outbox().await?;
    let rows = fixture.audit_rows(&session).await?;
    let dispatches = effect_rows(&rows, &host_read, "dispatch")?;
    let outcomes = effect_rows(&rows, &host_read, "outcome")?;
    assert_eq!(dispatches.len(), 1);
    assert_eq!(outcomes.len(), 1);
    assert_eq!(
        outcomes[0].event.payload.get("grant"),
        Some(&CanonValue::Str(original_grant.clone()))
    );
    assert_eq!(
        outcomes[0].event.payload.get("outcome"),
        Some(&args(&[("status", CanonValue::Str("cancelled".into()))]))
    );
    let delivered = approvals
        .terminal_delivery(&original_grant_id)?
        .expect("retried HostIo terminal receipt must remain queryable");
    assert!(delivered.acknowledged);
    assert_eq!(delivered.decision.outcome, Outcome::Cancelled);
    tokio::time::timeout(Duration::from_secs(3), session.close()).await??;
    Ok(())
}

#[tokio::test]
async fn stream_preserves_its_source_output_budget_across_multiple_pulls() -> TestResult {
    let fixture = Fixture::new(Options {
        provider_output_bytes: 32,
        ..Options::default()
    })?;
    let session = fixture.start().await?;
    let ui = fixture.ui(&session).await?;
    let handle = fixture.count_stream(&session, &ui, 4).await?;
    assert_eq!(
        fixture
            .pull(&session, &ui, handle, 2)
            .await?
            .as_array()
            .unwrap()
            .len(),
        2
    );
    let error = session
        .ui_stream_pull_buffer(&ui.session, handle, 1)
        .await
        .unwrap_err();
    assert_eq!(error_code(&error), "limit-exceeded");
    // The producing source's cumulative budget has been spent. The received
    // UI handle must retire with that source instead of allowing more output.
    assert!(matches!(
        session
            .ui_stream_pull_buffer(&ui.session, handle, 1)
            .await,
        Err(CoreError::Invalid(message)) if message == "unknown UI stream handle"
    ));
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn json_ui_cannot_mint_a_guest_local_callback_pointer() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    let ui = fixture.ui(&session).await?;
    assert!(
        fixture
            .ui_call(
                &session,
                &ui,
                "arithmetic",
                "with-callback",
                &args(&[
                    ("callback", args(&[("id", CanonValue::Int(1))])),
                    ("value", CanonValue::Int(7))
                ])
            )
            .await
            .is_err()
    );
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn old_handle_cannot_read_or_cancel_a_recreated_instance_local_id() -> TestResult {
    let fixture = Fixture::new(Options::default())?;
    let session = fixture.start().await?;
    let ui = fixture.ui(&session).await?;
    let old = fixture.count_stream(&session, &ui, 3).await?;
    assert!(
        session
            .call_user_port(shape("arithmetic"), op("trap"), &args(&[]))
            .await
            .is_err()
    );
    let fresh = fixture.count_stream(&session, &ui, 3).await?;
    assert_ne!(old, fresh);
    assert!(
        session
            .ui_stream_pull_buffer(&ui.session, old, 1)
            .await
            .is_err()
    );
    let _ = session.ui_stream_cancel(&ui.session, old).await;
    assert_eq!(
        fixture.pull(&session, &ui, fresh, 1).await?,
        CanonValue::Arr(vec![args(&[("index", CanonValue::Int(0))])])
    );
    assert_eq!(
        fixture
            .pull(&session, &ui, fresh, 2)
            .await?
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(
        fixture.pull(&session, &ui, fresh, 1).await?,
        CanonValue::Arr(Vec::new())
    );
    session.close().await?;
    Ok(())
}
