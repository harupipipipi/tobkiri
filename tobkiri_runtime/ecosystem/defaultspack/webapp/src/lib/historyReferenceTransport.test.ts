import assert from "node:assert/strict";
import test from "node:test";
import { buildHistoryChatReference, HISTORY_REFERENCE_DROP_EVENT } from "./historyReferences";
import { dispatchHistoryReferenceDrop, historyReferenceTargetAtPoint } from "./historyReferenceTransport";

test("composer targeting rejects nonfinite/outside points without DOM lookup", () => {
  const priorWindow = globalThis.window;
  const priorDocument = globalThis.document;
  let calls = 0;
  globalThis.window = { innerWidth: 800, innerHeight: 600, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }) } as unknown as Window & typeof globalThis;
  globalThis.document = { elementsFromPoint() { calls++; return []; } } as unknown as Document;
  try {
    for (const point of [{ x: NaN, y: 2 }, { x: Infinity, y: 2 }, { x: -1, y: 2 }, { x: 800, y: 2 }, { x: 2, y: 600 }]) {
      assert.equal(historyReferenceTargetAtPoint(point), null);
    }
    assert.equal(calls, 0);
  } finally { globalThis.window = priorWindow; globalThis.document = priorDocument; }
});

test("nested target resolves exact composer ID with bounded hit testing", () => {
  const priorWindow = globalThis.window;
  const priorDocument = globalThis.document;
  const seen: string[] = [];
  globalThis.window = { innerWidth: 800, innerHeight: 600, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }) } as unknown as Window & typeof globalThis;
  globalThis.document = { elementsFromPoint: () => [{ closest(selector: string) { seen.push(selector); return { id: "composer-a", isConnected: true, getBoundingClientRect: () => ({ left: 0, top: 0, right: 100, bottom: 100 }) }; } }] } as unknown as Document;
  try {
    assert.equal(historyReferenceTargetAtPoint({ x: 20, y: 30 }), "composer-a");
    assert.deepEqual(seen, ['[data-history-reference-drop-target="composer"]']);
  } finally { globalThis.window = priorWindow; globalThis.document = priorDocument; }
});

test("reference event carries candidate and exact drop point without authority", () => {
  const priorWindow = globalThis.window;
  let captured: CustomEvent | undefined;
  globalThis.window = { dispatchEvent(event: CustomEvent) { captured = event; return true; } } as unknown as Window & typeof globalThis;
  try {
    const candidate = buildHistoryChatReference({ id: "chat-a", title: "Example" }, "default")!;
    dispatchHistoryReferenceDrop(candidate, { x: 20, y: 30 }, "composer-a");
    assert.equal(captured?.type, HISTORY_REFERENCE_DROP_EVENT);
    assert.deepEqual(captured?.detail, { rawPayload: JSON.stringify(candidate), point: { x: 20, y: 30 }, targetId: "composer-a" });
    assert.equal("confirmed" in JSON.parse(captured!.detail.rawPayload), false);
  } finally { globalThis.window = priorWindow; }
});


test("covered composer cannot receive a reference through modal hit layers", () => {
  const priorWindow = globalThis.window;
  const priorDocument = globalThis.document;
  let underlyingLookups = 0;
  globalThis.window = { innerWidth: 800, innerHeight: 600 } as unknown as Window & typeof globalThis;
  globalThis.document = { elementsFromPoint: () => [
    { closest: () => null },
    { closest() { underlyingLookups++; return { id: "covered-composer" }; } },
  ] } as unknown as Document;
  try {
    assert.equal(historyReferenceTargetAtPoint({ x: 20, y: 30 }), null);
    assert.equal(underlyingLookups, 0);
  } finally { globalThis.window = priorWindow; globalThis.document = priorDocument; }
});

test("composer target must be connected visible and contain the finite point", () => {
  const priorWindow = globalThis.window;
  const priorDocument = globalThis.document;
  const target = { id: "composer-a", isConnected: true,
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 100, bottom: 100 }) };
  let visibility = "visible";
  globalThis.window = { innerWidth: 800, innerHeight: 600,
    getComputedStyle: () => ({ display: "block", visibility, opacity: "1" }),
  } as unknown as Window & typeof globalThis;
  globalThis.document = { elementsFromPoint: () => [{ closest: () => target }] } as unknown as Document;
  try {
    target.isConnected = false;
    assert.equal(historyReferenceTargetAtPoint({ x: 20, y: 30 }), null);
    target.isConnected = true;
    assert.equal(historyReferenceTargetAtPoint({ x: 120, y: 30 }), null);
    target.getBoundingClientRect = () => ({ left: NaN, top: 0, right: 100, bottom: 100 });
    assert.equal(historyReferenceTargetAtPoint({ x: 20, y: 30 }), null);
    target.getBoundingClientRect = () => ({ left: 0, top: 0, right: 100, bottom: 100 });
    visibility = "hidden";
    assert.equal(historyReferenceTargetAtPoint({ x: 20, y: 30 }), null);
  } finally { globalThis.window = priorWindow; globalThis.document = priorDocument; }
});
