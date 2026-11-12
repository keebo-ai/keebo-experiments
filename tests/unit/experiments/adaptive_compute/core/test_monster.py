"""The monster query's DAG of CTEs, and its two renderings."""

from __future__ import annotations

import re

import pytest

from experiments.adaptive_compute.core import monster

SCHEMA = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF10"
PREFIX = "DB.PUBLIC.T_X"


def _dag(fanout=monster.DEFAULT_FANOUT):
    return monster.build_dag(SCHEMA, fanout=fanout)


@pytest.mark.parametrize("fanout", [2, 3, 7, 16, 32])
def test_the_dag_has_two_ctes_per_branch_then_combines_down_to_one_row(fanout):
    nodes = _dag(fanout)
    assert len(nodes) == 3 * fanout
    assert monster.branches(nodes) == [monster.branch_name(i) for i in range(fanout)]
    assert nodes[-1].name == monster.FINAL
    assert nodes[-1].body.startswith(
        "SELECT COUNT(*) AS keys, SUM(v)::NUMBER(38, 4) AS total, "
        "HASH_AGG(k::NUMBER(38, 0), v::NUMBER(38, 4)) AS fingerprint FROM"
    )


@pytest.mark.parametrize("fanout", [2, 5, 16])
def test_every_cte_is_consumed_and_only_reads_what_comes_before_it(fanout):
    nodes = _dag(fanout)
    seen: set[str] = set()
    consumed: set[str] = set()
    for node in nodes:
        assert set(node.deps) <= seen, node.name
        consumed.update(node.deps)
        seen.add(node.name)
    assert consumed == {node.name for node in nodes[:-1]}


def test_every_branch_is_distinct():
    nodes = _dag(monster.MAX_FANOUT)
    bodies = [node.body for node in nodes if node.branch]
    assert len(set(bodies)) == len(bodies)


def test_every_filter_is_a_real_date_window_inside_the_data():
    windows = [monster.window(i) for i in range(monster.MAX_FANOUT)]
    assert len(set(windows)) == len(windows)
    for lo, hi in windows:
        assert "1992-01-01" <= lo[1:11] < hi[1:11] <= "1998-08-02"
    sql = monster.render_monolithic(_dag())
    for folded in ("1 = 0", "1=0", "FALSE", "WHERE TRUE", "LIMIT 0"):
        assert folded not in sql


def test_the_monolithic_query_is_one_statement_with_every_cte():
    nodes = _dag()
    sql = monster.render_monolithic(nodes)
    assert sql.startswith("WITH b01_a AS (")
    assert sql.count(" AS (\n") == len(nodes) - 1
    assert "{" not in sql and "}" not in sql
    assert ";" not in sql
    assert SCHEMA in sql
    assert sql.rstrip().endswith("FROM c4_01")
    for node in nodes[:-1]:
        # defined once, and read at least once
        assert len(re.findall(rf"\b{node.name}\b", sql)) >= 2, node.name


def test_the_stages_write_each_branch_to_a_temporary_table_then_combine_them():
    nodes = _dag(5)
    first, second = monster.render_stages(nodes, prefix=PREFIX)
    assert [name for name, _ in first] == ["b01", "b02", "b03", "b04", "b05"]
    for name, sql in first:
        table = monster.branch_table(PREFIX, name)
        assert sql.startswith(f"CREATE TEMPORARY TABLE {table} AS\nWITH {name}_a AS (")
        assert "_B0" not in sql.replace(table, "")  # a branch never reads another branch
    ((name, combine),) = second
    assert name == "combine"
    for branch in monster.branches(nodes):
        assert monster.branch_table(PREFIX, branch) in combine
    assert not re.search(r"\bb\d\d_[ab]\b", combine)  # no branch is recomputed in the combine
    assert "{" not in combine and "CREATE" not in combine


def test_both_renderings_run_the_same_logic():
    nodes = _dag(4)
    monolithic = monster.render_monolithic(nodes)
    (*branch_sql,), ((_, combine),) = monster.render_stages(nodes, prefix=PREFIX)
    for _name, sql in branch_sql:
        body = sql.split(" AS\n", 1)[1]
        # The branch's stage A and B read the same in both, up to the CTE wrapping.
        for line in body.splitlines():
            if line.strip() not in ("", ")") and not line.startswith("WITH"):
                assert line.strip() in monolithic
    assert combine.splitlines()[-1] == monolithic.splitlines()[-1]


def test_the_fanout_is_bounded():
    with pytest.raises(ValueError, match="fanout"):
        _dag(1)
    with pytest.raises(ValueError, match="fanout"):
        _dag(monster.MAX_FANOUT + 1)
