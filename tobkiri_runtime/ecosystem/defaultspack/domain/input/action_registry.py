from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.input.envelope import RumiInputEnvelope


InputActionHandler = Callable[[RumiInputEnvelope, dict[str, Any] | None], dict[str, Any]]


@dataclass(frozen=True)
class InputActionSpec:
    action_id: str
    handler: InputActionHandler


class InputActionRegistry:
    def __init__(self, actions: list[InputActionSpec] | None = None) -> None:
        self._actions = {
            spec.action_id: spec.handler
            for spec in (actions or _default_specs())
        }

    def resolve(self, action_id: str) -> InputActionHandler | None:
        return self._actions.get(str(action_id or "").strip())

    def list_actions(self) -> list[str]:
        return sorted(self._actions)


def _default_specs(settings_owner: SettingsOwnerPort | None = None) -> list[InputActionSpec]:
    from domain.input.actions.agent_delegate import handle as handle_agent_delegate
    from domain.input.actions.chat_message import handle as handle_chat_message
    from domain.input.actions.model_route import handle as handle_model_route
    from domain.input.actions.model_switch import handle as handle_model_switch
    from domain.input.actions.run_instruction import handle as handle_run_instruction
    from domain.input.actions.run_interrupt import handle as handle_run_interrupt

    if settings_owner is not None:
        handle_chat_message = partial(handle_chat_message, settings_owner=settings_owner)

    return [
        InputActionSpec("chat.message", handle_chat_message),
        InputActionSpec("dispatch_input", handle_chat_message),
        InputActionSpec("submit_input", handle_chat_message),
        InputActionSpec("defaults.console.input", handle_chat_message),
        InputActionSpec("defaultspack.console.input", handle_chat_message),
        InputActionSpec("run.instruction", handle_run_instruction),
        InputActionSpec("run.interrupt", handle_run_interrupt),
        InputActionSpec("agent.delegate", handle_agent_delegate),
        InputActionSpec("model.switch", handle_model_switch),
        InputActionSpec("model.route", handle_model_route),
    ]


_DEFAULT_REGISTRY: InputActionRegistry | None = None


def get_input_action_registry(*, settings_owner: SettingsOwnerPort | None = None) -> InputActionRegistry:
    # Bound handlers are request-local, never retained by the shared registry.
    if settings_owner is not None:
        return InputActionRegistry(_default_specs(settings_owner))
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = InputActionRegistry()
    return _DEFAULT_REGISTRY
