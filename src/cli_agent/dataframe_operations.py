"""Bounded, declarative pandas operations for workspace-local time series.

The result is returned as MCP structured content. cli-agent's existing
McpResultArtifacts mechanism persists it for the current session, forwards
only the requested metadata and deletes it on session close.
"""

from __future__ import annotations

import json
import math
import re
from typing import Literal

import pandas as pd
from mcp.types import CallToolResult, TextContent

from .data_operations import DataOperationError, DataOperations
from .dataframe_types import DataFrameSource, DerivedColumn, ResampleAggregation
from .data_result_metadata import INLINE_RESULT_BYTES
from .mcp_result_artifacts import METADATA_FOR_LLM_KEY, ONLY_METADATA_TO_LLM_KEY

MAX_FRAME_ROWS = 100_000
MAX_FRAME_COLUMNS = 100
MAX_FRAME_RESULT_CHARS = 8_000_000
MAX_FRAME_SOURCES = 8
MAX_FRAME_TRANSFORMS = 20
_FREQUENCY = re.compile(r"[1-9][0-9]{0,3}(?:s|min|h|d)\Z")


class DataFrameOperations:
    def __init__(self, base: DataOperations) -> None:
        if base.workspace is None:
            raise DataOperationError("DataFrame-Tools benötigen Workspace-Dateizugriff.")
        self.base = base

    def _read_frame(self, path: str, records_path: str | None) -> pd.DataFrame:
        columns, rows = self.base._read_records(path, records_path=records_path)
        if len(rows) > MAX_FRAME_ROWS or len(columns) > MAX_FRAME_COLUMNS:
            raise DataOperationError(
                f"DataFrame-Eingabe ist zu groß (maximal {MAX_FRAME_ROWS} Zeilen, "
                f"{MAX_FRAME_COLUMNS} Spalten). Filtere die Quelle zunächst."
            )
        return pd.DataFrame.from_records(rows, columns=columns)

    @staticmethod
    def _timestamps(frame: pd.DataFrame, column: str) -> pd.Series:
        if column not in frame:
            raise DataOperationError(f"Zeitstempelspalte fehlt: {column!r}.")
        if frame[column].isna().any():
            raise DataOperationError("Zeitstempel dürfen nicht leer sein.")
        try:
            timestamps = pd.to_datetime(frame[column], errors="raise")
        except (TypeError, ValueError, OverflowError) as exc:
            raise DataOperationError("Zeitstempel sind nicht einheitlich parsebar.") from exc
        if not pd.api.types.is_datetime64_any_dtype(timestamps.dtype):
            raise DataOperationError("Zeitstempel benötigen eine einheitliche Zeitzone.")
        return timestamps

    @staticmethod
    def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
        if column not in frame:
            raise DataOperationError(f"Numerische Spalte fehlt: {column!r}.")
        try:
            values = pd.to_numeric(frame[column], errors="raise").astype("float64")
        except (TypeError, ValueError, OverflowError) as exc:
            raise DataOperationError(f"Spalte {column!r} enthält nichtnumerische Werte.") from exc
        if values.notna().any() and not values.dropna().map(math.isfinite).all():
            raise DataOperationError(f"Spalte {column!r} enthält nichtendliche Werte.")
        return values

    @staticmethod
    def _result(frame: pd.DataFrame, *, operation: str) -> CallToolResult:
        if len(frame) > MAX_FRAME_ROWS or len(frame.columns) > MAX_FRAME_COLUMNS:
            raise DataOperationError("DataFrame-Ergebnis überschreitet das Zeilen-/Spaltenlimit.")
        if any(not isinstance(column, str) or not column for column in frame.columns):
            raise DataOperationError("DataFrame enthält ungültige Spaltennamen.")
        if not frame.columns.is_unique:
            raise DataOperationError("DataFrame enthält doppelte Spaltennamen.")
        # pandas handles its own scalar types and timestamps here; the JSON
        # payload is checked before it reaches the agent's MCP result limits.
        try:
            # Preserve sub-millisecond timestamps for subsequent exact joins.
            rows = json.loads(frame.to_json(orient="records", date_format="iso", date_unit="ns", double_precision=15))
            payload = {"columns": list(frame.columns), "rows": rows}
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, OverflowError) as exc:
            raise DataOperationError("DataFrame kann nicht als JSON serialisiert werden.") from exc
        if len(encoded) > MAX_FRAME_RESULT_CHARS or len(encoded.encode("utf-8")) > MAX_FRAME_RESULT_CHARS:
            raise DataOperationError(
                "DataFrame-Ergebnis ist für einen einzelnen MCP-Aufruf zu groß. "
                "Wähle einen kürzeren Zeitraum oder aggregiere die Daten."
            )
        metadata = {
            "operation": operation,
            "row_count": len(frame),
            "columns": list(frame.columns),
            "records_path": "/rows",
            "format": "json",
        }
        if len(encoded.encode("utf-8")) <= INLINE_RESULT_BYTES:
            metadata["rows"] = rows
        return CallToolResult(
            content=[TextContent(type="text", text="DataFrame-Ergebnis für Session-Datei vorbereitet.")],
            structuredContent=payload,
            **{"_meta": {
                ONLY_METADATA_TO_LLM_KEY: True,
                METADATA_FOR_LLM_KEY: metadata,
            }},
        )

    def join_data(
        self,
        sources: list[DataFrameSource],
        *,
        direction: Literal["exact", "nearest", "backward", "forward"] = "exact",
        how: Literal["inner", "left", "outer"] = "inner",
        tolerance_seconds: float | None = None,
    ) -> CallToolResult:
        if not 2 <= len(sources) <= MAX_FRAME_SOURCES:
            raise DataOperationError("join_data erwartet 2 bis 8 Quellen.")
        aliases = [source.alias for source in sources]
        if any(not alias or alias == "timestamp" or not alias.isidentifier() for alias in aliases) or len(set(aliases)) != len(aliases):
            raise DataOperationError("Jede Quelle benötigt einen eindeutigen Alias (Identifier, nicht 'timestamp').")
        if direction != "exact":
            if how != "left":
                raise DataOperationError("Tolerierte Joins sind ausschließlich left-Joins.")
            if tolerance_seconds is None or isinstance(tolerance_seconds, bool) or not math.isfinite(tolerance_seconds) or not 0 <= tolerance_seconds <= 86_400:
                raise DataOperationError("Tolerierte Joins benötigen tolerance_seconds zwischen 0 und 86400.")
        elif tolerance_seconds is not None:
            raise DataOperationError("tolerance_seconds ist nur für tolerierte Joins gültig.")

        frames: list[pd.DataFrame] = []
        for source in sources:
            raw = self._read_frame(source.path, source.records_path)
            times = self._timestamps(raw, source.time_column)
            if times.duplicated().any():
                raise DataOperationError(f"Quelle {source.alias!r} enthält doppelte Zeitstempel.")
            values = self._numeric(raw, source.value_column)
            frame = pd.DataFrame({"timestamp": times, source.alias: values})
            frames.append(frame.sort_values("timestamp").reset_index(drop=True))

        joined = frames[0]
        try:
            for frame in frames[1:]:
                if direction == "exact":
                    joined = joined.merge(frame, on="timestamp", how=how, validate="one_to_one", sort=True)
                else:
                    joined = pd.merge_asof(
                        joined.sort_values("timestamp"),
                        frame,
                        on="timestamp",
                        direction=direction,
                        tolerance=pd.Timedelta(seconds=tolerance_seconds),
                    )
                if len(joined) > MAX_FRAME_ROWS:
                    raise DataOperationError("Join-Ergebnis überschreitet das Zeilenlimit.")
        except (ValueError, TypeError) as exc:
            raise DataOperationError(f"Zeitreihen konnten nicht verknüpft werden: {exc}") from exc
        return self._result(joined, operation="join_data")

    def derive_columns(
        self,
        path: str,
        operations: list[DerivedColumn],
        *,
        records_path: str | None = None,
    ) -> CallToolResult:
        if not 1 <= len(operations) <= MAX_FRAME_TRANSFORMS:
            raise DataOperationError("derive_columns erwartet 1 bis 20 Transformationen.")
        frame = self._read_frame(path, records_path)
        for spec in operations:
            if not spec.name or spec.name in frame:
                raise DataOperationError(f"Ergebnisspalte ist leer oder bereits vorhanden: {spec.name!r}.")
            left = self._numeric(frame, spec.column)
            if spec.operation in {"add", "subtract", "multiply", "divide"}:
                if (spec.other_column is None) == (spec.constant is None):
                    raise DataOperationError("Binäre Operation benötigt exakt other_column oder constant.")
                if spec.other_column is not None:
                    right = self._numeric(frame, spec.other_column)
                else:
                    if isinstance(spec.constant, bool) or not isinstance(spec.constant, (int, float)) or not math.isfinite(spec.constant):
                        raise DataOperationError("constant muss eine endliche Zahl sein.")
                    right = spec.constant
                if spec.operation == "add":
                    result = left + right
                elif spec.operation == "subtract":
                    result = left - right
                elif spec.operation == "multiply":
                    result = left * right
                else:
                    if (right == 0).any() if isinstance(right, pd.Series) else right == 0:
                        raise DataOperationError("Division durch null ist nicht zulässig.")
                    result = left / right
            elif spec.operation == "diff":
                result = left.diff()
            elif spec.operation == "cumsum":
                result = left.cumsum()
            elif spec.operation == "rolling_mean":
                if isinstance(spec.window, bool) or not isinstance(spec.window, int) or not 2 <= spec.window <= 10_000:
                    raise DataOperationError("rolling_mean benötigt window zwischen 2 und 10000 (Zeilen).")
                result = left.rolling(spec.window, min_periods=spec.window).mean()
            else:
                raise DataOperationError(f"Nicht unterstützte Operation: {spec.operation!r}.")
            if result.notna().any() and not result.dropna().map(math.isfinite).all():
                raise DataOperationError("Berechnung erzeugte ein nichtendliches Ergebnis.")
            frame[spec.name] = result
        return self._result(frame, operation="derive_columns")

    def resample_data(
        self,
        path: str,
        frequency: str,
        aggregations: list[ResampleAggregation],
        *,
        time_column: str = "timestamp",
        records_path: str | None = None,
    ) -> CallToolResult:
        if not isinstance(frequency, str) or not _FREQUENCY.fullmatch(frequency):
            raise DataOperationError("frequency muss z.B. '5min', '30s', '1h' oder '1d' sein.")
        if not 1 <= len(aggregations) <= MAX_FRAME_TRANSFORMS:
            raise DataOperationError("resample_data erwartet 1 bis 20 Aggregationen.")
        aliases = [spec.alias for spec in aggregations]
        if any(not alias or alias == "timestamp" for alias in aliases) or len(set(aliases)) != len(aliases):
            raise DataOperationError("Aggregationsalias muss eindeutig und nicht 'timestamp' sein.")
        frame = self._read_frame(path, records_path)
        frame[time_column] = self._timestamps(frame, time_column)
        specs: dict[str, tuple[str, str]] = {}
        for spec in aggregations:
            if spec.column == time_column:
                raise DataOperationError("Zeitstempel können nicht numerisch aggregiert werden.")
            frame[spec.column] = self._numeric(frame, spec.column)
            if spec.method not in {"mean", "min", "max", "sum", "count"}:
                raise DataOperationError(f"Unbekannte Aggregation: {spec.method!r}.")
            specs[spec.alias] = (spec.column, spec.method)
        try:
            resampler = frame.set_index(time_column).sort_index().resample(
                frequency, closed="left", label="left"
            )
            grouped = resampler.agg(**specs)
            empty = resampler.size() == 0
            for spec in aggregations:
                if spec.method == "sum":
                    # pandas.sum defaults to 0 for an empty bucket, which
                    # would invent a measurement where none was recorded.
                    grouped.loc[empty, spec.alias] = float("nan")
            grouped = grouped.reset_index()
        except (ValueError, TypeError) as exc:
            raise DataOperationError(f"Resampling fehlgeschlagen: {exc}") from exc
        if time_column != "timestamp":
            grouped = grouped.rename(columns={time_column: "timestamp"})
        return self._result(grouped, operation="resample_data")
