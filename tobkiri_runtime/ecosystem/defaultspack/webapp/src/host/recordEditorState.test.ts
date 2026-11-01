import assert from "node:assert/strict";
import test from "node:test";
import {
  beginRecordDraft, filterEditorRecords, recordActionAvailable, recordDraftDirty,
  recordDraftUpdate, recordEditorRecords, recordOperationPayload, recordSavePayload,
  type RecordEditorDescriptor,
} from "./recordEditorState";

const operation = { contribution_id: "fixture.manage", contract_id: "tobkiri.action.fixture.v1", operation_id: "fixture.manage" };
const descriptor: RecordEditorDescriptor = {
  records_path: "records", id_path: "id", title_path: "name",
  search_paths: ["name", "payload.prompt", "context"],
  columns: [{ label: "Name", path: "name", kind: "text" }],
  fields: [
    { id: "name", label: "Name", path: "name", kind: "text", required: true },
    { id: "prompt", label: "Prompt", path: "payload.prompt", kind: "multiline" },
    { id: "interval", label: "Interval", path: "interval", kind: "integer", min: 0, max: 3600000 },
    { id: "context", label: "Context", path: "context", kind: "text", read_only: true },
  ],
  save: { operation, input: { operation: "update" }, source_bindings: { expected_revision: "revision" },
    record_bindings: { record_id: "id" }, draft_key: "updates" },
  actions: [{ id: "pause", label: "Pause", operation, input: { operation: "pause" },
    source_bindings: { expected_revision: "revision" }, record_bindings: { record_id: "id" },
    available_when: { path: "enabled", equals: true } }],
};
const record = { id: "one", name: "Weekly review", interval: 600000, enabled: true,
  context: "Research conversation", payload: { prompt: "Review progress", keep: "preserve" } };
const snapshot = { revision: 3, records: [record] };

test("list and search use actual record data and searchable body/context", () => {
  const records = recordEditorRecords(snapshot, descriptor);
  assert.deepEqual(records, [record]);
  assert.equal(filterEditorRecords(records!, descriptor, "review progress").length, 1);
  assert.equal(filterEditorRecords(records!, descriptor, "research").length, 1);
  assert.equal(filterEditorRecords(records!, descriptor, "absent").length, 0);
});

test("duplicate IDs, oversized lists and inherited record IDs fail closed", () => {
  assert.equal(recordEditorRecords({ records: [record, record] }, descriptor), null);
  assert.equal(recordEditorRecords({ records: Array.from({ length: 257 }, (_, id) => ({ id: String(id) })) }, descriptor), null);
  assert.equal(recordEditorRecords({ records: [Object.create({ id: "inherited" })] }, descriptor), null);
});

test("draft updates preserve unrelated payload siblings and omit read-only context", () => {
  const draft = beginRecordDraft(record, descriptor, snapshot);
  draft.values.prompt = "Changed body";
  draft.values.context = "Do not write this";
  const { update, errors } = recordDraftUpdate(draft, descriptor);
  assert.deepEqual(errors, {});
  assert.deepEqual(update, { name: "Weekly review", interval: 600000,
    payload: { prompt: "Changed body", keep: "preserve" } });
  assert.equal(record.payload.prompt, "Review progress");
});

test("a dirty draft preserves its opening CAS revision after authoritative refresh", () => {
  const draft = beginRecordDraft(record, descriptor, snapshot);
  draft.values.name = "Edited";
  const nextSnapshot = { revision: 4, records: [{ ...record, name: "Concurrent writer" }] };
  assert.equal(recordSavePayload(descriptor, nextSnapshot, draft).payload?.expected_revision, 3);
  assert.equal(draft.values.name, "Edited");
  assert.equal(recordDraftDirty(draft), true);
});

test("returning all fields to their original values clears dirty state", () => {
  const draft = beginRecordDraft(record, descriptor, snapshot);
  assert.equal(recordDraftDirty(draft), false);
  draft.values.name = "Edit";
  assert.equal(recordDraftDirty(draft), true);
  draft.values.name = draft.original.name;
  assert.equal(recordDraftDirty(draft), false);
});

test("partial, colliding, or inherited revision bindings never create a write payload", () => {
  assert.equal(recordOperationPayload(descriptor.save, {}, record), null);
  assert.equal(recordOperationPayload(descriptor.save, Object.create({ revision: 3 }), record), null);
  assert.equal(recordOperationPayload({ ...descriptor.save, input: { expected_revision: 1 } }, snapshot, record), null);
  assert.equal(recordOperationPayload({ ...descriptor.save, record_bindings: { expected_revision: "interval" } }, snapshot, record), null);
});

test("malformed integer and required fields preserve draft and disable saving", () => {
  const draft = beginRecordDraft(record, descriptor, snapshot);
  draft.values.interval = "1.5";
  draft.values.name = "";
  const result = recordSavePayload(descriptor, snapshot, draft);
  assert.equal(result.payload, null);
  assert.ok(result.errors.interval);
  assert.ok(result.errors.name);
  assert.equal(draft.values.interval, "1.5");
});

test("JSON and nested path edits cannot inject approval, private or prototype keys", () => {
  const jsonDescriptor = { ...descriptor, fields: [{ id: "payload", label: "Payload", path: "payload", kind: "json" as const }] };
  const draft = beginRecordDraft(record, jsonDescriptor, snapshot);
  for (const payload of ['{"approved":true}', '{"__proto__":{"polluted":true}}', '{"_secret":"hidden"}']) {
    draft.values.payload = payload;
    assert.equal(recordSavePayload(jsonDescriptor, snapshot, draft).payload, null);
  }
  const unsafe = { ...descriptor, fields: [{ id: "bad", label: "Bad", path: "payload.constructor", kind: "text" as const }] };
  assert.equal(recordDraftUpdate(beginRecordDraft(record, unsafe), unsafe).update, null);
  assert.equal(({} as Record<string, unknown>).polluted, undefined);
});

test("conditions compare own scalar values without accepting inherited flags", () => {
  const action = descriptor.actions![0];
  assert.equal(recordActionAvailable(action, record), true);
  assert.equal(recordActionAvailable(action, { ...record, enabled: false }), false);
  assert.equal(recordActionAvailable(action, Object.create({ enabled: true })), false);
});

test("context bindings are typed and fully resolved before a record action", () => {
  const request = { ...descriptor.save, context_bindings: { conversation: "conversation_id" as const } };
  assert.equal(recordOperationPayload(request, snapshot, record), null);
  assert.equal(recordOperationPayload(request, snapshot, record, { conversation_id: "chat-one" })?.conversation, "chat-one");
});
