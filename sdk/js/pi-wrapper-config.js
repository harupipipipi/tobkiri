/** Build-time selections for the optional descriptive inventory interface. */
export const inventoryFiles = (enabled) => enabled ? ["wit/deps/pi-adapter-v1-1.wit"] : [];
export const inventoryPorts = (enabled) => enabled
  ? [{ shape: "local.pi-adapter.command-inventory", version: "1.1", optional: false }]
  : [];
export const inventoryWorldExport = (enabled) => enabled
  ? " export local:pi-adapter/command-inventory@1.1.0;\n" : "";
export const inventoryEntryExport = (enabled) => enabled
  ? "\nexport const commandInventory={listCommands:()=>get().listCommands()};" : "";

/** Extend only the known 1.0 wrapper world; reject custom or already-modified worlds. */
export const addInventoryWorld = (world, packId) => {
  const expected = `package local:${packId.split(".")[1]}@1.0.0;\nworld pack {\n include tobkiri:core/pack-minimal@1.0.0;\n import tobkiri:core/host-io@1.0.0;\n export local:pi-adapter/pi-extension@1.0.0;\n}\n`;
  if (world !== expected) throw new Error("command inventory requires the unmodified 1.0 wrapper world");
  return expected.replace("}\n", `${inventoryWorldExport(true)}}\n`);
};
