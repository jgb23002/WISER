"""pytest entry point for the PostgreSQL test harness.

The real harness lives in ``tests/pg_harness.py`` so that test modules can
import it directly (and still run under plain ``python tests/test_x.py``).
Importing it here means pytest applies the ``DATABASE_URL`` redirect before it
imports any test module.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pg_harness import (  # noqa: E402,F401
    SCHEMA_SQL,
    TEST_DSN,
    TEST_SCHEMA,
    blank_conn,
    executemany,
    make_conn,
    new_connection,
    reset_schema,
)
