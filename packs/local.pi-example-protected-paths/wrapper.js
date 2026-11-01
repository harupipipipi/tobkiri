// packs/local.pi-example-protected-paths/upstream/protected-paths.ts
function protected_paths_default(pi) {
  const protectedPaths = [".env", ".git/", "node_modules/"];
  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName !== "write" && event.toolName !== "edit") {
      return void 0;
    }
    const path = event.input.path;
    const isProtected = protectedPaths.some((p) => path.includes(p));
    if (isProtected) {
      if (ctx.hasUI) {
        ctx.ui.notify(`Blocked write to protected path: ${path}`, "warning");
      }
      return { block: true, reason: `Path "${path}" is protected` };
    }
    return void 0;
  });
}

// sdk/js/pi-shim/index.js
var unsupported = (name) => {
  throw new Error(`unsupported by tobkiri.pi-adapter: ${name}`);
};
var supported = (value, prefix) => new Proxy(value, {
  get(target, key) {
    if (key in target || typeof key === "symbol") return target[key];
    return unsupported(`${prefix}${key}`);
  }
});
var createExtension = (factory, host) => {
  const tools = /* @__PURE__ */ new Map();
  const events = /* @__PURE__ */ new Map();
  const commands = /* @__PURE__ */ new Map();
  const api = supported({
    registerTool(tool) {
      if (!tool?.name || tools.has(tool.name)) throw new Error("duplicate or invalid pi tool");
      tools.set(tool.name, tool);
    },
    on(name, handler) {
      if (!["tool_call", "before_agent_start"].includes(name)) unsupported(`on(${name})`);
      const handlers = events.get(name) ?? [];
      handlers.push(handler);
      events.set(name, handlers);
    },
    registerCommand(name, command) {
      if (!name || commands.has(name)) throw new Error("duplicate or invalid pi command");
      commands.set(name, command);
    }
  }, "pi.");
  const initialized = factory(api);
  if (initialized?.then) unsupported("async extension factory");
  const context = (hasUI = false) => supported({
    hasUI,
    ui: supported({ notify: (text, level = "info") => host.notify(String(text), level) }, "ctx.ui.")
  }, "ctx.");
  return {
    load() {
      return {
        tools: [...tools.values()].map(({ name, label, description, parameters }) => ({
          name,
          label: label ?? name,
          description: description ?? "",
          parameters: JSON.stringify(parameters)
        })),
        events: [...events.keys()],
        commands: [...commands.keys()]
      };
    },
    async invokeTool(name, toolCallId, argumentsJson) {
      const tool = tools.get(name);
      if (!tool) throw new Error("pi tool not found");
      const signal = supported({ get aborted() {
        return host.isCancelled();
      } }, "signal.");
      const onUpdate = () => unsupported("onUpdate");
      return JSON.stringify(await tool.execute(toolCallId, JSON.parse(argumentsJson), signal, onUpdate, context(host.hasUI())));
    },
    async onEvent(name, eventJson, hasUI) {
      let result;
      for (const handler of events.get(name) ?? []) {
        const value = await handler(JSON.parse(eventJson), context(hasUI));
        if (value !== void 0) result = { ...result, ...value };
        if (result?.block) break;
      }
      return result === void 0 ? void 0 : JSON.stringify(result);
    },
    async runCommand(name, args) {
      const command = commands.get(name);
      if (!command) throw new Error("pi command not found");
      return JSON.stringify(await command.handler(args, context(host.hasUI())) ?? null);
    }
  };
};

// packs/local.pi-example-protected-paths/wrapper-entry.mjs
import { display } from "tobkiri:core/host-io@1.0.0";
import { isCancelled } from "tobkiri:core/flow@1.0.0";
var extension;
var get = () => extension ??= createExtension(protected_paths_default, { notify: (text, level) => display("notification", JSON.stringify({ text, level })), hasUI: () => false, isCancelled });
var lifecycle = { init: () => {
  get();
}, shutdown: () => {
  extension = void 0;
} };
var piExtension = { load: () => get().load(), invokeTool: (...a) => get().invokeTool(...a), onEvent: (...a) => get().onEvent(...a), runCommand: (...a) => get().runCommand(...a) };
export {
  lifecycle,
  piExtension
};
