/* eslint-disable */
// GENERATED FILE. Do not edit by hand.
// Source: tobkiri_protocol/schemas/defaults_setup_v4.schema.json
// Raw source digest: sha256:96cf4d6abbbbaff5b31f4dfcd287f7fb25266c497bcc6a599daca1db320520c7

export const DEFAULTS_SETUP_KEYS = ["setup_api_version","state","denial_diagnostic","packs","recommended_default_profile","required_transaction"] as const;
export const DEFAULTS_PROFILE_KEYS = ["available","profile_id","name","base_pack","shell","pack_ids","packs","conversation_provider","confirmation"] as const;
export const DEFAULTS_PROFILE_SHELL_KEYS = ["provider_id","contract_id"] as const;
export const DEFAULTS_PACK_KEYS = ["pack_id","display_name"] as const;
export const DEFAULTS_CONFIRMATION_KEYS = ["confirmation_api_version","operation_id","profile_id","catalog_revision","profile_revision","plan_digest","authority_snapshot_digest","security_epoch","base","shell","bindings","confirmation_digest"] as const;
export const DEFAULTS_BASE_KEYS = ["pack_id","artifact_digest","definition_digest"] as const;
export const DEFAULTS_CONFIRMED_SHELL_KEYS = ["provider_id","pack_id","artifact_digest","executable_artifact_digest","contract_id","definition_digest"] as const;
export const DEFAULTS_BINDING_KEYS = ["caller_function_id","pack_id","artifact_digest","function_principal","contract_id","operation_id","domain_kind","executable_catalog_digest","variant_id","platform","architecture","runtime_abi","backend","execution_kind","authority_reference","requested_scope_digest","adapter_digests"] as const;
export const DEFAULTS_BINDING_OPTIONAL_KEYS = ["authority_mode"] as const;
export const DEFAULTS_FUNCTION_PRINCIPAL_KEYS = ["parent_artifact_digest","function_implementation_digest","function_id","contract_revision_digest","operation_id"] as const;
export const DEFAULTS_SETUP_STATES = ["review_required","active","activation_denied"] as const;
export const DEFAULTS_BINDING_DOMAIN_KINDS = ["wasm_component","pack_vm","dedicated_process","remote"] as const;
export const DEFAULTS_BINDING_EXECUTION_KINDS = ["wasm","pack_vm","host_extension","remote"] as const;
export const DEFAULTS_REQUIRED_TRANSACTION = ["catalog.verify","profile.resolve","authority.snapshot","activation.prepare","activation.commit","runtime.capture"] as const;
