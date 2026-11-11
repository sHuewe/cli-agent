"""Lossless deterministic fragmentation of source-grounded topic profiles.

A topic and its evidence are indivisible. Warnings are distributed exactly
once (never dropped or duplicated). The profile's status and source metadata
are repeated on every fragment so downstream merges cannot lose provenance
or silently promote incomplete results to successful coverage.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from cli_agent.workflows.flow_json_operations import (
    MAX_BATCHES, MAX_RESULT_BYTES, _serialized_size,
)


def split_topic_profiles(
    profiles: list[Any],
    *,
    max_batch_bytes: int,
    target_fragment_bytes: int,
    serializer: Callable[[Any], str] | None = None,
) -> list[dict[str, Any]]:
    """Split only at complete topic and warning boundaries.

    The hard limit includes a conservative one-item batch envelope. Soft
    target sizes improve packing but may be exceeded by an indivisible
    topic/entry which still fits within the hard limit.
    """

    def size(value: Any) -> int:
        return _serialized_size(value, serializer=serializer)

    # Account for the larger possible last batch ID, not just b0001.
    envelope_bytes = size({"id": f"b{MAX_BATCHES:04d}", "items": []})
    hard_limit = max_batch_bytes - envelope_bytes - 1
    if hard_limit <= 0:
        raise ValueError("batch_topic_profiles: max_batch_bytes is too small.")
    fragments: list[dict[str, Any]] = []
    result_bytes = 0

    def emit(fragment: dict[str, Any], *, profile_index: int) -> None:
        nonlocal result_bytes
        fragment_size = size(fragment)
        if fragment_size > hard_limit:
            raise ValueError(
                f"batch_topic_profiles: profile {profile_index} exceeds "
                "max_batch_bytes even after splitting."
            )
        if len(fragments) >= MAX_BATCHES:
            raise ValueError("batch_topic_profiles exceeds the batch count limit.")
        result_bytes += fragment_size + (1 if fragments else 0)
        if result_bytes > MAX_RESULT_BYTES:
            raise ValueError("batch_topic_profiles exceeds the JSON result limit.")
        fragments.append(fragment)

    for profile_index, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            raise ValueError(f"batch_topic_profiles: profile {profile_index} is not an object.")
        status = profile.get("status")
        if status not in {"ok", "needs_attention"}:
            raise ValueError(f"batch_topic_profiles: invalid status in profile {profile_index}.")
        source_path = profile.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            raise ValueError(
                f"batch_topic_profiles: missing source_path in profile {profile_index}."
            )
        topics = profile.get("topics")
        warnings = profile.get("warnings")
        if not isinstance(topics, list) or any(not isinstance(t, dict) for t in topics):
            raise ValueError(f"batch_topic_profiles: topics must be objects in profile {profile_index}.")
        if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
            raise ValueError(f"batch_topic_profiles: warnings must be strings in profile {profile_index}.")

        # Preserve all additional metadata rather than discarding unknown
        # fields. Do not repeat topic/warning arrays across fragments.
        metadata = {key: value for key, value in profile.items()
                    if key not in {"topics", "warnings"}}

        def empty_fragment() -> dict[str, Any]:
            return {**metadata, "topics": [], "warnings": []}

        current = empty_fragment()
        if size(current) > hard_limit:
            raise ValueError(
                f"batch_topic_profiles: metadata in profile {profile_index} "
                "exceed max_batch_bytes."
            )
        has_items = False
        # Placing topics before warnings preserves both lists' original order.
        for field, values in (("topics", topics), ("warnings", warnings)):
            for entry_index, entry in enumerate(values):
                trial = {**current, field: [*current[field], entry]}
                trial_size = size(trial)
                if trial_size > target_fragment_bytes and has_items:
                    emit(current, profile_index=profile_index)
                    current = empty_fragment()
                    has_items = False
                    trial = {**current, field: [entry]}
                    trial_size = size(trial)
                if trial_size > hard_limit:
                    raise ValueError(
                        f"batch_topic_profiles: indivisible {field}[{entry_index}] "
                        f"in profile {profile_index} exceeds max_batch_bytes."
                    )
                current = trial
                has_items = True

        # Preserve even an empty original profile (and its status/warnings).
        emit(current, profile_index=profile_index)

    return fragments
