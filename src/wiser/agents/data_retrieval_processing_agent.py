from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wiser.data_pipeline import (
    load_raw_csv,
    normalize_rows,
    write_normalized_csvs,
)


@dataclass
class DataRetrievalProcessingAgent:
    raw_csv_path: str | Path

    def load_raw_data(self) -> list[dict[str, str]]:
        return load_raw_csv(self.raw_csv_path)

    def normalize(
        self,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
        raw_rows = self.load_raw_data()
        return normalize_rows(raw_rows)

    def save_normalized(self, output_dir: str | Path) -> tuple[Path, Path, Path]:
        storm_rows, county_rows, storm_county_rows = self.normalize()
        write_normalized_csvs(storm_rows, county_rows, storm_county_rows, output_dir)
        output_path = Path(output_dir)
        return (
            output_path / "storm.csv",
            output_path / "county.csv",
            output_path / "storm_county.csv",
        )
