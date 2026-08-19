from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from wiser.data_pipeline import (
    load_raw_csv,
    normalize_rows,
    write_normalized_csvs,
)
from wiser.database import connect, initialize_schema, load_normalized_rows


@dataclass
class DataRetrievalProcessingAgent:
    raw_csv_path: str | Path
    _normalized: (
        tuple[
            list[dict[str, object]],
            list[dict[str, object]],
            list[dict[str, object]],
        ]
        | None
    ) = field(default=None, init=False, repr=False, compare=False)

    def load_raw_data(self) -> list[dict[str, str]]:
        return load_raw_csv(self.raw_csv_path)

    def normalize(
        self,
    ) -> tuple[
        list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]
    ]:
        if self._normalized is None:
            raw_rows = self.load_raw_data()
            self._normalized = normalize_rows(raw_rows)
        return self._normalized

    def save_normalized(self, output_dir: str | Path) -> tuple[Path, Path, Path]:
        storm_rows, county_rows, weather_data_rows = self.normalize()
        write_normalized_csvs(storm_rows, county_rows, weather_data_rows, output_dir)
        output_path = Path(output_dir)
        return (
            output_path / "storm.csv",
            output_path / "county.csv",
            output_path / "weather_data.csv",
        )

    def build_database(self, dsn: str | None = None) -> psycopg.Connection:
        """Build the WISER schema and load it from the normalized CSVs.

        ``dsn`` defaults to ``DATABASE_URL`` (see ``database.resolve_dsn``).
        """
        storm_rows, county_rows, weather_data_rows = self.normalize()
        conn = connect(dsn)
        initialize_schema(conn)
        load_normalized_rows(conn, storm_rows, county_rows, weather_data_rows)
        return conn
