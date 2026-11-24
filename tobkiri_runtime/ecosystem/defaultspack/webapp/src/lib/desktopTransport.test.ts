import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { launchActivePresentationFromAuxiliary } from "./desktopApproval";
import { loadTauriInvoke, type TauriInvoke } from "./desktopTransport";

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
const setWindow = (value: object) => {
  Object.defineProperty(globalThis, "window", { configurable: true, value });
};
afterEach(() => {
  if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
  else Reflect.deleteProperty(globalThis, "window");
});

test("only a browser without native markers reports no desktop transport", async () => {
  setWindow({});
  assert.equal(await loadTauriInvoke(() => { throw new Error("must not load"); }), null);
  assert.equal(await launchActivePresentationFromAuxiliary(), false);
});

for (const marker of ["__TAURI__", "__TAURI_INTERNALS__"]) {
  test(`${marker} preserves module load rejection instead of allowing browser fallback`, async () => {
    setWindow({ [marker]: {} });
    const failure = new Error("native module unavailable");
    await assert.rejects(loadTauriInvoke(async () => { throw failure; }), (error) => error === failure);
    await assert.rejects(loadTauriInvoke(async () => ({ invoke: null as unknown as TauriInvoke })));
  });
}

test("auxiliary launch requests only the active presentation with no caller arguments", async () => {
  const calls: unknown[][] = [];
  setWindow({ __TAURI__: { core: { invoke: async (...args: unknown[]) => { calls.push(args); } } } });
  assert.equal(await launchActivePresentationFromAuxiliary(), true);
  assert.deepEqual(calls, [["launch_active_presentation_from_auxiliary"]]);
});

test("auxiliary launch preserves native command rejection", async () => {
  const failure = new Error("caller route denied");
  setWindow({ __TAURI_INTERNALS__: { invoke: async () => { throw failure; } } });
  await assert.rejects(launchActivePresentationFromAuxiliary(), (error) => error === failure);
});
