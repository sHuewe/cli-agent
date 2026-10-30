"""Model-facing, narrowly scoped evidence and plan operations.

The persisted state and verified provenance registry belong to EvidenceState.
No model-supplied tool name, artifact path or call-id is accepted here.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

PLAN_TOOL_NAME = "agent__update_plan"
ADD_EVIDENCE_TOOL_NAME = "agent__add_evidence"
UPDATE_EVIDENCE_TOOL_NAME = "agent__update_evidence"

_STATUSES = ("pending", "in_progress", "completed", "blocked", "skipped")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")


class EvidenceInputError(ValueError):
    pass


def annotate_source_result(content: str, *, source_ref: str, tool_name: str) -> str:
    """Label a verified result without breaking JSON object consumers.

    The caller must register the successful result in the trusted registry first.
    The source reference is always server generated, never read from tool text.
    """
    try:
        decoded = json.loads(content)
    except (TypeError, ValueError):
        decoded = None
    if isinstance(decoded, dict):
        decoded["source_ref"] = source_ref
        return json.dumps(decoded, ensure_ascii=False)
    return f"[Quelle {source_ref}: {tool_name}]\n" + content


def _tool(name: str, description: str, fields: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {
            "type": "object", "additionalProperties": False,
            "required": required, "properties": fields,
        },
    }}


_STEP_FIELDS = {
    "id": {"type": "string", "maxLength": 80},
    "description": {"type": "string", "maxLength": 320},
    "status": {"type": "string", "enum": list(_STATUSES)},
}
_REF_FIELDS = {
    "type": "array", "minItems": 1, "maxItems": 16,
    "items": {"type": "string", "pattern": r"^S[1-9][0-9]*$"},
}
_EVIDENCE_FIELDS = {
    "id": {"type": "string", "maxLength": 80},
    "type": {"type": "string", "enum": ["observation", "calculation", "hypothesis"]},
    "statement": {"type": "string", "maxLength": 1200},
    "plan_step_id": {"type": "string", "maxLength": 80},
    "source_refs": _REF_FIELDS,
}

MODEL_TOOLS = (
    _tool(
        PLAN_TOOL_NAME,
        "Create or patch only the current task's plan. Initially provide goal and "
        "complete steps (id, description, status). On later calls patch steps by id "
        "(e.g. only id and status); new steps need description and status. "
        "Plan is reset for each new user task; evidences stay in this session.",
        {
            "goal": {"type": "string", "maxLength": 300},
            "steps": {"type": "array", "minItems": 1, "maxItems": 16,
                      "items": {"type": "object", "additionalProperties": False,
                                "required": ["id"], "properties": _STEP_FIELDS}},
            "remove_step_ids": {"type": "array", "items": {"type": "string"}},
        },
        [],
    ),
    _tool(
        ADD_EVIDENCE_TOOL_NAME,
        "Save one new evidence. Use the short S1/S2/... source refs shown in "
        "completed tool results, NOT a tool call ID. The server verifies each "
        "reference and supplies real call-id, tool-name and artifact metadata. "
        "When a plan exists, provide its plan_step_id.",
        _EVIDENCE_FIELDS, ["id", "type", "statement", "source_refs"],
    ),
    _tool(
        UPDATE_EVIDENCE_TOOL_NAME,
        "Revise one existing session evidence by id; provide only changed "
        "fields. Omitted fields including sources remain unchanged. "
        "Set remove=true (and only id) to delete an obsolete evidence. "
        "For source changes use S1/S2/... tokens, NOT tool call IDs.",
        {**_EVIDENCE_FIELDS, "remove": {"type": "boolean"}},
        ["id"],
    ),
)


class EvidenceOperationsMixin:
    def tool_definitions(self) -> list[dict[str, Any]]:
        if not hasattr(self, "_model_evidence_tools"):
            self._model_evidence_tools = copy.deepcopy(MODEL_TOOLS)
            self.refresh_model_tools()
        return self._model_evidence_tools

    def refresh_model_tools(self) -> None:
        tools = getattr(self, "_model_evidence_tools", None)
        if tools is None:
            return
        # Keep every source cited by a persisted finding selectable even
        # after more than 96 new completions. The registry itself stays
        # authoritative for validation (and can evict only uncited sources).
        cited_ids = {
            source["call_id"]
            for evidence in self.evidences.values()
            for source in evidence["tool_calls"]
        }
        refs = [item["source_ref"] for call_id, item in self.tool_results.items()
                if "source_ref" in item]
        cited_refs = [item["source_ref"] for call_id, item in self.tool_results.items()
                      if call_id in cited_ids and "source_ref" in item]
        offered = list(dict.fromkeys([*cited_refs, *refs[-96:]]))
        for tool, template in zip(tools, MODEL_TOOLS):
            tool.clear()
            tool.update(copy.deepcopy(template))
            if tool["function"]["name"] != PLAN_TOOL_NAME and refs:
                source_items = tool["function"]["parameters"]["properties"]["source_refs"]["items"]
                source_items["enum"] = offered

    def begin_task(self) -> None:
        """Retain session findings, drop task plan and expired file references."""
        self.plan = None
        self.compacted.clear()
        self.pending_error = None
        self.debug_updates.clear()
        for source in self.tool_results.values():
            self._expire_source(source)
        for evidence in self.evidences.values():
            evidence.pop("plan_step_id", None)
            for source in evidence["tool_calls"]:
                self._expire_source(source)
        self.refresh_model_tools()

    @staticmethod
    def _expire_source(source: dict) -> None:
        if "workspace_path" in source:
            for key in ("workspace_path", "records_path", "record_sets"):
                source.pop(key, None)
            source["artifact_expired"] = True

    def _resolve_refs(self, value: Any) -> list[dict]:
        if (not isinstance(value, list) or not 1 <= len(value) <= 16
                or len(value) != len(set(value))):
            raise EvidenceInputError("source_refs benötigt 1–16 verschiedene Quellen.")
        sources = {s["source_ref"]: (call_id, s)
                   for call_id, s in self.tool_results.items()
                   if "source_ref" in s}
        result = []
        for ref in value:
            if not isinstance(ref, str) or ref not in sources:
                raise EvidenceInputError(f"Unbekannte source_ref: {ref!r}. Nutze eine angebotene S-ID.")
            call_id, source = sources[ref]
            result.append({"call_id": call_id,
                           "description": source["tool_name"], **copy.deepcopy(source)})
        return result

    def _validated_record(self, args: dict) -> dict:
        required = {"id", "type", "statement", "source_refs"}
        if not isinstance(args, dict) or not required <= set(args) or set(args) - (
            required | {"plan_step_id"}
        ):
            raise EvidenceInputError("add_evidence benötigt id, type, statement, source_refs und ggf. plan_step_id.")
        evidence_id = args["id"]
        if not isinstance(evidence_id, str) or not _ID.fullmatch(evidence_id):
            raise EvidenceInputError("Ungültige Evidence-ID.")
        if args["type"] not in ("observation", "calculation", "hypothesis"):
            raise EvidenceInputError("Ungültiger Evidence-Typ.")
        statement = args["statement"]
        if not isinstance(statement, str) or not statement.strip() or len(statement) > 1200:
            raise EvidenceInputError("Evidence-Statement fehlt oder ist zu lang.")
        step = args.get("plan_step_id")
        if self.plan is not None:
            if not isinstance(step, str) or step not in {s["id"] for s in self.plan["steps"]}:
                raise EvidenceInputError("plan_step_id muss einen aktuellen Planschritt bezeichnen.")
        elif "plan_step_id" in args:
            raise EvidenceInputError("plan_step_id ist ohne Plan nicht zulässig.")
        return {
            "id": evidence_id, "type": args["type"], "statement": statement,
            "tool_calls": self._resolve_refs(args["source_refs"]),
            **({"plan_step_id": step} if self.plan is not None else {}),
        }

    def _check_budget(self, entries: dict, plan: dict | None = None) -> None:
        if len(entries) > 64:
            raise EvidenceInputError("Evidence-Limit überschritten.")
        state = {"plan": self.plan if plan is None else plan, "evidences": list(entries.values())}
        if len(json.dumps(state, ensure_ascii=False).encode("utf-8")) > 32000:
            raise EvidenceInputError("Evidence-State überschreitet das Größenlimit.")

    def add_evidence(self, args: dict) -> None:
        record = self._validated_record(args)
        if record["id"] in self.evidences:
            raise EvidenceInputError("Evidence-ID existiert bereits. Nutze update_evidence.")
        candidate = {**self.evidences, record["id"]: record}
        self._check_budget(candidate)
        self.evidences = candidate
        self.compacted.update(s["call_id"] for s in record["tool_calls"])
        self._refresh_live_tool_definition()

    def update_evidence(self, args: dict) -> None:
        fields = {"id", "type", "statement", "plan_step_id", "source_refs", "remove"}
        if (not isinstance(args, dict) or "id" not in args or set(args) - fields
                or len(args) == 1):
            raise EvidenceInputError("update_evidence benötigt id und mindestens ein Änderungsfeld.")
        key = args["id"]
        if not isinstance(key, str) or key not in self.evidences:
            raise EvidenceInputError("Evidence-ID nicht gefunden.")
        if args.get("remove") is True:
            if set(args) != {"id", "remove"}:
                raise EvidenceInputError("remove=true erlaubt nur id und remove.")
            self.evidences = {k: v for k, v in self.evidences.items() if k != key}
            self._refresh_live_tool_definition()
            return
        if "remove" in args:
            raise EvidenceInputError("remove darf nur true sein.")
        record = copy.deepcopy(self.evidences[key])
        for field in ("statement", "type"):
            if field in args:
                record[field] = args[field]
        if (not isinstance(record["statement"], str) or not record["statement"].strip()
                or len(record["statement"]) > 1200
                or record["type"] not in ("observation", "calculation", "hypothesis")):
            raise EvidenceInputError("Ungültige Evidence-Aussage oder Typ.")
        if "plan_step_id" in args:
            sid = args["plan_step_id"]
            if self.plan is None or sid not in {s["id"] for s in self.plan["steps"]}:
                raise EvidenceInputError("Ungültige plan_step_id.")
            record["plan_step_id"] = sid
        if "source_refs" in args:
            record["tool_calls"] = self._resolve_refs(args["source_refs"])
        if record == self.evidences[key]:
            raise EvidenceInputError("Evidence-Update enthält keine Änderung.")
        candidate = {**self.evidences, key: record}
        self._check_budget(candidate)
        self.evidences = candidate
        self.compacted.update(s["call_id"] for s in record["tool_calls"])
        self._refresh_live_tool_definition()

    @staticmethod
    def compact_plan(plan: dict | None) -> dict | None:
        if plan is None:
            return None
        return {
            "goal": plan["goal"],
            "steps": [
                {"id": step["id"], "status": step["status"]}
                for step in plan["steps"]
            ],
        }

    @classmethod
    def plan_response(cls, before: dict | None, after: dict) -> dict:
        """Describe the committed plan change for the model."""
        kind = "created" if before is None else (
            "unchanged" if before == after else "updated"
        )
        changes = []
        old_steps = {step["id"]: step for step in before["steps"]} if before else {}
        new_steps = {step["id"]: step for step in after["steps"]}
        for step_id, step in new_steps.items():
            previous = old_steps.get(step_id)
            if previous is None:
                changes.append({"id": step_id, "action": "added", "status": step["status"]})
            else:
                fields = {
                    key: {"before": previous[key], "after": step[key]}
                    for key in ("description", "status")
                    if previous[key] != step[key]
                }
                if fields:
                    changes.append({"id": step_id, "action": "changed", "fields": fields})
        for step_id in old_steps:
            if step_id not in new_steps:
                changes.append({"id": step_id, "action": "removed"})
        if before is not None and before["goal"] != after["goal"]:
            changes.insert(0, {
                "action": "goal_changed",
                "before": before["goal"], "after": after["goal"],
            })
        result = {"status": kind, "plan": cls.compact_plan(after)}
        if changes:
            result["changes"] = changes
        if kind == "unchanged":
            result["message"] = "Plan unverändert. Keinen identischen Aufruf wiederholen."
        return result

    def update_plan(self, args: dict) -> dict:
        if not isinstance(args, dict) or not args or set(args) - {"goal", "steps", "remove_step_ids"}:
            raise EvidenceInputError("update_plan erwartet goal, steps oder remove_step_ids.")
        if self.plan is None:
            if set(args) != {"goal", "steps"}:
                raise EvidenceInputError("Initialer Plan benötigt goal und vollständige steps.")
            candidate = self._validate_plan(args)
        else:
            candidate = copy.deepcopy(self.plan)
            if "goal" in args:
                candidate["goal"] = args["goal"]
            seen = set()
            for patch in args.get("steps", []):
                if (not isinstance(patch, dict) or "id" not in patch
                        or set(patch) - {"id", "description", "status"}):
                    raise EvidenceInputError("Planschritt braucht id sowie optionale description/status.")
                sid = patch["id"]
                if sid in seen:
                    raise EvidenceInputError("Doppelte Planschritt-ID.")
                seen.add(sid)
                old = next((v for v in candidate["steps"] if v["id"] == sid), None)
                if old is None:
                    if set(patch) != {"id", "description", "status"}:
                        raise EvidenceInputError("Neuer Planschritt braucht description und status.")
                    candidate["steps"].append(copy.deepcopy(patch))
                else:
                    old.update({k: v for k, v in patch.items() if k != "id"})
            remove = args.get("remove_step_ids", [])
            if not isinstance(remove, list) or len(remove) != len(set(remove)) or any(
                not isinstance(x, str) for x in remove
            ):
                raise EvidenceInputError("Ungültige remove_step_ids.")
            if any(v.get("plan_step_id") in remove for v in self.evidences.values()):
                raise EvidenceInputError("Planschritt wird noch von einer Evidence verwendet.")
            candidate["steps"] = [s for s in candidate["steps"] if s["id"] not in remove]
            candidate = self._validate_plan(candidate)
            valid = {s["id"] for s in candidate["steps"]}
            if any(v.get("plan_step_id") not in (None, *valid)
                   for v in self.evidences.values()):
                raise EvidenceInputError("Evidence verliert ihre Planschritt-Zuordnung.")
        self._check_budget(self.evidences, plan=candidate)
        before = copy.deepcopy(self.plan)
        self.plan = candidate
        return self.plan_response(before, candidate)
