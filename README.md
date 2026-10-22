# wsc-hydrometric-tools

Simple Python workflows for processing daily HYDAT data, downloading Water Survey
of Canada (WSC) unit-value data, and generating hourly time series.

## Setup

Use Python 3.10 or newer. From this repository:

```bash
python -m pip install -r requirements.txt
```

## Three workflows

Each workflow has editable settings at the top. The default demo uses one station,
**02GB001, Grand River at Brantford**, and processes 2020. Everything runs in serial.
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

3. **Process hourly data.** Run workflow 2 first, then:

   ```bash
   python workflows/3_process_hourly_data.py
   ```

Workflows 1 and 3 save CSV and Raven RVT files in `outputs/`. CSVs retain quality
fields; RVTs contain values only, with `-1.2345` for missing observations. The demo
Raven subbasin ID is `1`; change `BASIN_ID` for your model. Use `DATA_TYPE = "Stage"`
for water level, and keep settings consistent between workflows 2 and 3. RVT stage
exports use `WATER_LEVEL`. Units are `m3/s` for discharge and `m` for stage.

## Processing rules

- Daily values retain HYDAT date labels, missing values, negative values, and
  original data symbols. No time-zone conversion is applied.
- Hourly means use linear interpolation and trapezoidal time integration between
  adjacent retained observations. Intervals of **121 minutes or longer** are
  excluded before splitting at hour boundaries. There is no edge extrapolation.
- Nonfinite unit values and Grade `-2` are dropped before forming intervals.
  Duplicate timestamps use the mean value and the union of quality metadata.
- Hourly bins are `[timestamp, timestamp + 1 hour)`, in the source file's fixed
  local UTC offset, without daylight saving time. The CSV includes `utc_offset`.
- The default `MIN_COVERAGE = 0.0` accepts any positive covered duration. Each
  hourly mean is divided by its covered duration. The CSV includes
  `coverage_fraction`; set `MIN_COVERAGE = 1.0` to require a complete hour.
- Grade, approval and qualifier fields are retained from both contributing
  endpoints. Ice and estimated values remain in the output. RVT files do not
  apply quality weights or remove those values automatically.

This demo provides extraction and aggregation. It omits the original project's
additional daily HYDAT comparison QA, station merging, external data sources,
caches, parallel processing, and summary statistics.

## Structure and checks

```text
library/       daily extraction, download, hourly integration, CSV/RVT export
workflows/     three editable single-station examples
tests/         small offline checks for the core processing rules
```

```bash
python -m unittest discover -s tests -v
```

## License and data source

Code: [MIT License](LICENSE), copyright 2025-2026 Hongren Shen. Adapted from the
author's hydrometric processing scripts.

Data source: Environment and Climate Change Canada, Water Survey of Canada.
Downloaded observations are separate from the code license and remain subject to
the provider's data terms, including the [Open Government Licence - Canada](https://open.canada.ca/en/open-government-licence-canada)
where applicable. Raw data and generated outputs are not committed to this repository.
