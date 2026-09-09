import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok, error
from domain.agent.engine import AgentEngine
from tobkiri_protocol.settings_state import SettingsOwnerPort
from blocks.agent._state import set_engine


def run(input_data, context, *, settings_owner: SettingsOwnerPort | None = None):
    task = input_data.get("task") if isinstance(input_data, dict) else None
    if not task:
        return error("task is required")
    tools = input_data.get("tools", [])
    model = input_data.get("model", "default")
    system_prompt = input_data.get("system_prompt", None)
    context = dict(context or {})
    if isinstance(input_data.get("runtime_profile_key"), str):
        context["runtime_profile_key"] = input_data["runtime_profile_key"]
    if isinstance(input_data.get("capability_profile"), dict):
        context["capability_profile"] = input_data["capability_profile"]
    for key in ("required_capabilities", "attachments", "target", "delivery"):
        if key in input_data and input_data.get(key) not in (None, "", []):
            context[key] = input_data.get(key)
    if isinstance(input_data.get("params"), dict):
        context["params"] = dict(input_data["params"])
    engine = AgentEngine(settings_owner=settings_owner)
    result = engine.execute(task, tools, model, system_prompt, context)
    execution_id = result.get("execution_id", "")
    set_engine(execution_id, engine)
    return ok(result)
