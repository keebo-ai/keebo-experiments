"""A tiny tabular result type shared by every experiment's report.

Pure data — no ``click`` — so domain (``core``) code can build report tables
without importing the CLI layer. Rendering lives in :mod:`common.render`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReportTable:
    """One report section: a step number, a title, column names, and rows."""

    step: int
    title: str
    columns: list[str]
    rows: list[tuple[Any, ...]]
