"""Pandas-independent, JSON-schema-friendly Data MCP operation parameters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class DataFrameSource:
    path: str
    alias: str
    time_column: str = "timestamp"
    value_column: str = "value"
    records_path: str | None = None


@dataclass(frozen=True)
class DataFrameInput:
    """One workspace dataset for row-wise concatenation."""

    path: str
    records_path: str | None = None


@dataclass(frozen=True)
class DerivedColumn:
    """One allowlisted calculation; no evaluated expressions or Python code."""

    name: str
    operation: Literal["add", "subtract", "multiply", "divide", "diff", "diff_per_time", "rolling_mean", "cumsum"]
    column: str
    other_column: str | None = None
    constant: float | None = None
    window: int | None = None
    time_column: str = "timestamp"
    time_unit: Literal["seconds", "minutes", "hours"] = "hours"
    max_gap_seconds: float | None = None


@dataclass(frozen=True)
class ResampleAggregation:
    column: str
    method: Literal["mean", "min", "max", "sum", "count"]
    alias: str
