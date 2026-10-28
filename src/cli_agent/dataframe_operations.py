"""Bounded, declarative pandas operations for workspace-local time series.

The result is returned as MCP structured content. cli-agent's existing
McpResultArtifacts mechanism persists it for the current session, forwards
only the requested metadata and deletes it at the next regular request or
session close.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from decimal import Decimal
from typing import Literal

import pandas as pd
from mcp.types import CallToolResult

from .data_operations import DataOperationError, DataOperations, _scalar_identity, _validate_column_name
from .dataframe_types import DataFrameInput, DataFrameSource, DerivedColumn, ResampleAggregation
from .data_result_metadata import structured_table_result

MAX_FRAME_ROWS = 100_000
MAX_FRAME_COLUMNS = 100
MAX_FRAME_RESULT_CHARS = 16_000_000
MAX_FRAME_SOURCES = 8
MAX_FRAME_TRANSFORMS = 20
MAX_DESCRIBE_RESULT_BYTES = 5_000
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
        # Pandas infers float64 for nullable integer columns, irreversibly
        # rounding identifiers above 2**53 even when a transformation uses
        # a different column. Keep source scalars unchanged; _numeric performs
        # explicit float64 conversion only for calculation operands.
        return pd.DataFrame(rows, columns=columns, dtype=object)

    @staticmethod
    def _timestamps(frame: pd.DataFrame, column: str) -> pd.Series:
        if column not in frame:
            raise DataOperationError(f"Zeitstempelspalte fehlt: {column!r}.")
        if frame[column].isna().any():
            raise DataOperationError("Zeitstempel dürfen nicht leer sein.")
        try:
            values = frame[column]
            if all(isinstance(value, str) for value in values):
                if any(value.strip().casefold() in {"", "nat"} for value in values):
                    raise DataOperationError("Zeitstempel dürfen nicht leer sein.")
                # Python's ISO parser is used only to distinguish aware from
                # naive values; pandas performs the actual conversion without
                # losing nanosecond precision. Non-ISO inputs retain the
                # previous generic pandas parsing behavior.
                try:
                    native_times = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values]
                except ValueError:
                    timestamps = pd.to_datetime(values, errors="raise")
                else:
                    awareness = {value.tzinfo is not None and value.utcoffset() is not None for value in native_times}
                    if len(awareness) > 1:
                        raise DataOperationError("Zeitstempel benötigen eine einheitliche Zeitzone.")
                    timestamps = pd.to_datetime(
                        values, format="ISO8601", errors="raise", utc=True in awareness,
                    )
            else:
                timestamps = pd.to_datetime(values, errors="raise")
        except DataOperationError:
            raise
        except (TypeError, ValueError, OverflowError) as exc:
            raise DataOperationError("Zeitstempel sind nicht einheitlich parsebar.") from exc
        # Strings such as "" and "NaT" become NaT without raising, even with
        # errors="raise". Never let joins match or resampling drop these rows.
        if timestamps.isna().any():
            raise DataOperationError("Zeitstempel dürfen nicht leer sein.")
        if not pd.api.types.is_datetime64_any_dtype(timestamps.dtype):
            raise DataOperationError("Zeitstempel benötigen eine einheitliche Zeitzone.")
        return timestamps

    @staticmethod
    def _source_timezone(frame: pd.DataFrame, column: str) -> str | None:
        """Return an observed ISO-8601 UTC offset, never an inferred IANA zone."""
        if column not in frame or frame.empty:
            return None
        observed: str | None = None
        for value in frame[column]:
            if not isinstance(value, str):
                return None
            try:
                offset = datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset()
            except (ValueError, OverflowError):
                return None
            if offset is None:
                return None
            seconds = offset.total_seconds()
            if not seconds.is_integer():
                return None
            hours, remainder = divmod(abs(int(seconds)), 3600)
            minutes, seconds_remainder = divmod(remainder, 60)
            suffix = f":{seconds_remainder:02d}" if seconds_remainder else ""
            current = f"{'-' if seconds < 0 else '+'}{hours:02d}:{minutes:02d}{suffix}"
            if observed is not None and observed != current:
                return None
            observed = current
        return observed

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
    def _artifact_scalar(value: object) -> object:
        if value is None or value is pd.NA or value is pd.NaT:
            return None
        if isinstance(value, pd.Timestamp):
            # Timestamp.isoformat retains nanoseconds for both naive and
            # timezone-aware dates; pandas DataFrame.to_json may not.
            stamp = value.tz_convert("UTC") if value.tzinfo is not None else value
            iso = stamp.isoformat(timespec="nanoseconds")
            return iso.replace("+00:00", "Z") if value.tzinfo is not None else iso
        if isinstance(value, Decimal):
            # Preserve untouched exact numeric values in the artifact codec.
            return value
        elif not isinstance(value, (str, bool, int, float)) and hasattr(value, "item"):
            value = value.item()
        if isinstance(value, float):
            if math.isnan(value):
                return None
            if not math.isfinite(value):
                raise ValueError("Nichtendliche DataFrame-Zahl im Ergebnis.")
        if not isinstance(value, (str, bool, int, float)):
            raise TypeError("Nicht unterstützter DataFrame-Skalar.")
        return value

    @staticmethod
    def _result(
        frame: pd.DataFrame, *, operation: str,
        summary: dict[str, object] | None = None,
        result_metadata: dict[str, object] | None = None,
    ) -> CallToolResult:
        if len(frame) > MAX_FRAME_ROWS or len(frame.columns) > MAX_FRAME_COLUMNS:
            raise DataOperationError("DataFrame-Ergebnis überschreitet das Zeilen-/Spaltenlimit.")
        # The result is fed back into DataOperations._normalize_json_records
        # on the next tool call. Enforce the same rule before advertising an
        # artifact that downstream Data MCP tools would be unable to read.
        for column in frame.columns:
            _validate_column_name(column, source="DataFrame-Ergebnis")
        if not frame.columns.is_unique:
            raise DataOperationError("DataFrame enthält doppelte Spaltennamen.")
        time_metadata = dict(result_metadata or {})
        if "timestamp" in frame and "timestamp_timezone" not in time_metadata:
            column = frame["timestamp"]
            if isinstance(column.dtype, pd.DatetimeTZDtype):
                # Aware pandas timestamps are always serialized as UTC below.
                time_metadata["timestamp_timezone"] = "UTC"
            else:
                offset = DataFrameOperations._source_timezone(frame, "timestamp")
                if offset is not None:
                    time_metadata["timestamp_timezone"] = "UTC" if offset == "+00:00" else offset
        # Reuse the same type-safe artifact and bounded metadata protocol as
        # the ordinary tabular operations, including for tiny result sets.
        try:
            rows = [
                {column: DataFrameOperations._artifact_scalar(value) for column, value in row.items()}
                for row in frame.to_dict(orient="records")
            ]
            return structured_table_result(
                list(frame.columns), rows, operation=operation, summary=summary,
                result_metadata=time_metadata,
                max_result_bytes=MAX_FRAME_RESULT_CHARS,
            )
        except (ValueError, TypeError, OverflowError) as exc:
            raise DataOperationError("DataFrame kann nicht als JSON serialisiert werden: " + str(exc)) from exc

    def _ordered_series(
        self, path: str, records_path: str | None,
        time_column: str, value_column: str,
    ) -> pd.DataFrame:
        frame = self._read_frame(path, records_path)
        stamps = self._timestamps(frame, time_column)
        values = self._numeric(frame, value_column)
        series = pd.DataFrame({"timestamp": stamps, "value": values})
        series = series.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
        if series["timestamp"].duplicated().any():
            raise DataOperationError("Zeitreihe enthält doppelte Zeitstempel.")
        return series

    @staticmethod
    def _gap_limit(max_gap_seconds: float) -> float:
        if (isinstance(max_gap_seconds, bool) or not isinstance(max_gap_seconds, (int, float))
                or not math.isfinite(max_gap_seconds) or not 0 < max_gap_seconds <= 604_800):
            raise DataOperationError("max_gap_seconds muss zwischen 0 (exklusiv) und 604800 liegen.")
        return float(max_gap_seconds)

    @staticmethod
    def _elapsed_seconds(start: pd.Timestamp, end: pd.Timestamp) -> float:
        # Timedelta.total_seconds() discards sub-microsecond remainders in
        # pandas 2.2. The integer .value is measured in nanoseconds; converting
        # only after subtraction retains them for duration and gap checks.
        return (end - start).value / 1_000_000_000

    def describe_data(
        self, path: str, *, columns: list[str] | None = None,
        time_column: str | None = None,
        records_path: str | None = None,
    ) -> str:
        """Compact descriptive statistics, without forwarding any measurement rows."""
        frame = self._read_frame(path, records_path)
        if columns is not None and (not columns or len(columns) != len(set(columns))):
            raise DataOperationError("columns muss eindeutige, nichtleere Auswahl enthalten.")
        if time_column is not None:
            timestamps = self._timestamps(frame, time_column)
            order = timestamps.argsort(kind="stable")
            frame = frame.iloc[order].reset_index(drop=True)
            timestamps = timestamps.iloc[order].reset_index(drop=True)
        else:
            timestamps = None
        selected = columns if columns is not None else [
            column for column in frame.columns
            if column != time_column and (
                frame[column].dropna().empty or all(
                    isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
                    for value in frame[column].dropna()
                )
            )
        ]
        if columns is not None and time_column in columns:
            raise DataOperationError("Die Zeitspalte darf nicht als numerische Datenspalte gewählt werden.")
        statistics: dict[str, dict[str, object]] = {}
        for column in selected:
            values = self._numeric(frame, column)
            valid = values.dropna()
            stats: dict[str, object] = {
                "numeric_count": len(valid), "null_count": len(values) - len(valid),
            }
            if not valid.empty:
                stats.update(
                    minimum=float(valid.min()), maximum=float(valid.max()),
                    mean=float(valid.mean()), median=float(valid.median()),
                    std=float(valid.std()) if len(valid) > 1 else None,
                    p25=float(valid.quantile(0.25)), p75=float(valid.quantile(0.75)),
                    first_value=float(valid.iloc[0]), last_value=float(valid.iloc[-1]),
                )
                if timestamps is not None:
                    stats["minimum_time"] = self._artifact_scalar(timestamps.iloc[values.idxmin()])
                    stats["maximum_time"] = self._artifact_scalar(timestamps.iloc[values.idxmax()])
            statistics[column] = stats
        result = {"row_count": len(frame), "statistics": statistics}
        if timestamps is not None and len(timestamps):
            result["first_time"] = self._artifact_scalar(timestamps.iloc[0])
            result["last_time"] = self._artifact_scalar(timestamps.iloc[-1])
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (ValueError, OverflowError) as exc:
            raise DataOperationError("Statistik enthält nichtendliche Werte.") from exc
        if len(encoded.encode("utf-8")) > MAX_DESCRIBE_RESULT_BYTES:
            raise DataOperationError("Statistikausgabe überschreitet 5 KB; wähle weniger columns.")
        return encoded

    def analyze_intervals(
        self, path: str, *, time_column: str, value_column: str,
        condition: Literal["lt", "lte", "gt", "gte", "eq", "ne"],
        threshold: float, max_gap_seconds: float,
        records_path: str | None = None,
    ) -> CallToolResult:
        """Contiguous, qualifying sample-and-hold intervals with bounded gaps."""
        gap_limit = self._gap_limit(max_gap_seconds)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
            raise DataOperationError("threshold muss eine endliche Zahl sein.")
        comparisons = {
            "lt": lambda v: v < threshold, "lte": lambda v: v <= threshold,
            "gt": lambda v: v > threshold, "gte": lambda v: v >= threshold,
            "eq": lambda v: v == threshold, "ne": lambda v: v != threshold,
        }
        if condition not in comparisons:
            raise DataOperationError("Unbekannte Intervallbedingung.")
        series = self._ordered_series(path, records_path, time_column, value_column)
        intervals: list[dict[str, object]] = []
        skipped_gaps = 0
        covered_seconds = 0.0
        active_start = None
        active_end = None

        def finish() -> None:
            nonlocal active_start, active_end
            if active_start is not None:
                intervals.append({
                    "start_time": self._artifact_scalar(active_start),
                    "end_time": self._artifact_scalar(active_end),
                    "duration_seconds": self._elapsed_seconds(active_start, active_end),
                })
                active_start = active_end = None

        for index in range(len(series) - 1):
            left, right = series.iloc[index], series.iloc[index + 1]
            delta = self._elapsed_seconds(left["timestamp"], right["timestamp"])
            if delta > gap_limit:
                skipped_gaps += 1
            usable = (0 < delta <= gap_limit and pd.notna(left["value"]) and pd.notna(right["value"]))
            if not usable:
                finish()
                continue
            covered_seconds += delta
            if comparisons[condition](left["value"]):
                if active_start is None:
                    active_start = left["timestamp"]
                active_end = right["timestamp"]
            else:
                finish()
        finish()
        duration = sum(interval["duration_seconds"] for interval in intervals)
        summary = {
            "interval_count": len(intervals), "total_seconds": duration,
            "longest_seconds": max((item["duration_seconds"] for item in intervals), default=0.0),
            "covered_seconds": covered_seconds, "skipped_gaps": skipped_gaps,
            "row_count": len(series), "interval_end_exclusive": True,
            "method": "sample_and_hold", "max_gap_seconds": gap_limit,
        }
        result_frame = pd.DataFrame(intervals, columns=["start_time", "end_time", "duration_seconds"])
        return self._result(result_frame, operation="analyze_intervals", summary=summary)

    def integrate_data(
        self, path: str, *, time_column: str, value_column: str,
        max_gap_seconds: float,
        method: Literal["trapezoid", "previous"] = "trapezoid",
        time_unit: Literal["seconds", "hours"] = "hours",
        records_path: str | None = None,
    ) -> str:
        """Integrate irregular samples without extrapolation over missing intervals."""
        gap_limit = self._gap_limit(max_gap_seconds)
        if method not in {"trapezoid", "previous"}:
            raise DataOperationError("Unbekanntes Integrationsverfahren.")
        if time_unit not in {"seconds", "hours"}:
            raise DataOperationError("Unbekannte Integrationseinheit.")
        series = self._ordered_series(path, records_path, time_column, value_column)
        integral = 0.0
        covered_seconds = 0.0
        used = skipped_gaps = skipped_nulls = 0
        for index in range(len(series) - 1):
            left, right = series.iloc[index], series.iloc[index + 1]
            delta = self._elapsed_seconds(left["timestamp"], right["timestamp"])
            if delta > gap_limit:
                skipped_gaps += 1
                continue
            if pd.isna(left["value"]) or pd.isna(right["value"]):
                skipped_nulls += 1
                continue
            if method == "trapezoid":
                left_value = float(left["value"])
                right_value = float(right["value"])
                combined = left_value + right_value
                # A finite average may exist even when its unscaled sum
                # overflows (e.g. two large, equal float64 measurements).
                value = (combined / 2 if math.isfinite(combined)
                         else left_value / 2 + right_value / 2)
            else:
                value = float(left["value"])
            part = value * delta
            if not math.isfinite(part) or not math.isfinite(integral + part):
                raise DataOperationError("Integration erzeugt ein nichtendliches Ergebnis.")
            integral += part
            covered_seconds += delta
            used += 1
        result = {
            "integral": integral / 3600 if time_unit == "hours" else integral,
            "time_unit": time_unit, "method": method,
            "covered_seconds": covered_seconds, "intervals_used": used,
            "intervals_skipped_gaps": skipped_gaps,
            "intervals_skipped_nulls": skipped_nulls,
            "row_count": len(series), "max_gap_seconds": gap_limit,
        }
        return json.dumps(result, ensure_ascii=False, allow_nan=False)

    def concat_data(
        self, sources: list[DataFrameInput], *,
        sort_by: list[str] | None = None,
        unique_by: list[str] | None = None,
        keep: Literal["first", "last"] = "first",
    ) -> CallToolResult:
        """Row-wise concatenation; no implicit schema union or deduplication."""
        if not 2 <= len(sources) <= MAX_FRAME_SOURCES:
            raise DataOperationError("concat_data erwartet 2 bis 8 Quellen.")
        if keep not in {"first", "last"}:
            raise DataOperationError("keep muss 'first' oder 'last' sein.")
        inputs = [self._read_frame(source.path, source.records_path) for source in sources]
        base_columns = list(inputs[0].columns)
        if any(set(frame.columns) != set(base_columns) for frame in inputs[1:]):
            raise DataOperationError("Alle concat_data-Quellen müssen dieselben Spalten besitzen.")
        if sum(len(frame) for frame in inputs) > MAX_FRAME_ROWS:
            raise DataOperationError("concat_data überschreitet das Zeilenlimit.")
        for names, parameter in ((sort_by, "sort_by"), (unique_by, "unique_by")):
            if names is not None and (not names or len(names) != len(set(names)) or any(name not in base_columns for name in names)):
                raise DataOperationError(f"{parameter} muss eine gültige Liste eindeutiger Spalten sein.")
        frame = pd.concat([source[base_columns] for source in inputs], ignore_index=True)
        if unique_by is not None:
            # Pandas treats True and 1 as duplicates even though the Data MCP
            # uses type-aware scalar identity. Keep the same semantics here.
            records = frame.to_dict(orient="records")
            positions = range(len(records)) if keep == "first" else range(len(records) - 1, -1, -1)
            seen: set[tuple[tuple[str, object], ...]] = set()
            chosen: list[int] = []
            for index in positions:
                key = tuple(_scalar_identity(records[index][column]) for column in unique_by)
                if key not in seen:
                    seen.add(key)
                    chosen.append(index)
            frame = frame.iloc[sorted(chosen)]
        if sort_by is not None:
            try:
                frame = frame.sort_values(sort_by, kind="stable")
            except (TypeError, ValueError) as exc:
                raise DataOperationError("sort_by-Spalten können nicht gemeinsam sortiert werden.") from exc
        return self._result(frame.reset_index(drop=True), operation="concat_data")

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
        source_offsets: list[str | None] = []
        for source in sources:
            raw = self._read_frame(source.path, source.records_path)
            source_offsets.append(self._source_timezone(raw, source.time_column))
            times = self._timestamps(raw, source.time_column)
            if times.duplicated().any():
                raise DataOperationError(f"Quelle {source.alias!r} enthält doppelte Zeitstempel.")
            values = self._numeric(raw, source.value_column)
            frame = pd.DataFrame({"timestamp": times, source.alias: values})
            frames.append(frame.sort_values("timestamp").reset_index(drop=True))

        if len({frame["timestamp"].dt.tz is not None for frame in frames}) > 1:
            raise DataOperationError("Join-Quellen dürfen keine Zeitstempel mit und ohne Zeitzone mischen.")
        # pandas 3.x infers datetime resolution per source (e.g. seconds ->
        # microseconds, fractional nanoseconds -> nanoseconds). merge_asof
        # requires identical key dtypes. Use the finest input resolution to
        # normalize losslessly, while keeping distant dates at their usable
        # lower resolution instead of always forcing ns.
        resolution_rank = {"s": 0, "ms": 1, "us": 2, "ns": 3}
        common_unit = max(
            (frame["timestamp"].dt.unit for frame in frames),
            key=resolution_rank.__getitem__,
        )
        try:
            for frame in frames:
                if frame["timestamp"].dt.unit != common_unit:
                    frame["timestamp"] = frame["timestamp"].dt.as_unit(common_unit)
        except (ValueError, TypeError, OverflowError) as exc:
            raise DataOperationError(
                "Zeitstempel können nicht verlustfrei auf eine gemeinsame Auflösung gebracht werden."
            ) from exc

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
        time_metadata: dict[str, object] = {}
        if frames[0]["timestamp"].dt.tz is not None:
            # _artifact_scalar serializes aware timestamps in UTC.
            time_metadata["timestamp_timezone"] = "UTC"
        if source_offsets[0] is not None and all(
            offset == source_offsets[0] for offset in source_offsets
        ):
            time_metadata["source_timezone"] = source_offsets[0]
        return self._result(
            joined, operation="join_data", result_metadata=time_metadata,
        )

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
        rate_units: dict[str, str] = {}
        for spec in operations:
            if not spec.name or spec.name in frame:
                raise DataOperationError(f"Ergebnisspalte ist leer oder bereits vorhanden: {spec.name!r}.")
            if spec.operation != "diff_per_time" and (
                spec.max_gap_seconds is not None
                or spec.time_column != "timestamp"
                or spec.time_unit != "hours"
            ):
                raise DataOperationError("Zeitparameter sind nur für diff_per_time zulässig.")
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
            elif spec.operation == "diff_per_time":
                if spec.max_gap_seconds is None:
                    raise DataOperationError("diff_per_time benötigt max_gap_seconds.")
                if spec.other_column is not None or spec.constant is not None or spec.window is not None:
                    raise DataOperationError("diff_per_time erlaubt keine Binär-/Fensterparameter.")
                gap_limit = self._gap_limit(spec.max_gap_seconds)
                units = {"seconds": 1, "minutes": 60, "hours": 3600}
                if spec.time_unit not in units:
                    raise DataOperationError("time_unit muss seconds, minutes oder hours sein.")
                timestamps = self._timestamps(frame, spec.time_column)
                rates = [float("nan")] * len(frame)
                for index in range(1, len(frame)):
                    seconds = self._elapsed_seconds(
                        timestamps.iloc[index - 1], timestamps.iloc[index],
                    )
                    if seconds < 0:
                        raise DataOperationError("diff_per_time benötigt aufsteigende Zeitstempel.")
                    if seconds == 0 or seconds > gap_limit:
                        continue
                    previous, current = left.iloc[index - 1], left.iloc[index]
                    if pd.isna(previous) or pd.isna(current):
                        continue
                    scale = units[spec.time_unit] / seconds
                    difference = float(current) - float(previous)
                    if math.isfinite(difference):
                        rate = difference * scale
                    else:
                        # Opposite large finite values can overflow when
                        # subtracted, even if the time-normalized rate fits.
                        rate = float(current) * scale - float(previous) * scale
                    if not math.isfinite(rate):
                        raise DataOperationError("diff_per_time erzeugte einen nichtendlichen Wert.")
                    rates[index] = rate
                result = pd.Series(rates, index=frame.index, dtype="float64")
                rate_units[spec.name] = spec.time_unit
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
        summary = {"rate_time_units": rate_units} if rate_units else None
        return self._result(frame, operation="derive_columns", summary=summary)

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
        source_offset = self._source_timezone(frame, time_column)
        frame[time_column] = self._timestamps(frame, time_column)
        time_metadata: dict[str, object] = {}
        if frame[time_column].dt.tz is not None:
            time_metadata["timestamp_timezone"] = "UTC"
        if source_offset is not None:
            time_metadata["source_timezone"] = source_offset
        specs: dict[str, tuple[str, str]] = {}
        for spec in aggregations:
            if spec.column == time_column:
                raise DataOperationError("Zeitstempel können nicht numerisch aggregiert werden.")
            frame[spec.column] = self._numeric(frame, spec.column)
            if spec.method not in {"mean", "min", "max", "sum", "count"}:
                raise DataOperationError(f"Unbekannte Aggregation: {spec.method!r}.")
            specs[spec.alias] = (spec.column, spec.method)
        try:
            if not frame.empty:
                # Even two input rows can produce millions of empty buckets.
                # All supported frequencies are fixed-length ticks. pandas
                # anchors these to the first day's midnight by default. Count
                # the occupied range of buckets *before* allocating its dense
                # resample index, including potentially empty buckets.
                timestamps = frame[time_column]
                interval = pd.Timedelta(frequency)
                first = timestamps.min()
                origin = first.normalize()
                bucket_count = (
                    (timestamps.max() - origin) // interval
                    - (first - origin) // interval
                    + 1
                )
                if bucket_count > MAX_FRAME_ROWS:
                    raise DataOperationError(
                        "Resampling überschreitet das Zeilenlimit. "
                        "Wähle einen kürzeren Zeitraum oder ein größeres Intervall."
                    )
            resampler = frame.set_index(time_column).sort_index().resample(
                frequency, closed="left", label="left"
            )
            grouped = resampler.agg(**specs)
            valid_counts: dict[str, pd.Series] = {}
            for spec in aggregations:
                if spec.method == "sum":
                    # pandas.sum defaults to 0 both for empty buckets and
                    # buckets containing only null measurements. The check
                    # must be per column: another sensor may have valid data.
                    if spec.column not in valid_counts:
                        valid_counts[spec.column] = resampler[spec.column].count()
                    grouped.loc[valid_counts[spec.column] == 0, spec.alias] = float("nan")
            for alias in specs:
                # pandas may overflow finite inputs to +/-inf, which to_json
                # would silently serialize as null. Genuine missing values are
                # allowed, but computed infinities must be rejected.
                values = grouped[alias].dropna()
                if not values.empty and not values.map(math.isfinite).all():
                    raise DataOperationError("Resampling erzeugte ein nichtendliches Ergebnis.")
            grouped = grouped.reset_index()
        except (ValueError, TypeError, OverflowError) as exc:
            raise DataOperationError(f"Resampling fehlgeschlagen: {exc}") from exc
        if time_column != "timestamp":
            grouped = grouped.rename(columns={time_column: "timestamp"})
        return self._result(
            grouped, operation="resample_data", result_metadata=time_metadata,
        )
