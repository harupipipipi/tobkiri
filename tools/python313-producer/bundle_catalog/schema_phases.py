"""Private markers for the actual, unsplit App cold schema-store operation."""

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


def mark(point: str) -> None:
    """Send one fixed phase and actual schema name; the response is only an ACK."""
    if (not _observing or _schema_name is None
            or point not in ('setup', 'initial-reference', 'evaluation', 'return')):
        raise RuntimeError('unknown cold schema phase or missing schema name')
    checkpoint(f'{point}:{_schema_name}')
