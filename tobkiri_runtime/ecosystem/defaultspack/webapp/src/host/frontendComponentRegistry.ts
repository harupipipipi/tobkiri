import type { ComponentType } from "react";

import type {
  FrontendCatalog,
  VerifiedFrontendContribution,
} from "./frontendContracts";

export const FRONTEND_COMPONENT_API_VERSION = "rumi.frontend.component.v1";
export const UNSUPPORTED_COMPONENT_ID = "rumi.ui.unsupported";

export type FrontendComponentProps = Record<string, unknown>;

export type FrontendComponentRegistration = {
  componentId: string;
  apiVersion: typeof FRONTEND_COMPONENT_API_VERSION;
  supportedSlots: string[];
  propsSchema: Record<string, unknown>;
  dataContract?: string | null;
  actionContract?: string | null;
  fallbackComponentId: string;
  ownerPackId: string;
  trust: "builtin" | "backend_verified_pack";
  renderer?: ComponentType<FrontendComponentProps>;
  contribution?: VerifiedFrontendContribution;
};

export type FrontendComponentBinding = {
  componentId: string;
  apiVersion: string;
  slot: string;
  props: FrontendComponentProps;
  dataContract?: string | null;
  actionContract?: string | null;
};

export type FrontendComponentDiagnostic = {
  code: string;
  message: string;
  componentId: string;
  ownerPackId?: string;
};

export type FrontendComponentResolution = {
  registration: FrontendComponentRegistration;
  props: FrontendComponentProps;
  diagnostic: FrontendComponentDiagnostic | null;
  usedFallback: boolean;
};

export class FrontendComponentRegistry {
  private readonly registrations = new Map<string, FrontendComponentRegistration>();
  private readonly registrationDiagnostics: FrontendComponentDiagnostic[] = [];

  register(registration: FrontendComponentRegistration): boolean {
    const existing = this.registrations.get(registration.componentId);
    if (existing) {
      this.registrationDiagnostics.push({
        code: "frontend_component_collision",
        message: `Component ${registration.componentId} is already registered by ${existing.ownerPackId}.`,
        componentId: registration.componentId,
        ownerPackId: registration.ownerPackId,
      });
      return false;
    }
    if (
      registration.apiVersion !== FRONTEND_COMPONENT_API_VERSION
      || registration.supportedSlots.length === 0
      || !registration.fallbackComponentId
      || !isRecord(registration.propsSchema)
    ) {
      this.registrationDiagnostics.push({
        code: "frontend_component_registration_invalid",
        message: `Component ${registration.componentId} has an invalid registry contract.`,
        componentId: registration.componentId,
        ownerPackId: registration.ownerPackId,
      });
      return false;
    }
    this.registrations.set(registration.componentId, registration);
    return true;
  }

  diagnostics(): FrontendComponentDiagnostic[] {
    return [...this.registrationDiagnostics];
  }

  resolve(binding: FrontendComponentBinding): FrontendComponentResolution {
    const requested = this.registrations.get(binding.componentId);
    if (!requested) {
      return this.fallback(binding, "frontend_component_unknown", `Unknown component: ${binding.componentId}.`);
    }
    if (binding.apiVersion !== requested.apiVersion) {
      return this.fallback(
        binding,
        "frontend_component_version_mismatch",
        `Component ${binding.componentId} does not support ${binding.apiVersion}.`,
        requested,
      );
    }
    if (!requested.supportedSlots.includes(binding.slot)) {
      return this.fallback(
        binding,
        "frontend_component_slot_mismatch",
        `Component ${binding.componentId} cannot render in slot ${binding.slot}.`,
        requested,
      );
    }
    if (!contractMatches(binding.actionContract, requested.actionContract)) {
      return this.fallback(
        binding,
        "frontend_component_action_contract_mismatch",
        `Component ${binding.componentId} requested an undeclared action contract.`,
        requested,
      );
    }
    if (!contractMatches(binding.dataContract, requested.dataContract)) {
      return this.fallback(
        binding,
        "frontend_component_data_contract_mismatch",
        `Component ${binding.componentId} requested an undeclared data contract.`,
        requested,
      );
    }
    const propsError = validateSchemaValue(binding.props, requested.propsSchema, "props");
    if (propsError) {
      return this.fallback(
        binding,
        "frontend_component_props_invalid",
        `Component ${binding.componentId} rejected ${propsError}.`,
        requested,
      );
    }
    return {
      registration: requested,
      props: binding.props,
      diagnostic: null,
      usedFallback: false,
    };
  }

  private fallback(
    binding: FrontendComponentBinding,
    code: string,
    message: string,
    requested?: FrontendComponentRegistration,
  ): FrontendComponentResolution {
    const fallbackId = requested?.fallbackComponentId || UNSUPPORTED_COMPONENT_ID;
    const fallback = this.registrations.get(fallbackId)
      ?? this.registrations.get(UNSUPPORTED_COMPONENT_ID);
    if (!fallback) {
      throw new Error("frontend component registry has no safe fallback");
    }
    return {
      registration: fallback,
      props: { componentId: binding.componentId, message },
      diagnostic: {
        code,
        message,
        componentId: binding.componentId,
        ownerPackId: requested?.ownerPackId,
      },
      usedFallback: true,
    };
  }
}

export function registerVerifiedPackComponents(
  registry: FrontendComponentRegistry,
  catalog: FrontendCatalog,
): void {
  for (const contribution of catalog.contributions) {
    if (
      contribution.kind !== "component"
      || contribution.mode !== "same_origin_builtin"
      || !contribution.component_id
      || contribution.api_version !== FRONTEND_COMPONENT_API_VERSION
      || !Array.isArray(contribution.supported_slots)
      || !isRecord(contribution.props_schema)
      || !contribution.fallback_component_id
      || !contribution.module
      || catalog.quarantined_pack_ids.includes(contribution.owner_pack_id)
      || contribution.resolved_plan_hash !== catalog.plan_hash
    ) {
      continue;
    }
    registry.register({
      componentId: contribution.component_id,
      apiVersion: FRONTEND_COMPONENT_API_VERSION,
      supportedSlots: contribution.supported_slots,
      propsSchema: contribution.props_schema,
      dataContract: contribution.data_contract,
      actionContract: contribution.action_contract,
      fallbackComponentId: contribution.fallback_component_id,
      ownerPackId: contribution.owner_pack_id,
      trust: "backend_verified_pack",
      contribution,
    });
  }
}

export function parseFrontendComponentBinding(
  view: Record<string, unknown> | null | undefined,
): FrontendComponentBinding | null {
  if (!view || view.type !== "component") return null;
  const componentId = boundedString(view.component_id, 160);
  const apiVersion = boundedString(view.api_version, 80);
  const slot = boundedString(view.slot, 80);
  const props = isRecord(view.props) ? view.props : null;
  if (!componentId || !apiVersion || !slot || !props) return null;
  return {
    componentId,
    apiVersion,
    slot,
    props,
    dataContract: optionalBoundedString(view.data_contract, 256),
    actionContract: optionalBoundedString(view.action_contract, 256),
  };
}

function contractMatches(requested: string | null | undefined, declared: string | null | undefined): boolean {
  return requested == null || requested === declared;
}

function validateSchemaValue(value: unknown, schema: Record<string, unknown>, path: string): string | null {
  if (Array.isArray(schema.enum) && !schema.enum.some((item) => Object.is(item, value))) {
    return `${path} outside enum`;
  }
  if (hasOwn(schema, "const") && !Object.is(schema.const, value)) {
    return `${path} const mismatch`;
  }
  const type = schema.type;
  if (type === "object") {
    if (!isRecord(value)) return `${path} type`;
    const properties = isRecord(schema.properties) ? schema.properties : {};
    const required = Array.isArray(schema.required)
      ? schema.required.filter((item): item is string => typeof item === "string")
      : [];
    for (const key of required) {
      if (!hasOwn(value, key)) return `${path}.${key} required`;
    }
    if (schema.additionalProperties === false) {
      const unknown = Object.keys(value).find((key) => !hasOwn(properties, key));
      if (unknown) return `${path}.${unknown} undeclared`;
    }
    for (const [key, propertySchema] of Object.entries(properties)) {
      if (!hasOwn(value, key) || !isRecord(propertySchema)) continue;
      const error = validateSchemaValue(value[key], propertySchema, `${path}.${key}`);
      if (error) return error;
    }
    return null;
  }
  if (type === "array") {
    if (!Array.isArray(value)) return `${path} type`;
    if (typeof schema.minItems === "number" && value.length < schema.minItems) return `${path} minItems`;
    if (typeof schema.maxItems === "number" && value.length > schema.maxItems) return `${path} maxItems`;
    if (isRecord(schema.items)) {
      for (const [index, item] of value.entries()) {
        const error = validateSchemaValue(item, schema.items, `${path}[${index}]`);
        if (error) return error;
      }
    }
    return null;
  }
  if (type === "string") {
    if (typeof value !== "string") return `${path} type`;
    if (typeof schema.minLength === "number" && value.length < schema.minLength) return `${path} minLength`;
    if (typeof schema.maxLength === "number" && value.length > schema.maxLength) return `${path} maxLength`;
    return null;
  }
  if (type === "integer") return Number.isInteger(value) ? null : `${path} type`;
  if (type === "number") return typeof value === "number" && Number.isFinite(value) ? null : `${path} type`;
  if (type === "boolean") return typeof value === "boolean" ? null : `${path} type`;
  if (type === "null") return value === null ? null : `${path} type`;
  return `${path} schema type`;
}

function boundedString(value: unknown, maxLength: number): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized && normalized.length <= maxLength ? normalized : null;
}

function optionalBoundedString(value: unknown, maxLength: number): string | null {
  if (value == null) return null;
  return boundedString(value, maxLength);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasOwn(value: object, key: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}
