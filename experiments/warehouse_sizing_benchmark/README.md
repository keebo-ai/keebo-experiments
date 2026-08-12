# Warehouse-sizing benchmark

Run one fixed query across every Snowflake warehouse size and read the timings
and credits back from Snowflake's own history — so you get your own sizing curve
on your own edition, region, and warehouse generation.

This is the script behind the Keebo article *"Run the warehouse-sizing
benchmark yourself"*. It creates a dedicated `SIZING_BENCHMARK_WH`, sweeps a
fixed 600M-row aggregation over `SNOWFLAKE_SAMPLE_DATA.TPCH_SF100.LINEITEM` from
X-Small to 2X-Large, then reports the curve, disk spill, and billed credits
straight from `SNOWFLAKE.ACCOUNT_USAGE`.

## What it demonstrates

A bigger warehouse is not always more expensive per query. As the warehouse
grows, runtime keeps falling while credits-per-query bottoms out and then
climbs — the bottom is the sweet spot. The reason is disk **spill**: small
warehouses run out of memory and push data to disk (slow); larger ones stop.
Partitions scanned stays constant, so the difference is compute and memory, not
how much data was read.

## ⚠️ Before you run it

This uses **real compute** on **your** account. The full X-Small to 2X-Large
sweep bills about **1.3 credits** against TPCH_SF100. The report reads from
`SNOWFLAKE.ACCOUNT_USAGE`, which needs `ACCOUNTADMIN` or granted access and lags
a few minutes (up to ~45); `QUERY_ATTRIBUTION_HISTORY` can trail several hours.
Everything runs on a dedicated `SIZING_BENCHMARK_WH` and touches nothing else.

## Requirements

- The `SNOWFLAKE_SAMPLE_DATA` share mounted (free, read-only; `run` checks for
  it and prints the mount command if it's missing).
- A role with access to `SNOWFLAKE.ACCOUNT_USAGE` for the report.

## Credentials

Credentials are read from the environment (never passed as flags). Copy the
repo-root [`.env.example`](../../.env.example) to `.env` and fill it in — the CLI
loads it automatically:

```bash
cp .env.example .env
# edit .env: SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD (or
# SNOWFLAKE_AUTHENTICATOR=externalbrowser for SSO), and SNOWFLAKE_ROLE.
```

## Usage

After `poetry install`, the experiment is available as a console script:

```bash
# 1. Create the warehouse and sweep every size (Steps 1-9).
poetry run warehouse-sizing-benchmark run

# 2. Wait a few minutes for ACCOUNT_USAGE to catch up, then read results
#    (Steps 10-16): the sizing curve, disk spill, and billed credits.
poetry run warehouse-sizing-benchmark report

# 3. Drop the benchmark warehouse when you're done (Step 17).
poetry run warehouse-sizing-benchmark cleanup
```

See all options with `--help` (or `-h`) on any command.

### Useful options

- `run --table SNOWFLAKE_SAMPLE_DATA.TPCH_SF1000.LINEITEM` — 6B rows for a
  sharper curve at ~10x the cost. `TPCH_SF10` (60M rows) is too small to show
  the effect.
- `run --size medium --size large` — sweep only a subset of sizes (repeatable).
- `run --runs 5` — runs per size (run 1 is cold, later runs warm; default 3).
- `report --hours 12` — widen the `ACCOUNT_USAGE` lookback window (default 6).
- `--warehouse MY_WH` — use a different benchmark warehouse name.

## How it maps to the article

| Article steps | Command   |
| ------------- | --------- |
| 1–9           | `run`     |
| 10–16         | `report`  |
| 17            | `cleanup` |

## Layout

- `cli.py` — thin `click` command layer (credentials, connection, output).
- `core/` — the domain logic, with no `click` dependency; each function takes an
  open connection so it stays testable:
  - `core/queries.py` — the SQL and constants (the workload + the reporting queries).
  - `core/sweep.py` — create, sweep, and drop the benchmark warehouse (Steps 1-9, 17).
  - `core/report.py` — read timings and credits back from `ACCOUNT_USAGE` (Steps 10-16).

## Related

- Keebo blog: <https://keebo.ai/blog>
