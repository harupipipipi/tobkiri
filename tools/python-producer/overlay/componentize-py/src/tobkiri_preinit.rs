//! Original-producer preinit inputs. This source has not been compiler-tested.
//! The filesystem admission gate deliberately remains closed until its typed
//! metadata/enumeration adapter has a proof; readonly mounts alone are not enough.

use anyhow::{Result, bail, ensure};
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};
use wasmtime_wasi::{
    WasiCtxBuilder,
    clocks::{HostMonotonicClock, HostWallClock},
};

const DOMAIN: &[u8] = b"tobkiri/componentize-py/0.25.0/deterministic-preinit/v1\0";
const MAX_PLAN_BYTES: usize = 256 * 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DeclaredPlan {
    format: String,
    producer_recipe_sha256: String,
    stage_receipt_sha256: String,
    audited_stdlib: Vec<String>,
    input_manifest_sha256: String,
    options: GeneratorOptions,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct GeneratorOptions {
    entrypoint: String,
    worlds: Vec<String>,
    features: Vec<String>,
    all_features: bool,
    world_module: Option<String>,
    module_worlds: Vec<(String, Vec<String>)>,
    import_interface_names: std::collections::BTreeMap<String, String>,
    export_interface_names: std::collections::BTreeMap<String, String>,
    full_names: bool,
    intersect_world: Option<String>,
    stub_wasi: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PreinitPlan {
    pub format: String,
    /// Exact canonical UTF-8 JSON of {plan: object, source_date_epoch: string}.
    pub canonical_declaration_hex: String,
    /// Host locator only; never part of the seed or guest environment.
    pub input_manifest_path: PathBuf,
    /// Explicit physical scratch root; no TMPDIR/PATH/home discovery.
    pub scratch_path: PathBuf,
    #[serde(skip)]
    manifest: Vec<crate::captured_inputs::Entry>,
    #[serde(skip)]
    manifest_sha256: String,
}

impl PreinitPlan {
    pub fn read(path: &Path) -> Result<Self> {
        let bytes = crate::captured_inputs::read_bounded(path, MAX_PLAN_BYTES)?;
        ensure!(bytes.len() <= MAX_PLAN_BYTES, "preinit plan too large");
        let mut plan: Self = toml::from_str(std::str::from_utf8(&bytes)?)?;
        let metadata = crate::captured_inputs::read_bounded(
            &plan.input_manifest_path,
            crate::captured_inputs::MAX_METADATA_BYTES,
        )?;
        ensure!(
            metadata.len() <= crate::captured_inputs::MAX_METADATA_BYTES,
            "input manifest too large"
        );
        let value: Value = serde_json::from_slice(&metadata)?;
        validate_value(&value, 0)?;
        ensure!(
            serde_json::to_vec(&value)? == metadata,
            "input manifest must be canonical"
        );
        plan.manifest = serde_json::from_value(value)?;
        plan.manifest_sha256 = Sha256::digest(&metadata)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        plan.declaration()?;
        Ok(plan)
    }

    pub(crate) fn scratch(&self) -> Result<&Path> {
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        {
            let _ = crate::captured_inputs::physical_directory(&self.scratch_path)?;
            Ok(&self.scratch_path)
        }
        #[cfg(not(any(target_os = "macos", target_os = "linux")))]
        bail!("Unavailable: audited producer scratch requires macOS/Linux")
    }

    fn declaration(&self) -> Result<(Vec<u8>, u64)> {
        ensure!(
            self.format == "tobkiri.compiler-init-plan/1",
            "unknown preinit format"
        );
        let bytes = decode_hex(&self.canonical_declaration_hex)?;
        ensure!(bytes.len() <= MAX_PLAN_BYTES, "declaration too large");
        let value: Value = serde_json::from_slice(&bytes)?;
        validate_value(&value, 0)?;
        // Duplicate keys, whitespace, noncanonical escapes, key order and number
        // spellings change the input bytes and therefore fail this equality.
        ensure!(
            serde_json::to_vec(&value)? == bytes,
            "declaration is not canonical"
        );
        let object = value
            .as_object()
            .ok_or_else(|| anyhow::anyhow!("declaration object required"))?;
        ensure!(
            object.len() == 2
                && object.contains_key("plan")
                && object.contains_key("source_date_epoch"),
            "declaration fields"
        );
        ensure!(object["plan"].is_object(), "plan object required");
        let plan: DeclaredPlan = serde_json::from_value(object["plan"].clone())?;
        ensure!(
            plan.format == "tobkiri.original-python-plan/2" && plan.options.stub_wasi,
            "unsupported original-producer declaration"
        );
        for digest in [
            &plan.producer_recipe_sha256,
            &plan.stage_receipt_sha256,
            &plan.input_manifest_sha256,
        ] {
            ensure!(
                digest.len() == 64 && decode_hex(digest)?.len() == 32,
                "declaration digest"
            );
        }
        ensure!(
            self.manifest_sha256 == plan.input_manifest_sha256,
            "input manifest differs from declaration"
        );
        ensure!(
            plan.audited_stdlib
                .iter()
                .all(|module| ["random", "secrets"].contains(&module.as_str()))
                && plan.audited_stdlib.windows(2).all(|pair| pair[0] < pair[1]),
            "audited stdlib declaration"
        );
        let epoch = object["source_date_epoch"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("epoch string required"))?;
        let seconds: u64 = epoch.parse()?;
        ensure!(
            seconds < (1u64 << 63) && seconds.to_string() == epoch,
            "noncanonical epoch"
        );
        Ok((bytes, seconds))
    }

    pub fn configure(&self, wasi: &mut WasiCtxBuilder) -> Result<()> {
        let (declaration, seconds) = self.declaration()?;
        let digest = Sha256::digest(&declaration);
        let mut seed_hash = Sha256::new();
        seed_hash.update(DOMAIN);
        seed_hash.update(digest);
        let seed = seed_hash.finalize();
        let seed_hex = seed
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let value: Value = serde_json::from_slice(&declaration)?;
        let plan: DeclaredPlan = serde_json::from_value(value["plan"].clone())?;
        wasi.secure_random(wasmtime_wasi::random::Deterministic::new(seed.to_vec()))
            .insecure_random(wasmtime_wasi::random::Deterministic::new(seed.to_vec()))
            // Independent insecure-seed interface; it is not the custom RNG's
            // constructor seed (pinned ctx.rs lines 328..337).
            .insecure_random_seed(u128::from_le_bytes(seed[..16].try_into()?))
            .max_random_size(crate::captured_inputs::MAX_READ_BYTES as u64)
            .wall_clock(BuildWallClock(seconds))
            .monotonic_clock(BuildMonotonicClock(AtomicU64::new(0)))
            .allow_tcp(false)
            .allow_udp(false)
            .allow_ip_name_lookup(false)
            .allow_blocking_current_thread(true)
            .env("PYTHONHASHSEED", "0")
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("TZ", "UTC")
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .env("SOURCE_DATE_EPOCH", seconds.to_string())
            .env("TOBKIRI_PREINIT_STDLIB", plan.audited_stdlib.join(","))
            .env("TOBKIRI_PUBLIC_PREINIT_SEED", seed_hex);
        Ok(())
    }

    pub(crate) fn verify_options(&self, generator: &crate::ComponentGenerator<'_>) -> Result<()> {
        let (bytes, _) = self.declaration()?;
        let value: Value = serde_json::from_slice(&bytes)?;
        let plan: DeclaredPlan = serde_json::from_value(value["plan"].clone())?;
        let actual = GeneratorOptions {
            entrypoint: generator.app_name.to_owned(),
            worlds: generator
                .worlds
                .iter()
                .map(|value| (*value).to_owned())
                .collect(),
            features: generator
                .features
                .iter()
                .map(|value| (*value).to_owned())
                .collect(),
            all_features: generator.all_features,
            world_module: generator.world_module.map(str::to_owned),
            module_worlds: generator
                .module_worlds
                .iter()
                .map(|(module, worlds)| {
                    (
                        (*module).to_owned(),
                        worlds.iter().map(|world| (*world).to_owned()).collect(),
                    )
                })
                .collect(),
            import_interface_names: generator
                .import_interface_names
                .iter()
                .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
                .collect(),
            export_interface_names: generator
                .export_interface_names
                .iter()
                .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
                .collect(),
            full_names: generator.full_names,
            intersect_world: generator.intersect_world.map(str::to_owned),
            stub_wasi: generator.stub_wasi,
        };
        ensure!(
            serde_json::to_value(actual)? == serde_json::to_value(plan.options)?,
            "actual producer flags differ from declaration"
        );
        Ok(())
    }

    pub(crate) fn capture_inputs(
        &self,
        mounts: &[crate::captured_inputs::Mount],
    ) -> Result<crate::captured_inputs::CapturedInputs> {
        let (bytes, epoch) = self.declaration()?;
        let _ = bytes;
        crate::captured_inputs::CapturedInputs::capture(&self.manifest, mounts, epoch)
    }

    pub fn require_input_admission(&self, _python: &[&str], _wit: &[&Path]) -> Result<()> {
        self.declaration()?;
        // Fail before CPython or source discovery: this patch must not produce
        // an artifact from host inode/mtime/order or mutable undeclared input.
        bail!(
            "Unavailable: captured WASI source adapter awaits actual original-preinit/deny/perturb/lifetime proof (FS-01)"
        )
    }
}

fn validate_value(value: &Value, depth: usize) -> Result<()> {
    ensure!(depth <= 32, "declaration depth");
    match value {
        Value::Null | Value::Bool(_) | Value::String(_) => (),
        Value::Number(number) => {
            let integer = number
                .as_i64()
                .ok_or_else(|| anyhow::anyhow!("integer required"))?;
            ensure!(
                integer.unsigned_abs() <= 9_007_199_254_740_991,
                "unsafe integer"
            );
        }
        Value::Array(values) => {
            for value in values {
                validate_value(value, depth + 1)?;
            }
        }
        Value::Object(values) => {
            for value in values.values() {
                validate_value(value, depth + 1)?;
            }
        }
    }
    Ok(())
}

fn decode_hex(hex: &str) -> Result<Vec<u8>> {
    ensure!(
        hex.len() % 2 == 0
            && hex
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "lowercase hex required"
    );
    hex.as_bytes()
        .chunks_exact(2)
        .map(|pair| Ok(u8::from_str_radix(std::str::from_utf8(pair)?, 16)?))
        .collect()
}

struct BuildWallClock(u64);
impl HostWallClock for BuildWallClock {
    fn resolution(&self) -> Duration {
        Duration::from_nanos(1000)
    }
    fn now(&self) -> Duration {
        Duration::from_secs(self.0)
    }
}
struct BuildMonotonicClock(AtomicU64);
impl HostMonotonicClock for BuildMonotonicClock {
    fn resolution(&self) -> u64 {
        1000
    }
    fn now(&self) -> u64 {
        self.0
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_add(1000))
            .expect("preinit monotonic clock exhausted")
    }
}
