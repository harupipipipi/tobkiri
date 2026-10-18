import assert from 'node:assert/strict';
import test from 'node:test';
import { parseWorkflowModels, loadWorkflowModels } from './workflowModelCatalog';
const row = { profile_id: 'daily', model_id: 'model-a', display_name: 'Daily', provider_id: 'connection-a', credential_handle: 'must-not-escape', metadata: { api_key: 'must-not-escape' } };
test('model picker shares safe owner identities without carrying credentials or parameters', () => {
    const parsed = parseWorkflowModels({ registry_revision: 2, count: 1, profiles: [row] });
    assert.deepEqual(parsed, { revision: 2, models: [{ profileId: 'daily', modelId: 'model-a', displayName: 'Daily', providerId: 'connection-a' }] });
    assert.doesNotMatch(JSON.stringify(parsed), /must-not-escape/);
});
test('model picker rejects ambiguous identities and unverified revisions', async () => {
    assert.equal(parseWorkflowModels({ registry_revision: true, count: 1, profiles: [row] }), null);
    assert.equal(parseWorkflowModels({ registry_revision: 1, count: 2, profiles: [row, row] }), null);
    assert.equal(parseWorkflowModels({ registry_revision: 1, count: 0, profiles: [row] }), null);
    await assert.rejects(() => loadWorkflowModels(async () => ({ profiles: [] })), /確認できません/);
});
