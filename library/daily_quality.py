# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hongren Shen
"""Compare source-local daily diagnostics with HYDAT and mask suspect hours."""

import math
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class DailyQualityPolicy:
    enabled: bool = True
    grade_fraction_threshold: float = 0.5
    min_daily_coverage_fraction: float = 0.9
    percent_bias_threshold: float = 20.0
    hydat_zero_tolerance: float = 1.0e-12
    approval_enabled: bool = True
    approval_fraction_threshold: float = 0.0
    unchecked_action: str = "set_missing_suspect"
    missing_hydat_action: str = "keep"

    def __post_init__(self):
        for name in ("grade_fraction_threshold", "min_daily_coverage_fraction",
                     "approval_fraction_threshold"):
            if not 0 <= float(getattr(self, name)) <= 1:
                raise ValueError(f"{name} must be between 0 and 1.")
        if not math.isfinite(self.percent_bias_threshold) or self.percent_bias_threshold <= 0:
            raise ValueError("percent_bias_threshold must be finite and positive.")
        if not math.isfinite(self.hydat_zero_tolerance) or self.hydat_zero_tolerance < 0:
            raise ValueError("hydat_zero_tolerance must be finite and nonnegative.")
        for name in ("unchecked_action", "missing_hydat_action"):
            if getattr(self, name) not in {"keep", "set_missing_suspect"}:
                raise ValueError(f"{name} must be keep or set_missing_suspect.")


def _date_index(index):
    dates = pd.DatetimeIndex(pd.to_datetime(index, errors="coerce"))
    if dates.tz is not None:
        raise ValueError("Daily QA requires naive source-local dates.")
    return dates.normalize()


def _finite(values):
    return pd.to_numeric(values, errors="coerce").astype(float).map(math.isfinite)


def assess_daily_quality(daily, hydat, *, policy=DailyQualityPolicy()):
    """Compare each station-variable day with its matching HYDAT daily reference.

    Grade and Approval triggers are independent and use source-interval duration.
    Both use the same whole-day mean and HYDAT reference. An applicable comparison
    requires a triggered day, adequate coverage, and a finite nonzero reference.
    Missing references remain unverified under the default keep policy.
    """
    out = daily.copy()
    out.index = _date_index(out.index).rename(daily.index.name or "timestamp")
    if out.index.hasnans or out.index.duplicated().any():
        raise ValueError("Daily diagnostics require unique, valid dates.")
    out["unit_daily_mean"] = pd.to_numeric(out["unit_daily_mean"], errors="coerce").astype(float)
    out["hydat_daily_value"] = float("nan")
    out["hydat_daily_symbol"] = ""
    if hydat is not None and not hydat.empty:
        source = hydat.copy()
        source.index = _date_index(source.index)
        source = source.loc[~source.index.isna()]
        if source.index.duplicated().any():
            raise ValueError("HYDAT contains duplicate daily dates.")
        out["hydat_daily_value"] = pd.to_numeric(source["value"], errors="coerce").reindex(out.index)
        if "symbol" in source:
            out["hydat_daily_symbol"] = source["symbol"].reindex(out.index).fillna("")
    out["hydat_daily_value"] = pd.to_numeric(out["hydat_daily_value"], errors="coerce").astype(float)
    finite = _finite(out["unit_daily_mean"])
    coverage_ok = finite & out["coverage_fraction"].ge(policy.min_daily_coverage_fraction)
    grade_trigger = out["suspect_grade_fraction"].gt(0) & out["suspect_grade_fraction"].ge(policy.grade_fraction_threshold)
    approval_trigger = (policy.approval_enabled & out["approval_suspect_fraction"].gt(0) &
                        out["approval_suspect_fraction"].ge(policy.approval_fraction_threshold))
    suspect = grade_trigger | approval_trigger
    hydat_finite = _finite(out["hydat_daily_value"])
    nonzero = out["hydat_daily_value"].abs().gt(policy.hydat_zero_tolerance)
    applicable = policy.enabled & suspect & coverage_ok & hydat_finite & nonzero
    comparable = finite & hydat_finite & nonzero
    bias = pd.Series(float("nan"), index=out.index)
    bias.loc[comparable] = ((out.loc[comparable, "unit_daily_mean"] - out.loc[comparable, "hydat_daily_value"]) /
                            out.loc[comparable, "hydat_daily_value"]).abs() * 100.0
    failed = applicable & bias.ge(policy.percent_bias_threshold)
    status = pd.Series("NOT_REQUIRED", index=out.index)
    status.loc[suspect & ~coverage_ok] = "INSUFFICIENT_COVERAGE"
    # A missing reference takes precedence over low source coverage.
    status.loc[suspect & ~hydat_finite] = "HYDAT_MISSING"
    status.loc[suspect & coverage_ok & hydat_finite & ~nonzero] = "HYDAT_ZERO"
    status.loc[applicable] = "PASS"
    status.loc[failed] = "FAIL"
    status.loc[~finite] = "NO_DATA"
    if not policy.enabled:
        status[:] = "DISABLED"
    out["daily_qa_status"] = status
    out["daily_qa_abs_percent_bias"] = bias
    out["daily_qa_check_applicable"] = applicable.astype(int)
    out["daily_qa_passed"] = status.eq("PASS").astype(int)
    out["daily_coverage_passed"] = coverage_ok.astype(int)
    out["grade_daily_suspect_flag"] = grade_trigger.astype(int)
    out["approval_level_daily_suspect_flag"] = approval_trigger.astype(int)
    out["grade_outlier_day_flag"] = (failed & grade_trigger).astype(int)
    out["approval_level_outlier_day_flag"] = (failed & approval_trigger).astype(int)
    unchecked = pd.Series(policy.missing_hydat_action == "set_missing_suspect", index=out.index)
    unchecked.loc[hydat_finite] = policy.unchecked_action == "set_missing_suspect"
    out["unchecked_suspect_set_missing_day_flag"] = (policy.enabled & unchecked & suspect & ~applicable).astype(int)
    out["missing_hydat_keep_day_flag"] = (policy.enabled & finite & suspect & ~hydat_finite &
                                         (policy.missing_hydat_action == "keep")).astype(int)
    return out


def _has_suspect_grade(text):
    for part in str(text).split(";"):
        try:
            if int(float(part.strip())) in {10, 20}:
                return True
        except (ValueError, OverflowError):
            pass
    return False


def _suspect_approval(text):
    text = str(text).lower()
    return "preliminary" in text or "suspect" in text


def apply_daily_qa(hourly, daily_qa, *, policy=DailyQualityPolicy()):
    """Mask affected suspect hourly bins without deleting or reintegrating sources.

    A failed comparison applies each hourly mask only when its corresponding
    daily trigger was met. Unchecked days use the union of suspect Grade and
    Approval bins. Other aggregated values retain their original values.
    """
    out = hourly.copy()
    out["value_before_daily_qa"] = out["value"]
    dates = _date_index(out.index)
    source = daily_qa.copy()
    source.index = _date_index(source.index)
    if source.index.hasnans or source.index.duplicated().any():
        raise ValueError("Daily QA decisions require unique, valid dates.")

    def mapped(name, default):
        if name not in source:
            return pd.Series(default, index=out.index)
        return pd.Series(source[name].reindex(dates).to_numpy(), index=out.index).fillna(default)

    out["daily_qa_status"] = mapped("daily_qa_status", "NO_DATA")
    out["daily_qa_abs_percent_bias"] = mapped("daily_qa_abs_percent_bias", float("nan"))
    out["hydat_daily_value"] = mapped("hydat_daily_value", float("nan"))
    out["hydat_daily_symbol"] = mapped("hydat_daily_symbol", "")
    suspect_grade = out["grades"].fillna("").map(_has_suspect_grade)
    suspect_approval = out["approval_levels"].fillna("").map(_suspect_approval) & policy.approval_enabled
    # A shared daily failure does not activate the other branch's hourly mask.
    bad_grade = mapped("grade_outlier_day_flag", 0).eq(1) & suspect_grade
    bad_approval = mapped("approval_level_outlier_day_flag", 0).eq(1) & suspect_approval
    # Unchecked days use both suspect categories regardless of which trigger fired.
    unchecked = mapped("unchecked_suspect_set_missing_day_flag", 0).eq(1) & (suspect_grade | suspect_approval)
    modified = (bad_grade | bad_approval | unchecked) & policy.enabled
    reasons = pd.Series("", index=out.index)
    if policy.enabled:
        for mask, reason in ((bad_grade, "GRADE_DAILY_QA_FAIL"),
                             (bad_approval, "APPROVAL_DAILY_QA_FAIL")):
            reasons.loc[mask] = reasons.loc[mask].map(lambda existing: existing + ";" if existing else "") + reason
        prefix = reasons.loc[unchecked].map(lambda existing: existing + ";" if existing else "")
        reasons.loc[unchecked] = prefix + "UNCHECKED_" + out.loc[unchecked, "daily_qa_status"]
    else:
        out["daily_qa_status"] = "DISABLED"
    out.loc[modified, "value"] = float("nan")
    out["qa_applied"] = modified.astype(int)
    out["mask_reason"] = reasons
    return out
