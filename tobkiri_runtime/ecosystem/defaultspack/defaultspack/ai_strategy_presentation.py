"""Present signed AI strategy Pack choices without naming any strategy Pack."""

from __future__ import annotations

from typing import Mapping, Protocol, cast

from tobkiri_protocol.canonical import canonical_digest

from .v4_frontend_contributions import project_selected_ai_strategies


_CATALOG_VERSION = "tobkiri.ai-strategy-catalog/v1"


class _StrategyCatalogSession(Protocol):
    """The minimal captured-session surface required for strategy display."""

    selected_pack_closure: tuple[object, ...] | list[object]
    profile_id: str
    profile_revision: str
    activation_id: str
    plan_digest: str

    def assert_current(self) -> None:
        """Reject a stale or revoked captured activation."""


def _strategy_catalog_session(session: object) -> _StrategyCatalogSession | None:
    """Narrow an untyped HTTP session before reading capture-bound fields."""

    closure = getattr(session, "selected_pack_closure", None)
    if not isinstance(closure, (tuple, list)):
        return None
    if not all(
        isinstance(getattr(session, attribute, None), str)
        for attribute in ("profile_id", "profile_revision", "activation_id", "plan_digest")
    ):
        return None
    if not callable(getattr(session, "assert_current", None)):
        return None
    return cast(_StrategyCatalogSession, session)


def present_ai_strategy_catalog(
    result: Mapping[str, object],
    *,
    session: object | None,
) -> Mapping[str, object]:
    """Return choices bound to one verified frontend and execution capture.

    A strategy can be displayed only when its signed frontend descriptor and
    the Host's read-only execution catalog agree on its full reference, owner
    Pack, and artifact digest.  Missing descriptors and stale selections are
    deliberately absent; dispatch still rejects a stale saved reference.
    """

    if session is None:
        return _empty_catalog("strategy catalog capture is unavailable")
    captured_session = _strategy_catalog_session(session)
    if captured_session is None:
        return _empty_catalog("strategy Pack closure is unavailable")
    try:
        captured_session.assert_current()
        strategies, diagnostics, quarantined = project_selected_ai_strategies(
            tuple(
                item
                for item in captured_session.selected_pack_closure
                if isinstance(item, Mapping)
            ),
            result,
            profile_id=captured_session.profile_id,
            profile_revision=captured_session.profile_revision,
            activation_id=captured_session.activation_id,
            plan_digest=captured_session.plan_digest,
        )
        captured_session.assert_current()
    except Exception as exc:
        return _empty_catalog(
            f"strategy catalog projection is unavailable: {type(exc).__name__}"
        )
    public = [_public_strategy(item) for item in strategies]
    return {
        "api_version": _CATALOG_VERSION,
        "strategies": public,
        "count": len(public),
        "catalog_revision": canonical_digest(
            {
                "version": _CATALOG_VERSION,
                "plan_digest": captured_session.plan_digest,
                "strategies": public,
            }
        ),
        "diagnostics": diagnostics,
        "quarantined_pack_ids": quarantined,
    }


def _public_strategy(item: Mapping[str, object]) -> dict[str, object]:
    """Discard internal capture details while retaining trusted UI metadata."""

    fields = {
        "strategy_reference",
        "label",
        "description",
        "command",
        "signature_verified",
        "plan_admitted",
        "available",
    }
    return {key: item[key] for key in fields if key in item}


def _empty_catalog(message: str) -> Mapping[str, object]:
    """Fail closed without making the Settings UI unavailable."""

    return {
        "api_version": _CATALOG_VERSION,
        "strategies": [],
        "count": 0,
        "catalog_revision": canonical_digest(
            {"version": _CATALOG_VERSION, "strategies": []}
        ),
        "diagnostics": [
            {
                "code": "strategy_catalog_unavailable",
                "severity": "error",
                "message": message,
            }
        ],
        "quarantined_pack_ids": [],
    }


__all__ = ["present_ai_strategy_catalog"]
