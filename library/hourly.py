# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hongren Shen
"""Integrate WSC unit values into source-local hourly and daily means."""

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


def read_unit_values(paths, start, stop, *, allow_empty=False):
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
                if timestamp.tzinfo is not None:
                    raise ValueError("Use naive source-local timestamps with the header's fixed UTC offset.")
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
                except (ValueError, OverflowError):
                    pass
                if not math.isfinite(value) or grade == "-2":
                    continue
                approval = row[fields[3]].strip()
                # Some source rows contain unquoted commas in the final Qualifiers field.
                qualifiers = ",".join(row[fields[4]:]).strip()
                records.append((timestamp, value, grade, approval, qualifiers))
    if len(offsets) != 1:
        raise ValueError("Select files with one consistent source UTC offset.")
    if not records and not allow_empty:
        raise ValueError("No usable unit values near the requested time window.")
    frame = pd.DataFrame(records, columns=["timestamp", "value", "grades", "approval_levels", "qualifiers"])
    # Overlapping source segments must not give identical observations extra weight.
    frame = frame.drop_duplicates(subset=["timestamp", "value", "grades", "approval_levels", "qualifiers"])
    frame = frame.groupby("timestamp", sort=True).agg(
        value=("value", "mean"), grades=("grades", _join),
        approval_levels=("approval_levels", _join), qualifiers=("qualifiers", _join),
    )
    frame.attrs["utc_offset"] = offsets.pop()
    return frame


def _has_grade(text, codes):
    for part in str(text).split(";"):
        try:
            if int(float(part.strip())) in codes:
                return True
        except (ValueError, OverflowError):
            pass
    return False


def _suspect_approval(text):
    text = str(text).lower()
    return "preliminary" in text or "suspect" in text


def _source_intervals(points):
    """Keep valid adjacent endpoints and their quality flags before bin splitting."""
    intervals = []
    rows = list(points.itertuples())
    for left, right in zip(rows, rows[1:]):
        duration = right.Index - left.Index
        if not timedelta(0) < duration < GAP_LIMIT:
            continue
        endpoints = (left, right)
        flags = {
            "grade10": any(_has_grade(row.grades, {10}) for row in endpoints),
            "grade20": any(_has_grade(row.grades, {20}) for row in endpoints),
            "suspect_grade": any(_has_grade(row.grades, {10, 20}) for row in endpoints),
            "approval_suspect": any(_suspect_approval(row.approval_levels) for row in endpoints),
            # An endpoint must carry both flags before they propagate to its interval.
            "approval_grade_suspect": any(
                _suspect_approval(row.approval_levels) and _has_grade(row.grades, {10, 20})
                for row in endpoints
            ),
        }
        intervals.append((left, right, duration, flags))
    return intervals


def _integrate_intervals(intervals, start, stop, *, frequency, min_coverage, metadata):
    """Split original linear intervals directly at the requested bin boundaries."""
    index = pd.date_range(start, stop, freq=frequency, inclusive="left", name="timestamp")
    width = pd.Timedelta(hours=1) if frequency == "h" else pd.Timedelta(days=1)
    width_seconds = width.total_seconds()
    integrals, covered = [0.0] * len(index), [0.0] * len(index)
    quality = [[set() for _ in range(3)] for _ in index] if metadata else None
    durations = {name: [0.0] * len(index) for name in (
        "grade10", "grade20", "suspect_grade", "approval_suspect", "approval_grade_suspect"
    )}
    for left, right, duration, flags in intervals:
        a, end_piece = max(left.Index, start), min(right.Index, stop)
        while a < end_piece:
            boundary = a.floor(frequency)
            b = min(boundary + width, end_piece)
            seconds = (b - a).total_seconds()
            va = left.value + (right.value - left.value) * ((a - left.Index) / duration)
            vb = left.value + (right.value - left.value) * ((b - left.Index) / duration)
            i = int((boundary - start).total_seconds() // width_seconds)
            integrals[i] += (va + vb) * 0.5 * seconds
            covered[i] += seconds
            for name, flagged in flags.items():
                if flagged:
                    durations[name][i] += seconds
            if metadata:
                for j, field in enumerate(("grades", "approval_levels", "qualifiers")):
                    quality[i][j].update((getattr(left, field), getattr(right, field)))
            a = b
    values = [total / seconds if seconds > 0 and seconds / width_seconds >= min_coverage
              else float("nan") for total, seconds in zip(integrals, covered)]
    frame = pd.DataFrame({"value": values,
                          "coverage_fraction": [seconds / width_seconds for seconds in covered],
                          "valid_duration_seconds": covered}, index=index)
    for name, flagged_seconds in durations.items():
        frame[f"{name}_fraction"] = [flagged / valid if valid > 0 else 0.0
                                     for flagged, valid in zip(flagged_seconds, covered)]
    if metadata:
        for j, field in enumerate(("grades", "approval_levels", "qualifiers")):
            frame[field] = [_join(parts[j]) for parts in quality]
    return frame


def aggregate_unit_values(paths, start, end, min_coverage=0.0):
    """Read once and integrate the same intervals independently into hours and days.

    END is an inclusive native source-local date. Daily diagnostics do not use
    hourly means, hourly quality unions, or the hourly coverage cutoff. The
    suspect-grade fraction counts intervals flagged 10 and/or 20 once, even
    when both grades occur. Flagged duration follows endpoint metadata; it is
    not a measured physical ice duration.
    """
    start = pd.Timestamp(start).normalize()
    stop = pd.Timestamp(end).normalize() + pd.Timedelta(days=1)
    if start.tzinfo is not None or stop.tzinfo is not None:
        raise ValueError("Use naive source-local dates for START and END.")
    if pd.isna(start) or pd.isna(stop) or stop <= start or not 0 <= min_coverage <= 1:
        raise ValueError("Use END >= START and MIN_COVERAGE between 0 and 1.")
    points = read_unit_values(paths, start.to_pydatetime(), stop.to_pydatetime(), allow_empty=True)
    intervals = _source_intervals(points)
    hourly = _integrate_intervals(intervals, start, stop, frequency="h",
                                  min_coverage=min_coverage, metadata=True)
    hourly = hourly[["value", "coverage_fraction", "grades", "approval_levels", "qualifiers"]].copy()
    daily = _integrate_intervals(intervals, start, stop, frequency="D",
                                 min_coverage=0.0, metadata=False)
    daily = daily.rename(columns={"value": "unit_daily_mean"})
    hourly["utc_offset"] = points.attrs["utc_offset"]
    for frame in (hourly, daily):
        frame.attrs["utc_offset"] = points.attrs["utc_offset"]
        frame.attrs["time_note"] = f"Source-local {points.attrs['utc_offset']}; fixed offset, no DST."
    return hourly, daily


def hourly_means(paths, start, end, min_coverage=0.0):
    """Return legacy hourly means; END is an inclusive native source-local date."""
    frame, _ = aggregate_unit_values(paths, start, end, min_coverage)
    if frame["value"].notna().sum() == 0:
        raise ValueError("No hourly values meet the gap and coverage rules in this window.")
    return frame
