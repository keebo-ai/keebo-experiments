"""The "monster" query: one long statement made of many CTEs, most of them independent.

This is the shape dbt-compiled models and BI tools produce: dozens of CTEs, many
of them branches that don't depend on each other, then a few layers that
combine them, and a final SELECT. The work isn't in any one huge scan. It's in
many moderate ones that could, in principle, all run at once.

:func:`build_dag` builds that logic once, as a list of :class:`Node` values, and
two renderers turn it into SQL:

- :func:`render_monolithic`: the whole DAG as one ``WITH ... SELECT`` (the
  ``monster`` workload), and
- :func:`render_stages`: the same logic dbt-style (the ``monster-split``
  workload). Each branch becomes its own ``CREATE TEMPORARY TABLE ... AS``, all
  submitted at once, then one statement combines them.

Because both come from the same nodes, they can't drift apart, and they return
the same one-row answer: a key count, a total, and a ``HASH_AGG`` fingerprint
that ``run`` compares across cells.

Each branch has two steps over a different table or slice of the TPC-H data:

- **stage A** aggregates a fact table by a high-cardinality id (supplier, part,
  customer, order, clerk) within a date window, and
- **stage B** rejoins the fact table to the ids whose stage-A measure is
  above average, and aggregates to the nation key (25 values).

The safety properties, which the tests check:

- every CTE is consumed, so the optimizer can't prune any of them away,
- every branch is distinct (a different template, or a different date window
  of the same one), so no two can be computed once and shared,
- every filter is a real date range on a real column, never a predicate that
  folds to a constant, and
- every measure is an exact sum or count (no floats), so the fingerprint is
  the same on every warehouse.

Pure string building, with no database or CLI dependencies.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

DEFAULT_FANOUT = 16
MIN_FANOUT = 2
MAX_FANOUT = 32

# The data starts in January 1992. Each branch reads a 12-month window starting
# ``template index`` months plus ``12 x slice`` years in, so every window is real
# data (the latest ends in 1998-08, where TPC-H's ship dates stop).
_FIRST_MONTH = dt.date(1992, 1, 1)
_WINDOW_MONTHS = 12


@dataclass(frozen=True)
class Branch:
    """One branch template. ``{s}`` is the schema, ``{lo}``/``{hi}`` the window, ``{a}`` stage A's name."""

    key: str
    a: str  # -> (id, v)
    b: str  # -> (k, v), k the nation key


# Stage B keeps the ids whose stage-A measure is above the average. An exact,
# parallel filter: a rank or percentile would need one global sort, and an
# approximate percentile could pick different ids on different warehouses.
_TOP_IDS = "(SELECT id FROM {a} WHERE v > (SELECT AVG(v) FROM {a}))"

BRANCHES: tuple[Branch, ...] = (
    Branch(
        "li_supp",
        "SELECT l_suppkey AS id, SUM(l_extendedprice * (1 - l_discount)) AS v FROM {s}.lineitem "
        "WHERE l_shipdate >= {lo} AND l_shipdate < {hi} GROUP BY l_suppkey",
        "SELECT s_nationkey AS k, SUM(l_quantity) AS v FROM {s}.lineitem "
        f"JOIN {_TOP_IDS} t ON l_suppkey = t.id JOIN {{s}}.supplier ON s_suppkey = l_suppkey "
        "WHERE l_shipdate >= {lo} AND l_shipdate < {hi} GROUP BY s_nationkey",
    ),
    Branch(
        "li_part",
        "SELECT l_partkey AS id, SUM(l_quantity) AS v FROM {s}.lineitem "
        "WHERE l_shipdate >= {lo} AND l_shipdate < {hi} GROUP BY l_partkey",
        "SELECT s_nationkey AS k, SUM(l_extendedprice) AS v FROM {s}.lineitem "
        f"JOIN {_TOP_IDS} t ON l_partkey = t.id JOIN {{s}}.supplier ON s_suppkey = l_suppkey "
        "WHERE l_shipdate >= {lo} AND l_shipdate < {hi} GROUP BY s_nationkey",
    ),
    Branch(
        "ord_cust",
        "SELECT o_custkey AS id, SUM(o_totalprice) AS v FROM {s}.orders "
        "WHERE o_orderdate >= {lo} AND o_orderdate < {hi} GROUP BY o_custkey",
        "SELECT c_nationkey AS k, COUNT(*) AS v FROM {s}.orders "
        f"JOIN {_TOP_IDS} t ON o_custkey = t.id JOIN {{s}}.customer ON c_custkey = o_custkey "
        "WHERE o_orderdate >= {lo} AND o_orderdate < {hi} GROUP BY c_nationkey",
    ),
    Branch(
        "li_ord_cust",
        "SELECT o_custkey AS id, SUM(l_extendedprice * (1 - l_discount)) AS v FROM {s}.lineitem "
        "JOIN {s}.orders ON o_orderkey = l_orderkey "
        "WHERE o_orderdate >= {lo} AND o_orderdate < {hi} GROUP BY o_custkey",
        "SELECT c_nationkey AS k, SUM(o_totalprice) AS v FROM {s}.orders "
        f"JOIN {_TOP_IDS} t ON o_custkey = t.id JOIN {{s}}.customer ON c_custkey = o_custkey "
        "WHERE o_orderdate >= {lo} AND o_orderdate < {hi} GROUP BY c_nationkey",
    ),
    Branch(
        "li_partsupp",
        "SELECT ps_partkey AS id, SUM(l_quantity * ps_supplycost) AS v FROM {s}.lineitem "
        "JOIN {s}.partsupp ON ps_partkey = l_partkey AND ps_suppkey = l_suppkey "
        "WHERE l_shipdate >= {lo} AND l_shipdate < {hi} GROUP BY ps_partkey",
        "SELECT s_nationkey AS k, SUM(l_extendedprice * l_discount) AS v FROM {s}.lineitem "
        f"JOIN {_TOP_IDS} t ON l_partkey = t.id JOIN {{s}}.supplier ON s_suppkey = l_suppkey "
        "WHERE l_shipdate >= {lo} AND l_shipdate < {hi} GROUP BY s_nationkey",
    ),
    Branch(
        "li_late_orders",
        "SELECT l_orderkey AS id, COUNT(*) AS v FROM {s}.lineitem "
        "WHERE l_receiptdate >= {lo} AND l_receiptdate < {hi} AND l_commitdate < l_receiptdate "
        "GROUP BY l_orderkey",
        "SELECT c_nationkey AS k, SUM(o_totalprice) AS v FROM {s}.orders "
        f"JOIN {_TOP_IDS} t ON o_orderkey = t.id JOIN {{s}}.customer ON c_custkey = o_custkey "
        "GROUP BY c_nationkey",
    ),
    Branch(
        "li_cust_parts",
        "SELECT o_custkey AS id, COUNT(DISTINCT l_partkey) AS v FROM {s}.lineitem "
        "JOIN {s}.orders ON o_orderkey = l_orderkey "
        "WHERE l_shipdate >= {lo} AND l_shipdate < {hi} GROUP BY o_custkey",
        "SELECT c_nationkey AS k, SUM(c_acctbal) AS v FROM {s}.customer "
        f"JOIN {_TOP_IDS} t ON c_custkey = t.id GROUP BY c_nationkey",
    ),
    Branch(
        "ord_clerk",
        "SELECT o_clerk AS id, SUM(o_totalprice) AS v FROM {s}.orders "
        "WHERE o_orderdate >= {lo} AND o_orderdate < {hi} GROUP BY o_clerk",
        "SELECT c_nationkey AS k, COUNT(*) AS v FROM {s}.orders "
        f"JOIN {_TOP_IDS} t ON o_clerk = t.id JOIN {{s}}.customer ON c_custkey = o_custkey "
        "WHERE o_orderdate >= {lo} AND o_orderdate < {hi} GROUP BY c_nationkey",
    ),
)


@dataclass(frozen=True)
class Node:
    """One CTE of the DAG.

    ``body`` refers to the nodes it reads as ``{name}`` placeholders, so a
    renderer can point them at a CTE or at a table. ``branch`` is the branch a
    branch node belongs to (``b01``), or ``None`` for the combine steps and the
    final SELECT.
    """

    name: str
    body: str
    deps: tuple[str, ...]
    branch: str | None = None


FINAL = "final"


def _month(offset: int) -> dt.date:
    year, month = divmod(_FIRST_MONTH.month - 1 + offset, 12)
    return dt.date(_FIRST_MONTH.year + year, month + 1, 1)


def window(index: int) -> tuple[str, str]:
    """Branch ``index``'s date window as SQL literals: template ``i`` months in, plus a year per slice."""
    template, slice_ = index % len(BRANCHES), index // len(BRANCHES)
    start = template + 12 * slice_
    lo, hi = _month(start), _month(start + _WINDOW_MONTHS)
    return f"'{lo.isoformat()}'::DATE", f"'{hi.isoformat()}'::DATE"


def branch_name(index: int) -> str:
    """Branch ``index``'s name, counting from 0: ``b01``, ``b02``, ..."""
    return f"b{index + 1:02d}"


def build_dag(schema: str, *, fanout: int = DEFAULT_FANOUT) -> list[Node]:
    """The monster query's CTEs in dependency order, ending with the one-row :data:`FINAL` SELECT.

    ``fanout`` independent branches (two CTEs each), then pairwise combine
    layers down to one, then the final SELECT: ``3 x fanout`` nodes in all.
    """
    if not MIN_FANOUT <= fanout <= MAX_FANOUT:
        raise ValueError(f"fanout must be between {MIN_FANOUT} and {MAX_FANOUT}, got {fanout}")
    nodes: list[Node] = []
    layer: list[str] = []
    for index in range(fanout):
        branch = BRANCHES[index % len(BRANCHES)]
        lo, hi = window(index)
        name = branch_name(index)
        a, b = f"{name}_a", f"{name}_b"
        fill = {"s": schema, "lo": lo, "hi": hi}
        nodes.append(Node(a, branch.a.format(**fill), (), name))
        nodes.append(Node(b, branch.b.format(**fill, a="{" + a + "}"), (a,), name))
        layer.append(b)

    depth = 0
    while len(layer) > 1:
        depth += 1
        merged: list[str] = []
        for pair in range(0, len(layer) - 1, 2):
            x, y = layer[pair], layer[pair + 1]
            name = f"c{depth}_{pair // 2 + 1:02d}"
            nodes.append(
                Node(
                    name,
                    "SELECT COALESCE(x.k, y.k) AS k, COALESCE(x.v, 0) + COALESCE(y.v, 0) AS v "
                    f"FROM {{{x}}} x FULL OUTER JOIN {{{y}}} y ON x.k = y.k",
                    (x, y),
                )
            )
            merged.append(name)
        if len(layer) % 2:
            merged.append(layer[-1])  # an odd one out goes up a layer as it is
        layer = merged

    (top,) = layer
    # Cast to fixed types before hashing: monster-split's temporary tables take
    # their column types from CTAS, which could differ from the CTEs' types and
    # change HASH_AGG even when the values match. Every measure has at most four
    # decimal places, so NUMBER(38, 4) is exact.
    nodes.append(
        Node(
            FINAL,
            "SELECT COUNT(*) AS keys, SUM(v)::NUMBER(38, 4) AS total, "
            f"HASH_AGG(k::NUMBER(38, 0), v::NUMBER(38, 4)) AS fingerprint FROM {{{top}}}",
            (top,),
        )
    )
    return nodes


def _fill(node: Node, refs: dict[str, str]) -> str:
    return node.body.format(**{dep: refs[dep] for dep in node.deps})


def _with(nodes: list[Node], refs: dict[str, str], final: Node) -> str:
    ctes = ",\n".join(f"{node.name} AS (\n    {_fill(node, refs)}\n)" for node in nodes)
    return f"WITH {ctes}\n{_fill(final, refs)}" if nodes else _fill(final, refs)


def render_monolithic(nodes: list[Node]) -> str:
    """The whole DAG as one ``WITH ... SELECT``: every node a CTE, the final SELECT last."""
    *ctes, final = nodes
    return _with(ctes, {node.name: node.name for node in nodes}, final)


def branch_table(prefix: str, branch: str) -> str:
    """The temporary table ``monster-split`` writes a branch to: ``<prefix>_B01``."""
    return f"{prefix}_{branch.upper()}"


def branches(nodes: list[Node]) -> list[str]:
    """The branch names, in order (``b01``, ``b02``, ...)."""
    return list(dict.fromkeys(node.branch for node in nodes if node.branch))


def render_stages(nodes: list[Node], *, prefix: str) -> list[list[tuple[str, str]]]:
    """The same logic as statements, in stages whose statements can all run at once.

    Stage 1 writes each branch to its own temporary table
    (:func:`branch_table`), its stage A inlined as a CTE. Stage 2 is the
    combine layers and the final SELECT, reading those tables: one statement.
    """
    first: list[tuple[str, str]] = []
    outputs: dict[str, str] = {}
    for branch in branches(nodes):
        members = [node for node in nodes if node.branch == branch]
        *inner, last = members
        table = branch_table(prefix, branch)
        refs = {node.name: node.name for node in inner}
        first.append((branch, f"CREATE TEMPORARY TABLE {table} AS\n{_with(inner, refs, last)}"))
        outputs[last.name] = table

    *combine, final = [node for node in nodes if node.branch is None]
    refs = {**outputs, **{node.name: node.name for node in combine}}
    return [first, [("combine", _with(combine, refs, final))]]
