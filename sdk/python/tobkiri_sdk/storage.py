"""Named DB-API subset over explicitly bound storage 1.1 guest resources.

SQL classification and authorization happen before step in the bounded engine.
The SDK always calls exec-typed with a result policy, never parses SQL or retries
queries as writes. Connections lazily begin one explicit transaction.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from ._binding import GuestError, enum_value, invoke
from .sql_result import SqlResult, decode_result, parameters as encode_parameters

apilevel = "2.0"
threadsafety = 0
paramstyle = "qmark"
Error = GuestError
DatabaseError = GuestError


class ProgrammingError(GuestError):
    """A closed handle or unsupported positional argument was used."""


class NotSupportedError(GuestError):
    """The operation lies outside the reviewed named DB-API subset."""


def _typed_call(function: Any, *arguments: Any, **keywords: Any) -> Any:
    try:
        return function(*arguments, **keywords)
    except Exception as error:
        # Preserve traps/cancellation and ordinary Python errors. Only the
        # pinned generator's typed WIT Err becomes a DB-API error.
        if (type(error).__module__ == "componentize_py_types"
                and type(error).__name__ == "Err"):
            value = getattr(error, "value", None)
            message = getattr(value, "message", None)
            if isinstance(message, str):
                raise DatabaseError(message) from None
        raise


def _release(resource: Any) -> None:
    # componentize-py generates __exit__ as the explicit WIT resource drop.
    release = getattr(resource, "__exit__", None)
    if not callable(release):
        raise NotSupportedError("the generated resource has no explicit release")
    _typed_call(release, None, None, None)


class Cursor:
    """A cursor over ordered, fully bounded materialized typed rows."""

    def __init__(self, connection: "Connection") -> None:
        self.connection = connection
        self.rowcount = -1
        self.description = None
        self._arraysize = 1
        self._rows: tuple[tuple[Any, ...], ...] = ()
        self._position = 0
        self._closed = False

    def _check_open(self) -> None:
        self.connection._check_open()
        if self._closed:
            raise ProgrammingError("cursor is closed")

    @property
    def arraysize(self) -> int:
        """Default fetchmany size; it does not raise the engine row limit."""
        return self._arraysize

    @arraysize.setter
    def arraysize(self, size: int) -> None:
        if type(size) is not int or size < 0:
            raise ProgrammingError("arraysize must be a nonnegative integer")
        self._arraysize = size

    @property
    def lastrowid(self) -> int | None:
        """Latest connection-level engine snapshot, shared by all cursors."""
        return self.connection.lastrowid

    def _set_result(self, result: SqlResult) -> None:
        self.description = result.description
        self.rowcount = result.rowcount
        self._rows, self._position = result.rows, 0
        self.connection.lastrowid = result.last_insert_rowid

    def execute(self, operation: str, parameters: Sequence[Any] = ()) -> "Cursor":
        """Execute once; the same prepared statement is classified before step."""
        self._check_open()
        result = self.connection._execute(operation, parameters,
                                          self.connection._allow_rows)
        self._set_result(result)
        return self

    def executemany(self, operation: str,
                    parameters: Iterable[Sequence[Any]]) -> "Cursor":
        """Use one transaction and reject all returning statements before step."""
        self._check_open()
        self._rows, self._position = (), 0
        self.description, self.rowcount = None, 0
        count, unknown = 0, False
        try:
            for index, values in enumerate(parameters):
                if index >= self.connection.batch_limit:
                    raise ProgrammingError("executemany exceeded its batch bound")
                result = self.connection._execute(operation, values,
                                                  self.connection._forbid_rows)
                if result.columns or result.rows:
                    raise DatabaseError("engine violated forbid-rows policy")
                unknown |= result.affected_rows is None
                count += result.affected_rows or 0
                self.connection.lastrowid = result.last_insert_rowid
            self.rowcount = -1 if unknown else count
        except BaseException:
            self.connection.rollback()
            raise
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        """Return one remaining row without consuming a resource or authority."""
        self._check_open()
        if self._position == len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        """Return at most size rows from the already bounded result."""
        self._check_open()
        size = self.arraysize if size is None else size
        if type(size) is not int or size < 0:
            raise ProgrammingError("fetchmany size must be a nonnegative integer")
        end = min(len(self._rows), self._position + size)
        rows = list(self._rows[self._position:end])
        self._position = end
        return rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Return all remaining rows already admitted by the engine bounds."""
        self._check_open()
        return self.fetchmany(len(self._rows) - self._position)

    def __iter__(self) -> "Cursor":
        """Iterate over remaining rows."""
        self._check_open()
        return self

    def __next__(self) -> tuple[Any, ...]:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self) -> None:
        """Release cursor rows; connection.close owns transaction rollback."""
        self._rows, self._position = (), 0
        self._closed = True

    def executescript(self, _: str) -> None:
        """Scripts are outside the reviewed single-statement contract."""
        raise NotSupportedError("SQL scripts are not supported")


class Connection:
    """Opaque store with lazy BEGIN DEFERRED, commit and rollback on close."""

    def __init__(self, store: Any, allow_rows: Any, forbid_rows: Any,
                 row_limit: int = 1000, batch_limit: int = 1000) -> None:
        self._store = store
        self._transaction = None
        self._failed = False
        self._allow_rows, self._forbid_rows = allow_rows, forbid_rows
        self.row_limit, self.batch_limit = row_limit, batch_limit
        self.lastrowid: int | None = None

    def _check_open(self) -> None:
        if self._store is None:
            raise ProgrammingError("connection is closed")

    def _execute(self, statement: str, values: Sequence[Any], policy: Any) -> SqlResult:
        self._check_open()
        if self._failed:
            raise DatabaseError("transaction requires rollback after failure")
        if type(statement) is not str:
            raise ProgrammingError("SQL statement must be text")
        statement.encode("utf-8", errors="strict")
        params = encode_parameters(values)
        if self._transaction is None:
            self._transaction = _typed_call(self._store.sql_begin)
        try:
            result = _typed_call(self._transaction.sql_exec_typed,
                                 statement, params, self.row_limit, policy)
            return decode_result(result, self.row_limit)
        except BaseException:
            self._failed = True
            raise

    def cursor(self) -> Cursor:
        """Create a cursor without beginning a transaction."""
        self._check_open()
        return Cursor(self)

    def execute(self, operation: str, parameters: Sequence[Any] = ()) -> Cursor:
        """Create a cursor and execute one statement."""
        return self.cursor().execute(operation, parameters)

    def executemany(self, operation: str,
                    parameters: Iterable[Sequence[Any]]) -> Cursor:
        """Create a cursor and execute a bounded batch in the current transaction."""
        return self.cursor().executemany(operation, parameters)

    def commit(self) -> None:
        """Commit explicitly; the engine checks live dirty WRITE authority."""
        self._check_open()
        if self._failed:
            raise DatabaseError("transaction requires rollback after failure")
        if self._transaction is not None:
            _typed_call(self._transaction.commit)
            transaction, self._transaction = self._transaction, None
            _release(transaction)

    def rollback(self) -> None:
        """Use reserved cleanup without a fresh approval or refreshed deadline."""
        self._check_open()
        if self._transaction is not None:
            _typed_call(self._transaction.rollback)
            transaction, self._transaction = self._transaction, None
            _release(transaction)
        self._failed = False

    def close(self) -> None:
        """Rollback pending work and drop handles; never commit on close."""
        if self._store is None:
            return
        try:
            self.rollback()
        finally:
            try:
                if self._transaction is not None:
                    transaction, self._transaction = self._transaction, None
                    _release(transaction)
            finally:
                store, self._store = self._store, None
                _release(store)

    def executescript(self, _: str) -> None:
        """Fail rather than splitting or retrying multiple statements."""
        raise NotSupportedError("SQL scripts are not supported")


def connect(store_name: str, *, row_limit: int = 1000, batch_limit: int = 1000,
            isolation_level: str = "DEFERRED", detect_types: int = 0) -> Connection:
    """Open a declared store with selected storage 1.1 imports and tagged cells."""
    if isolation_level != "DEFERRED" or detect_types != 0:
        raise NotSupportedError("custom converters and transaction modes are unsupported")
    for value in (row_limit, batch_limit):
        if type(value) is not int or not 0 < value <= 10000:
            raise ProgrammingError("row and batch limits must be from 1 to 10000")
    allow_rows = enum_value("storage", "SqlResultPolicy", "allow-rows")
    forbid_rows = enum_value("storage", "SqlResultPolicy", "forbid-rows")
    store = _typed_call(invoke, "storage", "open", name=store_name)
    if not callable(getattr(store, "sql_begin", None)):
        _release(store)
        raise NotSupportedError("DB-API requires explicitly bound storage 1.1")
    return Connection(store, allow_rows, forbid_rows, row_limit, batch_limit)


def kv_get(store_name: str, key: str) -> bytes | None:
    """Read a key; existing storage 1.0 KV meaning is unchanged."""
    store = invoke("storage", "open", name=store_name)
    try:
        return store.kv_get(key)
    finally:
        _release(store)


def kv_put(store_name: str, key: str, value: bytes) -> None:
    """Write through the selected generated resource and drop it explicitly."""
    store = invoke("storage", "open", name=store_name)
    try:
        store.kv_put(key, value)
    finally:
        _release(store)
