import { createExtension } from "./index.js";

/** Add descriptive command metadata without changing the 1.0 adapter contract. */
export const createExtensionWithCommandInventory = (factory, host) => {
  const commands = new Map();
  const extension = createExtension((api) => factory(new Proxy(api, {
    get(target, key) {
      if (key !== "registerCommand") return target[key];
      return (name, command) => {
        target.registerCommand(name, command);
        commands.set(name, command);
      };
    },
  })), host);
  return {
    ...extension,
    listCommands: () => [...commands].map(([name, command]) => ({
      name, description: command?.description ?? "",
    })),
  };
};
