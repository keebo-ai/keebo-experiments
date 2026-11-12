# Adaptive vs Gen1 vs Gen2

Run the same workloads on Snowflake's Adaptive warehouses, Gen1 standard
warehouses, and Gen2 standard warehouses, size by size, and read the bill back
from Snowflake. The goal is to find where the lines cross: for each kind of
work and each size, where Adaptive stops being faster or cheaper, and how
busy, or how idle, a standard warehouse has to be before the answer flips.

It started from select.dev's
[*Adaptive vs Gen1: where does Snowflake's new warehouse actually save you money?*](https://select.dev/posts/adaptive-vs-gen1-where-does-snowflake-s-new-warehouse-actually-save-you-money)
and repeats its workloads. It adds Gen2, reads per-cell bills instead of
estimating them, and turns each comparison into break-even points you can carry
to your own workload. It doesn't start from an answer. It's built to measure
the intersections, whichever way they fall.

## What it measures

Five workload shapes, after the select.dev article:

| Workload | What it runs | The question |
| --- | --- | --- |
| `single` | TPC-H Q1, once, on a new warehouse | Sparse work: is a 60-second minimum worse than paying per query? |
| `sequential` | The 22 TPC-H queries, back to back | Sustained work: the warehouse is busy the whole time |
| `concurrent` | The 22 TPC-H queries, all at once | A burst: standard warehouses queue or add clusters, Adaptive bursts |
| `dml` | Delete one month of `LINEITEM` (about 1%) from a fresh clone, insert it back | A refresh that touches little data when the table prunes |
| `ctas` | Two `CREATE TABLE AS SELECT`s: a narrow copy of `LINEITEM`, and `LINEITEM` joined to `ORDERS` and `CUSTOMER` | A transform: big scans, big writes |

Each runs on X-Small, Small, and Medium (by default) for three configurations:

- **Gen1** and **Gen2**: standard warehouses of that size, with the generation
  pinned. Snowflake now defaults new warehouses to Gen2, so Gen1 has to be
  asked for.
- **Adaptive**: `MAX_QUERY_PERFORMANCE_LEVEL` set to that size, and
  `QUERY_THROUGHPUT_MULTIPLIER` at its default of 2. A performance ceiling isn't
  the same thing as a size. It's the closest like-for-like there is, and it's
  how Snowflake maps a standard warehouse onto Adaptive when you convert one.

Each combination is a **cell**, and every cell runs on a brand-new warehouse of
its own, which is dropped as soon as the cell finishes. That's what makes the
bill exact: `WAREHOUSE_METERING_HISTORY` for that warehouse is what the cell
cost, cold start included.

For every workload and size, the report compares Adaptive vs Gen1,
Adaptive vs Gen2, and Gen2 vs Gen1:

- **Speed**: wall time from the first query's start to the last one's end.
- **Cost, fresh warehouse**: each one's bill as a brand-new warehouse. This is
  the standard warehouses' best case for idle time (they're dropped the moment
  the work ends) and their worst case for the 60-second minimum.
- **Cost, warm warehouse**: what the work alone costs on a standard warehouse
  that's already running, with no minimum and no idle time. That's its busy
  cluster time at the published rate. Adaptive bills by the query, so for it
  this is just the bill.
- **Break-even busy share** (Adaptive vs standard only): a standard warehouse
  kept running for this workload bills its busy time divided by the share of
  time it's busy. Above `warm(standard) / billed(Adaptive)`, the standard
  warehouse is cheaper. "never" means it's dearer even flat out.
- **Break-even AUTO_SUSPEND** (Adaptive vs standard only): run on its own, a
  standard warehouse bills the work plus `AUTO_SUSPEND` seconds of idle time,
  less whatever of that its 60-second minimum already covered. Above this
  value, Adaptive is cheaper. "any (0s)" means Adaptive is cheaper even with
  no idle time at all.

The last two are worked out from the measurements, not measured directly. The
formulas are in `core/report.py` (`Comparison`), so you can check them or
replace them.

## ⚠️ Before you run it

This runs real queries on your account and costs credits.

- **A default run** (TPC-H SF10, X-Small to Medium, 45 cells) takes about an
  hour. The 30 standard cells' 60-second minimums alone come to 1.37 credits.
  From the billing rules we expect the whole run to land around **2-4
  credits**, but treat that as an estimate until you've run it on your own
  account. `run` asks before it starts.
- **`run` never spends more than `--max-credits`** (10 by default). Before each
  cell, the budget left is split across the cells still to go, and the cell's
  share at its worst-case rate becomes a wall-clock limit. No statement runs
  past it, and a concurrent burst is cancelled when it hits. The worst case
  assumes every warehouse runs flat out on every cluster it's allowed, so it's
  a ceiling, not a forecast. A cell that hits its limit shows `+` (a lower
  bound). What earlier cells didn't use goes to later ones.
- **Adaptive has no published rate**, so the cap assumes it bills no faster
  than a Gen2 warehouse at its performance level, times its throughput
  multiplier for the concurrent workload. Step 3 of the report shows the rate
  it actually billed at, so you can check that assumption.
- **`setup`** costs under 0.05 credits at SF10. It copies `LINEITEM` once, sorted
  by ship date, for the DML workload (about 2 GB of storage at SF10, until
  `cleanup`). It also creates and drops a Gen2 and an Adaptive warehouse to
  check your account has them, which is free.
- **`report`** runs a handful of `ACCOUNT_USAGE` queries on an X-Small: about 0.02 credits.
- **Bigger runs:** `--scale 100` is about 10x the work and the cost. Pass a
  matching `--max-credits` (try 60). `--replicates 3` triples the run.
  `--max-clusters 4` (select.dev's setting) needs `--max-credits 15` or so.

`setup` creates a warehouse, `ADAPTIVE_COMPUTE_WH`, and a transient database,
`ADAPTIVE_COMPUTE_DB`. Each cell's warehouse is named
`ADAPTIVE_COMPUTE_WH_<run id>_<config>_<size>_<workload>_<replicate>`. All of
them carry the comment `Keebo adaptive-compute experiment - safe to drop`, and
the experiment won't touch anything with the same name that doesn't. You can
run any command more than once.

**Nothing is torn down until you run `cleanup`.** Each cell's warehouse is
dropped as soon as the cell finishes, but the home warehouse, the database,
and its `LINEITEM` copy stay, because `report` needs them for the hours
`ACCOUNT_USAGE` takes to catch up. `cleanup` drops them, plus any cell or probe
warehouse a crashed run left behind. It then checks the account for anything
else carrying the experiment's comment and says what's left. Objects from a
`setup` under other `--warehouse` / `--database` names are listed but not
dropped unless you pass `--everything`. If anything is still there at the end,
`cleanup` exits with an error.

## Requirements

- **Enterprise edition** or higher, in a
  [region that has Adaptive warehouses](https://docs.snowflake.com/en/user-guide/warehouses-adaptive).
  Gen2 isn't in every region either. `setup` checks both. Without them, measure
  what you have with `run --config gen1 --config gen2` (or `--config gen1`).
- A role that can create warehouses and databases, like `SYSADMIN`.
- The `SNOWFLAKE_SAMPLE_DATA` share. Most accounts have it.
- For `report`, access to `SNOWFLAKE.ACCOUNT_USAGE` (`ACCOUNTADMIN`, or
  `IMPORTED PRIVILEGES` on `SNOWFLAKE`). Adaptive's per-query cross-check reads
  `QUERY_METERING_HISTORY`, which needs the `USAGE_VIEWER` database role. The
  report works without it and says it skipped the check. `setup` tells you
  what's missing.

## Credentials

The same as every experiment here. See the
[warehouse-sizing README](../warehouse_sizing_benchmark/README.md#credentials).

## Usage

```bash
poetry run keebo-experiments adaptive-compute setup     # home warehouse + database + LINEITEM copy; checks Gen2/Adaptive
poetry run keebo-experiments adaptive-compute run       # every cell; prints runtimes when it's done
poetry run keebo-experiments adaptive-compute report    # bills and crossovers, once ACCOUNT_USAGE catches up
poetry run keebo-experiments adaptive-compute cleanup   # drop everything the experiment created, then check
```

`ACCOUNT_USAGE` lags. Query history can take up to 45 minutes, Adaptive's
per-query bill up to an hour, and warehouse billing up to 3 hours. `report`
says which cells are still missing a bill, so rerun it until they're all in.
It also writes two CSVs to the current directory, for whoever picks up the
analysis: `adaptive-compute-run-<id>.csv` (one row per cell) and
`adaptive-compute-run-<id>-crossovers.csv` (one row per comparison). Both
names are git-ignored.

Some options (`--help` lists them all):

- `run --workload single --workload sequential` runs only those workloads.
  `--size` and `--config` narrow the grid the same way.
- `run --replicates 3` runs the whole grid three times on fresh warehouses, and
  the report uses the median. Use it before you quote a number.
- `run --throughput-multiplier 8` repeats select.dev's higher-burst setting.
  Compare it with a default run to see what the extra burst buys.
- `run --max-clusters 1` makes the standard warehouses single-cluster for the
  concurrent workload. That's the fairest baseline if your real warehouse is
  single-cluster.
- `run --scale 100` reads `TPCH_SF100` (run `setup --scale 100` first if you
  want the DML workload). At SF10 most cells finish inside a minute, so the
  60-second minimum dominates the standard warehouses' bills. SF100 puts more
  weight on sustained throughput.
- `report --run-id ID` reports a specific run. `run` prints the ID.

### What the report looks like

The numbers below are **made up**, to show the shape of the output. They are
not results.

```
--- Step 2. Head to head: the challenger next to the baseline (where the lines cross) ---
  workload    size     challenger vs baseline  speed        cost, fresh warehouse  cost, warm warehouse  std is cheaper when busy >  Adaptive is cheaper when AUTO_SUSPEND >
  single      X-Small  Adaptive vs Gen1        1.4x faster  71% cheaper            40% more              71%                         any (0s)
  single      Medium   Adaptive vs Gen1        1.1x faster  about the same         3.1x as much          32%                         4s
  sequential  X-Small  Adaptive vs Gen1        1.2x faster  12% cheaper            12% cheaper           never                       any (0s)
  sequential  Medium   Adaptive vs Gen1        1.2x slower  18% more               18% more              85%                         38s
  ...

single, Adaptive vs Gen1: faster at X-Small, Small, Medium; cheaper as a fresh warehouse at X-Small, Small, even at Medium; ...
```

Read a row like this: on the made-up `sequential / Medium` row, a Gen1 Medium
that's busy more than 85% of the time it's up beats Adaptive. Run on its own,
Gen1 beats it unless `AUTO_SUSPEND` is above 38 seconds. Snowflake's default
is 600.

## How it's measured

- **Runtime** comes from `QUERY_HISTORY`: the cell's first query's start to its
  last one's end, by the cell's query tag
  (`adcomp:<run id>:<workload>:<size>:<config>:<replicate>`). Only the
  statements that do the work count (`SELECT`, `INSERT`, `DELETE`, and
  `CREATE TABLE AS SELECT`). The session settings, clones, and drops around
  them don't. `run` also prints a client-clock runtime as soon as it finishes.
- **Billed credits** come from `WAREHOUSE_METERING_HISTORY.CREDITS_USED_COMPUTE`
  for the cell's own warehouse. Cloud services credits are left out. They're
  usually waived, and they'd be the same for every configuration anyway.
- **Warm standard cost** is each cluster's busy time (from its first query to
  its last, by `QUERY_HISTORY.CLUSTER_NUMBER`) at the published rate. Gen2 is
  1.35x Gen1 (`common/warehouses.py`).
- **Measurement checks** (Step 3): for standard warehouses, billed credits are
  compared with each cluster's busy time, with its 60-second minimum, at the
  published rate. For Adaptive, the summed `QUERY_METERING_HISTORY` per-query
  credits are compared with the warehouse metering. A ratio far from 1.00 means
  the billing doesn't work the way the break-even formulas assume, so read the
  crossovers with suspicion. Step 1's `cr/hr of query time` is Adaptive's
  effective rate.
- **Fresh state:** the result cache is off. Every standard cell starts on a
  never-used warehouse, so its local cache is cold. The configurations take
  turns going first within each workload and size, so none of them is always
  the one that runs after the account warms up.
- **The data:** Snowflake's TPC-H sample share at the chosen scale, with the
  standard validation parameters. Q2, Q17, and Q20 use the usual decorrelated
  rewrites (same answers). The query text is identical on every warehouse, and
  Step 3 flags a cell whose query hashes don't line up.

### What could tilt the result (read before quoting it)

These all push one way or the other. They're here so whoever takes this on can
weigh them, or design them out:

- **Cache and warm compute.** Standard cells always start cold. Adaptive draws
  on a shared pool and may start warmer. That's how both behave in real life,
  but it favors Adaptive on short workloads.
- **Fresh vs warm cost.** "Fresh" charges the standard warehouses their full
  minimum, which favors Adaptive. "Warm" charges them nothing for starting or
  idling, which favors standard. The truth for any real workload lies between,
  and the two break-even columns say where.
- **Size equivalence.** `MAX_QUERY_PERFORMANCE_LEVEL = MEDIUM` is a ceiling.
  Adaptive may use less on a given query, so "Adaptive Medium vs Gen1 Medium"
  compares a ceiling with a fixed size.
- **The break-even formulas** assume per-second billing and a workload that
  repeats identically. Step 3 tests the first assumption. The second is yours
  to judge.
- **Scale.** SF10 is small. Many cells finish in seconds, where the 60-second
  minimum decides the standard warehouses' cost. select.dev ran SF1000. Use
  `--scale 100` or more to see how the crossovers move as the work grows.
- **The DML table is sorted by ship date**, so the one-month delete and insert
  prune well. That's typical of a fact table loaded by day. An unsorted table
  would make DML much more expensive on every configuration.
- **Concurrency settings.** Standard warehouses get `--max-clusters 2` and
  Adaptive gets throughput multiplier 2. Those are cheap, comparable defaults,
  not equivalent ones. Sweep them.
- **Noise.** One replicate per cell is a first look. The report treats
  differences under 5% as ties. Use `--replicates 3` before publishing.
- **Credits, not dollars.** Everything is in credits. If your contract prices
  Adaptive credits differently from standard ones, convert before you compare.

### How it maps to the select.dev article

| select.dev | Here |
| --- | --- |
| TPC-H SF1000 | `--scale 10` by default, `--scale 1000` available |
| Gen1 vs Adaptive | Gen1 vs Gen2 vs Adaptive |
| Fresh warehouse per configuration | Fresh warehouse per cell (workload x size x config x replicate) |
| Cost attributed at the warehouse level | Per-cell `WAREHOUSE_METERING_HISTORY`, cross-checked with `QUERY_METERING_HISTORY` |
| Gen1 multi-cluster, max 4 | `--max-clusters` (2 by default) |
| QTM 2 and 8 | `--throughput-multiplier` (2 by default) |
| Sequential: 22 queries x 3 | 22 queries x 1, `--replicates` for more |
| Five CTAS shapes | Two (narrow and wide) |
| Single run | `--replicates`, with medians |

## Layout

- `cli.py`: the `click` commands (`setup`, `run`, `report`, `cleanup`).
- `core/queries.py`: the configurations, workloads, TPC-H queries, DDL, and
  reporting SQL.
- `core/plan.py`: the grid of cells, their run order, and the cost cap.
- `core/infra.py`: creates, checks, and drops the experiment's objects.
- `core/run.py`: runs each cell on its own warehouse, within its limit.
- `core/report.py`: the live tables, the `ACCOUNT_USAGE` report, the
  crossovers, and the CSV rows.

Shared helpers live in `common/warehouses.py` (sizes and rates),
`common/dedicated.py` (the objects an experiment owns), and `common/compare.py`
(how two numbers compare, in words).

## Related

- select.dev, [*Adaptive vs Gen1*](https://select.dev/posts/adaptive-vs-gen1-where-does-snowflake-s-new-warehouse-actually-save-you-money)
- Snowflake docs: [Adaptive Compute](https://docs.snowflake.com/en/user-guide/warehouses-adaptive),
  [`QUERY_METERING_HISTORY`](https://docs.snowflake.com/en/sql-reference/account-usage/query_metering_history)
- The [warehouse-sizing benchmark](../warehouse_sizing_benchmark/), for one
  query's sizing curve
- Keebo blog: <https://keebo.ai/blog>
