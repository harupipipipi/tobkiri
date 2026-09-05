import type { ModelProfile, SettingsSection, UICatalog } from "../../lib/api";
import { selectedApisForModel } from "../../lib/modelApiRoutes";

export type SettingsProfileSource = "settings" | "catalog" | "model";
export type SettingsProfileReadiness = "ready" | "local" | "needs_connection" | "blocked" | "unknown";

export type SettingsProfileRecord = {
  id: string;
  name: string;
  description: string;
  role: string;
  providerId: string;
  modelId: string;
  routeRefs: string[];
  source: SettingsProfileSource;
  sourceLabel: string;
  editable: boolean;
  managed: boolean;
  active: boolean;
  default: boolean;
  favorite: boolean;
  readiness: SettingsProfileReadiness;
  readinessReason: string;
  capabilityTags: string[];
  raw: Record<string, unknown>;
  collectionIndex: number | null;
};

export type EditableSettingsProfileCollection = {
  sectionId: string;
  fieldId: string;
  records: Record<string, unknown>[];
  idField: string;
  nameField: string;
  activeFieldId: string | null;
  defaultFieldId: string | null;
};

export type SettingsProfileWorkspace = {
  profiles: SettingsProfileRecord[];
  activeProfileId: string;
  defaultProfileId: string;
  editableCollection: EditableSettingsProfileCollection | null;
  modelRoutesText: string;
};

const PROFILE_SECTION_IDS = ["profiles", "profile", "adaptive"] as const;
const PROFILE_COLLECTION_KEYS = ["profiles", "items", "definitions", "presets", "operating_profiles"] as const;
const PROFILE_ACTIVE_KEYS = ["active_profile", "active_profile_id", "selected_profile_id", "profile_id"] as const;
const PROFILE_DEFAULT_KEYS = ["default_profile", "default_profile_id"] as const;

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function recordList(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item))
    : [];
}

function stringValue(...values: unknown[]): string {
  for (const value of values) {
    const normalized = typeof value === "string"
      ? value.trim()
      : typeof value === "number" && Number.isFinite(value)
        ? String(value)
        : "";
    if (normalized) return normalized;
  }
  return "";
}

function displayStringValue(...values: unknown[]): string {
  for (const value of values) {
    const scalar = stringValue(value);
    if (scalar) return scalar;
    const record = recordValue(value);
    const localized = stringValue(
      record.ja,
      record.en,
      record.default,
      record.display_name,
      record.displayName,
      record.label,
      record.name,
      record.title,
      record.value,
    );
    if (localized) return localized;
  }
  return "";
}

function stringList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item ?? "").trim()).filter(Boolean);
  if (typeof value === "string") return value.split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean);
  return [];
}

function booleanValue(...values: unknown[]): boolean {
  return values.some((value) => value === true || value === "true" || value === 1 || value === "1");
}

function hasOwn(record: object, key: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function credentialReferenceId(value: unknown): string {
  if (typeof value === "string") return value.trim();
  const record = recordValue(value);
  return stringValue(record.credential_id, record.credential_ref, record.id, record.ref);
}

function fieldExists(settingsSections: SettingsSection[], sectionId: string, fieldId: string): boolean {
  return Boolean(settingsSections.find((section) => section.id === sectionId)?.fields.some((field) => field.id === fieldId));
}

function profileIdField(record: Record<string, unknown>): string {
  if (hasOwn(record, "profile_id")) return "profile_id";
  if (hasOwn(record, "id")) return "id";
  if (hasOwn(record, "key")) return "key";
  return "profile_id";
}

function profileNameField(record: Record<string, unknown>): string {
  if (hasOwn(record, "display_name")) return "display_name";
  if (hasOwn(record, "name")) return "name";
  if (hasOwn(record, "label")) return "label";
  return "display_name";
}

function findEditableCollection(
  settingsSections: SettingsSection[],
  settingsValues: Record<string, Record<string, unknown>>,
): EditableSettingsProfileCollection | null {
  for (const sectionId of PROFILE_SECTION_IDS) {
    const sectionValues = settingsValues[sectionId];
    if (!sectionValues) continue;
    for (const fieldId of PROFILE_COLLECTION_KEYS) {
      const records = recordList(sectionValues[fieldId]);
      if (!records.length && !Array.isArray(sectionValues[fieldId])) continue;
      if (!fieldExists(settingsSections, sectionId, fieldId)) continue;
      const template = records[0] ?? {};
      const activeFieldId = PROFILE_ACTIVE_KEYS.find((candidate) => (
        hasOwn(sectionValues, candidate) && fieldExists(settingsSections, sectionId, candidate)
      )) ?? null;
      const defaultFieldId = PROFILE_DEFAULT_KEYS.find((candidate) => (
        hasOwn(sectionValues, candidate) && fieldExists(settingsSections, sectionId, candidate)
      )) ?? null;
      return {
        sectionId,
        fieldId,
        records,
        idField: profileIdField(template),
        nameField: profileNameField(template),
        activeFieldId,
        defaultFieldId,
      };
    }
  }
  return null;
}

function activeProfileIdFromValues(
  settingsValues: Record<string, Record<string, unknown>>,
  catalog: UICatalog | null,
  activeModelProfileId?: string | null,
): string {
  const candidates: unknown[] = [];
  for (const sectionId of PROFILE_SECTION_IDS) {
    const section = settingsValues[sectionId] ?? {};
    for (const key of PROFILE_ACTIVE_KEYS) candidates.push(section[key]);
  }
  candidates.push(
    activeModelProfileId,
    settingsValues.models?.active_profile,
    settingsValues.models?.selected_profile_id,
    settingsValues.models?.preferred_model,
    catalog?.settings?.values?.profiles?.active_profile,
    catalog?.settings?.values?.models?.preferred_model,
  );
  return stringValue(...candidates);
}

function defaultProfileIdFromValues(
  settingsValues: Record<string, Record<string, unknown>>,
  catalog: UICatalog | null,
): string {
  const candidates: unknown[] = [];
  for (const sectionId of PROFILE_SECTION_IDS) {
    const section = settingsValues[sectionId] ?? {};
    for (const key of PROFILE_DEFAULT_KEYS) candidates.push(section[key]);
  }
  candidates.push(catalog?.agent_service?.default_profile, settingsValues.models?.preferred_model);
  return stringValue(...candidates);
}

function routeTextFromValues(settingsValues: Record<string, Record<string, unknown>>): string {
  const modelValues = settingsValues.models ?? {};
  return stringValue(modelValues.model_api_routes, modelValues.api_routes, modelValues.routes);
}

function favoriteIdsFromValues(settingsValues: Record<string, Record<string, unknown>>): Set<string> {
  return new Set(stringList(settingsValues.models?.favorite_profiles));
}

function providerStatus(
  providerId: string,
  settingsValues: Record<string, Record<string, unknown>>,
): { readiness: SettingsProfileReadiness; reason: string } {
  if (!providerId) {
    return { readiness: "unknown", reason: "No provider route has been reported for this profile." };
  }
  if (["stub", "local", "ollama", "lmstudio", "vllm", "llamacpp", "llama_cpp"].includes(providerId)) {
    return { readiness: "local", reason: "Local model; no remote provider credential is required." };
  }
  const providers = recordList(settingsValues.apis?.api_keys);
  const provider = providers.find((item) => stringValue(item.provider_id, item.id) === providerId);
  const accountProviders = recordValue(settingsValues.accounts_connections?.providers);
  const accountConnection = recordValue(accountProviders[providerId]);
  if (!provider && Object.keys(accountConnection).length === 0) {
    return { readiness: "needs_connection", reason: `No ${providerId} provider connection is registered.` };
  }
  const providerRecord = provider ?? {};
  const oauth = recordValue(providerRecord.oauth);
  const registeredApis = [
    ...recordList(providerRecord.apis),
    ...recordList(providerRecord.api_keys),
    ...recordList(providerRecord.registered_apis),
  ];
  const connectionStatus = stringValue(
    providerRecord.connection_status,
    oauth.connection_status,
    accountConnection.connection_status,
    providerRecord.status,
    accountConnection.status,
  ).toLowerCase();
  const permissionBlocked = /(?:blocked|forbidden|permission_denied|insufficient_scope|unauthorized|denied)/.test(connectionStatus);
  const blockedReason = stringValue(
    providerRecord.blocked_reason,
    oauth.blocked_reason,
    accountConnection.blocked_reason,
    providerRecord.disabled_reason,
    accountConnection.disabled_reason,
    permissionBlocked ? stringValue(accountConnection.status_label, providerRecord.status_label, oauth.status_label) : "",
    permissionBlocked ? `${providerId} access is blocked by the current permission or authorization state.` : "",
  );
  if (blockedReason) return { readiness: "blocked", reason: blockedReason };
  const hasCredentialRef = [
    providerRecord.credential_ref,
    oauth.credential_ref,
    accountConnection.credential_ref,
    ...registeredApis.map((item) => item.credential_ref),
  ].some((value) => Boolean(credentialReferenceId(value)));
  const hasConfiguredApi = registeredApis.some((item) => (
    booleanValue(item.connected, item.configured, item.api_key_configured, item.token_configured)
    || Boolean(credentialReferenceId(item.credential_ref))
  ));
  const statusConnected = /^(?:connected|ready|configured|active|ok)$/.test(connectionStatus);
  const connected = statusConnected || booleanValue(
    providerRecord.connected,
    providerRecord.configured,
    providerRecord.api_key_configured,
    providerRecord.token_configured,
    oauth.connected,
    oauth.configured,
    accountConnection.connected,
    accountConnection.configured,
    accountConnection.api_key_configured,
    accountConnection.token_configured,
  ) || hasCredentialRef || hasConfiguredApi;
  if (connected) return { readiness: "ready", reason: `${providerId} is connected or has a configured API route.` };
  return { readiness: "needs_connection", reason: `${providerId} requires a connected account or API key.` };
}

function modelProfileRole(profile: ModelProfile): string {
  const roles = profile.recommended_roles?.length ? profile.recommended_roles : profile.allowed_roles;
  if (roles?.length) return roles.slice(0, 3).join(" · ");
  if (profile.local) return "Local fallback";
  if (profile.supports_fast || profile.speed_tier === "fast") return "Fast response";
  if (profile.supports_thinking) return "Reasoning";
  if (profile.supports_vision || profile.supports_image_input) return "Multimodal";
  return "General assistant";
}

function profileFromSettingsRecord(
  raw: Record<string, unknown>,
  index: number,
  activeProfileId: string,
  defaultProfileId: string,
  favoriteIds: Set<string>,
  modelRoutesText: string,
  settingsValues: Record<string, Record<string, unknown>>,
): SettingsProfileRecord | null {
  const id = stringValue(raw.profile_id, raw.id, raw.key);
  if (!id) return null;
  const modelId = stringValue(
    raw.model_profile_id,
    raw.preferred_model,
    raw.main_model,
    raw.qualified_model_id,
    raw.model_id,
  );
  const providerId = stringValue(raw.provider_id, modelId.includes("/") ? modelId.split("/")[0] : "");
  const readiness = providerStatus(providerId, settingsValues);
  const routeRefs = modelId ? selectedApisForModel(modelRoutesText, modelId) : [];
  const managed = booleanValue(raw.managed, raw.builtin, raw.read_only, raw.readonly) || raw.editable === false;
  return {
    id,
    name: displayStringValue(raw.display_name, raw.name, raw.label, id),
    description: stringValue(raw.description, raw.summary, raw.purpose),
    role: stringValue(raw.role, raw.purpose, raw.recommended_role, "General workspace"),
    providerId,
    modelId,
    routeRefs,
    source: "settings",
    sourceLabel: stringValue(raw.source_label, raw.source, raw.origin, "Settings"),
    editable: !managed,
    managed,
    active: activeProfileId ? id === activeProfileId || modelId === activeProfileId : raw.active === true,
    default: defaultProfileId ? id === defaultProfileId || modelId === defaultProfileId : raw.default === true || raw.is_default === true,
    favorite: favoriteIds.has(id),
    readiness: readiness.readiness,
    readinessReason: readiness.reason,
    capabilityTags: stringList(raw.capability_tags ?? raw.capabilities),
    raw,
    collectionIndex: index,
  };
}

function profileFromModel(
  profile: ModelProfile,
  activeProfileId: string,
  defaultProfileId: string,
  favoriteIds: Set<string>,
  modelRoutesText: string,
  settingsValues: Record<string, Record<string, unknown>>,
): SettingsProfileRecord {
  const id = stringValue(profile.profile_id, profile.qualified_model_id, `${profile.provider_id ?? ""}/${profile.model_id ?? ""}`);
  const modelId = stringValue(profile.qualified_model_id, profile.profile_id, `${profile.provider_id ?? ""}/${profile.model_id ?? ""}`);
  const providerId = stringValue(profile.provider_id, modelId.includes("/") ? modelId.split("/")[0] : "");
  const status = providerStatus(providerId, settingsValues);
  const availability = recordValue(profile.availability);
  const availabilityStatus = stringValue(availability.status).toLowerCase();
  const availabilityBlockedReason = stringValue(
    availability.blocked_reason,
    /(?:blocked|unavailable|denied|error|failed)/.test(availabilityStatus) ? availability.reason : "",
  );
  const explicitlyConfigured = booleanValue(availability.configured, availability.active) || profile.local;
  const readiness = availabilityBlockedReason
    ? { readiness: "blocked" as const, reason: availabilityBlockedReason }
    : explicitlyConfigured
      ? { readiness: profile.local ? "local" as const : "ready" as const, reason: profile.local ? "Local model is available on this device." : "Model provider is configured." }
      : status;
  return {
    id,
    name: displayStringValue(profile.display_name, profile.disambiguated_name, id),
    description: stringValue(profile.metadata?.description, profile.metadata?.summary),
    role: modelProfileRole(profile),
    providerId,
    modelId,
    routeRefs: selectedApisForModel(modelRoutesText, modelId),
    source: "model",
    sourceLabel: stringValue(profile.provider_display_name, providerId, "Model catalog"),
    editable: false,
    managed: true,
    active: id === activeProfileId || modelId === activeProfileId,
    default: id === defaultProfileId || modelId === defaultProfileId,
    favorite: favoriteIds.has(id) || favoriteIds.has(modelId),
    readiness: readiness.readiness,
    readinessReason: readiness.reason,
    capabilityTags: [
      ...(profile.capability_tags ?? []),
      ...(profile.supports_thinking ? ["thinking"] : []),
      ...(profile.supports_vision || profile.supports_image_input ? ["vision"] : []),
      ...(profile.supports_tool_calling ? ["tools"] : []),
      ...(profile.supports_fast ? ["fast"] : []),
    ].filter((item, index, all) => all.indexOf(item) === index),
    raw: profile as unknown as Record<string, unknown>,
    collectionIndex: null,
  };
}

function profileFromCatalogRecord(
  raw: Record<string, unknown>,
  activeProfileId: string,
  defaultProfileId: string,
  favoriteIds: Set<string>,
  modelRoutesText: string,
  settingsValues: Record<string, Record<string, unknown>>,
): SettingsProfileRecord | null {
  const id = stringValue(raw.profile_id, raw.id, raw.key);
  if (!id) return null;
  const modelId = stringValue(raw.model_profile_id, raw.preferred_model, raw.main_model, raw.qualified_model_id, raw.model_id);
  const providerId = stringValue(raw.provider_id, modelId.includes("/") ? modelId.split("/")[0] : "");
  const readiness = providerStatus(providerId, settingsValues);
  return {
    id,
    name: displayStringValue(raw.display_name, raw.name, raw.label, id),
    description: stringValue(raw.description, raw.summary, raw.purpose),
    role: stringValue(raw.role, raw.purpose, raw.recommended_role, "Runtime preset"),
    providerId,
    modelId,
    routeRefs: modelId ? selectedApisForModel(modelRoutesText, modelId) : [],
    source: "catalog",
    sourceLabel: stringValue(raw.source_label, raw.source, raw.origin, "Runtime catalog"),
    editable: false,
    managed: true,
    active: activeProfileId ? id === activeProfileId || modelId === activeProfileId : raw.active === true,
    default: defaultProfileId ? id === defaultProfileId || modelId === defaultProfileId : raw.default === true || raw.is_default === true,
    favorite: favoriteIds.has(id),
    readiness: readiness.readiness,
    readinessReason: readiness.reason,
    capabilityTags: stringList(raw.capability_tags ?? raw.capabilities),
    raw,
    collectionIndex: null,
  };
}

export function buildSettingsProfileWorkspace({
  settingsSections,
  settingsValues,
  catalog,
  modelProfiles = [],
  activeModelProfileId,
}: {
  settingsSections: SettingsSection[];
  settingsValues: Record<string, Record<string, unknown>>;
  catalog: UICatalog | null;
  modelProfiles?: ModelProfile[];
  activeModelProfileId?: string | null;
}): SettingsProfileWorkspace {
  const activeProfileId = activeProfileIdFromValues(settingsValues, catalog, activeModelProfileId);
  const defaultProfileId = defaultProfileIdFromValues(settingsValues, catalog);
  const favoriteIds = favoriteIdsFromValues(settingsValues);
  const modelRoutesText = routeTextFromValues(settingsValues);
  const editableCollection = findEditableCollection(settingsSections, settingsValues);
  const profiles = new Map<string, SettingsProfileRecord>();

  editableCollection?.records.forEach((record, index) => {
    const profile = profileFromSettingsRecord(record, index, activeProfileId, defaultProfileId, favoriteIds, modelRoutesText, settingsValues);
    if (profile) profiles.set(profile.id, profile);
  });

  for (const raw of recordList(catalog?.agent_service?.profiles)) {
    const profile = profileFromCatalogRecord(raw, activeProfileId, defaultProfileId, favoriteIds, modelRoutesText, settingsValues);
    if (profile && !profiles.has(profile.id)) profiles.set(profile.id, profile);
  }

  for (const modelProfile of modelProfiles) {
    const profile = profileFromModel(modelProfile, activeProfileId, defaultProfileId, favoriteIds, modelRoutesText, settingsValues);
    if (!profiles.has(profile.id)) profiles.set(profile.id, profile);
  }

  const sorted = [...profiles.values()].sort((left, right) => (
    Number(right.active) - Number(left.active)
    || Number(right.default) - Number(left.default)
    || Number(right.editable) - Number(left.editable)
    || left.name.localeCompare(right.name)
  ));

  return { profiles: sorted, activeProfileId, defaultProfileId, editableCollection, modelRoutesText };
}

function cloneWithoutPlaintextSecrets(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(cloneWithoutPlaintextSecrets);
  if (!value || typeof value !== "object") return value;
  const next: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    const normalizedKey = key.toLowerCase();
    const isReference = normalizedKey.endsWith("_ref") || normalizedKey.endsWith("_id") || normalizedKey === "credential_ref";
    const looksSecret = !isReference && /(?:^|_)(?:secret|token|api_?key|private_?key|password|authorization)(?:$|_)/i.test(normalizedKey);
    if (looksSecret) continue;
    if (["active", "default", "is_default", "selected"].includes(normalizedKey)) continue;
    next[key] = cloneWithoutPlaintextSecrets(item);
  }
  return next;
}

function slugifyProfileName(name: string): string {
  const ascii = name.normalize("NFKD").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return ascii || "profile";
}

export function uniqueProfileId(name: string, existingIds: string[]): string {
  const base = slugifyProfileName(name);
  const usesNamespace = existingIds.some((id) => id.includes("/"));
  const candidateBase = usesNamespace ? `custom/${base}` : base;
  if (!existingIds.includes(candidateBase)) return candidateBase;
  let index = 2;
  while (existingIds.includes(`${candidateBase}-${index}`)) index += 1;
  return `${candidateBase}-${index}`;
}

export function createProfileRecord({
  collection,
  name,
  description,
  modelId,
}: {
  collection: EditableSettingsProfileCollection;
  name: string;
  description?: string;
  modelId?: string;
}): Record<string, unknown> {
  const existingIds = collection.records.map((record) => stringValue(record[collection.idField], record.profile_id, record.id)).filter(Boolean);
  const id = uniqueProfileId(name, existingIds);
  const template = collection.records[0] ?? {};
  const next: Record<string, unknown> = {
    [collection.idField]: id,
    [collection.nameField]: name.trim(),
  };
  if (description?.trim()) next.description = description.trim();
  if (modelId?.trim()) {
    if (hasOwn(template, "model_profile_id")) next.model_profile_id = modelId.trim();
    else if (hasOwn(template, "main_model")) next.main_model = modelId.trim();
    else if (hasOwn(template, "model_id") && !hasOwn(template, "preferred_model")) next.model_id = modelId.trim();
    else next.preferred_model = modelId.trim();
    if (modelId.includes("/")) next.provider_id = modelId.split("/")[0];
  }
  if (hasOwn(template, "editable")) next.editable = true;
  if (hasOwn(template, "managed")) next.managed = false;
  if (hasOwn(template, "builtin")) next.builtin = false;
  return next;
}

export function duplicateProfileRecord({
  collection,
  profile,
  name,
}: {
  collection: EditableSettingsProfileCollection;
  profile: SettingsProfileRecord;
  name: string;
}): Record<string, unknown> {
  const existingIds = collection.records.map((record) => stringValue(record[collection.idField], record.profile_id, record.id)).filter(Boolean);
  const copy = cloneWithoutPlaintextSecrets(profile.raw) as Record<string, unknown>;
  copy[collection.idField] = uniqueProfileId(name, existingIds);
  copy[collection.nameField] = name.trim();
  if (hasOwn(copy, "managed")) copy.managed = false;
  if (hasOwn(copy, "builtin")) copy.builtin = false;
  if (hasOwn(copy, "editable")) copy.editable = true;
  return copy;
}

export function renameProfileRecord(
  collection: EditableSettingsProfileCollection,
  profile: SettingsProfileRecord,
  name: string,
): Record<string, unknown>[] {
  return collection.records.map((record, index) => (
    index === profile.collectionIndex ? { ...record, [collection.nameField]: name.trim() } : record
  ));
}

export function deleteProfileRecord(
  collection: EditableSettingsProfileCollection,
  profile: SettingsProfileRecord,
): Record<string, unknown>[] {
  return collection.records.filter((_, index) => index !== profile.collectionIndex);
}
