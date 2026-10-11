"""Provider-neutral protocol execution adapters."""

from .adapter import (
    create_embedding_operation,
    create_generate_operation,
    create_image_operation,
    create_stream_operation,
)

__all__ = [
    "create_embedding_operation",
    "create_generate_operation",
    "create_image_operation",
    "create_stream_operation",
]

