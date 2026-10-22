# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hongren Shen
"""Read WSC unit values and integrate them into source-local hourly means."""

import csv
import lzma
import math
import re
from datetime import datetime, timedelta

import pandas as pd

# Match the original workflow: intervals of 121 minutes or longer are excluded.
GAP_LIMIT = timedelta(minutes=121)


def _join(values):
    return ";".join(sorted({part.strip() for value in values
                           for part in str(value).split(";") if part.strip()}))


def read_unit_values(paths, start, stop):
    """Read a time window plus boundary support, dropping Grade -2 and nonfinite values."""
    records, offsets = [], set()
    for path in paths:
        with lzma.open(path, "rt", encoding="utf-8-sig", newline="") as source:
            rows = csv.reader(line for line in source if line.strip() and not line.startswith("#"))
            header = next(rows)
            time_column = next(name for name in header if name.startswith("Timestamp"))
            match = re.fullmatch(r"Timestamp \((UTC[+-]\d{2}:\d{2})\)", time_column)
            if not match:
                raise ValueError(f"Missing fixed UTC offset in {path}")
            offsets.add(match.group(1))
            fields = [header.index(name) for name in
                      (time_column, "Value", "Grade", "Approval Level", "Qualifiers")]
            for row in rows:
                if len(row) < len(header):
                    raise ValueError(f"Incomplete CSV row in {path}: {row}")
                try:
                    timestamp = datetime.fromisoformat(row[fields[0]])
                except ValueError:
                    continue
                if not start - GAP_LIMIT <= timestamp <= stop + GAP_LIMIT:
                    continue
                try:
                    value = float(row[fields[1]])
                except ValueError:
                    continue
                grade = row[fields[2]].strip()
                # Grade values are numeric codes; empty or unknown codes remain in the CSV.
                try:
                    grade = str(int(float(grade)))
                except ValueError:
                    pass
                if not math.isfinite(value) or grade == "-2":
                    continue
                approval = row[fields[3]].strip()
                # Some source rows contain unquoted commas in the final Qualifiers field.
                qualifiers = ",".join(row[fields[4]:]).strip()
                records.append((timestamp, value, grade, approval, qualifiers))
    if len(offsets) != 1:
        raise ValueError("Select files with one consistent source UTC offset.")
    if not records:
        raise ValueError("No usable unit values near the requested time window.")
    frame = pd.DataFrame(records, columns=["timestamp", "value", "grades", "approval_levels", "qualifiers"])
    frame = frame.groupby("timestamp", sort=True).agg(
        value=("value", "mean"), grades=("grades", _join),
        approval_levels=("approval_levels", _join), qualifiers=("qualifiers", _join),
    )
    frame.attrs["utc_offset"] = offsets.pop()
    return frame


def hourly_means(paths, start, end, min_coverage=0.0):
    """Integrate linear segments into [hour, hour + 1h); END is an inclusive date."""
    start = pd.Timestamp(start).normalize()
    stop = pd.Timestamp(end).normalize() + pd.Timedelta(days=1)
    if stop <= start or not 0 <= min_coverage <= 1:
        raise ValueError("Use END >= START and MIN_COVERAGE between 0 and 1.")
    points = read_unit_values(paths, start.to_pydatetime(), stop.to_pydatetime())
    index = pd.date_range(start, stop, freq="h", inclusive="left", name="timestamp")
    integrals, covered = [0.0] * len(index), [0.0] * len(index)
    metadata = [[set() for _ in range(3)] for _ in index]
    rows = list(points.itertuples())
    for left, right in zip(rows, rows[1:]):
        duration = right.Index - left.Index
        # Check original adjacent endpoints before splitting at hour boundaries.
        if not timedelta(0) < duration < GAP_LIMIT:
            continue
        a, end_piece = max(left.Index, start), min(right.Index, stop)
        while a < end_piece:
            hour = a.floor("h")
            b = min(hour + pd.Timedelta(hours=1), end_piece)
            seconds = (b - a).total_seconds()
            va = left.value + (right.value - left.value) * ((a - left.Index) / duration)
            vb = left.value + (right.value - left.value) * ((b - left.Index) / duration)
            i = int((hour - start).total_seconds() // 3600)
            integrals[i] += (va + vb) * 0.5 * seconds
            covered[i] += seconds
            for j, field in enumerate(("grades", "approval_levels", "qualifiers")):
                metadata[i][j].update((getattr(left, field), getattr(right, field)))
            a = b
    values = [total / seconds if seconds > 0 and seconds / 3600 >= min_coverage
              else float("nan") for total, seconds in zip(integrals, covered)]
    frame = pd.DataFrame({"value": values,
                          "coverage_fraction": [seconds / 3600 for seconds in covered]}, index=index)
    for j, field in enumerate(("grades", "approval_levels", "qualifiers")):
        frame[field] = [_join(parts[j]) for parts in metadata]
    if frame["value"].notna().sum() == 0:
        raise ValueError("No hourly values meet the gap and coverage rules in this window.")
    frame["utc_offset"] = points.attrs["utc_offset"]
    frame.attrs["time_note"] = f"Source-local {points.attrs['utc_offset']}; fixed offset, no DST."
    return frame
