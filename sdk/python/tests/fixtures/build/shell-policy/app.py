"""Python-on-wasm shell policy adapter; authority and execution stay in core."""

import json
import wit_world.exports
from wit_world.imports import flow, clock, random, audit
from tobkiri_sdk.generated import bind_generated
from componentize_py_types import Err
from wit_world.imports.types import Error, ErrorCode
from policy import create_shell_policy_operation

bind_generated({"flow":flow,"clock":clock,"random":random,"audit":audit})


class Lifecycle(wit_world.exports.Lifecycle):
    """Initialize a pure classification Pack with no native Python runtime."""

    def init(self, config: str) -> None:
        """No effectful initialization is needed."""

    def shutdown(self) -> None:
        """No external resources are held."""


class Policy(wit_world.exports.Policy):
    """Expose the captured legacy classifier and tokenizer as typed operations."""

    def inspect(self, operation: str, payload: str) -> str:
        """Compute command policy without authorizing or executing the command."""
        try:
            result = create_shell_policy_operation(None)(operation, json.loads(payload))
        except (ValueError, TypeError):
            raise Err(Error(ErrorCode.INVALID, "invalid shell inspection request")) from None
        return json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True)
