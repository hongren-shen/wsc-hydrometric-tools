# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 Hongren Shen
"""Demo 1: process daily HYDAT observations for one station."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from library.daily import read_daily
from library.export import write_outputs

# Edit these settings. Paths do not depend on the current working directory.
HYDAT_DB = ROOT / "data" / "Hydat.sqlite3"
STATION = "02GB001"  # GRAND RIVER AT BRANTFORD
DATA_TYPE = "Discharge"  # Discharge or Stage
START = "2020-01-01"
END = "2020-12-31"  # Inclusive date
BASIN_ID = 1  # Example Raven subbasin ID; replace for your model.
OUTPUT = ROOT / "outputs" / f"{STATION}_daily_{DATA_TYPE.lower()}"


def main():
    frame = read_daily(HYDAT_DB, STATION, START, END, DATA_TYPE)
    write_outputs(frame, OUTPUT, STATION, DATA_TYPE, 1, BASIN_ID)


if __name__ == "__main__":
    main()
