# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hongren Shen
"""Offline checks of calendar handling, integration, and safe downloads."""

import io
import lzma
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from library.daily import read_daily
from library.download import download_station
from library.export import write_outputs
from library.hourly import hourly_means


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def unit_file(self, rows, name="unit.csv.xz", offset="UTC-05:00"):
        path = self.root / name
        header = f"ISO 8601 UTC,Timestamp ({offset}),Value,Approval Level,Grade,Qualifiers\n"
        with lzma.open(path, "wt", encoding="utf-8") as target:
            target.write("# Test fixture\n" + header)
            for time, value, grade, qualifiers in rows:
                target.write(f",{time},{value},Approved,{grade},{qualifiers}\n")
        return path

    def daily_database(self, data_type="Discharge"):
        table, prefix = {"Discharge": ("DLY_FLOWS", "FLOW"),
                         "Stage": ("DLY_LEVELS", "LEVEL")}[data_type]
        path = self.root / "HYDAT.sqlite3"
        columns = ", ".join(f"{prefix}{d} REAL, {prefix}_SYMBOL{d} TEXT" for d in range(1, 32))
        with sqlite3.connect(path) as connection:
            connection.execute(f"CREATE TABLE {table} (STATION_NUMBER TEXT, YEAR INTEGER, "
                               f"MONTH INTEGER, NO_DAYS INTEGER, {columns})")
            connection.execute(f"INSERT INTO {table} (STATION_NUMBER,YEAR,MONTH,NO_DAYS,"
                               f"{prefix}1,{prefix}_SYMBOL1,{prefix}29,{prefix}_SYMBOL29) "
                               "VALUES ('02GB001',2020,2,1,-0.5,'E',3.0,'B')")
        return path

    def test_daily_leap_month_missing_days_symbols_and_negative_values(self):
        database = self.daily_database()
        before = database.read_bytes()
        frame = read_daily(database, "02GB001", "2020-02-01", "2020-03-01")
        self.assertEqual(len(frame), 30)
        self.assertEqual(frame.loc["2020-02-01", "value"], -0.5)
        self.assertEqual(frame.loc["2020-02-29", "symbol"], "B")
        self.assertEqual(frame.loc["2020-02-29", "value"], 3.0)
        self.assertTrue(math.isnan(frame.loc["2020-03-01", "value"]))
        self.assertEqual(database.read_bytes(), before)

    def test_daily_stage_and_missing_station(self):
        database = self.daily_database("Stage")
        frame = read_daily(database, "02GB001", "2020-02-01", "2020-02-29", "Stage")
        self.assertEqual(frame.iloc[0].value, -0.5)
        with self.assertRaisesRegex(ValueError, "No Stage records"):
            read_daily(database, "00ZZ999", "2020-02-01", "2020-02-29", "Stage")

    def test_time_weighted_mean_differs_from_sample_mean(self):
        path = self.unit_file([("2020-01-01 00:00:00", 0, -1, ""),
                               ("2020-01-01 00:15:00", 0, -1, ""),
                               ("2020-01-01 01:00:00", 12, -1, "")])
        frame = hourly_means([path], "2020-01-01", "2020-01-01")
        self.assertAlmostEqual(frame.iloc[0].value, 4.5)
        self.assertEqual(frame.iloc[0].coverage_fraction, 1)
        self.assertTrue(frame.value.iloc[1:].isna().all())

    def test_boundary_support_partial_coverage_and_no_extrapolation(self):
        path = self.unit_file([("2019-12-31 23:30:00", 0, -1, ""),
                               ("2020-01-01 00:30:00", 6, -1, "")])
        frame = hourly_means([path], "2020-01-01", "2020-01-01")
        self.assertEqual(frame.iloc[0].value, 4.5)
        self.assertEqual(frame.iloc[0].coverage_fraction, 0.5)
        self.assertTrue(frame.value.iloc[1:].isna().all())

    def test_gap_of_exactly_121_minutes_is_excluded_before_splitting(self):
        path = self.unit_file([("2020-01-01 00:00:00", 0, -1, ""),
                               ("2020-01-01 02:01:00", 10, -1, ""),
                               ("2020-01-01 03:01:00", 14, -1, "")])
        frame = hourly_means([path], "2020-01-01", "2020-01-01")
        self.assertTrue(frame.value.iloc[:2].isna().all())
        self.assertAlmostEqual(frame.iloc[2].coverage_fraction, 59 / 60)

    def test_two_hour_interval_is_split_with_linear_boundary_values(self):
        path = self.unit_file([("2020-01-01 00:00:00", 0, -1, ""),
                               ("2020-01-01 02:00:00", 120, -1, "")])
        frame = hourly_means([path], "2020-01-01", "2020-01-01")
        self.assertEqual(frame.iloc[0].value, 30)
        self.assertEqual(frame.iloc[1].value, 90)

    def test_exclusions_duplicates_quality_and_unquoted_qualifier_commas(self):
        path = self.unit_file([("2020-01-01 00:00:00", 0, 10, "Ice,Other"),
                               ("2020-01-01 00:00:00", 4, 20, ""),
                               ("2020-01-01 00:15:00", 999, -2, ""),
                               ("2020-01-01 00:30:00", "NaN", -1, ""),
                               ("2020-01-01 01:00:00", 6, -1, "")])
        frame = hourly_means([path], "2020-01-01", "2020-01-01")
        self.assertEqual(frame.iloc[0].value, 4)
        self.assertEqual(set(frame.iloc[0].grades.split(";")), {"-1", "10", "20"})
        self.assertEqual(frame.iloc[0].qualifiers, "Ice,Other")
        self.assertEqual(frame.iloc[0].utc_offset, "UTC-05:00")

    def test_coverage_threshold_and_mixed_clocks(self):
        rows = [("2020-01-01 00:30:00", 2, -1, ""),
                ("2020-01-01 01:00:00", 4, -1, ""),
                ("2020-01-01 02:00:00", 8, -1, "")]
        first = self.unit_file(rows)
        frame = hourly_means([first], "2020-01-01", "2020-01-01", min_coverage=1)
        self.assertTrue(math.isnan(frame.iloc[0].value))
        self.assertEqual(frame.iloc[1].value, 6)
        second = self.unit_file(rows, name="other.csv.xz", offset="UTC-06:00")
        with self.assertRaisesRegex(ValueError, "consistent source UTC offset"):
            hourly_means([first, second], "2020-01-01", "2020-01-01")

    def test_rvt_count_missing_value_and_stage_token(self):
        frame = pd.DataFrame({"value": [float("nan"), -0.5]},
                             index=pd.date_range("2020-01-01", periods=2, freq="h", name="timestamp"))
        csv_path, rvt_path = write_outputs(frame, self.root / "stage", "02GB001", "Stage", 1 / 24)
        lines = rvt_path.read_text().splitlines()
        self.assertIn(":ObservationData WATER_LEVEL 1 m", lines)
        self.assertIn("    2020-01-01 00:00:00 0.04166666667 2", lines)
        self.assertEqual(lines[-3:], ["    -1.2345", "    -0.5000", ":EndObservationData"])
        self.assertTrue(pd.isna(pd.read_csv(csv_path).value.iloc[0]))

    def test_download_selects_one_station_checks_xz_and_skips_existing(self):
        name = "Discharge.Working@02GB001.20110101_corrected.csv.xz"
        listing = f'<a href="{name}">one</a><a href="Discharge.Working@02AB006.20110101_corrected.csv.xz">other</a>'
        payload = lzma.compress(b"test observations\n")
        with patch("library.download._open", side_effect=[io.BytesIO(listing.encode()), io.BytesIO(payload)]) as opened:
            paths = download_station("02GB001", self.root)
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(paths[0].read_bytes(), payload)
        with patch("library.download._open", return_value=io.BytesIO(listing.encode())) as opened:
            download_station("02GB001", self.root)
        self.assertEqual(opened.call_count, 1)
        with patch("library.download._open", side_effect=[io.BytesIO(listing.encode()), io.BytesIO(b"truncated")]):
            with self.assertRaises(lzma.LZMAError):
                download_station("02GB001", self.root, overwrite=True)
        self.assertEqual(paths[0].read_bytes(), payload)
        self.assertFalse(paths[0].with_name(name + ".part").exists())


if __name__ == "__main__":
    unittest.main()
