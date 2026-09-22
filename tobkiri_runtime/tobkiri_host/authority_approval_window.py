"""Host-owned narrow port for the authority approval window."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .ports import AuthorityApprovalWindowOpenCommand


class AuthorityApprovalWindowController:
    """Forward one authenticated open request to its composition callback.

    The controller holds no broker identity and trusts no callback-supplied
    request identity: its bounded result echoes only the authenticated
    ``request_id`` carried by the command.
    """

    def __init__(
        self,
        *,
        open_window: Callable[[str], Mapping[str, Any]],
    ) -> None:
        self._open = open_window

    def open_authority_approval_window(
        self,
        command: AuthorityApprovalWindowOpenCommand,
    ) -> Mapping[str, Any]:
        """Open the Launcher approval window for one exact request."""

        result = self._open(command.request_id)
        if not isinstance(result, Mapping) or result.get("opened") is not True:
            raise PermissionError("authority approval window is unavailable")
        return {"opened": True, "request_id": command.request_id}
