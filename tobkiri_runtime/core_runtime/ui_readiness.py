"""Bounded UI bootstrap readiness for the canonical Pack v4 Host.

The checker deliberately uses only the immutable frontend contract map, the
captured dispatch session, fixed first-party web mounts, and the Host-owned
panel authentication manager.  It never discovers a route, imports a Pack
handler, or falls back to a legacy HTTP registry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

from .api.web_mounts import WebMountEntry
from .frontend_contract_routes import FrontendContractBinding
from .host_contract import host_contract_value
from .panel_auth import PanelAuthManager


logger = logging.getLogger(__name__)

UI_READINESS_SCHEMA = "io.tobkiri.ui-readiness.v1"
UI_READINESS_PATH = "/ui-readiness"
UI_READINESS_CHALLENGE_HEADER = "X-Rumi-Desktop-Health-Challenge"
UI_READINESS_AUTHORIZATION_HEADER = "X-Tobkiri-UI-Readiness-Authorization"
_DESKTOP_HEALTH_KEY_LABEL = "tobkiri-desktop-health-key-v1"
_UI_READINESS_KEY_LABEL = "tobkiri-ui-readiness-key-v1"
REQUIRED_UI_READINESS_PROBES = (
    "static_bundle",
    "chat_route",
    "ui_catalog",
    "settings",
    "model_catalog",
    "tool_catalog",
    "conversation_bootstrap",
    "default_conversation_load",
    "auth_session",
)

class ReadinessDispatchSession(Protocol):
    """Captured Broker operations used by UI readiness probes."""

    @property
    def profile_id(self) -> str:
        """Return the captured Profile identity."""

    @property
    def plan_digest(self) -> str:
        """Return the captured ResolvedPlan digest."""

    def assert_current(self) -> None:
        """Reject a stale or revoked capture."""

    def assert_operation_ready(self, contract_id: str, operation_id: str) -> None:
        """Reject an operation without its exact production backend."""

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, object],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, object]:
        """Invoke one exact captured operation through RequestBroker."""


@dataclass(frozen=True)
class ProbeOutcome:
    """A non-sensitive result from one named bootstrap probe."""

    status: str = "UP"
    code: str = "READY"
    message: str = "ready"


@dataclass(frozen=True)
class ProbeSpec:
    """One bounded UI bootstrap readiness probe."""

    name: str
    check: Callable[[], ProbeOutcome]


class UIReadinessProbeError(RuntimeError):
    """A typed, safe failure for a readiness probe."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


def ui_readiness_request_proof(secret: str, challenge: str) -> str:
    """Authenticate a Launcher-owned readiness request without sending a secret."""

    return _ui_readiness_proof(secret, "request", challenge)


def ui_readiness_response_proof(secret: str, challenge: str) -> str:
    """Authenticate a readiness response to its requesting Launcher."""

    return _ui_readiness_proof(secret, "response", challenge)


def _ui_readiness_proof(secret: str, direction: str, challenge: str) -> str:
    return _domain_separated_proof(
        secret,
        _UI_READINESS_KEY_LABEL,
        f"{direction}:{challenge}",
    )


def desktop_health_response_proof(secret: str, challenge: str) -> str:
    """Authenticate liveness without exposing an oracle for other protocols."""

    return _domain_separated_proof(
        secret,
        _DESKTOP_HEALTH_KEY_LABEL,
        challenge,
    )


def _domain_separated_proof(secret: str, label: str, message: str) -> str:
    derived_key = hmac.new(
        secret.encode("utf-8"),
        label.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.new(
        derived_key.encode("ascii"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@dataclass
class _ProbeWorker:
    thread: threading.Thread
    results: queue.Queue[tuple[str, object]]
    started_at: float


class UIReadinessChecker:
    """Run the complete UI bootstrap contract with bounded daemon workers."""

    def __init__(
        self,
        probes: tuple[ProbeSpec, ...],
        *,
        timeout_seconds: float = 2.0,
        cache_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        identity: Mapping[str, str] | None = None,
    ) -> None:
        if not probes:
            raise ValueError("UI readiness requires at least one probe")
        names = tuple(probe.name for probe in probes)
        if len(set(names)) != len(names) or any(not name for name in names):
            raise ValueError("UI readiness probe names must be unique and non-empty")
        if timeout_seconds <= 0 or cache_seconds < 0:
            raise ValueError("UI readiness timing values are invalid")
        self._probes = probes
        self._timeout_seconds = float(timeout_seconds)
        self._cache_seconds = float(cache_seconds)
        self._clock = clock
        self._identity = dict(identity or {})
        self._lock = threading.Lock()
        self._assessment_lock = threading.Lock()
        self._workers: dict[str, _ProbeWorker] = {}
        self._cached: dict[str, object] | None = None
        self._cached_at = 0.0

    def snapshot(self, *, force: bool = False) -> dict[str, object]:
        """Return one bounded, named UI readiness assessment."""

        now = self._clock()
        with self._lock:
            if (
                not force
                and self._cached is not None
                and now - self._cached_at < self._cache_seconds
            ):
                return _copy_snapshot(self._cached)

        with self._assessment_lock:
            now = self._clock()
            with self._lock:
                if (
                    not force
                    and self._cached is not None
                    and now - self._cached_at < self._cache_seconds
                ):
                    return _copy_snapshot(self._cached)
            snapshot = self._assess()
            with self._lock:
                self._cached = snapshot
                self._cached_at = self._clock()
            return _copy_snapshot(snapshot)

    def _assess(self) -> dict[str, object]:
        started_at = self._clock()
        workers: dict[str, _ProbeWorker] = {}
        immediate: dict[str, dict[str, object]] = {}
        for probe in self._probes:
            worker, prior = self._worker_for(probe)
            if prior is not None:
                immediate[probe.name] = prior
            elif worker is not None:
                workers[probe.name] = worker

        deadline = started_at + self._timeout_seconds
        results = dict(immediate)
        for probe in self._probes:
            if probe.name in results:
                continue
            worker = workers[probe.name]
            remaining = max(0.0, deadline - self._clock())
            try:
                kind, value = worker.results.get(timeout=remaining)
            except queue.Empty:
                results[probe.name] = _probe_result(
                    "DOWN",
                    "PROBE_TIMEOUT",
                    "probe exceeded its readiness deadline",
                    (self._clock() - worker.started_at) * 1000.0,
                )
                continue
            results[probe.name] = self._completed_result(
                probe.name,
                worker,
                kind,
                value,
            )

        ordered = {probe.name: results[probe.name] for probe in self._probes}
        statuses = {str(result["status"]) for result in ordered.values()}
        if "DOWN" in statuses:
            status = "DOWN"
        elif statuses & {"DEGRADED", "UNKNOWN"}:
            status = "DEGRADED"
        else:
            status = "UP"
        snapshot: dict[str, object] = {
            "schema": UI_READINESS_SCHEMA,
            "status": status,
            "ready": status == "UP",
            "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "duration_ms": round((self._clock() - started_at) * 1000.0, 2),
            "probes": ordered,
        }
        snapshot.update(self._identity)
        return snapshot

    def _worker_for(
        self,
        probe: ProbeSpec,
    ) -> tuple[_ProbeWorker | None, dict[str, object] | None]:
        with self._lock:
            existing = self._workers.get(probe.name)
            if existing is not None:
                if existing.thread.is_alive():
                    return None, _probe_result(
                        "DOWN",
                        "PROBE_STILL_RUNNING",
                        "the prior readiness probe is still running",
                        (self._clock() - existing.started_at) * 1000.0,
                    )
                try:
                    kind, value = existing.results.get_nowait()
                except queue.Empty:
                    kind, value = "error", RuntimeError("probe returned no result")
                result = self._completed_result(
                    probe.name,
                    existing,
                    kind,
                    value,
                    lock_held=True,
                )
                return None, result

            results: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

            def run() -> None:
                try:
                    results.put_nowait(("ok", probe.check()))
                except Exception as error:  # readiness must convert all failures
                    results.put_nowait(("error", error))

            thread = threading.Thread(
                target=run,
                name=f"tobkiri-ui-readiness-{probe.name}",
                daemon=True,
            )
            worker = _ProbeWorker(thread, results, self._clock())
            self._workers[probe.name] = worker
            thread.start()
            return worker, None

    def _completed_result(
        self,
        name: str,
        worker: _ProbeWorker,
        kind: str,
        value: object,
        *,
        lock_held: bool = False,
    ) -> dict[str, object]:
        duration_ms = (self._clock() - worker.started_at) * 1000.0
        if not lock_held:
            with self._lock:
                if self._workers.get(name) is worker:
                    self._workers.pop(name, None)
        elif self._workers.get(name) is worker:
            self._workers.pop(name, None)
        if kind == "ok" and isinstance(value, ProbeOutcome):
            return _probe_result(
                value.status,
                value.code,
                value.message,
                duration_ms,
            )
        if isinstance(value, UIReadinessProbeError):
            return _probe_result(
                "DOWN",
                value.code,
                value.safe_message,
                duration_ms,
            )
        logger.warning(
            "UI readiness probe failed: %s (%s)",
            name,
            type(value).__name__,
        )
        return _probe_result(
            "DOWN",
            "PROBE_FAILED",
            "probe failed; inspect local launch diagnostics",
            duration_ms,
        )


def build_ui_readiness_checker(
    *,
    dispatch_session: ReadinessDispatchSession | None,
    contract_routes: Mapping[tuple[str, str], FrontendContractBinding],
    web_mounts: tuple[Mapping[str, object], ...],
    panel_auth_manager: PanelAuthManager | None,
    timeout_seconds: float = 2.0,
) -> UIReadinessChecker:
    """Build the canonical readiness probes from captured Host state."""

    routes = dict(contract_routes)
    mounts = tuple(dict(mount) for mount in web_mounts)

    def static_bundle() -> ProbeOutcome:
        mount = _mount(mounts, "/static")
        if mount is None:
            raise UIReadinessProbeError(
                "STATIC_BUNDLE_MOUNT_MISSING", "static bundle mount is unavailable"
            )
        index = _mount_index(mount)
        _validate_bundle_assets(index, mount)
        return ProbeOutcome(message="static bundle is available")

    def chat_route() -> ProbeOutcome:
        mount = _mount(mounts, "/chat")
        if mount is None:
            raise UIReadinessProbeError("CHAT_ROUTE_MISSING", "the /chat surface is unavailable")
        _mount_index(mount)
        return ProbeOutcome(message="chat route is available")

    def auth_session() -> ProbeOutcome:
        bootstrap_secret = host_contract_value("panel_bootstrap_secret")
        if (
            panel_auth_manager is None
            or not bootstrap_secret
            or not panel_auth_manager.validate_bootstrap_secret(bootstrap_secret)
        ):
            raise UIReadinessProbeError(
                "AUTH_SESSION_UNAVAILABLE",
                "Launcher-bound panel authentication is unavailable",
            )
        return ProbeOutcome(message="panel authentication is Launcher-bound")

    route_probes = (
        ("ui_catalog", "GET", "/api/ui/catalog", True),
        ("settings", "GET", "/api/runtime-surface/settings", True),
        ("model_catalog", "GET", "/api/ai/profiles", True),
        ("tool_catalog", "GET", "/api/tools/catalog", True),
        ("conversation_bootstrap", "GET", "/api/chat/conversations", True),
        (
            "default_conversation_load",
            "GET",
            "/api/chat/default-conversation",
            True,
        ),
    )
    probes = [
        ProbeSpec("static_bundle", static_bundle),
        ProbeSpec("chat_route", chat_route),
    ]
    probes.extend(
        ProbeSpec(
            name,
            _contract_probe(
                dispatch_session,
                routes,
                method,
                path,
                execute=execute,
            ),
        )
        for name, method, path, execute in route_probes
    )
    probes.append(ProbeSpec("auth_session", auth_session))
    identity: dict[str, str] = {
        "contract_map_digest": _contract_map_digest(routes),
    }
    if dispatch_session is not None:
        identity.update(
            profile_id=str(dispatch_session.profile_id),
            plan_digest=str(dispatch_session.plan_digest),
        )
    return UIReadinessChecker(
        tuple(probes),
        timeout_seconds=timeout_seconds,
        identity=identity,
    )


def _contract_probe(
    session: ReadinessDispatchSession | None,
    routes: Mapping[tuple[str, str], FrontendContractBinding],
    method: str,
    path: str,
    *,
    execute: bool,
) -> Callable[[], ProbeOutcome]:
    def check() -> ProbeOutcome:
        binding = routes.get((method, path))
        if binding is None:
            raise UIReadinessProbeError(
                "BOOTSTRAP_ROUTE_MISSING", f"{method} {path} is not captured"
            )
        if session is None:
            raise UIReadinessProbeError(
                "RESOLVED_PLAN_UNAVAILABLE", "captured UI dispatch is unavailable"
            )
        try:
            session.assert_current()
        except Exception as error:
            raise UIReadinessProbeError(
                "STALE_RESOLUTION",
                "captured ProfileLock or ResolvedPlan is no longer current",
            ) from error
        for target in binding.targets:
            try:
                session.assert_operation_ready(target.contract_id, target.operation_id)
            except Exception as error:
                raise UIReadinessProbeError(
                    "BOOTSTRAP_PROVIDER_UNAVAILABLE",
                    f"{method} {path} has no exact production-ready Provider",
                ) from error
            if not execute:
                continue
            try:
                result = session.invoke(
                    target.contract_id,
                    target.operation_id,
                    {"_session_id": "ui-readiness"},
                )
            except TimeoutError as error:
                raise UIReadinessProbeError(
                    "BOOTSTRAP_OPERATION_TIMEOUT",
                    f"{method} {path} exceeded its operation deadline",
                ) from error
            except Exception as error:
                raise UIReadinessProbeError(
                    "BOOTSTRAP_OPERATION_FAILED",
                    f"{method} {path} failed through its exact Provider",
                ) from error
            if not isinstance(result, Mapping) or result.get("state") == "error":
                raise UIReadinessProbeError(
                    "BOOTSTRAP_OPERATION_FAILED",
                    f"{method} {path} did not return a usable projection",
                )
        return ProbeOutcome(message=f"{method} {path} is ready")

    return check


def defaultspack_ui_web_mounts() -> tuple[WebMountEntry, ...]:
    """Return the canonical fixed Defaultspack chat and static bundle mounts."""

    ui_root = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack" / "ui"
    return (
        {
            "path_prefix": "/chat",
            "web_root": ui_root,
            "spa_fallback": True,
            "index_file": "shell.html",
            "auth_required": True,
        },
        {
            "path_prefix": "/static",
            "web_root": ui_root,
            "spa_fallback": False,
            "index_file": "shell.html",
            "auth_required": True,
        },
    )


def _mount(
    mounts: tuple[dict[str, object], ...],
    prefix: str,
) -> dict[str, object] | None:
    return next((mount for mount in mounts if mount.get("path_prefix") == prefix), None)


def _mount_index(mount: Mapping[str, object]) -> Path:
    root = mount.get("web_root")
    index_file = mount.get("index_file")
    if not isinstance(root, Path) or not isinstance(index_file, str) or not index_file:
        raise UIReadinessProbeError(
            "STATIC_BUNDLE_INVALID", "static bundle configuration is invalid"
        )
    try:
        resolved_root = root.resolve(strict=True)
        index = (resolved_root / index_file).resolve(strict=True)
        index.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise UIReadinessProbeError(
            "STATIC_BUNDLE_MISSING", "static bundle entrypoint is unavailable"
        ) from error
    if not index.is_file() or index.stat().st_size <= 0:
        raise UIReadinessProbeError(
            "STATIC_BUNDLE_MISSING", "static bundle entrypoint is unavailable"
        )
    return index


_MODULE_ASSET_RE = re.compile(
    r"(?:from|import\()\s*[\"']([^\"']+\.js)[\"']",
)


class _BundleAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.references.append(str(values["src"]))
        if tag == "link" and "stylesheet" in str(values.get("rel") or "").split():
            if values.get("href"):
                self.references.append(str(values["href"]))


def _validate_bundle_assets(index: Path, mount: Mapping[str, object]) -> None:
    """Require every local script and stylesheet referenced by the entrypoint."""

    root = mount.get("web_root")
    prefix = mount.get("path_prefix")
    if not isinstance(root, Path) or not isinstance(prefix, str):
        raise UIReadinessProbeError(
            "STATIC_BUNDLE_INVALID", "static bundle configuration is invalid"
        )
    try:
        document = index.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise UIReadinessProbeError(
            "STATIC_BUNDLE_MISSING", "static bundle entrypoint is unreadable"
        ) from error
    resolved_root = root.resolve(strict=True)
    parser = _BundleAssetParser()
    parser.feed(document)
    referenced: list[Path] = []
    pending = [(reference, resolved_root) for reference in parser.references]
    seen: set[Path] = set()
    while pending:
        raw_reference, base = pending.pop()
        reference = raw_reference.split("?", 1)[0].split("#", 1)[0]
        if not reference or reference.startswith(("data:", "http:", "https:")):
            continue
        if reference.startswith(f"{prefix}/"):
            reference = reference[len(prefix) + 1 :]
            base = resolved_root
        elif reference.startswith("/"):
            continue
        candidate = (base / reference).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError as error:
            raise UIReadinessProbeError(
                "STATIC_BUNDLE_INVALID", "static bundle asset escapes its mount"
            ) from error
        if candidate in seen:
            continue
        seen.add(candidate)
        referenced.append(candidate)
        if candidate.suffix == ".js" and candidate.is_file():
            try:
                module_source = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise UIReadinessProbeError(
                    "STATIC_BUNDLE_ASSETS_MISSING",
                    "a referenced static bundle module is unreadable",
                ) from error
            pending.extend(
                (module_reference, candidate.parent)
                for module_reference in _MODULE_ASSET_RE.findall(module_source)
            )
    if not referenced:
        raise UIReadinessProbeError(
            "STATIC_BUNDLE_ASSETS_MISSING",
            "static bundle has no local script or stylesheet assets",
        )
    if any(not asset.is_file() or asset.stat().st_size <= 0 for asset in referenced):
        raise UIReadinessProbeError(
            "STATIC_BUNDLE_ASSETS_MISSING",
            "a referenced static bundle asset is unavailable",
        )


def _probe_result(
    status: str,
    code: str,
    message: str,
    duration_ms: float,
) -> dict[str, object]:
    normalized = status if status in {"UP", "DOWN", "DEGRADED", "UNKNOWN"} else "DOWN"
    return {
        "status": normalized,
        "code": code,
        "message": message,
        "duration_ms": round(max(0.0, duration_ms), 2),
    }


def _contract_map_digest(
    routes: Mapping[tuple[str, str], FrontendContractBinding],
) -> str:
    document = [
        {
            "method": method,
            "path": path,
            "targets": [
                {
                    "contract_id": target.contract_id,
                    "operation_id": target.operation_id,
                    "provider_id": target.provider_id,
                    "function_id": target.function_id,
                }
                for target in binding.targets
            ],
        }
        for (method, path), binding in sorted(routes.items())
    ]
    payload = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _copy_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    copied = dict(snapshot)
    probes = snapshot.get("probes")
    if isinstance(probes, Mapping):
        copied["probes"] = {
            str(name): dict(result) if isinstance(result, Mapping) else result
            for name, result in probes.items()
        }
    return copied


__all__ = [
    "ProbeOutcome",
    "ProbeSpec",
    "REQUIRED_UI_READINESS_PROBES",
    "UI_READINESS_PATH",
    "UI_READINESS_SCHEMA",
    "UI_READINESS_AUTHORIZATION_HEADER",
    "UI_READINESS_CHALLENGE_HEADER",
    "UIReadinessChecker",
    "UIReadinessProbeError",
    "build_ui_readiness_checker",
    "defaultspack_ui_web_mounts",
    "desktop_health_response_proof",
    "ui_readiness_request_proof",
    "ui_readiness_response_proof",
]
