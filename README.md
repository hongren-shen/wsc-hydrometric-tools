# wsc-hydrometric-tools

Simple Python workflows for processing daily HYDAT data, downloading Water Survey
of Canada (WSC) unit-value data, and generating hourly time series with daily
quality checks against HYDAT.

## Setup

Use Python 3.10 or newer. The workflows and offline tests were checked with
Python 3.11.14 and pandas 2.3.3. The supported pandas range is listed in
`requirements.txt`. From this repository:

```bash
python -m pip install -r requirements.txt
```

## Three workflows

Each workflow has editable settings at the top. The default demo uses one station,
**02GB001, Grand River at Brantford**. Workflows 1 and 3 process 2020; workflow 2
downloads all available file segments for that station. Everything runs in serial.
Paths are relative to the repository, regardless of where the script is launched.

1. **Process daily HYDAT data.** Download and extract a [HYDAT SQLite database](https://www.canada.ca/en/environment-climate-change/services/water-overview/quantity/monitoring/survey/data-products-services/national-archive-hydat.html).
   Set `HYDAT_DB` to its path in workflow 1 (default: `data/Hydat.sqlite3`).
   The database is opened read-only. Microsoft Access files are not supported.

   ```bash
   python workflows/1_process_daily_hydat.py
   ```

2. **Download unit-value data.** Downloads available compressed file segments for
   one station from the [ECCC unit-value archive](https://collaboration.cmc.ec.gc.ca/cmc/hydrometrics/www/UnitValueData/).
   Files stay compressed in `data/unit_values/Discharge/corrected/`.
   Existing files are checked and skipped unless `OVERWRITE = True`.

   ```bash
   python workflows/2_download_unit_value_data.py
   ```

3. **Process and screen hourly data.** Run workflow 2 first. Set `HYDAT_DB` in
   workflow 3 to the same extracted database used by workflow 1, then:

   ```bash
   python workflows/3_process_hourly_data.py
   ```

   Daily QA is enabled by default. A missing database file raises an error;
   set `APPLY_DAILY_QA = False` to aggregate without HYDAT screening. Missing
   observations within an existing database follow the policy below.

Workflows 1 and 3 save CSV and Raven RVT files in `outputs/`. CSVs retain quality
fields; RVTs contain values only, with `-1.2345` for missing observations. The demo
Raven subbasin ID is `1`; change `BASIN_ID` for your model. Use `DATA_TYPE = "Stage"`
for water level, and keep settings consistent between workflows 2 and 3. RVT stage
exports use `WATER_LEVEL`. Units are `m3/s` for discharge and `m` for stage.

## Processing rules

Hourly series are calculated from WSC unit values using the aggregation rules
below. The interpolation, gap limit, and coverage threshold determine which
observations contribute to each hourly mean.

- Daily values retain HYDAT date labels, missing values, negative values, and
  original data symbols. No time-zone conversion is applied.
- Hourly means use linear interpolation and trapezoidal time integration between
  adjacent retained observations. Intervals of **121 minutes or longer** are
  excluded before splitting at hour boundaries. There is no edge extrapolation.
- Nonfinite unit values and Grade `-2` are dropped before forming intervals.
  Exact duplicate records are removed. Remaining records at the same timestamp
  use the mean value and the union of quality metadata.
- Grade `10` and `20` values remain available during integration. Daily QA then
  determines whether affected hourly bins should be set to missing.
- Hourly bins are `[timestamp, timestamp + 1 hour)`, in the source file's fixed
  local UTC offset, without daylight saving time. The CSV includes `utc_offset`.
- The default `MIN_COVERAGE = 0.0` accepts any positive covered duration. Each
  hourly mean is divided by its covered duration. The CSV includes
  `coverage_fraction`; set `MIN_COVERAGE = 1.0` to require a complete hour.
- Grade, approval and qualifier fields are retained from both contributing
  endpoints. Ice and estimated values that pass daily QA remain in the output.
  RVT files do not apply observation quality weights.

## Daily quality checks against HYDAT

Workflow 3 follows the daily QA rules in the OWL-Hydro hydrometric processing
workflow. These thresholds are project choices. They are configurable at the
top of the script.

Daily means are integrated directly from the original unit-value intervals on
the source file's native local dates. They are not averages of exported hourly
means. Daily diagnostics use the same source exclusions, gap limit, linear
interpolation, and endpoint metadata as hourly integration, before applying
the hourly `MIN_COVERAGE` threshold.

| Setting | Default behavior |
| --- | --- |
| Grade trigger | Grade `10` and/or `20` affects at least 50% of the day's valid interval duration |
| Approval trigger | Any positive interval duration with approval text containing `preliminary` or `suspect`, matched without case sensitivity |
| Daily coverage | At least 90% of the day, equivalent to 21.6 hours |
| HYDAT comparison | Same station, variable, and native local calendar date |
| Failure threshold | Absolute relative difference from HYDAT is at least 20%, including exactly 20% |
| Failure action | Apply the Grade and Approval masks independently, as detailed below |

Both triggers use the same mean integrated over all valid source intervals for
that day, compared with the same HYDAT daily value. There is no separate mean
for Grade-affected or Approval-affected intervals. The comparison uses unrounded
values:

```text
absolute_percent_bias = 100 * abs((unit_daily_mean - hydat_daily_value) / hydat_daily_value)
```

Grade fractions are based on source-interval durations, not sample counts or
hourly metadata. For linear interpolation, either endpoint can flag an
interval. The combined Grade 10/20 fraction counts overlapping flags once.
These flags describe the source quality metadata associated with integration
intervals; they do not measure the physical duration of ice conditions.

For an applicable comparison that fails the 20% threshold, the daily triggers
determine which hourly masks apply:

| Daily trigger met | Hourly bins set to missing after a failed comparison |
| --- | --- |
| Grade only | Bins involving Grade `10` or `20` intervals |
| Approval only | Bins involving `preliminary` or `suspect` Approval Level intervals, regardless of Grade |
| Both | The union of the two sets of bins |

For example, if Grade 10/20 affects 10% of valid duration and suspect Approval
affects some intervals, only the Approval trigger is met. If the daily comparison
fails, a Grade 10 bin with only Approved metadata is retained, while an otherwise
unflagged Grade bin with Preliminary metadata is set to missing. A daily failure
does not automatically remove every Grade 10/20 bin or every hour of that day.

Other aggregated hourly values retain their values. The source points are not
deleted and reconnected after QA, so rejected hours are not filled by
interpolation across the rejected data.

- Missing HYDAT values, dates, or station records retain finite hourly values
  under `MISSING_HYDAT_ACTION = "keep"`. Applicable suspect days are marked
  `HYDAT_MISSING`, not `PASS`.
- When a suspect day has a finite HYDAT reference but insufficient coverage,
  or a reference with absolute value at most `1e-12`, the comparison is unchecked.
  Under `UNCHECKED_ACTION = "set_missing_suspect"`, bins involving either Grade
  `10`/`20` or suspect Approval metadata are set to missing, even if only one
  daily trigger was met. The missing-HYDAT `keep` policy above takes precedence
  when no finite reference exists.
- `NOT_REQUIRED`, `NO_DATA`, `INSUFFICIENT_COVERAGE`, `HYDAT_ZERO`, `PASS`,
  `FAIL`, and `DISABLED` remain distinct diagnostic states.

Workflow 3 writes the screened hourly CSV/RVT pair and an additional
`outputs/02GB001_hourly_discharge_daily_qa.csv` for the default example. The
daily report contains the integrated mean, coverage, Grade/approval duration
fractions, HYDAT reference and symbol, source UTC offset, database path,
comparison result, and policy settings.
The hourly CSV preserves `value_before_daily_qa`, the daily comparison status,
and the masking decision and reason. Rerunning the same output path replaces
these generated files.

This demo omits the original project's Raven observation-weight exports,
station merging, external data sources, caches, parallel processing, and
summary statistics.

## Structure and checks

```text
library/       daily extraction, download, time integration, daily QA, CSV/RVT export
workflows/     three editable single-station examples
tests/         small offline checks for the core processing rules
```

```bash
python -m unittest discover -s tests -v
```

## Citation

Software citation metadata for version 1.0.0 is provided in
[CITATION.cff](CITATION.cff). For an archived code snapshot, cite its version DOI.
When using GitHub code, also record the exact commit used. The repository
citation is:

Shen, H. (2026). *WSC hydrometric tools* (Version 1.0.0) [Computer software].
[GitHub](https://github.com/hongren-shen/wsc-hydrometric-tools).

Please also acknowledge Environment and Climate Change Canada, Water Survey of
Canada, for the observations. Data citation examples and quality definitions
are available in the [WSC FAQ](https://wateroffice.ec.gc.ca/contactus/faq_e.html).
Record the HYDAT database version or unit-value download date when reporting
results, because source observations can be updated.

### Related OWL-Hydro dataset

Shen, H., Tolson, B., Han, M., Craig, J., Metcalfe, R. (2026).
*OWL-Hydro v1.0: An Integrated, Model-Ready Dataset for Semi-Distributed
Hydrologic Modelling in Ontario, Canada*. Federated Research Data Repository.
[https://doi.org/10.20383/103.01878](https://doi.org/10.20383/103.01878)

As of 23 October 2026, the OWL-Hydro deposit is undergoing repository review
at FRDR and public release is pending. Cite OWL-Hydro when using its data.

## License and data source

Code: [MIT License](LICENSE), copyright 2025-2026 Hongren Shen. Adapted from the
author's hydrometric processing scripts.

Data source: Environment and Climate Change Canada, Water Survey of Canada.
Downloaded observations are separate from the code license and remain subject to
the provider's data terms, including the [Open Government Licence - Canada](https://open.canada.ca/en/open-government-licence-canada)
where applicable. Raw data and generated outputs are not committed to this repository.
