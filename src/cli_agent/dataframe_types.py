"""Pandas-independent, JSON-schema-friendly Data MCP operation parameters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field


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
    operation: Annotated[Literal["add", "subtract", "multiply", "divide", "diff", "diff_per_time", "rolling_mean", "cumsum"], Field(description="diff is row-to-row; diff_per_time is time-normalized and requires max_gap_seconds.")]
    column: str
    other_column: str | None = None
    constant: float | None = None
    window: int | None = None
    time_column: Annotated[str, Field(description="Only relevant to diff_per_time; ignored for row-based diff.")] = "timestamp"
    time_unit: Annotated[Literal["seconds", "minutes", "hours"], Field(description="Only relevant to diff_per_time.")] = "hours"
    max_gap_seconds: Annotated[float | None, Field(description="Required for diff_per_time; not allowed for other operations.")] = None


@dataclass(frozen=True)
class ResampleAggregation:
    column: str
    method: Literal["mean", "min", "max", "sum", "count"]
    alias: str
