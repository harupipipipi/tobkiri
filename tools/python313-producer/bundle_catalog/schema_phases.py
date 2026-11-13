"""Private markers for the actual, unsplit App cold schema-store operation."""

from hashlib import sha256
from wit_world.imports.bindings import checkpoint

_observing = False
_schema_name: str | None = None


def begin_cold_schema_store() -> None:
    """Enable the fixed observer only within the diagnostic cold export."""
    global _observing
    if _observing:
        raise RuntimeError('nested cold schema phase observation')
    _observing = True


def end_cold_schema_store() -> None:
    """End observation without changing the original cache or output."""
    global _observing, _schema_name
    _observing = False
    _schema_name = None


def bind_schema(name: str) -> None:
    """Use the name from the real load_schema path, immediately before the check."""
    global _schema_name
    if _observing:
        if type(name) is not str or not 0 < len(name) <= 128 or ':' in name:
            raise RuntimeError('invalid cold schema phase name')
        _schema_name = name


def observing() -> bool:
    """Identify the explicitly bound diagnostic operation."""
    return _observing


def seed() -> int:
    """Select a repeatable sampling offset for the actual bound schema."""
    if not _observing or _schema_name is None:
        raise RuntimeError('missing cold schema name for sample seed')
    return int.from_bytes(sha256(_schema_name.encode()).digest()[:4], 'little')


def mark(point: str, bucket: str | None = None, ordinal: int = 0) -> None:
    """Send one fixed phase or sample and actual schema name."""
    if not _observing or _schema_name is None:
        raise RuntimeError('unknown cold schema phase or missing schema name')
    if bucket is None and point in ('setup', 'initial-reference', 'evaluation', 'return'):
        checkpoint(f'{point}:{_schema_name}')
    elif (point in ('sample-begin', 'sample-end', 'sample-count')
          and bucket in ('uri', 'registry', 'pointer', 'anchor', 'marker')
          and type(ordinal) is int and 0 <= ordinal < 2**32):
        checkpoint(f'{point}:{bucket}:{ordinal}:{_schema_name}')
    else:
        raise RuntimeError('unknown cold schema sample')
