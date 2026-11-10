"""The grid of cells, its run order, and the cost cap."""

from __future__ import annotations

from collections import Counter

import pytest

from experiments.adaptive_compute.core import plan, queries


def _grid(**overrides):
    settings = {
        "configs": queries.CONFIGS,
        "sizes": list(queries.DEFAULT_SIZES),
        "workloads": queries.DEFAULT_WORKLOADS,
        "replicates": 1,
    }
    settings.update(overrides)
    return plan.build_plan(**settings)


def test_the_default_grid_is_every_workload_size_and_config():
    cells = _grid()
    assert len(cells) == 5 * 3 * 3
    assert len(set(cells)) == len(cells)


def test_the_monster_workloads_are_opt_in():
    assert queries.MONSTER not in queries.DEFAULT_WORKLOADS
    assert queries.MONSTER_SPLIT not in queries.DEFAULT_WORKLOADS
    assert set(queries.WORKLOADS) == {*queries.DEFAULT_WORKLOADS, queries.MONSTER, queries.MONSTER_SPLIT}
    assert {w.weight for w in queries.DEFAULT_WORKLOADS} == {1.0}
    assert {w.max_wall_s for w in queries.DEFAULT_WORKLOADS} == {None}


def test_adaptive_ceilings_add_adaptive_only_cells_after_each_workloads_sizes():
    cells = _grid(workloads=[queries.MONSTER, queries.MONSTER_SPLIT], adaptive_ceilings=["XXLARGE", "XLARGE"])
    assert len(cells) == 2 * (9 + 2)
    first = cells[:11]
    assert [(c.size, c.config) for c in first[-2:]] == [("XLARGE", queries.ADAPTIVE), ("XXLARGE", queries.ADAPTIVE)]
    assert {c.workload for c in first} == {queries.MONSTER}
    assert cells[11].workload is queries.MONSTER_SPLIT


def test_a_ceiling_that_is_already_a_size_is_not_run_twice():
    cells = _grid(workloads=[queries.SINGLE], adaptive_ceilings=["MEDIUM"])
    assert len(cells) == 9
    gen1_only = _grid(workloads=[queries.SINGLE], configs=[queries.GEN1], adaptive_ceilings=["MEDIUM"])
    assert [(c.size, c.config.key) for c in gen1_only][-1] == ("MEDIUM", "adaptive")
    with pytest.raises(ValueError, match="size"):
        _grid(adaptive_ceilings=["X4LARGE"])


def test_cells_run_a_workload_at_a_time_smallest_size_first():
    cells = _grid(sizes=["MEDIUM", "XSMALL"])  # given out of order
    assert [c.workload for c in cells[:6]] == [queries.SINGLE] * 6
    assert [c.size for c in cells[:6]] == ["XSMALL"] * 3 + ["MEDIUM"] * 3


def test_each_config_goes_first_equally_often():
    cells = _grid()
    firsts = Counter(cells[i].config.key for i in range(0, len(cells), 3))
    assert set(firsts.values()) == {5}


def test_names_and_tags_identify_the_cell():
    cell = plan.Cell(queries.CONCURRENT, "SMALL", queries.ADAPTIVE, replicate=2)
    assert cell.warehouse("HOME_WH", "20261014_143318") == "HOME_WH_20261014_143318_AD_SMALL_CON_2"
    assert cell.tag("20261014_143318") == "adcomp:20261014_143318:concurrent:SMALL:adaptive:2"
    assert cell.table("DB", "20261014_143318") == "DB.PUBLIC.T_20261014_143318_AD_SMALL_CON_2"


def test_bad_grids_are_rejected():
    with pytest.raises(ValueError, match="at least one"):
        _grid(configs=[])
    with pytest.raises(ValueError, match="replicates"):
        _grid(replicates=0)
    with pytest.raises(ValueError, match="size"):
        _grid(sizes=["HUGE"])


def test_worst_case_rate():
    def rate(workload, config, size="SMALL", max_clusters=2, multiplier=2):
        cell = plan.Cell(workload, size, config)
        return plan.worst_case_rate(cell, max_clusters=max_clusters, throughput_multiplier=multiplier)

    assert rate(queries.SEQUENTIAL, queries.GEN1) == 2
    assert rate(queries.SEQUENTIAL, queries.GEN2) == pytest.approx(2.7)
    assert rate(queries.CONCURRENT, queries.GEN1, max_clusters=4) == 8
    # Adaptive is assumed to bill no faster than Gen2 at its level, times its burst.
    assert rate(queries.SEQUENTIAL, queries.ADAPTIVE) == pytest.approx(2.7)
    assert rate(queries.CONCURRENT, queries.ADAPTIVE, multiplier=2) == pytest.approx(5.4)
    assert rate(queries.CONCURRENT, queries.ADAPTIVE, multiplier=0) == pytest.approx(2.7 * 8)


def test_standard_cells_carry_the_60_second_minimum_and_adaptive_doesnt():
    gen1 = plan.Cell(queries.SINGLE, "XSMALL", queries.GEN1)
    adaptive = plan.Cell(queries.SINGLE, "XSMALL", queries.ADAPTIVE)
    assert plan.minimum_credits(gen1) == pytest.approx(1 / 60)
    assert plan.minimum_credits(adaptive) == 0
    kw = {"max_clusters": 1, "throughput_multiplier": 2}
    assert plan.worst_case_credits(gen1, 10, **kw) == pytest.approx(1 / 60)
    assert plan.worst_case_credits(adaptive, 10, **kw) == pytest.approx(1.35 * 10 / 3600)


def test_wall_limit_splits_the_budget_evenly_and_clamps():
    cell = plan.Cell(queries.SEQUENTIAL, "XSMALL", queries.GEN1)
    kw = {"max_clusters": 2, "throughput_multiplier": 2}
    assert plan.wall_limit(cell, budget=1.0, weight_left=10, **kw) == 360
    assert plan.wall_limit(cell, budget=100, weight_left=1, **kw) == queries.MAX_WALL_SECONDS
    with pytest.raises(ValueError, match="Raise --max-credits"):
        plan.wall_limit(cell, budget=0.1, weight_left=10, **kw)


def test_without_a_monster_workload_every_limit_is_the_even_split_it_always_was():
    cells = _grid(replicates=2)
    kw = {"max_clusters": 2, "throughput_multiplier": 2}
    budget = 25.0
    for index, cell in enumerate(cells):
        cells_left = len(cells) - index
        even = min(int(budget / cells_left * 3600 / plan.worst_case_rate(cell, **kw)), queries.MAX_WALL_SECONDS)
        assert plan.wall_limit(cell, budget=budget, weight_left=plan.weight_of(cells[index:]), **kw) == even


def test_a_monster_cell_gets_its_weight_in_budget_and_a_longer_ceiling():
    cells = _grid(workloads=[queries.SINGLE, queries.MONSTER], sizes=["LARGE"], configs=[queries.GEN1])
    kw = {"max_clusters": 2, "throughput_multiplier": 2}
    total = plan.weight_of(cells)
    assert total == 1 + queries.MONSTER.weight
    single, monster = (plan.wall_limit(c, budget=1.0, weight_left=total, **kw) for c in cells)
    assert monster == pytest.approx(single * queries.MONSTER.weight, abs=1)
    roomy = plan.wall_limit(cells[1], budget=100.0, weight_left=total, **kw)
    assert roomy == queries.MONSTER.max_wall_s > queries.MAX_WALL_SECONDS


def test_monster_split_is_single_cluster_unless_asked():
    cell = plan.Cell(queries.MONSTER_SPLIT, "SMALL", queries.GEN1)
    assert plan.clusters(cell, max_clusters=4) == 1
    assert plan.clusters(cell, max_clusters=4, split_max_clusters=3) == 3
    assert plan.clusters(plan.Cell(queries.CONCURRENT, "SMALL", queries.GEN1), max_clusters=4) == 4
    assert plan.clusters(plan.Cell(queries.MONSTER, "SMALL", queries.GEN1), max_clusters=4) == 1
    assert plan.worst_case_rate(cell, max_clusters=4, throughput_multiplier=2) == 2
    # Adaptive bursts on any workload that submits statements at once.
    adaptive = plan.Cell(queries.MONSTER_SPLIT, "SMALL", queries.ADAPTIVE)
    assert plan.worst_case_rate(adaptive, max_clusters=1, throughput_multiplier=2) == pytest.approx(5.4)


def test_summary_of_the_default_grid():
    summary = plan.summarize(_grid(), max_credits=10, max_clusters=2, throughput_multiplier=2)
    assert (summary.cells, summary.standard_cells, summary.adaptive_cells) == (45, 30, 15)
    # 5 workloads x (1+2+4 Gen1 + 1.35+2.7+5.4 Gen2) credits/hr x 60 s
    assert summary.minimum_credits == pytest.approx(5 * 16.45 / 60, abs=0.001)
    assert summary.tightest_wall_s >= queries.MIN_WALL_SECONDS


def test_summary_rejects_settings_snowflake_rejects():
    with pytest.raises(ValueError, match="throughput multiplier"):
        plan.summarize(_grid(), max_credits=10, max_clusters=2, throughput_multiplier=1)
    with pytest.raises(ValueError, match="max clusters"):
        plan.summarize(_grid(), max_credits=10, max_clusters=0, throughput_multiplier=2)
