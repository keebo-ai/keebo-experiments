# Warehouse-sizing benchmark

Run one fixed query across Snowflake warehouse sizes and see what each size
costs and how much it spills to disk, live, then read the billed credits back
from Snowflake's own history. You get your own sizing curve on your own account.

This is the script behind the Keebo article *"Run the warehouse-sizing
benchmark yourself"*. It's also the code-along for the Keebo spillage webinar:
[compare two sizes](#compare-two-sizes-the-spillage-demo) to show what disk
spill costs.

## What it demonstrates

A bigger warehouse is not always more expensive per query. As the warehouse
grows, runtime keeps falling while credits-per-query bottoms out and then
climbs — the bottom is the sweet spot. The reason is disk **spill**: the query
groups ~600M rows by (order, supplier), almost one group per row, and a small
warehouse runs out of memory for that hash table and pushes it to disk (slow);
larger ones stop. Partitions scanned stays constant, so the difference is
compute and memory, not how much data was read.

## ⚠️ Before you run it

This uses **real compute** on **your** account. The full X-Small to 2X-Large
sweep bills about **1.3 credits** against `TPCH_SF100`.

- **Every `run` has a hard cost cap, `--max-credits`, which defaults to 3.** It
  covers everything the run bills. Snowflake's 60-second minimum for each size
  is reserved first, and the rest is split across the run's queries and
  enforced as the warehouse's `STATEMENT_TIMEOUT_IN_SECONDS`, priced at the
  warehouse's actual generation (Gen2 bills 1.35×). A query that hits the cap
  is reported as a lower bound (`+`), not an error.
- **`setup`** costs about 0.02 credits, just Snowflake's 60-second minimum on
  an X-Small. If it has to generate the table instead (see below), that's
  capped at 30 minutes: **at most 0.5 credits** on Gen1.
- **`report`** runs its `ACCOUNT_USAGE` queries on the benchmark's X-Small:
  about 0.02 credits.

### What it creates, and what it never touches

`setup` creates exactly two objects. Both carry the comment
`Keebo warehouse-sizing benchmark - safe to drop`:

- **Warehouse** `SIZING_BENCHMARK_WH`: X-Small, generation pinned
  (`--generation`, default 1), auto-suspends after 60 seconds.
- **Transient database** `SIZING_BENCHMARK_DB`. It gives the live stats a
  database to run in. Only if your role can't read the sample data, it also
  holds a generated `PUBLIC.LINEITEM` with the same shape: TPC-H's columns and
  `TPCH_SF100`'s 600M rows. Transient means no Time Travel or Fail-safe
  storage charges.

The only thing the benchmark reads from your account is the sample data, a
read-only share. Change the names with `--warehouse` and `--database`, and pass
the same names to every command. `run` and `report` refuse to start unless both
objects exist and carry that comment; they point you back to `setup`. If an
object with one of those names already exists but wasn't created by the
benchmark, every command stops with an error rather than resize, suspend, or
drop it.

Every command is safe to rerun. `setup` reuses what's there. Each `run` gets
its own run id and leaves the warehouse X-Small and suspended afterwards.
`report` only reads. `cleanup` drops what exists and says what wasn't there.

## Requirements

- A role that can **create a warehouse and a database** (for example
  `SYSADMIN`). `setup` prints the role it's using.
- For `report` only: access to `SNOWFLAKE.ACCOUNT_USAGE` (`ACCOUNTADMIN`, or
  `IMPORTED PRIVILEGES` on the `SNOWFLAKE` database). `setup` warns up front if
  the role can't read it.
- **Optional:** the `SNOWFLAKE_SAMPLE_DATA` share, which most accounts have.
  Without it, or without `IMPORTED PRIVILEGES` on it, `setup` generates an
  equivalent table instead. An `ACCOUNTADMIN` can restore the share with
  `CREATE DATABASE SNOWFLAKE_SAMPLE_DATA FROM SHARE SFC_SAMPLES.SAMPLE_DATA`.

Nothing else is needed from the account: no default warehouse or database.

## Credentials

Pick whichever fits — no secrets are passed as flags:

**A named connection** from Snowflake's own `connections.toml` (the file the
Snowflake CLI uses). If you already have one, just point at it:

```bash
poetry run keebo-experiments warehouse-sizing run --connection mydemo
```

**Environment / `.env`.** Copy the repo-root
[`.env.example`](../../.env.example) to `.env` and fill it in — the CLI loads it
automatically:

```bash
cp .env.example .env
# edit .env: SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD (or
# SNOWFLAKE_AUTHENTICATOR=externalbrowser for SSO), and SNOWFLAKE_ROLE.
```

**Prompts.** Anything not supplied by `--connection` or the environment is
prompted for (the password with hidden input), so you can also just run a
command and type the values when asked.

> **MFA / SSO token caching.** If your account uses MFA or external-browser
> SSO, each command opens its own connection and would otherwise re-prompt. The
> project depends on `snowflake-connector-python[secure-local-storage]`, which
> caches the token after the first approval so `setup` → `run` → `report` →
> `cleanup` don't each pop a prompt (the account needs `ALLOW_CLIENT_MFA_CACHING`
> for MFA).
>
> The token is stored in your OS credential store, and `poetry install` pulls in
> the right backend for your platform automatically — no configuration or
> OS-specific setup:
>
> - **Windows** — Windows Credential Manager. Silent; no extra prompt.
> - **Linux (desktop)** — Secret Service (GNOME Keyring / KWallet); may ask once
>   to unlock the keyring.
> - **macOS** — the login Keychain. The first run shows a system dialog asking
>   to authorize the Python binary's access to the cached token — click
>   **Always Allow** so later commands don't re-prompt. (A later Python upgrade
>   can make it ask once more, since the grant is tied to the binary.)
>
> On a headless machine with no credential store, caching is simply skipped and
> you'll be prompted per command — use `--connection`, env vars, key-pair, or
> SSO for unattended runs.

## Usage

```bash
# 1. Create the warehouse and database (Steps 1-3). Safe to rerun.
poetry run keebo-experiments warehouse-sizing setup

# 2. Sweep every size (Steps 4-9). Results print live as it finishes.
poetry run keebo-experiments warehouse-sizing run

# 3. Wait a few minutes for ACCOUNT_USAGE to catch up, then read the latest run
#    (Steps 10-16): the sizing curve, disk spill, and billed credits.
poetry run keebo-experiments warehouse-sizing report

# 4. Drop everything setup created (Step 17).
poetry run keebo-experiments warehouse-sizing cleanup
```

See all options with `--help` (or `-h`) on any command.

`run` ends with a live table for each size: its cold and warm runtimes, local
and remote spill, the cold query's credits, and what the size billed. Then it
prints what the whole run cost and the cheapest size per query.

### Useful options

- `run --size medium --size large` — sweep only a subset of sizes
  (repeatable). With exactly two, `run` adds a side-by-side verdict.
- `run --runs 5` — runs per size (run 1 is cold, later runs warm; default 3).
- `run --table SNOWFLAKE_SAMPLE_DATA.TPCH_SF1000.LINEITEM` — 6B rows for a
  sharper curve at ~10x the cost. `TPCH_SF10` (60M rows) is too small to show
  the effect.
- `run --max-credits 3` — the run's hard cost cap (0.05–50).
- `setup --generation 2` — pin Gen2 instead of Gen1. The cap and the live
  credits use the Gen2 rate. Gen2 isn't available in every cloud region; there,
  Snowflake rejects `setup` and Gen1 (the default) works. The report's
  estimated-credit steps (12 and 14) use Gen1 rates.
- `report --run-id 20260930-120000` — report an earlier run (`run` prints its
  id); the default is the latest. `report --hours 12` widens the lookback.
- `--warehouse MY_WH --database MY_DB` — use different names (on every
  command).
- `--connection NAME` — connect via a `connections.toml` entry instead of env.

## Compare two sizes: the spillage demo

Pick two sizes and one run each, and `run` becomes an A/B: the same query on an
undersized and a right-sized warehouse.

```bash
poetry run keebo-experiments warehouse-sizing setup
poetry run keebo-experiments warehouse-sizing run --size xsmall --size medium --runs 1
poetry run keebo-experiments warehouse-sizing report     # the billed numbers, a few minutes later
poetry run keebo-experiments warehouse-sizing cleanup
```

The output ends with a verdict like this one. The numbers are made up to show
the layout; yours will differ:

```
--- The verdict (Medium vs X-Small) ---
  metric                    X-Small  Medium   change
  runtime, cold (s)         70.0     10.0     7.0x faster
  local spill (GB)          16.00    0.00     eliminated
  remote spill (GB)         0.00     0.00     —
  credits per query         0.01944  0.01111  43% cheaper
  credits billed, this run  0.01944  0.06667  243% pricier

Credits used by this run: about 0.086 (each size bills at least 60 seconds when it resumes). ...
```

The Medium costs 4× as much per hour, but finishes 7× faster with no spill, so
each query costs less. **This one run** still bills the Medium more, because
Snowflake charges at least 60 seconds each time a warehouse resumes and the
Medium needed only 10. On a warehouse that's already running, as with any
real workload, the per-query number is the one that adds up. That minimum is
its own talking point about account costs.

What we saw rehearsing it on a Gen1 account in AWS us-east-1:

- **Local spill is easy to show.** At around 500–600M groups (the default
  `TPCH_SF100` is in that range), an X-Small spills tens of GB to local SSD and
  a Medium spills nothing.
- **Remote spill is hard to reach on an X-Small.** Its local SSD absorbed more
  than 200 GB from 6B groups, in a run of over 15 minutes, without spilling to
  remote storage. With `--table ...TPCH_SF1000.LINEITEM`, expect heavy local
  spill on both sizes rather than remote spill; a Medium then spills too, so
  the contrast is weaker. `run` says so when a comparison misses.

If the undersized size doesn't spill, or the right-sized one spills too, `run`
suggests which way to change `--table` or `--size`.

## How it's measured

- **Runtime** is Snowflake's own elapsed time for each query, from the
  session's query history. The client's timing is only a fallback. Snowflake's
  time is the reliable one: if your laptop sleeps mid-run, the client's clock
  stops but the query doesn't.
- **Spill** comes from `GET_QUERY_OPERATOR_STATS` for each size's cold run,
  within seconds of the query finishing. `ACCOUNT_USAGE` can lag by minutes.
- **`query_credits`** = the cold run's runtime × the size's credits/hour ÷
  3600: the query's share of warehouse time. **`billed_credits`** is what the
  size added to the bill: its runs back to back, with a 60-second floor, since
  each size resumes the warehouse once.
- `report` reads one run's timings, spill, and billed credits from
  `ACCOUNT_USAGE`, filtered to the benchmark warehouse. Every query is tagged
  `wsbench:<run id>:<size>:<attempt>`, so rehearsals don't mix in.
- The result cache is turned off, and the warehouse is suspended between sizes
  so each size starts with a cold cache.

## How it maps to the article

| Article steps | Command   |
| ------------- | --------- |
| 1–3           | `setup`   |
| 4–9           | `run`     |
| 10–16         | `report`  |
| 17            | `cleanup` |

## Layout

- `cli.py` — thin `click` command layer (credentials, connection, output).
- `core/` — the domain logic, with no `click` dependency; each function takes an
  open connection so it stays testable:
  - `core/queries.py` — the SQL and constants: the workload, the fallback
    table, the cost cap, the live stats, and the reporting queries.
  - `core/infra.py` — create, check, and drop the warehouse and database
    (Steps 1-3, 17).
  - `core/sweep.py` — run the query on each size and read live stats
    (Steps 4-9).
  - `core/report.py` — the live table and verdict, and read the run back from
    `ACCOUNT_USAGE` (Steps 10-16).

It reuses the shared `common/warehouses.py` (sizes, credit rates, Gen2) and
`common/dedicated.py` (find, claim, and drop the objects an experiment owns).

## Related

- Keebo blog: <https://keebo.ai/blog>
