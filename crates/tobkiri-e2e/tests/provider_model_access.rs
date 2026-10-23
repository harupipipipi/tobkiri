//! Independent one-Pack actual Core replay for the pure provider compiler.
//!
//! This is an opt-in source-bound observation harness. It does not build the
//! component, execute Python, regenerate expectations, or claim publication,
//! signature, production activation, or full application parity.
//!
//! Required environment variables (all hashes are lowercase SHA-256 hex):
//! - `TOBKIRI_PROVIDER_MODEL_ACCESS_COMPONENT`
//! - `TOBKIRI_PROVIDER_MODEL_ACCESS_COMPONENT_SHA256`
//! - `TOBKIRI_PROVIDER_MODEL_ACCESS_BUILD_RECEIPT`
//! - `TOBKIRI_PROVIDER_MODEL_ACCESS_BUILD_RECEIPT_SHA256`
//! - `TOBKIRI_PROVIDER_MODEL_ACCESS_ORACLE_RECEIPT`
//! - `TOBKIRI_PROVIDER_MODEL_ACCESS_ORACLE_SHA256`
//! - `TOBKIRI_PROVIDER_MODEL_ACCESS_EVIDENCE_DIR` (must be new)
//!
//! Run only this ignored test after the independent component build and oracle
//! capture are complete:
//! `cargo test -p tobkiri-e2e --test provider_model_access all_finite_source_observations_cross_actual_core -- --ignored --exact`

mod support;

use anyhow::{Context, Result, ensure};
use serde_json::{Map, Value, json};
use sha2::{Digest as _, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, OpenOptions},
    io::Write as _,
    path::{Component as PathComponent, Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use support::{Host, parse_output, physical_temp, repo};
use tobkiri_canon::{
    CanonValue, Digest, Variant,
    ids::{OpName, ShapeRef},
};
use tobkiri_core::{Core, CoreConfig, CoreError, Session};
use tobkiri_gate::{EffectRequest, LoopLimits, Outcome, PermissionSet, ResourceLimits};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
    VerifiedPack,
};
use tobkiri_profile::{PackPin, Profile};

const PREFIX: &str = "TOBKIRI_PROVIDER_MODEL_ACCESS";
const FIXTURE: &str =
    "tests/fixtures/issue1520/provider-model-access/provider-model-access.cases.v1.json";
const PACK_ID: &str = "local.provider-model-access-rust";
const SHAPE: &str = "local.provider-model-access-rust.compiler@1.0";
const INTERFACE: &str = "local:provider-model-access-rust/compiler@1.0.0";
const CORE_WIT: &str = "crates/tobkiri-wit/wit/core/core.wit";
const MAX_RECEIPT: usize = 16 * 1024 * 1024;
const MAX_COMPONENT: usize = 64 * 1024 * 1024;
const MAX_MEMBER: usize = 16 * 1024 * 1024;
const MAX_OUTPUT: usize = 4 * 1024 * 1024;
const OPERATIONS: [&str; 8] = [
    "normalize_model_access",
    "effective_model_access",
    "model_is_allowed",
    "compile_connection_parameters",
    "is_official_openrouter",
    "normalize_native_filters",
    "discovery_url",
    "apply_native_routing",
];

#[derive(Clone, Copy)]
struct Span {
    start: usize,
    end: usize,
}

/// Small JSON range scanner. It preserves object order and the source number
/// spelling without introducing a serde feature or changing the case bytes.
struct Scanner<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl<'a> Scanner<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, at: 0 }
    }

    fn ws(&mut self) {
        while self.bytes.get(self.at).is_some_and(u8::is_ascii_whitespace) {
            self.at += 1;
        }
    }

    fn string(&mut self) -> Result<Span> {
        self.ws();
        ensure!(
            self.bytes.get(self.at) == Some(&b'"'),
            "JSON string expected"
        );
        let start = self.at;
        self.at += 1;
        while let Some(byte) = self.bytes.get(self.at).copied() {
            match byte {
                b'"' => {
                    self.at += 1;
                    return Ok(Span {
                        start,
                        end: self.at,
                    });
                }
                b'\\' => self.at += 2,
                0..=0x1f => anyhow::bail!("unescaped control byte in JSON string"),
                _ => self.at += 1,
            }
            ensure!(self.at <= self.bytes.len(), "truncated JSON escape");
        }
        anyhow::bail!("unterminated JSON string")
    }

    fn value(&mut self) -> Result<Span> {
        self.ws();
        let start = self.at;
        match self.bytes.get(self.at).copied() {
            Some(b'"') => return self.string(),
            Some(b'{') => {
                self.at += 1;
                self.ws();
                if self.bytes.get(self.at) == Some(&b'}') {
                    self.at += 1;
                } else {
                    loop {
                        self.string()?;
                        self.ws();
                        ensure!(
                            self.bytes.get(self.at) == Some(&b':'),
                            "JSON colon expected"
                        );
                        self.at += 1;
                        self.value()?;
                        self.ws();
                        match self.bytes.get(self.at) {
                            Some(b',') => self.at += 1,
                            Some(b'}') => {
                                self.at += 1;
                                break;
                            }
                            _ => anyhow::bail!("JSON object delimiter expected"),
                        }
                    }
                }
            }
            Some(b'[') => {
                self.at += 1;
                self.ws();
                if self.bytes.get(self.at) == Some(&b']') {
                    self.at += 1;
                } else {
                    loop {
                        self.value()?;
                        self.ws();
                        match self.bytes.get(self.at) {
                            Some(b',') => self.at += 1,
                            Some(b']') => {
                                self.at += 1;
                                break;
                            }
                            _ => anyhow::bail!("JSON array delimiter expected"),
                        }
                    }
                }
            }
            Some(b'-' | b'0'..=b'9') => {
                self.at += 1;
                while self.bytes.get(self.at).is_some_and(|b| {
                    b.is_ascii_digit() || matches!(b, b'.' | b'e' | b'E' | b'+' | b'-')
                }) {
                    self.at += 1;
                }
            }
            Some(b't') => self.literal(b"true")?,
            Some(b'f') => self.literal(b"false")?,
            Some(b'n') => self.literal(b"null")?,
            _ => anyhow::bail!("JSON value expected"),
        }
        Ok(Span {
            start,
            end: self.at,
        })
    }

    fn literal(&mut self, literal: &[u8]) -> Result<()> {
        ensure!(
            self.bytes.get(self.at..self.at + literal.len()) == Some(literal),
            "invalid JSON literal"
        );
        self.at += literal.len();
        Ok(())
    }

    fn object_members(&mut self) -> Result<Vec<(String, Span)>> {
        self.ws();
        ensure!(
            self.bytes.get(self.at) == Some(&b'{'),
            "JSON object expected"
        );
        self.at += 1;
        self.ws();
        let mut members = Vec::new();
        if self.bytes.get(self.at) == Some(&b'}') {
            self.at += 1;
            return Ok(members);
        }
        loop {
            let key_span = self.string()?;
            let key: String = serde_json::from_slice(&self.bytes[key_span.start..key_span.end])?;
            self.ws();
            ensure!(
                self.bytes.get(self.at) == Some(&b':'),
                "JSON colon expected"
            );
            self.at += 1;
            let value = self.value()?;
            ensure!(
                !members.iter().any(|(prior, _)| prior == &key),
                "duplicate JSON metadata key"
            );
            members.push((key, value));
            self.ws();
            match self.bytes.get(self.at) {
                Some(b',') => self.at += 1,
                Some(b'}') => {
                    self.at += 1;
                    break;
                }
                _ => anyhow::bail!("JSON object delimiter expected"),
            }
        }
        Ok(members)
    }

    fn array_items(&mut self) -> Result<Vec<Span>> {
        self.ws();
        ensure!(
            self.bytes.get(self.at) == Some(&b'['),
            "JSON array expected"
        );
        self.at += 1;
        self.ws();
        let mut items = Vec::new();
        if self.bytes.get(self.at) == Some(&b']') {
            self.at += 1;
            return Ok(items);
        }
        loop {
            items.push(self.value()?);
            self.ws();
            match self.bytes.get(self.at) {
                Some(b',') => self.at += 1,
                Some(b']') => {
                    self.at += 1;
                    break;
                }
                _ => anyhow::bail!("JSON array delimiter expected"),
            }
        }
        Ok(items)
    }
}

fn object_field(bytes: &[u8], name: &str) -> Result<Span> {
    Scanner::new(bytes)
        .object_members()?
        .into_iter()
        .find_map(|(key, span)| (key == name).then_some(span))
        .with_context(|| format!("JSON field {name} missing"))
}

fn array_spans(bytes: &[u8]) -> Result<Vec<Span>> {
    Scanner::new(bytes).array_items()
}

fn object_string<'a>(value: &'a Value, field: &str) -> Result<&'a str> {
    value[field]
        .as_str()
        .with_context(|| format!("missing string field {field}"))
}

fn digest(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

fn valid_hash(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn no_links(path: &Path) -> Result<()> {
    for ancestor in path.ancestors() {
        ensure!(
            !ancestor.is_symlink(),
            "source/evidence path contains a symlink"
        );
    }
    Ok(())
}

fn relative_bytes(root: &Path, name: &str, maximum: usize) -> Result<Vec<u8>> {
    ensure!(
        !name.is_empty()
            && Path::new(name)
                .components()
                .all(|part| matches!(part, PathComponent::Normal(_))),
        "source path must be a confined relative path"
    );
    no_links(root)?;
    Ok(tobkiri_canon::files::read_relative(
        root,
        name,
        maximum as u64,
    )?)
}

fn absolute_bytes(path: &Path, maximum: usize) -> Result<Vec<u8>> {
    ensure!(path.is_absolute(), "explicit absolute input path required");
    no_links(path)?;
    let parent = path.parent().context("absolute input parent missing")?;
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .context("absolute input filename must be UTF-8")?;
    relative_bytes(parent, name, maximum)
}

fn env_path(name: &str) -> Result<PathBuf> {
    Ok(PathBuf::from(
        std::env::var_os(name).with_context(|| format!("required {name} is absent"))?,
    ))
}

fn env_hash(name: &str, bytes: &[u8]) -> Result<String> {
    let value = std::env::var(name).with_context(|| format!("explicit {name} pin required"))?;
    ensure!(
        valid_hash(&value),
        "invalid lowercase SHA-256 pin in {name}"
    );
    ensure!(digest(bytes) == value, "input bytes differ from {name}");
    Ok(value)
}

fn strict_json(bytes: &[u8]) -> Result<Value> {
    // Receipts include opaque source inputs such as 4,301-digit JSON integers.
    // Keep the corpus out of Core StrictV1 parsing; serde_json's
    // arbitrary_precision feature validates number grammar while this ordered
    // parser independently rejects duplicate object keys.
    let value = serde_json::from_slice(bytes)?;
    let mut parser = ValueParser::new(bytes);
    parser.value()?;
    parser.ws();
    ensure!(parser.at == bytes.len(), "JSON metadata has trailing bytes");
    Ok(value)
}

#[derive(Debug, Clone)]
enum JsonValue {
    Null,
    // Scalar values are compared in the exact ABI bytes; this tree tracks types.
    Bool,
    Number(String),
    String(String),
    Array(Vec<JsonValue>),
    Object(Vec<(String, JsonValue)>),
}

struct ValueParser<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl<'a> ValueParser<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, at: 0 }
    }
    fn ws(&mut self) {
        while self.bytes.get(self.at).is_some_and(u8::is_ascii_whitespace) {
            self.at += 1;
        }
    }
    fn string(&mut self) -> Result<String> {
        let mut scanner = Scanner::new(self.bytes);
        scanner.at = self.at;
        let span = scanner.string()?;
        self.at = scanner.at;
        Ok(serde_json::from_slice(&self.bytes[span.start..span.end])?)
    }
    fn value(&mut self) -> Result<JsonValue> {
        self.ws();
        match self.bytes.get(self.at).copied() {
            Some(b'"') => Ok(JsonValue::String(self.string()?)),
            Some(b'{') => {
                self.at += 1;
                self.ws();
                let mut entries = Vec::new();
                if self.bytes.get(self.at) == Some(&b'}') {
                    self.at += 1;
                    return Ok(JsonValue::Object(entries));
                }
                loop {
                    let key = self.string()?;
                    self.ws();
                    ensure!(
                        self.bytes.get(self.at) == Some(&b':'),
                        "JSON colon expected"
                    );
                    self.at += 1;
                    let value = self.value()?;
                    ensure!(
                        !entries.iter().any(|(prior, _)| prior == &key),
                        "duplicate JSON object key"
                    );
                    entries.push((key, value));
                    self.ws();
                    match self.bytes.get(self.at) {
                        Some(b',') => self.at += 1,
                        Some(b'}') => {
                            self.at += 1;
                            break;
                        }
                        _ => anyhow::bail!("JSON object delimiter expected"),
                    }
                }
                Ok(JsonValue::Object(entries))
            }
            Some(b'[') => {
                self.at += 1;
                self.ws();
                let mut values = Vec::new();
                if self.bytes.get(self.at) == Some(&b']') {
                    self.at += 1;
                    return Ok(JsonValue::Array(values));
                }
                loop {
                    values.push(self.value()?);
                    self.ws();
                    match self.bytes.get(self.at) {
                        Some(b',') => self.at += 1,
                        Some(b']') => {
                            self.at += 1;
                            break;
                        }
                        _ => anyhow::bail!("JSON array delimiter expected"),
                    }
                }
                Ok(JsonValue::Array(values))
            }
            Some(b't') => {
                self.literal(b"true")?;
                Ok(JsonValue::Bool)
            }
            Some(b'f') => {
                self.literal(b"false")?;
                Ok(JsonValue::Bool)
            }
            Some(b'n') => {
                self.literal(b"null")?;
                Ok(JsonValue::Null)
            }
            Some(b'-' | b'0'..=b'9') => {
                let start = self.at;
                self.at += 1;
                while self.bytes.get(self.at).is_some_and(|b| {
                    b.is_ascii_digit() || matches!(b, b'.' | b'e' | b'E' | b'+' | b'-')
                }) {
                    self.at += 1;
                }
                Ok(JsonValue::Number(
                    std::str::from_utf8(&self.bytes[start..self.at])?.to_owned(),
                ))
            }
            _ => anyhow::bail!("JSON value expected"),
        }
    }
    fn literal(&mut self, literal: &[u8]) -> Result<()> {
        ensure!(
            self.bytes.get(self.at..self.at + literal.len()) == Some(literal),
            "invalid JSON literal"
        );
        self.at += literal.len();
        Ok(())
    }
}

fn python_hex(value: f64) -> String {
    let bits = value.to_bits();
    let sign = if bits >> 63 == 0 { "" } else { "-" };
    let exponent = (bits >> 52) & 2047;
    let fraction = bits & ((1u64 << 52) - 1);
    if exponent == 0 && fraction == 0 {
        format!("{sign}0x0.0p+0")
    } else {
        format!(
            "{sign}0x{}.{fraction:013x}p{:+}",
            if exponent == 0 { 0 } else { 1 },
            if exponent == 0 {
                -1022
            } else {
                exponent as i32 - 1023
            }
        )
    }
}

/// Convert Rust's shortest round-trip digits to CPython's repr exponent style.
fn python_float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".into();
    }
    if value.is_infinite() {
        return if value.is_sign_negative() {
            "-inf"
        } else {
            "inf"
        }
        .into();
    }
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        }
        .into();
    }
    let raw = format!("{value:?}");
    let (negative, unsigned) = raw
        .strip_prefix('-')
        .map_or((false, raw.as_str()), |rest| (true, rest));
    let (mantissa, exponent) = unsigned
        .split_once(['e', 'E'])
        .map_or((unsigned, 0i32), |(mantissa, exponent)| {
            (mantissa, exponent.parse::<i32>().unwrap_or(0))
        });
    let decimal = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
    let mut digits: String = mantissa.chars().filter(|ch| *ch != '.').collect();
    let mut decimal_position = decimal + exponent;
    while digits.starts_with('0') && digits.len() > 1 {
        digits.remove(0);
        decimal_position -= 1;
    }
    while digits.ends_with('0') && digits.len() > 1 {
        digits.pop();
    }
    let scientific_exponent = decimal_position - 1;
    let body = if !(-4..16).contains(&scientific_exponent) {
        let rest = &digits[1..];
        let mantissa = if rest.is_empty() {
            digits[..1].to_owned()
        } else {
            format!("{}.{}", &digits[..1], rest)
        };
        let sign = if scientific_exponent < 0 { '-' } else { '+' };
        let magnitude = scientific_exponent.unsigned_abs();
        let exponent = if magnitude < 10 {
            format!("0{magnitude}")
        } else {
            magnitude.to_string()
        };
        format!("{mantissa}e{sign}{exponent}")
    } else if decimal_position <= 0 {
        format!("0.{}{}", "0".repeat((-decimal_position) as usize), digits)
    } else if decimal_position as usize >= digits.len() {
        format!(
            "{}{}.0",
            digits,
            "0".repeat(decimal_position as usize - digits.len())
        )
    } else {
        let split = decimal_position as usize;
        format!("{}.{}", &digits[..split], &digits[split..])
    };
    if negative { format!("-{body}") } else { body }
}

fn float_tree(value: f64) -> Value {
    json!({"type":"float","repr":python_float_repr(value),"hex":python_hex(value),"finite":value.is_finite()})
}

fn marker_float(value: &JsonValue) -> bool {
    matches!(value, JsonValue::Object(entries)
        if entries.len() == 1
            && entries[0].0 == "$python_float"
            && matches!(&entries[0].1, JsonValue::String(name) if name == "nan"))
}

fn contains_nonfinite(value: &JsonValue) -> bool {
    if marker_float(value) {
        return true;
    }
    match value {
        JsonValue::Number(number)
            if number.contains('.') || number.contains('e') || number.contains('E') =>
        {
            number.parse::<f64>().is_ok_and(|value| !value.is_finite())
        }
        JsonValue::Array(values) => values.iter().any(contains_nonfinite),
        JsonValue::Object(entries) => entries.iter().any(|(_, value)| contains_nonfinite(value)),
        _ => false,
    }
}

fn type_tree(value: &JsonValue) -> Value {
    if marker_float(value) {
        return json!({"type":"float","repr":"nan","hex":"nan","finite":false});
    }
    match value {
        JsonValue::Null => json!({"type":"NoneType"}),
        JsonValue::Bool => json!({"type":"bool"}),
        JsonValue::Number(number) => {
            if number.contains('.') || number.contains('e') || number.contains('E') {
                match number.parse::<f64>() {
                    Ok(value) => float_tree(value),
                    Err(_) => {
                        json!({"type":"float","repr":"unparseable","hex":"unparseable","finite":false})
                    }
                }
            } else {
                json!({"type":"int"})
            }
        }
        JsonValue::String(_) => json!({"type":"str"}),
        JsonValue::Array(values) => {
            json!({"type":"list","items":values.iter().map(type_tree).collect::<Vec<_>>()})
        }
        JsonValue::Object(entries) => {
            json!({"type":"dict","entries":entries.iter().map(|(key,value)|json!({"key":key,"value":type_tree(value)})).collect::<Vec<_>>() })
        }
    }
}

fn order_tree(value: &JsonValue) -> Value {
    // The source-only marker is decoded to a Python float by the observer.
    if marker_float(value) {
        return Value::Null;
    }
    match value {
        JsonValue::Object(entries) => {
            let keys = entries.iter().map(|(key, _)| key).collect::<Vec<_>>();
            let mut values = Map::new();
            for (key, value) in entries {
                values.insert(key.clone(), order_tree(value));
            }
            json!({"keys":keys,"values":values})
        }
        JsonValue::Array(values) => json!(values.iter().map(order_tree).collect::<Vec<_>>()),
        _ => Value::Null,
    }
}

fn parse_ordered_json(bytes: &[u8]) -> Result<JsonValue> {
    let mut parser = ValueParser::new(bytes);
    let tree = parser.value()?;
    parser.ws();
    ensure!(parser.at == bytes.len(), "trailing source JSON bytes");
    Ok(tree)
}

fn validate_expected_outcome(row: &Value) -> Result<()> {
    let raw = object_string(row, "outcome_json")?;
    let parsed = strict_json(raw.as_bytes())?;
    ensure!(
        parsed == row["outcome"],
        "source outcome JSON differs from its receipt value"
    );
    let tree = parse_ordered_json(raw.as_bytes())?;
    let JsonValue::Object(entries) = &tree else {
        anyhow::bail!("source outcome JSON is not an object")
    };
    let status = entries
        .iter()
        .find_map(|(key, value)| match (key.as_str(), value) {
            ("status", JsonValue::String(status)) => Some(status.as_str()),
            _ => None,
        })
        .context("source outcome status missing")?;
    match status {
        "ok" => {
            let value = entries
                .iter()
                .find_map(|(key, value)| (key == "value").then_some(value))
                .context("successful source outcome lacks value")?;
            ensure!(
                type_tree(value) == row["outcome_python_type_tree"]
                    && order_tree(value) == row["outcome_mapping_key_order"],
                "source outcome JSON does not match its recorded type/order tree"
            );
        }
        "source-failure" => ensure!(
            row["outcome_python_type_tree"].is_null() && row["outcome_mapping_key_order"].is_null(),
            "source failure unexpectedly has a return-value type/order tree"
        ),
        _ => anyhow::bail!("unknown source outcome status: {status}"),
    }
    Ok(())
}

struct Evidence {
    directory: PathBuf,
}

impl Evidence {
    fn new(path: PathBuf) -> Result<Self> {
        ensure!(
            path.is_absolute(),
            "new absolute evidence directory required"
        );
        ensure!(
            path.components()
                .all(|part| !matches!(part, PathComponent::ParentDir)),
            "evidence directory cannot contain parent traversal"
        );
        no_links(&path)?;
        let parent = path
            .parent()
            .context("evidence parent missing")?
            .canonicalize()?;
        let directory = parent.join(path.file_name().context("evidence name missing")?);
        let mut builder = fs::DirBuilder::new();
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt as _;
            builder.mode(0o700);
        }
        builder
            .create(&directory)
            .context("evidence directory must be new")?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            ensure!(
                fs::metadata(&directory)?.permissions().mode() & 0o777 == 0o700,
                "evidence directory is not private mode 0700"
            );
        }
        Ok(Self { directory })
    }

    fn save(&self, name: &str, bytes: &[u8]) -> Result<Value> {
        ensure!(
            !name.is_empty()
                && Path::new(name)
                    .components()
                    .all(|part| matches!(part, PathComponent::Normal(_))),
            "evidence sidecar path must remain beneath the private directory"
        );
        let path = self.directory.join(name);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
            no_links(parent)?;
        }
        let mut options = OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt as _;
            options.mode(0o600);
        }
        let mut file = options.open(&path)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            ensure!(
                fs::metadata(&path)?.permissions().mode() & 0o777 == 0o600,
                "evidence sidecar is not private mode 0600"
            );
        }
        Ok(json!({"path":name,"bytes":bytes.len(),"sha256":digest(bytes)}))
    }

    fn json(&self, name: &str, value: &Value) -> Result<Value> {
        self.save(name, &serde_json::to_vec_pretty(value)?)
    }
}

#[derive(Clone)]
struct Case {
    id: String,
    operation: String,
    raw_arguments: Vec<u8>,
    row: Value,
}

struct Oracle {
    receipt_bytes: Vec<u8>,
    cases_bytes: Vec<u8>,
    cases: Vec<Case>,
    finite_count: usize,
    nonfinite_count: usize,
    identity: Value,
}

fn load_oracle(repository: &Path) -> Result<Oracle> {
    let path = env_path(&format!("{PREFIX}_ORACLE_RECEIPT"))?;
    let bytes = absolute_bytes(&path, MAX_RECEIPT)?;
    let hash = env_hash(&format!("{PREFIX}_ORACLE_SHA256"), &bytes)?;
    let receipt = strict_json(&bytes)?;
    ensure!(
        receipt["schema"] == "tobkiri.issue1520.provider-model-access-observations.v1"
            && receipt["producer"] == "direct-canonical-python-source-call",
        "direct canonical Python source receipt required"
    );
    let runtime = &receipt["runtime"];
    ensure!(
        runtime["implementation"] == "cpython"
            && runtime["version_info"]["major"] == 3
            && runtime["version_info"]["minor"] == 13
            && runtime["version_info"]["micro"].as_u64().is_some()
            && runtime["unicode_version"] == "15.1.0"
            && runtime["int_max_str_digits"] == 4300
            && runtime["recursion_limit"]
                .as_u64()
                .is_some_and(|limit| limit > 0),
        "oracle must record actual CPython 3.13.x, Unicode 15.1, int limit 4300, and recursion limit"
    );
    let receipt_dir = path.parent().context("oracle receipt directory missing")?;
    let reported_runtime_root = Path::new(object_string(&receipt, "runtime_root")?);
    ensure!(
        reported_runtime_root.is_absolute(),
        "oracle runtime root must be an absolute captured source location"
    );
    no_links(reported_runtime_root)?;
    let case_record = &receipt["cases"];
    let case_name = object_string(case_record, "path")?;
    let case_sidecar = relative_bytes(receipt_dir, case_name, MAX_MEMBER)?;
    ensure!(
        case_record["bytes"].as_u64() == Some(case_sidecar.len() as u64)
            && object_string(case_record, "sha256")? == format!("sha256:{}", digest(&case_sidecar)),
        "oracle case sidecar hash/size changed"
    );
    let tracked_cases = relative_bytes(repository, FIXTURE, MAX_MEMBER)?;
    ensure!(
        tracked_cases == case_sidecar,
        "tracked input-only fixture differs from the oracle's captured case bytes"
    );
    let cases_doc = strict_json(&tracked_cases)?;
    ensure!(
        cases_doc["schema"] == "tobkiri.issue1520.provider-model-access-cases.v1",
        "wrong input-only provider model-access corpus"
    );
    let source_operations = receipt["source_functions"]
        .as_array()
        .context("receipt source function provenance missing")?
        .iter()
        .map(|function| object_string(function, "operation"))
        .collect::<Result<Vec<_>>>()?;
    let fixture_operations = cases_doc["source_functions"]
        .as_array()
        .context("fixture source function list missing")?
        .iter()
        .map(|function| {
            function
                .as_str()
                .context("fixture operation must be a string")
        })
        .collect::<Result<Vec<_>>>()?;
    ensure!(
        source_operations == fixture_operations,
        "receipt source function order differs from its captured input fixture"
    );
    let fixture_cases = cases_doc["cases"]
        .as_array()
        .context("fixture cases missing")?;
    let declared_operations = fixture_operations.iter().copied().collect::<BTreeSet<_>>();
    let candidate_operations = OPERATIONS.iter().copied().collect::<BTreeSet<_>>();
    ensure!(
        declared_operations.len() == OPERATIONS.len()
            && declared_operations == candidate_operations,
        "input oracle must cover the candidate's complete declared operation set"
    );
    let fixture_cases_span = object_field(&tracked_cases, "cases")?;
    let fixture_spans =
        array_spans(&tracked_cases[fixture_cases_span.start..fixture_cases_span.end])?;
    ensure!(
        fixture_cases.len() == fixture_spans.len() && !fixture_cases.is_empty(),
        "case JSON spans/count disagree"
    );

    let source_closure = receipt["source_closure"]
        .as_array()
        .context("original source closure missing")?;
    ensure!(!source_closure.is_empty(), "empty original source closure");
    let mut closure_paths = BTreeSet::new();
    for record in source_closure {
        let source_path = object_string(record, "path")?;
        ensure!(
            closure_paths.insert(source_path),
            "duplicate original source path"
        );
        let current = relative_bytes(&repository.join("tobkiri_runtime"), source_path, MAX_MEMBER)?;
        let reported = relative_bytes(reported_runtime_root, source_path, MAX_MEMBER)?;
        let captured = relative_bytes(&receipt_dir.join("sources"), source_path, MAX_MEMBER)?;
        let expected = object_string(record, "sha256")?;
        ensure!(
            expected == format!("sha256:{}", digest(&current))
                && current == reported
                && current == captured
                && record["bytes"].as_u64() == Some(current.len() as u64),
            "original source closure changed: {source_path}"
        );
    }
    for required in [
        "tobkiri_protocol/provider_compiler/model_access.py",
        "tobkiri_protocol/provider_compiler/native_filters.py",
    ] {
        ensure!(
            closure_paths.contains(required),
            "original source closure lacks {required}"
        );
    }

    let observation_bytes = object_field(&bytes, "observations")?;
    let observation_spans = array_spans(&bytes[observation_bytes.start..observation_bytes.end])?;
    ensure!(
        receipt["observation_count"].as_u64() == Some(observation_spans.len() as u64)
            && observation_spans.len() == fixture_cases.len(),
        "receipt observation count differs from the captured input corpus"
    );
    let mut seen = BTreeSet::new();
    let mut cases = Vec::with_capacity(fixture_cases.len());
    let mut finite_count = 0usize;
    let mut nonfinite_count = 0usize;
    let mut observed_operations = BTreeSet::new();
    for (index, (fixture, span)) in fixture_cases.iter().zip(fixture_spans).enumerate() {
        let raw_case_start = fixture_cases_span.start + span.start;
        let raw_case_end = fixture_cases_span.start + span.end;
        let raw_case = &tracked_cases[raw_case_start..raw_case_end];
        let arg_span = object_field(raw_case, "arguments")?;
        let raw_arguments = raw_case[arg_span.start..arg_span.end].to_vec();
        let mut parser = ValueParser::new(&raw_arguments);
        let input_tree = parser.value()?;
        parser.ws();
        ensure!(
            parser.at == raw_arguments.len(),
            "fixture input has trailing JSON bytes"
        );
        let row_span = observation_spans[index];
        let row_bytes = &bytes
            [observation_bytes.start + row_span.start..observation_bytes.start + row_span.end];
        let row = strict_json(row_bytes)?;
        let id = object_string(fixture, "id")?.to_owned();
        let operation = object_string(fixture, "operation")?.to_owned();
        ensure!(
            row["sequence"].as_u64() == Some(index as u64)
                && row["id"] == id
                && row["operation"] == operation
                && row["arguments_before"] == fixture["arguments"]
                && row["inputs_unchanged"] == true
                && row["outcome_json"].is_string(),
            "receipt row {index} is not bound to the corresponding original input"
        );
        ensure!(seen.insert(id.clone()), "duplicate source observation ID");
        ensure!(
            cases_doc["source_functions"]
                .as_array()
                .is_some_and(|items| items.contains(&json!(operation))),
            "unknown source operation {operation}"
        );
        ensure!(
            OPERATIONS.contains(&operation.as_str()),
            "operation is outside the candidate's declared public surface: {operation}"
        );
        observed_operations.insert(operation.clone());
        let source_tree = type_tree(&input_tree);
        let source_order = order_tree(&input_tree);
        ensure!(
            row["arguments_before_python_type_tree"] == source_tree
                && row["arguments_before_mapping_key_order"] == source_order,
            "receipt argument type/order does not match captured case {id}"
        );
        validate_expected_outcome(&row)?;
        let comparison = object_string(&row, "comparison")?;
        let fixture_comparison = match fixture.get("comparison") {
            Some(value) => value
                .as_str()
                .context("fixture comparison classification is not a string")?,
            None => "native-wasm-finite",
        };
        ensure!(
            comparison == fixture_comparison,
            "receipt classification differs from input fixture metadata for {id}"
        );
        let has_nonfinite_input = contains_nonfinite(&input_tree);
        match comparison {
            "native-wasm-finite" => {
                ensure!(
                    !has_nonfinite_input,
                    "nonfinite source input incorrectly marked Core-comparable: {id}"
                );
                finite_count += 1;
            }
            "source-only-nonfinite" => {
                ensure!(
                    has_nonfinite_input,
                    "source-only-nonfinite case lacks a nonfinite input marker: {id}"
                );
                nonfinite_count += 1;
            }
            _ => anyhow::bail!("unknown source comparison classification: {comparison}"),
        }
        cases.push(Case {
            id,
            operation,
            raw_arguments,
            row,
        });
    }
    ensure!(
        observed_operations.len() == cases_doc["source_functions"].as_array().map_or(0, Vec::len),
        "oracle did not cover the declared source operation set"
    );

    let producer_capture = verify_producer_capture(&receipt, receipt_dir, repository)?;
    let identity = json!({
        "path":path,
        "sha256":hash,
        "bytes":bytes.len(),
        "schema":receipt["schema"],
        "producer":receipt["producer"],
        "runtime_root":receipt["runtime_root"],
        "runtime":runtime,
        "case_fixture":{"repo_path":FIXTURE,"sha256":digest(&tracked_cases),"bytes":tracked_cases.len()},
        "case_count":cases.len(),
        "finite_count":finite_count,
        "source_only_nonfinite_count":nonfinite_count,
        "source_closure_count":source_closure.len(),
        "producer_source_capture":producer_capture,
        "source_execution_is_not_core_evidence":true
    });
    Ok(Oracle {
        receipt_bytes: bytes,
        cases_bytes: tracked_cases,
        cases,
        finite_count,
        nonfinite_count,
        identity,
    })
}

fn verify_producer_capture(
    receipt: &Value,
    receipt_dir: &Path,
    repository: &Path,
) -> Result<Value> {
    let record = receipt
        .get("producer_artifact")
        .context("oracle producer source artifact binding missing")?;
    let name = object_string(record, "name")?;
    let reported_path = Path::new(object_string(record, "path")?);
    ensure!(
        reported_path.is_absolute()
            && reported_path.file_name().and_then(|value| value.to_str()) == Some(name),
        "oracle producer artifact path/name identity differs"
    );
    let captured = relative_bytes(receipt_dir, name, MAX_MEMBER)?;
    let reported_source = absolute_bytes(reported_path, MAX_MEMBER)?;
    let current = relative_bytes(
        repository,
        &format!("tests/fixtures/issue1520/provider-model-access/{name}"),
        MAX_MEMBER,
    )?;
    let expected = object_string(record, "sha256")?;
    ensure!(
        expected == format!("sha256:{}", digest(&captured))
            && reported_source == captured
            && captured == current
            && record["bytes"].as_u64() == Some(captured.len() as u64),
        "oracle producer source capture, reported path, or current file changed"
    );
    Ok(json!({
        "captured_in_receipt":true,
        "reported_path":reported_path.display().to_string(),
        "name":name,
        "sha256":digest(&captured),
        "bytes":captured.len(),
        "reported_absolute_source_matches_capture":true,
        "current_repository_copy_matches":true
    }))
}

struct Component {
    bytes: Vec<u8>,
    hash: String,
    receipt_bytes: Vec<u8>,
    receipt: Value,
    receipt_hash: String,
    source_bindings: Vec<Value>,
    source_identity: Value,
}

fn canonical_metadata(value: &Value) -> Result<Vec<u8>> {
    // serde_json's default map is lexically ordered. The receipt source records
    // contain only ASCII paths, byte counts and hex digests, matching Python's
    // sort_keys=True, separators=(",", ":") encoding.
    Ok(serde_json::to_vec(value)?)
}

fn load_component(repository: &Path) -> Result<Component> {
    let component_path = env_path(&format!("{PREFIX}_COMPONENT"))?;
    let bytes = absolute_bytes(&component_path, MAX_COMPONENT)?;
    let hash = env_hash(&format!("{PREFIX}_COMPONENT_SHA256"), &bytes)?;
    ensure!(
        bytes.starts_with(b"\0asm\x0d\0\x01\0"),
        "actual Wasm component required"
    );
    let receipt_path = env_path(&format!("{PREFIX}_BUILD_RECEIPT"))?;
    let receipt_bytes = absolute_bytes(&receipt_path, MAX_RECEIPT)?;
    let receipt_hash = env_hash(&format!("{PREFIX}_BUILD_RECEIPT_SHA256"), &receipt_bytes)?;
    let receipt = strict_json(&receipt_bytes)?;
    ensure!(
        receipt["format"] == "tobkiri.component-build/2"
            && receipt["artifact_sha256"] == hash
            && receipt["artifact_bytes"].as_u64() == Some(bytes.len() as u64),
        "component build receipt does not bind these exact component bytes"
    );
    let bindings = receipt["source_manifest"]
        .as_array()
        .context("component source manifest missing")?
        .clone();
    ensure!(!bindings.is_empty(), "empty component source manifest");
    let mut paths = BTreeSet::new();
    for binding in &bindings {
        let path = object_string(binding, "path")?;
        ensure!(paths.insert(path), "duplicate component source binding");
        let current = relative_bytes(repository, path, MAX_MEMBER)?;
        ensure!(
            valid_hash(object_string(binding, "sha256")?)
                && object_string(binding, "sha256")? == digest(&current)
                && binding["bytes"].as_u64() == Some(current.len() as u64),
            "component source changed after build: {path}"
        );
    }
    for required in [
        "Cargo.lock",
        "Cargo.toml",
        "packs/Cargo.lock",
        "packs/Cargo.toml",
        "packs/local.provider-model-access-rust/Cargo.toml",
        "packs/local.provider-model-access-rust/src/lib.rs",
        "packs/local.provider-model-access-rust/src/failure.rs",
        "packs/local.provider-model-access-rust/src/model_access.rs",
        "packs/local.provider-model-access-rust/src/native_filters.rs",
        "packs/local.provider-model-access-rust/src/python_url.rs",
        "packs/local.provider-model-access-rust/wit/pack.wit",
        "crates/tobkiri-wit/wit/core/core.wit",
        "packs/ordered-python-compat/Cargo.toml",
        "packs/ordered-python-compat/src/lib.rs",
        "packs/ordered-python-compat/src/codec.rs",
        "packs/ordered-python-compat/src/coercion.rs",
        "packs/ordered-python-compat/src/unicode.rs",
        "packs/ordered-python-compat/src/unicode15_1.json",
        "crates/tobkiri-canon/Cargo.toml",
        "crates/tobkiri-canon/src/codec.rs",
        "crates/tobkiri-canon/src/files.rs",
        "crates/tobkiri-canon/src/ids.rs",
        "crates/tobkiri-canon/src/lib.rs",
        "crates/tobkiri-canon/src/legacy.rs",
        "crates/tobkiri-canon/src/ownership.rs",
        "crates/tobkiri-canon/src/serde_value.rs",
    ] {
        ensure!(
            paths.contains(required),
            "component receipt source manifest lacks {required}"
        );
    }
    let source_hash = digest(&canonical_metadata(&Value::Array(bindings.clone()))?);
    ensure!(
        receipt["source_manifest_sha256"] == source_hash,
        "component source manifest aggregate hash differs"
    );
    let build_identity = &receipt["build"]["source_identity"];
    let dirty_files = build_identity["dirty_files"]
        .as_object()
        .context("component build source_identity.dirty_files must be a path/hash object")?;
    for (path, hash) in dirty_files {
        ensure!(
            !path.is_empty()
                && Path::new(path)
                    .components()
                    .all(|part| matches!(part, PathComponent::Normal(_)))
                && hash.as_str().is_some_and(valid_hash),
            "component build dirty_files entry is invalid: {path}"
        );
    }
    if let Some(untracked) = build_identity.get("untracked_source") {
        for source in untracked
            .as_array()
            .context("component build untracked_source must be an array")?
        {
            let path = object_string(source, "path")?;
            ensure!(
                !path.is_empty()
                    && Path::new(path)
                        .components()
                        .all(|part| matches!(part, PathComponent::Normal(_))),
                "component build untracked source path escapes the repository"
            );
            ensure!(
                valid_hash(object_string(source, "sha256")?),
                "component build untracked source hash is invalid: {path}"
            );
        }
    }
    ensure!(
        receipt["build"]["exit"] == 0
            && receipt["build"]["source_unchanged"] == true
            && valid_hash(
                receipt["build"]["receipt_sha256"]
                    .as_str()
                    .unwrap_or_default()
            )
            && receipt["build"]["command"]
                .as_array()
                .is_some_and(|items| !items.is_empty())
            && build_identity["head"].as_str().is_some_and(|head| {
                (head.len() == 40 || head.len() == 64)
                    && head
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            })
            && valid_hash(build_identity["diff_sha256"].as_str().unwrap_or_default())
            && receipt["compiler"]["rustc_vv"].is_string(),
        "component-build/2 source/build/compiler record is incomplete"
    );
    let source_identity = receipt["build"]["source_identity"].clone();
    Ok(Component {
        bytes,
        hash,
        receipt_bytes,
        receipt: receipt.clone(),
        receipt_hash,
        source_bindings: bindings,
        source_identity,
    })
}

fn current_core_host_sources(repository: &Path) -> Result<Vec<Value>> {
    let mut records = Vec::new();
    for path in [
        "crates/tobkiri-core/src/session.rs",
        "crates/tobkiri-core/src/lib.rs",
        "crates/tobkiri-wasm/src/lib.rs",
        "crates/tobkiri-e2e/tests/support/mod.rs",
        "crates/tobkiri-e2e/tests/provider_model_access.rs",
    ] {
        let bytes = relative_bytes(repository, path, MAX_MEMBER)?;
        records.push(json!({"path":path,"bytes":bytes.len(),"sha256":digest(&bytes)}));
    }
    Ok(records)
}

fn compare_current_core_host_sources(current: &[Value], bindings: &[Value]) -> Vec<Value> {
    current
        .iter()
        .map(|source| {
            let binding = bindings
                .iter()
                .find(|binding| binding["path"] == source["path"]);
            json!({
                "current_source":source,
                "component_build_receipt_source":binding,
                "receipt_includes_current_file":binding.is_some(),
                "receipt_hash_matches_current":binding.is_some_and(|binding|binding["sha256"] == source["sha256"] && binding["bytes"] == source["bytes"])
            })
        })
        .collect()
}

fn wit_closure(repository: &Path) -> Result<(Vec<u8>, Vec<u8>)> {
    let candidate = relative_bytes(
        repository,
        "packs/local.provider-model-access-rust/wit/pack.wit",
        64 * 1024,
    )?;
    let core = relative_bytes(repository, CORE_WIT, 256 * 1024)?;
    // Core definitions remain the separately verified, exact canonical dependency.
    // A Pack shape document must not redeclare the closed Core package inline.
    Ok((candidate, core))
}

fn limits() -> PermissionSet {
    PermissionSet {
        wasm: ResourceLimits {
            fuel_per_call: 2_000_000_000,
            memory_mib: 128,
            wall_ms: 300_000,
            table_elements: 65_536,
            output_bytes: MAX_OUTPUT as u64,
        },
        loops: LoopLimits {
            max_iterations: 16,
            max_wall_ms: 300_000,
            max_cost_units: 100_000,
        },
        ..Default::default()
    }
}

struct Pack {
    verified: VerifiedPack,
    pin: PackPin,
    shape: ShapeRef,
}

fn materialize(repository: &Path, root: &Path, component: &Component) -> Result<Pack> {
    let shape = ShapeRef::parse(SHAPE)?;
    let (wit, core_wit) = wit_closure(repository)?;
    let members = BTreeMap::from([
        ("component.wasm".to_owned(), component.bytes.clone()),
        (
            "component.build.json".to_owned(),
            component.receipt_bytes.clone(),
        ),
        ("wit/pack.wit".to_owned(), wit),
        ("wit/deps/core.wit".to_owned(), core_wit),
        (
            format!("wit/{}.shape.json", shape.name.interface()),
            serde_json::to_vec(
                &json!({"cardinality":"one","streams":{},"callbacks":{},"conformance":null}),
            )?,
        ),
    ]);
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PACK_ID.parse()?,
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            component: "component.wasm".into(),
            toolchain: "rust-wasip2/hash-pinned-test-fixture".into(),
            config: None,
        },
        shapes: vec![ShapeDefinition {
            name: shape.name.clone(),
            version: shape.version,
            kind: ShapeKind::Port,
            wit: "wit/pack.wit".into(),
        }],
        ports: Ports {
            provides: vec![PortDeclaration {
                shape: shape.name.clone(),
                version: shape.version,
                optional: false,
            }],
            ..Default::default()
        },
        inputs: vec![],
        flows: vec![],
        overlays: vec![],
        stores: vec![],
        permissions: limits(),
        ui: None,
        files: members
            .iter()
            .map(|(path, bytes)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(bytes),
                size: bytes.len() as u64,
            })
            .collect(),
    };
    let directory = root.join(PACK_ID);
    fs::create_dir(&directory)?;
    for (path, bytes) in &members {
        let destination = directory.join(path);
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        let mut file = options.open(destination)?;
        file.write_all(bytes)?;
        file.sync_all()?;
    }
    fs::write(
        directory.join("pack.json"),
        tobkiri_manifest::canonical_manifest(&manifest)?,
    )?;
    let verified = tobkiri_manifest::verify_directory(&directory, &BTreeMap::new())?;
    ensure!(
        verified.manifest.id.as_str() == PACK_ID
            && verified.manifest.ports.consumes.is_empty()
            && verified.manifest.permissions.ports.is_empty(),
        "one-Pack fixture requested unexpected dependency ports"
    );
    let pin = PackPin {
        id: verified.manifest.id.clone(),
        version: verified.manifest.version.clone(),
        sha256: verified.hash.clone(),
        source: format!("file:{}", directory.canonicalize()?.display()),
    };
    Ok(Pack {
        verified,
        pin,
        shape,
    })
}

fn profile(pack: &Pack) -> Result<Profile> {
    let ceiling = PermissionSet {
        ports: [SHAPE.to_owned()].into_iter().collect(),
        ..limits()
    };
    let user = PermissionSet {
        ports: ceiling.ports.clone(),
        wasm: ceiling.wasm.clone(),
        loops: ceiling.loops.clone(),
        ..Default::default()
    };
    let permissions = BTreeMap::from([(
        pack.pin.id.clone(),
        pack.verified.manifest.permissions.clone(),
    )]);
    let bindings = BTreeMap::from([(
        format!("{}@{}", pack.shape.name, pack.shape.version.major),
        PACK_ID.to_owned(),
    )]);
    Ok(serde_json::from_value(json!({
        "format":"tobkiri.profile/1",
        "id":"prof_provider_model_access_actual_core",
        "name":"Hash-pinned pure provider model-access actual Core fixture",
        "packs":[pack.pin.clone()],
        "publishers":{},
        "ceiling":ceiling,
        "pack_limits":permissions,
        "user_permissions":user,
        "bindings":bindings,
        "overlay_order":[],
        "user_overlays":[],
        "startup":{"inputs":[],"ui":null}
    }))?)
}

async fn start(data: &Path, profile: &Profile, host: Arc<Host>) -> Result<(Core, Session)> {
    host.utility_ports
        .lock()
        .unwrap()
        .insert((SHAPE.into(), "runtime-info".into()));
    host.utility_ports
        .lock()
        .unwrap()
        .insert((SHAPE.into(), "invoke".into()));
    host.approve_literal_user(profile)?;
    let core = Core::open(CoreConfig::for_test_data_root(data.to_path_buf()), host)?;
    tokio::time::timeout(
        Duration::from_secs(60),
        core.save_profile(profile.clone(), None),
    )
    .await
    .context("saving the isolated one-Pack profile timed out")??;
    let session = tokio::time::timeout(Duration::from_secs(60), core.start(&profile.id))
        .await
        .context("starting the isolated one-Pack Session timed out")??;
    Ok((core, session))
}

fn audit_rows(data: &Path, profile: &Profile) -> Result<Vec<Value>> {
    let relative = tobkiri_store::DataRoot::profile_relative(&profile.id, "audit.sqlite")?;
    let connection = rusqlite::Connection::open_with_flags(
        data.join(relative),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let mut statement = connection.prepare("SELECT body FROM audit ORDER BY seq LIMIT 4097")?;
    let mut query = statement.query([])?;
    let mut rows = Vec::new();
    while let Some(row) = query.next()? {
        ensure!(rows.len() < 4096, "fixture audit exceeded its bound");
        let bytes: Vec<u8> = row.get(0)?;
        ensure!(
            bytes.len() <= 64 * 1024,
            "fixture audit row exceeded its bound"
        );
        rows.push(serde_json::from_slice(&bytes)?);
    }
    Ok(rows)
}

fn user_terminal(rows: &[Value], operation: &str, args_digest: &Digest) -> Result<Value> {
    let effect = EffectRequest::PortCall {
        shape: ShapeRef::parse(SHAPE)?,
        op: OpName::parse(operation)?,
    }
    .digest()?
    .to_string();
    let principal = json!({"kind":"user"});
    let args = args_digest.to_string();
    let dispatches = rows
        .iter()
        .enumerate()
        .filter(|(_, row)| {
            row["kind"] == "dispatch"
                && row["principal"] == principal
                && row["payload"]["effect_digest"] == effect
                && row["payload"]["args_digest"] == args
        })
        .collect::<Vec<_>>();
    if dispatches.len() != 1 {
        return Ok(json!({"matched":false,"dispatch_count":dispatches.len(),"terminal_count":0}));
    }
    let (dispatch_index, dispatch) = dispatches[0];
    let grant = dispatch["payload"]["grant"].clone();
    let terminals = rows
        .iter()
        .enumerate()
        .filter(|(index, row)| {
            *index > dispatch_index
                && row["kind"] == "outcome"
                && row["principal"] == principal
                && row["request"] == dispatch["request"]
                && row["payload"]["grant"] == grant
                && row["payload"]["effect_digest"] == effect
                && row["payload"]["args_digest"] == args
        })
        .collect::<Vec<_>>();
    let succeeded = serde_json::to_value(Outcome::Succeeded)?;
    let succeeded_terminal = terminals
        .first()
        .is_some_and(|(_, row)| row["payload"]["outcome"] == succeeded);
    Ok(json!({
        "matched":grant.is_string() && terminals.len() == 1 && succeeded_terminal,
        "dispatch_count":1,
        "terminal_count":terminals.len(),
        "terminal_outcome_succeeded":succeeded_terminal,
        "dispatch":dispatch,
        "terminal":terminals.first().map(|(_,row)| *row)
    }))
}

fn core_error_kind(error: &CoreError) -> &'static str {
    match error {
        CoreError::Cleanup { .. } => "cleanup",
        CoreError::Invalid(_) => "invalid-configuration",
        CoreError::Conflicts(_) => "profile-conflicts",
        CoreError::NotFound(_) => "not-found",
        CoreError::Gate(_) => "gate",
        CoreError::Step(_) => "step",
        CoreError::Io(_) => "io",
        CoreError::Json(_) => "json",
        CoreError::Canon(_) => "canon",
        CoreError::Store(_) => "store",
        CoreError::Manifest(_) => "manifest",
        CoreError::Profile(_) => "profile",
        CoreError::Runtime(_) => "runtime",
    }
}

fn outer_arguments(operation: Option<&str>, payload: Option<&str>) -> CanonValue {
    let mut fields = BTreeMap::new();
    if let Some(operation) = operation {
        fields.insert("operation".into(), CanonValue::Str(operation.into()));
    }
    if let Some(payload) = payload {
        fields.insert("payload".into(), CanonValue::Str(payload.into()));
    }
    CanonValue::Obj(fields)
}

struct Harness<'a> {
    session: &'a Session,
    data: &'a Path,
    profile: &'a Profile,
    evidence: &'a Evidence,
}

impl Harness<'_> {
    async fn call_with_attempt(
        &self,
        operation: &str,
        args: &CanonValue,
        stem: &str,
        attempted: &mut bool,
    ) -> Result<Value> {
        let before = audit_rows(self.data, self.profile)?.len();
        let shape = ShapeRef::parse(SHAPE)?;
        let op = OpName::parse(operation)?;
        let args_digest = tobkiri_canon::digest(args, Variant::StrictV1).ok();
        let mut record = json!({
            "operation":operation,
            "core_call_attempted":false,
            "guest_execution_proven":false,
            "user_argument_digest_available":args_digest.is_some(),
            "status":"pending"
        });
        *attempted = true;
        record["core_call_attempted"] = json!(true);
        let mut future = std::pin::pin!(self.session.call_user_port(shape, op, args));
        match tokio::time::timeout(Duration::from_secs(45), &mut future).await {
            Ok(Ok(buffer)) => {
                record["core_buffer"] = self
                    .evidence
                    .save(&format!("{stem}.core.json"), buffer.as_bytes())?;
                match parse_output(&buffer) {
                    Ok(CanonValue::Str(raw)) => {
                        record["status"] = json!("result");
                        record["output"] = self
                            .evidence
                            .save(&format!("{stem}.output.json"), raw.as_bytes())?;
                        record["output_utf8"] = json!(raw);
                    }
                    Ok(other) => {
                        record["status"] = json!("non-string-core-value");
                        record["core_value_debug"] = json!(format!("{other:?}"));
                    }
                    Err(error) => {
                        record["status"] = json!("invalid-core-buffer");
                        record["parse_error"] = json!(format!("{error:#}"));
                    }
                }
            }
            Ok(Err(error)) => {
                record["status"] = json!("core-error");
                record["core_error_kind"] = json!(core_error_kind(&error));
                record["core_error"] = json!(error.to_string());
            }
            Err(_) => {
                let close =
                    tokio::time::timeout(Duration::from_secs(5), self.session.close()).await;
                record["status"] = json!("timeout");
                record["same_session_close_succeeded"] = json!(matches!(close, Ok(Ok(()))));
                record["physical_cleanup_established"] = json!(false);
                record["timeout_seconds"] = json!(45);
            }
        }
        let rows = audit_rows(self.data, self.profile)?;
        let audit = rows.get(before..).context("audit log shrank")?.to_vec();
        let terminal = if let Some(digest) = args_digest.as_ref() {
            user_terminal(&audit, operation, digest)?
        } else {
            json!({"matched":false,"reason":"argument digest unavailable"})
        };
        record["user_grant_dispatch_and_terminal"] = terminal.clone();
        record["audit_delta"] = self
            .evidence
            .json(&format!("{stem}.audit.json"), &json!(audit))?;
        record["audit_rows_before"] = json!(before);
        record["audit_rows_after"] = json!(rows.len());
        record["terminal_matched"] = json!(terminal["matched"] == true);
        record["guest_execution_proven"] =
            json!(record["status"] == "result" && record["terminal_matched"] == true);
        Ok(record)
    }
}

fn actual_outcome_trees(raw: &str) -> Result<(Value, Value, Value)> {
    ensure!(
        raw.len() <= MAX_OUTPUT,
        "guest output exceeds evidence limit"
    );
    let mut parser = ValueParser::new(raw.as_bytes());
    let value = parser.value()?;
    parser.ws();
    ensure!(
        parser.at == raw.len(),
        "guest output has trailing JSON bytes"
    );
    let JsonValue::Object(fields) = value else {
        anyhow::bail!("guest output envelope is not an object")
    };
    let field = |key: &str| {
        fields
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value)
    };
    let status = field("status")
        .and_then(|value| match value {
            JsonValue::String(value) => Some(value.as_str()),
            _ => None,
        })
        .context("guest outcome status missing")?;
    let all_tree = type_tree(&JsonValue::Object(fields.clone()));
    match status {
        "ok" => {
            let value = field("value").context("successful outcome lacks value")?;
            Ok((all_tree, type_tree(value), order_tree(value)))
        }
        "source-failure" => Ok((all_tree, Value::Null, Value::Null)),
        _ => anyhow::bail!("unknown provider outcome status: {status}"),
    }
}

async fn runtime_probe(harness: &Harness<'_>) -> Result<Value> {
    let mut attempted = false;
    let observed = harness
        .call_with_attempt(
            "runtime-info",
            &outer_arguments(None, None),
            "runtime-info",
            &mut attempted,
        )
        .await?;
    let raw = observed["output_utf8"]
        .as_str()
        .context("runtime-info did not return a Core string")?;
    tobkiri_canon::parse(raw.as_bytes(), Variant::StrictV1)?;
    let runtime: Value = serde_json::from_str(raw)?;
    let truthful = runtime["implementation"]
        .as_str()
        .is_some_and(|value| value.eq_ignore_ascii_case("rust"))
        && runtime["candidate"] == PACK_ID
        && runtime["source_owner"] == "tobkiri_protocol.provider_compiler"
        && runtime["protocol"] == "ordered-finite-json-v2"
        && runtime["promotion"] == "not-promoted"
        && runtime.get("python").is_none();
    Ok(json!({
        "probe":observed,
        "runtime_info_raw":raw,
        "runtime_info":runtime,
        "truthful_actual_rust_runtime":truthful,
        "actual_core_dispatch_was_observed":observed["terminal_matched"] == true,
        "core_call_attempted":attempted
    }))
}

async fn replay_case(
    harness: &Harness<'_>,
    case: &Case,
    index: usize,
    attempted: &mut bool,
) -> Result<Value> {
    let comparison = object_string(&case.row, "comparison")?;
    let stem = format!("case-{index:04}");
    let expected_bytes = object_string(&case.row, "outcome_json")?.as_bytes();
    let expected_sidecar = harness
        .evidence
        .save(&format!("{stem}.expected.json"), expected_bytes)?;
    let input_sidecar = harness
        .evidence
        .save(&format!("{stem}.input.json"), &case.raw_arguments)?;
    if comparison == "source-only-nonfinite" {
        return Ok(json!({
            "sequence":index,
            "id":case.id,
            "operation":case.operation,
            "classification":comparison,
            "source_input":input_sidecar,
            "expected_outcome":expected_sidecar,
            "original_source_observation":case.row,
            "native_attempted":false,
            "wasm_attempted":false,
            "core_attempted":false,
            "reason":"nonfinite Python input is source-only evidence, excluded from finite ordered-json Core calls",
            "matched_source_receipt_input_and_identity":true,
            "claim":"not a finite Core parity result"
        }));
    }
    let payload = std::str::from_utf8(&case.raw_arguments)?;
    let mut actual = harness
        .call_with_attempt(
            "invoke",
            &outer_arguments(Some(&case.operation), Some(payload)),
            &stem,
            attempted,
        )
        .await?;
    let output = actual["output_utf8"].as_str();
    let exact_abi = output.is_some_and(|value| value.as_bytes() == expected_bytes);
    let mut type_match = false;
    let mut order_match = false;
    let mut actual_type = Value::Null;
    let mut actual_order = Value::Null;
    let mut actual_envelope_type = Value::Null;
    let mut outcome_parse_error = None;
    if let Some(output) = output {
        match actual_outcome_trees(output) {
            Ok((envelope, outcome_type, outcome_order)) => {
                actual_envelope_type = envelope;
                actual_type = outcome_type;
                actual_order = outcome_order;
                type_match = actual_type == case.row["outcome_python_type_tree"];
                order_match = actual_order == case.row["outcome_mapping_key_order"];
            }
            Err(error) => outcome_parse_error = Some(format!("{error:#}")),
        }
    }
    let matched = exact_abi && type_match && order_match && actual["terminal_matched"] == true;
    actual["sequence"] = json!(index);
    actual["id"] = json!(case.id);
    actual["classification"] = json!(comparison);
    actual["source_input"] = input_sidecar;
    actual["expected_outcome"] = expected_sidecar;
    actual["expected_source_observation"] = case.row.clone();
    actual["exact_outcome_json_bytes"] = json!(exact_abi);
    actual["outcome_python_type_tree"] = actual_type.clone();
    actual["outcome_mapping_key_order"] = actual_order.clone();
    actual["actual_envelope_python_type_tree"] = actual_envelope_type;
    actual["exact_outcome_python_type_tree"] = json!(type_match);
    actual["exact_outcome_mapping_key_order"] = json!(order_match);
    actual["outcome_parse_error"] = outcome_parse_error.map_or(Value::Null, Value::String);
    actual["matched"] = json!(matched);
    actual["native_attempted"] = json!(false);
    actual["component_call_result_observed"] = json!(actual["guest_execution_proven"] == true);
    actual["claim"] = json!(
        "observed finite source outcome through actual one-Pack Core; no dependency-import timing or full app parity claim"
    );
    Ok(actual)
}

async fn execute(evidence: &Evidence, report: &mut Value) -> Result<()> {
    let repository = repo();
    let oracle = load_oracle(&repository)?;
    let component = load_component(&repository)?;
    let core_host_sources = current_core_host_sources(&repository)?;
    let oracle_id = evidence.save("original-observations.json", &oracle.receipt_bytes)?;
    let corpus_id = evidence.save("input-only-cases.json", &oracle.cases_bytes)?;
    let component_id = evidence.save("component.wasm", &component.bytes)?;
    let build_receipt_id = evidence.save("component.build.json", &component.receipt_bytes)?;
    report["source_oracle"] = oracle.identity.clone();
    report["source_oracle_receipt_sidecar"] = oracle_id;
    report["input_fixture_sidecar"] = corpus_id;
    report["component"] = json!({
        "path":env_path(&format!("{PREFIX}_COMPONENT"))?,
        "sha256":component.hash,
        "bytes":component.bytes.len(),
        "sidecar":component_id,
        "build_receipt_sha256":component.receipt_hash,
        "build_receipt_bytes":component.receipt_bytes.len(),
        "build_receipt_sidecar":build_receipt_id,
        "build_receipt":component.receipt.clone(),
        "source_bindings":component.source_bindings.clone(),
        "source_manifest_sha256":component.receipt["source_manifest_sha256"],
        "build_source_identity":component.source_identity.clone(),
        "current_core_host_source_hashes":core_host_sources,
        "current_core_host_source_vs_build_receipt_comparison":compare_current_core_host_sources(&core_host_sources,&component.source_bindings),
        "current_core_host_hashes_are_worktree_source_only_not_runtime_binary_provenance":true,
        "source_build_receipt_bound_to_exact_component_bytes":true,
        "signed_publisher_verified":false,
        "production_activation":false
    });
    report["actual_runtime_limits"] = json!({
        "core_strict_v1_outer_limit_bytes":tobkiri_canon::MAX_BYTES,
        "component_input_limit_bytes":MAX_COMPONENT,
        "receipt_input_limit_bytes":MAX_RECEIPT,
        "case_sidecar_limit_bytes":MAX_MEMBER,
        "guest_output_limit_bytes":MAX_OUTPUT,
        "per_call_timeout_seconds":45,
        "profile_resource_limits":limits()
    });
    let temporary = physical_temp()?;
    let fixture_root = temporary.keep();
    report["retained_fixture_directory"] = json!(fixture_root);
    let pack = materialize(&repository, &fixture_root, &component)?;
    let profile = profile(&pack)?;
    report["pack"] = json!({
        "id":PACK_ID,
        "shape":SHAPE,
        "interface":INTERFACE,
        "verified_pack_hash":pack.verified.hash,
        "profile_id":profile.id,
        "packaged_wit_sha256":digest(pack.verified.files["wit/pack.wit"].read().unwrap().as_slice()),
        "component_sha256":component.hash,
        "verified_one_pack_no_consumes":pack.verified.manifest.ports.consumes.is_empty()
    });
    report["profile_sidecar"] = evidence.json("profile.json", &serde_json::to_value(&profile)?)?;
    let data = fixture_root.join("data");
    let host = Arc::new(Host::default());
    let (_core, session) = start(&data, &profile, host.clone()).await?;
    let harness = Harness {
        session: &session,
        data: &data,
        profile: &profile,
        evidence,
    };
    let result = async {
        let runtime = match runtime_probe(&harness).await {
            Ok(value) => value,
            Err(error) => json!({"fixture_error":format!("{error:#}"),"matched":false}),
        };
        let runtime_matched = runtime["truthful_actual_rust_runtime"] == true
            && runtime["probe"]["terminal_matched"] == true;
        report["runtime_info"] = runtime;
        report["runtime_info_matched"] = json!(runtime_matched);
        let mut observations = Vec::with_capacity(oracle.cases.len());
        for (index, case) in oracle.cases.iter().enumerate() {
            let mut attempted = false;
            let mut record = match replay_case(&harness, case, index, &mut attempted).await {
                Ok(record) => record,
                Err(error) => json!({
                    "sequence":index,
                    "id":case.id,
                    "operation":case.operation,
                    "classification":case.row["comparison"],
                    "fixture_error":format!("{error:#}"),
                    "native_attempted":false,
                    "core_call_attempted":attempted,
                    "matched":false
                }),
            };
            if record["classification"] == "native-wasm-finite" {
                record["matched"] = json!(record["matched"] == true && runtime_matched);
                record["runtime_identity_matched"] = json!(runtime_matched);
            }
            if let Err(error) = evidence.json(&format!("case-{index:04}.case.json"), &record) {
                record["evidence_write_error"] = json!(format!("{error:#}"));
                report["evidence_write_failed"] = json!(true);
                observations.push(record);
                break;
            }
            observations.push(record);
        }
        report["observations"] = json!(observations);
        report["finite_attempted_count"] = json!(observations.iter().filter(|row| row["classification"] == "native-wasm-finite" && row["core_call_attempted"] == true).count());
        report["source_only_nonfinite"] = json!(observations.iter().filter(|row| row["classification"] == "source-only-nonfinite").collect::<Vec<_>>());
        report["observation_count"] = json!(observations.len());
        report["expected_observation_count"] = json!(oracle.cases.len());
        report["all_source_rows_retained"] = json!(observations.len() == oracle.cases.len());
        let close = tokio::time::timeout(Duration::from_secs(30), session.close()).await;
        report["session_close_awaited"] = json!(true);
        report["session_close_succeeded"] = json!(matches!(close, Ok(Ok(()))));
        report["session_close_timed_out"] = json!(close.is_err());
        let rows = audit_rows(&data, &profile)?;
        report["session_audit"] = evidence.json("session.audit.json", &json!(rows))?;
        report["reviewed_pack_permission_approvals"] = json!(*host.approvals.lock().unwrap());
        report["scope"] = json!({
            "native_replay_performed":false,
            "python_reexecuted":false,
            "source_expectations_regenerated":false,
            "dependency_import_order_proven":false,
            "full_gateway_or_provider_application_parity":false,
            "authority_or_secret_or_network_behavior_tested":false,
            "production_activation":false,
            "claim":"actual one-Pack Core transport, ordered finite input/output observation, source-failure envelope, and user grant dispatch/terminal only"
        });
        Ok::<(), anyhow::Error>(())
    }
    .await;
    if let Err(error) = result {
        report["harness_error"] = json!(format!("{error:#}"));
        report["observation_count"] = json!(report["observations"].as_array().map_or(0, Vec::len));
        report["all_source_rows_retained"] = json!(false);
        return Err(error);
    }
    let observations = report["observations"]
        .as_array()
        .context("actual observations missing")?;
    let finite_attempted = observations
        .iter()
        .filter(|row| {
            row["classification"] == "native-wasm-finite" && row["core_call_attempted"] == true
        })
        .count();
    let nonfinite_count = observations
        .iter()
        .filter(|row| row["classification"] == "source-only-nonfinite")
        .count();
    let source_only_nonfinite_excluded = nonfinite_count == oracle.nonfinite_count
        && observations
            .iter()
            .filter(|row| row["classification"] == "source-only-nonfinite")
            .all(|row| {
                row["native_attempted"] == false
                    && row["wasm_attempted"] == false
                    && row["core_attempted"] == false
            });
    let mismatches = observations
        .iter()
        .filter(|row| row["classification"] == "native-wasm-finite" && row["matched"] != true)
        .count();
    let all_observations_retained = observations.len() == oracle.cases.len();
    report["source_only_nonfinite_excluded_from_runtime"] = json!(source_only_nonfinite_excluded);
    report["summary"] = json!({
        "schema":"tobkiri.provider-model-access-rust.actual-core-observations.v1",
        "finite_source_cases":oracle.finite_count,
        "finite_core_attempts":finite_attempted,
        "source_only_nonfinite_cases":oracle.nonfinite_count,
        "observed_source_only_nonfinite":nonfinite_count,
        "mismatch_count":mismatches,
        "all_observations_retained":all_observations_retained,
        "evidence_write_failed":report["evidence_write_failed"] == true,
        "runtime_info_matched":report["runtime_info_matched"],
        "session_close_succeeded":report["session_close_succeeded"],
        "actual_core_parity_passed":finite_attempted == oracle.finite_count
            && mismatches == 0
            && all_observations_retained
            && source_only_nonfinite_excluded
            && report["evidence_write_failed"] != true
            && report["runtime_info_matched"] == true
            && report["session_close_succeeded"] == true
    });
    Ok(())
}

#[tokio::test]
#[ignore = "requires an independently pinned Rust component, direct-source observation receipt, and NEW private evidence directory"]
async fn all_finite_source_observations_cross_actual_core() {
    let evidence_path =
        env_path(&format!("{PREFIX}_EVIDENCE_DIR")).unwrap_or_else(|error| panic!("{error:#}"));
    let evidence = Evidence::new(evidence_path).unwrap_or_else(|error| panic!("{error:#}"));
    let mut report = json!({
        "schema":"tobkiri.provider-model-access-rust.actual-core-observations.v1",
        "status":"running",
        "source_only_oracle":true,
        "build_performed":false,
        "source_python_executed":false,
        "expectations_generated":false,
        "evidence_directory":evidence.directory,
        "observations":[]
    });
    let result = execute(&evidence, &mut report).await;
    match result {
        Ok(()) => {
            let observations = report["observations"]
                .as_array()
                .cloned()
                .unwrap_or_default();
            let expected_count =
                report["expected_observation_count"].as_u64().unwrap_or(0) as usize;
            let all_retained =
                observations.len() == expected_count && report["all_source_rows_retained"] == true;
            let finite_cases_matched = observations
                .iter()
                .filter(|row| row["classification"] == "native-wasm-finite")
                .all(|row| row["matched"] == true);
            let all_matched = finite_cases_matched
                && report["summary"]["actual_core_parity_passed"] == true
                && report["source_only_nonfinite_excluded_from_runtime"] == true
                && report["runtime_info_matched"] == true
                && report["session_close_succeeded"] == true;
            report["status"] = json!(if all_retained && all_matched {
                "passed"
            } else {
                "failed"
            });
            report["all_finite_cases_matched"] = json!(finite_cases_matched);
            report["all_replay_acceptance_conditions_met"] = json!(all_matched);
            report["all_cases_attempted_and_retained"] = json!(all_retained);
            report["summary_sidecar"] = evidence
                .json("summary.json", &report)
                .unwrap_or_else(|error| panic!("could not preserve actual summary: {error:#}"));
            assert!(
                all_retained && all_matched,
                "provider model-access actual Core mismatches retained in {}",
                evidence.directory.display()
            );
        }
        Err(error) => {
            report["status"] = json!("failed");
            report["harness_error"] = json!(format!("{error:#}"));
            report["summary_sidecar"] =
                evidence
                    .json("summary.json", &report)
                    .unwrap_or_else(|write_error| {
                        panic!("{}; could not preserve summary: {write_error:#}", error)
                    });
            panic!(
                "provider model-access actual Core harness failed; partial evidence retained in {}: {error:#}",
                evidence.directory.display()
            );
        }
    }
}
