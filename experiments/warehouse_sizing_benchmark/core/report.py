"""Read timings and credits back from ACCOUNT_USAGE (Steps 10-16).

Takes an open connection and returns structured results; the CLI formats them.
No ``click`` here.
"""

from __future__ import annotations

from typing import Any

from common.tables import ReportTable
from experiments.warehouse_sizing_benchmark.core import queries


def read_report(
    conn: Any,
    *,
    warehouse_name: str = queries.DEFAULT_WAREHOUSE,
    hours: int = 6,
) -> list[ReportTable]:
    """Run each reporting query and return one :class:`ReportTable` per step.

    Empty ``rows`` mean ACCOUNT_USAGE hasn't caught up yet — wait and rerun.
    """
    queries.validate_identifier(warehouse_name, "warehouse")
    hours = int(hours)

    cur = conn.cursor()
    tables: list[ReportTable] = []
    try:
        for step, title, sql in queries.REPORT_STEPS:
            cur.execute(sql.format(hours=hours, wh=warehouse_name))
            columns = [col[0] for col in cur.description]
            tables.append(ReportTable(step, title, columns, list(cur.fetchall())))
    finally:
        cur.close()
    return tables
