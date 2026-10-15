/** Build-time alias for pi's ExtensionAPI. No Node runtime is shipped. */
export const defineTool = (definition) => definition;
export const unsupported = (name) => { throw new Error(`unsupported by tobkiri.pi-adapter: ${name}`); };

const supported = (value, prefix) => new Proxy(value, {
  get(target, key) {
    if (key in target || typeof key === "symbol") return target[key];
    return unsupported(`${prefix}${key}`);
  },
});

/** Register one unchanged extension and marshal its operations to adapter WIT. */
export const createExtension = (factory, host) => {
  const tools = new Map();
  const events = new Map();
  const commands = new Map();
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
    },
  }, "pi.");
  const initialized = factory(api);
  if (initialized?.then) unsupported("async extension factory");
  const context = (hasUI = false) => supported({
    hasUI,
    ui: supported({ notify: (text, level = "info") => host.notify(String(text), level) }, "ctx.ui."),
  }, "ctx.");
  return {
    load() {
      return {
        tools: [...tools.values()].map(({ name, label, description, parameters }) => ({
          name, label: label ?? name, description: description ?? "", parameters: JSON.stringify(parameters),
        })),
        events: [...events.keys()],
        commands: [...commands.keys()],
      };
    },
    async invokeTool(name, toolCallId, argumentsJson) {
      const tool = tools.get(name);
      if (!tool) throw new Error("pi tool not found");
      const signal = supported({ get aborted() { return host.isCancelled(); } }, "signal.");
      const onUpdate = () => unsupported("onUpdate");
      return JSON.stringify(await tool.execute(toolCallId, JSON.parse(argumentsJson), signal, onUpdate, context(host.hasUI())));
    },
    async onEvent(name, eventJson, hasUI) {
      let result;
      for (const handler of events.get(name) ?? []) {
        const value = await handler(JSON.parse(eventJson), context(hasUI));
        if (value !== undefined) result = { ...result, ...value };
        if (result?.block) break;
      }
      return result === undefined ? undefined : JSON.stringify(result);
    },
    async runCommand(name, args) {
      const command = commands.get(name);
      if (!command) throw new Error("pi command not found");
      return JSON.stringify(await command.handler(args, context(host.hasUI())) ?? null);
    },
  };
};
