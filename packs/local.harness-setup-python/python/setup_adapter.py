"""Finite DATA projection around the original pure Setup methods.

Every confirmation and receipt here is inert Pack DATA. Only the trusted Host
can validate a frozen review, acquire authority and commit activation. In
particular success retains the original restart_required=False intermediate
projection; it is not a native activation receipt accepted by the Setup UI.
"""

from types import SimpleNamespace

from ordered_json_v2 import dumps, loads
from original_setup import DefaultspackProfileRuntime


class SetupInputError(ValueError):
    """The typed boundary received malformed or unsupported DATA."""


class SetupProjectionError(RuntimeError):
    """The original projection cannot consume the supplied recorded facts."""


class SetupOutputError(RuntimeError):
    """The projected result exceeds the unchanged finite DATA transport."""


def invoke(operation: str, payload: str) -> str:
    """Run exactly one original method, without retries or Host effects."""
    try:
        value = loads(payload)
    except (ValueError, TypeError, OverflowError):
        raise SetupInputError("invalid Setup DATA") from None
    if type(value) is not dict:
        raise SetupInputError("Setup input must be an object")
    runtime = DefaultspackProfileRuntime()
    if operation == "decision":
        if (set(value) != {"body", "listing"}
                or type(value["body"]) is not dict
                or (value["listing"] is not None
                    and type(value["listing"]) is not dict)):
            raise SetupInputError("invalid decision input")
        decision = runtime.setup_activation_decision(value["body"], value["listing"])
        result = {"confirmation": decision.confirmation, "response": decision.response}
    elif operation == "success":
        if (set(value) != {"profile", "plan", "activation", "audit_receipt"}
                or any(type(item) is not dict for item in value.values())):
            raise SetupInputError("invalid success projection input")
        # Read-only attribute views of recorded JSON, never ActiveDefaultProfile
        # or a Host-bound activation/capture/receipt capability.
        active = SimpleNamespace(
            resolved=SimpleNamespace(profile=value["profile"], plan=value["plan"]),
            activation=value["activation"],
        )
        try:
            result = runtime.setup_activation_success(active, value["audit_receipt"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SetupProjectionError("incomplete recorded activation") from None
    elif operation == "failure":
        if value:
            raise SetupInputError("failure projection takes an empty object")
        result = runtime.setup_activation_failure()
    elif operation == "retired":
        if (set(value) != {"route"}
                or (value["route"] is not None and type(value["route"]) is not str)):
            raise SetupInputError("invalid retired route input")
        result = runtime.retired_setup_response(route=value["route"])
    else:
        raise SetupInputError("unknown Setup projection")
    try:
        return dumps(result)
    except (ValueError, TypeError, OverflowError):
        raise SetupOutputError("Setup result outside DATA boundary") from None
