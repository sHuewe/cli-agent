# Workspace Data MCP Server

The built-in Data MCP server performs deterministic analysis of structured data. The only entry point for **new textual datasets** is `wrap_data(content)`. Existing workspace files are processed directly through `path` and, for JSON where needed, `records_path`.

For file access it uses the same `Workspace` implementation as built-in OS MCP, including path, secret, symlink/junction, hardlink and protected-path checks. It does **not** expose general file tools such as `read_file`, `write_file` or `delete_file`.

## Activation

Enable Data MCP with:

```powershell
cli-agent --with-data
```

Filesystem permissions are determined solely through the existing OS switches:

```powershell
# Calculator only; no workspace file access for Data
cli-agent --with-data

# wrap_data and tabular analysis over workspace files
cli-agent --with-os-read --with-data

# Same Data pipeline; OS tools may also write
cli-agent --with-os-write --with-data
```

**Pandas is installed with every cli-agent installation.** With `--with-data --with-os-read` (also `--with-os-write`), `join_data`, `derive_columns`, `resample_data`, `describe_data`, `analyze_intervals`, `integrate_data` and `concat_data` are available. They require no generic code execution and accept no arbitrary Python/Pandas expressions.

Without `--with-os-read` or `--with-os-write`, Data MCP creates no `Workspace` and exposes only `calculate`. Every tabular tool requires OS read access to store complete results as session artifacts and allow subsequent tools to read them. Data MCP registers no mutating file tools.

Data MCP is built in. Launch arguments are agent-generated, and OS permissions are derived from CLI capabilities outside model control.

## Supported formats

Workspace files support `.csv`, `.tsv`, `.json`, `.jsonl`, `.ndjson`, `.md` and `.markdown`. `wrap_data(content: str)` autodetects Markdown pipe tables, JSON, CSV, TSV and JSONL/NDJSON without a format argument. Invalid JSON-like content produces a parse error rather than a silent fallback; ambiguous/non-tabular text is rejected.

For existing JSON files, `records_path` selects an array of flat objects via JSON Pointer (e.g. `/measurements` or `/rows`). When the structure is unknown, `inspect_json(path)` locates candidate arrays and exposes their paths, row counts and column names **without data values**. No pointer is needed for a root-level array. Known pointers are used directly without `wrap_data`.

`wrap_data` normalizes unambiguously recognized datasets: a single table becomes `/rows`; multiple tables become `/tables/0/rows`, `/tables/1/rows`, etc., with `record_sets` metadata. JSONL/NDJSON requires exactly one object per nonempty line. Nested values **inside** selected rows are deliberately rejected. Heterogeneous JSON/JSONL rows remain sparse internally: absent fields are not materialized as explicit `null` in every row, but tabular output treats them as `null`. CSV/TSV scalars are conservatively inferred as null, boolean, integer, float or string.

CSV/TSV and JSON/JSONL column names must be nonempty and have no leading/trailing spaces. Explicit aggregation aliases obey the same rule to keep generated column names stable in downstream operations.

Dataset files are limited to **100 MB and 1,000,000 records**. `wrap_data` content is limited to **2,000,000 UTF-8 bytes and characters**. The Python CSV/TSV field limit is raised to the existing maximum file size; new text inputs still obey their separate 2,000,000-byte bound. Markdown checks the total number of rows to materialize **before** tokenization.

Tool results also have size bounds. `select_data` and `aggregate_data` **never truncate rows**: if the complete result exceeds the current **16 MB** limit, the call fails. With workspace access, large results are exposed to the LLM as session paths and metadata rather than raw rows.

## External data export: delivery only

**Internal analysis artifacts and external exported files serve different purposes.** `select_data`, `aggregate_data`, `join_data` and other Data tools create reusable session artifacts. Pass their `workspace_path` and `records_path` directly to subsequent Data tools without exporting or rereading raw rows into the LLM.

`export_data(path, format, records_path=...)` is **exclusively an output step** when the final dataset must be delivered outside the agent, for example as CSV, JSON or Markdown. The tool deliberately returns **no export rows to the LLM**, only metadata and the exported artifact path. Never use it for analysis, checking, summarization, transformations, context reduction or intermediate pipeline stages.

For a requested downloadable data file:

1. Complete analysis/preparation with ordinary Data tools.
2. Export the final result with `export_data` in the requested format.
3. Pass **the path returned by export** to `agent__publish_artifact(path=..., filename=...)`.

If the user asks only a question about data, and no dataset needs delivery, **do not call `export_data`**. `agent__publish_artifact` can also publish other session artifacts; export is needed only for the corresponding data export.

## Read and analysis tools

| Tool | Purpose |
| --- | --- |
| `calculate(expression)` | Evaluate a bounded arithmetic expression deterministically using decimal arithmetic. |
| `wrap_data(content)` | Only entry point for new text datasets (Markdown, JSON, CSV, TSV, JSONL/NDJSON); create structured artifacts. |
| `inspect_json(path)` | Discover record arrays in nested workspace JSON without exposing individual measurements; needs file-read permission. |
| `inspect_schema(...)` | Schema/profile metadata without records: rows, columns, inferred types, null/unique counts and complete low-cardinality distinct domains. |
| `preview_data(path, sample_rows=5, offset=0, records_path=...)` | Schema metadata and at most **50 complete records** from zero-based `offset`; `next_offset` points to the next page or is `null` at end. Prefer `inspect_schema` for schema-only exploration. |
| `select_data(path, ...)` | Filter, project and sort records without a row limit; structured artifact. |
| `value_counts(column, path, ...)` | Count distinct values in a column; bounded result artifact. |
| `aggregate_data(aggregations, path, ...)` | Deterministic grouped aggregations; structured artifact. |

To inspect largest/longest events, first sort with `select_data(sort_by=[...], descending=true)`, then pass its `workspace_path` and `records_path` to `preview_data(sample_rows=50, offset=0)`. Continue using `next_offset` until `null`. Preview limits affect only model-visible rows, not the complete underlying artifact.

### Time series and DataFrames (Pandas, `--with-os-read`)

Advanced tools are designed for many measurements. Their full output is structured JSON with `columns` and `rows`, but `cli-agent` does **not** place all of it in LLM context. Through `_meta.io.github.shuewe.cli-agent/only_metadata_to_llm`, the existing session artifact mechanism stores it separately.

The model receives Data MCP metadata (`operation`, `row_count`, `columns`, `records_path`) and the agent-supplied `workspace_path`. Regardless of output size, it also receives an **automatically calculated concise summary** with up to five complete example rows (`sample_rows`), per-column `null_count`, numeric min/max/mean, ISO timestamp ranges, and complete `value_counts` for up to five distinct short categorical values. Mean is an approximate float. Statistics cover **all result rows**, never only the preview. Ambiguous or mixed-type columns do not get misleading numeric/time statistics.

All table-producing tools (including `wrap_data`, `select_data`, `aggregate_data`, `value_counts`) return complete data in MCP `structuredContent`. The LLM **never** receives a separate full `rows` field in metadata. For 1–5 rows, sample rows may happen to cover everything; `sample_rows_complete=true` indicates that.

Booleans and large integers preserve scalar types. Exact `Decimal` values use reversible encoding `{"__cli_agent_decimal__":"..."}` with `scalar_encoding="cli-agent-decimal-v1"`; downstream Data tools reconstruct them numerically. Unchanged Float64 values retain their Float64 semantics.

The metadata budget is **5,000 UTF-8 bytes**, including space reserved for agent-added `workspace_path`. Whole preview rows or column statistics are omitted to fit; individual cell values are **never truncated**. `sample_rows_complete` and `stats_columns_complete` indicate completeness. For extremely wide schemas, `column_count` and `columns_complete=false` are included. The complete artifact remains accessible at `workspace_path`.

Example L1/L3 phase measurement workflow:

1. Reuse known `workspace_path`/`records_path` metadata. Call `inspect_json` **only if JSON structure is unknown** to find, for example, `records_path="/measurements"`.
2. Use `join_data` to align two time series on exactly equal timestamps. Each source provides `path`, `alias`, `time_column`, `value_column`, and optional `records_path`:

   ```json
   {
     "sources": [
       {"path": "l1.json", "records_path": "/measurements", "alias": "L1", "time_column": "timestamp", "value_column": "power"},
       {"path": "l3.json", "records_path": "/measurements", "alias": "L3", "time_column": "timestamp", "value_column": "power"}
     ],
     "direction": "exact",
     "how": "inner"
   }
   ```

3. The result includes `workspace_path` (e.g. `mcp-data-<id>/r-<id>.json`) and `records_path="/rows"`. Apply `derive_columns` to that joined DataFrame:

   ```json
   {
     "path": "mcp-data-<id>/r-<id>.json",
     "records_path": "/rows",
     "operations": [
       {"name": "D", "operation": "subtract", "column": "L1", "other_column": "L3"}
     ]
   }
   ```

4. For trends, `resample_data` can summarize the result in five-minute windows using `mean` or `max`, again returning `workspace_path` and `records_path="/rows"`.

### Processing long time series in chunks

For longer histories, such as 13 weekly blocks, the following is an **optional** approach. Every step uses actual returned metadata; stop when the question is already answered.

1. Fetch measurements in source-permitted chunks, e.g. `get_history(include_points=true)`. Use responses already stored by the agent directly via `workspace_path`. Use `wrap_data(content)` only once when data exist **solely as text**.
2. Reuse the `records_path` pointers from each source. Use `inspect_json(path)` only when external JSON structure is unknown.
3. Combine matching time series with `concat_data`. Each call accepts **2–8 sources**, so 13 blocks may be merged as 8 + 5, then combined again. Pass `workspace_path` and `records_path="/rows"` for intermediates; sort chronologically if needed.
4. Reduce the combined series with `resample_data(frequency="10min")`, then locate relevant periods using `derive_columns` (e.g. `diff`) or `describe_data`.
5. Extract only interesting **original time windows** with `select_data`; use `preview_data` for specific measurements. Fetch other sensors only when needed.

After each table-producing step, pass `path=<returned workspace_path>` and `records_path=<returned records_path>`. For normalized single tables this is `/rows`; multi-table inputs can use `/tables/1/rows`. No tool needs earlier raw rows reconstructed in the LLM context. Do not refetch daily data already included in an existing weekly artifact. File, row and session artifact limits still apply.

| Tool | Purpose and constraints |
| --- | --- |
| `join_data(sources, direction, how, tolerance_seconds)` | Exact joins are default and never interpolate; differing sampling rates can use `nearest`, `backward`, `forward` only with explicit maximum tolerance and `how="left"`. Duplicate timestamps are rejected. |
| `derive_columns(path, operations, records_path)` | Operations: `add`, `subtract`, `multiply`, `divide`, `diff`, `diff_per_time`, `cumsum`, `rolling_mean`. Binary operations accept columns or constants; `rolling_mean.window` counts rows. `diff_per_time` uses real intervals and an explicit gap limit. |
| `resample_data(path, frequency, aggregations, time_column, records_path)` | Bounded grids such as `30s`, `5min`, `1h`, `1d`. Functions `mean`, `min`, `max`, `sum`, `count`; no interpolation. Output timestamp marks interval start. |
| `describe_data(path, columns, time_column, records_path)` | Compact JSON statistics: count, nulls, min/max, mean, median, sample standard deviation, quartiles and first/last valid value. Optional time of extrema with `time_column`. Select numeric columns by default. Direct model result limited to 5 KB. |
| `analyze_intervals(path, time_column, value_column, condition, threshold, max_gap_seconds, records_path)` | Contiguous periods using `lt`, `lte`, `gt`, `gte`, `eq`, `ne`. Return count, total duration and longest interval in metadata; complete intervals in session artifact. |
| `integrate_data(path, time_column, value_column, max_gap_seconds, method, time_unit, records_path)` | Integrate irregular samples using trapezoids (`trapezoid`) or predecessor-held values (`previous`). Compact numeric result in value-unit × hours (default) or × seconds. Watts × hours yield Wh, **not** kWh. |
| `concat_data(sources, sort_by, unique_by, keep)` | Append 2–8 datasets with identical column names, regardless of column order. Preserve duplicates/source ordering by default; sorting and deduplication on `unique_by` are opt-in. |

### Temporal change rates (D2)

`diff` is the difference from the preceding row. For irregular sampling, `diff_per_time` provides a comparable rate: (current value − prior value) / actual elapsed time.

```json
{
  "operations": [{
    "name": "temperature_rate",
    "operation": "diff_per_time",
    "column": "temperature",
    "time_column": "timestamp",
    "time_unit": "hours",
    "max_gap_seconds": 3600
  }]
}
```

For Celsius inputs, this produces °C per hour. `time_unit` accepts `seconds`, `minutes` or `hours` (default). `max_gap_seconds` is **mandatory**, greater than zero and at most 604,800 seconds. The unit is also reported as `summary.rate_time_units` in artifact metadata.

The first rate is `null`; equal timestamps, missing values or gaps exceeding the limit also produce `null`. There is no interpolation. Missing timestamps or reverse-ordered series are rejected. Offset changes use real elapsed time, not wall-clock differences. For 30/60/90-minute trends, resample first and then apply `diff`; this measures differences of aggregated intervals, **not exact sliding windows**.

### Time-zone metadata (D3)

`join_data` and `resample_data` serialize timezone-aware result timestamps as UTC (`Z`) and expose `timestamp_timezone="UTC"` in model metadata. `source_timezone` contains a fixed offset such as `"+02:00"` **only if all directly used sources consistently provide that offset as ISO-8601**. This does **not** identify an IANA zone such as `Europe/Berlin`.

When sources have different offsets (e.g. at daylight saving transitions), `source_timezone` is omitted; the output remains unambiguously UTC. No time-zone information is invented for naive timestamps. To report local event times, apply the actual target time zone separately and inspect the series.

For `analyze_intervals`, a sample holds from its timestamp until the next point; the last value is not extrapolated. `integrate_data` can instead use trapezoidal integration. Both require `max_gap_seconds` greater than zero and at most 604,800: larger gaps and missing endpoint values are excluded. Duplicate timestamps are rejected. Results report actual covered duration and excluded intervals so unobserved periods are not treated as measurements. Unit conversion (e.g. Wh to kWh) is **not automatic**.

Joins reconcile timestamp precision (e.g. microseconds versus nanoseconds) losslessly. Nanoseconds remain intact even for timezone-aware timestamps. A source may contain ISO timestamps with varying fractional precision. Explicit timezone offsets are normalized to UTC, allowing multiple offsets together; naive and aware sources may **not** be mixed.

Even a zero-row join retains declared `columns` in its JSON artifact for `inspect_schema` and downstream DataFrame tools.

Pandas tools compute with `float64`; use `calculate` or existing Decimal-based aggregations for exact decimal arithmetic. Only selected calculation columns convert to `float64`; untouched source columns preserve scalar types, even large integer IDs containing nulls. DataFrame processing is bounded to **100,000 rows, 100 columns, 8 join/concat sources and 16 MB** per tabular result. Narrow long time ranges at the source when practical.

Finite `float64` outputs serialize to JSON without additional decimal rounding and deserialize bit-identically; this does not guarantee exact decimal arithmetic during computation. **Summing instantaneous power is not computing energy**; integration requires a time basis.

Temporary artifacts belong to the active session and are retained for inspection after a response. At the next regular user request, the session subdirectory and its artifacts are removed. `enable`/`disable` and frontend tool continuation do **not** trigger cleanup. `reset_history()` and normal session shutdown also clean them. Paths reported in earlier answers then become invalid. Other workspace files, including files explicitly created by users or OS tools, remain untouched. A process crash may prevent cleanup.

### Markdown tables

The Markdown pipe parser is used internally by `wrap_data`; `extract_markdown_tables` is no longer a public MCP tool. Supply the complete relevant Markdown document to `wrap_data`; the LLM need not cut out or reserialize tables. Multiple recognized tables are stored together and separately addressable through `record_sets` pointers.

The parser retains its safeguards: unique replacements for blank/duplicate headers, narrowly limited repairs of separator/line-break defects, and no invented cells. Existing versioned Markdown scalar encoding remains readable for exact round-trips. New tabular outputs use structured JSON artifacts rather than Markdown render-and-reparse cycles.

Example:

1. `wrap_data(content="| group | value |\\n|---|---|\\n| A | 3 |")`
2. `select_data(path=workspace_path, records_path="/rows", ...)`
3. `aggregate_data(path=new_workspace_path, records_path="/rows", ...)`

Step 1 is unnecessary for existing files. In either case, the LLM should never reconstruct raw rows between tool calls.

### Schema exploration and previews

`inspect_schema` is intended for query planning and **never returns full dataset rows**. It reads the dataset internally but exposes only:

- `row_count`.
- Per-column `name`, `dtype`, `null_count`, `unique_count`.
- Optional complete `distinct_values` domains for low-cardinality columns.

`distinct_values_limit` defaults to 20 and is bounded to 100. When cardinality is at or below this value, the **complete** domain is returned with `distinct_values_complete=true`, provided the values fit within schema output budgets. Oversized individual values or domains exceeding cumulative distinct-value budgets are withheld entirely, with `distinct_values_complete=false`; **partial/truncated distinct lists are never returned**. Cardinality over the limit behaves the same way. Setting the limit to zero disables distinct domains entirely.

`inspect_schema` also enforces a total serialized-result budget. To preserve as much column metadata as possible, optional distinct values are dropped first. If bare column metadata is still too large, the list is truncated with `column_count` reporting the real total and `columns_complete=false`; complete listings set `columns_complete=true`. Compact JSON minimizes formatting overhead.

Thus the model can learn real column names or small enum-like domains without seeing cross-column relationships in sample rows. For value frequencies, use `value_counts`.

`preview_data` deliberately differs: it includes up to `sample_rows` full records and is a **data preview**. Use it only when relationships between fields in a few example records are genuinely needed, rather than for routine schema discovery or instead of targeted `select_data`.

Typical order:

1. `inspect_schema` when columns or small domains are unknown.
2. `select_data` to filter/project concrete records.
3. `value_counts` when value frequencies are needed.
4. `preview_data` only for a justified small row preview.

### Calculator

`calculate(expression)` is available with `--with-data` regardless of OS permissions. It handles nontrivial arithmetic, ratios, percentages, decimal calculations, powers and roots.

```text
calculate("(417.3 - 382.1) / 382.1 * 100")
```

Only numeric literals, parentheses, `+`, `-`, `*`, `/`, `%`, `**`, unary `+`/`-`, and `abs()`, `round()`, `min()`, `max()`, `sqrt()` are supported. Computation uses bounded-precision `Decimal`, not binary float. No `eval()`, variables, attributes, imports or arbitrary Python expressions.

Expression length, AST size/depth, exponents, function arguments and result size have limits to prevent abuse as a resource-exhaustion/code-execution primitive.

### Filters

Filters are declarative and combined using AND:

```json
[
  {"column": "country", "op": "eq", "value": "DE"},
  {"column": "revenue", "op": "gt", "value": 100}
]
```

Supported operators: `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `not_in`, `is_null`, `not_null`, `contains`.

No Python expressions, Pandas expressions, SQL fragments, regex or lambdas are evaluated. Numeric filter inputs are normalized by numeric value for comparisons with exact JSON Decimal values, including membership and ordering; booleans remain distinct from numeric 0/1.

### Aggregations

An aggregation specifies `column`, `function`, and optional `alias`:

```json
[
  {"column": "revenue", "function": "sum", "alias": "revenue_total"},
  {"column": "revenue", "function": "mean", "alias": "revenue_mean"}
]
```

Supported functions: `count`, `sum`, `mean`, `min`, `max`, `median`, `nunique`, `std`.

Integer-only sums use Python integer arithmetic without float conversion. For mean, median and standard deviation on integer-only columns, and for aggregations with precise decimal values, `Decimal` uses working precision based on the values' magnitudes. Working precision is capped at **10,000 digits**; datasets requiring greater exponent/precision ranges are rejected before creating such a context.

Existing float columns retain their float semantics for compatibility; nonfinite aggregation results are rejected. Exact decimal values survive via reversible encoding in JSON artifacts. Booleans have distinct scalar identity from 0/1 for equality, membership, grouping and distinct counts. Nulls sort last in both ascending and descending order.

## Automatic session artifacts

All table-producing tools return complete structured datasets in MCP `structuredContent` and model-appropriate metadata through the cli-agent-specific `_meta` protocol. The agent saves data as JSON under a self-selected verified session path; Data MCP does not write arbitrary chosen files. The LLM receives only `workspace_path`, `records_path`, schema, statistics and bounded preview, **even for one-row results**.

Downstream tools read that data solely using `path` and supplied `records_path`. No raw rows are copied, parsed or reconstructed in model context. Other MCP clients still receive full `structuredContent`. Without OS read access, no tabular tools are registered.

On clean shutdown, only agent-owned session files are removed; existing workspace data remain unchanged. Crashes may leave artifacts. After completed responses, artifacts are available for inspection until the next regular user request or history reset.

## Workspace security

Paths are validated through the same `Workspace` boundary as OS MCP before parsing:

- Only relative paths inside the fixed workspace.
- No `..` traversal.
- No symlink/junction indirection for Data files.
- No protected or sensitive paths.
- No multiply hard-linked files.
- Only the agent's own temporary session directory is created.
- Active agent configuration remains protected.

When OS access is enabled, `--exclude-path` is forwarded to both OS MCP and Data MCP. With only `--with-data`, Data MCP has no filesystem capability and needs no workspace exclusions.

## Implementation boundary

Basic operations such as `select_data`, `aggregate_data` and `inspect_schema` still use Python standard library. The advanced time-series and DataFrame tools use Pandas, installed as a regular project dependency. All operations are declarative and never execute arbitrary Python expressions supplied by the model.
