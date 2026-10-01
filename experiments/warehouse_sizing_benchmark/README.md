# Warehouse-sizing benchmark

Run the same query on each Snowflake warehouse size and see how long it takes,
how much it spills, and what it costs. You get the results as soon as the run
finishes, and `report` reads the billed credits back from Snowflake afterward.
This is the code behind the Keebo article *"Run the warehouse-sizing benchmark
yourself"* and the Keebo spillage webinar ([compare two sizes](#compare-two-sizes)).

## What it demonstrates

A bigger warehouse isn't always more expensive per query. As you size up, the
query gets faster and the cost per query drops, until it starts climbing again.
The low point is the size you want.

Most of that comes from spill. The query groups about 600M rows into almost one
group per row. A small warehouse doesn't have the memory to hold that, so
Snowflake writes it to local disk and keeps going, which is slow. A bigger
warehouse keeps it in memory. The data scanned is the same at every size. What
changes is how much memory and CPU the warehouse has, and on the small sizes the
memory runs out.

## ⚠️ Before you run it

This runs real queries on your account and costs credits. A full sweep (X-Small
to 2X-Large, three runs each) costs about 1.3 credits.

- `run` is designed to stay under `--max-credits`, 3 by default, counting
  Snowflake's 60-second minimum for each size. It does that by giving each query
  a time limit based on the warehouse's rate. A query that hits it is cancelled,
  and its numbers show with a `+` to mark them as a lower bound. Cloud services
  credits aren't counted, but they're usually waived.
- `setup` and `report` cost about 0.02 credits each. If your role can't read the
  sample data, `setup` builds its own copy of the table. That costs up to 0.5
  credits, plus storage (15-25 GB) until you run `cleanup`.

`setup` creates a warehouse, `SIZING_BENCHMARK_WH`, and a transient database,
`SIZING_BENCHMARK_DB`. Both get the comment
`Keebo warehouse-sizing benchmark - safe to drop`, and the benchmark won't touch
anything with the same name that doesn't have it. You can run any command more
than once. `cleanup` drops both.

## Requirements

- A role that can create a warehouse and a database, like `SYSADMIN`.
- For `report`, access to `SNOWFLAKE.ACCOUNT_USAGE` (`ACCOUNTADMIN`, or
  `IMPORTED PRIVILEGES` on `SNOWFLAKE`). `setup` tells you if it's missing.
- The `SNOWFLAKE_SAMPLE_DATA` share, if you have it. Most accounts do. If not,
  `setup` builds an equivalent table.

## Credentials

Credentials never go in flags. You can:

- point at a named connection in Snowflake's `connections.toml` with
  `--connection mydemo`,
- copy [`.env.example`](../../.env.example) to `.env` and fill in
  `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD` (or
  `SNOWFLAKE_AUTHENTICATOR=externalbrowser`), and `SNOWFLAKE_ROLE`, or
- run a command and answer the prompts.

If you use MFA or SSO, you approve the first login and the connector caches the
token in your OS credential store, so the next commands don't ask again. MFA
caching needs `ALLOW_CLIENT_MFA_CACHING` on the account. On macOS, click
**Always Allow** when Keychain asks.

## Usage

```bash
poetry run keebo-experiments warehouse-sizing setup     # Steps 1 and 3: create the warehouse and database
poetry run keebo-experiments warehouse-sizing run       # Steps 2-9: run the query on every size
poetry run keebo-experiments warehouse-sizing report    # Steps 10-16: the billed numbers, once ACCOUNT_USAGE catches up
poetry run keebo-experiments warehouse-sizing cleanup   # Step 17: drop what setup created
```

When `run` finishes, it prints each size's runtime, spill, and credits, what the
run cost, and the cheapest size per query. Some options (`--help` lists them
all):

- `run --size medium --size large` runs only those sizes.
- `run --runs 5` changes the number of runs per size. The first run is cold.
- `run --table SNOWFLAKE_SAMPLE_DATA.TPCH_SF1000.LINEITEM` uses 6B rows. It costs
  about 10x as much, so raise `--max-credits` to 15 or the bigger queries stop
  early.
- `run --max-credits 3` sets the cost cap.
- `setup --generation 2` pins Gen2. It costs 1.35x as much per hour and isn't
  available in every region.
- `report --run-id ID` reports a specific run. `run` prints the ID. Without it,
  `report` uses the latest run `ACCOUNT_USAGE` has and tells you which one.
- `--warehouse` and `--database` change the names. Pass them to every command.

### Compare two sizes

To see what spill costs, run the same query on two sizes:

```bash
poetry run keebo-experiments warehouse-sizing run --size xsmall --size medium --runs 1
```

With two sizes, `run` puts them side by side. These numbers are made up:

```
--- X-Small vs Medium ---
  metric                       X-Small  Medium   change
  ---------------------------  -------  -------  ------------
  runtime, cold (s)            80.0     11.0     7.3x faster
  local spill (GB)             18.00    1.20     93% less
  remote spill (GB)            0.00     0.00     none
  credits per query            0.02222  0.01222  45% cheaper
  credits billed for this run  0.02306  0.06667  2.9x as much

This run used about 0.090 credits, counting the 60-second minimum each size bills when it resumes. ...
Cheapest per query: Medium (0.01222 credits).
The Medium ran for 14s but bills the 60-second minimum. On a warehouse that's already running, each query costs 0.01222 credits.
```

The Medium costs four times as much per hour, but it finishes seven times faster
and barely spills, so each query is cheaper. This run still bills the Medium
more, because a warehouse bills at least 60 seconds every time it resumes and
the Medium only needed 14. On a warehouse that's already running, like one
serving a real workload, the cost per query is what matters.

When we tried it, an X-Small spilled tens of GB on the default table and a
Medium spilled a small fraction of that. We couldn't get an X-Small to spill to
remote storage: its local disk took more than 200 GB first. If a comparison
doesn't show much, `run` suggests a different `--size` or `--table`.

## How it's measured

- Runtime is Snowflake's elapsed time for each query. If that isn't available,
  it falls back to the client's clock.
- Spill comes from `GET_QUERY_OPERATOR_STATS` for each size's cold run, a few
  seconds after it finishes.
- Credits per query is the median run's runtime times the size's rate (with one
  run, that's the cold run). It's what the query costs on a warehouse that's
  already running. Credits billed is how long the size was up, from resume to
  suspend, with a 60-second minimum. `report` Steps 12 and 14 work out the same
  two numbers from `ACCOUNT_USAGE`, and Steps 15 and 16 are what Snowflake billed.
- `report` reads one run from `ACCOUNT_USAGE`. It finds the run by its query tag
  (`wsbench:<run id>:<size>:<attempt>`) on the benchmark warehouse.
- The result cache is off. The warehouse suspends before each size, which drops
  its local cache, so each size's first run is cold and later runs are warm.

## Layout

- `cli.py`: the `click` commands (`setup`, `run`, `report`, `cleanup`).
- `core/queries.py`: the SQL and constants.
- `core/infra.py`: creates, checks, and drops the warehouse and database
  (Steps 1, 3, and 17).
- `core/sweep.py`: the cost cap, and runs each size and reads its stats
  (Steps 2-9).
- `core/report.py`: the results tables and the `ACCOUNT_USAGE` report
  (Steps 10-16).

Shared helpers live in `common/warehouses.py` (sizes and rates) and
`common/dedicated.py` (the objects an experiment owns).

## Related

- Keebo blog: <https://keebo.ai/blog>
