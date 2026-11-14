import test from "node:test";
import assert from "node:assert/strict";
import { createExtensionWithCommandInventory } from "../pi-shim/command-inventory.js";
import { addInventoryWorld, inventoryFiles, inventoryPorts, inventoryWorldExport, inventoryEntryExport } from "../pi-wrapper-config.js";

test("opt-in extends only the known pinned wrapper world", () => {
  const world = "package local:pi-example-hello@1.0.0;\nworld pack {\n include tobkiri:core/pack-minimal@1.0.0;\n import tobkiri:core/host-io@1.0.0;\n export local:pi-adapter/pi-extension@1.0.0;\n}\n";
  assert.equal(addInventoryWorld(world, "local.pi-example-hello"),
    world.replace("}\n", " export local:pi-adapter/command-inventory@1.1.0;\n}\n"));
  assert.throws(() => addInventoryWorld(world + "// changed\n", "local.pi-example-hello"), /unmodified 1.0 wrapper world/);
  assert.throws(() => addInventoryWorld(world, "local.other"), /unmodified 1.0 wrapper world/);
  assert.throws(() => addInventoryWorld(addInventoryWorld(world, "local.pi-example-hello"), "local.pi-example-hello"), /unmodified 1.0 wrapper world/);
});

test("wrapper inventory files, port, world and entry are absent by default", () => {
  assert.deepEqual(inventoryFiles(false), []);
  assert.deepEqual(inventoryPorts(false), []);
  assert.equal(inventoryWorldExport(false), "");
  assert.equal(inventoryEntryExport(false), "");
  assert.deepEqual(inventoryFiles(true), ["wit/deps/pi-adapter-v1-1.wit"]);
  assert.deepEqual(inventoryPorts(true), [
    { shape: "local.pi-adapter.command-inventory", version: "1.1", optional: false },
  ]);
  assert.match(inventoryWorldExport(true), /command-inventory@1\.1\.0/);
  assert.match(inventoryEntryExport(true), /get\(\)\.listCommands\(\)/);
});

test("command inventory describes registrations without invoking handlers or changing load", async () => {
  let calls = 0;
  const extension = createExtensionWithCommandInventory((pi) => {
    pi.registerCommand("first", { description: "First command", handler: () => { calls++; return "done"; } });
    pi.registerCommand("second", { handler: () => { calls++; } });
  }, { notify() {}, hasUI: () => false, isCancelled: () => false });
  assert.deepEqual(extension.load().commands, ["first", "second"]);
  assert.deepEqual(extension.listCommands(), [
    { name: "first", description: "First command" },
    { name: "second", description: "" },
  ]);
  assert.equal(calls, 0);
  assert.equal(JSON.parse(await extension.runCommand("first", "")), "done");
  assert.equal(calls, 1);
  assert.deepEqual(extension.load().commands, ["first", "second"]);
});

test("command inventory keeps existing registration validation and unsupported APIs", () => {
  assert.throws(() => createExtensionWithCommandInventory((pi) => {
    pi.registerCommand("duplicate", {});
    pi.registerCommand("duplicate", {});
  }, {}), /duplicate or invalid pi command/);
  assert.throws(() => createExtensionWithCommandInventory((pi) => pi.sendMessage({}), {}),
    /unsupported by tobkiri.pi-adapter: pi.sendMessage/);
  assert.deepEqual(createExtensionWithCommandInventory(() => {}, {}).listCommands(), []);
});
