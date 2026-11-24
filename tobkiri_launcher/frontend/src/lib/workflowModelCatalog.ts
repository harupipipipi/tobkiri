/** Read the same owner-managed model identities used by Harness Settings. */
import { fetchFrontendContractOperation } from './defaultspackClient';
export interface RegisteredWorkflowModel {
    profileId: string;
    modelId: string;
    displayName: string;
    providerId: string | null;
}
export interface WorkflowModelSnapshot {
    revision: number;
    models: RegisteredWorkflowModel[];
}
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const label = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0 && value.length <= 512;
export function parseWorkflowModels(value: unknown): WorkflowModelSnapshot | null {
    if (!record(value) || !Array.isArray(value.profiles) || value.profiles.length > 4096 || !Number.isSafeInteger(value.registry_revision) || Number(value.registry_revision) < 0 || value.count !== value.profiles.length)
        return null;
    const models: RegisteredWorkflowModel[] = [];
    const seen = new Set<string>();
    for (const profile of value.profiles) {
        if (!record(profile) || !label(profile.profile_id) || !label(profile.model_id) || !label(profile.display_name) || seen.has(profile.profile_id))
            return null;
        if (profile.enabled !== undefined && profile.enabled !== true) return null;
        if (profile.provider_id !== undefined && !label(profile.provider_id))
            return null;
        seen.add(profile.profile_id);
        // Copy an explicit safe projection, never provider credentials or opaque metadata.
        models.push({ profileId: profile.profile_id, modelId: profile.model_id, displayName: profile.display_name, providerId: typeof profile.provider_id === 'string' ? profile.provider_id : null });
    }
    return { revision: Number(value.registry_revision), models };
}
export async function loadWorkflowModels(read: () => Promise<unknown> = () => fetchFrontendContractOperation('GET', '/api/ai/profiles')): Promise<WorkflowModelSnapshot> {
    const parsed = parseWorkflowModels(await read());
    if (!parsed)
        throw new Error('登録済みモデル一覧を確認できません');
    return parsed;
}
