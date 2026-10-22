# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hongren Shen
"""Demo 2: download the available unit-value files for one station, in serial."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from library.download import download_station

# Edit these settings. Downloads remain compressed as .csv.xz files.
STATION = "02GB001"
DATA_TYPE = "Discharge"  # Discharge or Stage
QUALITY = "corrected"  # corrected or raw
OUTPUT_DIR = ROOT / "data" / "unit_values"
OVERWRITE = False


def main():
    download_station(STATION, OUTPUT_DIR, DATA_TYPE, QUALITY, OVERWRITE)


if __name__ == "__main__":
    main()
