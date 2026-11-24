"""Install a private import audit observer around the original probe run."""

import sys

from wit_world.imports import import_trace
from baseline_app import Probe as _BaselineProbe

_record_import = import_trace.record_import


def _import_audit(event: str, args: tuple[object, ...]) -> None:
    if event == "import":
        name = args[0] if args and type(args[0]) is str else ""
        _record_import(name)


class Probe(_BaselineProbe):
    """Use the original probe and report only bounded import names natively."""

    def __init__(self) -> None:
        super().__init__()
        self._import_audit_installed = False

    def run(self) -> str:
        """Observe import audit events, then call the unchanged original run."""
        if not self._import_audit_installed:
            sys.addaudithook(_import_audit)
            self._import_audit_installed = True
        return super().run()
