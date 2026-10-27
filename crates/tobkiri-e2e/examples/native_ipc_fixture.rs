use anyhow::{Context, Result, bail, ensure};
use async_trait::async_trait;
use std::{
    collections::BTreeMap,
    env, fs,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tobkiri_canon::{
    Digest,
    ids::{PackId, ProfileId, UiSessionId},
};
use tobkiri_core::{
    ApprovalAnswer, ApprovalPrompt, CanonValue, Core, CoreConfig, EffectRequest, HostError, HostIo,
    PermissionSet, TrustedPrompt, UiEmbed,
};
use tobkiri_gate::{LoopLimits, ResourceLimits};
use tobkiri_manifest::{
    FileEntry, PackManifest, Ports, RuntimeSpec, UiDefinition, build_tpk, verify_tpk,
};
use tobkiri_profile::{PackPin, Profile, Startup};

const PROFILE_ID: &str = "prof_native_ipc_probe";
const PROFILE_NAME: &str = "Native IPC probe";
const PACK_ID: &str = "local.native-ipc-probe";
const PACK_VERSION: &str = "1.0.0";
const INDEX_HTML: &str = include_str!("../../../tools/native-ipc-probe/index.html");
const ATTACK_JS: &str = include_str!("../../../tools/native-ipc-probe/attack.js");

#[derive(Default)]
struct FixtureHost;

#[async_trait]
impl TrustedPrompt for FixtureHost {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        match prompt.effect {
            EffectRequest::Permissions { .. } | EffectRequest::UserPermissions { .. } => {
                ApprovalAnswer::Approve
            }
            _ => ApprovalAnswer::Deny,
        }
    }
}

#[async_trait]
impl HostIo for FixtureHost {
    fn display(&self, _surface: &str, _content: &CanonValue) {}

    async fn terminal_read_line(&self, _prompt: &str) -> Result<String, HostError> {
        Err(HostError("fixture host denies terminal input".into()))
    }

    fn terminal_write(&self, _text: &str) {}

    fn embed_ui(&self, _request: UiEmbed) -> Result<UiSessionId, HostError> {
        Err(HostError("fixture host does not embed UI".into()))
    }

    async fn pick_directory(&self, _purpose: &str) -> Option<PathBuf> {
        None
    }
}

fn output_path() -> Result<PathBuf> {
    let mut args = env::args_os().skip(1);
    let Some(flag) = args.next() else {
        bail!("usage: native_ipc_fixture --output PATH");
    };
    ensure!(flag == "--output", "expected exactly: --output PATH");
    let Some(path) = args.next() else {
        bail!("missing PATH after --output");
    };
    ensure!(args.next().is_none(), "expected exactly one --output PATH");
    Ok(PathBuf::from(path))
}

fn ci_temp_root() -> Result<PathBuf> {
    let configured = env::var_os("RUNNER_TEMP")
        .or_else(|| env::var_os("CI_TEMP_DIR"))
        .or_else(|| env::var_os("TMPDIR"));
    let root = configured.map(PathBuf::from).unwrap_or_else(env::temp_dir);
    root.canonicalize()
        .context("resolve CI temporary directory")
}

#[cfg(unix)]
fn create_private_dir(path: &Path) -> Result<()> {
    use std::os::unix::fs::DirBuilderExt;

    let mut builder = fs::DirBuilder::new();
    builder.mode(0o700);
    builder
        .create(path)
        .context("create private fixture directory")
}

#[cfg(not(unix))]
fn create_private_dir(path: &Path) -> Result<()> {
    fs::create_dir(path).context("create private fixture directory")
}

fn create_fixture_root(requested: &Path) -> Result<PathBuf> {
    let name = requested
        .file_name()
        .context("output path must name a new fixture directory")?;
    ensure!(name != "." && name != "..", "invalid output directory name");
    let parent = requested
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
        .canonicalize()
        .context("resolve fixture parent directory")?;
    let temp_root = ci_temp_root()?;
    ensure!(
        parent.starts_with(&temp_root),
        "output directory must be under the CI temporary directory"
    );
    let root = parent.join(name);
    create_private_dir(&root)?;
    Ok(root)
}

fn permission_bounds() -> PermissionSet {
    PermissionSet {
        wasm: ResourceLimits {
            fuel_per_call: 100_000,
            memory_mib: 4,
            wall_ms: 10_000,
            table_elements: 512,
            output_bytes: 64 * 1024,
        },
        loops: LoopLimits {
            max_iterations: 8,
            max_wall_ms: 10_000,
            max_cost_units: 32,
        },
        ..Default::default()
    }
}

fn build_pack(
    pack_dir: &Path,
    permissions: &PermissionSet,
) -> Result<(PathBuf, tobkiri_manifest::VerifiedPack)> {
    let files = BTreeMap::from([
        ("ui/index.html".to_owned(), INDEX_HTML.as_bytes().to_vec()),
        ("ui/attack.js".to_owned(), ATTACK_JS.as_bytes().to_vec()),
    ]);
    let pack_id = PackId::parse(PACK_ID)?;
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: pack_id,
        version: PACK_VERSION.into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes: Vec::new(),
        ports: Ports::default(),
        inputs: Vec::new(),
        flows: Vec::new(),
        overlays: Vec::new(),
        stores: Vec::new(),
        permissions: permissions.clone(),
        ui: Some(UiDefinition {
            entry: "ui/index.html".into(),
            ports_used: Vec::new(),
        }),
        files: files
            .iter()
            .map(|(path, bytes)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(bytes),
                size: bytes.len() as u64,
            })
            .collect(),
    };
    let artifact = build_tpk(&manifest, &files, None)?;
    let verified = verify_tpk(&artifact, &BTreeMap::new())?;
    let artifact_path = pack_dir.join("native-ipc-probe.tpk");
    fs::write(&artifact_path, artifact).context("write unsigned probe Pack")?;
    Ok((artifact_path, verified))
}

fn profile(
    artifact_path: &Path,
    pack: &tobkiri_manifest::VerifiedPack,
    permissions: PermissionSet,
) -> Result<Profile> {
    let pack_id = PackId::parse(PACK_ID)?;
    let source = artifact_path
        .to_str()
        .context("fixture Pack path must be valid UTF-8")?;
    Ok(Profile {
        format: "tobkiri.profile/1".into(),
        id: ProfileId::parse(PROFILE_ID)?,
        name: PROFILE_NAME.into(),
        packs: vec![PackPin {
            id: pack_id.clone(),
            version: PACK_VERSION.into(),
            sha256: pack.hash.clone(),
            source: format!("file:{source}"),
        }],
        publishers: BTreeMap::new(),
        ceiling: permissions.clone(),
        user_permissions: Some(permissions.clone()),
        pack_limits: BTreeMap::from([(pack_id, permissions)]),
        bindings: BTreeMap::new(),
        overlay_order: Vec::new(),
        user_overlays: Vec::new(),
        startup: Startup {
            inputs: Vec::new(),
            ui: Some(PackId::parse(PACK_ID)?),
        },
    })
}

fn private_config(root: PathBuf) -> Result<CoreConfig> {
    #[cfg(debug_assertions)]
    {
        Ok(CoreConfig::for_test_data_root(root))
    }
    #[cfg(not(debug_assertions))]
    {
        let _ = root;
        bail!("native IPC fixture requires a debug build")
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    // Release workspace builds must compile this example, but it may never
    // fall back to a normal user's data root when test overrides are absent.
    ensure!(
        cfg!(debug_assertions),
        "native IPC fixture requires a debug build"
    );
    let requested = output_path()?;
    let root = create_fixture_root(&requested)?;
    let pack_dir = root.join("pack");
    let data_dir = root.join("data");
    create_private_dir(&pack_dir)?;
    create_private_dir(&data_dir)?;

    let permissions = permission_bounds();
    let (artifact_path, verified) = build_pack(&pack_dir, &permissions)?;
    let profile = profile(&artifact_path, &verified, permissions)?;
    let data_root = data_dir.canonicalize()?;
    let core = Core::open(private_config(data_root)?, Arc::new(FixtureHost))?;
    tokio::time::timeout(
        Duration::from_secs(30),
        core.save_profile(profile.clone(), None),
    )
    .await
    .context("fixture Profile save timed out")??;

    println!(
        "{{\"profile_id\":\"{}\",\"pack_sha256\":\"{}\"}}",
        profile.id, verified.hash
    );
    Ok(())
}
