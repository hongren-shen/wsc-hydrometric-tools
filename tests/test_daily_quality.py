# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hongren Shen
"""Check daily HYDAT QA decisions and raw-interval integration independently."""

import csv
import lzma
import math
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import pandas as pd

from library.daily import read_daily
from library.daily_quality import DailyQualityPolicy, apply_daily_qa, assess_daily_quality
from library.hourly import aggregate_unit_values


class DailyQualityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.policy = DailyQualityPolicy()

    def unit_file(self, rows):
        path = self.root / "unit.csv.xz"
        with lzma.open(path, "wt", encoding="utf-8", newline="") as target:
            target.write("# Synthetic WSC observations\n")
            writer = csv.writer(target)
            writer.writerow(["ISO 8601 UTC", "Timestamp (UTC-05:00)", "Value",
                             "Approval Level", "Grade", "Qualifiers"])
            for timestamp, value, grade, approval in rows:
                writer.writerow(["", timestamp, value, approval, grade, ""])
        return path

    def daily(self, **overrides):
        values = {"unit_daily_mean": 12.0, "coverage_fraction": 1.0,
                  "valid_duration_seconds": 86400.0, "grade10_fraction": 0.5,
                  "grade20_fraction": 0.0, "suspect_grade_fraction": 0.5,
                  "approval_suspect_fraction": 0.0,
                  "approval_grade_suspect_fraction": 0.0}
        values.update(overrides)
        return pd.DataFrame([values], index=pd.DatetimeIndex(["2020-01-01"], name="timestamp"))

    def hydat(self, value=10.0, symbol="E"):
        return pd.DataFrame({"value": [value], "symbol": [symbol]},
                            index=pd.DatetimeIndex(["2020-01-01"], name="timestamp"))

    def hourly(self):
        return pd.DataFrame({"value": [12.0, 12.0, 12.0, float("nan")],
                             "coverage_fraction": [1.0, 1.0, 1.0, 0.0],
                             "grades": ["10", "-1", "20", ""],
                             "approval_levels": ["Approved", "Approved", "Approved", ""],
                             "qualifiers": ["Ice", "", "Estimated", ""],
                             "utc_offset": ["UTC-05:00"] * 4},
                            index=pd.date_range("2020-01-01", periods=4, freq="h", name="timestamp"))

    def test_exact_grade_coverage_and_bias_thresholds_fail(self):
        daily = self.daily(coverage_fraction=0.9, valid_duration_seconds=77760.0)
        qa = assess_daily_quality(daily, self.hydat(), policy=self.policy)
        self.assertEqual(qa.iloc[0].daily_qa_status, "FAIL")
        self.assertAlmostEqual(qa.iloc[0].daily_qa_abs_percent_bias, 20.0)
        self.assertEqual(qa.iloc[0].grade_outlier_day_flag, 1)
        self.assertEqual(qa.iloc[0].hydat_daily_symbol, "E")

    def test_below_grade_threshold_does_not_trigger_comparison(self):
        qa = assess_daily_quality(self.daily(suspect_grade_fraction=0.499999),
                                  self.hydat(), policy=self.policy)
        self.assertEqual(qa.iloc[0].daily_qa_status, "NOT_REQUIRED")
        self.assertEqual(qa.iloc[0].grade_outlier_day_flag, 0)
        out = apply_daily_qa(self.hourly(), qa, policy=self.policy)
        pd.testing.assert_series_equal(out.value, self.hourly().value)

    def test_below_bias_threshold_passes_and_retains_values(self):
        qa = assess_daily_quality(self.daily(unit_daily_mean=11.999),
                                  self.hydat(), policy=self.policy)
        self.assertEqual(qa.iloc[0].daily_qa_status, "PASS")
        out = apply_daily_qa(self.hourly(), qa, policy=self.policy)
        pd.testing.assert_series_equal(out.value, self.hourly().value)
        self.assertEqual(out.qa_applied.sum(), 0)

    def test_failed_grade_day_masks_suspect_hours_only_and_keeps_originals(self):
        hourly = self.hourly()
        before = hourly.copy(deep=True)
        qa = assess_daily_quality(self.daily(), self.hydat(), policy=self.policy)
        daily_before = qa.copy(deep=True)
        out = apply_daily_qa(hourly, qa, policy=self.policy)
        self.assertTrue(out.value.iloc[[0, 2]].isna().all())
        self.assertEqual(out.value.iloc[1], 12.0)
        self.assertTrue(math.isnan(out.value.iloc[3]))
        self.assertEqual(out.qa_applied.tolist(), [1, 0, 1, 0])
        self.assertTrue(out.mask_reason.iloc[[0, 2]].str.len().gt(0).all())
        pd.testing.assert_series_equal(out.value_before_daily_qa,
                                       before.value.rename("value_before_daily_qa"))
        pd.testing.assert_frame_equal(hourly, before)
        pd.testing.assert_frame_equal(qa, daily_before)

    def test_missing_hydat_keeps_suspect_hours_even_when_coverage_is_low(self):
        daily = self.daily(coverage_fraction=0.5, valid_duration_seconds=43200.0,
                           approval_suspect_fraction=0.1,
                           approval_grade_suspect_fraction=0.1)
        for hydat in [None, pd.DataFrame(), self.hydat(float("nan")),
                      self.hydat().set_axis(pd.DatetimeIndex(["2020-01-02"]))]:
            with self.subTest(reference_type=type(hydat).__name__):
                qa = assess_daily_quality(daily, hydat, policy=self.policy)
                self.assertEqual(qa.iloc[0].daily_qa_status, "HYDAT_MISSING")
                self.assertEqual(qa.iloc[0].missing_hydat_keep_day_flag, 1)
                self.assertEqual(qa.iloc[0].unchecked_suspect_set_missing_day_flag, 0)
                out = apply_daily_qa(self.hourly(), qa, policy=self.policy)
                pd.testing.assert_series_equal(out.value, self.hourly().value)
                self.assertEqual(out.qa_applied.sum(), 0)

    def test_zero_and_near_zero_hydat_mask_suspect_hours_as_unchecked(self):
        for reference in [0.0, 1.0e-12, -1.0e-12]:
            with self.subTest(reference=reference):
                qa = assess_daily_quality(self.daily(), self.hydat(reference), policy=self.policy)
                self.assertEqual(qa.iloc[0].daily_qa_status, "HYDAT_ZERO")
                self.assertEqual(qa.iloc[0].grade_outlier_day_flag, 0)
                self.assertEqual(qa.iloc[0].unchecked_suspect_set_missing_day_flag, 1)
                out = apply_daily_qa(self.hourly(), qa, policy=self.policy)
                self.assertTrue(out.value.iloc[[0, 2]].isna().all())
                self.assertEqual(out.value.iloc[1], 12.0)

    def test_finite_reference_with_insufficient_coverage_masks_suspect_hours(self):
        qa = assess_daily_quality(self.daily(coverage_fraction=0.899999),
                                  self.hydat(), policy=self.policy)
        self.assertEqual(qa.iloc[0].daily_qa_status, "INSUFFICIENT_COVERAGE")
        self.assertEqual(qa.iloc[0].unchecked_suspect_set_missing_day_flag, 1)
        out = apply_daily_qa(self.hourly(), qa, policy=self.policy)
        self.assertEqual(out.qa_applied.tolist(), [1, 0, 1, 0])

    def test_approval_trigger_is_independent_of_grade_duration_threshold(self):
        daily = self.daily(grade10_fraction=0.0, suspect_grade_fraction=0.0,
                           approval_suspect_fraction=1.0 / 86400.0)
        qa = assess_daily_quality(daily, self.hydat(), policy=self.policy)
        self.assertEqual(qa.iloc[0].daily_qa_status, "FAIL")
        self.assertEqual(qa.iloc[0].grade_outlier_day_flag, 0)
        self.assertEqual(qa.iloc[0].approval_level_outlier_day_flag, 1)
        hourly = self.hourly()
        hourly["grades"] = "-1"
        hourly["approval_levels"] = ["pReLiMiNaRy Approved", "Approved", "SuSpEcT", ""]
        out = apply_daily_qa(hourly, qa, policy=self.policy)
        self.assertEqual(out.qa_applied.tolist(), [1, 0, 1, 0])

    def test_approval_can_be_disabled_without_disabling_grade_qa(self):
        policy = replace(self.policy, approval_enabled=False)
        daily = self.daily(grade10_fraction=0.0, suspect_grade_fraction=0.0,
                           approval_suspect_fraction=1.0)
        qa = assess_daily_quality(daily, self.hydat(), policy=policy)
        self.assertEqual(qa.iloc[0].daily_qa_status, "NOT_REQUIRED")
        qa = assess_daily_quality(self.daily(), self.hydat(), policy=policy)
        self.assertEqual(qa.iloc[0].daily_qa_status, "FAIL")

    def test_failed_approval_check_does_not_mask_unrelated_grade_hours(self):
        daily = self.daily(grade10_fraction=0.1, suspect_grade_fraction=0.1,
                           approval_suspect_fraction=0.01)
        qa = assess_daily_quality(daily, self.hydat(), policy=self.policy)
        hourly = self.hourly()
        hourly["approval_levels"] = ["Approved", "Preliminary", "Approved", ""]
        out = apply_daily_qa(hourly, qa, policy=self.policy)
        self.assertEqual(out.qa_applied.tolist(), [0, 1, 0, 0])
        self.assertEqual(out.value.iloc[0], 12.0)
        self.assertEqual(out.value.iloc[2], 12.0)

    def test_failed_grade_and_approval_checks_mask_union_and_preserve_both_reasons(self):
        daily = self.daily(grade10_fraction=0.25, grade20_fraction=0.25,
                           suspect_grade_fraction=0.5, approval_suspect_fraction=0.2,
                           approval_grade_suspect_fraction=0.1)
        hourly = self.hourly()
        hourly["value"] = 12.0
        hourly["coverage_fraction"] = 1.0
        hourly["grades"] = ["10", "-1", "20", "-1"]
        hourly["approval_levels"] = ["Approved", "Preliminary", "Suspect", "Approved"]

        qa = assess_daily_quality(daily, self.hydat(), policy=self.policy)
        self.assertEqual(qa.iloc[0].daily_qa_status, "FAIL")
        self.assertEqual(qa.iloc[0].grade_outlier_day_flag, 1)
        self.assertEqual(qa.iloc[0].approval_level_outlier_day_flag, 1)
        out = apply_daily_qa(hourly, qa, policy=self.policy)
        self.assertEqual(out.qa_applied.tolist(), [1, 1, 1, 0])
        self.assertTrue(out.value.iloc[:3].isna().all())
        self.assertEqual(out.value.iloc[3], 12.0)
        self.assertEqual(out.mask_reason.iloc[0], "GRADE_DAILY_QA_FAIL")
        self.assertEqual(out.mask_reason.iloc[1], "APPROVAL_DAILY_QA_FAIL")
        self.assertEqual(set(out.mask_reason.iloc[2].split(";")),
                         {"GRADE_DAILY_QA_FAIL", "APPROVAL_DAILY_QA_FAIL"})
        self.assertEqual(out.mask_reason.iloc[3], "")

    def test_unchecked_approval_only_days_mask_union_of_suspect_hour_types(self):
        hourly = self.hourly()
        hourly["value"] = 12.0
        hourly["coverage_fraction"] = 1.0
        hourly["grades"] = ["10", "-1", "20", "-1"]
        hourly["approval_levels"] = ["Approved", "Preliminary", "Suspect", "Approved"]
        for coverage, reference, expected_status in [
                (0.5, 10.0, "INSUFFICIENT_COVERAGE"),
                (1.0, 0.0, "HYDAT_ZERO")]:
            with self.subTest(status=expected_status):
                daily = self.daily(coverage_fraction=coverage,
                                   valid_duration_seconds=coverage * 86400.0,
                                   grade10_fraction=0.125, grade20_fraction=0.125,
                                   suspect_grade_fraction=0.25, approval_suspect_fraction=0.2,
                                   approval_grade_suspect_fraction=0.1)
                qa = assess_daily_quality(daily, self.hydat(reference), policy=self.policy)
                self.assertEqual(qa.iloc[0].grade_daily_suspect_flag, 0)
                self.assertEqual(qa.iloc[0].approval_level_daily_suspect_flag, 1)
                self.assertEqual(qa.iloc[0].daily_qa_status, expected_status)
                self.assertEqual(qa.iloc[0].grade_outlier_day_flag, 0)
                self.assertEqual(qa.iloc[0].approval_level_outlier_day_flag, 0)
                self.assertEqual(qa.iloc[0].unchecked_suspect_set_missing_day_flag, 1)
                out = apply_daily_qa(hourly, qa, policy=self.policy)
                # Unchecked handling uses both suspect hour types, even with one trigger.
                self.assertEqual(out.qa_applied.tolist(), [1, 1, 1, 0])
                self.assertTrue(out.value.iloc[:3].isna().all())
                self.assertEqual(out.value.iloc[3], 12.0)
                self.assertTrue(out.mask_reason.iloc[:3].eq("UNCHECKED_" + expected_status).all())

    def test_missing_hydat_keeps_mixed_grade_and_approval_suspect_hours(self):
        daily = self.daily(coverage_fraction=0.5, valid_duration_seconds=43200.0,
                           grade10_fraction=0.25, grade20_fraction=0.25,
                           suspect_grade_fraction=0.5, approval_suspect_fraction=0.2,
                           approval_grade_suspect_fraction=0.1)
        hourly = self.hourly()
        hourly["value"] = 12.0
        hourly["coverage_fraction"] = 1.0
        hourly["grades"] = ["10", "-1", "20", "-1"]
        hourly["approval_levels"] = ["Approved", "Preliminary", "Suspect", "Approved"]
        qa = assess_daily_quality(daily, None, policy=self.policy)
        self.assertEqual(qa.iloc[0].daily_qa_status, "HYDAT_MISSING")
        self.assertEqual(qa.iloc[0].grade_daily_suspect_flag, 1)
        self.assertEqual(qa.iloc[0].approval_level_daily_suspect_flag, 1)
        self.assertEqual(qa.iloc[0].missing_hydat_keep_day_flag, 1)
        self.assertEqual(qa.iloc[0].unchecked_suspect_set_missing_day_flag, 0)
        out = apply_daily_qa(hourly, qa, policy=self.policy)
        pd.testing.assert_series_equal(out.value, hourly.value)
        self.assertTrue(out.daily_qa_status.eq("HYDAT_MISSING").all())
        self.assertEqual(out.qa_applied.sum(), 0)
        self.assertTrue(out.mask_reason.eq("").all())

    def test_negative_stage_reference_uses_absolute_relative_bias(self):
        qa = assess_daily_quality(self.daily(unit_daily_mean=-0.6),
                                  self.hydat(-0.5), policy=self.policy)
        self.assertAlmostEqual(qa.iloc[0].daily_qa_abs_percent_bias, 20.0)
        # Use a larger difference to avoid floating-point ambiguity at the threshold.
        qa = assess_daily_quality(self.daily(unit_daily_mean=-0.625),
                                  self.hydat(-0.5), policy=self.policy)
        self.assertEqual(qa.iloc[0].daily_qa_status, "FAIL")
        self.assertAlmostEqual(qa.iloc[0].daily_qa_abs_percent_bias, 25.0)

    def test_disabled_policy_keeps_hourly_values(self):
        policy = replace(self.policy, enabled=False)
        qa = assess_daily_quality(self.daily(), self.hydat(), policy=policy)
        self.assertEqual(qa.iloc[0].daily_qa_status, "DISABLED")
        out = apply_daily_qa(self.hourly(), qa, policy=policy)
        pd.testing.assert_series_equal(out.value, self.hourly().value)
        self.assertEqual(out.qa_applied.sum(), 0)

    def test_no_daily_data_is_distinct_from_a_pass(self):
        qa = assess_daily_quality(self.daily(unit_daily_mean=float("nan"), coverage_fraction=0.0,
                                             valid_duration_seconds=0.0),
                                  self.hydat(), policy=self.policy)
        self.assertEqual(qa.iloc[0].daily_qa_status, "NO_DATA")

    def test_optional_actions_can_keep_unchecked_or_mask_missing_reference(self):
        keep = replace(self.policy, unchecked_action="keep")
        qa = assess_daily_quality(self.daily(), self.hydat(0.0), policy=keep)
        out = apply_daily_qa(self.hourly(), qa, policy=keep)
        pd.testing.assert_series_equal(out.value, self.hourly().value)
        mask = replace(self.policy, missing_hydat_action="set_missing_suspect")
        qa = assess_daily_quality(self.daily(), None, policy=mask)
        out = apply_daily_qa(self.hourly(), qa, policy=mask)
        self.assertEqual(out.qa_applied.tolist(), [1, 0, 1, 0])

    def test_grade_union_counts_source_duration_once(self):
        times = pd.date_range("2020-01-01", periods=25, freq="h")
        rows = [(time.isoformat(sep=" "), 12.0,
                 10 if hour < 6 else 20 if hour < 12 else -1, "Approved")
                for hour, time in enumerate(times)]
        hourly, daily = aggregate_unit_values([self.unit_file(rows)], "2020-01-01", "2020-01-01")
        day = daily.iloc[0]
        self.assertEqual(day.valid_duration_seconds, 86400.0)
        self.assertAlmostEqual(day.grade10_fraction, 6 / 24)
        self.assertAlmostEqual(day.grade20_fraction, 7 / 24)
        self.assertAlmostEqual(day.suspect_grade_fraction, 12 / 24)
        self.assertGreater(day.grade10_fraction + day.grade20_fraction, day.suspect_grade_fraction)
        qa = assess_daily_quality(daily, self.hydat(), policy=self.policy)
        out = apply_daily_qa(hourly, qa, policy=self.policy)
        self.assertTrue(out.value.iloc[:12].isna().all())
        self.assertTrue(out.value.iloc[12:].eq(12.0).all())
        self.assertEqual(out.qa_applied.sum(), 12)
        self.assertEqual(qa.iloc[0].unit_daily_mean, 12.0)
        self.assertEqual(qa.iloc[0].valid_duration_seconds, 86400.0)

    def test_raw_daily_mean_is_weighted_and_independent_of_hourly_coverage_filter(self):
        path = self.unit_file([("2020-01-01 00:30:00", 2, 10, "Approved"),
                               ("2020-01-01 01:00:00", 4, -1, "Approved"),
                               ("2020-01-01 02:00:00", 8, -1, "Approved")])
        hourly, daily = aggregate_unit_values([path], "2020-01-01", "2020-01-01")
        filtered, filtered_daily = aggregate_unit_values([path], "2020-01-01", "2020-01-01",
                                                        min_coverage=1.0)
        self.assertEqual(hourly.value.iloc[0], 3.0)
        self.assertEqual(hourly.value.iloc[1], 6.0)
        self.assertEqual(daily.iloc[0].unit_daily_mean, 5.0)
        self.assertNotEqual(daily.iloc[0].unit_daily_mean, hourly.value.mean())
        self.assertTrue(math.isnan(filtered.value.iloc[0]))
        self.assertEqual(filtered.value.iloc[1], 6.0)
        pd.testing.assert_frame_equal(daily, filtered_daily)

    def test_cross_midnight_intervals_use_native_dates_and_both_endpoint_grades(self):
        path = self.unit_file([("2019-12-31 23:30:00", 0, 10, "Approved"),
                               ("2020-01-01 00:30:00", 6, 20, "Approved"),
                               ("2020-01-01 01:30:00", 12, -1, "Approved")])
        hourly, daily = aggregate_unit_values([path], "2019-12-31", "2020-01-01")
        first, second = daily.iloc[0], daily.iloc[1]
        self.assertEqual(first.unit_daily_mean, 1.5)
        self.assertEqual(first.valid_duration_seconds, 1800.0)
        self.assertEqual(second.unit_daily_mean, 7.5)
        self.assertEqual(second.valid_duration_seconds, 5400.0)
        self.assertAlmostEqual(second.grade10_fraction, 1 / 3)
        self.assertEqual(second.grade20_fraction, 1.0)
        self.assertEqual(second.suspect_grade_fraction, 1.0)
        self.assertIsNone(hourly.index.tz)
        self.assertTrue(hourly.utc_offset.eq("UTC-05:00").all())
        self.assertEqual(len(hourly), 48)

    def test_daily_integration_drops_unusable_samples_before_reconnecting(self):
        path = self.unit_file([("2020-01-01 10:00:00", 10, 10, "Approved"),
                               ("2020-01-01 10:15:00", "NaN", -1, "Approved"),
                               ("2020-01-01 10:30:00", 999, -2, "Approved"),
                               ("2020-01-01 11:00:00", 20, -1, "Approved")])
        hourly, daily = aggregate_unit_values([path], "2020-01-01", "2020-01-01")
        self.assertEqual(hourly.loc["2020-01-01 10:00:00", "value"], 15.0)
        self.assertEqual(daily.iloc[0].unit_daily_mean, 15.0)
        self.assertEqual(daily.iloc[0].valid_duration_seconds, 3600.0)
        self.assertEqual(daily.iloc[0].suspect_grade_fraction, 1.0)

    def test_exact_duplicate_records_do_not_weight_timestamp_average_twice(self):
        path = self.unit_file([("2020-01-01 00:00:00", 10, 50, "Approved"),
                               ("2020-01-01 00:00:00", 10, 50, "Approved"),
                               ("2020-01-01 00:00:00", 30, 50, "Approved"),
                               ("2020-01-01 01:00:00", 20, 50, "Approved")])
        hourly, daily = aggregate_unit_values([path], "2020-01-01", "2020-01-01")
        self.assertEqual(hourly.value.iloc[0], 20.0)
        self.assertEqual(daily.iloc[0].unit_daily_mean, 20.0)

        # Equal values with different quality metadata are distinct source records.
        path = self.unit_file([("2020-01-01 00:00:00", 10, 10, "Approved"),
                               ("2020-01-01 00:00:00", 10, 20, "Approved"),
                               ("2020-01-01 00:00:00", 30, 50, "Approved"),
                               ("2020-01-01 01:00:00", 20, 50, "Approved")])
        hourly, daily = aggregate_unit_values([path], "2020-01-01", "2020-01-01")
        self.assertAlmostEqual(hourly.value.iloc[0], 55 / 3)
        self.assertAlmostEqual(daily.iloc[0].unit_daily_mean, 55 / 3)
        self.assertEqual(set(hourly.grades.iloc[0].split(";")), {"10", "20", "50"})
        self.assertEqual(daily.iloc[0].suspect_grade_fraction, 1.0)

    def test_daily_integration_excludes_121_minute_gap_before_midnight_split(self):
        path = self.unit_file([("2019-12-31 23:00:00", 100, 10, "Approved"),
                               ("2020-01-01 01:01:00", 2, -1, "Approved"),
                               ("2020-01-01 02:01:00", 4, -1, "Approved")])
        hourly, daily = aggregate_unit_values([path], "2019-12-31", "2020-01-01")
        self.assertEqual(daily.iloc[0].valid_duration_seconds, 0.0)
        self.assertTrue(math.isnan(daily.iloc[0].unit_daily_mean))
        self.assertEqual(daily.iloc[1].unit_daily_mean, 3.0)
        self.assertEqual(daily.iloc[1].valid_duration_seconds, 3600.0)
        self.assertEqual(daily.iloc[1].suspect_grade_fraction, 0.0)
        self.assertTrue(hourly.value.iloc[:25].isna().all())

    def test_fixed_source_clock_has_24_hours_on_daylight_saving_transition(self):
        times = pd.date_range("2020-03-08", periods=25, freq="h")
        path = self.unit_file([(time.isoformat(sep=" "), 2.0, -1, "Approved") for time in times])
        hourly, daily = aggregate_unit_values([path], "2020-03-08", "2020-03-08")
        self.assertEqual(len(hourly), 24)
        self.assertEqual(daily.iloc[0].valid_duration_seconds, 86400.0)
        self.assertEqual(daily.iloc[0].coverage_fraction, 1.0)

    def test_approval_durations_inherit_both_source_endpoints(self):
        path = self.unit_file([("2020-01-01 00:00:00", 2, -1, "Approved"),
                               ("2020-01-01 01:00:00", 2, 20, "Preliminary Approved"),
                               ("2020-01-01 02:00:00", 2, -1, "Approved"),
                               ("2020-01-01 03:00:00", 2, -1, "Approved")])
        hourly, daily = aggregate_unit_values([path], "2020-01-01", "2020-01-01")
        self.assertAlmostEqual(daily.iloc[0].approval_suspect_fraction, 2 / 3)
        self.assertAlmostEqual(daily.iloc[0].approval_grade_suspect_fraction, 2 / 3)
        self.assertIn("Preliminary Approved", hourly.approval_levels.iloc[0])
        self.assertIn("Preliminary Approved", hourly.approval_levels.iloc[1])
        self.assertEqual(hourly.approval_levels.iloc[2], "Approved")

    def test_allow_missing_hydat_returns_regular_missing_dates_but_requires_database(self):
        path = self.root / "HYDAT.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE DLY_FLOWS (STATION_NUMBER TEXT, YEAR INTEGER, MONTH INTEGER)")
        frame = read_daily(path, "02GB001", "2020-01-01", "2020-01-03", allow_missing=True)
        self.assertEqual(len(frame), 3)
        self.assertTrue(frame.value.isna().all())
        self.assertTrue(frame.symbol.eq("").all())
        with self.assertRaises(FileNotFoundError):
            read_daily(self.root / "absent.sqlite3", "02GB001", "2020-01-01", "2020-01-03",
                       allow_missing=True)

    def test_allow_missing_hydat_handles_station_with_all_null_daily_values(self):
        path = self.root / "HYDAT.sqlite3"
        columns = ", ".join(f"FLOW{day} REAL, FLOW_SYMBOL{day} TEXT" for day in range(1, 32))
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE DLY_FLOWS (STATION_NUMBER TEXT, YEAR INTEGER, "
                               f"MONTH INTEGER, {columns})")
            connection.execute("INSERT INTO DLY_FLOWS (STATION_NUMBER,YEAR,MONTH) "
                               "VALUES ('02GB001',2020,1)")
        frame = read_daily(path, "02GB001", "2020-01-01", "2020-01-03", allow_missing=True)
        self.assertTrue(frame.value.isna().all())
        with self.assertRaisesRegex(ValueError, "All daily observations"):
            read_daily(path, "02GB001", "2020-01-01", "2020-01-03")


if __name__ == "__main__":
    unittest.main()
