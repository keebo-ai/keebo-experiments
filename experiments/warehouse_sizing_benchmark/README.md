# Warehouse-sizing benchmark

Run one fixed query across Snowflake warehouse sizes and see each size's
runtime, disk spill, and cost — live — then read the billed credits back from
Snowflake's own history. It's the script behind the Keebo article *"Run the
warehouse-sizing benchmark yourself"*, and the code-along for the Keebo
spillage webinar ([compare two sizes](#compare-two-sizes-the-spillage-demo)).

## What it demonstrates

A bigger warehouse isn't always more expensive per query. As the warehouse
grows, runtime falls while credits per query bottom out and then climb; the
bottom is the sweet spot. The reason is disk **spill**: the query groups ~600M
rows into almost one group per row, and a small warehouse runs out of memory for
that hash table and pushes it to disk. Partitions scanned stay the same at every
size, so the difference is memory, not data read.

## ⚠️ Before you run it

This uses **real compute** on **your** account. The full sweep (X-Small to
2X-Large, 3 runs each) bills about **1.3 credits**.

- **Every `run` is capped by `--max-credits` (default 3).** The cap covers the
  run's compute credits, Snowflake's 60-second minimums included, and is
  enforced with per-query statement timeouts at the warehouse's actual
  generation. A query that hits it is reported as a lower bound (`+`), not an
  error. Cloud-services credits (normally waived) aren't counted.
- **`setup`** and **`report`** cost about 0.02 credits each. If the sample data
  isn't readable, `setup` generates a table instead: at most 0.5 credits, plus
  storage (roughly 15–25 GB) until `cleanup`.

`setup` creates a warehouse (`SIZING_BENCHMARK_WH`, X-Small, generation pinned)
and a transient database (`SIZING_BENCHMARK_DB`), both commented
`Keebo warehouse-sizing benchmark - safe to drop`. Every command refuses to touch
a same-named object without that comment, and every command is safe to rerun.
`cleanup` drops both.

## Requirements

- A role that can create a warehouse and a database (e.g. `SYSADMIN`).
- For `report`: access to `SNOWFLAKE.ACCOUNT_USAGE` (`ACCOUNTADMIN`, or
  `IMPORTED PRIVILEGES` on `SNOWFLAKE`). `setup` warns if it's missing.
- Optional: the `SNOWFLAKE_SAMPLE_DATA` share, which most accounts have. Without
  it, `setup` generates an equivalent 600M-row table.

## Credentials

No secrets are passed as flags. Use whichever fits:

- **A named connection** from Snowflake's `connections.toml`:
  `--connection mydemo`.
- **`.env`**: copy the repo-root [`.env.example`](../../.env.example) to `.env`
  and fill in `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD` (or
  `SNOWFLAKE_AUTHENTICATOR=externalbrowser`), and `SNOWFLAKE_ROLE`.
- **Prompts** for anything not supplied.

> **MFA / SSO.** Each command opens its own connection. The connector caches
> the token in your OS credential store after the first approval, so later
> commands don't prompt again (for MFA, the account needs
> `ALLOW_CLIENT_MFA_CACHING`). On macOS, click **Always Allow** on the Keychain
> dialog. Without a credential store, you're prompted per command.

## Usage

```bash
poetry run keebo-experiments warehouse-sizing setup     # Steps 1-3: create the objects
poetry run keebo-experiments warehouse-sizing run       # Steps 4-9: sweep; results print live
poetry run keebo-experiments warehouse-sizing report    # Steps 10-16: billed numbers (a few minutes later)
poetry run keebo-experiments warehouse-sizing cleanup   # Step 17: drop the objects
```

`run` ends with each size's runtime, spill, and credits, what the run cost, and
the cheapest size per query. Options (see `--help`):

- `run --size medium --size large` — a subset of sizes (repeatable).
- `run --runs 5` — runs per size (run 1 is cold; default 3).
- `run --table SNOWFLAKE_SAMPLE_DATA.TPCH_SF1000.LINEITEM` — 6B rows, ~10× the cost.
- `run --max-credits 3` — the cost cap.
- `setup --generation 2` — pin Gen2 (1.35× the rate; not in every region).
- `report --run-id ID` — a specific run (`run` prints the id). By default
  `report` reads the latest run `ACCOUNT_USAGE` has caught up with, and says so.
- `--warehouse` / `--database` — different names (pass them to every command).

### Compare two sizes: the spillage demo

```bash
poetry run keebo-experiments warehouse-sizing run --size xsmall --size medium --runs 1
```

With two sizes, `run` adds a verdict. The numbers below are made up to show the
layout:

```
--- The verdict (Medium vs X-Small) ---
  metric                    X-Small  Medium   change
  runtime, cold (s)         70.0     10.0     7.0x faster
  local spill (GB)          16.00    0.00     eliminated
  remote spill (GB)         0.00     0.00     —
  credits per query         0.01944  0.01111  43% cheaper
  credits billed, this run  0.01944  0.06667  243% pricier
```

The Medium costs 4× as much per hour, but finishes 7× faster without spilling,
so each query costs less. This one run still bills it more, because a warehouse
bills at least 60 seconds each time it resumes. On a warehouse that's already
running, the per-query number is the one that adds up.

In rehearsal, an X-Small spilled tens of GB on the default table, and a Medium
spilled a small fraction of that. Remote spill was out of reach: an X-Small's
local SSD absorbed more than 200 GB. If a comparison misses, `run` suggests a
different `--size` or `--table`.

## How it's measured

- **Runtime** is Snowflake's elapsed time for each query (the client's clock is
  a fallback). **Spill** comes from `GET_QUERY_OPERATOR_STATS` for each size's
  cold run, seconds after it finishes.
- **`query_credits`** is the cold run's runtime × the size's rate.
  **`billed_credits`** is the size's runs back to back, with a 60-second floor.
- `report` reads one run from `ACCOUNT_USAGE`, filtered to the benchmark
  warehouse by its query tags (`wsbench:<run id>:<size>:<attempt>`).
- The result cache is off, and the warehouse is suspended between sizes so each
  size starts cold.

## Layout

- `cli.py` — the `click` commands (`setup`, `run`, `report`, `cleanup`).
- `core/queries.py` — the SQL and constants: workload, fallback table, cost cap,
  live stats, report.
- `core/infra.py` — create, check, and drop the objects (Steps 1-3, 17).
- `core/sweep.py` — run each size and read live stats (Steps 4-9).
- `core/report.py` — the live tables and the `ACCOUNT_USAGE` report (Steps 10-16).

Shared helpers: `common/warehouses.py` (sizes, rates) and `common/dedicated.py`
(owned objects).

## Related

- Keebo blog: <https://keebo.ai/blog>
