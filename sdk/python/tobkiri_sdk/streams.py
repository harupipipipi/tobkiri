"""Pull-based core streams and cancellation; no threads or guest sockets."""

import json
from typing import Any, Iterator
from ._binding import invoke


def pull(handle: Any, max_events: int = 16) -> list[Any]:
    """Pull a bounded batch; handles stay scoped to the core call context."""
    if not 0 < max_events <= 1024:
        raise ValueError("max_events must be between 1 and 1024")
    return [json.loads(value) for value in invoke("flow", "pull", s=handle, max=max_events)]


def cancel(handle: Any) -> None:
    """Cancel a core stream and drop the provider's outstanding response."""
    invoke("flow", "cancel-stream", s=handle)


def events(handle: Any) -> Iterator[Any]:
    """Iterate batches until the provider reaches EOF."""
    while True:
        batch = pull(handle)
        if not batch:
            return
        yield from batch
