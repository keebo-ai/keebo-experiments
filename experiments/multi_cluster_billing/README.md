# Multi-cluster billing test

Snowflake bills warehouses per second with a 60-second minimum. When a multi-cluster warehouse scales out, the docs do not say what that minimum attaches to, and the difference decides whether a cost controller that adds and removes clusters is saving money or spending it.

This experiment runs real warehouses on your account and answers four questions.

## The four questions

**Q1. Does suspending an extra cluster before it has run a full minute save
anything?**

Scale-out is bursty: a cluster comes up for a spike of work and goes away again, often after only a few seconds. If Snowflake charges for the seconds it ran, shutting it down 20 seconds sooner takes 20 seconds off the bill, and shortening bursts is worth doing. If instead the first minute is charged in full whatever happens, a cluster that ran 20 seconds and one that ran 55 cost exactly the same, and there is nothing to gain until a cluster is close to a full minute.

**Q2. Does it matter when an extra cluster starts?**

A warehouse that runs 45 seconds is still charged for a full 60, so the last 15 of those seconds are paid for whether anything uses them or not. If a second cluster started inside that window is covered by the minute already paid, then adding capacity early is free and work should be arranged to use it. If instead each cluster's minute begins when that cluster begins, the clock is the same wherever it lands and the starting moment is not worth tuning.

**Q3. What does an extra cluster that runs longer than a minute cost?**

The first two questions are about clusters that die before their first minute is up. Once a cluster is past that minute there are two plausible ways to charge for the rest: by the second, or by rounding up to the next whole minute. For a cluster that runs 90 seconds that is the difference between paying for 90 seconds and paying for 120 — a third more for the same work — and it decides whether a long-running cluster is worth controlling to the second or only to the minute.

**Q4. Does every extra cluster carry a minute of its own, or do clusters started
together share one?**

Scaling a warehouse from one cluster to five is a single decision, but it starts four clusters at once. If those four share one minimum between them, a wide scale-out is cheap: one minute covers all of it. If each carries its own minute, the same decision costs four minutes, and scaling out by four costs four times as much as scaling out by one. On a warehouse that scales out repeatedly through the day, that is the difference between a rounding error and the largest item on the bill.

## The four ways Snowflake could be charging

Every one of those questions is answered by the same thing: which of four rules the meter follows. Each predicts, from the warehouse's own lifetime `T` and each extra cluster's start offset `Sᵢ` and lifetime `Lᵢ`, the whole number of cluster-seconds the bill should come to.

| rule | what it says | predicted cluster-seconds |
|---|---|---|
| `PER_SECOND` | every cluster is charged for the seconds it ran, with no minimum of its own | `max(60, T) + Σᵢ Lᵢ` |
| `OWN_MINUTE` | every cluster is charged a full minute at the least, and by the second beyond that | `max(60, T) + Σᵢ max(60, Lᵢ)` |
| `WHOLE_MINUTES` | every cluster is charged in whole minutes, rounded up | `max(60, T) + Σᵢ 60·⌈Lᵢ/60⌉` |
| `SHARES_THE_MINUTE` | the warehouse's first minute covers every cluster running inside it, and a cluster is charged only for the part that runs past it | `max(60, T) + Σᵢ max(0, min(Lᵢ, Sᵢ + Lᵢ − 60))` |

The whole total is then rounded up to the next whole second, because that is
what the meter charges in — see the credit rate section.

## The scenarios

Each scenario is a fixed shape of warehouse run, chosen so that the four rules predict numbers far enough apart to tell them apart. `S` is when the extra cluster starts, `T` is when the whole warehouse suspends, so the extra cluster lives `T − S`.

| scenario | clusters | S | T | extra cluster lives | what it is for |
|---|---|---|---|---|---|
| `short` | 1 | — | 45 s | — | does the 60-second minimum exist on this account at all |
| `control` | 1 | — | 90 s | — | does a credit bill decode back to the seconds the warehouse ran |
| `inside` | 2 | 10 s | 45 s | 35 s | a cluster that starts and ends inside the warehouse's first minute — Q2 |
| `brief` | 2 | 70 s | 90 s | 20 s | the ordinary short burst, warehouse already past its own minute — Q1, Q2 |
| `nearly` | 2 | 65 s | 120 s | 55 s | the same, but just under a full minute — Q1 |
| `outlives` | 2 | 60 s | 150 s | 90 s | a cluster that runs past its own minute — Q3 |
| `k5` | 5 | 70 s | 90 s | 20 s each | four clusters started by one scale-out — Q4 |
| `natural` | 2 | — | — | — | Snowflake scales out on its own; cross-check, does not decide |

What each rule predicts the extra clusters add to the bill:

| scenario | `PER_SECOND` | `OWN_MINUTE` | `WHOLE_MINUTES` | `SHARES_THE_MINUTE` |
|---|---|---|---|---|
| `inside` | 35 | 60 | 60 | 0 |
| `brief` | 20 | 60 | 60 | 20 |
| `nearly` | 55 | 60 | 60 | 55 |
| `outlives` | 90 | 90 | 120 | 90 |
| `k5` | 80 | 240 | 240 | 80 |

Every pair of rules is separated by at least one row, which is what makes the
run able to return a single answer rather than a shortlist. Two rows carry a
pair on their own:

- `inside` is the only scenario that separates `SHARES_THE_MINUTE` from
  `PER_SECOND`. Everywhere else the extra clusters come up after the 60-second
  mark, and once the warehouse's own minute is spent there is no minimum left to
  share — "one minimum for the whole warehouse" and "no minimum at all" then
  charge the same thing. Only a cluster that overlaps the first minute can tell
  them apart.
- `outlives` is the only scenario that separates `WHOLE_MINUTES` from
  `OWN_MINUTE`. They differ only past the first minute, and it is the only
  scenario with a cluster that gets there.

The tightest margin is `nearly`, where two rules predict 55 and two predict 60.
Five seconds is small, but the meter charges in whole seconds, so it is five
quanta rather than a rounding question.

### `natural`

One extra warehouse, one replicate, where Snowflake decides to scale out on its
own. It is created with `MAX_CONCURRENCY_LEVEL = 1`, and two identical generated
queries are submitted together on separate cursors of one connection: the first
occupies cluster 1, the second queues, and the STANDARD scaling policy starts
cluster 2 without being told to. There is no fixed cycle — it waits for both
queries and then suspends.

It does not feed the verdict, because its shape is whatever Snowflake chose that
day rather than something the experiment set. It is there to check that a
cluster Snowflake started for itself is billed the same way as one forced up
with `MIN_CLUSTER_COUNT`. If it were not, none of the other answers would carry
over to a warehouse left to scale on its own, and the report says so.

## The cycle

Every measured warehouse runs exactly one cycle:

1. Resume the warehouse. Poll `SHOW WAREHOUSES` once a second until one cluster
   is running.
2. Idle until `S`. Raise `MIN_CLUSTER_COUNT` to the scenario's target and poll
   for the clusters, but do not wait past the end of the cycle.
3. **Suspend at `T`, whatever the clusters did.** Then reset
   `MIN_CLUSTER_COUNT` to 1 — after the suspend, never before, so the extra
   clusters are never drainable while the warehouse is still running.

Both offsets are fixed wall-clock times from the resume, not held for a fixed
time after the extra clusters are sighted. Fixed offsets move that variability 
into the extra clusters' lifetimes instead, where the analysis handles it — 
nothing is assumed about how long a cluster lived, it is measured and fed to 
the rules.

Every warehouse pins the same settings, because the measurement is a warehouse's
lifetime and anything Snowflake decides on its own is a confound.
`AUTO_RESUME = FALSE` stops a stray query from resuming a warehouse mid-cycle.
`AUTO_SUSPEND = 3600` stops Snowflake from suspending during the idle phase.
`ENABLE_QUERY_ACCELERATION = FALSE` keeps serverless compute out of the bill.
`SCALING_POLICY = STANDARD` is the policy a cost controller will actually meet.
`MAX_CLUSTER_COUNT = 5` is the same in every scenario, including the
single-cluster ones, so scenarios differ in one way rather than two.
`GENERATION` is pinned rather than inherited — see the credit rate section.

## Where the numbers come from

**Durations come from `ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY`, not from the
polling clock.** The poller sees a state after the fact, at one-second
granularity, through a `SHOW WAREHOUSES` round trip; the event history is what
Snowflake itself recorded. Warehouse lifetime is measured from the
`WAREHOUSE_CONSISTENT` that completes the resume to the `WAREHOUSE_CONSISTENT`
that completes the suspend — the completion marker, not the `RESUME_WAREHOUSE`
or `SUSPEND_WAREHOUSE` rows, which are requests. Each extra cluster's lifetime,
and how long after the warehouse started it came up, are measured the same way
from its own `RESUME_CLUSTER`/`SUSPEND_CLUSTER` pair.

Rows sharing a timestamp come back from `ACCOUNT_USAGE` in no fixed order, so
the report imposes a total order — in the SQL and again in Python — with
`WAREHOUSE_CONSISTENT` sorting last within a timestamp, because it is the row
that says the transition finished.

The report prints the warehouse's own lifetime, each cluster's lifetime, and
each extra cluster's start offset, separately per replicate. It also prints 
the polled figures alongside, and warns when the two disagree by more than 
5 seconds — that means a missed event or a bad pairing, not a result.

**Bills are decoded at the published credit rate, and `control` checks the
decoding rather than defining it.** 

So the published rate decodes the bills, and two checks stand in front of
everything else. Every decoded bill must land within 0.02 of a whole number of
seconds — if the rate were wrong by any meaningful factor, they would not. And
`control` must be billed its own runtime rounded up. Only then is any
multi-cluster number read:

    metering rows → decode at the published rate → every bill on a whole second
                  → `control` billed the time it ran → read the rest

A bill matches a rule when it is within one second of that rule's prediction.
One second, because that is the quantum the meter charges in: a lifetime read
from an event log lands somewhere inside the second the meter rounded up to, so
an exact match is not something a correct rule can promise.

## Replicates and ordering

Each measured scenario gets **four warehouses, one cycle each** — one metering
row per cycle. Four is enough to show that a bill is repeatable rather than 
a one-off.

The 29 warehouses run sequentially over about an hour, so they are ordered as a
**Latin square**: in block `r`, position `p` runs scenario `(r + p) mod n`. Each
scenario occupies each position within a block exactly once, so anything
drifting across the run window — account load, region behaviour — cannot
correlate with scenario. The obvious loop, which runs all four `control`
replicates back to back, would let it.

## Running it

```bash
cp .env.example .env      # fill in your Snowflake account, user, credential
poetry install

poetry run multi-cluster-billing run
# ... about an hour ...

# Safe to run at any point. A metering row appears only once its hour has
# closed, and ACCOUNT_USAGE can lag up to 3 hours after that, but it is often
# much quicker, so this asks whether the rows are there rather than waiting out
# the worst case.
poetry run multi-cluster-billing report

poetry run multi-cluster-billing cleanup
```

The three commands are separate because cluster lifetimes are the measurement
and are not recoverable afterwards with the precision the verdict needs, while
the bill is not readable until hours later.

`run` drives the warehouses and writes a manifest
(`cluster-billing-run-<timestamp>.json`, in the directory you run it from)
holding every timestamp and every poll it recorded. **The manifest is
checkpointed after every warehouse**, not once at the end: 29 warehouses over
about an hour is a long window to crash in, and `cleanup` can only drop what it
can read. `run` suspends the warehouses but does not drop them. The manifest is
git-ignored — it is a record of your account, and `report` is the only thing
that needs it.

`report` reads that manifest plus `ACCOUNT_USAGE`. It decides on the rows rather
than on the clock: if the metering and event rows for this run's warehouses are
there, you get a verdict, whether or not the documented 3-hour worst case has
elapsed. If they are not, it prints what it has, names the warehouses still
missing rows, and gives the time by which they should have appeared — and once
that time has passed it says so, because waiting longer is then no longer the
explanation. It never blocks. It reads `credits_used_compute` only, because the
ALTERs and the polling generate cloud-services credits that have nothing to do
with cluster lifetime and `CREDITS_USED_CLOUD_SERVICES` lags six hours against
the view's three.

`cleanup` drops exactly the warehouses the manifest names, and nothing else.
Each warehouse name carries the run's timestamp token, so two runs in the same
hour cannot sum into one metering row.

## Requirements

- Enterprise edition or higher. Multi-cluster warehouses are not available on
  Standard, and `run` fails with a clear message if they are rejected.
- A role with `CREATE WAREHOUSE` and `ACCOUNT_USAGE` access (`ACCOUNTADMIN`
  works; so does any role granted `SNOWFLAKE.OBJECT_VIEWER` plus warehouse
  creation). `run` probes the view up front and warns immediately if the role
  cannot read it, rather than letting that surface hours later.

## Cost and duration

About an hour, and 1.5 to 2 credits on an X-Small Generation 2 warehouse — a few
dollars on most contracts. The range is the answer itself: if every cluster
carries its own minute, the run costs more than if clusters are charged by the
second, which is the same effect this experiment exists to measure.

## Reading the result

`report` prints, in order:

1. **The tables.** The run's settings, then one row per replicate with the
   warehouse's lifetime, every cluster's lifetime, when each extra cluster
   started, the bill in seconds, and how much of that bill the extra clusters
   added. Then the raw metering rows and derived lifetimes.
2. **The credit rate check.** The rate used, and how far the worst bill sits
   from a whole second. If that check fails nothing further is read, because
   every scenario's number would be wrong by the same factor.
3. **The verdict**, in plain English: which rule the bills follow, what that
   means for a bill you are trying to reduce, what the bills actually were
   against that rule, and which scenario contradicted each of the other three.
4. **The four questions**, each with why it matters, the measurements that bear
   on it, and its answer in the same plain English.
5. **Each scenario**, with what it ran, why it was worth running, its numbers,
   and what those numbers alone rule out.

Everything is also written to `cluster-billing-run-<timestamp>.txt`, alongside
the manifest it came from and git-ignored for the same reason, so the answer
outlives the terminal scrollback. `--out PATH` puts it somewhere else;
`--no-file` skips it. A report with no verdict yet is written too — between
retries, what is still missing is the thing worth keeping.

The verdict is one of:

- **`PER_SECOND`**, **`OWN_MINUTE`**, **`WHOLE_MINUTES`** or
  **`SHARES_THE_MINUTE`** — that rule predicted every scenario and the other
  three did not.
- **`NO_RULE_FITS`** — nothing predicted the bills. That points at something
  outside all four rather than a partial answer, and the whole-second check on
  the credit rate is where to start looking.
- **`INCONCLUSIVE`** — more than one rule still fits, because the scenario that
  would separate them did not run or did not reach its cluster count. The report
  names which scenario that is.

A failed `short` scenario is reported before anything else: if 45 seconds of
warehouse bills 45 seconds rather than 60, there is no minimum on this account
and none of the four questions mean anything.
