"""Provider-reported, never estimated token usage for SDK runs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class UsageSummary:
    """Usage of a request or of the logical SDK session.

    Token totals are None when any model request did not report its usage.
    model_calls counts recorded model responses, including responses without
    usage data; it is not a billable token estimate.
    """

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    model_calls: int
    reported_calls: int
    unreported_calls: int


def usage_history(client: Any) -> tuple[Any, ...]:
    """Snapshot the provider usage log without mutating it."""
    values = getattr(client, "usage_history", None)
    return tuple(values) if isinstance(values, (list, tuple)) else ()


def summarize_usage(records: Sequence[Any]) -> UsageSummary:
    """Do not misrepresent partial reports as total usage."""
    tokens = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    missing = {key: False for key in tokens}
    reported = 0
    for record in records:
        if record is not None:
            reported += 1
        for field in tokens:
            value = getattr(record, field, None)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                missing[field] = True
            else:
                tokens[field] += value
    return UsageSummary(
        input_tokens=None if missing["input_tokens"] else tokens["input_tokens"],
        output_tokens=None if missing["output_tokens"] else tokens["output_tokens"],
        total_tokens=None if missing["total_tokens"] else tokens["total_tokens"],
        model_calls=len(records),
        reported_calls=reported,
        unreported_calls=len(records) - reported,
    )
