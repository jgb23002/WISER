"""Tests for the optional connection pool (wiser.database).

Both production surfaces -- the Streamlit app and the CLI REPL -- reach the
database exclusively through ``connect_pooled``, so the wrapper it returns is
production code and is covered here rather than left to the fallback path.

Every test closes its pools: a pool owns background worker threads and a live
connection into the test schema, and leaking one across tests would outlive
the schema rebuild that follows.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).parent.parent / "src"
TESTS_DIR = Path(__file__).parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from pg_harness import make_conn  # noqa: E402,F401

from wiser import database  # noqa: E402
from wiser.database import (  # noqa: E402
    PooledConnection,
    close_pools,
    connect,
    connect_pooled,
    pooling_available,
)


@unittest.skipUnless(pooling_available(), "psycopg_pool is not installed")
class TestPooledConnection(unittest.TestCase):
    """The wrapper must be a drop-in for a psycopg connection."""

    def setUp(self):
        close_pools()
        make_conn()  # a live schema for the pooled connections to query

    def tearDown(self):
        close_pools()

    def test_pooled_connection_executes_queries(self):
        conn = connect_pooled()
        try:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        finally:
            conn.close()

    def test_close_returns_the_connection_instead_of_tearing_it_down(self):
        """close() must hand the connection back, not close it.

        This is the whole difference between the pooled wrapper and a plain
        connection, and it is checkable without reading pool internals: after
        close(), the underlying psycopg connection is still open, because it
        is sitting in the pool. Pool counters are deliberately not asserted
        on -- background add tasks make them nondeterministic at any instant.
        """
        conn = connect_pooled()
        raw = conn._conn
        conn.execute("SELECT 1")
        conn.close()

        self.assertFalse(raw.closed, "pooled connection was torn down, not returned")
        # And the wrapper still refuses to let the caller keep using it.
        self.assertTrue(conn.closed)

    def test_a_plain_connection_really_does_close(self):
        """The contrast that makes the previous test meaningful."""
        conn = connect()
        conn.execute("SELECT 1")
        conn.close()

        self.assertTrue(conn.closed)

    def test_returned_connections_serve_later_checkouts(self):
        conn = connect_pooled()
        conn.close()

        for _ in range(3):
            later = connect_pooled()
            try:
                self.assertEqual(later.execute("SELECT 1").fetchone()[0], 1)
            finally:
                later.close()

    def test_backends_are_reused_rather_than_opened_per_checkout(self):
        """The point of pooling: N checkouts must not mean N new backends."""
        warmup = connect_pooled()
        warmup.close()
        list(database._pools.values())[0].wait(timeout=10.0)

        pids = []
        for _ in range(12):
            conn = connect_pooled()
            pids.append(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
            conn.close()

        # Unpooled this would be 12 distinct backends. Pooled, it is bounded
        # by the pool itself -- a loose bound on purpose, because the exact
        # count is the pool's business.
        self.assertLessEqual(
            len(set(pids)),
            database.POOL_MAX_SIZE,
            f"more backends than the pool ceiling: {sorted(set(pids))}",
        )
        self.assertLess(len(set(pids)), len(pids))

    def test_use_after_return_raises_rather_than_corrupting_the_pool(self):
        conn = connect_pooled()
        conn.close()

        with self.assertRaises(psycopg.OperationalError):
            conn.execute("SELECT 1")

    def test_closed_property_reports_returned_connections(self):
        conn = connect_pooled()
        self.assertFalse(conn.closed)
        conn.close()
        self.assertTrue(conn.closed)

    def test_double_close_is_harmless(self):
        conn = connect_pooled()
        conn.close()
        conn.close()  # must not raise, and must not return it twice

    def test_lock_timeout_survives_pooling(self):
        """The startup option has to reach pooled connections too."""
        conn = connect_pooled()
        try:
            self.assertEqual(conn.execute("SHOW lock_timeout").fetchone()[0], "30s")
        finally:
            conn.close()

    def test_connection_is_returned_idle_not_in_transaction(self):
        """A read leaves the connection INTRANS; checkin must clear that."""
        conn = connect_pooled()
        conn.execute("SELECT 1")
        raw = conn._conn
        conn.close()

        self.assertEqual(
            raw.info.transaction_status,
            psycopg.pq.TransactionStatus.IDLE,
        )

    def test_settable_connection_attributes_forward_to_the_connection(self):
        """__slots__ plus a read-only __getattr__ would break every write.

        autocommit/row_factory/etc. are settable on a real connection, and
        both production surfaces only ever hold a pooled one.
        """
        conn = connect_pooled()
        try:
            conn.autocommit = True
            self.assertTrue(conn.autocommit)
            self.assertTrue(conn._conn.autocommit)

            conn.row_factory = dict_row
            self.assertIs(conn._conn.row_factory, dict_row)
            self.assertEqual(
                conn.execute("SELECT 1 AS n").fetchone(), {"n": 1}
            )
        finally:
            # Restore before returning it: the pool hands this same
            # connection to the next caller.
            conn.autocommit = False
            conn.row_factory = None
            conn.close()

    def test_missing_attributes_report_as_missing_not_as_pool_errors(self):
        """hasattr()/getattr(default) must work on a returned connection."""
        conn = connect_pooled()
        conn.close()

        self.assertFalse(hasattr(conn, "definitely_not_an_attribute"))
        self.assertIsNone(getattr(conn, "definitely_not_an_attribute", None))

    def test_real_attributes_still_flag_use_after_return(self):
        """A genuine connection attribute must not degrade to AttributeError."""
        conn = connect_pooled()
        conn.close()

        with self.assertRaises(psycopg.OperationalError):
            conn.execute
        self.assertTrue(hasattr(psycopg.Connection, "execute"))

    def test_uninitialized_instance_does_not_recurse(self):
        """__getattr__ reading a slot it may not have must not loop forever.

        object.__new__ (and unpickling) produce an instance whose slots were
        never assigned; a plain self._conn read there misses the slot and
        routes straight back into __getattr__.
        """
        orphan = object.__new__(PooledConnection)

        with self.assertRaises(AttributeError):
            orphan.execute("SELECT 1")


@unittest.skipUnless(pooling_available(), "psycopg_pool is not installed")
class TestPooledConnectionContextManager(unittest.TestCase):
    """`with conn:` is idiomatic psycopg 3 and must work on the wrapper.

    Special-method lookup goes to the type, so a forwarding __getattr__ alone
    leaves this raising TypeError -- and only on the pooled path, i.e. only
    in production.
    """

    def setUp(self):
        # make_conn() -> reset_schema() closes pools itself; closing first is
        # what guarantees no pooled connection survives the schema rebuild.
        close_pools()
        self.conn = make_conn()
        self.conn.execute("DROP TABLE IF EXISTS pool_ctx_probe")
        self.conn.execute("CREATE TABLE pool_ctx_probe (value INTEGER)")
        self.conn.commit()

    def tearDown(self):
        close_pools()
        self.conn.rollback()
        self.conn.execute("DROP TABLE IF EXISTS pool_ctx_probe")
        self.conn.commit()

    def _probe_values(self) -> list[int]:
        return [
            int(row[0])
            for row in self.conn.execute(
                "SELECT value FROM pool_ctx_probe ORDER BY value"
            ).fetchall()
        ]

    def test_with_block_is_supported(self):
        conn = connect_pooled()
        with conn:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

    def test_with_block_commits_on_clean_exit(self):
        conn = connect_pooled()
        with conn:
            conn.execute("INSERT INTO pool_ctx_probe(value) VALUES(1)")

        self.assertEqual(self._probe_values(), [1])

    def test_with_block_rolls_back_on_exception(self):
        conn = connect_pooled()
        with self.assertRaises(RuntimeError):
            with conn:
                conn.execute("INSERT INTO pool_ctx_probe(value) VALUES(2)")
                raise RuntimeError("boom")

        self.assertEqual(self._probe_values(), [])

    def test_with_block_returns_the_connection_to_the_pool(self):
        conn = connect_pooled()
        with conn:
            conn.execute("SELECT 1")

        self.assertTrue(conn.closed)
        with self.assertRaises(psycopg.OperationalError):
            conn.execute("SELECT 1")

    def test_entering_a_returned_connection_raises(self):
        conn = connect_pooled()
        conn.close()
        with self.assertRaises(psycopg.OperationalError):
            with conn:
                pass

    def test_entering_a_closed_connection_raises(self):
        """`closed` and the entry guard must agree.

        A server-side disconnect closes the connection while it is still
        checked out. Entering anyway gives a block that commits nothing and
        raises nothing -- the caller believes it wrote.
        """
        conn = connect_pooled()
        conn._conn.close()
        self.assertTrue(conn.closed)

        with self.assertRaises(psycopg.OperationalError):
            with conn:
                pass
        conn.close()

    def test_a_failed_commit_propagates_instead_of_reporting_success(self):
        """A deferred-constraint violation surfaces at COMMIT, not at INSERT.

        Swallowing it exits the block cleanly with the rows silently gone:
        the caller reports success on lost data.
        """
        self.conn.execute("DROP TABLE IF EXISTS deferred_probe")
        self.conn.execute(
            "CREATE TABLE deferred_probe (id INTEGER, "
            "CONSTRAINT deferred_probe_uq UNIQUE (id) DEFERRABLE INITIALLY DEFERRED)"
        )
        self.conn.commit()
        self.addCleanup(self.conn.commit)
        self.addCleanup(
            self.conn.execute, "DROP TABLE IF EXISTS deferred_probe"
        )

        conn = connect_pooled()
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with conn:
                conn.execute("INSERT INTO deferred_probe(id) VALUES(1)")
                conn.execute("INSERT INTO deferred_probe(id) VALUES(1)")

        # And the connection still went back to the pool despite the failure.
        self.assertTrue(conn.closed)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM deferred_probe").fetchone()[0],
            0,
        )


class TestPoolingFallback(unittest.TestCase):
    """Without psycopg_pool the code must still work, unpooled."""

    def setUp(self):
        close_pools()
        make_conn()

    def tearDown(self):
        close_pools()

    def test_connect_pooled_falls_back_to_a_plain_connection(self):
        with patch.object(database, "_ConnectionPool", None):
            conn = connect_pooled()
            try:
                self.assertNotIsInstance(conn, PooledConnection)
                self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
            finally:
                conn.close()

    def test_pooling_available_reports_the_import(self):
        with patch.object(database, "_ConnectionPool", None):
            self.assertFalse(pooling_available())

    def test_fallback_connection_still_carries_lock_timeout(self):
        with patch.object(database, "_ConnectionPool", None):
            conn = connect_pooled()
            try:
                self.assertEqual(
                    conn.execute("SHOW lock_timeout").fetchone()[0], "30s"
                )
            finally:
                conn.close()


@unittest.skipUnless(pooling_available(), "psycopg_pool is not installed")
class TestPoolLifecycle(unittest.TestCase):
    def setUp(self):
        close_pools()
        make_conn()
        # These tests reset the registration flag to observe registration.
        # Restoring it is not optional: leaving it False disables the real
        # atexit hook for the rest of the process, and leaving it True stops
        # a later pool from ever registering one -- reintroducing exactly the
        # finalization stall the hook exists to prevent.
        original = database._atexit_registered
        self.addCleanup(setattr, database, "_atexit_registered", original)

    def tearDown(self):
        close_pools()

    def test_close_pools_clears_the_registry(self):
        conn = connect_pooled()
        conn.close()
        self.assertTrue(database._pools)

        close_pools()

        self.assertFalse(database._pools)

    def test_a_shutdown_hook_is_registered_once_a_pool_exists(self):
        """An unclosed pool stalls finalization; the hook closes it first."""
        database._atexit_registered = False
        with patch("wiser.database.atexit.register") as mock_register:
            conn = connect_pooled()
            conn.close()
            mock_register.assert_called_once_with(close_pools)

    def test_shutdown_hook_is_registered_only_once(self):
        database._atexit_registered = False
        with patch("wiser.database.atexit.register") as mock_register:
            connect_pooled().close()
            close_pools()
            connect_pooled().close()
            self.assertEqual(mock_register.call_count, 1)

    def test_one_pool_per_database_regardless_of_timeout(self):
        """The registry key must not carry the per-call timeout.

        It used to be the full conninfo, which bakes `timeout` in as
        lock_timeout/connect_timeout -- so one database reached with two
        timeouts got two pools, each able to grow to POOL_MAX_SIZE. Against
        a provider that caps connections per project, that doubling is the
        failure mode.
        """
        first = connect_pooled(timeout=30.0)
        first.close()
        second = connect_pooled(timeout=5.0)
        second.close()

        self.assertEqual(len(database._pools), 1)

    def test_distinct_dsns_get_distinct_pools(self):
        first = connect_pooled()
        first.close()
        # Same server, explicitly different conninfo: must not share a pool.
        other_dsn = f"{database.resolve_dsn(None)}&application_name=wiser_pool_probe"
        second = connect_pooled(other_dsn)
        second.close()

        self.assertEqual(len(database._pools), 2)


if __name__ == "__main__":
    unittest.main()
