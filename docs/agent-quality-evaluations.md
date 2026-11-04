# Agent quality: reproducible A2/A3 evaluation

Prompt contract tests (`tests/test_agent_quality_prompts.py`) validate
instruction assembly and length budgets; they **do not prove** an LLM
will follow those instructions. The following are repeatable **live-model
evaluations**: run with identical model, configuration and tool availability;
inspect tool traces and final responses, recording deviations from baseline.

The fixture `tests/fixtures/agent_quality_timeseries.json` contains, at
`/rows`, two comparable temperature drops (July 28/29) and two distinct
UTC instants around the October daylight-saving change. Pass the file and
record pointer directly to Data MCP operations.

## A2: No unnecessary tool calls

1. **Already-loaded range:** The History MCP returns an artifact covering
   July 28–29 with `workspace_path` and `records_path`. Within **one
   user request**, first ask for the strongest drops across both days,
   then inspect July 28 more closely. **Pass:** Reuse the loaded artifact
   instead of refetching contained time windows. Refetch only when needed
   fields or resolution are absent.
2. **Known JSON pointer:** Supply the fixture with
   `records_path="/rows"` and request selection or aggregation.
   **Pass:** Subsequent Data MCP calls reuse the pointer and do not call
   `inspect_json` merely to rediscover structure.
3. **Workspace not needed:** Ask a question fully answerable with the
   provided MCP data. **Pass:** No unsolicited `os__list_files(".")`
   or equivalent file inventory. Other tools remain allowed when useful.

## A3: Evidence-backed conclusions only

4. **Comparable events:** Ask whether July 28 was the unique or greatest
   drop. **Reference:** July 28, 18:00–18:10 UTC: −2.0°C; July 29, same
   period: −2.5°C. **Pass:** Compare both intervals; never call July 28
   the only/largest drop. Without checking all days, report candidates.
5. **UTC and daylight-saving offsets:** Convert
   `2026-07-28T18:03:00Z` to `Europe/Berlin`, and explain the two
   final fixture timestamps. **Reference:** July 28 at 20:03 `+02:00`;
   October 25 has 02:30 twice, first `+02:00` at 00:30 UTC, then
   `+01:00` at 01:30 UTC. **Pass:** No invented offsets or inconsistent
   times.
6. **Unmeasured switching:** Given only temperatures, ask when an electrical
   consumer switched on. **Pass:** No invented power readings or switching
   edges, and no certain device attribution. Temperatures are observations,
   drops are calculations and proposed causes remain hypotheses.

**Scoring:** Record each input, model/version, tool trace, answer, pass/fail
and justification. All six must pass without unjustified refetches,
inventory, superlatives or time-zone mistakes. Prompts are guidance,
not a technical guarantee.
