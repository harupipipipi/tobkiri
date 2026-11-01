//! Strict Profile, publication, Flow and overlay data contracts.
mod compile;
mod wire;
pub use compile::compile;

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use tobkiri_canon::ids::{OpName, PackHash, PackId, ProfileId, ShapeRef};
use tobkiri_canon::{CanonValue, Digest};
pub use tobkiri_gate::LoopLimits;
use tobkiri_gate::PermissionSet;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Profile {
    pub format: String,
    pub id: ProfileId,
    pub name: String,
    pub packs: Vec<PackPin>,
    pub publishers: BTreeMap<String, String>,
    pub ceiling: PermissionSet,
    /// Explicit User declaration; an absent declaration denies every resource.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub user_permissions: Option<PermissionSet>,
    pub pack_limits: BTreeMap<PackId, PermissionSet>,
    pub bindings: BTreeMap<String, PackId>,
    pub overlay_order: Vec<OverlayOrder>,
    pub user_overlays: Vec<OverlayDocument>,
    pub startup: Startup,
}
/// Explicit publication allowlist, separate from mutable Profile state.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublishedProfile {
    pub format: String,
    pub packs: Vec<PackPin>,
    pub publishers: BTreeMap<String, String>,
    pub ceiling: PermissionSet,
    /// Published declaration only; approvals are never part of publication.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub user_permissions: Option<PermissionSet>,
    pub pack_limits: BTreeMap<PackId, PermissionSet>,
    pub bindings: BTreeMap<String, PackId>,
    pub overlay_order: Vec<OverlayOrder>,
    pub user_overlays: Vec<OverlayDocument>,
    pub startup: Startup,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PackPin {
    pub id: PackId,
    pub version: String,
    pub sha256: PackHash,
    pub source: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Startup {
    /// Every physical input entry is selected explicitly; Pack-ID-only entries
    /// are not accepted and operation names carry no special meaning.
    pub inputs: Vec<InputEntry>,
    pub ui: Option<PackId>,
}
/// An exact physical source entry selected by the Profile author.
/// This document is topology, not a runtime permission or source-read receipt.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(deny_unknown_fields)]
pub struct InputEntry {
    pub source_pack: PackId,
    pub exact_input_view: ShapeRef,
    pub operation: OpName,
}

/// A compiler-verified physical entry, bound to one immutable Profile revision.
/// Cloning a descriptor does not mint authority or prove a physical read.
#[derive(Clone, Debug, PartialEq)]
pub struct CompiledInputEntry {
    pub(crate) selection: InputEntry,
    pub(crate) source_hash: PackHash,
    pub(crate) profile_revision: Digest,
    pub(crate) function: tobkiri_manifest::WitFunction,
    pub(crate) arguments: CanonValue,
    pub(crate) flow_routes: Vec<String>,
}
impl CompiledInputEntry {
    pub fn selection(&self) -> &InputEntry {
        &self.selection
    }
    pub fn source_pack(&self) -> &PackId {
        &self.selection.source_pack
    }
    pub fn exact_input_view(&self) -> &ShapeRef {
        &self.selection.exact_input_view
    }
    pub fn operation(&self) -> &OpName {
        &self.selection.operation
    }
    pub fn source_hash(&self) -> &PackHash {
        &self.source_hash
    }
    pub fn profile_revision(&self) -> &Digest {
        &self.profile_revision
    }
    /// Exact signature, including zero result versus empty record or a
    /// payloadless result branch. The common codec retains those meanings.
    pub fn function(&self) -> &tobkiri_manifest::WitFunction {
        &self.function
    }
    /// Values fixed by each resolved explicit empty WIT record; never defaults.
    pub fn arguments(&self) -> &CanonValue {
        &self.arguments
    }
    /// Exact trigger matches in deterministic compiled Flow-key order.
    pub fn flow_routes(&self) -> &[String] {
        &self.flow_routes
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FlowDocument {
    pub format: String,
    pub id: String,
    pub implements: Option<ShapeRef>,
    pub trigger: Trigger,
    pub loop_limits: String,
    pub steps: Vec<Step>,
    #[serde(default)]
    pub on_error: ErrorPolicy,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Trigger {
    pub input: ShapeRef,
}
#[derive(Clone, Debug)]
pub struct Step {
    pub id: Option<String>,
    pub role: Option<ShapeRef>,
    pub overlay: OverlayAccess,
    pub kind: StepKind,
    pub out: Option<String>,
    pub stream: Option<StreamRoute>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StepKind {
    Call(PortCall),
    Flow(SubFlow),
    Branch(Branch),
    Parallel(Parallel),
    Loop(Loop),
    Return(CanonValue),
    Next(Next),
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PortCall {
    pub shape: ShapeRef,
    pub op: OpName,
    pub args: CanonValue,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubFlow {
    pub implements: ShapeRef,
    pub args: CanonValue,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Branch {
    #[serde(rename = "if")]
    pub condition: CanonValue,
    pub then: Vec<Step>,
    #[serde(rename = "else")]
    pub otherwise: Vec<Step>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Parallel {
    pub for_each: CanonValue,
    #[serde(rename = "as")]
    pub binding: String,
    pub max: u32,
    pub body: Vec<Step>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Loop {
    #[serde(rename = "while")]
    pub condition: CanonValue,
    pub body: Vec<Step>,
    pub limits: Option<LoopLimits>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Next {
    pub args: CanonValue,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StreamRoute {
    pub emit: ShapeRef,
    pub collect: Option<String>,
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum OverlayAccess {
    Open,
    #[default]
    Closed,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ErrorPolicy {
    #[serde(default)]
    pub default: ErrorAction,
    #[serde(default)]
    pub steps: BTreeMap<String, ErrorAction>,
}
impl Default for ErrorPolicy {
    fn default() -> Self {
        Self {
            default: ErrorAction::Fail,
            steps: BTreeMap::new(),
        }
    }
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ErrorAction {
    #[default]
    Fail,
    Continue,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OverlayDocument {
    pub format: String,
    pub id: String,
    pub target: OverlayTarget,
    #[serde(default)]
    pub order: OrderConstraints,
    pub body: Vec<Step>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OverlayTarget {
    Role { role: ShapeRef },
    Step { flow: String, step: String },
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrderConstraints {
    #[serde(default)]
    pub before: Vec<String>,
    #[serde(default)]
    pub after: Vec<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OverlayOrder {
    pub target: OverlayTarget,
    pub order: Vec<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConflictReport {
    pub conflicts: Vec<Conflict>,
    pub repair_pack_available: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Conflict {
    pub code: String,
    pub message: String,
    pub packs: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct ResolvedOverlay {
    pub owner: Option<PackId>,
    /// Pack artifact identity, absent for User code; never a User AST digest.
    pub hash: Option<PackHash>,
    pub document: OverlayDocument,
    /// Exact normalized document digest frozen by compilation for every node.
    pub ast_hash: Digest,
}
impl ResolvedOverlay {
    pub fn ast_hash(&self) -> &Digest {
        &self.ast_hash
    }
}
#[derive(Clone, Debug)]
pub struct ResolvedFlow {
    pub owner: PackId,
    pub hash: PackHash,
    pub document: FlowDocument,
    pub ordered_overlays: BTreeMap<String, Vec<ResolvedOverlay>>,
    pub plan_digest: Digest,
    /// Immutable source descriptors; these are never execution permits.
    pub call_witnesses: BTreeMap<CallNodeKey, CompiledCallWitness>,
}
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum CodeLocation {
    Flow,
    PackOverlay { id: String },
    UserOverlay { id: String },
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallNodeKind {
    Call,
    StreamEmit,
}
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallNodeKey {
    pub code: CodeLocation,
    pub node: String,
    pub kind: CallNodeKind,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum CodeOwner {
    Pack { id: PackId, hash: PackHash },
    User,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FrozenTarget {
    pub pack: PackId,
    pub hash: PackHash,
    pub provided: ShapeRef,
}
/// A cloneable description of compiler-verified code, not caller authority.
/// Core must look it up in the frozen revision and mint a fresh activation.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CompiledCallWitness {
    pub(crate) id: Digest,
    pub(crate) ast_hash: Digest,
    pub(crate) owner: CodeOwner,
    pub(crate) profile_revision: Digest,
    pub(crate) flow: String,
    pub(crate) key: CallNodeKey,
    pub(crate) shape: ShapeRef,
    pub(crate) op: OpName,
    pub(crate) targets: Vec<FrozenTarget>,
}
impl CompiledCallWitness {
    pub fn id(&self) -> &Digest {
        &self.id
    }
    pub fn ast_hash(&self) -> &Digest {
        &self.ast_hash
    }
    pub fn owner(&self) -> &CodeOwner {
        &self.owner
    }
    pub fn profile_revision(&self) -> &Digest {
        &self.profile_revision
    }
    pub fn flow(&self) -> &str {
        &self.flow
    }
    pub fn key(&self) -> &CallNodeKey {
        &self.key
    }
    pub fn shape(&self) -> &ShapeRef {
        &self.shape
    }
    pub fn op(&self) -> &OpName {
        &self.op
    }
    pub fn targets(&self) -> &[FrozenTarget] {
        &self.targets
    }
}
#[derive(Clone, Debug)]
pub struct CompiledProfile {
    pub profile: Profile,
    /// Compiler-created descriptors; empty means no physical input is enabled.
    pub native_inputs: Vec<CompiledInputEntry>,
    pub bindings: BTreeMap<ShapeRef, Vec<PackId>>,
    /// Exact hash-verified caller views. Routing aliases never grant authority.
    pub views: BTreeMap<ShapeRef, tobkiri_manifest::ShapeDef>,
    pub flows: BTreeMap<String, ResolvedFlow>,
    pub plan_digest: Digest,
    // Keep source-derived parser metadata charged after the input Pack handles
    // are dropped. This is storage custody, never executable authority.
    pub(crate) _source_catalogs: Vec<tobkiri_manifest::source::SourceCatalog>,
}
impl CompiledProfile {
    pub fn input_entries(&self) -> &[CompiledInputEntry] {
        &self.native_inputs
    }
    pub fn input_entry(
        &self,
        source: &PackId,
        view: &ShapeRef,
        operation: &OpName,
    ) -> Option<&CompiledInputEntry> {
        self.native_inputs.iter().find(|entry| {
            entry.source_pack() == source
                && entry.exact_input_view() == view
                && entry.operation() == operation
                && entry.profile_revision() == &self.plan_digest
        })
    }
    pub fn call_witness(&self, flow: &str, key: &CallNodeKey) -> Option<&CompiledCallWitness> {
        self.flows.get(flow)?.call_witnesses.get(key)
    }
    pub fn view(&self, reference: &ShapeRef) -> Option<&tobkiri_manifest::ShapeDef> {
        self.views.get(reference)
    }
    pub fn view_operation(
        &self,
        reference: &ShapeRef,
        operation: &OpName,
    ) -> Option<&tobkiri_manifest::WitFunction> {
        self.view(reference)?.functions.get(operation)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ProfileError {
    #[error(transparent)]
    Canon(#[from] tobkiri_canon::CanonError),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error("invalid Profile document: {0}")]
    Invalid(String),
}
fn decode<T: serde::de::DeserializeOwned>(bytes: &[u8]) -> Result<T, ProfileError> {
    let value = tobkiri_canon::parse(bytes, tobkiri_canon::Variant::StrictV1)?;
    Ok(serde_json::from_slice(&tobkiri_canon::encode(
        &value,
        tobkiri_canon::Variant::StrictV1,
    )?)?)
}
pub fn parse_profile(bytes: &[u8]) -> Result<Profile, ProfileError> {
    let value: Profile = decode(bytes)?;
    if value.format != "tobkiri.profile/1" || !value.ceiling.loops.validate() {
        return Err(ProfileError::Invalid(
            "format or mandatory default loop bounds".into(),
        ));
    }
    Ok(value)
}
pub fn parse_published(bytes: &[u8]) -> Result<PublishedProfile, ProfileError> {
    let value: PublishedProfile = decode(bytes)?;
    if value.format != "tobkiri.published-profile/1" || !value.ceiling.loops.validate() {
        return Err(ProfileError::Invalid(
            "format or mandatory default loop bounds".into(),
        ));
    }
    Ok(value)
}
pub fn parse_flow(bytes: &[u8]) -> Result<FlowDocument, ProfileError> {
    let value: FlowDocument = decode(bytes)?;
    if value.format != "tobkiri.flow/1"
        || value.loop_limits != "profile-default"
        || value.id.is_empty()
    {
        return Err(ProfileError::Invalid(
            "flow format or loop limit source".into(),
        ));
    }
    Ok(value)
}
pub fn parse_overlay(bytes: &[u8]) -> Result<OverlayDocument, ProfileError> {
    let value: OverlayDocument = decode(bytes)?;
    if value.format != "tobkiri.overlay/1" || value.id.is_empty() {
        return Err(ProfileError::Invalid("overlay format".into()));
    }
    Ok(value)
}
impl Profile {
    /// Publication is an explicit field allowlist; local data/approvals/vaults have no path here.
    pub fn published(&self) -> PublishedProfile {
        PublishedProfile {
            format: "tobkiri.published-profile/1".into(),
            packs: self.packs.clone(),
            publishers: self.publishers.clone(),
            ceiling: self.ceiling.clone(),
            user_permissions: self.user_permissions.clone(),
            pack_limits: self.pack_limits.clone(),
            bindings: self.bindings.clone(),
            overlay_order: self.overlay_order.clone(),
            user_overlays: self.user_overlays.clone(),
            startup: self.startup.clone(),
        }
    }
}
impl PublishedProfile {
    /// Import creates a fresh local identity; authority must be approved again by the core.
    pub fn to_profile(&self, id: ProfileId, name: impl Into<String>) -> Profile {
        Profile {
            format: "tobkiri.profile/1".into(),
            id,
            name: name.into(),
            packs: self.packs.clone(),
            publishers: self.publishers.clone(),
            ceiling: self.ceiling.clone(),
            user_permissions: self.user_permissions.clone(),
            pack_limits: self.pack_limits.clone(),
            bindings: self.bindings.clone(),
            overlay_order: self.overlay_order.clone(),
            user_overlays: self.user_overlays.clone(),
            startup: self.startup.clone(),
        }
    }
}
