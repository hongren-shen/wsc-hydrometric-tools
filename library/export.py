# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 Hongren Shen
"""Write a regular single-station time series to CSV and Raven RVT."""

import math
from pathlib import Path

import pandas as pd

RAVEN_NA = -1.2345


def write_outputs(frame, output_stem, station, data_type, timestep_days, basin_id=1):
    """Keep quality fields in CSV; RVT contains observation values only."""
    token, units = {"Discharge": ("HYDROGRAPH", "m3/s"),
                    "Stage": ("WATER_LEVEL", "m")}[data_type]
    if frame.empty:
        raise ValueError("Cannot export an empty time series.")
    expected = pd.date_range(frame.index[0], periods=len(frame),
                             freq=pd.Timedelta(days=timestep_days))
    if not frame.index.equals(expected):
        raise ValueError("RVT requires a complete, regular time index.")
    stem = Path(output_stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    csv_path, rvt_path = stem.with_suffix(".csv"), stem.with_suffix(".rvt")
    frame.to_csv(csv_path, float_format="%.6f", na_rep="")
    with rvt_path.open("w", encoding="utf-8", newline="\n") as target:
        target.write(f"# WSC station: {station}; {data_type}\n")
        target.write(f"# {frame.attrs.get('time_note', '')}\n")
        target.write(f"# Missing value: {RAVEN_NA}; quality flags are in the companion CSV.\n")
        target.write(f":ObservationData {token} {basin_id} {units}\n")
        target.write(f"    {frame.index[0]:%Y-%m-%d %H:%M:%S} {timestep_days:.10g} {len(frame)}\n")
        for value in frame["value"]:
            number = float(value)
            target.write(f"    {number if math.isfinite(number) else RAVEN_NA:.4f}\n")
        target.write(":EndObservationData\n")
    print(f"Saved {len(frame)} rows: {csv_path} and {rvt_path}")
    return csv_path, rvt_path
