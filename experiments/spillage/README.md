# Spillage demo

Run the **same** workload on an undersized Snowflake warehouse and on a
right-sized one, and watch what disk spill does to runtime and cost, live, on
your own account.

Built as a code-along for the Keebo spillage webinar. `setup` creates the
demo's own warehouse, database, and data. `run` sorts that data on an X-Small,
then on a Medium, and prints a side-by-side comparison the moment it finishes.
A hard cost cap (1.5 credits per run by default) keeps spending bounded.

## What it demonstrates

When a query needs more memory than its warehouse has, Snowflake **spills**
intermediate data to disk:

1. **Local spill** goes to the warehouse nodes' SSD, which is much slower than
   memory.
2. **Remote spill** starts once the local SSD is full too, and goes to cloud
   object storage. It's slower again by a wide margin: this is the performance
   cliff.

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
which forces Snowflake to sort every row in a single window. It returns only
`MAX(row_rank)`, so one row comes back but the sort can't be skipped.
`--fanout N` sorts the 60M-row table N times over. It's the one setting that
sizes the demo. Both sizes in a scenario run the same query text, so any
difference in runtime or spill comes from the warehouse size.

## ⚠️ Before you run it

This uses **real compute** on **your** account. Gen1 rates are X-Small at 1
credit/hour and Medium at 4; Gen2 bills 1.35× that.

- **`setup`** generates the 60M-row table on an X-Small. It's capped at 15
  minutes, which is **at most 0.25 credits** on Gen1 and usually far less.
- **Every `run` has a hard cost cap, `--max-credits`, which defaults to 1.5.**
  The budget is split evenly between the two sizes and enforced as the
  warehouse's `STATEMENT_TIMEOUT_IN_SECONDS`, using the rate for the
  warehouse's actual generation. At the defaults on Gen1, the X-Small gets 45
  minutes and the Medium about 11, which is 0.75 credits each. Snowflake
  cancels anything that runs longer, so **setup plus both scenarios spend
  about 3.3 credits at most**. The only extras are a few seconds of stats
  lookups and Snowflake's 60-second minimum each time the warehouse resumes.
- **`report`** runs three `ACCOUNT_USAGE` queries on the demo's X-Small. They
  take seconds, but resuming the warehouse bills Snowflake's 60-second minimum,
  about 0.017 credits per `report`.

A run that hits the cap isn't an error. It's reported with `+` on its numbers
(for example `2700.0+` seconds and "at least 30.0x faster"), which is a fair
result in itself: the X-Small couldn't finish within the budget. Pass
`--max-credits 3` to give it more room, or `--max-credits 0.5` to spend less.

Most `local` runs should finish well under the cap. `remote` is the one likely
to use most of its budget, since remote spill is slow by nature. For the
webinar you may want to show a run you recorded beforehand.

### What it creates, and what it never touches

`setup` creates exactly two objects. Both are marked with the comment
`Created by keebo-experiments spillage - safe to drop`:

- **Warehouse** `SPILLAGE_DEMO_WH`: X-Small, generation pinned (`--generation`,
  default 1), auto-suspends after 60 seconds.
- **Transient database** `SPILLAGE_DEMO_DB`, holding the generated
  `PUBLIC.LINEITEM` table. Transient means no Time Travel or Fail-safe storage
  charges.

Change the names with `--warehouse` and `--database`, and pass the same names
to every command. `run` and `report` refuse to start unless both objects, and
the table, exist and carry that comment; they point you back to `setup`. If an
object with one of those names already exists but wasn't created by the demo,
every command stops with an error rather than resize, suspend, or drop it.
`cleanup` drops the demo's objects and reports anything that wasn't there.

### Calibrate before the webinar

Medium has about 4× the memory and local SSD of an X-Small, so the sort has to
be sized to land between them. The band is wide, but how much memory and SSD
each size has varies by cloud and region. Treat the default fanouts as
starting points and do one rehearsal run per scenario:

1. Run `spillage run --scenario local`. If the X-Small didn't spill, the run
   tells you to raise `--fanout`. If the Medium spilled too, it tells you to
   lower it. Rerun until the X-Small spills and the Medium doesn't.
2. Do the same with `--scenario remote`, aiming for remote spill on the
   X-Small and none on the Medium.
3. Write down the two fanouts and pass them in the webinar, e.g.
   `spillage run --scenario local --fanout N`.

## Requirements

- A role that can **create a warehouse and a database** (for example
  `SYSADMIN`). `setup` prints the role it's using.
- For `report` only: access to `SNOWFLAKE.ACCOUNT_USAGE` (`ACCOUNTADMIN`, or
  `IMPORTED PRIVILEGES` on the `SNOWFLAKE` database). `setup` warns up front
  if the role can't read it.

Nothing else is needed from the account: no sample-data share, no default
warehouse or database.

## Credentials

Same as every experiment here, and no secrets are passed as flags. Use
`--connection NAME` for an entry in Snowflake's `connections.toml`, or copy
[`.env.example`](../../.env.example) to `.env` and fill it in. Anything missing
is prompted for. See the
[warehouse-sizing README](../warehouse_sizing_benchmark/README.md#credentials)
for details, including MFA/SSO token caching.

## Usage

```bash
# 1. Create the warehouse, database, and data. Safe to rerun.
poetry run keebo-experiments spillage setup

# 2. Local spill: X-Small vs Medium. The comparison prints as soon as it finishes.
poetry run keebo-experiments spillage run --scenario local

# 3. Local + remote spill: X-Small vs Medium on a much larger sort.
poetry run keebo-experiments spillage run --scenario remote

# 4. (Optional, after a few minutes) runtime, spill, and billed credits from ACCOUNT_USAGE.
poetry run keebo-experiments spillage report

# 5. Drop everything setup created.
poetry run keebo-experiments spillage cleanup
```

`run` prints two tables. The numbers below only illustrate the layout:

```
Cost cap: at most 1.5 credits of Gen1 compute (X-Small stops after 45 min, Medium stops after 11 min).
...
--- Step 1. Same workload, two warehouse sizes — Local spill ---
  side         size     credits_per_hr  runtime_s  spill_local_gb  spill_remote_gb  est_credits
  undersized   X-Small  1               512.4      21.60           0.00             0.14233
  right-sized  Medium   4               58.9       0.00            0.00             0.06544

--- Step 2. The verdict (right-sized vs undersized) ---
  metric             undersized  right-sized  change
  runtime (s)        512.4       58.9         8.7x faster
  local spill (GB)   21.60       0.00         eliminated
  remote spill (GB)  0.00        0.00         —
  est. credits       0.14233     0.06544      54% cheaper
```

### How it's measured

- **Runtime** is Snowflake's own elapsed time for the query, from
  `INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION`. If that isn't available yet,
  the client's timing is used instead.
- **Spill** comes from the same place. It's updated within seconds, while
  `ACCOUNT_USAGE` can lag by minutes.
- **Estimated credits** = runtime × the size's credits/hour ÷ 3600, at the
  warehouse's generation. That's the query's share of warehouse time, without
  the 60-second billing minimum.
- `report` reads the real billed credits from `QUERY_ATTRIBUTION_HISTORY` and
  `WAREHOUSE_METERING_HISTORY`. It lists each run separately, by the run id
  `run` prints, so rehearsals are easy to tell apart.
- The result cache is turned off, and the warehouse is suspended between sizes
  so each size starts with a cold cache.

### Useful options

- `run --max-credits 1.5` — the hard compute cap for the run, split between the
  two sizes (0.05–20).
- `run --fanout N` — sort N × 60M rows (local defaults to 8, remote to 40, max
  100). A higher N means more spill and a longer run, up to the cap.
- `run --undersized SMALL --right-sized LARGE` — choose a different pair of
  sizes. The cap still applies, but a bigger size gets fewer minutes for the
  same credits.
- `setup --generation 2` — pin Gen2 instead of Gen1. The cap and the estimates
  use the Gen2 rate. Gen2 isn't available in every cloud region; there,
  Snowflake rejects `setup` with an error, and Gen1 (the default) works.
- `report --hours 48` — widen the `ACCOUNT_USAGE` lookback (default 24).
- `--warehouse MY_WH --database MY_DB` — use different names (on every
  command).
- `--connection NAME` — connect via a `connections.toml` entry.

## Layout

- `cli.py` — thin `click` command layer (`setup`, `run`, `report`, `cleanup`).
- `core/` — the domain logic, with no `click` dependency; each function takes an
  open connection so it stays testable:
  - `core/queries.py` — the generated table, the workload, the two scenarios,
    the cost cap, the live-stats query, and the `ACCOUNT_USAGE` report queries.
  - `core/infra.py` — create, check, and drop the demo's warehouse and database.
  - `core/run.py` — run both sizes, read live stats, and suggest calibration.
  - `core/report.py` — the live side-by-side table and verdict, plus the
    reconciliation report.

It reuses the shared `common/warehouses.py` (sizes, credit rates, Gen2) and
`common/dedicated.py` (find, claim, and drop the objects an experiment owns).

## Related

- Keebo blog: <https://keebo.ai/blog>
- [warehouse-sizing](../warehouse_sizing_benchmark/) — sweeps every size and
  charts the full cost curve that spill produces.
