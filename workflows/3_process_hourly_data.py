# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hongren Shen
"""Demo 3: turn one station's downloaded unit values into hourly means."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from library.hourly import hourly_means
from library.export import write_outputs

# Keep station, data type and quality consistent with workflow 2.
STATION = "02GB001"
DATA_TYPE = "Discharge"  # Discharge or Stage
QUALITY = "corrected"
START = "2020-01-01"
END = "2020-12-31"  # Inclusive date
MIN_COVERAGE = 0.0  # Keep any hour with positive coverage, matching the source workflow.
BASIN_ID = 1  # Example Raven subbasin ID; replace for your model.
INPUT_DIR = ROOT / "data" / "unit_values" / DATA_TYPE / QUALITY
OUTPUT = ROOT / "outputs" / f"{STATION}_hourly_{DATA_TYPE.lower()}"


def main():
    files = sorted(INPUT_DIR.glob(f"{DATA_TYPE}.Working@{STATION}.*_{QUALITY}.csv.xz"))
    if not files:
        raise FileNotFoundError("No input files found. Run workflow 2 first.")
    frame = hourly_means(files, START, END, MIN_COVERAGE)
    write_outputs(frame, OUTPUT, STATION, DATA_TYPE, 1 / 24, BASIN_ID)


if __name__ == "__main__":
    main()
