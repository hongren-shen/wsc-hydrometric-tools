# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hongren Shen
"""Demo 3: aggregate unit values and screen suspect hours against HYDAT daily data."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from library.daily import read_daily
from library.daily_quality import DailyQualityPolicy, apply_daily_qa, assess_daily_quality
from library.hourly import aggregate_unit_values
from library.export import write_outputs

# Keep station, data type and quality consistent with workflow 2.
STATION = "02GB001"
DATA_TYPE = "Discharge"  # Discharge or Stage
QUALITY = "corrected"
START = "2020-01-01"
END = "2020-12-31"  # Inclusive date
MIN_COVERAGE = 0.0  # Keep any hour with positive coverage, matching the source workflow.
# HYDAT is read-only. Missing dates remain unverified; a wrong database path raises an error.
HYDAT_DB = ROOT / "data" / "Hydat.sqlite3"
APPLY_DAILY_QA = True
# Grade and Approval triggers share one daily comparison but apply separate failure masks.
GRADE_FRACTION_THRESHOLD = 0.50  # Combined Grade 10/20 source-interval duration fraction.
MIN_DAILY_COVERAGE = 0.90  # Independent of the hourly MIN_COVERAGE setting.
DAILY_PERCENT_BIAS_THRESHOLD = 20.0  # Absolute relative difference from HYDAT, in percent.
CHECK_SUSPECT_APPROVAL = True  # Any contributing Preliminary/Suspect interval triggers QA.
UNCHECKED_ACTION = "set_missing_suspect"  # Finite HYDAT but insufficient coverage or near zero.
MISSING_HYDAT_ACTION = "keep"  # Keep unverified hours when the HYDAT reference is absent.
BASIN_ID = 1  # Example Raven subbasin ID; replace for your model.
INPUT_DIR = ROOT / "data" / "unit_values" / DATA_TYPE / QUALITY
OUTPUT = ROOT / "outputs" / f"{STATION}_hourly_{DATA_TYPE.lower()}"


def main():
    files = sorted(INPUT_DIR.glob(f"{DATA_TYPE}.Working@{STATION}.*_{QUALITY}.csv.xz"))
    if not files:
        raise FileNotFoundError("No input files found. Run workflow 2 first.")
    policy = DailyQualityPolicy(
        enabled=APPLY_DAILY_QA,
        grade_fraction_threshold=GRADE_FRACTION_THRESHOLD,
        min_daily_coverage_fraction=MIN_DAILY_COVERAGE,
        percent_bias_threshold=DAILY_PERCENT_BIAS_THRESHOLD,
        approval_enabled=CHECK_SUSPECT_APPROVAL,
        unchecked_action=UNCHECKED_ACTION,
        missing_hydat_action=MISSING_HYDAT_ACTION,
    )
    if APPLY_DAILY_QA and not Path(HYDAT_DB).is_file():
        raise FileNotFoundError(
            f"Daily QA requires a HYDAT SQLite database at {HYDAT_DB}. "
            "Set HYDAT_DB to an extracted database, or set APPLY_DAILY_QA = False "
            "to export hourly values without daily screening."
        )
    hydat = (read_daily(HYDAT_DB, STATION, START, END, DATA_TYPE, allow_missing=True)
             if APPLY_DAILY_QA else None)
    hourly, daily = aggregate_unit_values(files, START, END, MIN_COVERAGE)
    if hourly["value"].notna().sum() == 0:
        raise ValueError("No hourly values meet the gap and coverage rules in this window.")
    daily_qa = assess_daily_quality(daily, hydat, policy=policy)
    screened = apply_daily_qa(hourly, daily_qa, policy=policy)
    write_outputs(screened, OUTPUT, STATION, DATA_TYPE, 1 / 24, BASIN_ID)

    daily_qa.insert(0, "station", STATION)
    daily_qa.insert(1, "data_type", DATA_TYPE)
    daily_qa.insert(2, "utc_offset", hourly.attrs["utc_offset"])
    daily_qa["hydat_database"] = str(Path(HYDAT_DB).resolve()) if APPLY_DAILY_QA else ""
    daily_qa["qa_enabled"] = APPLY_DAILY_QA
    daily_qa["grade_fraction_threshold"] = GRADE_FRACTION_THRESHOLD
    daily_qa["min_daily_coverage_fraction"] = MIN_DAILY_COVERAGE
    daily_qa["percent_bias_threshold"] = DAILY_PERCENT_BIAS_THRESHOLD
    daily_qa["approval_qa_enabled"] = CHECK_SUSPECT_APPROVAL
    daily_qa["approval_fraction_threshold"] = policy.approval_fraction_threshold
    daily_qa["hydat_zero_tolerance"] = policy.hydat_zero_tolerance
    daily_qa["unchecked_action"] = UNCHECKED_ACTION
    daily_qa["missing_hydat_action"] = MISSING_HYDAT_ACTION
    qa_path = OUTPUT.with_name(OUTPUT.name + "_daily_qa").with_suffix(".csv")
    daily_qa.to_csv(qa_path, float_format="%.10g", na_rep="")
    newly_missing = screened["value_before_daily_qa"].notna() & screened["value"].isna()
    print(f"Daily QA set {int(newly_missing.sum())} hourly values to missing.")
    print(f"Daily QA status counts: {daily_qa['daily_qa_status'].value_counts().to_dict()}")
    print(f"Saved daily QA diagnostics: {qa_path}")


if __name__ == "__main__":
    main()
