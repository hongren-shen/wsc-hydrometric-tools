# SPDX-License-Identifier: MIT
# Copyright (c) 2025-2026 Hongren Shen
"""Read one station's daily discharge or water level from HYDAT SQLite."""

import calendar
import sqlite3
from pathlib import Path

import pandas as pd


def read_daily(database, station, start, end, data_type="Discharge", *, allow_missing=False):
    """Read HYDAT dates and symbols; optionally retain an entirely missing reference."""
    table, prefix = {
        "Discharge": ("DLY_FLOWS", "FLOW"),
        "Stage": ("DLY_LEVELS", "LEVEL"),
    }[data_type]
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    if end < start:
        raise ValueError("END must be on or after START.")
    database = Path(database).resolve()
    if not database.is_file():
        raise FileNotFoundError(f"Place an extracted HYDAT SQLite file at {database}")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE STATION_NUMBER = ? "
            "AND YEAR BETWEEN ? AND ? ORDER BY YEAR, MONTH",
            (station, start.year, end.year),
        ).fetchall()
    if not rows and not allow_missing:
        raise ValueError(f"No {data_type} records for {station} in the selected years.")

    fields = {key.upper(): key for key in rows[0].keys()} if rows else {}
    records = []
    for row in rows:
        year, month = int(row["YEAR"]), int(row["MONTH"])
        # NO_DAYS counts observations; it is not the last calendar day.
        for day in range(1, calendar.monthrange(year, month)[1] + 1):
            date = pd.Timestamp(year, month, day)
            if not start <= date <= end:
                continue
            value_key = next(
                key for key in (f"{prefix}{day}", f"{prefix}{day:02d}") if key in fields
            )
            symbol_key = next(
                (key for key in (
                    f"{prefix}_SYMBOL{day}", f"{prefix}_SYMBOL{day:02d}",
                    f"{prefix}_SYMB{day}", f"{prefix}_SYMB{day:02d}",
                ) if key in fields), None,
            )
            symbol = row[fields[symbol_key]] if symbol_key else ""
            records.append((date, row[fields[value_key]], symbol or ""))
    frame = pd.DataFrame(records, columns=["timestamp", "value", "symbol"])
    if frame.empty and not allow_missing:
        raise ValueError("No daily records in the requested date window.")
    if frame["timestamp"].duplicated().any():
        raise ValueError("Duplicate HYDAT dates found; check the database.")
    frame = frame.set_index("timestamp").reindex(pd.date_range(start, end, freq="D"))
    frame.index.name = "timestamp"
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame["symbol"] = frame["symbol"].fillna("")
    if frame["value"].notna().sum() == 0 and not allow_missing:
        raise ValueError("All daily observations in the requested window are missing.")
    frame.attrs["time_note"] = "HYDAT source date labels; no time-zone conversion."
    return frame
