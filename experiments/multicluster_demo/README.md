# What multi-cluster does, and how you benefit

**Cost: spends real credits.** This experiment creates a temporary XSMALL
warehouse and runs real queries on it. The light defaults cost roughly **0.1–0.3
credits**; run `... run --estimate` to see the projected cost before spending,
and the warehouse is dropped automatically when the run finishes.

## What it demonstrates

A [multi-cluster warehouse](https://docs.snowflake.com/en/user-guide/warehouses-multicluster)
adds clusters when queries pile up and removes them when load drops. This
experiment makes that visible by running the **same batch of concurrent
queries twice** on a throwaway warehouse:

- **Round A — single cluster** (`MAX_CLUSTER_COUNT = 1`): more concurrency than
  one cluster admits, so queries **queue**.
- **Round B — multi-cluster** (`MAX_CLUSTER_COUNT = N`): Snowflake **spins up
  clusters** to run them in parallel; the queue drains and the batch finishes
  faster.

Then it shows the difference: clusters used, queue time (p50/p95), and total
wall-clock to clear the batch.

## The mechanism (and why it's honest)

The concurrency is **N genuinely concurrent client sessions** — N real
connections released together on a barrier — never a single query faked into
looking concurrent with `GENERATOR`/`UNNEST`. Only real concurrent sessions
exercise Snowflake's admission control and queuing, which is what triggers
multi-cluster scale-out. Each round tags its queries with a unique `QUERY_TAG`
and reads timings back from `INFORMATION_SCHEMA.QUERY_HISTORY` (near real-time).

## How to run

```bash
poetry install
# See the projected cost first (no spend):
poetry run keebo-experiments multicluster-demo run --estimate
# Run it:
poetry run keebo-experiments multicluster-demo run --concurrency 16 --max-clusters 3
# Safety net if a run was interrupted before teardown:
poetry run keebo-experiments multicluster-demo cleanup
```

Your role needs `CREATE WAREHOUSE`. If a single cluster doesn't queue on your
account (fast/small workload), increase `--concurrency`.

## What to expect

```
--- Step 1. Single vs multi-cluster — 16 concurrent queries ---
  metric                single cluster  multi-cluster (max 3)
  --------------------  --------------  ---------------------
  clusters used         1               3
  queue time p50 (ms)   4200            0
  queue time p95 (ms)   9100            120
  total wall-clock (s)  38.5            13.2
  queries               16              16
  failed                0               0

What multi-cluster did:
  Snowflake ran the same 16 concurrent queries across 3 cluster(s) instead of 1.
  p95 queue time: 9100 ms -> 120 ms (99% lower).
  Total wall-clock to clear the batch: 38.5 s -> 13.2 s (66% faster).
```
