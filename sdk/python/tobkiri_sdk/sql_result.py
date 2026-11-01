"""Ordered metadata and rows from storage 1.1, without statement classification."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .sql_codec import decode_cell, encode_cell, integer


def parameters(values: Sequence[Any]) -> list[str]:
    """Encode positional parameters only; reject records and implicit converters."""
    if (not isinstance(values, Sequence)
            or isinstance(values, (str, bytes, bytearray, memoryview))):
        raise TypeError("SQL parameters must be a positional sequence")
    return [encode_cell(value) for value in values]


@dataclass(frozen=True)
class SqlResult:
    """Materialized bounded result with duplicate names and empty-row metadata."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    affected_rows: int | None
    last_insert_rowid: int | None

    @property
    def description(self) -> tuple[tuple[Any, ...], ...] | None:
        """DB-API description: column name and six unavailable metadata fields."""
        if not self.columns:
            return None
        return tuple((name, None, None, None, None, None, None)
                     for name in self.columns)

    @property
    def rowcount(self) -> int:
        """Return affected rows when provided, and -1 for unknown counts."""
        return -1 if self.affected_rows is None else self.affected_rows


def decode_result(result: Any, max_rows: int) -> SqlResult:
    """Validate a generated TypedSqlResult without truncating or renaming rows."""
    if type(max_rows) is not int or not 0 <= max_rows <= 10000:
        raise ValueError("max_rows must be an integer from 0 to 10000")
    columns, rows = result.columns, result.rows
    if (not isinstance(columns, list)
            or any(type(name) is not str for name in columns)):
        raise ValueError("SQL columns must be an ordered list of names")
    for name in columns:
        name.encode("utf-8", errors="strict")
    if not isinstance(rows, list) or len(rows) > max_rows:
        raise ValueError("SQL result exceeded the requested row bound")
    decoded = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError("SQL row width differs from its ordered metadata")
        decoded.append(tuple(decode_cell(cell) for cell in row))
    affected = (None if result.affected_rows is None
                else integer(result.affected_rows))
    if affected is not None and affected < 0:
        raise ValueError("SQL affected-row count cannot be negative")
    last_insert = (None if result.last_insert_rowid is None
                   else integer(result.last_insert_rowid))
    return SqlResult(tuple(columns), tuple(decoded), affected, last_insert)
