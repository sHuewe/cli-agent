"""Data MCP public facade: validation, reader and query composition."""
from __future__ import annotations
from cli_agent.data.data_core import *
from cli_agent.data.data_readers import DataReadMixin
from cli_agent.data.data_queries import DataQueryMixin

class DataOperations(DataReadMixin, DataQueryMixin):
    """Bounded, declarative operations for workspace-local tabular data.

    The filesystem boundary is delegated to the same Workspace instance used
    by the OS MCP server. This class deliberately does not evaluate Python,
    pandas expressions, SQL, regexes, lambdas, or model-provided code.
    """

    def __init__(self, workspace: Workspace | None) -> None:
        self.workspace = workspace

    @staticmethod
    def calculate(expression: str) -> str:
        try:
            result = calculate_expression(expression)
        except CalculatorError as exc:
            raise DataOperationError(str(exc)) from exc
        return json.dumps(
            {
                "expression": expression,
                "result": result,
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _validate_suffix(path: Path) -> str:
        suffix = path.suffix.casefold()
        if suffix not in DATA_SUFFIXES:
            raise DataOperationError(
                "Nicht unterstütztes Datenformat. Erlaubt sind .csv, .tsv, "
                ".json, .jsonl, .ndjson, .md und .markdown."
            )
        return suffix

    @staticmethod
    def _validate_limit(value: int, *, maximum: int, name: str = "limit") -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise DataOperationError(
                f"{name} muss zwischen 1 und {maximum} liegen."
            )
        return value

    @staticmethod
    def _validate_non_negative_limit(
        value: int,
        *,
        maximum: int,
        name: str,
    ) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= maximum
        ):
            raise DataOperationError(
                f"{name} muss zwischen 0 und {maximum} liegen."
            )
        return value

