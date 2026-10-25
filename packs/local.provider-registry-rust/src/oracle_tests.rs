//! Opt-in native consumer of the captured original Python Provider Registry.
//!
//! This test verifies the source receipt, its complete source closure, every
//! input/observation sidecar, and then replays the decoded state-machine calls
//! that the current pure Rust API can execute. Filesystem locking/permissions,
//! raw JSON parser behavior, and opaque captured Host objects remain explicit
//! unverified scope. A green run is not complete Registry, Core, or Wasm parity.
//!
//! Required environment:
//! - `TOBKIRI_PROVIDER_REGISTRY_ORACLE_RECEIPT`
//! - `TOBKIRI_PROVIDER_REGISTRY_ORACLE_SHA256`
//! - `TOBKIRI_PROVIDER_REGISTRY_EVIDENCE_DIR` (must be a new private path)
//!
//! Run only this ignored test after setting the three variables:
//! `cargo test --manifest-path packs/local.provider-registry-rust/Cargo.toml --lib oracle_tests::provider_registry_decoded_source_consumer -- --ignored --exact`

use crate::{
    failure::{SourceError, SourceResult},
    host::{self, FactoryMode},
    record,
    registry::Registry,
    state::{Clock, MemoryStore},
};
use anyhow::{Context, Result, bail, ensure};
use ordered_python_compat::{PyValue, dumps, loads, pystr, pytruthy};
use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};
use std::{
    cell::Cell,
    collections::{BTreeMap, BTreeSet},
    env,
    fs::{self, File, OpenOptions},
    io::Write as _,
    path::{Component, Path, PathBuf},
    rc::Rc,
};

const RECEIPT_ENV: &str = "TOBKIRI_PROVIDER_REGISTRY_ORACLE_RECEIPT";
const RECEIPT_HASH_ENV: &str = "TOBKIRI_PROVIDER_REGISTRY_ORACLE_SHA256";
const EVIDENCE_ENV: &str = "TOBKIRI_PROVIDER_REGISTRY_EVIDENCE_DIR";
const RECEIPT_SCHEMA: &str = "io.tobkiri.original-python-provider-registry-stateful.v1";
const PINNED_RECEIPT_SHA256: &str =
    "e87762d4647d05ad8800c337c2352a272c767d252cb7ad4c4906760fc5590b37";
const PINNED_SOURCE_MANIFEST_SHA256: &str =
    "edb7d654c898dcd5e0648ec3120fed78f034a684945266963e4ebb1afab1b35a";
const PINNED_GENERATOR_SHA256: &str =
    "6cd9a6e796468794a1e46bd1cc891cf4f550a9fe9469b21a3633b47f3269458a";
const PINNED_CASE_SOURCE_SHA256: &str =
    "d9b5d432a262354facbaf1e67b74265c29c0f5fec00c83f7af3bb9eeeeb9eba8";
const GENERATOR_PATH: &str = "tools/quality/python_provider_registry_compat/generate.py";
const CASE_SOURCE_PATH: &str = "tools/quality/python_provider_registry_compat/cases.py";
const MAX_RECEIPT_BYTES: usize = 8 * 1024 * 1024;
const MAX_CASE_BYTES: usize = 2 * 1024 * 1024;
const MAX_SOURCE_BYTES: usize = 8 * 1024 * 1024;
const EXPECTED_SOURCE_FILES: usize = 346;
const EXPECTED_CASES: usize = 30;
const EXPECTED_ACTIONS: usize = 85;
const EXPECTED_INPUT_ACTIONS: usize = 86;
const EXPECTED_NATIVE_CALLS: usize = 80;
const EXPECTED_MATCHED_OUTCOMES: usize = 81;
const EXPECTED_UNVERIFIED_ACTIONS: usize = 5;
const EXPECTED_NOT_RUN_ACTIONS: usize = 1;
const EXPECTED_HOST_INVOKE_HELPER_CALLS: usize = 9;

type NativeOutcome = std::result::Result<PyValue, SourceError>;

struct Evidence {
    directory: PathBuf,
    journal: File,
}

impl Evidence {
    fn create() -> Result<Self> {
        let requested = required_env_path(EVIDENCE_ENV)?;
        let parent = requested
            .parent()
            .context("evidence directory must have an existing parent")?
            .canonicalize()
            .context("canonicalize evidence parent")?;
        let name = requested
            .file_name()
            .context("evidence directory name is missing")?;
        let directory = parent.join(name);
        ensure!(
            fs::symlink_metadata(&directory).is_err(),
            "evidence directory already exists; use a new path"
        );
        fs::create_dir(&directory).context("create new evidence directory")?;
        set_mode(&directory, 0o700)?;
        let journal_path = directory.join("actions.jsonl");
        let journal = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&journal_path)
            .context("create private action journal")?;
        set_mode(&journal_path, 0o600)?;
        Ok(Self { directory, journal })
    }

    fn append(&mut self, row: &Value) -> Result<()> {
        serde_json::to_writer(&mut self.journal, row).context("write action journal row")?;
        self.journal
            .write_all(b"\n")
            .context("terminate journal row")?;
        self.journal.flush().context("flush action journal")?;
        self.journal.sync_data().context("sync action journal")?;
        Ok(())
    }

    fn write_json(&self, name: &str, value: &Value) -> Result<()> {
        let path = self.directory.join(name);
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .with_context(|| format!("create evidence file {name}"))?;
        set_mode(&path, 0o600)?;
        serde_json::to_writer_pretty(&mut file, value)
            .with_context(|| format!("write evidence file {name}"))?;
        file.write_all(b"\n")?;
        file.flush()?;
        file.sync_all()?;
        Ok(())
    }

    fn failure(&self, message: &str) -> Result<()> {
        self.write_json(
            "failure.json",
            &json!({
                "schema":"io.tobkiri.provider-registry-native-consumer-failure.v1",
                "error":message,
                "actions_journal":"actions.jsonl",
            }),
        )
    }
}

#[cfg(unix)]
fn set_mode(path: &Path, mode: u32) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
        .with_context(|| format!("set private permissions on {}", path.display()))
}

#[cfg(not(unix))]
fn set_mode(_path: &Path, _mode: u32) -> Result<()> {
    Ok(())
}

struct PreparedCase {
    id: String,
    input: PyValue,
    observation: PyValue,
    sidecars: BTreeMap<String, Vec<u8>>,
}

#[derive(Default)]
struct Counts {
    input_actions: usize,
    source_actions: usize,
    native_calls: usize,
    matched_outcomes: usize,
    mismatches: usize,
    unverified_actions: usize,
    not_run_actions: usize,
    host_invoke_boundary_only: usize,
    capture_predicate_only: usize,
    raw_store_parser_actions: usize,
}

#[derive(Clone, Copy, Default)]
struct TimestampProvenance {
    created_at_generated: bool,
    updated_at_generated: bool,
}

#[derive(Clone)]
struct OracleClock(Rc<Cell<usize>>);

impl Clock for OracleClock {
    fn now(&mut self) -> SourceResult<String> {
        let next = self.0.get() + 1;
        self.0.set(next);
        Ok(clock_value(next))
    }
}

fn clock_value(index: usize) -> String {
    format!("2099-01-01T00:00:{:02}Z", index % 60)
}

#[test]
#[ignore = "requires the pinned Python source receipt and a new private evidence directory"]
fn provider_registry_decoded_source_consumer() {
    if let Err(error) = run_consumer() {
        panic!("provider registry source consumer failed: {error:#}");
    }
}

fn run_consumer() -> Result<()> {
    let mut evidence = Evidence::create()?;
    let result = run_inner(&mut evidence);
    match result {
        Ok(()) => Ok(()),
        Err(error) => {
            let message = format!("{error:#}");
            evidence
                .failure(&message)
                .context("preserve consumer failure")?;
            Err(error)
        }
    }
}

fn run_inner(evidence: &mut Evidence) -> Result<()> {
    let receipt_path = required_env_path(RECEIPT_ENV)?;
    let requested_hash = env::var(RECEIPT_HASH_ENV)
        .with_context(|| format!("required environment variable {RECEIPT_HASH_ENV}"))?;
    ensure!(
        requested_hash == PINNED_RECEIPT_SHA256,
        "oracle hash environment value does not match the pinned receipt"
    );
    let receipt_path = canonical_regular_path(&receipt_path)?;
    ensure!(
        receipt_path.file_name().and_then(|name| name.to_str()) == Some("receipt.json"),
        "oracle input must be the captured receipt.json"
    );
    let receipt_root = receipt_path
        .parent()
        .context("receipt root is missing")?
        .canonicalize()
        .context("canonicalize receipt root")?;
    let receipt_bytes = read_regular(&receipt_path, MAX_RECEIPT_BYTES)?;
    let receipt_hash = digest(&receipt_bytes);
    ensure!(
        receipt_hash == PINNED_RECEIPT_SHA256,
        "oracle receipt SHA-256 differs from the pinned original observation"
    );
    let receipt: Value = serde_json::from_slice(&receipt_bytes).context("parse source receipt")?;
    verify_receipt(&receipt)?;

    let repository = repository_root()?;
    verify_source_closure(&repository, &receipt)?;
    let cases = load_cases(&receipt_root, &receipt)?;
    validate_corpus(&cases)?;

    let mut counts = Counts::default();
    let mut tracked_timestamps: BTreeMap<String, TimestampProvenance> = BTreeMap::new();
    for case in &cases {
        replay_case(case, evidence, &mut counts, &mut tracked_timestamps)?;
        tracked_timestamps.clear();
    }

    ensure!(
        counts.source_actions == EXPECTED_ACTIONS,
        "source action count changed"
    );
    ensure!(
        counts.input_actions == EXPECTED_INPUT_ACTIONS,
        "input action inventory changed"
    );
    ensure!(
        counts.native_calls == EXPECTED_NATIVE_CALLS,
        "native decoded/helper call coverage changed"
    );
    ensure!(
        counts.unverified_actions == EXPECTED_UNVERIFIED_ACTIONS,
        "explicitly unverified source action count changed"
    );
    ensure!(
        counts.not_run_actions == EXPECTED_NOT_RUN_ACTIONS,
        "source setup-not-run action count changed"
    );
    ensure!(
        counts.host_invoke_boundary_only == EXPECTED_HOST_INVOKE_HELPER_CALLS,
        "captured Host invocation helper count changed"
    );
    ensure!(
        counts.matched_outcomes == EXPECTED_MATCHED_OUTCOMES,
        "matched result row count changed"
    );
    let complete_comparable_outcomes = counts.native_calls > 0 && counts.mismatches == 0;
    let summary = json!({
        "schema":"io.tobkiri.provider-registry-native-consumer.v1",
        "source_oracle":{
            "schema":RECEIPT_SCHEMA,
            "receipt_sha256":receipt_hash,
            "source_manifest_sha256":PINNED_SOURCE_MANIFEST_SHA256,
            "runtime":"CPython 3.13.9 / Unicode 15.1.0 / int limit 4300 / recursion 1000",
            "case_count":EXPECTED_CASES,
            "recorded_source_action_count":EXPECTED_ACTIONS,
        },
        "counts":{
            "input_action_definitions":counts.input_actions,
            "recorded_source_actions":counts.source_actions,
            "native_invocations":counts.native_calls,
            "matched_native_outcomes":counts.matched_outcomes,
            "native_mismatches":counts.mismatches,
            "explicitly_unverified_actions":counts.unverified_actions,
            "not_run_after_source_setup_error":counts.not_run_actions,
            "host_invoke_boundary_helper_only":counts.host_invoke_boundary_only,
            "capture_predicate_helper_only":counts.capture_predicate_only,
            "raw_store_parser_actions":counts.raw_store_parser_actions,
        },
        "all_exercised_decoded_outcomes_matched":complete_comparable_outcomes,
        "complete_provider_registry_parity_passed":false,
        "limitations":[
            "the native store is an in-memory decoded-document adapter; filesystem bytes, locks, permissions, directory effects, atomic replacement and concurrency are not verified",
            "the malformed raw-store JSON action remains unverified because this candidate has no raw JSON/file adapter",
            "host.invoke uses a pure helper and does not recreate the opaque CapturedHostProviderV4 object or its runtime dispatch context",
            "host.capture checks only the supported source predicate helper; non-JSON CapturedHostProviderV4 returns and configuration-factory captures remain unverified",
            "this consumer does not prove Core, Wasm, production routing, activation, or external credential revocation parity",
        ],
        "actions_journal":"actions.jsonl",
    });
    evidence.write_json("summary.json", &summary)?;
    ensure!(
        counts.mismatches == 0,
        "{} native source outcome/state/timing mismatches; see private evidence {}",
        counts.mismatches,
        evidence.directory.display()
    );
    Ok(())
}

fn required_env_path(name: &str) -> Result<PathBuf> {
    let raw = env::var(name).with_context(|| format!("required environment variable {name}"))?;
    let path = PathBuf::from(raw);
    ensure!(path.is_absolute(), "{name} must be an absolute path");
    ensure!(
        !path
            .components()
            .any(|component| component == Component::ParentDir),
        "{name} must not contain parent traversal"
    );
    Ok(path)
}

fn canonical_regular_path(path: &Path) -> Result<PathBuf> {
    let metadata = fs::symlink_metadata(path).context("inspect oracle receipt path")?;
    ensure!(
        metadata.is_file() && !metadata.file_type().is_symlink(),
        "oracle receipt is not a regular file"
    );
    path.canonicalize().context("canonicalize oracle receipt")
}

fn read_regular(path: &Path, maximum: usize) -> Result<Vec<u8>> {
    let metadata =
        fs::symlink_metadata(path).with_context(|| format!("inspect {}", path.display()))?;
    ensure!(
        metadata.is_file() && !metadata.file_type().is_symlink(),
        "not a regular non-symlink file: {}",
        path.display()
    );
    ensure!(
        metadata.len() <= maximum as u64,
        "file exceeds bounded read: {}",
        path.display()
    );
    fs::read(path).with_context(|| format!("read {}", path.display()))
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn verify_receipt(receipt: &Value) -> Result<()> {
    ensure!(
        receipt.get("schema").and_then(Value::as_str) == Some(RECEIPT_SCHEMA),
        "unexpected source receipt schema"
    );
    ensure!(
        receipt
            .get("scope")
            .and_then(Value::as_str)
            .is_some_and(|scope| {
                scope.contains("captured original Python ProviderRegistry")
                    && scope.contains("no Rust/Wasm execution")
            }),
        "source receipt scope does not identify an original-only observation"
    );
    ensure!(
        receipt
            .get("source_manifest_sha256")
            .and_then(Value::as_str)
            == Some(PINNED_SOURCE_MANIFEST_SHA256),
        "source manifest digest differs from the pinned capture"
    );
    let runtime = receipt.get("runtime").context("runtime pin missing")?;
    ensure!(
        runtime["implementation"] == "cpython",
        "receipt is not CPython"
    );
    ensure!(
        runtime["version"]["major"] == 3,
        "unexpected Python major version"
    );
    ensure!(
        runtime["version"]["minor"] == 13,
        "unexpected Python minor version"
    );
    ensure!(
        runtime["version"]["micro"] == 9,
        "unexpected Python micro version"
    );
    ensure!(
        runtime["version"]["releaselevel"] == "final",
        "unexpected Python release type"
    );
    ensure!(
        runtime["unicode_version"] == "15.1.0",
        "unexpected Unicode database version"
    );
    ensure!(
        runtime["int_max_str_digits"] == 4300,
        "unexpected integer string limit"
    );
    ensure!(
        runtime["recursion_limit"] == 1000,
        "unexpected recursion limit"
    );
    let runtime_hash = runtime["runtime_identity_sha256"]
        .as_str()
        .context("runtime identity hash missing")?;
    let mut runtime_identity = runtime.clone();
    runtime_identity
        .as_object_mut()
        .context("runtime identity is not a mapping")?
        .remove("runtime_identity_sha256");
    let canonical_runtime = serde_json::to_vec(&runtime_identity)?;
    ensure!(
        canonical_runtime.is_ascii(),
        "runtime identity must use ASCII JSON"
    );
    ensure!(
        digest(&canonical_runtime) == runtime_hash,
        "runtime identity hash is invalid"
    );
    ensure!(
        receipt["runtime_after_source"]["unchanged"] == true
            && receipt["runtime_after_source"]["runtime_identity_sha256"] == runtime_hash,
        "captured source runtime changed during observation"
    );

    let manifest = receipt["source_manifest"]
        .as_array()
        .context("source manifest missing")?;
    ensure!(
        manifest.len() == EXPECTED_SOURCE_FILES,
        "source closure inventory changed"
    );
    let canonical_manifest = serde_json::to_vec(&receipt["source_manifest"])?;
    ensure!(
        canonical_manifest.is_ascii(),
        "source manifest must use ASCII JSON"
    );
    ensure!(
        digest(&canonical_manifest) == PINNED_SOURCE_MANIFEST_SHA256,
        "source manifest bytes do not match the pinned digest"
    );
    ensure!(
        receipt["cases"]
            .as_array()
            .is_some_and(|cases| cases.len() == EXPECTED_CASES),
        "source case count changed"
    );
    Ok(())
}

fn repository_root() -> Result<PathBuf> {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .context("resolve repository root from package manifest")?
        .canonicalize()
        .context("canonicalize repository root")
}

fn safe_relative(text: &str) -> Result<PathBuf> {
    let path = Path::new(text);
    ensure!(
        !path.is_absolute(),
        "absolute path in source receipt: {text}"
    );
    ensure!(
        !text.contains('\\'),
        "backslash path in source receipt: {text}"
    );
    ensure!(
        path.components()
            .all(|component| matches!(component, Component::Normal(_))),
        "unsafe relative path in source receipt: {text}"
    );
    Ok(path.to_owned())
}

fn read_beneath(root: &Path, relative: &str, maximum: usize) -> Result<Vec<u8>> {
    let relative_path = safe_relative(relative)?;
    let path = root.join(relative_path);
    let metadata = fs::symlink_metadata(&path).with_context(|| format!("inspect {relative}"))?;
    ensure!(
        metadata.is_file() && !metadata.file_type().is_symlink(),
        "source path is not a regular non-symlink file: {relative}"
    );
    let canonical = path
        .canonicalize()
        .with_context(|| format!("canonicalize {relative}"))?;
    ensure!(
        canonical.starts_with(root),
        "source path escaped its root: {relative}"
    );
    read_regular(&canonical, maximum)
}

fn verify_source_closure(repository: &Path, receipt: &Value) -> Result<()> {
    let mut seen = BTreeSet::new();
    let manifest = receipt["source_manifest"]
        .as_array()
        .context("source manifest missing")?;
    for source in manifest {
        let path = source["path"].as_str().context("manifest path missing")?;
        ensure!(
            seen.insert(path.to_owned()),
            "duplicate manifest path {path}"
        );
        let expected_bytes = source["bytes"]
            .as_u64()
            .context("manifest byte count missing")?;
        let expected_hash = source["sha256"]
            .as_str()
            .context("manifest digest missing")?;
        ensure!(
            is_sha256(expected_hash),
            "invalid manifest digest for {path}"
        );
        let current = read_beneath(repository, path, MAX_SOURCE_BYTES)
            .with_context(|| format!("verify captured source {path}"))?;
        ensure!(
            current.len() as u64 == expected_bytes,
            "source size changed: {path}"
        );
        ensure!(
            digest(&current) == expected_hash,
            "source digest changed: {path}"
        );
    }
    verify_source_pin(repository, receipt, GENERATOR_PATH, PINNED_GENERATOR_SHA256)?;
    verify_source_pin(
        repository,
        receipt,
        CASE_SOURCE_PATH,
        PINNED_CASE_SOURCE_SHA256,
    )?;
    Ok(())
}

fn verify_source_pin(repository: &Path, receipt: &Value, path: &str, pinned: &str) -> Result<()> {
    let source = receipt["source_manifest"]
        .as_array()
        .and_then(|manifest| manifest.iter().find(|source| source["path"] == path))
        .context("pinned generator/case source is absent from closure")?;
    ensure!(
        source["sha256"] == pinned,
        "pinned source digest differs for {path}"
    );
    let bytes = read_beneath(repository, path, MAX_CASE_BYTES)?;
    ensure!(
        digest(&bytes) == pinned,
        "current generator/case source changed: {path}"
    );
    Ok(())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn load_cases(receipt_root: &Path, receipt: &Value) -> Result<Vec<PreparedCase>> {
    let rows = receipt["cases"].as_array().context("case rows missing")?;
    let mut cases = Vec::with_capacity(rows.len());
    let mut recorded_actions = 0usize;
    let mut input_actions = 0usize;
    for row in rows {
        let id = row["id"].as_str().context("case id missing")?.to_owned();
        let mut sidecars = BTreeMap::new();
        let input_bytes = read_reference(receipt_root, &row["input"], &mut sidecars)?;
        let observation_bytes = read_reference(receipt_root, &row["observation"], &mut sidecars)?;
        let input = parse_py(&input_bytes, &format!("{id} input"))?;
        let observation = parse_py(&observation_bytes, &format!("{id} observation"))?;
        ensure!(
            py_field_str(&input, "case_id")? == id,
            "input case id differs for {id}"
        );
        ensure!(
            py_field_str(&observation, "id")? == id,
            "observation case id differs for {id}"
        );
        ensure!(
            py_field_str(&observation, "source_manifest_sha256")? == PINNED_SOURCE_MANIFEST_SHA256,
            "observation source manifest digest differs for {id}"
        );

        let observation_json: Value = serde_json::from_slice(&observation_bytes)
            .with_context(|| format!("parse structured observation {id}"))?;
        collect_references(&observation_json, receipt_root, &mut sidecars)?;
        for action in py_array(&observation, "actions")? {
            if let Some(outcome) = action.get("outcome")
                && outcome.get("kind").and_then(PyValue::as_str) == Some("result")
            {
                let reference = outcome
                    .get("result")
                    .context("result sidecar reference missing")?;
                let path = py_field_str(reference, "path")?;
                parse_py(
                    sidecars
                        .get(path)
                        .with_context(|| format!("validated result sidecar missing: {path}"))?,
                    path,
                )?;
            }
            for phase in ["state_before", "state_after"] {
                if let Some(store) = action.get(phase).and_then(|state| state.get("store"))
                    && store.get("exists").and_then(py_bool) == Some(true)
                    && store.get("parse").and_then(PyValue::as_str) == Some("json")
                {
                    let path = py_field_str(store, "path")?;
                    parse_py(
                        sidecars
                            .get(path)
                            .with_context(|| format!("validated store sidecar missing: {path}"))?,
                        path,
                    )?;
                }
            }
        }
        let input_action_count = py_array(&input, "actions")?.len();
        input_actions += input_action_count;
        let source_action_count = py_array(&observation, "actions")?
            .iter()
            .filter(|action| action.get("status").and_then(PyValue::as_str) != Some("not-run"))
            .count();
        if let Some(expected_count) = row["action_count"].as_u64() {
            ensure!(
                source_action_count as u64 == expected_count,
                "receipt action count changed for {id}"
            );
        } else {
            ensure!(
                source_action_count == 0,
                "setup-failed case unexpectedly recorded actions: {id}"
            );
        }
        recorded_actions += source_action_count;
        cases.push(PreparedCase {
            id,
            input,
            observation,
            sidecars,
        });
    }
    ensure!(
        recorded_actions == EXPECTED_ACTIONS,
        "recorded source actions are not 85"
    );
    ensure!(
        input_actions == EXPECTED_INPUT_ACTIONS,
        "input action definitions are not 86"
    );
    Ok(cases)
}

fn read_reference(
    root: &Path,
    reference: &Value,
    cache: &mut BTreeMap<String, Vec<u8>>,
) -> Result<Vec<u8>> {
    let path = reference["path"].as_str().context("sidecar path missing")?;
    ensure!(path.starts_with("cases/"), "unexpected sidecar path {path}");
    let bytes = read_beneath(root, path, MAX_CASE_BYTES)?;
    let expected_length = reference["bytes"]
        .as_u64()
        .context("sidecar byte count missing")?;
    let expected_hash = reference["sha256"]
        .as_str()
        .context("sidecar digest missing")?;
    if let Some(encoding) = reference.get("encoding").and_then(Value::as_str) {
        ensure!(
            encoding == "ascii-json",
            "unexpected sidecar encoding for {path}"
        );
    }
    ensure!(
        bytes.len() as u64 == expected_length,
        "sidecar size changed: {path}"
    );
    ensure!(
        digest(&bytes) == expected_hash,
        "sidecar digest changed: {path}"
    );
    if let Some(existing) = cache.insert(path.into(), bytes.clone()) {
        ensure!(
            existing == bytes,
            "sidecar path resolves to changing bytes: {path}"
        );
    }
    Ok(bytes)
}

fn collect_references(
    value: &Value,
    root: &Path,
    cache: &mut BTreeMap<String, Vec<u8>>,
) -> Result<()> {
    match value {
        Value::Object(fields) => {
            if fields
                .get("path")
                .and_then(Value::as_str)
                .is_some_and(|path| path.starts_with("cases/"))
                && fields.contains_key("bytes")
                && fields.contains_key("sha256")
            {
                let reference = Value::Object(fields.clone());
                read_reference(root, &reference, cache)?;
            }
            for nested in fields.values() {
                collect_references(nested, root, cache)?;
            }
        }
        Value::Array(values) => {
            for nested in values {
                collect_references(nested, root, cache)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn parse_py(bytes: &[u8], name: &str) -> Result<PyValue> {
    let text = std::str::from_utf8(bytes).with_context(|| format!("{name} is not UTF-8"))?;
    loads(text).map_err(|error| anyhow::anyhow!("decode {name}: {error}"))
}

fn validate_corpus(cases: &[PreparedCase]) -> Result<()> {
    ensure!(cases.len() == EXPECTED_CASES, "case inventory changed");
    for case in cases {
        let inputs = py_array(&case.input, "actions")?;
        let observations = py_array(&case.observation, "actions")?;
        ensure!(
            inputs.len() == observations.len(),
            "action row count differs for {}",
            case.id
        );
        for (index, (input, observed)) in inputs.iter().zip(observations).enumerate() {
            let operation = py_field_str(input, "operation")?;
            let payload = input
                .get("payload")
                .context("input action payload missing")?;
            ensure!(
                payload.as_object().is_some(),
                "input payload is not a mapping in {}#{index}",
                case.id
            );
            ensure!(
                matches!(
                    operation,
                    "registry.snapshot"
                        | "registry.health"
                        | "registry.prepare_save"
                        | "registry.save"
                        | "registry.set_model_access"
                        | "registry.delete"
                        | "host.invoke"
                        | "host.capture"
                ),
                "unknown corpus operation {operation} in {}#{index}",
                case.id
            );
            if observed.get("status").and_then(PyValue::as_str) == Some("not-run") {
                ensure!(
                    py_field_str(observed, "reason")?
                        == "registry construction raised before dispatch",
                    "unknown source not-run reason in {}#{index}",
                    case.id
                );
                continue;
            }
            ensure!(
                py_field_str(input, "action_id")? == py_field_str(observed, "id")?,
                "action id changed in {}#{index}",
                case.id
            );
            ensure!(
                operation == py_field_str(observed, "operation")?,
                "operation changed in {}#{index}",
                case.id
            );
            ensure!(
                payload
                    == observed
                        .get("payload")
                        .context("source action payload missing")?,
                "action payload changed in {}#{index}",
                case.id
            );
            validate_dispatch_shape(operation, payload)
                .with_context(|| format!("validate corpus selector {}#{index}", case.id))?;
        }
    }
    Ok(())
}

fn validate_dispatch_shape(operation: &str, payload: &PyValue) -> Result<()> {
    match operation {
        "registry.snapshot" | "registry.health" => Ok(()),
        "registry.prepare_save" | "registry.save" => {
            require_field(payload, "record")?;
            require_field(payload, "expected_revision")?;
            Ok(())
        }
        "registry.set_model_access" => {
            require_field(payload, "provider_instance_id")?;
            require_field(payload, "policy")?;
            require_field(payload, "expected_revision")?;
            Ok(())
        }
        "registry.delete" => {
            require_field(payload, "provider_instance_id")?;
            require_field(payload, "expected_revision")?;
            Ok(())
        }
        "host.invoke" => {
            let factory = py_field_str(payload, "factory")?;
            ensure!(
                matches!(factory, "readonly" | "manage"),
                "unknown host factory selector {factory}"
            );
            py_field_str(payload, "operation_id")?;
            ensure!(
                payload
                    .get("payload")
                    .is_some_and(|value| value.as_object().is_some()),
                "host invoke payload is not an object"
            );
            Ok(())
        }
        "host.capture" => {
            let factory = py_field_str(payload, "factory")?;
            ensure!(
                matches!(
                    factory,
                    "readonly" | "configure-prepare" | "configure-execute"
                ),
                "unknown host capture selector {factory}"
            );
            ensure!(
                matches!(py_field_str(payload, "binding_mode")?, "empty" | "complete"),
                "unknown host binding selector"
            );
            Ok(())
        }
        _ => bail!("unrecognized action operation {operation}"),
    }
}

fn replay_case(
    case: &PreparedCase,
    evidence: &mut Evidence,
    counts: &mut Counts,
    tracked_timestamps: &mut BTreeMap<String, TimestampProvenance>,
) -> Result<()> {
    tracked_timestamps.clear();
    let profile = case
        .input
        .get("profile_id")
        .context("case profile missing")?;
    let initial_text = case
        .input
        .get("initial_store_text")
        .context("initial store field missing")?;
    let source_setup_error = case.observation.get("setup_error");

    // The source constructor runs before the fixture writes its raw store file.
    // Exercise that constructor independently so invalid profile setup is not
    // misreported as an action result.
    let setup_clock = OracleClock(Rc::new(Cell::new(0)));
    let setup = Registry::new(profile, MemoryStore::absent(), setup_clock);
    if let Some(expected_setup) = source_setup_error {
        match setup {
            Err(actual) => {
                let matched = error_matches(&actual, expected_setup)?;
                evidence.append(&json!({
                    "case_id":case.id,
                    "record_kind":"registry-construction",
                    "native_kind":"error",
                    "native_exception_class":actual.exception_class,
                    "native_message":actual.message,
                    "native_args_repr":actual.args_repr(),
                    "source_match":matched,
                    "classification":"decoded-profile-validation",
                }))?;
                if matched {
                    counts.matched_outcomes += 1;
                } else {
                    counts.mismatches += 1;
                }
            }
            Ok(_) => {
                counts.mismatches += 1;
                evidence.append(&json!({
                    "case_id":case.id,
                    "record_kind":"registry-construction",
                    "source_match":false,
                    "native_kind":"success",
                }))?;
            }
        }
        for action in py_array(&case.input, "actions")? {
            let observed = py_array(&case.observation, "actions")?
                .iter()
                .find(|row| {
                    row.get("id").and_then(PyValue::as_str)
                        == action.get("action_id").and_then(PyValue::as_str)
                })
                .context("source not-run action row missing")?;
            ensure!(
                observed.get("status").and_then(PyValue::as_str) == Some("not-run"),
                "setup error must stop source action dispatch"
            );
            counts.input_actions += 1;
            counts.not_run_actions += 1;
            evidence.append(&json!({
                "case_id":case.id,
                "action_id":py_field_str(action,"action_id")?,
                "operation":py_field_str(action,"operation")?,
                "native_status":"not-run",
                "reason":"source setup error occurred before action dispatch",
                "classification":"source-not-dispatched",
            }))?;
        }
        return Ok(());
    }
    if let Err(actual) = setup {
        counts.mismatches += 1;
        evidence.append(&json!({
            "case_id":case.id,
            "record_kind":"registry-construction",
            "native_kind":"error",
            "native_exception_class":actual.exception_class,
            "native_message":actual.message,
            "source_match":false,
            "classification":"unexpected-native-setup-error",
        }))?;
        for action in py_array(&case.input, "actions")? {
            counts.input_actions += 1;
            counts.source_actions += 1;
            counts.unverified_actions += 1;
            evidence.append(&json!({
                "case_id":case.id,
                "action_id":py_field_str(action,"action_id")?,
                "native_status":"unverified",
                "reason":"native Registry construction failed before decoded replay",
            }))?;
        }
        return Ok(());
    }

    let initial_document = match initial_text {
        PyValue::Null => None,
        PyValue::String(text) => match loads(text) {
            Ok(value) => Some(value),
            Err(error) => {
                let actions = py_array(&case.observation, "actions")?;
                for action in actions {
                    counts.input_actions += 1;
                    if action.get("status").and_then(PyValue::as_str) == Some("not-run") {
                        counts.not_run_actions += 1;
                        continue;
                    }
                    counts.source_actions += 1;
                    counts.unverified_actions += 1;
                    counts.raw_store_parser_actions += 1;
                    evidence.append(&json!({
                        "case_id":case.id,
                        "action_id":py_field_str(action,"id")?,
                        "operation":py_field_str(action,"operation")?,
                        "native_status":"unverified",
                        "reason":"source reads malformed raw store bytes before dispatch; raw file/JSON parser adapter is not implemented",
                        "native_decode_error":error.to_string(),
                        "classification":"raw-store-parser-unimplemented",
                    }))?;
                }
                return Ok(());
            }
        },
        _ => bail!(
            "initial_store_text must be null or a string for {}",
            case.id
        ),
    };
    let store = initial_document.map_or_else(MemoryStore::absent, MemoryStore::initialized);
    let ticks = Rc::new(Cell::new(0));
    let mut registry = Registry::new(profile, store, OracleClock(Rc::clone(&ticks)))
        .context("construct native Registry after source setup validation")?;

    for (input_action, observed_action) in py_array(&case.input, "actions")?
        .iter()
        .zip(py_array(&case.observation, "actions")?)
    {
        counts.input_actions += 1;
        if observed_action.get("status").and_then(PyValue::as_str) == Some("not-run") {
            counts.not_run_actions += 1;
            evidence.append(&json!({
                "case_id":case.id,
                "action_id":py_field_str(input_action,"action_id")?,
                "operation":py_field_str(input_action,"operation")?,
                "native_status":"not-run",
                "classification":"source-not-dispatched",
            }))?;
            continue;
        }
        counts.source_actions += 1;

        let operation = py_field_str(input_action, "operation")?;
        let payload = input_action
            .get("payload")
            .context("action payload missing")?;
        let expected = observed_action
            .get("outcome")
            .context("source action outcome missing")?;
        let expected_kind = py_field_str(expected, "kind")?;

        let source_store_before = observed_action
            .get("state_before")
            .and_then(|value| value.get("store"))
            .context("source state_before store observation missing")?;
        let source_document_before =
            source_store_document(Some(source_store_before), &case.sidecars)?;
        let source_store_before_bytes =
            source_store_sidecar_bytes(Some(source_store_before), &case.sidecars)?;
        if let Err(state_match_error) = compare_source_store(
            registry.store().document(),
            Some(source_store_before),
            &case.sidecars,
            tracked_timestamps,
        ) {
            // The native call still runs after a state mismatch so the journal
            // retains all source rows and cascaded errors for diagnosis.
            counts.mismatches += 1;
            evidence.append(&json!({
                "case_id":case.id,
                "action_id":py_field_str(input_action,"action_id")?,
                "phase":"state-before",
                "source_match":false,
                "error":format!("{state_match_error:#}"),
            }))?;
        }

        let raw_store =
            matches!(initial_text, PyValue::String(_)) && case.id == "malformed-store-json";
        if raw_store {
            counts.unverified_actions += 1;
            counts.raw_store_parser_actions += 1;
            evidence.append(&json!({
                "case_id":case.id,
                "action_id":py_field_str(input_action,"action_id")?,
                "operation":operation,
                "native_status":"unverified",
                "source_expected_kind":expected_kind,
                "reason":"source opens and parses malformed store bytes before the method call; raw file/JSON parser adapter is not implemented",
                "classification":"raw-store-parser-unimplemented",
            }))?;
            continue;
        }

        let factory = if operation == "host.invoke" {
            payload.get("factory").and_then(PyValue::as_str)
        } else {
            None
        };
        if operation == "host.capture"
            && matches!(
                payload.get("factory").and_then(PyValue::as_str),
                Some("configure-prepare" | "configure-execute")
            )
        {
            counts.unverified_actions += 1;
            evidence.append(&json!({
                "case_id":case.id,
                "action_id":py_field_str(input_action,"action_id")?,
                "operation":operation,
                "native_status":"unverified",
                "source_expected_kind":expected_kind,
                "reason":"configuration factory and CapturedHostProviderV4 return are outside this Registry helper API",
                "classification":"opaque-host-capture-unimplemented",
            }))?;
            continue;
        }

        let write_before = registry.store().write_count();
        let clock_before = ticks.get();
        let old_created = mutation_target(operation, payload).and_then(|(kind, record_id)| {
            let id = record::identifier(record_id).ok()?;
            if kind == "save" {
                store_created_as_python_string(registry.store().document(), &id)
            } else {
                store_created_as_json(registry.store().document(), &id)
            }
        });
        let native_document_before = registry.store().document().cloned();
        let native = dispatch(&mut registry, profile, operation, payload)?;
        counts.native_calls += 1;
        if factory.is_some() {
            counts.host_invoke_boundary_only += 1;
        }
        if operation == "host.capture" {
            counts.capture_predicate_only += 1;
        }
        let clock_after = ticks.get();
        let write_after = registry.store().write_count();
        let clock_delta = clock_after.saturating_sub(clock_before);
        let write_delta = write_after.saturating_sub(write_before);
        let source_mutation_succeeded =
            expected_kind == "result" && mutation_target(operation, payload).is_some();
        let source_expected_clock_delta = if source_mutation_succeeded { 1 } else { 0 };
        let source_write_changed = observed_action
            .get("effects")
            .and_then(|effects| effects.get("store_bytes_changed"))
            .and_then(py_bool)
            .context("source store byte-change observation missing")?;
        let source_store_after = observed_action
            .get("state_after")
            .and_then(|value| value.get("store"))
            .context("source state_after store observation missing")?;
        let source_document_after =
            source_store_document(Some(source_store_after), &case.sidecars)?;
        let source_store_after_bytes =
            source_store_sidecar_bytes(Some(source_store_after), &case.sidecars)?;
        advance_timestamp_provenance(
            operation,
            payload,
            expected_kind,
            source_document_before.as_ref(),
            tracked_timestamps,
        );
        let mut issues = Vec::new();

        if clock_delta != source_expected_clock_delta {
            issues.push(format!(
                "clock-consumption expected {source_expected_clock_delta}, observed {clock_delta}"
            ));
        }
        if (write_delta != 0) != source_write_changed {
            issues.push(format!(
                "memory-store write effect expected {source_write_changed}, observed {}",
                write_delta != 0
            ));
        }

        if !source_write_changed
            && (source_store_before_bytes != source_store_after_bytes
                || source_document_before != source_document_after)
        {
            issues.push("source read/error action changed its store bytes or decoded state".into());
        }
        if write_delta == 0 && native_document_before != registry.store().document().cloned() {
            issues.push(
                "native read/error action changed decoded store state without a write".into(),
            );
        }

        let source_success_target = if expected_kind == "result" {
            mutation_target(operation, payload)
                .and_then(|(_, id)| record::identifier(id).ok())
                .or_else(|| {
                    delete_target(operation, payload).and_then(|id| record::identifier(id).ok())
                })
        } else {
            None
        };
        let allow_created_update = expected_kind == "result"
            && mutation_target(operation, payload).is_some_and(|(kind, _)| kind == "save");
        let allow_updated = expected_kind == "result"
            && mutation_target(operation, payload)
                .is_some_and(|(kind, _)| matches!(kind, "save" | "model_access"));
        if !timestamp_state_transition_matches(
            source_document_before.as_ref(),
            source_document_after.as_ref(),
            source_success_target.as_deref(),
            allow_created_update,
            allow_updated,
        ) {
            issues.push("source action changed unrelated or retained provider timestamps".into());
        }
        if !timestamp_state_transition_matches(
            native_document_before.as_ref(),
            registry.store().document(),
            source_success_target.as_deref(),
            allow_created_update,
            allow_updated,
        ) {
            issues.push("native action changed unrelated or retained provider timestamps".into());
        }

        let outcome_match = compare_outcome(&native, expected, &case.sidecars, tracked_timestamps)?;
        if !outcome_match {
            issues.push("native result/exception differs from original observation".into());
        }
        let timestamp_check = check_timestamp_relationships(
            &registry,
            TimestampCheck {
                operation,
                payload,
                native: &native,
                source_outcome: expected,
                expected_kind,
                source_before: source_document_before.as_ref(),
                source_after: source_document_after.as_ref(),
                previous_created: old_created.as_deref(),
                clock_after,
                sidecars: &case.sidecars,
                tracked: tracked_timestamps,
            },
        )?;
        if !timestamp_check {
            issues.push("independent clock/created_at/updated_at check failed".into());
        }
        if let Err(error) = compare_source_store(
            registry.store().document(),
            Some(source_store_after),
            &case.sidecars,
            tracked_timestamps,
        ) {
            issues.push(format!(
                "native post-state differs from source decoded store: {error:#}"
            ));
        }

        let matched = issues.is_empty();
        if matched {
            counts.matched_outcomes += 1;
        } else {
            counts.mismatches += 1;
        }
        evidence.append(&action_report(ActionReport {
            case,
            input_action,
            observed_action,
            operation,
            native: &native,
            expected_kind,
            matched,
            issues: &issues,
            clock_before,
            clock_after,
            write_delta,
            source_write_changed,
            timestamp_check,
            host_invoke: factory.is_some(),
            capture_predicate: operation == "host.capture",
        })?)?;
    }
    Ok(())
}

fn dispatch<C: Clock>(
    registry: &mut Registry<MemoryStore, C>,
    profile: &PyValue,
    operation: &str,
    payload: &PyValue,
) -> Result<Option<NativeOutcome>> {
    let native = match operation {
        "registry.snapshot" => Some(registry.snapshot()),
        "registry.health" => Some(registry.health()),
        "registry.prepare_save" => Some(registry.prepare_save(
            require_field(payload, "record")?,
            require_field(payload, "expected_revision")?,
        )),
        "registry.save" => Some(registry.save(
            require_field(payload, "record")?,
            require_field(payload, "expected_revision")?,
        )),
        "registry.set_model_access" => Some(registry.set_model_access(
            require_field(payload, "provider_instance_id")?,
            require_field(payload, "policy")?,
            require_field(payload, "expected_revision")?,
        )),
        "registry.delete" => Some(registry.delete(
            require_field(payload, "provider_instance_id")?,
            require_field(payload, "expected_revision")?,
        )),
        "host.invoke" => {
            let mode = py_field_str(payload, "factory")?;
            let operation_id = py_field_str(payload, "operation_id")?;
            let request = require_field(payload, "payload")?;
            Some(match mode {
                "readonly" => host::invoke_readonly(
                    registry,
                    py_field_str_value(profile)?,
                    operation_id,
                    request,
                ),
                "manage" => host::invoke_manage(
                    registry,
                    py_field_str_value(profile)?,
                    operation_id,
                    request,
                ),
                _ => bail!("unknown host invocation selector {mode}"),
            })
        }
        "host.capture" => {
            let factory = py_field_str(payload, "factory")?;
            let binding_mode = py_field_str(payload, "binding_mode")?;
            if factory != "readonly" || binding_mode != "empty" {
                return Ok(None);
            }
            Some(
                host::validate_capture_facts(FactoryMode::Readonly, true, profile, &[])
                    .map(|count| PyValue::Int(count.to_string())),
            )
        }
        _ => bail!("validated corpus operation unexpectedly became unknown: {operation}"),
    };
    Ok(native)
}

fn compare_outcome(
    native: &Option<NativeOutcome>,
    source_outcome: &PyValue,
    sidecars: &BTreeMap<String, Vec<u8>>,
    timestamps: &BTreeMap<String, TimestampProvenance>,
) -> Result<bool> {
    let kind = py_field_str(source_outcome, "kind")?;
    match kind {
        "result" => {
            let expected_reference = source_outcome
                .get("result")
                .context("source result sidecar reference missing")?;
            let path = py_field_str(expected_reference, "path")?;
            let expected_bytes = sidecars
                .get(path)
                .with_context(|| format!("validated source result sidecar missing: {path}"))?;
            let expected = parse_py(expected_bytes, path)?;
            match native {
                Some(Ok(actual)) => Ok(source_value_equal(actual, &expected, timestamps)),
                _ => Ok(false),
            }
        }
        "exception" => match native {
            Some(Err(actual)) => error_matches(actual, source_outcome),
            _ => Ok(false),
        },
        _ => Ok(false),
    }
}

fn error_matches(actual: &SourceError, expected: &PyValue) -> Result<bool> {
    if expected.get("exception_class").and_then(PyValue::as_str) != Some(actual.exception_class)
        || expected.get("message").and_then(PyValue::as_str) != Some(actual.message.as_str())
    {
        return Ok(false);
    }
    if let Some(args_repr) = expected.get("args_repr").and_then(PyValue::as_str) {
        let actual_args_repr = actual.args_repr();
        return Ok(args_repr == actual_args_repr.as_str());
    }
    Ok(true)
}

fn source_value_equal(
    actual: &PyValue,
    expected: &PyValue,
    timestamps: &BTreeMap<String, TimestampProvenance>,
) -> bool {
    match (actual, expected) {
        (PyValue::Object(actual), PyValue::Object(expected)) => {
            let actual_id = object_string_field(actual, "provider_instance_id");
            let expected_id = object_string_field(expected, "provider_instance_id");
            let provider_id = actual_id.filter(|id| expected_id == Some(*id));
            actual.len() == expected.len()
                && actual.iter().zip(expected).all(
                    |((actual_key, actual_value), (expected_key, expected_value))| {
                        if actual_key != expected_key {
                            return false;
                        }
                        if let Some(provider_id) = provider_id
                            && matches!(actual_key.as_str(), "created_at" | "updated_at")
                            && timestamp_is_generated(timestamps, provider_id, actual_key)
                        {
                            return matches!(actual_value, PyValue::String(_))
                                && matches!(expected_value, PyValue::String(_));
                        }
                        source_value_equal(actual_value, expected_value, timestamps)
                    },
                )
        }
        (PyValue::Array(actual), PyValue::Array(expected)) => {
            actual.len() == expected.len()
                && actual
                    .iter()
                    .zip(expected)
                    .all(|(left, right)| source_value_equal(left, right, timestamps))
        }
        _ => actual == expected,
    }
}

fn object_string_field<'a>(object: &'a [(String, PyValue)], field: &str) -> Option<&'a str> {
    object
        .iter()
        .find(|(key, _)| key == field)
        .and_then(|(_, value)| value.as_str())
}

fn timestamp_is_generated(
    timestamps: &BTreeMap<String, TimestampProvenance>,
    provider_id: &str,
    field: &str,
) -> bool {
    timestamps
        .get(provider_id)
        .is_some_and(|provenance| match field {
            "created_at" => provenance.created_at_generated,
            "updated_at" => provenance.updated_at_generated,
            _ => false,
        })
}

fn compare_source_store(
    native: Option<&PyValue>,
    source_store: Option<&PyValue>,
    sidecars: &BTreeMap<String, Vec<u8>>,
    timestamps: &BTreeMap<String, TimestampProvenance>,
) -> Result<()> {
    let source_exists = source_store
        .and_then(|store| store.get("exists"))
        .and_then(py_bool)
        .context("source store existence observation missing")?;
    if !source_exists {
        ensure!(
            native.is_none(),
            "source store is absent but native decoded store exists"
        );
        return Ok(());
    }
    let reference = source_store
        .and_then(|store| store.get("path"))
        .and_then(PyValue::as_str)
        .context("source store sidecar path missing")?;
    let bytes = sidecars
        .get(reference)
        .with_context(|| format!("validated store sidecar missing: {reference}"))?;
    let expected = parse_py(bytes, reference)?;
    let native = native.context("source store exists but native decoded store is absent")?;
    ensure!(
        source_value_equal(native, &expected, timestamps),
        "decoded document differs"
    );
    Ok(())
}

fn mutation_target<'a>(
    operation: &str,
    payload: &'a PyValue,
) -> Option<(&'static str, &'a PyValue)> {
    match operation {
        "registry.save" => Some(("save", payload.get("record")?.get("provider_instance_id")?)),
        "registry.set_model_access" => Some(("model_access", payload.get("provider_instance_id")?)),
        "host.invoke" if payload.get("factory").and_then(PyValue::as_str) == Some("manage") => {
            let request = payload.get("payload")?;
            match request.get("operation").and_then(PyValue::as_str)? {
                "save" => Some(("save", request.get("record")?.get("provider_instance_id")?)),
                "model_access.save" => Some(("model_access", request.get("provider_instance_id")?)),
                _ => None,
            }
        }
        _ => None,
    }
}

fn source_store_document(
    source_store: Option<&PyValue>,
    sidecars: &BTreeMap<String, Vec<u8>>,
) -> Result<Option<PyValue>> {
    let exists = source_store
        .and_then(|store| store.get("exists"))
        .and_then(py_bool)
        .context("source store existence observation missing")?;
    if !exists {
        return Ok(None);
    }
    let reference = source_store
        .and_then(|store| store.get("path"))
        .and_then(PyValue::as_str)
        .context("source store sidecar path missing")?;
    let bytes = sidecars
        .get(reference)
        .with_context(|| format!("validated store sidecar missing: {reference}"))?;
    Ok(Some(parse_py(bytes, reference)?))
}

fn source_store_sidecar_bytes<'a>(
    source_store: Option<&PyValue>,
    sidecars: &'a BTreeMap<String, Vec<u8>>,
) -> Result<Option<&'a [u8]>> {
    let exists = source_store
        .and_then(|store| store.get("exists"))
        .and_then(py_bool)
        .context("source store existence observation missing")?;
    if !exists {
        return Ok(None);
    }
    let reference = source_store
        .and_then(|store| store.get("path"))
        .and_then(PyValue::as_str)
        .context("source store sidecar path missing")?;
    Ok(Some(
        sidecars
            .get(reference)
            .with_context(|| format!("validated store sidecar missing: {reference}"))?
            .as_slice(),
    ))
}

fn advance_timestamp_provenance(
    operation: &str,
    payload: &PyValue,
    expected_kind: &str,
    source_before: Option<&PyValue>,
    tracked: &mut BTreeMap<String, TimestampProvenance>,
) {
    if expected_kind != "result" {
        return;
    }
    if let Some((kind, id_value)) = mutation_target(operation, payload) {
        let Ok(id) = record::identifier(id_value) else {
            return;
        };
        let previous = tracked.get(&id).copied().unwrap_or_default();
        let previous_record = stored_record(source_before, &id);
        let generated_created = if kind == "save" {
            previous.created_at_generated
                || previous_record
                    .and_then(|record| record.get("created_at"))
                    .is_none_or(|value| !pytruthy(value))
        } else {
            previous.created_at_generated
        };
        tracked.insert(
            id,
            TimestampProvenance {
                created_at_generated: generated_created,
                updated_at_generated: true,
            },
        );
        return;
    }
    if let Some(id) =
        delete_target(operation, payload).and_then(|value| record::identifier(value).ok())
    {
        tracked.remove(&id);
    }
}

fn delete_target<'a>(operation: &str, payload: &'a PyValue) -> Option<&'a PyValue> {
    match operation {
        "registry.delete" => payload.get("provider_instance_id"),
        "host.invoke" if payload.get("factory").and_then(PyValue::as_str) == Some("manage") => {
            let request = payload.get("payload")?;
            (request.get("operation").and_then(PyValue::as_str) == Some("delete"))
                .then(|| request.get("provider_instance_id"))
                .flatten()
        }
        _ => None,
    }
}

fn mutation_result_id<'a>(result: &'a PyValue, kind: &str) -> Option<&'a str> {
    match kind {
        "save" => result
            .get("provider")
            .and_then(|provider| provider.get("provider_instance_id"))
            .and_then(PyValue::as_str),
        "model_access" => result.get("provider_instance_id").and_then(PyValue::as_str),
        _ => None,
    }
}

struct TimestampCheck<'a> {
    operation: &'a str,
    payload: &'a PyValue,
    native: &'a Option<NativeOutcome>,
    source_outcome: &'a PyValue,
    expected_kind: &'a str,
    source_before: Option<&'a PyValue>,
    source_after: Option<&'a PyValue>,
    previous_created: Option<&'a str>,
    clock_after: usize,
    sidecars: &'a BTreeMap<String, Vec<u8>>,
    tracked: &'a BTreeMap<String, TimestampProvenance>,
}

fn check_timestamp_relationships<C: Clock>(
    registry: &Registry<MemoryStore, C>,
    check: TimestampCheck<'_>,
) -> Result<bool> {
    let TimestampCheck {
        operation,
        payload,
        native,
        source_outcome,
        expected_kind,
        source_before,
        source_after,
        previous_created,
        clock_after,
        sidecars,
        tracked,
    } = check;
    if expected_kind != "result" {
        return Ok(true);
    }
    let result_reference = source_outcome
        .get("result")
        .context("source result sidecar reference missing for timestamp checks")?;
    let reference = py_field_str(result_reference, "path")?;
    let bytes = sidecars
        .get(reference)
        .with_context(|| format!("validated source result sidecar missing: {reference}"))?;
    let source_result = parse_py(bytes, reference)?;
    let Some(Ok(native_result)) = native else {
        return Ok(false);
    };
    let native_after = registry.store().document();

    let mut matched = true;
    if returns_raw_snapshot(operation, payload) {
        matched &= snapshot_timestamp_records_match(&source_result, source_after, tracked);
        matched &= snapshot_timestamp_records_match(native_result, native_after, tracked);
    }

    if let Some((kind, id_value)) = mutation_target(operation, payload) {
        let Ok(id) = record::identifier(id_value) else {
            return Ok(false);
        };
        let source_result_id = mutation_result_id(&source_result, kind);
        let native_result_id = mutation_result_id(native_result, kind);
        if source_result_id != Some(id.as_str()) || native_result_id != Some(id.as_str()) {
            matched = false;
        }
        let source_record = stored_record(source_after, &id);
        let native_record = stored_record(native_after, &id);
        if kind == "save" {
            matched &= source_result
                .get("provider")
                .zip(source_record)
                .is_some_and(|(result, stored)| {
                    provider_timestamp_fields_match(result, stored, tracked)
                });
            matched &= native_result
                .get("provider")
                .zip(native_record)
                .is_some_and(|(result, stored)| {
                    provider_timestamp_fields_match(result, stored, tracked)
                });
        }

        let Some(source_record) = source_record else {
            return Ok(false);
        };
        let Some(native_record) = native_record else {
            return Ok(false);
        };
        let tick = clock_value(clock_after);
        let source_updated = source_record.get("updated_at");
        let native_updated = native_record.get("updated_at");
        matched &= matches!(source_updated, Some(PyValue::String(_)));
        matched &= native_updated.and_then(PyValue::as_str) == Some(tick.as_str());
        if kind == "save" {
            if let Some(previous) =
                stored_record(source_before, &id).and_then(|r| r.get("created_at"))
                && pytruthy(previous)
            {
                let expected_source_created = pystr(previous);
                matched &= source_record.get("created_at").and_then(PyValue::as_str)
                    == Some(expected_source_created.as_str());
            } else {
                matched &= matches!(
                    (source_record.get("created_at"), source_updated),
                    (Some(PyValue::String(created)), Some(PyValue::String(updated))) if created == updated
                );
            }
            let expected_created = previous_created
                .map(str::to_owned)
                .unwrap_or_else(|| tick.clone());
            matched &= native_record.get("created_at").and_then(PyValue::as_str)
                == Some(expected_created.as_str());
        } else {
            matched &= same_optional_field(
                stored_record(source_before, &id),
                Some(source_record),
                "created_at",
            );
            if let Some(previous) = previous_created {
                matched &= native_record
                    .get("created_at")
                    .is_some_and(|value| dumps(value).ok().as_deref() == Some(previous));
            }
        }
    }
    Ok(matched)
}

fn returns_raw_snapshot(operation: &str, payload: &PyValue) -> bool {
    if operation == "registry.snapshot" {
        return true;
    }
    let Some(operation_id) = payload.get("operation_id").and_then(PyValue::as_str) else {
        return false;
    };
    operation == "host.invoke"
        && payload.get("factory").and_then(PyValue::as_str) == Some("readonly")
        && matches!(
            operation_id,
            host::READONLY_GENERATE_OPERATION | host::READONLY_STREAM_OPERATION
        )
}

fn same_optional_field(left: Option<&PyValue>, right: Option<&PyValue>, field: &str) -> bool {
    match (
        left.and_then(|record| record.get(field)),
        right.and_then(|record| record.get(field)),
    ) {
        (None, None) => true,
        (Some(left), Some(right)) => left == right,
        _ => false,
    }
}

fn provider_timestamp_fields_match(
    result_record: &PyValue,
    stored_record: &PyValue,
    tracked: &BTreeMap<String, TimestampProvenance>,
) -> bool {
    let Some(id) = result_record
        .get("provider_instance_id")
        .and_then(PyValue::as_str)
    else {
        return false;
    };
    if stored_record
        .get("provider_instance_id")
        .and_then(PyValue::as_str)
        != Some(id)
    {
        return false;
    }
    ["created_at", "updated_at"].into_iter().all(|field| {
        let generated = timestamp_is_generated(tracked, id, field);
        match (result_record.get(field), stored_record.get(field)) {
            (None, None) => !generated,
            (Some(PyValue::String(left)), Some(PyValue::String(right))) if generated => {
                left == right
            }
            (Some(left), Some(right)) => left == right,
            _ => false,
        }
    })
}

fn snapshot_timestamp_records_match(
    snapshot: &PyValue,
    document: Option<&PyValue>,
    tracked: &BTreeMap<String, TimestampProvenance>,
) -> bool {
    let Some(snapshot_records) = snapshot.get("providers").and_then(PyValue::as_array) else {
        return false;
    };
    let stored_records = document
        .and_then(|value| value.get("providers"))
        .and_then(PyValue::as_object);
    if snapshot_records.len() != stored_records.map_or(0, |records| records.len()) {
        return false;
    }
    snapshot_records.iter().all(|result_record| {
        let Some(id) = result_record
            .get("provider_instance_id")
            .and_then(PyValue::as_str)
        else {
            return false;
        };
        stored_record(document, id)
            .is_some_and(|stored| provider_timestamp_fields_match(result_record, stored, tracked))
    })
}

fn timestamp_state_transition_matches(
    before: Option<&PyValue>,
    after: Option<&PyValue>,
    target_id: Option<&str>,
    allow_created_update: bool,
    allow_updated: bool,
) -> bool {
    let before_records = before
        .and_then(|value| value.get("providers"))
        .and_then(PyValue::as_object);
    let after_records = after
        .and_then(|value| value.get("providers"))
        .and_then(PyValue::as_object);

    if let Some(before_records) = before_records {
        for (id, before_record) in before_records {
            let is_target = target_id == Some(id.as_str());
            let after_record = after_records
                .and_then(|records| records.iter().find(|(after_id, _)| after_id == id))
                .map(|(_, record)| record);
            let Some(after_record) = after_record else {
                if is_target && !allow_created_update && !allow_updated {
                    continue;
                }
                return false;
            };
            if !(is_target && allow_created_update
                || same_optional_field(Some(before_record), Some(after_record), "created_at"))
            {
                return false;
            }
            if !(is_target && allow_updated
                || same_optional_field(Some(before_record), Some(after_record), "updated_at"))
            {
                return false;
            }
        }
    }

    if let Some(after_records) = after_records {
        for (id, _) in after_records {
            let existed_before = before_records
                .is_some_and(|records| records.iter().any(|(before_id, _)| before_id == id));
            if !existed_before && target_id != Some(id.as_str()) {
                return false;
            }
        }
    }
    true
}

fn stored_record<'a>(document: Option<&'a PyValue>, id: &str) -> Option<&'a PyValue> {
    document?.get("providers")?.get(id)
}

fn store_created_as_python_string(document: Option<&PyValue>, id: &str) -> Option<String> {
    let created = stored_record(document, id)?.get("created_at")?;
    pytruthy(created).then(|| pystr(created))
}

fn store_created_as_json(document: Option<&PyValue>, id: &str) -> Option<String> {
    dumps(stored_record(document, id)?.get("created_at")?).ok()
}

struct ActionReport<'a> {
    case: &'a PreparedCase,
    input_action: &'a PyValue,
    observed_action: &'a PyValue,
    operation: &'a str,
    native: &'a Option<NativeOutcome>,
    expected_kind: &'a str,
    matched: bool,
    issues: &'a [String],
    clock_before: usize,
    clock_after: usize,
    write_delta: usize,
    source_write_changed: bool,
    timestamp_check: bool,
    host_invoke: bool,
    capture_predicate: bool,
}

fn action_report(report: ActionReport<'_>) -> Result<Value> {
    let ActionReport {
        case,
        input_action,
        observed_action,
        operation,
        native,
        expected_kind,
        matched,
        issues,
        clock_before,
        clock_after,
        write_delta,
        source_write_changed,
        timestamp_check,
        host_invoke,
        capture_predicate,
    } = report;
    let native_json = match native {
        Some(Ok(value)) => Some(dumps(value)?),
        _ => None,
    };
    let native_error = match native {
        Some(Err(error)) => Some(json!({
            "exception_class":error.exception_class,
            "message":error.message,
            "args_repr":error.args_repr(),
        })),
        _ => None,
    };
    let expected_reference = observed_action
        .get("outcome")
        .and_then(|outcome| outcome.get("result"))
        .and_then(|result| {
            Some(json!({
                "path":result.get("path").and_then(PyValue::as_str)?,
                "sha256":result.get("sha256").and_then(PyValue::as_str)?,
            }))
        });
    let expected_exception = observed_action.get("outcome").map(|outcome| {
        json!({
            "exception_class":outcome.get("exception_class").and_then(PyValue::as_str),
            "message":outcome.get("message").and_then(PyValue::as_str),
            "args_repr":outcome.get("args_repr").and_then(PyValue::as_str),
        })
    });
    Ok(json!({
        "case_id":case.id,
        "action_id":py_field_str(input_action,"action_id")?,
        "operation":operation,
        "classification":if host_invoke {"captured-operation-helper-only"} else if capture_predicate {"capture-predicate-helper-only"} else {"decoded-memory-state-only"},
        "unverified_scope":if host_invoke {"opaque CapturedHostProviderV4 object and runtime dispatch context"} else if capture_predicate {"non-JSON CapturedHostProviderV4 return and resolved binding identity"} else {"filesystem bytes, locks, permissions, atomic replacement and concurrency"},
        "source_expected_kind":expected_kind,
        "source_expected_result_sidecar":expected_reference,
        "source_expected_exception":expected_exception,
        "native_kind":if native.as_ref().is_some_and(|outcome| outcome.is_ok()) {"result"} else {"exception"},
        "native_result_json":native_json,
        "native_error":native_error,
        "source_match":matched,
        "issues":issues,
        "memory_store_write_delta":write_delta,
        "source_store_bytes_changed":source_write_changed,
        "clock_calls_before":clock_before,
        "clock_calls_after":clock_after,
        "clock_timestamp_after":if clock_after>clock_before {Some(clock_value(clock_after))} else {None::<String>},
        "independent_timestamp_check":timestamp_check,
    }))
}

fn require_field<'a>(value: &'a PyValue, name: &str) -> Result<&'a PyValue> {
    value
        .get(name)
        .with_context(|| format!("payload field {name} missing"))
}

fn py_array<'a>(value: &'a PyValue, field: &str) -> Result<&'a [PyValue]> {
    value
        .get(field)
        .and_then(PyValue::as_array)
        .with_context(|| format!("{field} is not an array"))
}

fn py_field_str<'a>(value: &'a PyValue, field: &str) -> Result<&'a str> {
    value
        .get(field)
        .and_then(PyValue::as_str)
        .with_context(|| format!("{field} is not a string"))
}

fn py_field_str_value(value: &PyValue) -> Result<&str> {
    value.as_str().context("profile_id is not a string")
}

fn py_bool(value: &PyValue) -> Option<bool> {
    if let PyValue::Bool(value) = value {
        Some(*value)
    } else {
        None
    }
}

#[cfg(test)]
mod timestamp_regressions {
    use super::*;

    fn provider(created_at: Option<PyValue>, updated_at: Option<PyValue>) -> PyValue {
        let mut fields = vec![(
            "provider_instance_id".into(),
            PyValue::String("provider-a".into()),
        )];
        if let Some(value) = created_at {
            fields.push(("created_at".into(), value));
        }
        if let Some(value) = updated_at {
            fields.push(("updated_at".into(), value));
        }
        PyValue::object(fields)
    }

    fn generated_timestamps() -> BTreeMap<String, TimestampProvenance> {
        BTreeMap::from([(
            "provider-a".into(),
            TimestampProvenance {
                created_at_generated: true,
                updated_at_generated: true,
            },
        )])
    }

    fn document(record: PyValue) -> PyValue {
        PyValue::object([(
            "providers".into(),
            PyValue::object([("provider-a".into(), record)]),
        )])
    }

    fn snapshot(record: PyValue) -> PyValue {
        PyValue::object([("providers".into(), PyValue::Array(vec![record]))])
    }

    #[test]
    fn generated_clock_values_may_differ_but_returned_timestamps_must_match_store() {
        let source = provider(
            Some(PyValue::String("python-created".into())),
            Some(PyValue::String("python-updated".into())),
        );
        let native = provider(
            Some(PyValue::String("native-created".into())),
            Some(PyValue::String("native-updated".into())),
        );
        let provenance = generated_timestamps();

        assert!(source_value_equal(&native, &source, &provenance));
        assert!(provider_timestamp_fields_match(
            &native,
            &native,
            &provenance
        ));

        let missing = provider(Some(PyValue::String("native-created".into())), None);
        assert!(!source_value_equal(&missing, &source, &provenance));
        assert!(!provider_timestamp_fields_match(
            &missing,
            &native,
            &provenance
        ));

        let altered = provider(
            Some(PyValue::String("native-created".into())),
            Some(PyValue::String("corrupted-return".into())),
        );
        assert!(source_value_equal(&altered, &source, &provenance));
        assert!(!provider_timestamp_fields_match(
            &altered,
            &native,
            &provenance
        ));
    }

    #[test]
    fn snapshot_timestamp_projection_rejects_missing_or_changed_returned_fields() {
        let stored = provider(
            Some(PyValue::String("native-created".into())),
            Some(PyValue::String("native-updated".into())),
        );
        let stored_document = document(stored.clone());
        let provenance = generated_timestamps();
        assert!(snapshot_timestamp_records_match(
            &snapshot(stored.clone()),
            Some(&stored_document),
            &provenance
        ));

        let missing = provider(Some(PyValue::String("native-created".into())), None);
        assert!(!snapshot_timestamp_records_match(
            &snapshot(missing),
            Some(&stored_document),
            &provenance
        ));

        let altered = provider(
            Some(PyValue::String("native-created".into())),
            Some(PyValue::String("corrupted-return".into())),
        );
        assert!(!snapshot_timestamp_records_match(
            &snapshot(altered),
            Some(&stored_document),
            &provenance
        ));
    }

    #[test]
    fn raw_host_snapshot_readers_receive_timestamp_projection_checks() {
        let payload = |operation_id: &str| {
            PyValue::object([
                ("factory".into(), PyValue::String("readonly".into())),
                ("operation_id".into(), PyValue::String(operation_id.into())),
                (
                    "payload".into(),
                    PyValue::object([("profile_id".into(), PyValue::String("defaults".into()))]),
                ),
            ])
        };

        assert!(returns_raw_snapshot("registry.snapshot", &PyValue::Null));
        assert!(returns_raw_snapshot(
            "host.invoke",
            &payload(host::READONLY_GENERATE_OPERATION)
        ));
        assert!(returns_raw_snapshot(
            "host.invoke",
            &payload(host::READONLY_STREAM_OPERATION)
        ));
        assert!(!returns_raw_snapshot(
            "host.invoke",
            &payload(host::READONLY_RESOURCE_OPERATION)
        ));
    }

    #[test]
    fn seeded_timestamps_remain_exact_across_source_and_native_values() {
        let source = provider(
            Some(PyValue::String("seed-created".into())),
            Some(PyValue::String("seed-updated".into())),
        );
        let unchanged = source.clone();
        let changed = provider(
            Some(PyValue::String("seed-created".into())),
            Some(PyValue::String("changed-seed".into())),
        );
        let no_generated_fields = BTreeMap::new();

        assert!(source_value_equal(
            &unchanged,
            &source,
            &no_generated_fields
        ));
        assert!(!source_value_equal(&changed, &source, &no_generated_fields));
    }
}
