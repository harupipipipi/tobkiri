"""Adapt explicit componentize-py generated modules to the guest SDK protocol."""

from typing import Any, Mapping
from enum import Enum
from ._binding import GuestError, bind


class GeneratedImports:
    """Only the generated interfaces included in this Pack's world are exposed."""

    def __init__(self, interfaces: Mapping[str, Any]) -> None:
        self._interfaces = dict(interfaces)

    def invoke(self, interface: str, operation: str, **arguments: Any) -> Any:
        """Marshal generic core records and call a generated import function."""
        module = self._interfaces.get(interface)
        if module is None:
            raise GuestError(f"core interface is not declared by this Pack: {interface}")
        function = getattr(module, operation.replace("-", "_"), None)
        if function is None:
            raise GuestError(f"core operation is not supported: {interface}.{operation}")
        if interface == "network" and operation == "fetch":
            request = dict(arguments["req"])
            request["secret_headers"] = [module.HeaderFromSecret(**header) for header in request["secret_headers"]]
            arguments = {**arguments, "req": module.Request(**request)}
        return function(**arguments)

    def enum_value(self, interface: str, name: str, case: str) -> Any:
        """Resolve only an actual enum in an explicitly bound generated module."""
        module = self._interfaces.get(interface)
        enum = getattr(module, name, None) if module is not None else None
        if not isinstance(enum, type) or not issubclass(enum, Enum):
            raise GuestError(f"generated enum is not declared: {interface}.{name}")
        try:
            return enum[case.replace("-", "_").upper()]
        except KeyError:
            raise GuestError(f"generated enum case is not declared: {name}.{case}") from None


def bind_generated(interfaces: Mapping[str, Any]) -> None:
    """Bind caller-selected generated modules with no provider/interface discovery."""
    bind(GeneratedImports(interfaces))
