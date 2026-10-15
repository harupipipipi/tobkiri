"""SDK routing/lifetime tests, not substitutes for bounded-engine runtime proof."""

from enum import Enum
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tobkiri_sdk import _binding, storage
from tobkiri_sdk.generated import GeneratedImports
from tobkiri_sdk.sql_codec import encode_cell


class Policy(Enum):
    ALLOW_ROWS = 0
    FORBID_ROWS = 1


class Transaction:
    """Record typed calls and explicit rollback/commit/drop without native SQL."""

    def __init__(self, store):
        self.store = store
        self.calls = []
        self.pending = []
        self.commits = self.rollbacks = self.drops = 0

    def sql_exec_typed(self, statement, params, max_rows, policy):
        self.calls.append((statement, params, max_rows, policy))
        if self.store.fail_at == len(self.calls):
            raise storage.DatabaseError("bounded engine denied before step")
        result = self.store.result
        if policy is Policy.FORBID_ROWS and result.columns:
            raise storage.DatabaseError("returning statement forbidden before step")
        self.pending.append(statement)
        return result

    def commit(self):
        self.commits += 1
        self.store.committed.extend(self.pending)
        self.pending = []

    def rollback(self):
        self.rollbacks += 1
        self.pending = []

    def __exit__(self, *_):
        self.drops += 1


class Store:
    def __init__(self, *, columns=(), rows=(), affected=None, lastrowid=None):
        self.result = SimpleNamespace(columns=list(columns), rows=list(rows),
                                      affected_rows=affected,
                                      last_insert_rowid=lastrowid)
        self.transactions = []
        self.committed = []
        self.fail_at = None
        self.drops = 0

    def sql_begin(self):
        transaction = Transaction(self)
        self.transactions.append(transaction)
        return transaction

    def __exit__(self, *_):
        self.drops += 1


class Imports:
    def __init__(self, store):
        self.store = store

    def enum_value(self, interface, name, case):
        assert (interface, name) == ("storage", "SqlResultPolicy")
        return Policy[case.replace("-", "_").upper()]

    def invoke(self, interface, operation, **arguments):
        assert (interface, operation, arguments) == ("storage", "open", {"name": "test"})
        return self.store


class DbApiTests(unittest.TestCase):
    def tearDown(self):
        _binding._imports = None

    def connect(self, store, **arguments):
        _binding.bind(Imports(store))
        return storage.connect("test", **arguments)

    def test_lazy_begin_execute_always_exec_typed_without_sql_classification(self):
        store = Store()
        connection = self.connect(store)
        cursor = connection.cursor()
        self.assertEqual(store.transactions, [])
        statements = [" SELECT 1", "/* comment */ WITH x AS (SELECT 1) DELETE FROM x"]
        for statement in statements:
            cursor.execute(statement, [None, 1, 1.0, "1", b"\xff"])
        self.assertEqual(len(store.transactions), 1)
        calls = store.transactions[0].calls
        self.assertEqual([call[0] for call in calls], statements)
        self.assertTrue(all(call[3] is Policy.ALLOW_ROWS for call in calls))
        self.assertEqual(calls[0][1], [encode_cell(v) for v in [None, 1, 1.0, "1", b"\xff"]])
        connection.close()

    def test_fetchmany_iteration_and_zero_row_duplicate_metadata(self):
        store = Store(columns=["same", "same"],
                      rows=[[encode_cell("1"), encode_cell(1)],
                            [encode_cell("2"), encode_cell(2)]], lastrowid="-5")
        connection = self.connect(store)
        cursor = connection.execute("statement")
        self.assertEqual(cursor.description, (("same", None, None, None, None, None, None),)*2)
        self.assertEqual(cursor.rowcount, -1)
        self.assertEqual(cursor.lastrowid, -5)
        cursor.arraysize = 1
        self.assertEqual(cursor.fetchmany(), [("1", 1)])
        self.assertEqual(list(cursor), [("2", 2)])
        self.assertIsNone(cursor.fetchone())
        store.result.rows = []
        cursor.execute("empty query")
        self.assertEqual(len(cursor.description), 2)
        self.assertEqual(cursor.fetchall(), [])
        connection.close()

    def test_executemany_one_transaction_forbid_rows_no_partial_commit(self):
        store = Store(affected="1", lastrowid="3")
        connection = self.connect(store)
        cursor = connection.executemany("mutation", [(1,), (2,), (3,)])
        self.assertEqual(cursor.rowcount, 3)
        self.assertEqual(len(store.transactions), 1)
        tx = store.transactions[0]
        self.assertTrue(all(call[3] is Policy.FORBID_ROWS for call in tx.calls))
        self.assertEqual(store.committed, [])
        connection.commit()
        self.assertEqual(tx.commits, 1)
        self.assertEqual(tx.drops, 1)
        self.assertEqual(len(store.committed), 3)
        connection.close()
        self.assertEqual(tx.rollbacks, 0)

    def test_batch_failure_and_bound_rollback_all_pending_work(self):
        store = Store(affected="1")
        connection = self.connect(store, batch_limit=2)
        with self.assertRaises(storage.ProgrammingError):
            connection.executemany("mutation", [(1,), (2,), (3,)])
        tx = store.transactions[0]
        self.assertEqual(len(tx.calls), 2)
        self.assertEqual((tx.commits, tx.rollbacks, tx.drops), (0, 1, 1))
        self.assertEqual(tx.pending, [])
        self.assertEqual(store.committed, [])
        connection.close()

    def test_returning_batch_rejects_before_test_double_step(self):
        store = Store(columns=["id"])
        connection = self.connect(store)
        with self.assertRaisesRegex(storage.DatabaseError, "before step"):
            connection.executemany("returning statement", [(1,)])
        tx = store.transactions[0]
        self.assertEqual(tx.pending, [])
        self.assertEqual((tx.commits, tx.rollbacks), (0, 1))
        connection.close()

    def test_execute_failure_requires_rollback_and_close_never_commits(self):
        store = Store()
        store.fail_at = 1
        connection = self.connect(store)
        with self.assertRaises(storage.DatabaseError):
            connection.execute("denied")
        with self.assertRaisesRegex(storage.DatabaseError, "requires rollback"):
            connection.commit()
        cursor = connection.cursor()
        connection.close()
        self.assertEqual(store.transactions[0].rollbacks, 1)
        self.assertEqual(store.transactions[0].commits, 0)
        self.assertEqual(store.transactions[0].drops, 1)
        self.assertEqual(store.drops, 1)
        connection.close()
        self.assertEqual(store.drops, 1)
        with self.assertRaises(storage.ProgrammingError):
            cursor.fetchone()

    def test_unsupported_modes_converters_records_and_scripts_fail_explicitly(self):
        for arguments in [{"isolation_level": None}, {"isolation_level": "IMMEDIATE"},
                          {"detect_types": 1}]:
            with self.assertRaises(storage.NotSupportedError):
                storage.connect("test", **arguments)
        store = Store()
        connection = self.connect(store)
        with self.assertRaises(TypeError):
            connection.execute("statement", {"name": 1})
        with self.assertRaises(storage.NotSupportedError):
            connection.executescript("one; two")
        with self.assertRaises(storage.NotSupportedError):
            connection.cursor().executescript("one; two")
        self.assertEqual(store.transactions, [])
        connection.close()

    def test_generated_policy_is_real_declared_enum_not_string_fallback(self):
        adapter = GeneratedImports({"storage": SimpleNamespace(SqlResultPolicy=Policy)})
        self.assertIs(adapter.enum_value("storage", "SqlResultPolicy", "allow-rows"),
                      Policy.ALLOW_ROWS)
        with self.assertRaises(storage.GuestError):
            adapter.enum_value("storage", "SqlResultPolicy", "other")
        with self.assertRaises(storage.GuestError):
            GeneratedImports({}).enum_value("storage", "SqlResultPolicy", "allow-rows")

    def test_only_generated_typed_errors_map_to_dbapi_error(self):
        generated_error = type("Err", (Exception,), {"__module__": "componentize_py_types"})
        error = generated_error()
        error.value = SimpleNamespace(message="typed operation denied")

        def denied():
            raise error

        with self.assertRaisesRegex(storage.DatabaseError, "typed operation denied"):
            storage._typed_call(denied)
        with self.assertRaises(ValueError):
            storage._typed_call(lambda: (_ for _ in ()).throw(ValueError("local error")))


if __name__ == "__main__":
    unittest.main()
