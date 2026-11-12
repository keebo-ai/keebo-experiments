"""The grid of cells, its run order, and the cost cap."""

from __future__ import annotations

from collections import Counter

import pytest

from experiments.adaptive_compute.core import plan, queries


def _grid(**overrides):
    settings = {
        "configs": queries.CONFIGS,
        "sizes": list(queries.DEFAULT_SIZES),
        "workloads": queries.WORKLOADS,
        "replicates": 1,
    }
    settings.update(overrides)
    return plan.build_plan(**settings)


def test_the_default_grid_is_every_workload_size_and_config():
    cells = _grid()
    assert len(cells) == 5 * 3 * 3
    assert len(set(cells)) == len(cells)


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
    assert plan.wall_limit(cell, budget=1.0, cells_left=10, **kw) == 360
    assert plan.wall_limit(cell, budget=100, cells_left=1, **kw) == queries.MAX_WALL_SECONDS
    with pytest.raises(ValueError, match="Raise --max-credits"):
        plan.wall_limit(cell, budget=0.1, cells_left=10, **kw)


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
