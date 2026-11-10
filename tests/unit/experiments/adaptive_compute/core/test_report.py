"""Turning a run into tables, crossovers, and CSV rows."""

from __future__ import annotations

import pytest

from experiments.adaptive_compute.core import plan, queries, report, run

from ..conftest import OBJECTS, RUN_ID, cell_row


def _stats(config, *, workload="single", size="SMALL", wall_s=5.0, busy_s=None, billed=None, **extra):
    values = {
        "workload": workload,
        "size": size,
        "config": config,
        "replicates": 1,
        "statements": 1,
        "failed": 0,
        "distinct_hashes": 1,
        "wall_s": wall_s,
        "elapsed_s": wall_s,
        "queued_s": 0.0,
        "clusters": 1,
        "busy_s": wall_s if busy_s is None else busy_s,
        "floor_s": max(60.0, wall_s),
        "billed": billed,
        "query_metered": None,
    }
    values.update(extra)
    return report.CellStats(**values)


def test_parse_tag():
    assert report.parse_tag(f"adcomp:{RUN_ID}:dml:MEDIUM:gen2:3") == ("dml", "MEDIUM", "gen2", 3)
    assert report.parse_tag("wsbench:20261001-143318:XSMALL:1") is None
    assert report.parse_tag(f"adcomp:{RUN_ID}:nope:MEDIUM:gen2:1") is None


def test_warm_cost_is_busy_time_at_the_published_rate_and_adaptive_is_its_bill():
    gen2 = _stats(queries.GEN2, size="MEDIUM", busy_s=100, floor_s=100.0, billed=0.2)
    assert gen2.rate == pytest.approx(5.4)
    assert gen2.warm == pytest.approx(100 * 5.4 / 3600)
    assert gen2.modelled == pytest.approx(100 * 5.4 / 3600)
    adaptive = _stats(queries.ADAPTIVE, billed=0.07)
    assert adaptive.rate is None and adaptive.warm == 0.07 and adaptive.modelled is None


def test_break_even_when_adaptive_costs_more_than_a_fresh_warehouse():
    # Small Gen1 (2 credits/hr): a 5 s query billed at the 60-second minimum. Adaptive billed 0.05.
    gen1 = _stats(queries.GEN1, billed=2 * 60 / 3600)
    adaptive = _stats(queries.ADAPTIVE, wall_s=4.0, billed=0.05)

    c = report.compare_cells(adaptive, gen1)

    assert c.speedup == pytest.approx(1.25)
    assert c.fresh_ratio == pytest.approx(1.5)
    # Kept running, Gen1 bills 5 s of work per run divided by how busy it is: cheaper above 5.6% busy.
    assert c.breakeven_busy == pytest.approx((5 * 2 / 3600) / 0.05)
    # Run on its own, Gen1's idle tail first fills the 55 s left of its minimum, then costs 2 credits/hr.
    assert c.breakeven_auto_suspend_s == pytest.approx(55 + (0.05 - 2 / 60) * 3600 / 2)


def test_break_even_when_adaptive_is_cheaper_even_flat_out():
    gen1 = _stats(queries.GEN1, workload="sequential", size="XSMALL", wall_s=120, billed=120 / 3600)
    adaptive = _stats(queries.ADAPTIVE, workload="sequential", size="XSMALL", wall_s=100, billed=0.025)

    c = report.compare_cells(adaptive, gen1)

    assert c.breakeven_busy > 1
    assert c.breakeven_auto_suspend_s == 0
    assert report._busy_text(c) == "never"
    assert report._auto_suspend_text(c) == "any (0s)"


def test_standard_against_standard_has_no_break_even():
    c = report.compare_cells(_stats(queries.GEN2, billed=0.04), _stats(queries.GEN1, billed=0.03))
    assert c.breakeven_busy is None and c.breakeven_auto_suspend_s is None
    assert report._busy_text(c) == ""


def test_costs_wait_for_the_bill():
    c = report.compare_cells(_stats(queries.ADAPTIVE), _stats(queries.GEN1, billed=0.03))
    assert c.fresh_ratio is None and c.breakeven_busy is None
    assert c.speedup == pytest.approx(1.0)


def test_aggregate_takes_the_median_and_waits_for_every_replicates_bill():
    rows = [
        dict(zip(_columns(), cell_row("single", "XSMALL", "gen1", replicate, wall_s=wall), strict=True))
        for replicate, wall in ((1, 10.0), (2, 30.0), (3, 11.0))
    ]
    billed = {rows[0]["warehouse_name"]: 0.02, rows[1]["warehouse_name"]: 0.03}

    (cell,) = report.aggregate(rows, billed=billed, metered={})

    assert (cell.replicates, cell.wall_s) == (3, 11.0)
    assert cell.billed is None  # replicate 3's bill hasn't landed


def _columns():
    from ..conftest import CELL_COLUMNS

    return CELL_COLUMNS


def test_sentences_name_the_sizes_on_each_side_of_the_line():
    sentence = report._sizes_sentence(
        "faster", "slower", "the same speed", [("XSMALL", 1.4, True), ("SMALL", 1.02, True), ("MEDIUM", 0.7, True)]
    )
    assert sentence == "faster at X-Small, slower at Medium, the same speed at Small"


def test_break_even_sentences_read_naturally():
    gen1_small = _stats(queries.GEN1, billed=2 * 60 / 3600)
    gen1_xs = _stats(queries.GEN1, size="XSMALL", wall_s=120, billed=120 / 3600)
    group = [
        report.compare_cells(_stats(queries.ADAPTIVE, size="XSMALL", wall_s=100, billed=0.025), gen1_xs),
        report.compare_cells(_stats(queries.ADAPTIVE, wall_s=4.0, billed=0.05), gen1_small),
    ]
    assert report._busy_sentence("Gen1", group) == (
        "a Gen1 warehouse that's kept running beats it above 6% busy at Small, and never at X-Small, even flat out"
    )
    assert report._auto_suspend_sentence("Gen1", group) == (
        "run on its own, it's cheaper than Gen1 at any AUTO_SUSPEND at X-Small, and once AUTO_SUSPEND is above "
        "85s at Small"
    )


def _full_account(account, *, billed=True):
    cells, metering, query_metering = [], {}, {}
    for size, scale in (("XSMALL", 1), ("SMALL", 2)):
        for config, wall in (("gen1", 30 / scale), ("gen2", 22 / scale), ("adaptive", 25 / scale)):
            row = cell_row("sequential", size, config, statements=22, wall_s=wall, elapsed_s=wall, busy_s=wall,
                           floor_s=max(60, wall))  # fmt: skip
            cells.append(row)
            if billed:
                metering[row[1]] = 0.01 * scale if config != "gen2" else 0.0135 * scale
            if config == "adaptive":
                query_metering[row[0]] = 0.01 * scale
    return account(cells=cells, metering=metering, query_metering=query_metering)


def test_read_report_reads_the_latest_run_on_the_home_warehouse(account):
    cursor, conn, _fake = _full_account(account)

    summary = report.read_report(conn, objects=OBJECTS)

    assert summary.run_id == RUN_ID
    assert f"USE WAREHOUSE {OBJECTS.warehouse}" in cursor.executed
    assert f"ALTER WAREHOUSE {OBJECTS.warehouse} SUSPEND" in cursor.executed
    assert [t.step for t in summary.tables] == [1, 2, 3, 4]
    assert summary.workload_comparisons == []
    assert len(summary.cells) == 6
    assert len(summary.comparisons) == 6  # 2 sizes x 3 pairs
    assert any(line.startswith("sequential, Adaptive vs Gen1: faster at X-Small, Small") for line in summary.lines)
    assert any("One replicate per cell" in line for line in summary.lines)


def test_read_report_says_what_is_still_missing(account):
    _cursor, conn, _fake = _full_account(account, billed=False)
    summary = report.read_report(conn, objects=OBJECTS)
    assert summary.lines[0].startswith("6 of 6 cells have no metering yet")


def test_read_report_carries_on_without_query_metering(account):
    _cursor, conn, fake = _full_account(account)
    fake.fail["QUERY_METERING_HISTORY"] = [RuntimeError("not authorized")]
    summary = report.read_report(conn, objects=OBJECTS)
    assert "Adaptive's per-query check is skipped" in summary.lines[-1]


def test_read_report_returns_none_before_any_run_shows_up(account):
    _cursor, conn, _fake = account(latest_run=None)
    assert report.read_report(conn, objects=OBJECTS) is None


def test_read_report_validates_its_inputs(account):
    _cursor, conn, _fake = account()
    with pytest.raises(ValueError, match="run id"):
        report.read_report(conn, objects=OBJECTS, run_id="latest")
    with pytest.raises(ValueError, match="hours"):
        report.read_report(conn, objects=OBJECTS, hours=0)


def test_checks_compare_the_bill_with_the_billing_rules(account):
    _cursor, conn, _fake = _full_account(account)
    summary = report.read_report(conn, objects=OBJECTS)
    checks = {(row[1], row[2]): row for row in summary.tables[2].rows}
    # X-Small Gen1: 30 s of work billed at the 60-second minimum = 1/60 credits, but 0.01 was billed.
    assert checks[("X-Small", "Gen1")][6] == f"{0.01 / (1 / 60):.2f}"
    assert checks[("X-Small", "Adaptive")][6] == "1.00"


def test_csv_rows_match_their_columns(account):
    _cursor, conn, _fake = _full_account(account)
    cells, comparisons = report.csv_rows(report.read_report(conn, objects=OBJECTS))
    assert {len(r) for r in cells} == {len(report.CELL_CSV_COLUMNS)}
    assert {len(r) for r in comparisons} == {len(report.COMPARISON_CSV_COLUMNS)}


def _cell_run(config, wall, *, capped=0, workload=queries.SINGLE):
    return run.CellRun(
        cell=plan.Cell(workload, "XSMALL", config),
        warehouse="W",
        wall_limit_s=300,
        wall_s=wall,
        up_s=wall + 2,
        statements=1,
        succeeded=1 - capped,
        capped=capped,
        failed=0,
        worst_case_credits=0.02,
    )


def test_live_tables_put_the_configs_side_by_side():
    result = run.RunResult(
        run_id=RUN_ID,
        scale=10,
        max_clusters=2,
        throughput_multiplier=2,
        cells=(_cell_run(queries.GEN1, 10.0), _cell_run(queries.GEN2, 8.0), _cell_run(queries.ADAPTIVE, 5.0, capped=1)),
    )

    cells, side = report.live_tables(result)

    assert cells.rows[2][-1] == "report"  # Adaptive's cost waits for the bill
    assert side.rows == [("single", "X-Small", "10.0", "8.0", "5.0+", "2.0x faster", "1.6x faster")]
    lines = report.live_summary_lines(result)
    assert "--run-id 20261014_143318" in lines[1]
    assert any("marked +" in line for line in lines)


def test_checks_flag_a_cell_that_ran_different_work():
    cells = [
        _stats(queries.GEN1, workload="sequential", distinct_hashes=22, billed=0.01),
        _stats(queries.GEN2, workload="sequential", distinct_hashes=22, billed=0.01),
        _stats(queries.ADAPTIVE, workload="sequential", distinct_hashes=21, billed=0.01),
    ]
    notes = [row[-1] for row in report._checks_table(cells).rows]
    assert notes == ["", "", "21 distinct statements, others ran 22"]


def test_a_single_cluster_concurrent_estimate_is_not_a_lower_bound():
    c = _cell_run(queries.GEN1, 10.0, workload=queries.CONCURRENT)
    assert report._lower_bound(c, 2) is True
    assert report._lower_bound(c, 1) is False


def test_aggregate_carries_where_the_time_went_and_the_spill():
    rows = [
        dict(zip(_columns(), cell_row("monster", "XSMALL", "gen1", compile_s=2.5, exec_s=80.0,
                                      spilled_local_gb=1.5, spilled_remote_gb=0.25), strict=True))
    ]  # fmt: skip

    (cell,) = report.aggregate(rows, billed={}, metered={})

    assert (cell.compile_s, cell.exec_s, cell.spilled_local_gb, cell.spilled_remote_gb) == (2.5, 80.0, 1.5, 0.25)
    table = report._cells_table([cell])
    row = dict(zip(table.columns, table.rows[0], strict=True))
    assert (row["compile (s)"], row["exec (s)"], row["spill local GB"], row["spill remote GB"]) == (
        "2.5", "80.0", "1.50", "0.25",
    )  # fmt: skip


def _monster_account(account):
    cells, metering = [], {}
    for workload, wall in (("monster", 40.0), ("monster-split", 20.0)):
        for config in ("gen1", "adaptive"):
            row = cell_row(workload, "SMALL", config, wall_s=wall, busy_s=wall, floor_s=60.0,
                           compile_s=12.0 if workload == "monster" else 0.5, exec_s=wall - 4)  # fmt: skip
            cells.append(row)
            metering[row[1]] = 2 * 60 / 3600 if config == "gen1" else wall / 1000
    return account(cells=cells, metering=metering)


def test_monster_split_is_set_against_monster_on_the_same_config_and_size(account):
    _cursor, conn, _fake = _monster_account(account)

    summary = report.read_report(conn, objects=OBJECTS)

    pairs = summary.workload_comparisons
    assert [(c.workload, c.against, c.challenger.key, c.baseline.key) for c in pairs] == [
        ("monster-split", "monster", "gen1", "gen1"),
        ("monster-split", "monster", "adaptive", "adaptive"),
    ]
    assert pairs[0].speedup == pytest.approx(2.0)
    assert pairs[0].breakeven_busy is None  # same configuration: no break-even
    titles = [t.title for t in summary.tables]
    assert titles[2].startswith("Step 2b. Split vs one statement")
    assert summary.tables[2].rows[0][:4] == ("monster-split vs monster", "Small", "Gen1", "2.0x faster")
    assert "monster-split vs monster, on Adaptive: faster at Small; cheaper as a fresh warehouse at Small." in (
        summary.lines
    )
    assert any("spent at least a quarter as long compiling as executing" in line for line in summary.lines)


def test_the_csvs_carry_the_workload_pairs_and_the_new_columns(account):
    _cursor, conn, _fake = _monster_account(account)
    summary = report.read_report(conn, objects=OBJECTS)

    cells, comparisons = report.csv_rows(summary)

    assert {len(r) for r in cells} == {len(report.CELL_CSV_COLUMNS)}
    assert {len(r) for r in comparisons} == {len(report.COMPARISON_CSV_COLUMNS)}
    against = report.COMPARISON_CSV_COLUMNS.index("baseline_workload")
    # monster's own Adaptive vs Gen1, then monster-split's, then the two workload pairs.
    assert [r[against] for r in comparisons] == ["monster", "monster-split", "monster", "monster"]
    compile_s = report.CELL_CSV_COLUMNS.index("compile_s")
    assert cells[0][compile_s] == 12.0


def test_the_frontier_keeps_only_cells_nothing_beats_on_both_time_and_cost():
    cheap_slow = _stats(queries.GEN1, workload="monster", size="XSMALL", wall_s=100, billed=0.03)
    dear_fast = _stats(queries.ADAPTIVE, workload="monster", size="XLARGE", wall_s=10, billed=0.2)
    beaten = _stats(queries.GEN2, workload="monster", size="SMALL", wall_s=120, billed=0.05)
    other_workload = _stats(queries.GEN2, workload="single", wall_s=500, billed=0.9)
    failed = _stats(queries.GEN1, workload="monster", wall_s=1, billed=0.001, failed=1)
    waiting = _stats(queries.GEN1, workload="monster", size="MEDIUM", wall_s=5)

    cells = [cheap_slow, dear_fast, beaten, other_workload, failed, waiting]
    assert report.frontier(cells) == {cheap_slow, dear_fast, other_workload}

    table = report._frontier_table(cells)
    assert table.step == 4
    monster_rows = [row for row in table.rows if row[0] == "monster"]
    assert [(row[1], row[-1]) for row in monster_rows] == [
        ("Small", ""),  # the failed one, ordered by bill but never on the frontier
        ("X-Small", "yes"),
        ("Small", ""),
        ("X-Large", "yes"),
    ]


def test_monster_split_estimates_are_exact_on_one_cluster():
    c = _cell_run(queries.GEN1, 10.0, workload=queries.MONSTER_SPLIT)
    assert report._lower_bound(c, 4) is False
    assert report._lower_bound(c, 4, 2) is True


def test_the_live_summary_warns_about_mismatched_answers():
    result = run.RunResult(
        run_id=RUN_ID,
        scale=10,
        max_clusters=2,
        throughput_multiplier=2,
        cells=(_cell_run(queries.GEN1, 10.0, workload=queries.MONSTER),),
        mismatched_answers=("monster-split / X-Small / Gen1",),
    )
    assert any(line.startswith("WARNING: these monster cells") for line in report.live_summary_lines(result))
