import assert from 'node:assert/strict';
import test from 'node:test';
import {parseWorkflowRun} from './workflowAuthoring';
const locator = `interactive-effect-${'a'.repeat(36)}`;
const view = (state: string, approval_request_id: unknown) => ({
  run: {run_id: 'run-one', definition_id: 'flow-one', revision_digest: `sha256:${'b'.repeat(64)}`, state: 'waiting_approval'},
  attempts: [{run_id: 'run-one', step_id: 'stt', state, attempt_number: 1, approval_request_id}],
});
test('only a pending owned attempt carries an exact approval request locator', () => {
  assert.equal(parseWorkflowRun(view('waiting_approval', locator))?.attempts[0].approvalRequestId, locator);
  assert.equal(parseWorkflowRun(view('waiting_approval', undefined))?.attempts[0].approvalRequestId, undefined);
});
test('malformed or misplaced approval locators reject the whole status response', () => {
  for (const id of ['', 'other-request', `workflow-attempt.${'a'.repeat(64)}`, `workflow-approval-sha256:${'a'.repeat(64)}`, `pending-effect-${'a'.repeat(36)}`, true, {}, `${locator}\n`, `${locator}x`]) assert.equal(parseWorkflowRun(view('waiting_approval', id)), null);
  assert.equal(parseWorkflowRun(view('succeeded', locator)), null);
});
