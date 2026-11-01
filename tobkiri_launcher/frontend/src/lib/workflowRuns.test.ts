import assert from 'node:assert/strict';
import test from 'node:test';
import { createWorkflowRun, controlWorkflowRun, getWorkflowRun, parseWorkflowRun, type WorkflowAuthoringDependencies } from './workflowAuthoring';
import { MutationResultUnknownError } from './mutationJournal';
const digest = (x: string) => `sha256:${x.repeat(64)}`;
const catalog = { version: 'rumi.ui.contribution.v1', profile_id: 'defaults', profile_revision: digest('1'), activation_id: 'activation:test', plan_hash: digest('2'), catalog_hash: digest('3'), diagnostics: [], quarantined_pack_ids: [], contributions: ['run.create', 'run.advance', 'run.stop', 'run.observe'].map((id) => ({ contribution_id: `pack.tobkiri_workflow_pack.${id}`, owner_pack_id: 'tobkiri_workflow_pack', owner_pack_hash: digest('4'), label: id, action_contract: id === 'run.stop' ? 'tobkiri.workflow.stop.v4' : 'tobkiri.workflow.v4', operation_id: id, provider_id: id === 'run.stop' ? 'tobkiri.workflow.stop.provider' : 'tobkiri.workflow.provider', function_id: id === 'run.stop' ? 'tobkiri.workflow.stop.provider' : 'tobkiri.workflow.provider', build_identity: id === 'run.stop' ? 'tobkiri.workflow.stop.provider' : 'tobkiri.workflow.provider', descriptor_hash: digest('5'), kind: 'action' as const, mode: 'declarative' as const, resolved_profile_id: 'defaults', resolved_profile_revision: digest('1'), resolved_activation_id: 'activation:test', resolved_plan_hash: digest('2') })) };
const run = (id: string, state = 'queued') => ({ run_id: id, definition_id: 'flow.voice', revision_digest: digest('6'), state, inputs: { sensitive: 'not projected' } });
test('run status projects only exact safe identity and same-run attempts', () => {
    assert.deepEqual(parseWorkflowRun(run('a')), { runId: 'a', definitionId: 'flow.voice', state: 'queued', revisionDigest: digest('6'), attempts: [] });
    assert.equal(parseWorkflowRun({ run: run('a'), attempts: [{ run_id: 'other', step_id: 'stt', state: 'succeeded', attempt_number: 1 }] }), null);
});
test('captured Run and Stop preserve separate mutation identities while advance is pending', async () => {
    let release: (value: unknown) => void = () => { };
    const waiting = new Promise((resolve) => { release = resolve; });
    const calls: string[] = [];
    const dependencies: WorkflowAuthoringDependencies = { fetchCatalog: async () => catalog, invoke: async (request) => {
            calls.push(request.contributionId);
            assert.equal(request.profileId, 'defaults');
            if (request.contributionId.endsWith('run.advance'))
                return waiting;
            return run(String(request.payload.run_id), request.contributionId.endsWith('run.stop') ? 'cancelled' : 'queued');
        } };
    await createWorkflowRun('flow.voice', digest('6'), 'test-concurrent', { audio: 'fixture' }, dependencies);
    const advance = controlWorkflowRun('run.advance', 'test-concurrent', dependencies);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const cancelled = await controlWorkflowRun('run.stop', 'test-concurrent', dependencies);
    assert.equal(cancelled.state, 'cancelled');
    release({ run: run('test-concurrent', 'cancelled'), attempts: [] });
    await advance;
    assert.equal(calls.length, 3);
});
test('wrong Run identity is not accepted as success or automatically replayed', async () => {
    let calls = 0;
    const dependencies: WorkflowAuthoringDependencies = { fetchCatalog: async () => catalog, invoke: async () => { calls++; return run('wrong'); } };
    await assert.rejects(() => createWorkflowRun('flow.voice', digest('6'), 'test-wrong', {}, dependencies), MutationResultUnknownError);
    assert.equal(calls, 1);
    await assert.rejects(() => getWorkflowRun('test-wrong', dependencies), /Malformed/);
});

test('run creation carries the exact displayed revision through the captured invocation', async () => {
    const dependencies: WorkflowAuthoringDependencies = {fetchCatalog: async () => catalog, invoke: async request => {
        assert.deepEqual(request.payload, {definition_id:'flow.voice', revision_digest:digest('6'), run_id:'run-pinned', inputs:{text:'hello'}});
        return run('run-pinned');
    }};
    const created = await createWorkflowRun('flow.voice', digest('6'), 'run-pinned', {text:'hello'}, dependencies);
    assert.equal(created.revisionDigest, digest('6'));
});

test('a same-ID run at another revision is unresolved and never accepted as the selected graph', async () => {
    let calls = 0;
    const dependencies: WorkflowAuthoringDependencies = {fetchCatalog: async () => catalog, invoke: async () => {
        calls++;
        return {...run('run-other-revision'), revision_digest:digest('7')};
    }};
    await assert.rejects(() => createWorkflowRun('flow.voice', digest('6'), 'run-other-revision', {}, dependencies), MutationResultUnknownError);
    assert.equal(calls, 1);
});
