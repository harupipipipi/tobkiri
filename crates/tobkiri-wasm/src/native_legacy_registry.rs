//! Fixed native entry points for the original Profile Registries.
use super::{NativeSetupStore, NativeState, entry::ensure_active};
use crate::owned_values::core_to_val;
use anyhow::{Context, Result, bail, ensure};
use std::time::{SystemTime, UNIX_EPOCH};
use std::{fmt, io, sync::Arc};
use tobkiri_canon::{
    AllocationBudget, AllocationError, CoreBuffer, CoreRef, CoreValue, ids::ShapeRef,
};
use tobkiri_gate::ControlKind;
use tobkiri_manifest::{
    RuntimeSpec, ShapeKind, VerifiedPack, WitFunction, WitType, source::StagedArtifact,
};
use wasmtime::component::types::{ComponentFunc, ComponentInstance, ComponentItem, Type};
use wasmtime::component::{BudgetedBox, BudgetedString, BudgetedVec, Component, Linker, Val};
use wasmtime::{Engine, Store};

const PROVIDER_PACK: &str = "local.provider-registry";
const MODEL_PACK: &str = "local.model-registry";
const CONVERSATION_PACK: &str = "local.saved-conversation";
const FLOW: &str = "tobkiri:core/flow@1.2.0";
const PROFILE: &str = "current-profile";
const CALLBACK: &str = "invoke-callback";
const OBSERVE_ID: u64 = 1;
const ERROR_CODES: [&str; 9] = [
    "denied",
    "approval-required",
    "cancelled",
    "not-found",
    "invalid",
    "limit-exceeded",
    "conflict",
    "unavailable",
    "internal",
];
const MAX_DOCUMENT_BYTES: usize = 1024 * 1024;
const MAX_COMPONENT_BYTES: u64 = 4 * 1024 * 1024;

struct RegistryExport {
    pack: &'static str,
    shape: &'static str,
    interface: &'static str,
    function: &'static str,
    error_subject: &'static str,
    parameters: &'static [&'static str],
    reads: u8,
    clock: RegistryClock,
}

#[derive(Clone, Copy)]
enum RegistryClock {
    None,
    Required,
    ForReplacement,
}

const PREPARE: RegistryExport = RegistryExport {
    pack: PROVIDER_PACK,
    shape: "local.provider-registry.legacy-configuration-live@1.0",
    interface: "local:provider-registry/legacy-configuration-live@1.0.0",
    function: "prepare-local",
    error_subject: "legacy Provider prepare",
    parameters: &["request", "observe"],
    reads: 2,
    clock: RegistryClock::None,
};
const VALIDATE_COMMIT: RegistryExport = RegistryExport {
    pack: PROVIDER_PACK,
    shape: "local.provider-registry.legacy-configuration-commit@1.0",
    interface: "local:provider-registry/legacy-configuration-commit@1.0.0",
    function: "validate-local",
    error_subject: "legacy Provider prepare",
    parameters: &["request", "plan", "observe"],
    reads: 3,
    clock: RegistryClock::None,
};
const STAGE_COMMIT: RegistryExport = RegistryExport {
    pack: PROVIDER_PACK,
    shape: "local.provider-registry.legacy-configuration-apply@1.0",
    interface: "local:provider-registry/legacy-configuration-apply@1.0.0",
    function: "stage-local",
    error_subject: "legacy Provider prepare",
    parameters: &["request", "plan", "observe"],
    reads: 4,
    clock: RegistryClock::Required,
};
const MODEL_LIST: RegistryExport = RegistryExport {
    pack: MODEL_PACK,
    shape: "local.model-registry.legacy-settings@1.0",
    interface: "local:model-registry/legacy-settings@1.0.0",
    function: "list-models",
    error_subject: "legacy Model Registry list",
    parameters: &["document"],
    reads: 0,
    clock: RegistryClock::None,
};
const MODEL_RESOLVE: RegistryExport = RegistryExport {
    pack: MODEL_PACK,
    shape: "local.model-registry.legacy-resolve@1.0",
    interface: "local:model-registry/legacy-resolve@1.0.0",
    function: "resolve",
    error_subject: "legacy Model Registry resolve",
    parameters: &["document", "identifier"],
    reads: 0,
    clock: RegistryClock::None,
};
const MODEL_SAVE: RegistryExport = RegistryExport {
    pack: MODEL_PACK,
    shape: "local.model-registry.legacy-save@1.0",
    interface: "local:model-registry/legacy-save@1.0.0",
    function: "stage-model",
    error_subject: "legacy Model Registry save",
    parameters: &["request", "provider", "observe"],
    reads: 1,
    clock: RegistryClock::ForReplacement,
};
const CONVERSATION_GET: RegistryExport = RegistryExport {
    pack: CONVERSATION_PACK,
    shape: "local.saved-conversation.legacy-read@1.0",
    interface: "local:saved-conversation/legacy-read@1.0.0",
    function: "get",
    error_subject: "legacy Conversation read",
    parameters: &["document", "conversation-id"],
    reads: 0,
    clock: RegistryClock::None,
};
const CONVERSATION_RECEIPT: RegistryExport = RegistryExport {
    pack: CONVERSATION_PACK,
    shape: "local.saved-conversation.legacy-receipt@1.0",
    interface: "local:saved-conversation/legacy-receipt@1.0.0",
    function: "get",
    error_subject: "legacy Conversation saved receipt read",
    parameters: &["document", "turn-id"],
    reads: 0,
    clock: RegistryClock::None,
};
const CONVERSATION_CONFIRMATION: RegistryExport = RegistryExport {
    pack: CONVERSATION_PACK,
    shape: "local.saved-conversation.legacy-confirmation@1.0",
    interface: "local:saved-conversation/legacy-confirmation@1.0.0",
    function: "confirm",
    error_subject: "legacy Conversation completion confirmation",
    parameters: &["document", "turn"],
    reads: 0,
    clock: RegistryClock::None,
};

enum LegacyProviderOperation<'a> {
    Prepare,
    Validate(&'a CoreValue),
    Stage(&'a CoreValue),
}

enum RegistryInput<'a> {
    Provider {
        request: &'a CoreValue,
        plan: Option<&'a CoreValue>,
        read: LegacyProviderRegistryRead,
    },
    ModelList(Option<&'a str>),
    DocumentLookup {
        document: Option<&'a str>,
        identifier: &'a str,
    },
    ConversationConfirmation {
        document: &'a str,
        turn: &'a str,
    },
    ModelSave {
        request: &'a CoreValue,
        provider: &'a CoreValue,
        read: LegacyRegistryRead,
    },
}

/// A closed domain error from a legacy Registry operation. The guest's free
/// text is discarded to keep Registry data out of public errors and logs.
#[derive(Debug)]
pub struct LegacyRegistryError {
    code: &'static str,
    subject: &'static str,
}

impl LegacyRegistryError {
    /// The typed Core ABI code returned by the fixed entry point.
    pub fn code(&self) -> &'static str {
        self.code
    }
}

impl fmt::Display for LegacyRegistryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{} returned {}", self.subject, self.code)
    }
}

impl std::error::Error for LegacyRegistryError {}

/// Trusted native reader of the original Profile's fixed legacy Registry.
/// The supplied budget owns the bytes returned to the isolated guest.
pub type LegacyRegistryRead =
    Box<dyn FnMut(&Arc<dyn AllocationBudget>) -> Result<Option<CoreBuffer>> + Send>;

pub use LegacyRegistryRead as LegacyProviderRegistryRead;

/// Verify a receipt-checked Pack's fixed Model Save declaration at Launch.
/// The native invocation still checks the Component's actual imports and export.
pub fn verify_legacy_model_save_declaration(pack: &VerifiedPack) -> Result<()> {
    declared_registry_component(pack, &MODEL_SAVE)?;
    declared_registry_function(pack, &MODEL_SAVE)?;
    Ok(())
}

/// Check the receipt-verified declaration for fixed, private Model resolution.
/// Verify the actual Component ABI independently when it is invoked.
pub fn verify_legacy_model_resolve_declaration(pack: &VerifiedPack) -> Result<()> {
    declared_registry_component(pack, &MODEL_RESOLVE)?;
    declared_registry_function(pack, &MODEL_RESOLVE)?;
    Ok(())
}

/// Check the receipt-verified declaration for the fixed Conversation read.
/// The actual Component ABI is checked independently at invocation.
pub fn verify_legacy_conversation_read_declaration(pack: &VerifiedPack) -> Result<()> {
    declared_registry_component(pack, &CONVERSATION_GET)?;
    declared_registry_function(pack, &CONVERSATION_GET)?;
    Ok(())
}

/// Check the fixed saved receipt declaration independently from Conversation GET.
pub fn verify_legacy_conversation_receipt_declaration(pack: &VerifiedPack) -> Result<()> {
    declared_registry_component(pack, &CONVERSATION_RECEIPT)?;
    declared_registry_function(pack, &CONVERSATION_RECEIPT)?;
    Ok(())
}

/// Check the fixed completion confirmation declaration independently from the
/// read and receipt ports. Invocation also checks the executable ABI.
pub fn verify_legacy_conversation_confirmation_declaration(pack: &VerifiedPack) -> Result<()> {
    declared_registry_component(pack, &CONVERSATION_CONFIRMATION)?;
    declared_registry_function(pack, &CONVERSATION_CONFIRMATION)?;
    Ok(())
}

pub(super) struct LiveObservations {
    read: LegacyProviderRegistryRead,
    count: u8,
    limit: u8,
    clock_pending: bool,
}

impl<T: Send + 'static> NativeSetupStore<T> {
    /// Read each original Registry document only when the fixed planner has
    /// reached that read. The reader never receives a guest path or operation.
    pub async fn invoke_live_legacy_provider_prepare(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        request: &CoreValue,
        read: LegacyProviderRegistryRead,
    ) -> Result<CoreValue> {
        self.invoke_live_legacy_provider(
            pack,
            profile_id,
            request,
            LegacyProviderOperation::Prepare,
            read,
        )
        .await
    }

    /// Revalidate a frozen plan and its final prepare-save record using the
    /// original Registry. This Store cannot commit data or grant authority.
    pub async fn validate_live_legacy_provider_commit(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        request: &CoreValue,
        plan: &CoreValue,
        read: LegacyProviderRegistryRead,
    ) -> Result<CoreValue> {
        self.invoke_live_legacy_provider(
            pack,
            profile_id,
            request,
            LegacyProviderOperation::Validate(plan),
            read,
        )
        .await
    }

    /// Stage the exact legacy document for a trusted writer that supplies the
    /// fourth observation under the original Registry lock. The guest has no
    /// publication capability. The writer owns the final lease and CAS check.
    pub async fn stage_live_legacy_provider_commit(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        request: &CoreValue,
        plan: &CoreValue,
        read: LegacyProviderRegistryRead,
    ) -> Result<CoreValue> {
        self.invoke_live_legacy_provider(
            pack,
            profile_id,
            request,
            LegacyProviderOperation::Stage(plan),
            read,
        )
        .await
    }

    async fn invoke_live_legacy_provider(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        request: &CoreValue,
        operation: LegacyProviderOperation<'_>,
        read: LegacyProviderRegistryRead,
    ) -> Result<CoreValue> {
        let (export, plan) = match operation {
            LegacyProviderOperation::Prepare => (&PREPARE, None),
            LegacyProviderOperation::Validate(plan) => (&VALIDATE_COMMIT, Some(plan)),
            LegacyProviderOperation::Stage(plan) => (&STAGE_COMMIT, Some(plan)),
        };
        self.invoke_legacy_registry(
            pack,
            profile_id,
            export,
            RegistryInput::Provider {
                request,
                plan,
                read,
            },
        )
        .await
    }

    /// Project the saved Model Settings from the original owner's file bytes.
    /// The native caller distinguishes a physically absent file from JSON null.
    pub async fn list_legacy_model_settings(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        document: Option<&str>,
    ) -> Result<CoreValue> {
        self.invoke_legacy_registry(
            pack,
            profile_id,
            &MODEL_LIST,
            RegistryInput::ModelList(document),
        )
        .await
    }

    /// Resolve an identifier from the original owner's file without granting
    /// storage, Provider access, or credentials to the guest.
    /// Only the admitted native caller receives the private owner result.
    pub async fn resolve_legacy_model(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        document: Option<&str>,
        identifier: &str,
    ) -> Result<CoreValue> {
        self.invoke_legacy_registry(
            pack,
            profile_id,
            &MODEL_RESOLVE,
            RegistryInput::DocumentLookup {
                document,
                identifier,
            },
        )
        .await
    }

    /// Stage a Model Registry save from a trusted narrowed Provider snapshot
    /// and the original Model document while the native caller holds its lock.
    /// A successful no-op neither supplies a replacement nor observes a clock.
    pub async fn stage_live_legacy_model_save(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        request: &CoreValue,
        provider: &CoreValue,
        read: LegacyRegistryRead,
    ) -> Result<CoreValue> {
        self.invoke_legacy_registry(
            pack,
            profile_id,
            &MODEL_SAVE,
            RegistryInput::ModelSave {
                request,
                provider,
                read,
            },
        )
        .await
    }

    /// Project one original Conversation record without granting storage to the guest.
    pub async fn get_legacy_conversation(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        document: Option<&str>,
        conversation_id: &str,
    ) -> Result<CoreValue> {
        self.invoke_legacy_registry(
            pack,
            profile_id,
            &CONVERSATION_GET,
            RegistryInput::DocumentLookup {
                document,
                identifier: conversation_id,
            },
        )
        .await
    }

    /// Read one original saved receipt without exposing storage or settlement.
    pub async fn get_legacy_saved_receipt(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        document: Option<&str>,
        turn_id: &str,
    ) -> Result<CoreValue> {
        self.invoke_legacy_registry(
            pack,
            profile_id,
            &CONVERSATION_RECEIPT,
            RegistryInput::DocumentLookup {
                document,
                identifier: turn_id,
            },
        )
        .await
    }

    /// Stage a completion confirmation against the original Conversation owner
    /// document and the durable Turn record read by the host. The native caller
    /// owns any publication of the staged replacement.
    pub async fn stage_legacy_conversation_confirmation(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        document: &str,
        turn: &str,
    ) -> Result<CoreValue> {
        let reply = self
            .invoke_legacy_registry(
                pack,
                profile_id,
                &CONVERSATION_CONFIRMATION,
                RegistryInput::ConversationConfirmation { document, turn },
            )
            .await?;
        validate_conversation_confirmation_reply(&reply)?;
        Ok(reply)
    }

    async fn invoke_legacy_registry(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        export: &RegistryExport,
        input: RegistryInput<'_>,
    ) -> Result<CoreValue> {
        let store = self
            .store
            .as_mut()
            .context("native Setup Store was retired")?;
        let state = store.data_mut();
        ensure_active(state)?;
        state.claim_component()?;

        let artifact = declared_registry_component(pack, export)?;
        let function = declared_registry_function(pack, export)?;
        let abi_matches = match &input {
            RegistryInput::Provider { plan, .. } => {
                request_abi(&function.params[0].1)
                    && (plan.is_none() || function.params[1].1 == WitType::Json)
                    && function
                        .params
                        .last()
                        .is_some_and(|(_, ty)| *ty == WitType::CallbackHandle)
            }
            RegistryInput::ModelList(_) => optional_string(&function.params[0].1),
            RegistryInput::ModelSave { .. }
            | RegistryInput::DocumentLookup { .. }
            | RegistryInput::ConversationConfirmation { .. } => true,
        };
        ensure!(
            abi_matches,
            "legacy Registry declared parameter ABI differs"
        );

        let owners = state.owners.clone();
        let profile = profile_input(profile_id, &owners)?;
        let length = artifact.len();
        // Retain conservative native compilation/instance metadata funding
        // until the actual Store owning the compiled code has been destroyed.
        let compilation_bytes = length * 8 + 1024 * 1024;
        self.component_control = Some(
            state
                .invocation
                .control()
                .reserve(ControlKind::Load, compilation_bytes)?,
        );
        let bytes = read_component(artifact, &owners)?;
        ensure_active(store.data_mut())?;
        let engine = self._engine.engine();
        let component = Component::new(engine, bytes.as_bytes())
            .map_err(anyhow::Error::from)
            .context("compile verified legacy Provider component")?;
        drop(bytes);
        let image = component.image_range();
        ensure!(
            image.end.addr() - image.start.addr() <= usize::try_from(compilation_bytes)?,
            "legacy Provider compiled image exceeds its envelope"
        );
        ensure_active(store.data_mut())?;
        // Validate both the hashed WIT shape and the actual executable's type
        // before instantiating anything or accepting an invocation of the host.
        let linker = registry_linker(engine, &component, &function.params, export, &owners)?;
        let model_parameters;
        let two_parameters;
        let without_plan;
        let with_plan;
        let (values, read): (&[Val], _) = match input {
            RegistryInput::Provider {
                request,
                plan,
                read,
            } => {
                let request = core_to_val(request, &function.params[0].1, &owners)?;
                let observe = observe_handle(&owners)?;
                let values: &[Val] = if let Some(plan) = plan {
                    with_plan = [
                        request,
                        core_to_val(plan, &function.params[1].1, &owners)?,
                        observe,
                    ];
                    &with_plan
                } else {
                    without_plan = [request, observe];
                    &without_plan
                };
                (values, Some(read))
            }
            RegistryInput::ModelList(document) => {
                model_parameters = [optional_document(document, &owners)?];
                (&model_parameters, None)
            }
            RegistryInput::DocumentLookup {
                document,
                identifier,
            } => {
                let id = BudgetedString::copy_from(identifier, owners.component(), 1)?;
                two_parameters = [optional_document(document, &owners)?, Val::String(id)];
                (&two_parameters, None)
            }
            RegistryInput::ConversationConfirmation { document, turn } => {
                ensure!(
                    turn.len() <= MAX_DOCUMENT_BYTES,
                    "legacy Conversation durable Turn record exceeds its limit"
                );
                two_parameters = [
                    raw_document(document, &owners)?,
                    Val::String(BudgetedString::copy_from(turn, owners.component(), 1)?),
                ];
                (&two_parameters, None)
            }
            RegistryInput::ModelSave {
                request,
                provider,
                read,
            } => {
                with_plan = [
                    core_to_val(request, &function.params[0].1, &owners)?,
                    core_to_val(provider, &function.params[1].1, &owners)?,
                    observe_handle(&owners)?,
                ];
                (&with_plan, Some(read))
            }
        };
        store.host_allocation_budget(owners.component().clone());
        let (reply, reads, clock_pending) =
            call_registry(store, &linker, &component, profile, values, read, export).await?;
        ensure_active(store.data_mut())?;
        let reply = decode_reply(
            reply.as_slice(),
            pack.manifest.permissions.wasm.output_bytes,
            export.error_subject,
            &owners,
        );
        ensure_active(store.data_mut())?;
        let reply = reply?;
        if returns_owner_result(export) {
            let owner = reply
                .get_key("owner_result")
                .context("missing legacy owner result")?;
            ensure!(
                matches!(reply.inspect(), CoreRef::Obj(fields) if fields.len() == 1)
                    && matches!(owner.inspect(), CoreRef::Str(value)
                        if value.starts_with('{') && value.ends_with('}')),
                "legacy owner result differs"
            );
        }
        let expected_clock_pending = match export.clock {
            RegistryClock::None | RegistryClock::Required => false,
            RegistryClock::ForReplacement => validate_model_save_reply(&reply)?,
        };
        ensure!(
            reads == export.reads && clock_pending == expected_clock_pending,
            "legacy Registry observations differ from the fixed operation"
        );
        Ok(reply)
    }
}

fn validate_conversation_confirmation_reply(reply: &CoreValue) -> Result<()> {
    let CoreRef::Obj(fields) = reply.inspect() else {
        bail!("legacy Conversation confirmation stage must return an object");
    };
    ensure!(
        fields.len() == 2,
        "legacy Conversation confirmation stage fields differ"
    );
    let owner = fields
        .get("owner_result")
        .context("missing legacy Conversation confirmation owner result")?;
    ensure!(
        matches!(owner.inspect(), CoreRef::Str(value) if value.starts_with('{') && value.ends_with('}')),
        "legacy Conversation confirmation owner result must enclose an object"
    );
    let document = fields
        .get("next_document")
        .context("missing legacy Conversation confirmation next document")?;
    match document.inspect() {
        CoreRef::Null => Ok(()),
        CoreRef::Str(value) if value.starts_with('{') && value.ends_with('}') => Ok(()),
        _ => bail!("legacy Conversation confirmation next document differs"),
    }
}

fn validate_model_save_reply(reply: &CoreValue) -> Result<bool> {
    let CoreRef::Obj(fields) = reply.inspect() else {
        bail!("legacy Model Registry stage must return an object");
    };
    ensure!(
        fields.len() == 3,
        "legacy Model Registry stage fields differ"
    );
    let result = reply
        .get_key("result")
        .context("missing Model Settings result")?;
    ensure!(
        matches!(result.inspect(), CoreRef::Obj(_)),
        "legacy Model Settings result must be an object"
    );
    let owner = reply
        .get_key("owner_result")
        .context("missing Model owner result")?;
    // The owner reply remains opaque to the Core DATA codec.
    ensure!(
        matches!(owner.inspect(), CoreRef::Str(value) if value.starts_with('{') && value.ends_with('}')),
        "legacy Model owner result must enclose an object"
    );
    let document = reply
        .get_key("document")
        .context("missing Model Registry stage document")?;
    match document.inspect() {
        CoreRef::Null => Ok(true),
        CoreRef::Str(value) if value.ends_with('\n') => Ok(false),
        _ => bail!("legacy Model Registry stage document differs"),
    }
}

fn declared_registry_component<'a>(
    pack: &'a VerifiedPack,
    export: &RegistryExport,
) -> Result<&'a StagedArtifact> {
    ensure!(
        pack.manifest.id.as_str() == export.pack,
        "legacy Registry Pack identity differs"
    );
    let RuntimeSpec::Wasm {
        component,
        core_modules,
        ..
    } = &pack.manifest.runtime
    else {
        bail!("legacy Registry Pack must use Wasm");
    };
    ensure!(
        core_modules.is_empty(),
        "legacy Registry Pack declares external core modules"
    );
    let artifact = pack
        .files
        .get(component)
        .context("verified legacy Registry component missing")?;
    ensure!(
        !artifact.is_empty() && artifact.len() <= MAX_COMPONENT_BYTES,
        "legacy Registry component size exceeds its envelope"
    );
    Ok(artifact)
}

fn declared_registry_function<'a>(
    pack: &'a VerifiedPack,
    export: &RegistryExport,
) -> Result<&'a WitFunction> {
    let shape = ShapeRef::parse(export.shape)?;
    ensure!(
        pack.manifest.ports.provides.iter().any(|port| {
            port.shape == shape.name && port.version == shape.version && !port.optional
        }),
        "legacy Registry Pack does not provide the fixed port"
    );
    let mut shapes = pack
        .shapes
        .iter()
        .filter(|definition| definition.reference == shape);
    let definition = shapes.next().context("legacy Registry shape missing")?;
    ensure!(
        shapes.next().is_none()
            && definition.kind == ShapeKind::Port
            && definition.functions.len() == 1
            && definition.streams.is_empty()
            && definition.callbacks.is_empty(),
        "legacy Registry shape differs"
    );
    let function = definition
        .functions
        .iter()
        .find_map(|(operation, function)| {
            (operation.as_str() == export.function).then_some(function)
        })
        .context("legacy Registry operation missing")?;
    ensure!(
        function
            .params
            .iter()
            .map(|(name, _)| name.as_str())
            .eq(export.parameters.iter().copied())
            && valid_declared_result(function.result.as_ref()),
        "legacy Registry declared function ABI differs"
    );
    if export.shape == MODEL_SAVE.shape {
        ensure!(
            function.params[0].1 == WitType::Json
                && function.params[1].1 == WitType::Json
                && function.params[2].1 == WitType::CallbackHandle,
            "legacy Model Registry save parameter ABI differs"
        );
    }
    if returns_owner_result(export) {
        ensure!(
            optional_string(&function.params[0].1) && function.params[1].1 == WitType::String,
            "legacy owner lookup parameter ABI differs"
        );
    }
    if export.shape == CONVERSATION_CONFIRMATION.shape {
        ensure!(
            function.params.iter().all(|(_, ty)| *ty == WitType::String),
            "legacy Conversation confirmation parameter ABI differs"
        );
    }
    Ok(function)
}

fn returns_owner_result(export: &RegistryExport) -> bool {
    [
        CONVERSATION_GET.shape,
        CONVERSATION_RECEIPT.shape,
        MODEL_RESOLVE.shape,
    ]
    .contains(&export.shape)
}

fn optional_document(document: Option<&str>, owners: &crate::AllocationOwners) -> Result<Val> {
    let Some(document) = document else {
        return Ok(Val::Option(None));
    };
    Ok(Val::Option(Some(BudgetedBox::new(
        raw_document(document, owners)?,
        owners.component(),
        0,
    )?)))
}

fn raw_document(document: &str, owners: &crate::AllocationOwners) -> Result<Val> {
    ensure!(
        document.len() <= MAX_DOCUMENT_BYTES,
        "legacy owner document exceeds its limit"
    );
    Ok(Val::String(BudgetedString::copy_from(
        document,
        owners.component(),
        1,
    )?))
}

fn observe_handle(owners: &crate::AllocationOwners) -> Result<Val> {
    let owner = owners.component();
    let field = (
        BudgetedString::copy_from("id", owner, 1)?,
        Val::U64(OBSERVE_ID),
    );
    Ok(Val::Record(BudgetedVec::from_iter(
        [field].into_iter(),
        owner,
        0,
    )?))
}

fn profile_input(profile_id: &str, owners: &crate::AllocationOwners) -> Result<CoreBuffer> {
    let mut bytes = profile_id.bytes();
    ensure!(
        profile_id.len() <= 128
            && bytes.next().is_some_and(|b| b.is_ascii_alphanumeric())
            && bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'),
        "legacy Provider Profile identity is invalid"
    );
    Ok(CoreBuffer::copy_from(
        profile_id.as_bytes(),
        owners.graph(),
    )?)
}

pub(super) fn read_component(
    artifact: &tobkiri_manifest::source::StagedArtifact,
    owners: &crate::AllocationOwners,
) -> Result<CoreBuffer> {
    let length = usize::try_from(artifact.len())?;
    Ok(artifact.inspect(|input| {
        let mut failure = None;
        let bytes = CoreBuffer::write_exact(length, owners.graph(), |out| {
            if let Err(error) = input.read_exact(out) {
                failure = Some(error);
                return Err(AllocationError::Invalid);
            }
            Ok(())
        });
        if let Some(error) = failure {
            return Err(error.into());
        }
        bytes.map_err(|error| tobkiri_manifest::ManifestError::Invalid(error.to_string()))
    })?)
}

fn registry_linker<T: Send + 'static>(
    engine: &Engine,
    component: &Component,
    parameters: &[(String, WitType)],
    export: &RegistryExport,
    owners: &crate::AllocationOwners,
) -> Result<Linker<NativeState<T>>> {
    let ty = component.component_type();
    let flow = instance_type(ty.get_import(engine, FLOW).map(|item| item.ty))?;
    let interface = instance_type(ty.get_export(engine, export.interface).map(|item| item.ty))?;
    let function = function_type(
        interface
            .get_export(engine, export.function)
            .map(|item| item.ty),
    )?;
    let profile = function_type(flow.get_export(engine, PROFILE).map(|item| item.ty))?;
    ensure!(
        profile.params().len() == 0 && valid_result(&profile),
        "legacy Provider current-profile ABI differs"
    );
    ensure!(
        function.params().len() == parameters.len() && valid_result(&function),
        "legacy Provider prepare ABI differs"
    );
    for ((name, actual), (declared_name, declared_type)) in function.params().zip(parameters) {
        ensure!(
            name == declared_name && valid_parameter(&actual, declared_type),
            "legacy Provider prepare parameter ABI differs"
        );
    }
    if export.reads != 0 {
        let callback = function_type(flow.get_export(engine, CALLBACK).map(|item| item.ty))?;
        let mut arguments = callback.params();
        ensure!(
            arguments
                .next()
                .is_some_and(|(name, ty)| name == "c" && is_callback_handle(&ty))
                && arguments.next() == Some(("args", Type::String))
                && arguments.next().is_none()
                && valid_result(&callback),
            "legacy Provider observation callback ABI differs"
        );
    }
    let mut linker = Linker::new(engine);
    linker.define_unknown_imports_as_traps(component)?;
    // Replace only the exact flow instance, keeping the other import traps.
    // A nested linker instance replacement creates a fresh definition map.
    linker.allow_shadowing(true);
    let mut replacement = linker.instance(FLOW)?;
    for (name, import) in flow.exports(engine) {
        match import.ty {
            ComponentItem::ComponentFunc(_) if name == PROFILE => {
                replacement.func_new_owned(
                    name,
                    owners.component(),
                    |mut cx, _, args, output| {
                        let state = cx.data_mut();
                        ensure_active(state).map_err(wasmtime::Error::from_anyhow)?;
                        let ([], [result]) = (args, output) else {
                            return Err(wasmtime::Error::msg(
                                "legacy Provider current-profile argument mismatch",
                            ));
                        };
                        let profile = state.registry_profile.take().ok_or_else(|| {
                            wasmtime::Error::msg(
                                "legacy Provider current-profile is outside its admitted call",
                            )
                        })?;
                        let owner = state.owners.component();
                        let text = profile.as_str().map_err(|_| {
                            wasmtime::Error::msg("legacy Provider Profile is not UTF-8")
                        })?;
                        let value = Val::String(BudgetedString::copy_from(text, owner, 1)?);
                        *result = Val::Result(Ok(Some(BudgetedBox::new(value, owner, 0)?)));
                        ensure_active(state).map_err(wasmtime::Error::from_anyhow)
                    },
                )?;
            }
            ComponentItem::ComponentFunc(_) if name == CALLBACK && export.reads != 0 => {
                replacement.func_new_owned(
                    name,
                    owners.component(),
                    |mut cx, _, args, output| {
                        observe_registry(cx.data_mut(), args, output)
                            .map_err(wasmtime::Error::from_anyhow)
                    },
                )?;
            }
            ComponentItem::ComponentFunc(_) => {
                replacement.func_new_owned(name, owners.component(), |_, _, _, _| {
                    Err(wasmtime::Error::msg(
                        "legacy Provider flow capability is unavailable",
                    ))
                })?;
            }
            ComponentItem::Type(_) => {}
            _ => bail!("legacy Provider flow import contains an unsupported item"),
        }
    }
    Ok(linker)
}

fn observe_registry<T>(state: &mut NativeState<T>, args: &[Val], output: &mut [Val]) -> Result<()> {
    ensure_active(state)?;
    let ([Val::Record(handle), Val::String(arguments)], [result]) = (args, output) else {
        bail!("legacy Provider observation arguments differ");
    };
    ensure!(
        matches!(handle.as_slice(), [(name, Val::U64(OBSERVE_ID))] if name.as_str() == "id")
            && state.registry_profile.is_none(),
        "legacy Provider observation is outside its admitted call"
    );
    let mut observations = state
        .provider_prepare_reads
        .take()
        .context("legacy Provider observations are unavailable")?;
    if arguments.as_str() == "\"clock\"" {
        ensure!(
            observations.clock_pending && observations.count == observations.limit,
            "legacy Provider clock is outside its admitted mutation staging"
        );
        let seconds = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
        let owner = state.owners.component();
        let value = Val::String(BudgetedString::copy_from(&seconds.to_string(), owner, 1)?);
        *result = Val::Result(Ok(Some(BudgetedBox::new(value, owner, 0)?)));
        ensure_active(state)?;
        observations.clock_pending = false;
        state.provider_prepare_reads = Some(observations);
        return Ok(());
    }
    ensure!(
        arguments.as_str() == "null" && observations.count < observations.limit,
        "legacy Provider observations are exhausted"
    );
    let document = (observations.read)(state.owners.graph())
        .map_err(|_| anyhow::anyhow!("legacy Provider Registry read failed"))?;
    ensure_active(state)?;
    let owner = state.owners.component();
    let value = match document {
        Some(document) => {
            ensure!(
                document.len() <= MAX_DOCUMENT_BYTES,
                "legacy Provider observation exceeds its limit"
            );
            // JSON quoting keeps an absent file distinct from a present raw
            // document containing null, and preserves raw order and syntax.
            let mut encoded = CoreBuffer::builder(state.owners.graph(), document.len() * 6 + 2);
            let text = document.as_str()?;
            encoded.write_chunk(document.len() * 6 + 2, |output| {
                let mut writer = io::Cursor::new(output);
                serde_json::to_writer(&mut writer, text).map_err(|_| AllocationError::Invalid)?;
                usize::try_from(writer.position()).map_err(|_| AllocationError::Overflow)
            })?;
            let encoded = encoded.finish()?;
            BudgetedString::copy_from(encoded.as_str()?, owner, 1)?
        }
        None => BudgetedString::copy_from("null", owner, 1)?,
    };
    *result = Val::Result(Ok(Some(BudgetedBox::new(Val::String(value), owner, 0)?)));
    ensure_active(state)?;
    observations.count += 1;
    state.provider_prepare_reads = Some(observations);
    Ok(())
}

fn instance_type(item: Option<ComponentItem>) -> Result<ComponentInstance> {
    match item {
        Some(ComponentItem::ComponentInstance(instance)) => Ok(instance),
        _ => bail!("legacy Provider instance ABI differs"),
    }
}

fn function_type(item: Option<ComponentItem>) -> Result<ComponentFunc> {
    match item {
        Some(ComponentItem::ComponentFunc(function)) if !function.async_() => Ok(function),
        _ => bail!("legacy Provider function ABI differs"),
    }
}

fn valid_result(function: &ComponentFunc) -> bool {
    let mut results = function.results();
    if results.len() != 1 {
        return false;
    }
    let Some(Type::Result(result)) = results.next() else {
        return false;
    };
    let Some(Type::Record(error)) = result.err() else {
        return false;
    };
    let mut fields = error.fields();
    let (Some(code), Some(message)) = (fields.next(), fields.next()) else {
        return false;
    };
    result.ok() == Some(Type::String)
        && code.name == "code"
        && matches!(code.ty, Type::Enum(cases) if cases.names().eq(ERROR_CODES))
        && message.name == "message"
        && message.ty == Type::String
        && fields.next().is_none()
}

fn optional_string(ty: &WitType) -> bool {
    matches!(ty, WitType::Option(inner) if **inner == WitType::String)
}

fn valid_declared_result(result: Option<&WitType>) -> bool {
    let Some(WitType::Result {
        ok: Some(ok),
        err: Some(error),
    }) = result
    else {
        return false;
    };
    let WitType::Record(fields) = error.as_ref() else {
        return false;
    };
    let [code, message] = fields.as_slice() else {
        return false;
    };
    **ok == WitType::Json
        && code.name == "code"
        && matches!(&code.ty, WitType::Enum(cases) if cases.iter().map(String::as_str).eq(ERROR_CODES))
        && message.name == "message"
        && message.ty == WitType::String
}

fn request_abi(ty: &WitType) -> bool {
    let WitType::Record(fields) = ty else {
        return false;
    };
    let [name, endpoint, catalog, display, capabilities] = fields.as_slice() else {
        return false;
    };
    name.name == "connection-name"
        && name.ty == WitType::String
        && endpoint.name == "endpoint"
        && endpoint.ty == WitType::String
        && catalog.name == "catalog-provider-id"
        && optional_string(&catalog.ty)
        && display.name == "display-name"
        && optional_string(&display.ty)
        && capabilities.name == "capabilities"
        && matches!(&capabilities.ty, WitType::Option(list) if matches!(list.as_ref(), WitType::List(inner) if **inner == WitType::String))
}

fn valid_parameter(actual: &Type, declared: &WitType) -> bool {
    match (actual, declared) {
        (Type::String, WitType::String | WitType::Json) => true,
        (Type::Option(actual), WitType::Option(declared)) => {
            valid_parameter(&actual.ty(), declared)
        }
        (Type::List(actual), WitType::List(declared)) => valid_parameter(&actual.ty(), declared),
        (actual, WitType::CallbackHandle) => is_callback_handle(actual),
        (Type::Record(actual), WitType::Record(declared)) => {
            let fields = actual.fields();
            fields.len() == declared.len()
                && fields
                    .zip(declared)
                    .all(|(a, d)| a.name == d.name && valid_parameter(&a.ty, &d.ty))
        }
        _ => false,
    }
}

fn is_callback_handle(ty: &Type) -> bool {
    matches!(ty, Type::Record(record) if record.fields().map(|f| (f.name, f.ty)).eq([("id", Type::U64)]))
}

async fn call_registry<T: Send + 'static>(
    store: &mut Store<NativeState<T>>,
    linker: &Linker<NativeState<T>>,
    component: &Component,
    profile: CoreBuffer,
    parameters: &[Val],
    read: Option<LegacyProviderRegistryRead>,
    export: &RegistryExport,
) -> Result<(BudgetedVec<Val>, u8, bool)> {
    let instance = linker
        .instantiate_async(&mut *store, component)
        .await
        .map_err(anyhow::Error::from)
        .context("instantiate verified legacy Provider component")?;
    let interface = instance
        .get_export_index(&mut *store, None, export.interface)
        .context("legacy Provider configuration instance export missing")?;
    let function = instance
        .get_export_index(&mut *store, Some(&interface), export.function)
        .and_then(|index| instance.get_func(&mut *store, index))
        .context("legacy Provider configuration function export missing")?;
    ensure_active(store.data_mut())?;
    store.data_mut().registry_profile = Some(profile);
    let expects_clock = !matches!(export.clock, RegistryClock::None);
    store.data_mut().provider_prepare_reads = read.map(|read| LiveObservations {
        read,
        count: 0,
        limit: export.reads,
        clock_pending: expects_clock,
    });
    let result = function.call_async_owned(&mut *store, parameters).await;
    let read_profile = store.data_mut().registry_profile.take().is_none();
    let (reads, clock_pending) = store
        .data_mut()
        .provider_prepare_reads
        .take()
        .map_or((0, expects_clock), |read| (read.count, read.clock_pending));
    let result = result
        .map_err(anyhow::Error::from)
        .context("execute verified legacy Provider prepare")?;
    ensure!(
        read_profile,
        "legacy Provider prepare omitted current-profile"
    );
    Ok((result, reads, clock_pending))
}

fn decode_reply(
    values: &[Val],
    max_bytes: u64,
    subject: &'static str,
    owners: &crate::AllocationOwners,
) -> Result<CoreValue> {
    match values {
        [Val::Result(Ok(Some(value)))] => {
            let Val::String(text) = value.as_ref() else {
                bail!("legacy Provider prepare reply type differs")
            };
            ensure!(
                text.as_str().len() as u64 <= max_bytes,
                "legacy Provider prepare reply is too large"
            );
            Ok(CoreValue::parse_guest(
                text.as_str().as_bytes(),
                owners.graph(),
            )?)
        }
        [Val::Result(Err(Some(value)))] => {
            let Val::Record(fields) = value.as_ref() else {
                bail!("legacy Provider prepare error type differs")
            };
            let [(_, Val::Enum(code)), (_, Val::String(message))] = fields.as_slice() else {
                bail!("legacy Provider prepare error fields differ")
            };
            ensure!(
                code.as_str().len().saturating_add(message.as_str().len()) as u64 <= max_bytes,
                "legacy Provider prepare error is too large"
            );
            let code = ERROR_CODES
                .into_iter()
                .find(|candidate| *candidate == code.as_str())
                .context("legacy Provider prepare error code differs")?;
            Err(LegacyRegistryError { code, subject }.into())
        }
        _ => bail!("legacy Provider prepare reply differs"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tobkiri_canon::AllocationRequest;

    struct FixtureBudget;

    impl AllocationBudget for FixtureBudget {
        fn reserve(&self, _: AllocationRequest) -> Result<(), AllocationError> {
            Ok(())
        }

        fn release(&self, _: u64) {}
    }

    #[test]
    fn confirmation_requires_the_closed_reply_and_preserves_opaque_json() -> Result<()> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(FixtureBudget);
        let owner = r#"{"revision": 12345678901234567890, "detail": -0.0}"#;
        let document = r#"{"z": "会話", "a": []}"#;
        let encoded = serde_json::to_vec(&serde_json::json!({
            "owner_result": owner,
            "next_document": document,
        }))?;
        let staged = CoreValue::parse_guest(&encoded, &budget)?;
        validate_conversation_confirmation_reply(&staged)?;
        let result = staged.get_key("owner_result").unwrap();
        assert!(matches!(result.inspect(), CoreRef::Str(value) if value == owner));
        let next = staged.get_key("next_document").unwrap();
        assert!(matches!(next.inspect(), CoreRef::Str(value) if value == document));

        let no_op =
            CoreValue::parse_guest(br#"{"owner_result":"{}","next_document":null}"#, &budget)?;
        validate_conversation_confirmation_reply(&no_op)?;

        for invalid in [
            &br#"{"owner_result":"{}"}"#[..],
            br#"{"owner_result":{},"next_document":null}"#,
            br#"{"owner_result":"{}","next_document":{}}"#,
            br#"{"owner_result":"{}","next_document":"null"}"#,
            br#"{"owner_result":"{}","next_document":null,"extra":null}"#,
        ] {
            let invalid = CoreValue::parse_guest(invalid, &budget)?;
            assert!(validate_conversation_confirmation_reply(&invalid).is_err());
        }
        Ok(())
    }
}
