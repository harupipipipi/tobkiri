"""Incremental frame sanitation before any provider Pack receives response data."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Iterator, Mapping

from tobkiri_protocol.provider_sse_v1 import SSEDecoder


class StreamSecretFilter:
    """Withhold secret prefixes across event boundaries, including tool strings."""

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self._pending: dict[tuple[str | int, ...], tuple[str, dict[str, Any]]] = {}

    def apply(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Redact complete secrets and retain only ambiguous string suffixes."""

        result = deepcopy(dict(record))

        def visit(value: Any, path: tuple[str | int, ...]) -> Any:
            if isinstance(value, dict):
                return {key: visit(item, (*path, key)) for key, item in value.items()}
            if isinstance(value, list):
                return [visit(item, (*path, index)) for index, item in enumerate(value)]
            if not isinstance(value, str) or not self._secret:
                return value
            prior = self._pending.pop(path, ("", {}))[0]
            text = (prior + value).replace(self._secret, "[REDACTED]")
            held = 0
            for length in range(min(len(text), len(self._secret) - 1), 0, -1):
                if text.endswith(self._secret[:length]):
                    held = length
                    break
            if held:
                if len(self._pending) >= 256:
                    raise ValueError("provider stream sanitation exceeds its limit")
                self._pending[path] = (text[-held:], deepcopy(dict(record)))
                return text[:-held]
            return text

        result["data"] = visit(result.get("data"), ())
        return result

    def finish(self) -> list[dict[str, Any]]:
        """Release harmless residual prefixes before the terminal provider event."""

        records = []
        for path, (tail, original) in self._pending.items():

            def blank(value: Any) -> Any:
                if isinstance(value, dict):
                    return {
                        key: (item if key == "type" else blank(item)) for key, item in value.items()
                    }
                if isinstance(value, list):
                    return [blank(item) for item in value]
                return "" if isinstance(value, str) else value

            original["data"] = blank(original["data"])
            node = original["data"]
            for key in path[:-1]:
                node = node[key]
            if path:
                node[path[-1]] = tail
            records.append(original)
        self._pending.clear()
        return records


def received_sse(
    response: Any,
    *,
    secret: str = "",
    sanitize: Callable[[Any, str], Any] | None = None,
    guard: Callable[[], None],
) -> Iterator[dict[str, Any]]:
    """Yield sanitized actual frames while the owned response is still open."""

    decoder, redactor = SSEDecoder(), StreamSecretFilter(secret)
    reader = getattr(response, "read1", None) or response.read
    terminal = False
    while True:
        guard()
        chunk = reader(4096)
        guard()
        if not chunk:
            break
        for record in decoder.feed(chunk):
            guard()
            if terminal:
                raise ValueError("provider stream has data after its terminal event")
            if record["done"] or record.get("data", {}).get("type") == "message_stop":
                for final in redactor.finish():
                    guard()
                    if sanitize is not None:
                        final["data"] = sanitize(final["data"], secret)
                    yield final
                terminal = True
            clean = redactor.apply(record)
            if sanitize is not None:
                clean["data"] = sanitize(clean["data"], secret)
            yield clean
        if terminal:
            break
    decoder.finish()
    if not terminal:
        raise ValueError("provider stream ended without a terminal event")
