//! Select legacy Registry routes from authenticated v5 records.
use crate::OriginalActiveProfile;
use sha2::{Digest as _, Sha256};
use std::{fmt, sync::Arc};
use tobkiri_canon::{
    AllocationBudget, AllocationKind, AllocationRequest, CoreRef, CoreValue, Digest,
};

const PIN_FIELDS: [&str; 10] = [
    "pack_id",
    "variant_id",
    "artifact_digest",
    "executable_catalog_digest",
    "platform",
    "architecture",
    "runtime_abi",
    "backend",
    "execution_kind",
    "domain_kind",
];

const PLATFORM: &str = "any";
const ARCHITECTURE: &str = "any";
const RUNTIME_ABI: &str = "python3.13";
const BACKEND: &str = "tobkiri.python-host-v4";
const EXECUTION_KIND: &str = "host_extension";
const DOMAIN_KIND: &str = "dedicated_process";

const LEGACY_EXECUTION: [(&str, &str); 6] = [
    ("platform", PLATFORM),
    ("architecture", ARCHITECTURE),
    ("runtime_abi", RUNTIME_ABI),
    ("backend", BACKEND),
    ("execution_kind", EXECUTION_KIND),
    ("domain_kind", DOMAIN_KIND),
];

struct Route {
    pack: &'static str,
    function: &'static str,
    contract: &'static str,
    operation: &'static str,
    variant: &'static str,
}

static COORDINATOR: Route = Route {
    pack: "rumi_host_authority_bridge_pack",
    function: "rumi_host_authority_bridge_pack.host-authority.interactive-effect",
    contract: "tobkiri.service.interactive-effect.v1",
    operation: "interactive_effect.manage",
    variant: "rumi_host_authority_bridge_pack.host-authority.interactive-effect.python",
};

static PROVIDER_PREPARE: Route = Route {
    pack: "rumi_provider_registry_pack",
    function: "rumi_provider_registry_pack.provider-configure.prepare",
    contract: "tobkiri.action.ai.provider.registry.manage.v1",
    operation: "rumi_provider_registry_pack.provider-configure-prepare",
    variant: "rumi_provider_registry_pack.provider-configure.prepare.python",
};

static MODEL_LIST: Route = Route {
    pack: "rumi_model_registry_pack",
    function: "rumi_model_registry_pack.model-registry.profile",
    contract: "tobkiri.resource.ai.model.profile.v1",
    operation: "rumi_model_registry_pack.model-profile-resource",
    variant: "rumi_model_registry_pack.model-registry.profile.python",
};

static MODEL_RESOLVE_GENERATE: Route = Route {
    operation: "rumi_model_registry_pack.model-profile-resource.generate",
    ..MODEL_LIST
};

static MODEL_RESOLVE_STREAM: Route = Route {
    operation: "rumi_model_registry_pack.model-profile-resource.stream",
    ..MODEL_LIST
};

static MODEL_MANAGE: Route = Route {
    pack: "rumi_model_registry_pack",
    function: "rumi_model_registry_pack.model-registry.manage",
    contract: "tobkiri.action.ai.model.profile.manage.v1",
    operation: "rumi_model_registry_pack.model-profile-manage",
    variant: "rumi_model_registry_pack.model-registry.manage.python",
};

static PROVIDER_READ: Route = Route {
    pack: "rumi_provider_registry_pack",
    function: "rumi_provider_registry_pack.provider-registry.resource",
    contract: "tobkiri.resource.ai.provider.registry.v1",
    operation: "rumi_provider_registry_pack.provider-registry-resource",
    variant: "rumi_provider_registry_pack.provider-registry.resource.python",
};

static CONVERSATION_READ: Route = Route {
    pack: "rumi_conversation_store_pack",
    function: "rumi_conversation_store_pack.conversation-store.resource",
    contract: "tobkiri.resource.conversation.v1",
    operation: "rumi_conversation_store_pack.conversation-resource",
    variant: "rumi_conversation_store_pack.conversation-store.resource.python",
};

const SAVED_TURN_CALLER: &str = "rumi_turn_runtime_pack.turn-runtime.saved";
const SAVED_ORCHESTRATION_CALLER: &str = "tobkiri_conversation_orchestration_pack.saved";
const GATEWAY_QUOTE_CALLER: &str = "rumi_ai_gateway_pack.ai-gateway.route-quote";
const GATEWAY_GENERATE_CALLER: &str = "rumi_ai_gateway_pack.ai-gateway.generate";
const GATEWAY_STREAM_CALLER: &str = "rumi_ai_gateway_pack.ai-gateway.stream";

/// The authenticated route is inconsistent or its verification budget is unavailable.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalRouteSelectionError {
    /// An exact route disagrees across its current Profile, Plan, or Lock.
    Inconsistent(&'static str),
    /// The current owner could not fund the record checks.
    Capacity,
}

impl fmt::Display for OriginalRouteSelectionError {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Inconsistent(reason) => {
                write!(output, "old settings route selection: {reason}")
            }
            Self::Capacity => output.write_str("old settings route selection exceeded its budget"),
        }
    }
}
impl std::error::Error for OriginalRouteSelectionError {}

use OriginalRouteSelectionError::{Capacity, Inconsistent};
type Result<T> = std::result::Result<T, OriginalRouteSelectionError>;

/// Pins for a selected function, backed by the original authenticated record storage.
pub struct OriginalRoutePin {
    route: &'static Route,
    caller: CoreValue,
    artifact_digest: CoreValue,
    executable_catalog_digest: CoreValue,
    contract_revision_digest: CoreValue,
    function_implementation_digest: CoreValue,
    authority_reference: CoreValue,
    requested_scope_digest: Digest,
}

impl OriginalRoutePin {
    /// Selected old Pack identity.
    pub fn pack_id(&self) -> &'static str {
        self.route.pack
    }

    /// Digest of the selected old Pack artifact.
    pub fn artifact_digest(&self) -> &str {
        verified_text(&self.artifact_digest)
    }

    /// Digest of the selected old executable catalog.
    pub fn executable_catalog_digest(&self) -> &str {
        verified_text(&self.executable_catalog_digest)
    }

    /// Selected legacy executable variant.
    pub fn variant_id(&self) -> &'static str {
        self.route.variant
    }

    /// Selected platform pin.
    pub fn platform(&self) -> &'static str {
        PLATFORM
    }

    /// Selected architecture pin.
    pub fn architecture(&self) -> &'static str {
        ARCHITECTURE
    }

    /// Selected runtime ABI pin.
    pub fn runtime_abi(&self) -> &'static str {
        RUNTIME_ABI
    }

    /// Selected runtime backend pin.
    pub fn backend(&self) -> &'static str {
        BACKEND
    }

    /// Selected execution kind pin.
    pub fn execution_kind(&self) -> &'static str {
        EXECUTION_KIND
    }

    /// Selected isolation domain pin.
    pub fn domain_kind(&self) -> &'static str {
        DOMAIN_KIND
    }

    /// Caller function named by the exact Profile edge.
    pub fn caller_function_id(&self) -> &str {
        verified_text(&self.caller)
    }

    /// Selected target function.
    pub fn function_id(&self) -> &'static str {
        self.route.function
    }

    /// Contract on the exact Profile edge.
    pub fn contract_id(&self) -> &'static str {
        self.route.contract
    }

    /// Operation on the exact Profile edge.
    pub fn operation_id(&self) -> &'static str {
        self.route.operation
    }

    /// Contract semantics committed by the function principal and scope.
    pub fn contract_revision_digest(&self) -> &str {
        verified_text(&self.contract_revision_digest)
    }

    /// Implementation digest committed by the function principal.
    pub fn function_implementation_digest(&self) -> &str {
        verified_text(&self.function_implementation_digest)
    }

    /// Profile Authority reference for this exact edge and snapshot.
    pub fn authority_reference(&self) -> &str {
        verified_text(&self.authority_reference)
    }

    /// Digest of the exact operation invocation scope.
    pub fn requested_scope_digest(&self) -> &str {
        self.requested_scope_digest.as_str()
    }
}

/// Immutable metadata for the selected Shell's direct Model List grant.
pub struct OriginalModelSettingsSelection<'a> {
    _active: &'a OriginalActiveProfile,
    model_list: OriginalRoutePin,
}

impl<'a> OriginalModelSettingsSelection<'a> {
    /// Select the exact signed Python Model List route from the active records.
    ///
    /// An absent or unsupported route yields `None`. Inconsistent records and
    /// exhausted budget produce errors. Verify the returned pin independently
    /// against the packaged catalog before using it.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        Ok(
            select_model_list_records(active.records(), budget)?.map(|model_list| Self {
                _active: active,
                model_list,
            }),
        )
    }

    /// The selected Shell-to-Model List grant target.
    pub fn model_list(&self) -> &OriginalRoutePin {
        &self.model_list
    }
}

/// Immutable metadata for the two original Profile grant edges used by Model Save.
pub struct OriginalModelSaveSelection<'a> {
    _active: &'a OriginalActiveProfile,
    model_manage: OriginalRoutePin,
    provider_read: OriginalRoutePin,
}

impl<'a> OriginalModelSaveSelection<'a> {
    /// Select the exact Shell Model Manage route and its Provider Registry read.
    ///
    /// An absent or unsupported route yields `None`. Inconsistent records and
    /// exhausted budget produce errors. Verify both returned pins independently
    /// against the packaged catalogs before using them.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let Some((model_manage, provider_read)) =
            select_route_chain(active.records(), &MODEL_MANAGE, &PROVIDER_READ, budget)?
        else {
            return Ok(None);
        };
        Ok(Some(Self {
            _active: active,
            model_manage,
            provider_read,
        }))
    }

    /// The selected Shell-to-Model Manage grant target.
    pub fn model_manage(&self) -> &OriginalRoutePin {
        &self.model_manage
    }

    /// The selected Model Manage-to-Provider Registry read grant target.
    pub fn provider_read(&self) -> &OriginalRoutePin {
        &self.provider_read
    }
}

/// The original conversation-owner read grants for the three supported callers.
pub struct OriginalConversationReadSelection<'a> {
    _active: &'a OriginalActiveProfile,
    shell: Option<OriginalRoutePin>,
    saved_turn: Option<OriginalRoutePin>,
    saved_orchestration: Option<OriginalRoutePin>,
}

impl<'a> OriginalConversationReadSelection<'a> {
    /// Select each caller's exact signed Python route independently.
    ///
    /// An absent or unsupported role has no pin. Verify each selected pin
    /// independently against the packaged catalog before using it.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let [shell, saved_turn, saved_orchestration] =
            select_conversation_read_records(active.records(), budget)?;
        if shell.is_none() && saved_turn.is_none() && saved_orchestration.is_none() {
            return Ok(None);
        }
        Ok(Some(Self {
            _active: active,
            shell,
            saved_turn,
            saved_orchestration,
        }))
    }

    /// The selected Shell's direct conversation-owner read grant.
    pub fn shell(&self) -> Option<&OriginalRoutePin> {
        self.shell.as_ref()
    }

    /// The Saved Turn function's nested conversation-owner read grant.
    pub fn saved_turn(&self) -> Option<&OriginalRoutePin> {
        self.saved_turn.as_ref()
    }

    /// The Saved Orchestration function's nested conversation-owner read grant.
    pub fn saved_orchestration(&self) -> Option<&OriginalRoutePin> {
        self.saved_orchestration.as_ref()
    }
}

/// A Gateway caller and Model Registry Resolve child operation.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalModelResolveRole {
    /// Route Quote reading the Generate child operation.
    QuoteGenerate,
    /// Route Quote reading the Stream child operation.
    QuoteStream,
    /// Generate reading the Generate child operation.
    Generate,
    /// Stream reading the Stream child operation.
    Stream,
}

impl OriginalModelResolveRole {
    const fn index(self) -> usize {
        match self {
            Self::QuoteGenerate => 0,
            Self::QuoteStream => 1,
            Self::Generate => 2,
            Self::Stream => 3,
        }
    }
}

/// Signed Model Registry Resolve grants for the four supported Gateway routes.
pub struct OriginalModelResolveSelection<'a> {
    _active: &'a OriginalActiveProfile,
    pins: [Option<OriginalRoutePin>; 4],
}

impl<'a> OriginalModelResolveSelection<'a> {
    /// Select each exact signed Python child route independently.
    ///
    /// Absent or unsupported roles have no pin. Record inconsistencies or
    /// capacity failures prevent selection. Verify each pin independently
    /// against the packaged catalog before using it.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let pins = select_model_resolve_records(active.records(), budget)?;
        Ok(pins.iter().any(Option::is_some).then_some(Self {
            _active: active,
            pins,
        }))
    }

    /// The exact signed grant for this Gateway caller and child operation.
    pub fn pin(&self, role: OriginalModelResolveRole) -> Option<&OriginalRoutePin> {
        self.pins[role.index()].as_ref()
    }
}

/// Immutable metadata for the two Profile grant edges to old Provider prepare.
pub struct OriginalProviderPrepareSelection<'a> {
    _active: &'a OriginalActiveProfile,
    coordinator: OriginalRoutePin,
    provider: OriginalRoutePin,
}

impl<'a> OriginalProviderPrepareSelection<'a> {
    /// Select the legacy route from the authenticated active Profile.
    ///
    /// An absent or unsupported route yields `None`. Inconsistent records and
    /// exhausted budget produce errors. The resulting pins require independent
    /// verification against the packaged catalogs.
    pub fn select(
        active: &'a OriginalActiveProfile,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Self>> {
        let Some((coordinator, provider)) =
            select_route_chain(active.records(), &COORDINATOR, &PROVIDER_PREPARE, budget)?
        else {
            return Ok(None);
        };
        Ok(Some(Self {
            _active: active,
            coordinator,
            provider,
        }))
    }

    /// The selected Shell-to-coordinator grant target.
    pub fn coordinator(&self) -> &OriginalRoutePin {
        &self.coordinator
    }

    /// The selected coordinator-to-Provider-prepare grant target.
    pub fn provider(&self) -> &OriginalRoutePin {
        &self.provider
    }
}

struct Records<'a> {
    shell_caller: CoreValue,
    edges: CoreValue,
    references: CoreValue,
    bindings: CoreValue,
    pack_pins: CoreValue,
    variant_pins: CoreValue,
    snapshot: CoreValue,
    budget: &'a Arc<dyn AllocationBudget>,
}

fn select_route_chain(
    active: (&CoreValue, &CoreValue, &CoreValue),
    shell_target: &'static Route,
    nested_target: &'static Route,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<(OriginalRoutePin, OriginalRoutePin)>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    let Some(first) = records.select_shell_route(shell_target)? else {
        return Ok(None);
    };
    Ok(records
        .select_route(first.function_id(), nested_target)?
        .map(|second| (first, second)))
}

fn select_model_list_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<OriginalRoutePin>> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(None);
    };
    records.select_shell_route(&MODEL_LIST)
}

fn select_conversation_read_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<[Option<OriginalRoutePin>; 3]> {
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok([None, None, None]);
    };
    Ok([
        records.select_shell_route(&CONVERSATION_READ)?,
        records.select_route(SAVED_TURN_CALLER, &CONVERSATION_READ)?,
        records.select_route(SAVED_ORCHESTRATION_CALLER, &CONVERSATION_READ)?,
    ])
}

fn select_model_resolve_records(
    active: (&CoreValue, &CoreValue, &CoreValue),
    budget: &Arc<dyn AllocationBudget>,
) -> Result<[Option<OriginalRoutePin>; 4]> {
    let mut pins = [None, None, None, None];
    let Some(records) = selected_shell_records(active, budget)? else {
        return Ok(pins);
    };
    use OriginalModelResolveRole::{Generate, QuoteGenerate, QuoteStream, Stream};
    for (role, caller, route) in [
        (QuoteGenerate, GATEWAY_QUOTE_CALLER, &MODEL_RESOLVE_GENERATE),
        (QuoteStream, GATEWAY_QUOTE_CALLER, &MODEL_RESOLVE_STREAM),
        (Generate, GATEWAY_GENERATE_CALLER, &MODEL_RESOLVE_GENERATE),
        (Stream, GATEWAY_STREAM_CALLER, &MODEL_RESOLVE_STREAM),
    ] {
        pins[role.index()] = records.select_route(caller, route)?;
    }
    Ok(pins)
}

fn selected_shell_records<'a>(
    (profile, lock, plan): (&CoreValue, &CoreValue, &CoreValue),
    budget: &'a Arc<dyn AllocationBudget>,
) -> Result<Option<Records<'a>>> {
    let shell = required(profile, "shell")?;
    if !shell
        .eq_guest(&required(lock, "shell")?, budget)
        .map_err(|_| Capacity)?
    {
        return Err(Inconsistent("selected Shell differs across records"));
    }
    // Plan carries a definition digest, while Profile and Lock record the
    // definition revision and platform. Compare their shared Shell identity.
    let plan_shell = required(plan, "shell")?;
    for field in [
        "provider_id",
        "pack_id",
        "artifact_digest",
        "executable_artifact_digest",
        "contract_id",
    ] {
        let expected = text_field(&shell, field)?;
        if !is_text(&plan_shell, field, verified_text(&expected)) {
            return Err(Inconsistent("selected Shell differs across records"));
        }
    }
    if !is_text(&shell, "contract_id", "app.shell.v1") {
        return Ok(None);
    }
    Ok(Some(Records {
        shell_caller: text_field(&shell, "provider_id")?,
        edges: required(profile, "requested_edges")?,
        references: required(profile, "authority_references")?,
        bindings: required(plan, "bindings")?,
        pack_pins: required(profile, "packs")?,
        variant_pins: required(lock, "variant_pins")?,
        snapshot: digest_field(profile, "profile_authority_snapshot_digest")?,
        budget,
    }))
}

impl Records<'_> {
    fn select_shell_route(&self, route: &'static Route) -> Result<Option<OriginalRoutePin>> {
        self.select_route(verified_text(&self.shell_caller), route)
    }

    fn select_route(
        &self,
        caller: &str,
        route: &'static Route,
    ) -> Result<Option<OriginalRoutePin>> {
        let matches_route = |value: &CoreValue| {
            is_text(value, "caller_function_id", caller)
                && is_text(value, "contract_id", route.contract)
                && is_text(value, "operation_id", route.operation)
        };
        let edge = self.unique(&self.edges, |edge| {
            matches_route(edge) && is_text(edge, "target_provider_id", route.function)
        })?;
        let binding = self.unique(&self.bindings, |binding| {
            matches_route(binding)
                && binding
                    .get_key("function_principal")
                    .is_some_and(|principal| is_text(&principal, "function_id", route.function))
        })?;
        let (edge, binding) = match (edge, binding) {
            (None, None) => return Ok(None),
            (Some(edge), Some(binding)) => (edge, binding),
            _ => return Err(Inconsistent("the Profile edge and Plan binding differ")),
        };

        let grant = is_profile_grant(&edge)?;
        if grant != is_profile_grant(&binding)? {
            return Err(Inconsistent(
                "the Profile edge and Plan Authority modes differ",
            ));
        }
        let reference = text_field(&edge, "authority_reference")?;
        let expected_reference =
            authority_reference(caller, route, verified_text(&self.snapshot), self.budget)?;
        let reference_text = verified_text(&reference);
        if reference_text.strip_prefix("authority-ref:") != Some(expected_reference.hex())
            || !is_text(&binding, "authority_reference", reference_text)
            || self
                .unique(&self.references, |value| {
                    value.as_str() == Some(reference_text)
                })?
                .is_none()
        {
            return Err(Inconsistent(
                "the Authority reference differs from the Profile edge",
            ));
        }

        let artifact = digest_field(&binding, "artifact_digest")?;
        let catalog = digest_field(&binding, "executable_catalog_digest")?;
        let principal = required(&binding, "function_principal")?;
        let implementation = digest_field(&principal, "function_implementation_digest")?;
        let revision = digest_field(&principal, "contract_revision_digest")?;
        if !is_text(&binding, "pack_id", route.pack)
            || !is_text(&principal, "operation_id", route.operation)
            || !is_text(
                &principal,
                "parent_artifact_digest",
                verified_text(&artifact),
            )
        {
            return Err(Inconsistent(
                "the selected function principal differs from its route",
            ));
        }
        let pack = self
            .unique(&self.pack_pins, |pack| is_text(pack, "pack_id", route.pack))?
            .ok_or(Inconsistent(
                "the selected Pack is missing from the Profile",
            ))?;
        if !is_text(&pack, "artifact_digest", verified_text(&artifact)) {
            return Err(Inconsistent(
                "the selected Pack artifact differs from the Profile",
            ));
        }

        let variant = text_field(&binding, "variant_id")?;
        let pin = self
            .unique(&self.variant_pins, |pin| {
                is_text(pin, "pack_id", route.pack)
                    && is_text(pin, "variant_id", verified_text(&variant))
            })?
            .ok_or(Inconsistent(
                "the selected variant is missing from the Lock",
            ))?;
        if pin.object().is_none_or(|pin| pin.len() != PIN_FIELDS.len())
            || PIN_FIELDS.into_iter().any(|field| {
                !binding
                    .get_key(field)
                    .and_then(|value| value.as_str().map(|text| is_text(&pin, field, text)))
                    .unwrap_or(false)
            })
        {
            return Err(Inconsistent("the Lock variant pin differs from the Plan"));
        }

        let scope = required(&edge, "requested_scope_template")?;
        let requested_scope_digest = scope.digest_guest(self.budget).map_err(|_| Capacity)?;
        if !is_text(
            &binding,
            "requested_scope_digest",
            requested_scope_digest.as_str(),
        ) {
            return Err(Inconsistent(
                "the Plan scope digest differs from the Profile edge",
            ));
        }
        let adapters = required(&binding, "adapter_digests")?;
        let adapters = adapters.array().ok_or(Inconsistent("adapter_digests"))?;
        if !grant
            || verified_text(&variant) != route.variant
            || LEGACY_EXECUTION
                .into_iter()
                .any(|(field, expected)| !is_text(&binding, field, expected))
            || !adapters.is_empty()
            || !exact_scope(&scope, route, verified_text(&revision))?
        {
            return Ok(None);
        }
        Ok(Some(OriginalRoutePin {
            route,
            caller: text_field(&binding, "caller_function_id")?,
            artifact_digest: artifact,
            executable_catalog_digest: catalog,
            contract_revision_digest: revision,
            function_implementation_digest: implementation,
            authority_reference: reference,
            requested_scope_digest,
        }))
    }

    fn unique(
        &self,
        rows: &CoreValue,
        matches: impl Fn(&CoreValue) -> bool,
    ) -> Result<Option<CoreValue>> {
        let rows = rows.array().ok_or(Inconsistent("record list is invalid"))?;
        // A match probes only fixed keys and compares against the already
        // selected identities. No row storage is copied or reallocated.
        charge(self.budget, rows.len().saturating_mul(8))?;
        let mut matches = rows.iter().filter(matches);
        let first = matches.next();
        if matches.next().is_some() {
            return Err(Inconsistent("the selected record is duplicated"));
        }
        Ok(first)
    }
}

fn is_profile_grant(value: &CoreValue) -> Result<bool> {
    match value.get_key("authority_mode") {
        // The old resolver omits the default mode in the Plan.
        None => Ok(true),
        Some(mode) if mode.as_str() == Some("profile_grant") => Ok(true),
        Some(mode) if mode.as_str() == Some("interactive_only") => Ok(false),
        Some(_) => Err(Inconsistent("the selected Authority mode is invalid")),
    }
}

fn authority_reference(
    caller: &str,
    route: &Route,
    snapshot: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Digest> {
    // profile_capture._authority_reference commits the exact four-part edge
    // and the current snapshot in canonical JSON. Profile identifiers cannot
    // contain JSON escapes or the edge separator.
    require_identifier(caller, budget)?;
    let parts = [
        "{\"edge\":\"",
        caller,
        "|",
        route.function,
        "|",
        route.contract,
        "|",
        route.operation,
        "\",\"profile_authority_snapshot_digest\":\"",
        snapshot,
        "\",\"schema\":\"io.tobkiri.profile-authority-edge.v1\"}",
    ];
    charge(budget, parts.into_iter().map(str::len).sum())?;
    let mut hash = Sha256::new();
    for part in parts {
        hash.update(part.as_bytes());
    }
    Ok(Digest::from_sha256(hash.finalize().into()))
}

fn require_identifier(caller: &str, budget: &Arc<dyn AllocationBudget>) -> Result<()> {
    charge(budget, caller.len())?;
    if caller
        .bytes()
        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte))
        && !caller.is_empty()
    {
        Ok(())
    } else {
        Err(Inconsistent("the selected Shell identifier is invalid"))
    }
}

fn exact_scope(scope: &CoreValue, route: &Route, revision: &str) -> Result<bool> {
    let dimensions = required(scope, "dimensions")?;
    let matches_dimension = |field, expected| {
        dimensions
            .get_key(field)
            .and_then(|value| {
                let values = value.array()?;
                values.get(0).filter(|_| values.len() == 1)
            })
            .is_some_and(|value| value.as_str() == Some(expected))
    };
    Ok(scope.object().is_some_and(|object| object.len() == 6)
        && is_text(scope, "capability", "operation.invoke")
        && is_text(scope, "semantics_digest", revision)
        && dimensions.object().is_some_and(|object| object.len() == 2)
        && matches_dimension("contract", route.contract)
        && matches_dimension("operation", route.operation)
        && required(scope, "quotas")?
            .object()
            .is_some_and(|quotas| quotas.is_empty())
        && required(scope, "opaque")?.as_bool() == Some(false)
        && matches!(
            required(scope, "exact_request_digest")?.inspect(),
            CoreRef::Null
        ))
}

fn required(value: &CoreValue, field: &'static str) -> Result<CoreValue> {
    value.get_key(field).ok_or(Inconsistent(field))
}

fn is_text(value: &CoreValue, field: &str, expected: &str) -> bool {
    value
        .get_key(field)
        .is_some_and(|value| value.as_str() == Some(expected))
}

fn text_field(value: &CoreValue, field: &'static str) -> Result<CoreValue> {
    let value = required(value, field)?;
    if value.as_str().is_none_or(str::is_empty) {
        return Err(Inconsistent(field));
    }
    Ok(value)
}

fn digest_field(value: &CoreValue, field: &'static str) -> Result<CoreValue> {
    let value = text_field(value, field)?;
    let digest = verified_text(&value);
    if digest.strip_prefix("sha256:").is_none_or(|hex| {
        hex.len() != 64
            || !hex
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    }) {
        return Err(Inconsistent(field));
    }
    Ok(value)
}

fn verified_text(value: &CoreValue) -> &str {
    value.as_str().expect("verified selection string")
}

fn charge(budget: &Arc<dyn AllocationBudget>, work: usize) -> Result<()> {
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: work as u64,
        })
        .map_err(|_| Capacity)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};
    use tobkiri_canon::AllocationError;

    struct TestBudget;

    impl AllocationBudget for TestBudget {
        fn reserve(&self, _: AllocationRequest) -> std::result::Result<(), AllocationError> {
            Ok(())
        }

        fn release(&self, _: u64) {}
    }

    // Use the snapshot and exact edge/scope digests for the Registry routes.
    // The rest of the original activation closure is omitted.
    fn records() -> Value {
        const SHELL: &str = "shell.tauri.default";
        let mut shell = json!({
            "contract_id": "app.shell.v1",
            "provider_id": SHELL,
            "pack_id": SHELL,
            "artifact_digest": Digest::hash(b"shell").as_str(),
            "executable_artifact_digest": Digest::hash(b"executable").as_str()
        });
        let mut plan_shell = shell.clone();
        plan_shell["definition_digest"] = json!(Digest::hash(b"definition").as_str());
        shell["definition_revision"] = json!(Digest::hash(b"revision").as_str());
        shell["architecture"] = json!("arm64");
        shell["platform"] = json!("macos");
        let mut records = json!({
            "profile": {
                "shell": shell,
                "packs": [],
                "requested_edges": [],
                "authority_references": [],
                "profile_authority_snapshot_digest":
                    "sha256:0df912a62eed85abe0cadd3f18e5e419d2b011f7be059759fe71537879bdb276"
            },
            "lock": {"shell": shell, "variant_pins": []},
            "plan": {"shell": plan_shell, "bindings": []}
        });
        let rows = [
            (
                &COORDINATOR,
                SHELL,
                "authority-ref:8a8b1444d963a1da5fd8a89b9486c7c575994bd2d20e7f85c290ae59e7271911",
                "sha256:87f5d2c6e9ee6430c26ba87fe91e05bf0c21bc34dbe553ca81971154eaa45242",
                "sha256:e4783f46c56c558c2435d944ef2662d3a91b3d102226dbc151feff009ef821ed",
            ),
            (
                &PROVIDER_PREPARE,
                COORDINATOR.function,
                "authority-ref:33b0249506c3d5ae1b94c9aa40fa766708ec93a2ff1e24b9cc9e7ff11de7c35f",
                "sha256:f4d8e87f748eee562a010984e56839084ac012ad6bd7ceec5f55f2e30bf6adb9",
                "sha256:5e79aeb456ca085625c28d9f4d8fe219d1c1a0a621bdcabd66ba8b8eec01a5e3",
            ),
            (
                &MODEL_LIST,
                SHELL,
                "authority-ref:805eb0ebcb6d6d912e6f9080be3a75c5d16ff2f0ffaf20f7fe8dc5bce6eafac3",
                "sha256:2604f5c72ce089f2cfcc4c4561c4b7ac04119c64b52ac1f495ac013d1a946658",
                "sha256:64fa11ae6949412f39039e7069da0a614e78401c55071be40755f2920b28932e",
            ),
            (
                &MODEL_MANAGE,
                SHELL,
                "authority-ref:370c601554aeff4cfeea4302eb9e9a53652f6ff5f801936d26b558a122899a2b",
                "sha256:e6a4c0eb6f5dd04d117aa8343af2319b3d5d06a502b14ecebfbba08e0b2dd052",
                "sha256:7e5d3aa5434e2f9a7d23ed7df2bab9df80d2bd8bcf5811ef9af33aa1bada567b",
            ),
            (
                &PROVIDER_READ,
                MODEL_MANAGE.function,
                "authority-ref:e432e888768241f9db182dfbe3b5278b04787a3d6e16f6856dd512a5af3654a7",
                "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d",
                "sha256:1b58a95d7c67e6f789b5e17a10a61c360c706443adbbe3026502e7e29395bd6f",
            ),
            (
                &PROVIDER_READ,
                SHELL,
                "authority-ref:c0b9128e7e44426c9df7b301c4903249948d782b264fdf9c33e9d3b9aebbe310",
                "sha256:081588f1316a3daf91c2223f8a6c2625cd2ce2dc08c40be0c7d87d87c74d698d",
                "sha256:1b58a95d7c67e6f789b5e17a10a61c360c706443adbbe3026502e7e29395bd6f",
            ),
            (
                &CONVERSATION_READ,
                SHELL,
                "authority-ref:1ac460445bfec95ce8451f251740514d6e4275fb9fdb3bbe3f60a1df9ad41b83",
                "sha256:254d3ee4bdbe11bf21f658bbb3b89a1d6510ef7dbf77fb9108dcc4b426496156",
                "sha256:d0069dfedd4def12d6921155433e7f280b2e2fb4842b9c32d07062f913dfaa8c",
            ),
            (
                &CONVERSATION_READ,
                SAVED_TURN_CALLER,
                "authority-ref:102d72ffc48697e9c5aa19b70b82a502237abf81b823ce3bf6fc65ff34729462",
                "sha256:254d3ee4bdbe11bf21f658bbb3b89a1d6510ef7dbf77fb9108dcc4b426496156",
                "sha256:d0069dfedd4def12d6921155433e7f280b2e2fb4842b9c32d07062f913dfaa8c",
            ),
            (
                &CONVERSATION_READ,
                SAVED_ORCHESTRATION_CALLER,
                "authority-ref:5d9e74a9f4f8824cdef4dafb34937f7721fd09730df71eb54c14c48a3634b80d",
                "sha256:254d3ee4bdbe11bf21f658bbb3b89a1d6510ef7dbf77fb9108dcc4b426496156",
                "sha256:d0069dfedd4def12d6921155433e7f280b2e2fb4842b9c32d07062f913dfaa8c",
            ),
            (
                &MODEL_RESOLVE_GENERATE,
                GATEWAY_QUOTE_CALLER,
                "authority-ref:a6fedd87679222f2f377774171406f2c65ad5d1f643ba367022ca6a67e30964f",
                "sha256:2604f5c72ce089f2cfcc4c4561c4b7ac04119c64b52ac1f495ac013d1a946658",
                "sha256:e0adca96baa7c877bc049187d16aee8eee3dbc00c5a06f5160e7c92baa7be0f6",
            ),
            (
                &MODEL_RESOLVE_STREAM,
                GATEWAY_QUOTE_CALLER,
                "authority-ref:d8d87c62e86763d17ca93bd0d95669062aa8da60c069294e24c56ae3b15a0c59",
                "sha256:2604f5c72ce089f2cfcc4c4561c4b7ac04119c64b52ac1f495ac013d1a946658",
                "sha256:f5746a0621a144db146e8e05bd24564378fb7c72fd8d3b4d07ecf7d9a4369472",
            ),
            (
                &MODEL_RESOLVE_GENERATE,
                GATEWAY_GENERATE_CALLER,
                "authority-ref:08510fb1f887bd4e1af388b73f30f0850b2392f6fb174d5bd4a948403b5a9bde",
                "sha256:2604f5c72ce089f2cfcc4c4561c4b7ac04119c64b52ac1f495ac013d1a946658",
                "sha256:e0adca96baa7c877bc049187d16aee8eee3dbc00c5a06f5160e7c92baa7be0f6",
            ),
            (
                &MODEL_RESOLVE_STREAM,
                GATEWAY_STREAM_CALLER,
                "authority-ref:a180b8c58d6bea4e5521b0cdc85924aece477562653a7a3e27a428ef8bc739b5",
                "sha256:2604f5c72ce089f2cfcc4c4561c4b7ac04119c64b52ac1f495ac013d1a946658",
                "sha256:f5746a0621a144db146e8e05bd24564378fb7c72fd8d3b4d07ecf7d9a4369472",
            ),
        ];
        for (route, caller, reference, revision, scope_digest) in rows {
            let artifact = Digest::hash(route.pack.as_bytes());
            let catalog = Digest::hash(route.variant.as_bytes());
            let implementation = Digest::hash(route.function.as_bytes());
            let pin = json!({
                "pack_id": route.pack,
                "variant_id": route.variant,
                "artifact_digest": artifact.as_str(),
                "executable_catalog_digest": catalog.as_str(),
                "platform": "any",
                "architecture": "any",
                "runtime_abi": "python3.13",
                "backend": "tobkiri.python-host-v4",
                "execution_kind": "host_extension",
                "domain_kind": "dedicated_process"
            });
            let mut binding = pin.clone();
            binding["caller_function_id"] = json!(caller);
            binding["contract_id"] = json!(route.contract);
            binding["operation_id"] = json!(route.operation);
            binding["authority_reference"] = json!(reference);
            binding["requested_scope_digest"] = json!(scope_digest);
            binding["adapter_digests"] = json!([]);
            binding["function_principal"] = json!({
                "function_id": route.function,
                "contract_revision_digest": revision,
                "function_implementation_digest": implementation.as_str(),
                "operation_id": route.operation,
                "parent_artifact_digest": artifact.as_str(),
            });
            records["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .push(binding);
            let variant_pins = records["lock"]["variant_pins"].as_array_mut().unwrap();
            if !variant_pins.contains(&pin) {
                variant_pins.push(pin);
            }
            let packs = records["profile"]["packs"].as_array_mut().unwrap();
            if !packs.iter().any(|pack| pack["pack_id"] == route.pack) {
                packs.push(json!({
                    "pack_id": route.pack,
                    "role": "provider",
                    "artifact_digest": artifact.as_str(),
                }));
            }
            records["profile"]["authority_references"]
                .as_array_mut()
                .unwrap()
                .push(json!(reference));
            records["profile"]["requested_edges"]
                .as_array_mut().unwrap().push(json!({
                    "caller_function_id": caller,
                    "target_provider_id": route.function,
                    "contract_id": route.contract,
                    "operation_id": route.operation,
                    "authority_mode": "profile_grant",
                    "authority_reference": reference,
                    "requested_scope_template": {
                        "capability": "operation.invoke",
                        "semantics_digest": revision,
                        "dimensions": {"operation": [route.operation], "contract": [route.contract]},
                        "quotas": {},
                        "opaque": false,
                        "exact_request_digest": null,
                    },
                }));
        }
        records
    }

    fn parse_records(records: &Value, budget: &Arc<dyn AllocationBudget>) -> [CoreValue; 3] {
        ["profile", "lock", "plan"].map(|field| {
            let bytes = serde_json::to_vec(&records[field]).unwrap();
            CoreValue::parse_guest(&bytes, budget).unwrap()
        })
    }

    fn select(records: &Value) -> Result<Option<(OriginalRoutePin, OriginalRoutePin)>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_route_chain(
            (&profile, &lock, &plan),
            &COORDINATOR,
            &PROVIDER_PREPARE,
            &budget,
        )
    }

    fn select_model_list(records: &Value) -> Result<Option<OriginalRoutePin>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_model_list_records((&profile, &lock, &plan), &budget)
    }

    fn select_model_save(records: &Value) -> Result<Option<(OriginalRoutePin, OriginalRoutePin)>> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_route_chain(
            (&profile, &lock, &plan),
            &MODEL_MANAGE,
            &PROVIDER_READ,
            &budget,
        )
    }

    fn select_conversation_read(records: &Value) -> Result<[Option<OriginalRoutePin>; 3]> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_conversation_read_records((&profile, &lock, &plan), &budget)
    }

    fn select_model_resolve(records: &Value) -> Result<[Option<OriginalRoutePin>; 4]> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let [profile, lock, plan] = parse_records(records, &budget);
        select_model_resolve_records((&profile, &lock, &plan), &budget)
    }

    const MODEL_MANAGE_ROW: usize = 3;
    const MODEL_PROVIDER_READ_ROW: usize = 4;
    const CONVERSATION_SHELL_ROW: usize = 6;
    const CONVERSATION_SAVED_TURN_ROW: usize = 7;
    const CONVERSATION_SAVED_ORCHESTRATION_ROW: usize = 8;
    const MODEL_RESOLVE_ROWS: std::ops::Range<usize> = 9..13;

    #[test]
    fn model_resolve_requires_each_exact_caller_and_child_operation() {
        for (role_index, row) in MODEL_RESOLVE_ROWS.enumerate() {
            let mut fixture = records();
            let plan_binding = fixture["plan"]["bindings"][row].clone();
            let pins = select_model_resolve(&fixture).unwrap();
            let pin = pins[role_index].as_ref().unwrap();
            assert_eq!(pin.caller_function_id(), plan_binding["caller_function_id"]);
            assert_eq!(pin.operation_id(), plan_binding["operation_id"]);
            assert_eq!(
                pin.authority_reference(),
                plan_binding["authority_reference"]
            );
            assert_eq!(
                pin.requested_scope_digest(),
                plan_binding["requested_scope_digest"]
            );

            fixture["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(matches!(
                select_model_resolve(&fixture),
                Err(Inconsistent(_))
            ));

            fixture["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            for (other_index, pin) in select_model_resolve(&fixture)
                .unwrap()
                .into_iter()
                .enumerate()
            {
                assert_eq!(pin.is_some(), other_index != role_index);
            }
        }

        let mut fixture = records();
        fixture["profile"]["requested_edges"]
            .as_array_mut()
            .unwrap()
            .drain(MODEL_RESOLVE_ROWS);
        fixture["plan"]["bindings"]
            .as_array_mut()
            .unwrap()
            .drain(MODEL_RESOLVE_ROWS);
        assert!(
            select_model_resolve(&fixture)
                .unwrap()
                .iter()
                .all(Option::is_none)
        );
        assert!(select_model_list(&fixture).unwrap().is_some());
        assert!(select_model_save(&fixture).unwrap().is_some());
    }

    #[test]
    fn model_resolve_checks_signed_scope_and_authority_for_both_child_operations() {
        for (role_index, row) in MODEL_RESOLVE_ROWS.enumerate() {
            let mut fixture = records();
            // Two callers share each operation scope but have separate authority references.
            let other_caller_row = if role_index < 2 { row + 2 } else { row - 2 };
            let wrong_reference =
                fixture["plan"]["bindings"][other_caller_row]["authority_reference"].clone();
            fixture["plan"]["bindings"][row]["authority_reference"] = wrong_reference.clone();
            fixture["profile"]["requested_edges"][row]["authority_reference"] = wrong_reference;
            assert!(matches!(
                select_model_resolve(&fixture),
                Err(Inconsistent(_))
            ));

            let mut fixture = records();
            let scope = &mut fixture["profile"]["requested_edges"][row]["requested_scope_template"];
            scope["dimensions"]["operation"] = json!([
                MODEL_RESOLVE_GENERATE.operation,
                MODEL_RESOLVE_STREAM.operation
            ]);
            let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
            let scope_digest = CoreValue::parse_guest(&serde_json::to_vec(scope).unwrap(), &budget)
                .unwrap()
                .digest_guest(&budget)
                .unwrap();
            assert!(matches!(
                select_model_resolve(&fixture),
                Err(Inconsistent(_))
            ));
            fixture["plan"]["bindings"][row]["requested_scope_digest"] =
                json!(scope_digest.as_str());
            for (other_index, pin) in select_model_resolve(&fixture)
                .unwrap()
                .into_iter()
                .enumerate()
            {
                assert_eq!(pin.is_some(), other_index != role_index);
            }
        }
    }

    #[test]
    fn conversation_read_selects_each_exact_caller_and_accepts_the_legacy_default_mode() {
        let mut fixture = records();
        let rows = [
            (CONVERSATION_SHELL_ROW, "shell.tauri.default"),
            (CONVERSATION_SAVED_TURN_ROW, SAVED_TURN_CALLER),
            (
                CONVERSATION_SAVED_ORCHESTRATION_ROW,
                SAVED_ORCHESTRATION_CALLER,
            ),
        ];
        for (row, _) in rows {
            fixture["profile"]["requested_edges"][row]
                .as_object_mut()
                .unwrap()
                .remove("authority_mode");
        }
        for (pin, (row, caller)) in select_conversation_read(&fixture)
            .unwrap()
            .into_iter()
            .zip(rows)
        {
            let pin = pin.unwrap();
            assert_eq!(pin.caller_function_id(), caller);
            assert_eq!(pin.function_id(), CONVERSATION_READ.function);
            assert_eq!(pin.contract_id(), CONVERSATION_READ.contract);
            assert_eq!(pin.operation_id(), CONVERSATION_READ.operation);
            assert_eq!(pin.variant_id(), CONVERSATION_READ.variant);
            assert_eq!(
                pin.authority_reference(),
                fixture["profile"]["requested_edges"][row]["authority_reference"]
            );
        }
    }

    #[test]
    fn conversation_read_needs_each_role_to_have_its_own_profile_grant() {
        let mut fixture = records();
        let row = CONVERSATION_SAVED_TURN_ROW;
        fixture["profile"]["requested_edges"][row]["authority_mode"] = json!("interactive_only");
        fixture["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
        let row = CONVERSATION_SAVED_ORCHESTRATION_ROW;
        fixture["profile"]["requested_edges"][row]["caller_function_id"] =
            json!("unrelated.reader");
        fixture["plan"]["bindings"][row]["caller_function_id"] = json!("unrelated.reader");
        let [shell, saved_turn, saved_orchestration] = select_conversation_read(&fixture).unwrap();
        assert!(shell.is_some());
        assert!(saved_turn.is_none());
        assert!(saved_orchestration.is_none());

        let mut fixture = records();
        fixture["plan"]["bindings"][CONVERSATION_SAVED_TURN_ROW]["authority_mode"] =
            json!("interactive_only");
        assert!(matches!(
            select_conversation_read(&fixture),
            Err(Inconsistent(
                "the Profile edge and Plan Authority modes differ"
            ))
        ));
    }

    #[test]
    fn selects_the_exact_model_save_grants_with_both_scopes_and_variants() {
        let fixture = records();
        let (manage, read) = select_model_save(&fixture).unwrap().unwrap();
        assert_eq!(manage.caller_function_id(), "shell.tauri.default");
        assert_eq!(
            manage.function_id(),
            "rumi_model_registry_pack.model-registry.manage"
        );
        assert_eq!(read.caller_function_id(), manage.function_id());
        assert_eq!(
            read.function_id(),
            "rumi_provider_registry_pack.provider-registry.resource"
        );
        for (row, pin) in [(MODEL_MANAGE_ROW, manage), (MODEL_PROVIDER_READ_ROW, read)] {
            let binding = &fixture["plan"]["bindings"][row];
            assert_eq!(pin.contract_id(), binding["contract_id"].as_str().unwrap());
            assert_eq!(
                pin.operation_id(),
                binding["operation_id"].as_str().unwrap()
            );
            assert_eq!(pin.variant_id(), binding["variant_id"].as_str().unwrap());
            assert_eq!(
                pin.requested_scope_digest(),
                binding["requested_scope_digest"].as_str().unwrap()
            );
            assert_eq!(
                pin.authority_reference(),
                binding["authority_reference"].as_str().unwrap()
            );
        }
    }

    #[test]
    fn model_save_requires_each_grant_even_when_other_settings_grants_are_present() {
        for row in [MODEL_MANAGE_ROW, MODEL_PROVIDER_READ_ROW] {
            let mut fixture = records();
            fixture["profile"]["requested_edges"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(
                matches!(select_model_save(&fixture), Err(Inconsistent(_))),
                "row {row}"
            );

            fixture["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(select_model_save(&fixture).unwrap().is_none(), "row {row}");
            assert!(select_model_list(&fixture).unwrap().is_some());

            let mut fixture = records();
            fixture["plan"]["bindings"]
                .as_array_mut()
                .unwrap()
                .remove(row);
            assert!(
                matches!(select_model_save(&fixture), Err(Inconsistent(_))),
                "row {row}"
            );
        }
    }

    #[test]
    fn model_save_rejects_drift_in_each_grant_scope_authority_and_pin() {
        for row in [MODEL_MANAGE_ROW, MODEL_PROVIDER_READ_ROW] {
            for field in [
                "requested_scope_digest",
                "authority_reference",
                "variant_id",
            ] {
                let mut fixture = records();
                fixture["plan"]["bindings"][row][field] = json!("changed");
                assert!(
                    matches!(select_model_save(&fixture), Err(Inconsistent(_))),
                    "row {row} {field}"
                );
            }

            let mut fixture = records();
            fixture["profile"]["requested_edges"][row]["authority_mode"] =
                json!("interactive_only");
            assert!(
                matches!(select_model_save(&fixture), Err(Inconsistent(_))),
                "row {row}"
            );
            fixture["plan"]["bindings"][row]["authority_mode"] = json!("interactive_only");
            assert!(select_model_save(&fixture).unwrap().is_none(), "row {row}");
        }
    }

    #[test]
    fn selects_only_the_shell_model_list_grant_with_exact_scope_and_variant() {
        let fixture = records();
        let model_list = select_model_list(&fixture).unwrap().unwrap();
        assert_eq!(model_list.caller_function_id(), "shell.tauri.default");
        assert_eq!(
            model_list.requested_scope_digest(),
            fixture["plan"]["bindings"][2]["requested_scope_digest"]
                .as_str()
                .unwrap()
        );

        let mut other_caller = fixture.clone();
        other_caller["profile"]["requested_edges"][2]["caller_function_id"] =
            json!(COORDINATOR.function);
        other_caller["plan"]["bindings"][2]["caller_function_id"] = json!(COORDINATOR.function);
        assert!(select_model_list(&other_caller).unwrap().is_none());

        let mut interactive = fixture.clone();
        interactive["profile"]["requested_edges"][2]["authority_mode"] = json!("interactive_only");
        assert!(matches!(
            select_model_list(&interactive),
            Err(Inconsistent(_))
        ));
        interactive["plan"]["bindings"][2]["authority_mode"] = json!("interactive_only");
        assert!(select_model_list(&interactive).unwrap().is_none());

        let mut other_variant = fixture.clone();
        other_variant["plan"]["bindings"][2]["variant_id"] = json!("other.model.profile");
        assert!(matches!(
            select_model_list(&other_variant),
            Err(Inconsistent(_))
        ));
        other_variant["lock"]["variant_pins"][2]["variant_id"] = json!("other.model.profile");
        assert!(select_model_list(&other_variant).unwrap().is_none());

        let mut other_scope = fixture;
        let scope = &mut other_scope["profile"]["requested_edges"][2]["requested_scope_template"];
        scope["dimensions"]["operation"] =
            json!(["rumi_model_registry_pack.model-profile-resource.generate"]);
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let digest = CoreValue::parse_guest(&serde_json::to_vec(scope).unwrap(), &budget)
            .unwrap()
            .digest_guest(&budget)
            .unwrap();
        assert!(matches!(
            select_model_list(&other_scope),
            Err(Inconsistent(_))
        ));
        other_scope["plan"]["bindings"][2]["requested_scope_digest"] = json!(digest.as_str());
        assert!(select_model_list(&other_scope).unwrap().is_none());
    }

    #[test]
    fn selects_the_old_keyless_prepare_from_the_two_current_grants() {
        let (coordinator, provider) = select(&records()).unwrap().unwrap();
        assert_eq!(coordinator.caller_function_id(), "shell.tauri.default");
        assert_eq!(provider.caller_function_id(), coordinator.function_id());
        assert_eq!(
            provider.operation_id(),
            "rumi_provider_registry_pack.provider-configure-prepare"
        );
        assert_eq!(provider.variant_id(), PROVIDER_PREPARE.variant);
        assert_eq!(
            provider.artifact_digest(),
            Digest::hash(PROVIDER_PREPARE.pack.as_bytes()).as_str()
        );
        assert_eq!(
            provider.executable_catalog_digest(),
            Digest::hash(PROVIDER_PREPARE.variant.as_bytes()).as_str()
        );
    }

    #[test]
    fn leaves_absent_or_unsupported_routes_to_the_original_owner() {
        const EXECUTE: &str = "rumi_provider_registry_pack.provider-configure";
        const EXECUTE_FUNCTION: &str = "rumi_provider_registry_pack.provider-configure.execute";
        let mut execute = records();
        let edge = &mut execute["profile"]["requested_edges"][1];
        edge["target_provider_id"] = json!(EXECUTE_FUNCTION);
        edge["operation_id"] = json!(EXECUTE);
        edge["authority_mode"] = json!("interactive_only");
        let binding = &mut execute["plan"]["bindings"][1];
        binding["operation_id"] = json!(EXECUTE);
        binding["function_principal"]["function_id"] = json!(EXECUTE_FUNCTION);
        binding["authority_mode"] = json!("interactive_only");
        assert!(select(&execute).unwrap().is_none());

        let mut alternative = records();
        alternative["plan"]["bindings"][1]["variant_id"] = json!("other.provider.prepare");
        alternative["lock"]["variant_pins"][1]["variant_id"] = json!("other.provider.prepare");
        assert!(select(&alternative).unwrap().is_none());
    }

    #[test]
    fn rejects_ambiguous_or_inconsistent_selected_records() {
        for (record, field) in [
            ("profile", "requested_edges"),
            ("profile", "authority_references"),
            ("lock", "variant_pins"),
            ("plan", "bindings"),
        ] {
            let mut fixture = records();
            let rows = fixture[record][field].as_array_mut().unwrap();
            rows.push(rows[1].clone());
            assert!(matches!(select(&fixture), Err(Inconsistent(_))), "{field}");
        }

        for field in PIN_FIELDS {
            let mut fixture = records();
            fixture["lock"]["variant_pins"][1][field] = json!("changed");
            assert!(matches!(select(&fixture), Err(Inconsistent(_))), "{field}");
        }

        let mut fixture = records();
        fixture["plan"]["bindings"]
            .as_array_mut()
            .unwrap()
            .remove(1);
        assert!(matches!(select(&fixture), Err(Inconsistent(_))));

        let mut fixture = records();
        fixture["plan"]["bindings"][1]["authority_mode"] = json!("interactive_only");
        assert!(matches!(select(&fixture), Err(Inconsistent(_))));

        let mut fixture = records();
        fixture["plan"]["bindings"][1]["function_principal"]["parent_artifact_digest"] =
            json!(Digest::hash(b"other artifact").as_str());
        assert!(matches!(select(&fixture), Err(Inconsistent(_))));
    }

    #[test]
    fn rejects_inconsistent_authority_and_skips_an_unsupported_legacy_route() {
        let mut rebound = records();
        let reference =
            json!("authority-ref:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
        rebound["profile"]["authority_references"][1] = reference.clone();
        rebound["profile"]["requested_edges"][1]["authority_reference"] = reference.clone();
        rebound["plan"]["bindings"][1]["authority_reference"] = reference;
        assert!(matches!(select(&rebound), Err(Inconsistent(_))));

        let mut adapted = records();
        adapted["plan"]["bindings"][1]["adapter_digests"] =
            json!([Digest::hash(b"adapter").as_str()]);
        assert!(select(&adapted).unwrap().is_none());

        let mut platform = records();
        platform["plan"]["bindings"][1]["platform"] = json!("macos");
        platform["lock"]["variant_pins"][1]["platform"] = json!("macos");
        assert!(select(&platform).unwrap().is_none());

        let mut widened = records();
        widened["profile"]["requested_edges"][1]["requested_scope_template"]["dimensions"]["workspace"] =
            json!(["other.workspace"]);
        assert!(matches!(select(&widened), Err(Inconsistent(_))));
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let authored_scope = &widened["profile"]["requested_edges"][1]["requested_scope_template"];
        let encoded = serde_json::to_vec(authored_scope).unwrap();
        let digest = CoreValue::parse_guest(&encoded, &budget)
            .unwrap()
            .digest_guest(&budget)
            .unwrap();
        widened["plan"]["bindings"][1]["requested_scope_digest"] = json!(digest.as_str());
        assert!(select(&widened).unwrap().is_none());

        let mut inconsistent = records();
        inconsistent["plan"]["bindings"][1]["requested_scope_digest"] =
            json!(Digest::hash(b"other scope").as_str());
        assert!(matches!(select(&inconsistent), Err(Inconsistent(_))));
    }
}
