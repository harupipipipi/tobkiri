from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import statistics
import time
from collections.abc import Iterable, Iterator
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1
EWMA_ALPHA = 0.30
RECENT_WINDOW = 9
MAX_RECENT_SAMPLES = 20
MAX_SERIES = 1000
SERIES_TTL_SECONDS = 30 * 24 * 60 * 60
DIRECT_PROVIDER_EXCLUSIONS = {
    "stub",
    "rumi",
    "human-operator",
    "openrouter",
    "vercel-ai-gateway",
    "gitlawb-opengateway",
    "opencode-go",
    "opencode-zen",
    "ollama",
    "lmstudio",
    "vllm",
    "llamacpp",
    "openai_compatible",
}


def utc_now() -> str:
    """Return a stable UTC timestamp for persisted aggregate metrics."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def endpoint_scope(value: Any) -> str:
    """Normalize an endpoint without preserving query credentials or fragments."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme and parts.hostname:
        host = parts.hostname.lower()
        if parts.port:
            host = f"{host}:{parts.port}"
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), host, path, "", ""))
    return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def is_direct_provider(provider_id: Any) -> bool:
    """Return whether the provider is eligible for measured direct routing."""
    normalized = str(provider_id or "").strip().lower()
    return bool(
        normalized
        and normalized not in DIRECT_PROVIDER_EXCLUSIONS
        and not normalized.startswith("xiaomi-")
    )


class ProviderPerformanceStore:
    """Concurrency-safe, bounded aggregate provider performance storage."""

    def __init__(self, path: Path | None = None) -> None:
        root = Path(__file__).resolve().parents[2] / "user_data" / "shared"
        self.path = path or root / "provider_performance.sqlite3"
        self.key_path = self.path.with_suffix(".hmac.key")

    def connection_scope(self, credential: Any, endpoint: Any) -> str | None:
        """Return a non-reversible scope for a credential and endpoint pair."""
        secret = str(credential or "").strip()
        normalized_endpoint = endpoint_scope(endpoint)
        if not secret or not normalized_endpoint:
            return None
        digest = hmac.new(
            self._hmac_key(),
            f"{normalized_endpoint}\0{secret}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac:{digest}"

    def record(
        self,
        *,
        provider_id: str,
        model_id: str,
        endpoint: str,
        connection_scope: str,
        method: str,
        output_tokens: int,
        generation_seconds: float,
        ttft_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        """Record one successful aggregate sample and return its public summary."""
        if method not in {"stream_generation", "end_to_end_estimate"}:
            return None
        if output_tokens <= 0 or generation_seconds <= 0:
            return None
        provider = str(provider_id or "").strip().lower()
        model = str(model_id or "").strip()
        scope = str(connection_scope or "").strip()
        normalized_endpoint = endpoint_scope(endpoint)
        if not is_direct_provider(provider) or not model or not scope.startswith("hmac:"):
            return None
        tps = float(output_tokens) / float(generation_seconds)
        if not (0 < tps < 1_000_000):
            return None
        now_epoch = int(time.time())
        updated_at = utc_now()
        with closing(self._connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT successful_samples, ewma_tokens_per_second, recent_samples_json
                FROM provider_performance
                WHERE provider_id=? AND model_id=? AND endpoint_scope=?
                  AND connection_scope=? AND method=?
                """,
                (provider, model, normalized_endpoint, scope, method),
            ).fetchone()
            sample_count = int(row[0]) if row else 0
            prior_ewma = float(row[1]) if row else 0.0
            try:
                recent = list(json.loads(row[2])) if row else []
            except (TypeError, ValueError, json.JSONDecodeError):
                recent = []
            recent = [float(item) for item in recent if isinstance(item, (int, float))]
            recent.append(tps)
            recent = recent[-MAX_RECENT_SAMPLES:]
            ewma = tps if sample_count == 0 else EWMA_ALPHA * tps + (1 - EWMA_ALPHA) * prior_ewma
            median_recent = statistics.median(recent[-RECENT_WINDOW:])
            values = (
                SCHEMA_VERSION,
                provider,
                model,
                normalized_endpoint,
                scope,
                method,
                sample_count + 1,
                sample_count + 1,
                tps,
                ewma,
                median_recent,
                max(0.0, float(ttft_seconds)) if ttft_seconds is not None else None,
                json.dumps(recent, separators=(",", ":")),
                updated_at,
                now_epoch,
            )
            connection.execute(
                """
                INSERT INTO provider_performance (
                    schema_version, provider_id, model_id, endpoint_scope,
                    connection_scope, method, samples, successful_samples,
                    latest_tokens_per_second, ewma_tokens_per_second,
                    median_recent_tokens_per_second, latest_ttft_seconds,
                    recent_samples_json, updated_at, updated_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id, model_id, endpoint_scope, connection_scope, method)
                DO UPDATE SET
                    schema_version=excluded.schema_version,
                    samples=excluded.samples,
                    successful_samples=excluded.successful_samples,
                    latest_tokens_per_second=excluded.latest_tokens_per_second,
                    ewma_tokens_per_second=excluded.ewma_tokens_per_second,
                    median_recent_tokens_per_second=excluded.median_recent_tokens_per_second,
                    latest_ttft_seconds=excluded.latest_ttft_seconds,
                    recent_samples_json=excluded.recent_samples_json,
                    updated_at=excluded.updated_at,
                    updated_epoch=excluded.updated_epoch
                """,
                values,
            )
            self._prune(connection, now_epoch)
            connection.commit()
        return self.get(
            provider_id=provider,
            model_id=model,
            endpoint=normalized_endpoint,
            connection_scope=scope,
            method=method,
        )

    def get(
        self,
        *,
        provider_id: str,
        model_id: str,
        endpoint: str,
        connection_scope: str,
        method: str = "stream_generation",
    ) -> dict[str, Any] | None:
        """Return one allowlisted aggregate series without raw samples."""
        try:
            with closing(self._connection()) as connection:
                row = connection.execute(
                    """
                    SELECT schema_version, provider_id, model_id, endpoint_scope,
                           connection_scope, method, samples, successful_samples,
                           latest_tokens_per_second, ewma_tokens_per_second,
                           median_recent_tokens_per_second, latest_ttft_seconds,
                           updated_at, updated_epoch
                    FROM provider_performance
                    WHERE provider_id=? AND model_id=? AND endpoint_scope=?
                      AND connection_scope=? AND method=?
                    """,
                    (
                        str(provider_id).strip().lower(),
                        str(model_id).strip(),
                        endpoint_scope(endpoint),
                        str(connection_scope).strip(),
                        method,
                    ),
                ).fetchone()
        except sqlite3.DatabaseError:
            return None
        if not row:
            return None
        keys = (
            "schema_version", "provider_id", "model_id", "endpoint_scope",
            "connection_scope", "method", "samples", "successful_samples",
            "latest_tokens_per_second", "ewma_tokens_per_second",
            "median_recent_tokens_per_second", "latest_ttft_seconds",
            "updated_at", "updated_epoch",
        )
        return dict(zip(keys, row, strict=True))

    def _connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(100):
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(self.path, timeout=15)
                connection.execute("PRAGMA busy_timeout=15000")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                self._create_schema(connection)
                return connection
            except sqlite3.OperationalError as exc:
                if connection is not None:
                    connection.close()
                if "locked" in str(exc).lower() and attempt < 99:
                    time.sleep(0.02)
                    continue
                raise
            except sqlite3.DatabaseError:
                if connection is not None:
                    connection.close()
                self._quarantine_corrupt_database()
                connection = sqlite3.connect(self.path, timeout=15)
                self._create_schema(connection)
                return connection
        raise sqlite3.OperationalError("provider performance database is locked")

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_performance (
                schema_version INTEGER NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                endpoint_scope TEXT NOT NULL,
                connection_scope TEXT NOT NULL,
                method TEXT NOT NULL,
                samples INTEGER NOT NULL,
                successful_samples INTEGER NOT NULL,
                latest_tokens_per_second REAL NOT NULL,
                ewma_tokens_per_second REAL NOT NULL,
                median_recent_tokens_per_second REAL NOT NULL,
                latest_ttft_seconds REAL,
                recent_samples_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_epoch INTEGER NOT NULL,
                PRIMARY KEY (provider_id, model_id, endpoint_scope, connection_scope, method)
            )
            """
        )

    def _prune(self, connection: sqlite3.Connection, now_epoch: int) -> None:
        connection.execute(
            "DELETE FROM provider_performance WHERE updated_epoch < ?",
            (now_epoch - SERIES_TTL_SECONDS,),
        )
        connection.execute(
            """
            DELETE FROM provider_performance WHERE rowid IN (
                SELECT rowid FROM provider_performance
                ORDER BY updated_epoch DESC LIMIT -1 OFFSET ?
            )
            """,
            (MAX_SERIES,),
        )

    def _quarantine_corrupt_database(self) -> None:
        if not self.path.exists():
            return
        quarantine = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
        try:
            self.path.replace(quarantine)
        except OSError:
            raise sqlite3.DatabaseError("provider performance database is corrupt")

    def _hmac_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            try:
                value = self.key_path.read_bytes()
                if len(value) >= 32:
                    return value
            except FileNotFoundError:
                value = secrets.token_bytes(32)
                try:
                    with self.key_path.open("xb") as handle:
                        handle.write(value)
                        handle.flush()
                        os.fsync(handle.fileno())
                    try:
                        self.key_path.chmod(0o600)
                    except OSError:
                        pass
                    return value
                except FileExistsError:
                    pass
            time.sleep(0.01)
        raise RuntimeError("provider performance HMAC key is unavailable")


def provider_measurement_context(
    provider_id: str,
    model_id: str,
    provider: Any,
    *,
    store: ProviderPerformanceStore | None = None,
) -> dict[str, str] | None:
    """Build a privacy-safe measurement context without retaining credentials."""
    if not is_direct_provider(provider_id):
        return None
    provider_class = provider.__class__
    class_token = provider_class.__name__.lower()
    module_token = str(provider_class.__module__ or "").lower()
    if any(token in class_token for token in ("fake", "dummy", "stub")) or module_token.startswith(
        "tests"
    ):
        return None
    endpoint = endpoint_scope(
        getattr(provider, "_base_url", "") or getattr(provider, "BASE_URL", "")
    )
    credential = getattr(provider, "_api_key", "")
    target_store = store or ProviderPerformanceStore()
    try:
        scope = target_store.connection_scope(credential, endpoint)
    except (OSError, RuntimeError):
        return None
    if not scope:
        return None
    return {
        "provider_id": str(provider_id).strip().lower(),
        "model_id": str(model_id).strip(),
        "endpoint_scope": endpoint,
        "connection_scope": scope,
    }


def select_fast_model(
    models: Iterable[dict[str, Any]],
    providers: dict[str, Any],
    *,
    current_model: str,
    min_samples: int = 3,
    required_context_tokens: int = 0,
    requires_tools: bool = False,
    requires_image: bool = False,
    store: ProviderPerformanceStore | None = None,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    """Select the fastest compatible direct model with sufficient fresh samples."""
    target_store = store or ProviderPerformanceStore()
    threshold = max(1, int(min_samples or 3))
    current_time = int(now_epoch if now_epoch is not None else time.time())
    ranked: list[tuple[tuple[float, float, float, int], dict[str, Any]]] = []
    for model in models:
        if not isinstance(model, dict) or not _is_compatible_chat_model(
            model,
            required_context_tokens=required_context_tokens,
            requires_tools=requires_tools,
            requires_image=requires_image,
        ):
            continue
        provider_id = str(model.get("provider_id") or model.get("provider") or "").strip().lower()
        provider = providers.get(provider_id)
        if provider is None or not is_direct_provider(provider_id):
            continue
        model_id = str(model.get("model_id") or model.get("model") or "").strip()
        qualified = str(
            model.get("qualified_model_id")
            or model.get("profile_id")
            or model.get("id")
            or (f"{provider_id}/{model_id}" if model_id else "")
        ).strip()
        if not model_id or not qualified:
            continue
        context = provider_measurement_context(
            provider_id,
            model_id,
            provider,
            store=target_store,
        )
        if not context:
            continue
        series = target_store.get(
            provider_id=provider_id,
            model_id=model_id,
            endpoint=context["endpoint_scope"],
            connection_scope=context["connection_scope"],
        )
        if not series or int(series.get("successful_samples") or 0) < threshold:
            continue
        if current_time - int(series.get("updated_epoch") or 0) > SERIES_TTL_SECONDS:
            continue
        median_tps = float(series.get("median_recent_tokens_per_second") or 0.0)
        ewma_tps = float(series.get("ewma_tokens_per_second") or 0.0)
        ttft = float(series.get("latest_ttft_seconds") or 1_000_000.0)
        samples = int(series.get("successful_samples") or 0)
        ranked.append(
            (
                (median_tps, ewma_tps, -ttft, samples),
                {
                    "profile_id": qualified,
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "successful_samples": samples,
                    "median_recent_tokens_per_second": median_tps,
                    "ewma_tokens_per_second": ewma_tps,
                    "latest_ttft_seconds": series.get("latest_ttft_seconds"),
                },
            )
        )
    if not ranked:
        return {
            "selected_model": current_model,
            "changed": False,
            "reason": "INSUFFICIENT_PERFORMANCE_SAMPLES",
        }
    ranked.sort(key=lambda item: item[0], reverse=True)
    selected = ranked[0][1]
    selected["selected_model"] = selected.pop("profile_id")
    selected["changed"] = selected["selected_model"] != current_model
    selected["reason"] = "MEASURED_DIRECT_PROVIDER_TPS"
    return selected


def record_complete_response(
    response: Any,
    context: dict[str, str] | None,
    started_at: float,
    *,
    store: ProviderPerformanceStore | None = None,
    ended_at: float | None = None,
) -> dict[str, Any] | None:
    """Record a successful non-stream response when provider usage is reliable."""
    if not context or not isinstance(response, dict):
        return None
    metadata = response.get("metadata") if isinstance(response.get("metadata"), dict) else {}
    if metadata.get("cache_replay") or not _actual_model_matches(response, context["model_id"]):
        return None
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    output_tokens = _output_tokens(usage)
    if output_tokens <= 0:
        return None
    duration = float(ended_at if ended_at is not None else time.monotonic()) - started_at
    return (store or ProviderPerformanceStore()).record(
        provider_id=context["provider_id"],
        model_id=context["model_id"],
        endpoint=context["endpoint_scope"],
        connection_scope=context["connection_scope"],
        method="end_to_end_estimate",
        output_tokens=output_tokens,
        generation_seconds=duration,
    )


def track_stream(
    events: Iterable[dict[str, Any]],
    context: dict[str, str] | None,
    started_at: float,
    *,
    store: ProviderPerformanceStore | None = None,
    clock: Any = time.monotonic,
) -> Iterator[dict[str, Any]]:
    """Yield stream events unchanged and record only a complete final-usage sample."""
    first_delta_at: float | None = None
    final_usage: dict[str, Any] | None = None
    completed = False
    final_event: dict[str, Any] | None = None
    for event in events:
        now = float(clock())
        if first_delta_at is None and _is_content_delta(event):
            first_delta_at = now
        if _is_final_event(event):
            usage = event.get("usage")
            final_usage = dict(usage) if isinstance(usage, dict) else None
            completed = True
            final_event = event
        yield event
    ended_at = float(clock())
    output_tokens = _output_tokens(final_usage or {})
    valid_final = bool(
        final_event
        and str(final_event.get("finish_reason") or "").lower()
        not in {"cancelled", "canceled", "abort", "aborted", "error"}
        and not final_event.get("cache_replay")
        and (not context or _actual_model_matches(final_event, context["model_id"]))
    )
    if context and completed and valid_final and first_delta_at is not None and output_tokens > 0:
        try:
            (store or ProviderPerformanceStore()).record(
                provider_id=context["provider_id"],
                model_id=context["model_id"],
                endpoint=context["endpoint_scope"],
                connection_scope=context["connection_scope"],
                method="stream_generation",
                output_tokens=output_tokens,
                generation_seconds=ended_at - first_delta_at,
                ttft_seconds=first_delta_at - started_at,
            )
        except Exception:
            return


def _output_tokens(usage: dict[str, Any]) -> int:
    for key in ("output_tokens", "completion_tokens"):
        try:
            value = int(usage.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _is_compatible_chat_model(
    model: dict[str, Any],
    *,
    required_context_tokens: int,
    requires_tools: bool,
    requires_image: bool,
) -> bool:
    model_type = str(model.get("type") or "chat").strip().lower()
    if model_type not in {"", "chat", "reasoning"}:
        return False
    availability = model.get("availability") if isinstance(model.get("availability"), dict) else {}
    if availability and not bool(
        availability.get("configured")
        or availability.get("active")
        or availability.get("available")
    ):
        return False
    capabilities = model.get("capabilities") if isinstance(model.get("capabilities"), dict) else {}
    metadata = model.get("metadata") if isinstance(model.get("metadata"), dict) else {}
    metadata_capabilities = (
        metadata.get("capabilities") if isinstance(metadata.get("capabilities"), dict) else {}
    )

    def supports(*keys: str) -> bool:
        return any(
            bool(model.get(key) or capabilities.get(key) or metadata_capabilities.get(key))
            for key in keys
        )

    if requires_tools and not supports("supports_tool_calling", "tool_calling", "tool_calls"):
        return False
    if requires_image and not supports("supports_vision", "image_input", "vision"):
        return False
    context_window = 0
    for value in (
        model.get("context_window"),
        model.get("max_context_tokens"),
        metadata.get("context_window"),
    ):
        try:
            context_window = max(context_window, int(value or 0))
        except (TypeError, ValueError):
            continue
    return required_context_tokens <= 0 or context_window >= required_context_tokens


def _is_content_delta(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "")
    if event_type in {"text_delta", "reasoning_delta"}:
        return bool(event.get("text") or event.get("delta"))
    if event_type == "content_delta":
        delta = event.get("delta")
        return bool(delta.get("text") if isinstance(delta, dict) else delta)
    return False


def _is_final_event(event: dict[str, Any]) -> bool:
    return str(event.get("type") or "") == "stream_end" or bool(
        event.get("finish_reason") and isinstance(event.get("usage"), dict)
    )


def _actual_model_matches(payload: dict[str, Any], requested_model: str) -> bool:
    raw_extra = payload.get("raw_extra") if isinstance(payload.get("raw_extra"), dict) else {}
    actual = str(
        payload.get("actual_model_id")
        or payload.get("model_id")
        or payload.get("model")
        or raw_extra.get("model")
        or ""
    ).strip()
    if not actual:
        return True
    requested = str(requested_model or "").strip()
    return actual == requested or actual.endswith(f"/{requested}")
