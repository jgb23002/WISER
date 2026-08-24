"""Shared PostgreSQL harness for the WISER test suite.

Under SQLite every test built its own private ``sqlite3.connect(":memory:")``
database, which was free and perfectly isolated. PostgreSQL has no equivalent:
a connection always points at a real server. Isolation here therefore comes
from a dedicated schema that is dropped and rebuilt before each test that
needs a database.

Two details make this work:

* **The search_path is pinned in the DSN itself**, not issued as a ``SET``
  after connecting. Production code calls ``wiser.database.connect()`` with no
  arguments deep inside handlers, so there is no place to inject a ``SET``.
  Putting ``options=-csearch_path=<schema>`` in the connection string means
  every connection opened anywhere in the process lands in the test schema.

* **Rebuilding beats truncating.** The plan assumed ``TRUNCATE`` would be much
  faster than recreating the schema; measured on this database they are within
  5 ms of each other (17 ms vs 22 ms). Rebuilding also clears tables that
  individual tests create on the fly -- RagDocument, RagChunk,
  ExternalOutageObservation -- which a fixed TRUNCATE list would miss.

Set ``WISER_TEST_DSN`` to run the suite against a different server. Never
point it at anything you care about: the schema below is dropped repeatedly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wiser.database import DEFAULT_DSN  # noqa: E402
from wiser.database import close_pools as _db_close_pools  # noqa: E402
from wiser.database import executemany as _db_executemany  # noqa: E402

# Under pytest-xdist every worker gets its own schema. Workers share one
# server and this harness DROPs its schema constantly, so a single shared
# name would have them deleting each other's tables mid-test.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
TEST_SCHEMA = f"wiser_test_{_XDIST_WORKER}" if _XDIST_WORKER else "wiser_test"
SCHEMA_SQL = SCHEMA_PATH.read_text(encoding="utf-8")

# Deliberately NOT resolve_dsn(): that reads $DATABASE_URL, so a developer
# pointing it at a shared or staging server would have this suite run
# DROP SCHEMA against it on import. Opting in to another server has to be
# explicit, via WISER_TEST_DSN.
_ADMIN_DSN = (os.environ.get("WISER_TEST_DSN") or DEFAULT_DSN).split("?", 1)[0]
TEST_DSN = f"{_ADMIN_DSN}?options=-csearch_path%3D{TEST_SCHEMA}"

# Production code resolves its DSN from this variable, so pointing it at the
# test schema redirects the whole process.
os.environ["DATABASE_URL"] = TEST_DSN

_shared: psycopg.Connection | None = None


def new_connection() -> psycopg.Connection:
    """Open an extra connection into the test schema.

    Use this only when a test genuinely needs two connections at once; the
    shared one from :func:`make_conn` is cheaper.
    """
    return psycopg.connect(TEST_DSN)


def _close_pools() -> None:
    """Drop any pooled connections before the schema is rebuilt.

    A pool holds live connections pinned to the test schema plus background
    workers that can reopen them. Dropping the schema out from under those
    races the rebuild -- two sessions running ``CREATE TABLE Storm`` at once
    surfaces as a UniqueViolation on ``pg_type_typname_nsp_index``. Closing
    first makes the rebuild the only writer.

    Note psycopg_pool's close() does NOT close connections that are currently
    checked out, so a test holding one still races the rebuild; tests must
    return their pooled connections.

    Called directly rather than via sys.modules: this module imports
    wiser.database at import time, so unlike _reset_module_caches() there is
    no import to avoid and no chance of the module being absent.
    """
    _db_close_pools()


def _reset_module_caches() -> None:
    """Clear process-level caches in already-imported wiser modules.

    Production code caches results that are invalidated only when the schema
    is rebuilt through its own code paths (the required-tables probe, static
    lookup tables). Tests rebuild the schema underneath the process, so those
    caches must be dropped here. Modules are looked up in sys.modules rather
    than imported: the harness must not drag the whole handler stack into
    test files that never touch it.
    """
    handlers = sys.modules.get("wiser.handlers")
    if handlers is not None:
        handlers.reset_schema_probe()
    external_outage = sys.modules.get("wiser.integrations.external_outage")
    if external_outage is not None:
        external_outage.reset_external_outage_schema_cache()
    storm_analysis = sys.modules.get("wiser.storm_analysis")
    if storm_analysis is not None:
        storm_analysis.clear_lookup_caches()
    rag = sys.modules.get("wiser.rag")
    if rag is not None:
        rag.reset_rag_caches()


def reset_schema() -> None:
    """Drop and recreate the test schema, then load ``sql/schema.sql``."""
    global _shared
    _close_pools()
    if _shared is None or _shared.closed:
        _shared = psycopg.connect(TEST_DSN)
    _shared.rollback()
    _shared.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
    _shared.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
    _shared.commit()
    _shared.execute(SCHEMA_SQL)
    _shared.commit()
    _reset_module_caches()


def make_conn() -> psycopg.Connection:
    """Return a clean, schema-loaded connection.

    This is the drop-in replacement for the old
    ``sqlite3.connect(":memory:")`` + ``executescript(schema)`` pair.
    """
    reset_schema()
    assert _shared is not None
    return _shared


def blank_conn() -> psycopg.Connection:
    """Return a connection whose schema is empty (no tables loaded).

    Replaces tests that opened an in-memory database and deliberately did not
    apply the schema.
    """
    global _shared
    _close_pools()
    if _shared is None or _shared.closed:
        _shared = psycopg.connect(TEST_DSN)
    _shared.rollback()
    _shared.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
    _shared.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
    _shared.commit()
    _reset_module_caches()
    return _shared


def executemany(conn: psycopg.Connection, sql: str, rows) -> None:
    """``executemany`` lives on the cursor in psycopg, not the connection.

    Delegates to the production helper so test fixtures get its small-batch
    fast path: psycopg's pipeline-mode executemany costs ~100 ms per call
    regardless of size, which across a few hundred fixture loads dominated
    this suite's runtime.
    """
    _db_executemany(conn, sql, rows)
