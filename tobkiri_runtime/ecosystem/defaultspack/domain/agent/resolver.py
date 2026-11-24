"""Resolver — resolves model and tool selections for agent steps."""



class Resolver:
    """Resolves which model and tools to use for a given agent step."""

    def resolve_model(self, agent_def, step, consec_err):
        """Resolve the model to use based on agent config and error count.

        Falls back to the fallback model if consecutive errors exceed threshold.
        Returns the model identifier string.
        """
        model_config = agent_def.get("model", {})
        err_threshold = agent_def.get("loop", {}).get("error_fallback_threshold", 3)
        if consec_err >= err_threshold and model_config.get("fallback"):
            return model_config["fallback"]
        if model_config.get("default"):
            return model_config["default"]
        raise ValueError("agent model.default is required")

    def resolve_tools(self, agent_def, step):
        """Resolve the list of enabled tools for a given step.

        Returns a list of tool identifier strings.
        """
        tools_config = agent_def.get("tools", {})
        return tools_config.get("enabled", [])
