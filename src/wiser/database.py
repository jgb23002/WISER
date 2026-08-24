from __future__ import annotations

import atexit
import os
import threading
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import psycopg
from psycopg import conninfo as _conninfo

# Optional pooling: psycopg_pool is a separate package. Everything here
# degrades to plain per-call connections without it, matching how faiss and
# sqlglot are treated elsewhere in the project.
try:
    from psycopg_pool import ConnectionPool as _ConnectionPool
except ImportError:
    _ConnectionPool = None

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "sql" / "schema.sql"

# Table names are written in camelCase for readability, but PostgreSQL folds
# unquoted identifiers to lowercase, so introspection against
# information_schema must compare against the folded form.
#
# Introspection also has to target current_schema() rather than a literal
# 'public': every DML statement here uses unqualified table names, which
# resolve through search_path. The test harness and the evaluation/smoke
# fixtures all pin search_path to a private schema, so asking about
# 'public' would report on a different database than the one being written.
REQUIRED_TABLES = frozenset(
    {
        "Storm",
        "County",
        "WeatherData",
        "RegionCountyMapping",
        "StormMetricDefinition",
        "HourlyWeatherObservation",
        "CurrentWeatherObservation",
    }
)
_REQUIRED_TABLES_FOLDED = frozenset(name.lower() for name in REQUIRED_TABLES)

DEFAULT_DSN = "postgresql://wiser:wiser123@localhost:5433/wiser"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
# PostgreSQL blocks on row/table locks rather than whole-file locks, so the
# SQLite busy_timeout is replaced by lock_timeout: how long a statement waits
# for a lock before giving up.
DEFAULT_LOCK_TIMEOUT_MS = int(DEFAULT_CONNECT_TIMEOUT_SECONDS * 1000)

DEFAULT_REGION_COUNTY_ROWS: tuple[dict[str, object], ...] = (
    {"region": "Northern CT", "county": "Hartford", "state": "CT"},
    {"region": "Northern CT", "county": "Litchfield", "state": "CT"},
    {"region": "Northern CT", "county": "Tolland", "state": "CT"},
    {"region": "Northern CT", "county": "Windham", "state": "CT"},
    {"region": "Southern CT", "county": "Fairfield", "state": "CT"},
    {"region": "Southern CT", "county": "Middlesex", "state": "CT"},
    {"region": "Southern CT", "county": "New Haven", "state": "CT"},
    {"region": "Southern CT", "county": "New London", "state": "CT"},
    {"region": "Eastern CT", "county": "New London", "state": "CT"},
    {"region": "Eastern CT", "county": "Tolland", "state": "CT"},
    {"region": "Eastern CT", "county": "Windham", "state": "CT"},
    {"region": "Western CT", "county": "Fairfield", "state": "CT"},
    {"region": "Western CT", "county": "Litchfield", "state": "CT"},
    {"region": "Central CT", "county": "Hartford", "state": "CT"},
    {"region": "Central CT", "county": "Middlesex", "state": "CT"},
    {"region": "Central CT", "county": "New Haven", "state": "CT"},
)

DEFAULT_METRIC_DEFINITION_ROWS: tuple[dict[str, object], ...] = (
    {
        "metricKey": "outage",
        "displayName": "Outages",
        "storedUnit": "trouble spots",
        "higherIsMoreSevere": 1,
        "sourceColumn": "outage",
    },
    {
        "metricKey": "peakWSPD10",
        "displayName": "Peak sustained wind",
        "storedUnit": "m/s",
        "higherIsMoreSevere": 1,
        "sourceColumn": "peakWSPD10",
    },
    {
        "metricKey": "peakGUST10",
        "displayName": "Peak wind gust",
        "storedUnit": "m/s",
        "higherIsMoreSevere": 1,
        "sourceColumn": "peakGUST10",
    },
    {
        "metricKey": "sumAFWA_TOTPRECIP",
        "displayName": "Total precipitation",
        "storedUnit": "mm",
        "higherIsMoreSevere": 1,
        "sourceColumn": "sumAFWA_TOTPRECIP",
    },
    {
        "metricKey": "wgt5",
        "displayName": "Gust hours above 5 m/s",
        "storedUnit": "hours",
        "higherIsMoreSevere": 1,
        "sourceColumn": "wgt5",
    },
    {
        "metricKey": "wgt9",
        "displayName": "Gust hours above 9 m/s",
        "storedUnit": "hours",
        "higherIsMoreSevere": 1,
        "sourceColumn": "wgt9",
    },
    {
        "metricKey": "wgt13",
        "displayName": "Gust hours above 13 m/s",
        "storedUnit": "hours",
        "higherIsMoreSevere": 1,
        "sourceColumn": "wgt13",
    },
    {
        "metricKey": "wgt18",
        "displayName": "Gust hours above 18 m/s",
        "storedUnit": "hours",
        "higherIsMoreSevere": 1,
        "sourceColumn": "wgt18",
    },
    {
        "metricKey": "humidity",
        "displayName": "Humidity",
        "storedUnit": "%",
        "higherIsMoreSevere": 1,
        "sourceColumn": None,
    },
)


# psycopg's executemany runs the batch in pipeline mode, which carries a
# fixed setup cost of roughly 100 ms on this driver/server pair -- the same
# whether the batch is 3 rows or 1000. Measured here (local PostgreSQL 17,
# psycopg 3.3, per call, schema-rebuild baseline subtracted):
#
#     rows    executemany    per-row execute
#        3        102 ms            3.1 ms
#       10        100 ms            8.9 ms
#       50         98 ms           30   ms
#      200        104 ms           99   ms
#     1000        123 ms          500   ms
#
# So pipeline mode only pays off past ~200 rows. Below that a plain loop is
# much faster, and most batches in this codebase (9 metric definitions, 16
# region mappings, a handful of test fixture rows) are far below it.
EXECUTEMANY_PIPELINE_MIN_ROWS = 200


def executemany(conn: psycopg.Connection, sql: str, rows) -> None:
    """Run a bulk statement, closing the cursor afterwards.

    psycopg puts ``executemany`` on the cursor, not the connection, so the
    obvious translation of ``conn.executemany(...)`` is
    ``conn.cursor().executemany(...)`` -- which leaks a cursor at every call
    site. This is that translation, done once, plus the small-batch fast path
    described above. Both forms send the same statements in the same order,
    so callers see no behavioural difference.
    """
    row_list = rows if isinstance(rows, (list, tuple)) else list(rows)
    if not row_list:
        return
    if len(row_list) < EXECUTEMANY_PIPELINE_MIN_ROWS:
        with conn.cursor() as cursor:
            for row in row_list:
                cursor.execute(sql, row)
        return
    with conn.cursor() as cursor:
        cursor.executemany(sql, row_list)


def copy_rows(
    conn: psycopg.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Mapping[str, object]],
) -> None:
    """Bulk-load rows with COPY, the fastest path psycopg offers.

    COPY streams one statement instead of round-tripping per row, which is
    what makes it worth using for the WeatherData/hourly loads. It is only
    valid for plain inserts: no ON CONFLICT clause, so callers needing upsert
    semantics stay on :func:`executemany`.

    A COPY failure aborts the whole batch, which suits every caller here --
    the database build is all-or-nothing and runs inside one transaction.
    """
    column_list = ", ".join(columns)
    with conn.cursor() as cursor:
        with cursor.copy(f"COPY {table} ({column_list}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row([row.get(column) for column in columns])


def resolve_dsn(dsn: str | None = None) -> str:
    """Return the connection string to use.

    Precedence: explicit argument, then ``DATABASE_URL``, then the local
    Docker Compose default.
    """
    return dsn or os.environ.get("DATABASE_URL") or DEFAULT_DSN


def _startup_conninfo(dsn: str | None, timeout: float) -> str:
    """Resolve the DSN and fold lock_timeout into the libpq startup options.

    Carrying lock_timeout in ``options`` makes the connection ready in a
    single round trip; the old post-connect ``SET`` + ``COMMIT`` pair cost
    two more and left every read-only session idle-in-transaction. Existing
    ``options`` in the DSN (the test harness pins search_path there) are
    preserved, not replaced.
    """
    params = _conninfo.conninfo_to_dict(resolve_dsn(dsn))
    existing_options = str(params.get("options") or "")
    lock_option = f"-c lock_timeout={int(timeout * 1000)}"
    params["options"] = f"{existing_options} {lock_option}".strip()
    # libpq reads connect_timeout=0 as "wait forever", so a sub-second
    # timeout must not truncate to zero -- round up to one second instead.
    params["connect_timeout"] = max(1, int(round(timeout)))
    return _conninfo.make_conninfo(**params)


def connect(
    dsn: str | None = None,
    *,
    timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> psycopg.Connection:
    return psycopg.connect(_startup_conninfo(dsn, timeout))


# ---------------------------------------------------------------------------
# Optional connection pooling
# ---------------------------------------------------------------------------

POOL_MAX_SIZE = 8

_pools: dict[str, object] = {}
_pools_lock = threading.Lock()
_atexit_registered = False


def pooling_available() -> bool:
    """True when psycopg_pool is importable."""
    return _ConnectionPool is not None


def _register_pool_shutdown() -> None:
    """Close pools at interpreter exit, once a pool actually exists.

    Not because interpreter shutdown blocks on the workers -- psycopg_pool
    creates them as daemon threads, which threading._shutdown never joins.
    The stall comes later: an unclosed pool is finalized by its own __del__,
    which calls the same gather(timeout=5.0) that close() does, and that is
    what prints "couldn't stop thread 'pool-N-worker-M' within 5.0 seconds"
    per worker during finalization. Closing deterministically at exit, while
    the interpreter is still healthy, avoids the whole shutdown-ordering
    question. Registration is lazy so a process that never pools carries no
    exit hook.
    """
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(close_pools)
    _atexit_registered = True


class PooledConnection:
    """A pool checkout that behaves like the psycopg connection it wraps.

    Every caller in this codebase owns its connection and calls ``close()``
    when done; here ``close()`` returns the connection to the pool instead of
    tearing it down, so the calling convention stays identical whether or not
    pooling is active.

    ``__enter__``/``__exit__`` are defined explicitly rather than left to
    ``__getattr__``: Python looks up special methods on the type, not the
    instance, so a forwarding ``__getattr__`` alone leaves ``with conn:`` --
    the idiomatic psycopg 3 transaction block -- raising TypeError, and only
    on the pooled path.
    """

    __slots__ = ("_pool", "_conn")

    def __init__(self, pool: object, conn: psycopg.Connection) -> None:
        self._pool = pool
        self._conn = conn

    @property
    def closed(self) -> bool:
        return self._conn is None or self._conn.closed

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        # psycopg starts an implicit transaction on the first execute, so a
        # read-only user of this connection returns it INTRANS. The pool
        # would roll that back itself, but with a warning per checkin.
        try:
            if not conn.closed and conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn.rollback()
        except Exception:  # noqa: BLE001 - a broken conn is the pool's problem
            pass
        self._pool.putconn(conn)

    def __enter__(self) -> "PooledConnection":
        conn = self._require_open()
        if conn.closed:
            # A server-side disconnect can close the connection while it is
            # checked out. Entering anyway would give a block that commits
            # nothing and raises nothing.
            raise psycopg.OperationalError("connection is already closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        # Delegate the transaction semantics to psycopg rather than
        # reimplementing them: its Connection.__exit__ commits on a clean
        # exit and lets a failed COMMIT propagate (a deferred-constraint or
        # serialization failure must not be reported as success), rolls back
        # on an exception without clobbering the one bubbling up, and is
        # already pool-aware -- it skips its own close() when _pool is set,
        # which psycopg_pool does on every checkout. The connection is
        # returned in a finally so a failing commit cannot leak it.
        conn = self._conn
        try:
            if conn is not None and not conn.closed:
                conn.__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()

    def _require_open(self) -> psycopg.Connection:
        # object.__getattribute__, not self._conn: on an instance whose slots
        # were never assigned (object.__new__, unpickling) a plain read misses
        # the slot, routes back into __getattr__, and recurses until
        # RecursionError instead of reporting a missing attribute.
        conn = object.__getattribute__(self, "_conn")
        if conn is None:
            raise psycopg.OperationalError(
                "connection has been returned to the pool"
            )
        return conn

    def __getattr__(self, name: str):
        # Reached only for names not found on the type or in __slots__.
        conn = object.__getattribute__(self, "_conn")
        if conn is None:
            # Distinguish "you are using a connection you gave back" from
            # "this attribute does not exist": raising OperationalError for
            # the latter breaks hasattr() and getattr(obj, name, default),
            # whose contract is that a missing name raises AttributeError.
            if hasattr(psycopg.Connection, name):
                raise psycopg.OperationalError(
                    "connection has been returned to the pool"
                )
            raise AttributeError(name)
        return getattr(conn, name)

    def __setattr__(self, name: str, value) -> None:
        # Writes have to forward too, or settable connection attributes
        # (autocommit, row_factory, isolation_level, read_only,
        # prepare_threshold) raise AttributeError -- and only on the pooled
        # path, which is the only path production takes.
        if name in PooledConnection.__slots__:
            object.__setattr__(self, name, value)
            return
        setattr(self._require_open(), name, value)


def connect_pooled(
    dsn: str | None = None,
    *,
    timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> psycopg.Connection | PooledConnection:
    """Check a connection out of a process-wide pool.

    Falls back to a plain :func:`connect` when psycopg_pool is not installed.
    Pools are keyed by the fully resolved conninfo, created lazily, and
    checked on checkout so a server-side disconnect (common on serverless
    Postgres) is repaired instead of surfacing as an error mid-query.
    """
    if _ConnectionPool is None:
        return connect(dsn, timeout=timeout)
    # Keyed on the resolved DSN, NOT on the full conninfo: the conninfo bakes
    # in `timeout` (as lock_timeout and connect_timeout), so keying on it
    # would give one database two independent pools of up to POOL_MAX_SIZE
    # each as soon as any caller passed a non-default timeout. Neon caps
    # connections per project, so that doubling is exactly the wrong thing to
    # spend them on. Pooled connections therefore all carry the default
    # lock_timeout; `timeout` here bounds the wait for a free connection. A
    # caller that needs a different lock_timeout wants connect(), not a pool.
    key = resolve_dsn(dsn)
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            pool = _ConnectionPool(
                _startup_conninfo(dsn, DEFAULT_CONNECT_TIMEOUT_SECONDS),
                min_size=1,
                max_size=POOL_MAX_SIZE,
                open=True,
                # Verify the connection on checkout. Serverless Postgres
                # (Neon) drops idle connections on autosuspend; without this
                # the first query after a quiet spell fails instead of the
                # pool quietly replacing the dead connection.
                check=_ConnectionPool.check_connection,
            )
            _pools[key] = pool
            _register_pool_shutdown()
    return PooledConnection(pool, pool.getconn(timeout=timeout))


def close_pools() -> None:
    """Close every pool. Test hook, and a clean-shutdown aid."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


def has_required_tables(conn: psycopg.Connection) -> bool:
    table_names = {
        str(row[0]).lower()
        for row in conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
            """
        ).fetchall()
    }
    if not _REQUIRED_TABLES_FOLDED.issubset(table_names):
        return False
    weather_columns = {
        str(row[0]).lower()
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s
            """,
            ("weatherdata",),
        ).fetchall()
    }
    return "peakgust10" in weather_columns


def open_weather_db(dsn: str | None = None) -> psycopg.Connection:
    """Open an existing WISER database or raise a clear error."""
    conn = connect(dsn)
    if not has_required_tables(conn):
        conn.close()
        raise psycopg.OperationalError(
            "Database schema is incomplete. Run: python scripts/bootstrap_wiser.py"
        )
    return conn


def initialize_schema(conn: psycopg.Connection) -> None:
    # psycopg sends a parameterless query over the simple protocol, which
    # accepts the multi-statement schema file in one round trip. This is the
    # replacement for sqlite3's executescript().
    with SCHEMA_PATH.open("r", encoding="utf-8") as schema_file:
        conn.execute(schema_file.read())
    conn.commit()


def load_normalized_rows(
    conn: psycopg.Connection,
    storm_rows: list[dict[str, object]],
    county_rows: list[dict[str, object]],
    weather_data_rows: list[dict[str, object]],
    hourly_weather_rows: list[dict[str, object]] | None = None,
) -> None:
    # Plain inserts into freshly created tables, so COPY applies: one
    # statement per table instead of a round trip per row.
    copy_rows(
        conn,
        "Storm",
        ("stormCode", "eventDateTime", "eventLength"),
        storm_rows,
    )
    copy_rows(conn, "County", ("county", "state", "ohLine"), county_rows)
    copy_rows(
        conn,
        "WeatherData",
        (
            "stormCode",
            "county",
            "sumAFWA_TOTPRECIP",
            "peakWSPD10",
            "peakGUST10",
            "wgt5",
            "wgt9",
            "wgt13",
            "wgt18",
            "outage",
        ),
        weather_data_rows,
    )
    ensure_reference_data(conn)
    if hourly_weather_rows:
        load_hourly_weather_rows(conn, hourly_weather_rows, reference_ensured=True)
    conn.commit()


def ensure_reference_data(conn: psycopg.Connection) -> None:
    """Seed default metric definitions and CT regions when the tables exist."""
    if _table_exists(conn, "StormMetricDefinition"):
        executemany(
        conn,
            """
            INSERT INTO StormMetricDefinition(
                metricKey,
                displayName,
                storedUnit,
                higherIsMoreSevere,
                sourceColumn
            ) VALUES(
                %(metricKey)s,
                %(displayName)s,
                %(storedUnit)s,
                %(higherIsMoreSevere)s,
                %(sourceColumn)s
            )
            ON CONFLICT DO NOTHING
            """,
            DEFAULT_METRIC_DEFINITION_ROWS,
        )
    if _table_exists(conn, "RegionCountyMapping"):
        available_counties = {
            str(row[0]).lower()
            for row in conn.execute("SELECT county FROM County").fetchall()
        }
        rows = [
            row
            for row in DEFAULT_REGION_COUNTY_ROWS
            if str(row["county"]).lower() in available_counties
        ]
        executemany(
        conn,
            """
            INSERT INTO RegionCountyMapping(region, county, state)
            VALUES(%(region)s, %(county)s, %(state)s)
            ON CONFLICT DO NOTHING
            """,
            rows,
        )


def load_hourly_weather_rows(
    conn: psycopg.Connection,
    hourly_weather_rows: list[dict[str, object]],
    *,
    reference_ensured: bool = False,
) -> None:
    """Load normalized hourly weather rows into the optional hourly table.

    ``reference_ensured`` lets load_normalized_rows say it already seeded the
    reference tables, so a combined load does that work once rather than twice.
    """
    if not _table_exists(conn, "HourlyWeatherObservation"):
        raise psycopg.OperationalError(
            "HourlyWeatherObservation is not present in this database schema"
        )
    if not reference_ensured:
        ensure_reference_data(conn)
    _ensure_hourly_metric_definitions(conn, hourly_weather_rows)
    # SQLite's INSERT OR REPLACE deleted and reinserted the row; every column
    # is listed here, so updating all non-key columns is equivalent.
    executemany(
        conn,
        """
        INSERT INTO HourlyWeatherObservation(
            stormCode,
            county,
            hourOffset,
            metricKey,
            metricValue,
            observedAt,
            sourceColumn
        ) VALUES(
            %(stormCode)s,
            %(county)s,
            %(hourOffset)s,
            %(metricKey)s,
            %(metricValue)s,
            %(observedAt)s,
            %(sourceColumn)s
        )
        ON CONFLICT (stormCode, county, hourOffset, metricKey) DO UPDATE SET
            metricValue = EXCLUDED.metricValue,
            observedAt = EXCLUDED.observedAt,
            sourceColumn = EXCLUDED.sourceColumn
        """,
        hourly_weather_rows,
    )
    conn.commit()


def load_current_weather_rows(
    conn: psycopg.Connection,
    current_weather_rows: list[dict[str, object]],
) -> None:
    """Load current/live weather observations into the staging table."""
    if not _table_exists(conn, "CurrentWeatherObservation"):
        raise psycopg.OperationalError(
            "CurrentWeatherObservation is not present in this database schema"
        )
    executemany(
        conn,
        """
        INSERT INTO CurrentWeatherObservation(
            sourceSystem,
            sourceRecordId,
            observedAt,
            county,
            state,
            peakWSPD10,
            peakGUST10,
            sumAFWA_TOTPRECIP,
            humidity,
            outage,
            rawPayload
        ) VALUES(
            %(sourceSystem)s,
            %(sourceRecordId)s,
            %(observedAt)s,
            %(county)s,
            %(state)s,
            %(peakWSPD10)s,
            %(peakGUST10)s,
            %(sumAFWA_TOTPRECIP)s,
            %(humidity)s,
            %(outage)s,
            %(rawPayload)s
        )
        ON CONFLICT (sourceSystem, sourceRecordId, observedAt, county) DO UPDATE SET
            state = EXCLUDED.state,
            peakWSPD10 = EXCLUDED.peakWSPD10,
            peakGUST10 = EXCLUDED.peakGUST10,
            sumAFWA_TOTPRECIP = EXCLUDED.sumAFWA_TOTPRECIP,
            humidity = EXCLUDED.humidity,
            outage = EXCLUDED.outage,
            rawPayload = EXCLUDED.rawPayload
        """,
        current_weather_rows,
    )
    conn.commit()


def _ensure_hourly_metric_definitions(
    conn: psycopg.Connection,
    hourly_weather_rows: list[dict[str, object]],
) -> None:
    if not _table_exists(conn, "StormMetricDefinition"):
        return
    rows = [
        {
            "metricKey": str(metric_key),
            "displayName": _metric_display_name(str(metric_key)),
            "storedUnit": "unknown",
            "higherIsMoreSevere": 1,
            "sourceColumn": None,
        }
        for metric_key in sorted(
            {row["metricKey"] for row in hourly_weather_rows}
        )
    ]
    executemany(
        conn,
        """
        INSERT INTO StormMetricDefinition(
            metricKey,
            displayName,
            storedUnit,
            higherIsMoreSevere,
            sourceColumn
        ) VALUES(
            %(metricKey)s,
            %(displayName)s,
            %(storedUnit)s,
            %(higherIsMoreSevere)s,
            %(sourceColumn)s
        )
        ON CONFLICT DO NOTHING
        """,
        rows,
    )


def _metric_display_name(metric_key: str) -> str:
    return metric_key.replace("_", " ").strip().title() or metric_key


def _table_exists(conn: psycopg.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_name = %s
        """,
        (table_name.lower(),),
    ).fetchone()
    return row is not None


def get_table_counts(conn: psycopg.Connection) -> dict[str, int]:
    result: dict[str, int] = {}
    for table_name in ("Storm", "County", "WeatherData"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        result[table_name] = int(count)
    return result
