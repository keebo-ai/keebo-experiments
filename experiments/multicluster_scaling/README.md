# How multi-cluster scaling works

**Cost: close to zero.** This experiment only reads history from
`SNOWFLAKE.ACCOUNT_USAGE`. It never creates a warehouse or runs a workload of its
own. Its two read queries do need a running warehouse, though (see
[Before you run it](#before-you-run-it)).

## What it demonstrates

A [multi-cluster warehouse](https://docs.snowflake.com/en/user-guide/warehouses-multicluster)
adds extra clusters when queries queue and removes them when load drops. This
experiment shows how that auto-scaling actually played out on your own
warehouses: how many clusters ran, how often the *extra* ("marginal") clusters
spun up, how long they lived, and how busy they were while up. It's a window
into the mechanism — not a recommendation.

## The mechanism

For each warehouse it:

1. Pairs `RESUME_CLUSTER`/`SUSPEND_CLUSTER` events from
   `WAREHOUSE_EVENTS_HISTORY` into cluster **lifetimes**, and takes the highest
   cluster number seen as the **peak cluster count**.
2. Counts how many times marginal clusters (cluster number ≥ 2) spun up.
3. Measures each marginal cluster's **occupancy** — the fraction of its lifetime
   with at least one query running on it — from `QUERY_HISTORY.CLUSTER_NUMBER`.
4. Prints the distributions and a plain-language description of the pattern
   (short-lived vs long-running, busy vs idle).

## Before you run it

- **Cost.** The two `ACCOUNT_USAGE` queries run on your role's default warehouse,
  or on the one you name with `--run-warehouse`, and usually finish in seconds.
  If that warehouse is suspended, resuming it bills Snowflake's 60-second
  minimum: about 0.017 credits on an X-Small. If it's already running, the
  queries add nothing noticeable. Pick an X-Small for `--run-warehouse`.
- **Nothing to clean up.** The experiment creates no objects, so there is no
  `cleanup` command.
- **Access.** Your role needs access to the `SNOWFLAKE.ACCOUNT_USAGE` schema.

## How to run

Set your Snowflake connection (see `.env.example`, or pass `--connection NAME`),
then:

```bash
poetry install
poetry run keebo-experiments multicluster-scaling --days 14
# Limit to specific warehouses:
poetry run keebo-experiments multicluster-scaling --warehouse ANALYTICS_WH --warehouse BI_WH
# If your role has no default warehouse, name one to run the read queries on:
poetry run keebo-experiments multicluster-scaling --run-warehouse MY_XS_WH
```

`ACCOUNT_USAGE` views can lag real time by up to ~45 minutes, so very recent activity may not
appear yet.

## What to expect

A per-warehouse table plus a one-line description of the behavior:

```
--- Step 1. How multi-cluster scaling behaved (last 14 days) ---
  warehouse     peak clusters  marginal spin-ups  cluster life p50 (s)  cluster life p90 (s)  occupancy p50  occupancy p90
  ------------  -------------  -----------------  --------------------  --------------------  -------------  -------------
  ANALYTICS_WH  5              214                92                    240                   18%            44%
  BATCH_WH      2              6                  3300                  5400                  81%            95%

What the numbers show:
  ANALYTICS_WH: Marginal clusters are short-lived and spend most of their life idle before Snowflake scales them back in.
  BATCH_WH: Marginal clusters run for long stretches and stay busy while up.
```
