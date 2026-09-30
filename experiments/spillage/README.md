# Spillage demo

Run the **same** workload on an undersized Snowflake warehouse and on a
right-sized one, and watch what disk spill does to runtime and cost — live, on
your own account.

Built as a code-along for the Keebo spillage webinar. It creates a dedicated
`SPILLAGE_DEMO_WH`, runs a spill-forcing sort over
`SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM` on an X-Small, then on a Medium, and
prints a side-by-side comparison the moment it finishes. A hard cost cap keeps
each run to a few credits at most.

## What it demonstrates

When a query needs more memory than its warehouse has, Snowflake **spills**
intermediate data to disk:

1. **Local spill** — to the warehouse nodes' SSD. Much slower than memory.
2. **Remote spill** — once local SSD is full too, to cloud object storage.
   Slower again by a wide margin: this is the performance cliff.

A Medium costs 4× as much per hour as an X-Small. But if spilling makes the
X-Small more than 4× slower, the "cheap" X-Small costs *more* per query, and it
holds up everything queued behind it. Right-sizing removes the spill: the query
finishes much faster and can cost less.

Two scenarios, one command each:

| Scenario | Undersized | Right-sized | Workload (default) | What you should see |
| --- | --- | --- | --- | --- |
| `local` | X-Small | Medium | sort of 480M rows (`--fanout 8`) | X-Small spills to local SSD; Medium doesn't |
| `remote` | X-Small | Medium | sort of 2.4B rows (`--fanout 40`) | X-Small spills past local SSD to remote storage; Medium's spill stays local |

The workload is one `ROW_NUMBER() OVER (ORDER BY ...)` with no `PARTITION BY`,
which forces Snowflake to sort every row in a single window, wrapped in
`COUNT(*)` so only one row comes back. `--fanout N` sorts the 60M-row table N
times over, and it's the one setting that sizes the demo. Both sizes in a
scenario run the same query text, so any difference in runtime or spill comes
from the warehouse size.

## ⚠️ Before you run it

This uses **real compute** on **your** account: X-Small at 1 credit/hour and
Medium at 4.

**Every `run` has a hard cost cap, `--max-credits`, which defaults to 1.5.** The
budget is split evenly between the two sizes and enforced as a warehouse
`STATEMENT_TIMEOUT_IN_SECONDS`. At the default, the X-Small gets 45 minutes and
the Medium about 11 minutes, which is 0.75 credits each. Snowflake cancels
anything that runs longer, so **both scenarios together spend about 3 credits
of compute at most**. The only extra is a few seconds for the stats lookups;
Snowflake's 60-second minimum per resume already falls within the cap. The
warehouse is suspended after each size, even if something fails.

A run that hits the cap isn't an error. It's reported with `+` on its numbers
(for example `2700.0+` seconds and "at least 30.0x faster"), which is a fair
result in itself: the X-Small couldn't finish within the budget. Use
`--max-credits 3` to give it more room, or `--max-credits 0.5` to spend less.

Most `local` runs should finish well under the cap. `remote` is the one likely
to use most of its budget, since remote spill is slow by nature. For the
webinar you may want to show a run you recorded beforehand.

Everything runs on the dedicated `SPILLAGE_DEMO_WH` and touches nothing else.
`cleanup` drops it.

### Calibrate before the webinar

Medium has about 4× the memory and local SSD of an X-Small, so the sort has to
be sized to land between them. The band is wide, but not guaranteed. How much memory and SSD each size has
varies by cloud, region, and warehouse generation, so treat the default fanouts
as starting points and do one rehearsal run per scenario:

1. `spillage run --scenario local`. If the X-Small didn't spill, the run tells
   you to raise `--fanout`. If the Medium spilled too, it tells you to lower it.
   Rerun until the X-Small spills and the Medium doesn't.
2. Do the same with `--scenario remote`, aiming for remote spill on the X-Small
   and none on the Medium.
3. Write down the two fanouts and use them in the webinar, e.g.
   `spillage run --scenario local --fanout 3`.

## Requirements

- The `SNOWFLAKE_SAMPLE_DATA` share mounted (free, read-only; `run` checks for
  it and prints the mount command if it's missing).
- A role that can create a warehouse.
- For `report` only: access to `SNOWFLAKE.ACCOUNT_USAGE`.

## Credentials

Same as every experiment here, and no secrets are passed as flags. Use
`--connection NAME` for an entry in Snowflake's `connections.toml`, or copy
[`.env.example`](../../.env.example) to `.env` and fill it in. Anything missing
is prompted for. See the
[warehouse-sizing README](../warehouse_sizing_benchmark/README.md#credentials)
for details, including MFA/SSO token caching.

## Usage

```bash
# 1. Local spill: X-Small vs Medium. The comparison prints as soon as it finishes.
poetry run keebo-experiments spillage run --scenario local

# 2. Local + remote spill: X-Small vs Medium on a much larger sort.
poetry run keebo-experiments spillage run --scenario remote

# 3. (Optional, after a few minutes) exact billed credits from ACCOUNT_USAGE.
poetry run keebo-experiments spillage report

# 4. Drop the demo warehouse.
poetry run keebo-experiments spillage cleanup
```

`run` prints two tables. The numbers below only illustrate the layout:

```
Cost cap: at most 1.5 credits of compute (X-Small stops after 45 min, Medium stops after 11 min).
...
--- Step 1. Same workload, two warehouse sizes — Local spill ---
  arm          size     credits_per_hr  runtime_s  spill_local_gb  spill_remote_gb  est_credits
  undersized   X-Small  1               512.4      21.60           0.00             0.14233
  right-sized  Medium   4               58.9       0.00            0.00             0.06544

--- Step 2. The verdict (right-sized vs undersized) ---
  metric             undersized  right_sized  change
  runtime (s)        512.4       58.9         8.7x faster
  local spill (GB)   21.60       0.00         eliminated
  remote spill (GB)  0.00        0.00         —
  est. credits       0.14233     0.06544      54% cheaper
```

### How it's measured

- **Runtime** is the client's wall-clock time for the cold run on each size.
- **Spill and partitions** come live from
  `INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION`, which is updated within
  seconds. `ACCOUNT_USAGE` can lag by minutes.
- **Estimated credits** = Snowflake's elapsed time × the size's credits/hour
  ÷ 3600. That is the query's share of warehouse time, without the 60-second
  billing minimum. `report` reads the real billed credits from
  `QUERY_ATTRIBUTION_HISTORY` and `WAREHOUSE_METERING_HISTORY`.
- The result cache is turned off, and the warehouse is suspended between sizes
  so each size starts with a cold cache.

### Useful options

- `--max-credits 1.5` — the hard compute cap for the run, split between the two
  sizes (0.05–20).
- `--fanout N` — sort N × 60M rows (local defaults to 8, remote to 40, max
  100). A higher N means more spill and a longer run, up to the cap.
- `--undersized SMALL --right-sized LARGE` — choose a different pair of sizes.
  The cap still applies, but a bigger size gets fewer minutes for the same
  credits.
- `--table DB.SCHEMA.TABLE` — any table with TPCH `LINEITEM` columns.
- `--runs 3` — repeat each size; the cold first run is the one reported. The
  cap is shared across the runs.
- `report --hours 12` — widen the `ACCOUNT_USAGE` lookback (default 6).
- `--warehouse MY_WH`, `--connection NAME`.

## Layout

- `cli.py` — thin `click` command layer (`run`, `report`, `cleanup`).
- `core/` — the domain logic, with no `click` dependency; each function takes an
  open connection so it stays testable:
  - `core/queries.py` — the workload, the two scenarios, the live-stats query,
    and the `ACCOUNT_USAGE` reconciliation queries.
  - `core/run.py` — create the warehouse, run both sizes, read live stats, and
    drop the warehouse.
  - `core/report.py` — the live A/B table and verdict, plus the reconciliation
    report.

## Related

- Keebo blog: <https://keebo.ai/blog>
- [warehouse-sizing](../warehouse_sizing_benchmark/) — sweeps every size and
  charts the full cost curve that spill produces.
