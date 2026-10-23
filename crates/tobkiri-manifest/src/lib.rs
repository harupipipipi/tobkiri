//! Pack identity, integrity and authoritative WIT shape documents.
mod files;
mod package;
mod shapes;
pub mod source;
pub use files::ArtifactFiles;
mod wit_preflight;
pub use package::{
    build_tpk, canonical_manifest, parse_manifest, sign_manifest, verify_directory,
    verify_directory_in_catalog, verify_tpk, verify_tpk_file, verify_tpk_file_in_catalog,
};
pub use shapes::{
    PackCoreErrorBindings, PackWitInspection, WitBindingDirection, allowed_imports,
    allowed_imports_for_pack, compatibility, core_error_result_operations, encode_f32, encode_f64,
    inspect_pack_wit, json_schema, load_shapes, parse_f32, parse_f64, project_json, validate_json,
    wit_interface_name,
};

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::collections::BTreeSet;
use tobkiri_canon::Digest;
pub use tobkiri_canon::ids::{OpName, PackHash, PackId, ShapeName, ShapeRef, ShapeVersion};
use tobkiri_gate::PermissionSet;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PackManifest {
    pub format: String,
    pub id: PackId,
    pub version: String,
    pub publisher_key: Option<String>,
    pub runtime: RuntimeSpec,
    pub shapes: Vec<ShapeDefinition>,
    pub ports: Ports,
    /// Formal physical source entries. Ordinary provided ports are not Inputs.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub inputs: Vec<InputDeclaration>,
    pub flows: Vec<String>,
    pub overlays: Vec<String>,
    pub stores: Vec<StoreDefinition>,
    pub permissions: PermissionSet,
    pub ui: Option<UiDefinition>,
    pub files: Vec<FileEntry>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase", deny_unknown_fields)]
pub enum RuntimeSpec {
    Wasm {
        component: String,
        toolchain: String,
        #[serde(default)]
        config: Option<RuntimeConfig>,
    },
    Native {
        executable: String,
        #[serde(default)]
        args: Vec<String>,
        #[serde(default)]
        config: Option<RuntimeConfig>,
    },
    Data,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeConfig {
    pub path: String,
    pub shape: Option<ShapeRef>,
}
impl RuntimeSpec {
    pub fn config(&self) -> Option<&RuntimeConfig> {
        match self {
            Self::Wasm { config, .. } | Self::Native { config, .. } => config.as_ref(),
            Self::Data => None,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShapeDefinition {
    pub name: ShapeName,
    pub version: ShapeVersion,
    pub kind: ShapeKind,
    pub wit: String,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ShapeKind {
    Port,
    Data,
    Flow,
    Role,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Ports {
    pub provides: Vec<PortDeclaration>,
    pub consumes: Vec<PortDeclaration>,
    pub untyped: Vec<UntypedPort>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PortDeclaration {
    pub shape: ShapeName,
    pub version: ShapeVersion,
    #[serde(default)]
    pub optional: bool,
}
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InputDeclaration {
    pub exact_input_view: ShapeRef,
    pub operation: OpName,
}

/// Resolve a formal Input using only an exact authoritative caller view.
/// A provider alias or another operation in the same interface is insufficient.
pub fn input_function<'a>(
    manifest: &PackManifest,
    declaration: &InputDeclaration,
    view: &'a ShapeDef,
) -> Result<&'a WitFunction, ManifestError> {
    if !manifest.inputs.contains(declaration)
        || !manifest.ports.provides.iter().any(|port| {
            port.shape == declaration.exact_input_view.name
                && port.version == declaration.exact_input_view.version
        })
        || view.reference != declaration.exact_input_view
        || view.kind != ShapeKind::Port
    {
        return Err(ManifestError::Invalid(
            "Input requires a formal declaration and exact provided Port view".into(),
        ));
    }
    view.functions.get(&declaration.operation).ok_or_else(|| {
        ManifestError::Invalid("Input operation is absent from its exact WIT view".into())
    })
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UntypedPort {
    pub name: String,
    pub direction: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StoreDefinition {
    pub name: String,
    pub tier: StoreTier,
    pub data_shape: ShapeRef,
    pub seed: Option<String>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum StoreTier {
    Private,
    Public,
    ReadOnly,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UiDefinition {
    pub entry: String,
    #[serde(default)]
    pub ports_used: Vec<ShapeRef>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileEntry {
    pub path: String,
    pub sha256: Digest,
    pub size: u64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PackSignature {
    pub algorithm: String,
    pub key: String,
    pub signature: String,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Cardinality {
    One,
    Many,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShapeSidecar {
    pub cardinality: Cardinality,
    pub streams: BTreeMap<String, String>,
    pub callbacks: BTreeMap<String, String>,
    pub conformance: Option<String>,
}

/// Verified bytes and authoritative shapes of one independently admitted Pack.
#[derive(Clone, Debug)]
pub struct VerifiedPack(
    tobkiri_canon::ownership::LeasedShared<VerifiedPackData, source::SourceCatalog>,
);
/// Native verifier output. A shared VerifiedPack retains its prepaid metadata;
/// cloning it never copies the source catalog, manifest or shape graph.
#[derive(Debug)]
pub struct VerifiedPackData {
    pub manifest: PackManifest,
    pub hash: PackHash,
    pub files: ArtifactFiles,
    pub shapes: Vec<ShapeDef>,
}
impl VerifiedPack {
    /// Trusted native verifier construction, not a guest admission endpoint.
    /// The verifier must prepay the owned manifest and shape metadata first.
    pub fn try_new_precharged(data: VerifiedPackData) -> Result<Self, ManifestError> {
        let catalog = data.files.catalog().clone();
        catalog.prepay_metadata(tobkiri_canon::ownership::LeasedShared::<
            VerifiedPackData,
            source::SourceCatalog,
        >::storage_bytes() as u64)?;
        Ok(Self(
            tobkiri_canon::ownership::LeasedShared::try_new_precharged(data, catalog)
                .map_err(|error| ManifestError::Invalid(error.to_string()))?,
        ))
    }
    /// Exactly immutable verified defaults; never a host or Profile config map.
    pub fn init_config(&self) -> Result<Vec<u8>, ManifestError> {
        if let Some(config) = self.manifest.runtime.config() {
            let raw = self
                .files
                .get(&config.path)
                .ok_or_else(|| ManifestError::Invalid("config file missing".into()))?
                .read()?;
            let value = tobkiri_canon::parse(&raw, tobkiri_canon::Variant::StrictV1)?;
            Ok(tobkiri_canon::encode(
                &value,
                tobkiri_canon::Variant::StrictV1,
            )?)
        } else {
            Ok(b"{}".to_vec())
        }
    }
}
impl std::ops::Deref for VerifiedPack {
    type Target = VerifiedPackData;
    fn deref(&self) -> &Self::Target {
        self.0.get()
    }
}
/// Sidecar stream/callback handles are never inferred from record layout.
#[derive(Clone, Debug, PartialEq)]
pub struct ShapeDef {
    pub reference: ShapeRef,
    pub kind: ShapeKind,
    pub cardinality: Cardinality,
    pub functions: BTreeMap<OpName, WitFunction>,
    pub types: BTreeMap<String, WitType>,
    pub streams: BTreeMap<String, String>,
    pub callbacks: BTreeMap<String, String>,
    pub wit: String,
}
/// Exact policy operands resolved independently of provider routing.
pub type PermissionViews = BTreeMap<ShapeRef, BTreeSet<OpName>>;
/// Resolve original literal policy refs to their frozen caller operations.
/// Empty structural Role/Flow interfaces remain representable; being present
/// in this table alone conveys no permission and grants no missing operation.
pub fn resolve_permission_views(
    references: &BTreeSet<String>,
    views: &BTreeMap<ShapeRef, ShapeDef>,
) -> Result<PermissionViews, ManifestError> {
    references
        .iter()
        .map(|literal| {
            let reference = ShapeRef::parse(literal)?;
            let view = views.get(&reference).ok_or_else(|| {
                ManifestError::Invalid(format!(
                    "policy reference has no exact verified view: {literal}"
                ))
            })?;
            Ok((reference, view.functions.keys().cloned().collect()))
        })
        .collect()
}
#[derive(Clone, Debug, PartialEq)]
pub struct WitFunction {
    pub params: Vec<(String, WitType)>,
    pub result: Option<WitType>,
}
#[derive(Clone, Debug, PartialEq)]
pub enum WitType {
    Bool,
    U8,
    U16,
    U32,
    U64,
    S8,
    S16,
    S32,
    S64,
    F32,
    F64,
    Char,
    String,
    List(Box<WitType>),
    Tuple(Vec<WitType>),
    Record(Vec<WitField>),
    Variant(Vec<WitCase>),
    Enum(Vec<String>),
    Flags(Vec<String>),
    Option(Box<WitType>),
    Result {
        ok: Option<Box<WitType>>,
        err: Option<Box<WitType>>,
    },
    StreamHandle,
    CallbackHandle,
    Json,
}
#[derive(Clone, Debug, PartialEq)]
pub struct WitField {
    pub name: String,
    pub ty: WitType,
}
#[derive(Clone, Debug, PartialEq)]
pub struct WitCase {
    pub name: String,
    pub ty: Option<WitType>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Compat {
    Exact,
    MinorCompatible,
    NeedsAdapter,
}

#[derive(Debug, thiserror::Error)]
pub enum ManifestError {
    #[error("invalid Pack: {0}")]
    Invalid(String),
    #[error(transparent)]
    Canon(#[from] tobkiri_canon::CanonError),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}
