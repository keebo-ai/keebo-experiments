"""The SQL, workloads, and constants behind the Adaptive vs Gen1 vs Gen2 experiment.

Kept apart from the orchestration so the parts you'd tweak to change the
experiment (the warehouse configurations, the workloads, the TPC-H queries, the
reporting SQL) read as data in one place. No database or CLI dependencies here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from common.sql import validate_name
from experiments.adaptive_compute.core import monster

DEFAULT_WAREHOUSE = "ADAPTIVE_COMPUTE_WH"
DEFAULT_DATABASE = "ADAPTIVE_COMPUTE_DB"

# Marks every warehouse and the database as the experiment's own. Nothing is
# dropped without it, and a same-named object without it is never touched.
OWNER_COMMENT = "Keebo adaptive-compute experiment - safe to drop"

# --------------------------------------------------------------------------- #
# The three warehouse configurations under test
#
# Gen1 and Gen2 are standard warehouses of a fixed size. Adaptive has no size:
# the same keyword sets MAX_QUERY_PERFORMANCE_LEVEL, its per-query ceiling. That
# is the closest like-for-like there is, and it is how Snowflake itself maps a
# standard warehouse onto Adaptive when you convert one.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Config:
    """One warehouse configuration: how it's named, created, and billed."""

    key: str  # CLI value and query-tag field
    label: str
    code: str  # in warehouse and table names
    generation: str | None  # '1' / '2' for a standard warehouse, None for Adaptive

    @property
    def adaptive(self) -> bool:
        return self.generation is None


GEN1 = Config("gen1", "Gen1", "G1", "1")
GEN2 = Config("gen2", "Gen2", "G2", "2")
ADAPTIVE = Config("adaptive", "Adaptive", "AD", None)
CONFIGS = (GEN1, GEN2, ADAPTIVE)
CONFIGS_BY_KEY = {config.key: config for config in CONFIGS}

DEFAULT_SIZES = ("XSMALL", "SMALL", "MEDIUM")

# Adaptive's burst dial. 2 is Snowflake's default; 0 means unlimited.
DEFAULT_THROUGHPUT_MULTIPLIER = 2
# The standard warehouses' cluster cap for the concurrent workload. Every other
# workload runs one query at a time, so it never needs a second cluster.
DEFAULT_MAX_CLUSTERS = 2
# monster-split submits its branches at once too, but on one cluster by
# default, so it measures concurrency inside one warehouse against parallelism
# inside one statement. --max-clusters, if given, sets both.
DEFAULT_SPLIT_MAX_CLUSTERS = 1

# --------------------------------------------------------------------------- #
# The data: Snowflake's free TPC-H sample share
# --------------------------------------------------------------------------- #
SCALES = (1, 10, 100, 1000)
DEFAULT_SCALE = 10


def sample_schema(scale: int) -> str:
    if scale not in SCALES:
        raise ValueError(f"scale must be one of {', '.join(map(str, SCALES))}, got {scale!r}")
    return f"SNOWFLAKE_SAMPLE_DATA.TPCH_SF{scale}"


def base_table(database: str, scale: int) -> str:
    """The experiment's own copy of LINEITEM, sorted by ship date, that the DML workload clones."""
    sample_schema(scale)  # validates the scale
    return f"{database}.PUBLIC.LINEITEM_SF{scale}"


# setup copies LINEITEM on a size big enough to sort it in a few minutes, with a
# hard timeout. SF1000 isn't offered for the copy: it would cost several credits.
BASE_COPY_SIZE = {1: "XSMALL", 10: "SMALL", 100: "MEDIUM"}
BASE_COPY_TIMEOUT_SECONDS = 900

# Sorting by ship date clusters the copy the way a fact table loaded by day
# usually is, so the DML workload's one-month delete and insert can prune.
BASE_COPY_SQL = "CREATE TABLE {table} AS SELECT * FROM {schema}.LINEITEM ORDER BY l_shipdate"

# --------------------------------------------------------------------------- #
# The 22 TPC-H queries
#
# Standard validation parameters. Q2, Q17, and Q20 use the usual decorrelated
# rewrites (same answers), so no warehouse type trips over a correlated
# subquery. ``{s}`` is the sample schema. The text is identical on every
# warehouse, so QUERY_HISTORY's query_hash shows the workload didn't change.
# --------------------------------------------------------------------------- #
TPCH: dict[str, str] = {
    "q01": """
SELECT l_returnflag, l_linestatus, SUM(l_quantity) AS sum_qty, SUM(l_extendedprice) AS sum_base_price,
       SUM(l_extendedprice * (1 - l_discount)) AS sum_disc_price,
       SUM(l_extendedprice * (1 - l_discount) * (1 + l_tax)) AS sum_charge,
       AVG(l_quantity) AS avg_qty, AVG(l_extendedprice) AS avg_price, AVG(l_discount) AS avg_disc,
       COUNT(*) AS count_order
FROM {s}.lineitem
WHERE l_shipdate <= '1998-09-02'::DATE
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus""",
    "q02": """
WITH min_cost AS (
    SELECT ps_partkey AS mc_partkey, MIN(ps_supplycost) AS mc_supplycost
    FROM {s}.partsupp, {s}.supplier, {s}.nation, {s}.region
    WHERE s_suppkey = ps_suppkey AND s_nationkey = n_nationkey AND n_regionkey = r_regionkey AND r_name = 'EUROPE'
    GROUP BY ps_partkey
)
SELECT s_acctbal, s_name, n_name, p_partkey, p_mfgr, s_address, s_phone, s_comment
FROM {s}.part, {s}.supplier, {s}.partsupp, {s}.nation, {s}.region, min_cost
WHERE p_partkey = ps_partkey AND s_suppkey = ps_suppkey AND p_size = 15 AND p_type LIKE '%BRASS'
  AND s_nationkey = n_nationkey AND n_regionkey = r_regionkey AND r_name = 'EUROPE'
  AND ps_partkey = mc_partkey AND ps_supplycost = mc_supplycost
ORDER BY s_acctbal DESC, n_name, s_name, p_partkey
LIMIT 100""",
    "q03": """
SELECT l_orderkey, SUM(l_extendedprice * (1 - l_discount)) AS revenue, o_orderdate, o_shippriority
FROM {s}.customer, {s}.orders, {s}.lineitem
WHERE c_mktsegment = 'BUILDING' AND c_custkey = o_custkey AND l_orderkey = o_orderkey
  AND o_orderdate < '1995-03-15'::DATE AND l_shipdate > '1995-03-15'::DATE
GROUP BY l_orderkey, o_orderdate, o_shippriority
ORDER BY revenue DESC, o_orderdate
LIMIT 10""",
    "q04": """
SELECT o_orderpriority, COUNT(*) AS order_count
FROM {s}.orders
WHERE o_orderdate >= '1993-07-01'::DATE AND o_orderdate < '1993-10-01'::DATE
  AND EXISTS (SELECT * FROM {s}.lineitem WHERE l_orderkey = o_orderkey AND l_commitdate < l_receiptdate)
GROUP BY o_orderpriority
ORDER BY o_orderpriority""",
    "q05": """
SELECT n_name, SUM(l_extendedprice * (1 - l_discount)) AS revenue
FROM {s}.customer, {s}.orders, {s}.lineitem, {s}.supplier, {s}.nation, {s}.region
WHERE c_custkey = o_custkey AND l_orderkey = o_orderkey AND l_suppkey = s_suppkey
  AND c_nationkey = s_nationkey AND s_nationkey = n_nationkey AND n_regionkey = r_regionkey
  AND r_name = 'ASIA' AND o_orderdate >= '1994-01-01'::DATE AND o_orderdate < '1995-01-01'::DATE
GROUP BY n_name
ORDER BY revenue DESC""",
    "q06": """
SELECT SUM(l_extendedprice * l_discount) AS revenue
FROM {s}.lineitem
WHERE l_shipdate >= '1994-01-01'::DATE AND l_shipdate < '1995-01-01'::DATE
  AND l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24""",
    "q07": """
SELECT supp_nation, cust_nation, l_year, SUM(volume) AS revenue
FROM (
    SELECT n1.n_name AS supp_nation, n2.n_name AS cust_nation, YEAR(l_shipdate) AS l_year,
           l_extendedprice * (1 - l_discount) AS volume
    FROM {s}.supplier, {s}.lineitem, {s}.orders, {s}.customer, {s}.nation n1, {s}.nation n2
    WHERE s_suppkey = l_suppkey AND o_orderkey = l_orderkey AND c_custkey = o_custkey
      AND s_nationkey = n1.n_nationkey AND c_nationkey = n2.n_nationkey
      AND ((n1.n_name = 'FRANCE' AND n2.n_name = 'GERMANY') OR (n1.n_name = 'GERMANY' AND n2.n_name = 'FRANCE'))
      AND l_shipdate BETWEEN '1995-01-01'::DATE AND '1996-12-31'::DATE
) shipping
GROUP BY supp_nation, cust_nation, l_year
ORDER BY supp_nation, cust_nation, l_year""",
    "q08": """
SELECT o_year, SUM(CASE WHEN nation = 'BRAZIL' THEN volume ELSE 0 END) / SUM(volume) AS mkt_share
FROM (
    SELECT YEAR(o_orderdate) AS o_year, l_extendedprice * (1 - l_discount) AS volume, n2.n_name AS nation
    FROM {s}.part, {s}.supplier, {s}.lineitem, {s}.orders, {s}.customer, {s}.nation n1, {s}.nation n2, {s}.region
    WHERE p_partkey = l_partkey AND s_suppkey = l_suppkey AND l_orderkey = o_orderkey AND o_custkey = c_custkey
      AND c_nationkey = n1.n_nationkey AND n1.n_regionkey = r_regionkey AND r_name = 'AMERICA'
      AND s_nationkey = n2.n_nationkey AND o_orderdate BETWEEN '1995-01-01'::DATE AND '1996-12-31'::DATE
      AND p_type = 'ECONOMY ANODIZED STEEL'
) all_nations
GROUP BY o_year
ORDER BY o_year""",
    "q09": """
SELECT nation, o_year, SUM(amount) AS sum_profit
FROM (
    SELECT n_name AS nation, YEAR(o_orderdate) AS o_year,
           l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity AS amount
    FROM {s}.part, {s}.supplier, {s}.lineitem, {s}.partsupp, {s}.orders, {s}.nation
    WHERE s_suppkey = l_suppkey AND ps_suppkey = l_suppkey AND ps_partkey = l_partkey AND p_partkey = l_partkey
      AND o_orderkey = l_orderkey AND s_nationkey = n_nationkey AND p_name LIKE '%green%'
) profit
GROUP BY nation, o_year
ORDER BY nation, o_year DESC""",
    "q10": """
SELECT c_custkey, c_name, SUM(l_extendedprice * (1 - l_discount)) AS revenue, c_acctbal, n_name, c_address,
       c_phone, c_comment
FROM {s}.customer, {s}.orders, {s}.lineitem, {s}.nation
WHERE c_custkey = o_custkey AND l_orderkey = o_orderkey
  AND o_orderdate >= '1993-10-01'::DATE AND o_orderdate < '1994-01-01'::DATE
  AND l_returnflag = 'R' AND c_nationkey = n_nationkey
GROUP BY c_custkey, c_name, c_acctbal, c_phone, n_name, c_address, c_comment
ORDER BY revenue DESC
LIMIT 20""",
    "q11": """
SELECT ps_partkey, SUM(ps_supplycost * ps_availqty) AS value
FROM {s}.partsupp, {s}.supplier, {s}.nation
WHERE ps_suppkey = s_suppkey AND s_nationkey = n_nationkey AND n_name = 'GERMANY'
GROUP BY ps_partkey
HAVING SUM(ps_supplycost * ps_availqty) > (
    SELECT SUM(ps_supplycost * ps_availqty) * 0.0001
    FROM {s}.partsupp, {s}.supplier, {s}.nation
    WHERE ps_suppkey = s_suppkey AND s_nationkey = n_nationkey AND n_name = 'GERMANY'
)
ORDER BY value DESC""",
    "q12": """
SELECT l_shipmode,
       SUM(CASE WHEN o_orderpriority = '1-URGENT' OR o_orderpriority = '2-HIGH' THEN 1 ELSE 0 END) AS high_line_count,
       SUM(CASE WHEN o_orderpriority <> '1-URGENT' AND o_orderpriority <> '2-HIGH' THEN 1 ELSE 0 END) AS low_line_count
FROM {s}.orders, {s}.lineitem
WHERE o_orderkey = l_orderkey AND l_shipmode IN ('MAIL', 'SHIP')
  AND l_commitdate < l_receiptdate AND l_shipdate < l_commitdate
  AND l_receiptdate >= '1994-01-01'::DATE AND l_receiptdate < '1995-01-01'::DATE
GROUP BY l_shipmode
ORDER BY l_shipmode""",
    "q13": """
SELECT c_count, COUNT(*) AS custdist
FROM (
    SELECT c_custkey, COUNT(o_orderkey) AS c_count
    FROM {s}.customer LEFT OUTER JOIN {s}.orders
      ON c_custkey = o_custkey AND o_comment NOT LIKE '%special%requests%'
    GROUP BY c_custkey
) c_orders
GROUP BY c_count
ORDER BY custdist DESC, c_count DESC""",
    "q14": """
SELECT 100.00 * SUM(CASE WHEN p_type LIKE 'PROMO%' THEN l_extendedprice * (1 - l_discount) ELSE 0 END)
       / SUM(l_extendedprice * (1 - l_discount)) AS promo_revenue
FROM {s}.lineitem, {s}.part
WHERE l_partkey = p_partkey AND l_shipdate >= '1995-09-01'::DATE AND l_shipdate < '1995-10-01'::DATE""",
    "q15": """
WITH revenue AS (
    SELECT l_suppkey AS supplier_no, SUM(l_extendedprice * (1 - l_discount)) AS total_revenue
    FROM {s}.lineitem
    WHERE l_shipdate >= '1996-01-01'::DATE AND l_shipdate < '1996-04-01'::DATE
    GROUP BY l_suppkey
)
SELECT s_suppkey, s_name, s_address, s_phone, total_revenue
FROM {s}.supplier, revenue
WHERE s_suppkey = supplier_no AND total_revenue = (SELECT MAX(total_revenue) FROM revenue)
ORDER BY s_suppkey""",
    "q16": """
SELECT p_brand, p_type, p_size, COUNT(DISTINCT ps_suppkey) AS supplier_cnt
FROM {s}.partsupp, {s}.part
WHERE p_partkey = ps_partkey AND p_brand <> 'Brand#45' AND p_type NOT LIKE 'MEDIUM POLISHED%'
  AND p_size IN (49, 14, 23, 45, 19, 3, 36, 9)
  AND ps_suppkey NOT IN (SELECT s_suppkey FROM {s}.supplier WHERE s_comment LIKE '%Customer%Complaints%')
GROUP BY p_brand, p_type, p_size
ORDER BY supplier_cnt DESC, p_brand, p_type, p_size""",
    "q17": """
WITH part_avg AS (
    SELECT l_partkey AS pa_partkey, 0.2 * AVG(l_quantity) AS pa_quantity
    FROM {s}.lineitem
    GROUP BY l_partkey
)
SELECT SUM(l_extendedprice) / 7.0 AS avg_yearly
FROM {s}.lineitem, {s}.part, part_avg
WHERE p_partkey = l_partkey AND pa_partkey = l_partkey AND p_brand = 'Brand#23' AND p_container = 'MED BOX'
  AND l_quantity < pa_quantity""",
    "q18": """
SELECT c_name, c_custkey, o_orderkey, o_orderdate, o_totalprice, SUM(l_quantity) AS total_qty
FROM {s}.customer, {s}.orders, {s}.lineitem
WHERE o_orderkey IN (SELECT l_orderkey FROM {s}.lineitem GROUP BY l_orderkey HAVING SUM(l_quantity) > 300)
  AND c_custkey = o_custkey AND o_orderkey = l_orderkey
GROUP BY c_name, c_custkey, o_orderkey, o_orderdate, o_totalprice
ORDER BY o_totalprice DESC, o_orderdate
LIMIT 100""",
    "q19": """
SELECT SUM(l_extendedprice * (1 - l_discount)) AS revenue
FROM {s}.lineitem, {s}.part
WHERE (p_partkey = l_partkey AND p_brand = 'Brand#12'
       AND p_container IN ('SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
       AND l_quantity >= 1 AND l_quantity <= 11 AND p_size BETWEEN 1 AND 5
       AND l_shipmode IN ('AIR', 'AIR REG') AND l_shipinstruct = 'DELIVER IN PERSON')
   OR (p_partkey = l_partkey AND p_brand = 'Brand#23'
       AND p_container IN ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
       AND l_quantity >= 10 AND l_quantity <= 20 AND p_size BETWEEN 1 AND 10
       AND l_shipmode IN ('AIR', 'AIR REG') AND l_shipinstruct = 'DELIVER IN PERSON')
   OR (p_partkey = l_partkey AND p_brand = 'Brand#34'
       AND p_container IN ('LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
       AND l_quantity >= 20 AND l_quantity <= 30 AND p_size BETWEEN 1 AND 15
       AND l_shipmode IN ('AIR', 'AIR REG') AND l_shipinstruct = 'DELIVER IN PERSON')""",
    "q20": """
WITH shipped AS (
    SELECT l_partkey AS sh_partkey, l_suppkey AS sh_suppkey, 0.5 * SUM(l_quantity) AS sh_quantity
    FROM {s}.lineitem
    WHERE l_shipdate >= '1994-01-01'::DATE AND l_shipdate < '1995-01-01'::DATE
    GROUP BY l_partkey, l_suppkey
)
SELECT s_name, s_address
FROM {s}.supplier, {s}.nation
WHERE s_suppkey IN (
        SELECT ps_suppkey
        FROM {s}.partsupp, shipped
        WHERE sh_partkey = ps_partkey AND sh_suppkey = ps_suppkey AND ps_availqty > sh_quantity
          AND ps_partkey IN (SELECT p_partkey FROM {s}.part WHERE p_name LIKE 'forest%')
    )
  AND s_nationkey = n_nationkey AND n_name = 'CANADA'
ORDER BY s_name""",
    "q21": """
SELECT s_name, COUNT(*) AS numwait
FROM {s}.supplier, {s}.lineitem l1, {s}.orders, {s}.nation
WHERE s_suppkey = l1.l_suppkey AND o_orderkey = l1.l_orderkey AND o_orderstatus = 'F'
  AND l1.l_receiptdate > l1.l_commitdate
  AND EXISTS (SELECT * FROM {s}.lineitem l2 WHERE l2.l_orderkey = l1.l_orderkey AND l2.l_suppkey <> l1.l_suppkey)
  AND NOT EXISTS (
      SELECT * FROM {s}.lineitem l3
      WHERE l3.l_orderkey = l1.l_orderkey AND l3.l_suppkey <> l1.l_suppkey AND l3.l_receiptdate > l3.l_commitdate
  )
  AND s_nationkey = n_nationkey AND n_name = 'SAUDI ARABIA'
GROUP BY s_name
ORDER BY numwait DESC, s_name
LIMIT 100""",
    "q22": """
SELECT cntrycode, COUNT(*) AS numcust, SUM(c_acctbal) AS totacctbal
FROM (
    SELECT SUBSTRING(c_phone, 1, 2) AS cntrycode, c_acctbal
    FROM {s}.customer
    WHERE SUBSTRING(c_phone, 1, 2) IN ('13', '31', '23', '29', '30', '18', '17')
      AND c_acctbal > (
          SELECT AVG(c_acctbal) FROM {s}.customer
          WHERE c_acctbal > 0.00 AND SUBSTRING(c_phone, 1, 2) IN ('13', '31', '23', '29', '30', '18', '17')
      )
      AND NOT EXISTS (SELECT * FROM {s}.orders WHERE o_custkey = c_custkey)
) custsale
GROUP BY cntrycode
ORDER BY cntrycode""",
}

# --------------------------------------------------------------------------- #
# The workloads: five after the select.dev article, and two opt-in "monster" ones
#
# Each is a fixed list of statements. ``prepare`` runs before the cell's
# warehouse is used and ``teardown`` after; both are metadata-only (a zero-copy
# CLONE, DROP TABLE), so they need no warehouse and bill nothing.
# ``{table}`` is the cell's own scratch table, ``{base}`` setup's sorted copy.
# --------------------------------------------------------------------------- #
DML_MONTH = ("1995-06-01", "1995-07-01")


@dataclass(frozen=True)
class Workload:
    """One workload shape: what it runs, and the question it puts to the warehouses."""

    key: str  # CLI value and query-tag field
    code: str  # in warehouse and table names
    concurrent: bool  # submit every statement at once, rather than one after another
    does: str
    why: str
    # Its share of the cost cap, relative to the other cells in the run. Every
    # select.dev workload weighs 1, so without a monster workload the budget
    # splits evenly per cell, as it always has.
    weight: float = 1.0
    # The longest one cell of it may run; None means MAX_WALL_SECONDS.
    max_wall_s: int | None = None
    # Its statements run in stages: everything in a stage at once, each stage
    # after the one before. Implies ``concurrent``.
    staged: bool = False
    # Every cell of it returns the same one-row answer, which run compares.
    fingerprinted: bool = False


SINGLE = Workload(
    "single",
    "ONE",
    False,
    "TPC-H Q1, once, on a warehouse that has never run anything.",
    "The sparse case: one short query. A standard warehouse bills at least 60 seconds each time it resumes; "
    "Adaptive bills by the query.",
)
SEQUENTIAL = Workload(
    "sequential",
    "SEQ",
    False,
    "The 22 TPC-H queries, one after another with no gaps.",
    "The sustained case: the warehouse is busy the whole time, so the 60-second minimum barely matters.",
)
CONCURRENT = Workload(
    "concurrent",
    "CON",
    True,
    "The 22 TPC-H queries, all submitted at once.",
    "The burst case: a standard warehouse queues or scales out clusters; Adaptive bursts up to its "
    "throughput multiplier.",
)
DML = Workload(
    "dml",
    "DML",
    False,
    "Delete one month of LINEITEM (about 1% of rows) from a fresh clone, then insert it back.",
    "The refresh case: writes that touch little data when the table prunes well.",
)
CTAS = Workload(
    "ctas",
    "CTAS",
    False,
    "Two CREATE TABLE AS SELECTs: a narrow copy of LINEITEM, then LINEITEM joined to ORDERS and CUSTOMER.",
    "The transform case: big scans and big writes.",
)
MONSTER = Workload(
    "monster",
    "MON",
    False,
    "One long statement: a wide DAG of CTEs (16 independent branches by default, each a moderate aggregation "
    "and rejoin over a different table or date window), combined layer by layer down to one row.",
    "The dbt or BI-tool case: one statement with dozens of CTEs, most of them independent. Can a warehouse run "
    "the branches in parallel, and does Adaptive give one such statement more than its size?",
    weight=3.0,
    max_wall_s=3600,
    fingerprinted=True,
)
MONSTER_SPLIT = Workload(
    "monster-split",
    "MSP",
    True,
    "The same logic as monster, dbt-style: each branch a CREATE TEMPORARY TABLE, all submitted at once, then one "
    "statement that combines them.",
    "Against monster: does Adaptive's advantage come from parallelism inside one statement, or from concurrency "
    "across statements?",
    weight=3.0,
    max_wall_s=3600,
    staged=True,
    fingerprinted=True,
)
# What `run` measures unless --workload says otherwise. The monster workloads are opt-in.
DEFAULT_WORKLOADS = (SINGLE, SEQUENTIAL, CONCURRENT, DML, CTAS)
WORKLOADS = (*DEFAULT_WORKLOADS, MONSTER, MONSTER_SPLIT)
WORKLOADS_BY_KEY = {workload.key: workload for workload in WORKLOADS}
# The report sets each monster-split cell against the monster cell of the same size and config.
WORKLOAD_PAIRS = ((MONSTER_SPLIT, MONSTER),)
DEFAULT_FANOUT = monster.DEFAULT_FANOUT


def workload_stages(
    workload: Workload, *, schema: str, table: str, base: str, fanout: int = DEFAULT_FANOUT
) -> list[list[tuple[str, str]]]:
    """A concurrent workload's statements in stages: each stage all at once, after the one before.

    The concurrent workload is one stage. monster-split is two: every branch
    into its own temporary table, then the statement that combines them.
    """
    if workload is MONSTER_SPLIT:
        return monster.render_stages(monster.build_dag(schema, fanout=fanout), prefix=table)
    if not workload.concurrent:
        raise ValueError(f"the {workload.key} workload runs one statement at a time, not in stages")
    return [workload_statements(workload, schema=schema, table=table, base=base, fanout=fanout)]


def workload_statements(
    workload: Workload, *, schema: str, table: str, base: str, fanout: int = DEFAULT_FANOUT
) -> list[tuple[str, str]]:
    """The ``(name, sql)`` statements a workload runs on the cell's warehouse, in order."""
    if workload is MONSTER:
        return [("monster", monster.render_monolithic(monster.build_dag(schema, fanout=fanout)))]
    if workload is MONSTER_SPLIT:
        stages = workload_stages(workload, schema=schema, table=table, base=base, fanout=fanout)
        return [statement for stage in stages for statement in stage]
    if workload is SINGLE:
        return [("q01", TPCH["q01"].format(s=schema))]
    if workload in (SEQUENTIAL, CONCURRENT):
        return [(name, sql.format(s=schema)) for name, sql in TPCH.items()]
    start, end = DML_MONTH
    month = f"l_shipdate >= '{start}'::DATE AND l_shipdate < '{end}'::DATE"
    if workload is DML:
        return [
            ("delete_month", f"DELETE FROM {table} WHERE {month}"),
            ("insert_month", f"INSERT INTO {table} SELECT * FROM {base} WHERE {month}"),
        ]
    if workload is CTAS:
        return [
            (
                "ctas_narrow",
                f"CREATE TABLE {table}_NARROW AS "
                "SELECT l_orderkey, l_partkey, l_suppkey, l_quantity, l_extendedprice, l_shipdate "
                f"FROM {schema}.lineitem",
            ),
            (
                "ctas_wide",
                f"CREATE TABLE {table}_WIDE AS SELECT l.*, o.*, c.c_name, c.c_mktsegment, c.c_nationkey "
                f"FROM {schema}.lineitem l JOIN {schema}.orders o ON l.l_orderkey = o.o_orderkey "
                f"JOIN {schema}.customer c ON o.o_custkey = c.c_custkey",
            ),
        ]
    raise ValueError(f"unknown workload {workload.key!r}")


def prepare_statements(workload: Workload, *, table: str, base: str) -> list[str]:
    """Metadata-only statements to run before the workload (the DML workload's fresh clone)."""
    return [f"CREATE TABLE {table} CLONE {base}"] if workload is DML else []


def teardown_statements(workload: Workload, *, table: str, fanout: int = DEFAULT_FANOUT) -> list[str]:
    """Metadata-only statements to run after the workload, whether or not it finished."""
    if workload is MONSTER_SPLIT:
        # Temporary tables would last as long as the run's session, across every cell. Drop them now.
        return [
            f"DROP TABLE IF EXISTS {monster.branch_table(table, monster.branch_name(index))}" for index in range(fanout)
        ]
    if workload is DML:
        return [f"DROP TABLE IF EXISTS {table}"]
    if workload is CTAS:
        return [f"DROP TABLE IF EXISTS {table}_NARROW", f"DROP TABLE IF EXISTS {table}_WIDE"]
    return []


# --------------------------------------------------------------------------- #
# Warehouse DDL
#
# Every cell gets a brand-new warehouse, so WAREHOUSE_METERING_HISTORY for that
# warehouse is exactly what the cell cost, cold start and all. Creating one
# costs nothing; a standard warehouse bills from its first query, an Adaptive
# one by the query.
# --------------------------------------------------------------------------- #
def create_cell_warehouse_sql(
    name: str,
    *,
    config: Config,
    size: str,
    concurrent: bool,
    max_clusters: int,
    throughput_multiplier: int,
    timeout_s: int,
) -> str:
    """The CREATE WAREHOUSE for one cell. ``timeout_s`` caps each statement, queued or running."""
    validate_name(name, "warehouse")
    timeouts = f"STATEMENT_TIMEOUT_IN_SECONDS = {int(timeout_s)} STATEMENT_QUEUED_TIMEOUT_IN_SECONDS = {int(timeout_s)}"
    if config.adaptive:
        return (
            f"CREATE WAREHOUSE {name} WITH WAREHOUSE_TYPE = 'ADAPTIVE' "
            f"MAX_QUERY_PERFORMANCE_LEVEL = {size} QUERY_THROUGHPUT_MULTIPLIER = {int(throughput_multiplier)} "
            f"{timeouts} COMMENT = '{OWNER_COMMENT}'"
        )
    clusters = (
        f"MIN_CLUSTER_COUNT = 1 MAX_CLUSTER_COUNT = {int(max_clusters)} SCALING_POLICY = STANDARD "
        if concurrent and max_clusters > 1
        else ""
    )
    # GENERATION is always explicit: Snowflake's default is now '2'.
    return (
        f"CREATE WAREHOUSE {name} WITH WAREHOUSE_TYPE = STANDARD WAREHOUSE_SIZE = {size} "
        f"GENERATION = '{config.generation}' {clusters}"
        f"AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE {timeouts} COMMENT = '{OWNER_COMMENT}'"
    )


# The home warehouse: setup's copy, and the report's ACCOUNT_USAGE reads.
IDLE_TIMEOUT_SECONDS = 1800

# --------------------------------------------------------------------------- #
# The cost cap (see plan.wall_limits)
# --------------------------------------------------------------------------- #
DEFAULT_MAX_CREDITS = 10.0
# Every cell gets at least this long. Less would cancel queries on a warehouse
# that bills its first 60 seconds regardless.
MIN_WALL_SECONDS = 60
MAX_WALL_SECONDS = 1800
# Held back from each cell's wall time for the DDL, the session settings, and the drop.
OVERHEAD_SECONDS = 5
# How often the concurrent workload checks on its queries.
POLL_SECONDS = 1.0

# --------------------------------------------------------------------------- #
# Query tags and run ids
#
# Every cell's statements are tagged adcomp:<run id>:<workload>:<size>:<config>:<replicate>.
# Run ids are UTC timestamps (20261014_143318), so the latest sorts last.
# --------------------------------------------------------------------------- #
QUERY_TAG_PREFIX = "adcomp"
_RUN_ID = re.compile(r"[0-9]{8}_[0-9]{6}")


def validate_run_id(run_id: str) -> str:
    """Return ``run_id`` if it looks like one ``run`` made, else raise ``ValueError``."""
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"run id must look like 20261014_143318, got {run_id!r}")
    return run_id


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #
ACCOUNT_USAGE_PROBES = {
    "QUERY_HISTORY": "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY LIMIT 1",
    "WAREHOUSE_METERING_HISTORY": "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY LIMIT 1",
    "QUERY_METERING_HISTORY": "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_METERING_HISTORY LIMIT 1",
}

# --------------------------------------------------------------------------- #
# Reporting queries
#
# Latency: QUERY_HISTORY up to ~45 minutes, QUERY_METERING_HISTORY up to ~1
# hour, WAREHOUSE_METERING_HISTORY up to ~3 hours. ``{tag}`` is the run's tag
# prefix and ``{cells}`` its cell-warehouse name prefix; both are built from
# validated names and run ids.
# --------------------------------------------------------------------------- #

# Only the statements that do the work. The session settings, CLONEs, and DROPs
# around them are tagged too, but use no warehouse.
WORK_QUERY_TYPES = ("SELECT", "INSERT", "DELETE", "CREATE_TABLE_AS_SELECT")
_TYPES = ", ".join(f"'{t}'" for t in WORK_QUERY_TYPES)

LATEST_RUN_SQL = """
SELECT MAX(SPLIT_PART(query_tag, ':', 2)) AS run_id
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE query_tag LIKE 'adcomp:%'
  AND STARTSWITH(warehouse_name, '{home}_')
  AND REGEXP_LIKE(SPLIT_PART(query_tag, ':', 2), '[0-9]{{8}}_[0-9]{{6}}')
  AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
"""

# One row per cell. A cluster is busy from its first query's start to its last
# query's end. Summed over clusters, ``busy_s`` is what the work costs on a
# standard warehouse that's already running, and ``floor_s`` (each cluster's
# busy time, 60 seconds at least) is what a fresh one should bill for it.
#
# Where the time went, summed over the cell's statements: ``compile_s`` runs in
# cloud services, before the warehouse sees the query, and long SQL can be
# compile-bound; ``exec_s`` is on the warehouse; the two queue times say whether
# it waited for a warehouse to start or for room on one. Spill shows a warehouse
# that ran out of memory (local) or local disk (remote). Cloud services credits
# are the per-query figure, before Snowflake's daily 10% adjustment.
CELLS_SQL = f"""
WITH q AS (
    SELECT query_tag, warehouse_name, COALESCE(cluster_number, 0) AS cluster_number, start_time, end_time,
           total_elapsed_time / 1000.0                                     AS elapsed_s,
           (queued_overload_time + queued_provisioning_time) / 1000.0      AS queued_s,
           compilation_time / 1000.0                                       AS compile_s,
           execution_time / 1000.0                                         AS exec_s,
           queued_provisioning_time / 1000.0                               AS provisioning_s,
           queued_overload_time / 1000.0                                   AS overload_s,
           COALESCE(bytes_spilled_to_local_storage, 0)                     AS spilled_local,
           COALESCE(bytes_spilled_to_remote_storage, 0)                    AS spilled_remote,
           COALESCE(credits_used_cloud_services, 0)                        AS cloud_services,
           execution_status = 'SUCCESS'                                    AS ok,
           query_hash
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
    WHERE query_tag LIKE '{{tag}}%'
      AND query_type IN ({_TYPES})
      -- run's fallback for cancelling a burst at its limit is a tagged SELECT too
      AND query_text NOT ILIKE '%SYSTEM$CANCEL_QUERY%'
      AND start_time > DATEADD('hour', -{{hours}}, CURRENT_TIMESTAMP())
), per_cluster AS (
    SELECT query_tag, cluster_number, DATEDIFF('millisecond', MIN(start_time), MAX(end_time)) / 1000.0 AS span_s
    FROM q GROUP BY query_tag, cluster_number
), clusters AS (
    SELECT query_tag, COUNT(*) AS clusters, SUM(span_s) AS busy_s, SUM(GREATEST(span_s, 60)) AS floor_s
    FROM per_cluster GROUP BY query_tag
)
SELECT q.query_tag,
       ANY_VALUE(q.warehouse_name)                                              AS warehouse_name,
       COUNT(*)                                                                 AS statements,
       COUNT_IF(NOT q.ok)                                                       AS failed,
       COUNT(DISTINCT q.query_hash)                                             AS distinct_hashes,
       DATEDIFF('millisecond', MIN(q.start_time), MAX(q.end_time)) / 1000.0     AS wall_s,
       SUM(q.elapsed_s)                                                         AS elapsed_s,
       SUM(q.queued_s)                                                          AS queued_s,
       SUM(q.compile_s)                                                         AS compile_s,
       SUM(q.exec_s)                                                            AS exec_s,
       SUM(q.provisioning_s)                                                    AS provisioning_s,
       SUM(q.overload_s)                                                        AS overload_s,
       SUM(q.spilled_local) / POWER(1024, 3)                                    AS spilled_local_gb,
       SUM(q.spilled_remote) / POWER(1024, 3)                                   AS spilled_remote_gb,
       SUM(q.cloud_services)                                                    AS cloud_services_credits,
       ANY_VALUE(c.clusters)                                                    AS clusters,
       ANY_VALUE(c.busy_s)                                                      AS busy_s,
       ANY_VALUE(c.floor_s)                                                     AS floor_s
FROM q JOIN clusters c ON c.query_tag = q.query_tag
GROUP BY q.query_tag
ORDER BY q.query_tag
"""

# What Snowflake billed each cell's warehouse, cold start and all.
METERING_SQL = """
SELECT warehouse_name, SUM(credits_used_compute) AS billed_credits
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE STARTSWITH(warehouse_name, '{cells}')
  -- Metering rows are hourly, so start from the top of the window's first hour.
  AND start_time >= DATE_TRUNC('hour', DATEADD('hour', -{hours}, CURRENT_TIMESTAMP()))
GROUP BY warehouse_name
"""

# Adaptive's per-query bill, summed per cell: a cross-check on the metering above.
QUERY_METERING_SQL = """
SELECT query_tag, SUM(credits_used_compute) AS query_credits
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_METERING_HISTORY
WHERE query_tag LIKE '{tag}%'
  AND query_start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
GROUP BY query_tag
"""


def report_sql(sql: str, *, home: str, run_id: str, hours: int) -> str:
    """Fill a reporting query's placeholders. Everything formatted in is validated."""
    if int(hours) < 1:
        raise ValueError(f"hours must be at least 1, got {hours}")
    home = validate_name(home, "warehouse")
    return sql.format(
        home=home,
        tag=f"{QUERY_TAG_PREFIX}:{validate_run_id(run_id)}:",
        cells=f"{home}_{run_id}_",
        hours=int(hours),
    )
