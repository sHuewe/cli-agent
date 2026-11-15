# Declarative JSON relations in Flow

These operations are intentionally **schema-neutral**. They do not interpret
concepts, doctrines, source references, theme registries, or any domain-specific
field names. They operate on JSON Pointer selectors, IDs, and arrays.

Every `json_inputs` value must come from an earlier completed Flow output.
The runner verifies file ownership and checkpoint fingerprints. Unknown IDs,
duplicate aliases, missing assignments, and incomplete partitions fail closed.

## `json_project`

Select and rename top-level fields from a JSON object, copying original values
without mutation. Field values are RFC 6901 JSON Pointers.

```toml
[[steps]]
id = "project"
output = "state/project.json"
[steps.json_operation]
kind = "json_project"
input = "original"
[steps.json_operation.fields]
identifier = "/metadata/id"
records = "/data/records"
[steps.json_inputs.original]
path = "state/input.json"
```

## `json_join_aliases`

Resolve each source record's key through a mapping from alias IDs to canonical
IDs. Requires each source ID and every mapping alias to be unique. With
`require_all_aliases=true`, every alias must have at least one source record.
The output is a list containing a single object with an `assignments` array
by default, compatible with the existing `group_items` operation; the
field names are configurable.

```toml
[[steps]]
id = "join"
output = "state/join.json"
[steps.json_operation]
kind = "json_join_aliases"
input = "items"
items_path = "/*/entries/*"
item_id_path = "/id"
item_key_path = "/category"
mapping_path = "/groups/*"
mapping_id_path = "/id"
mapping_alias_path = "/aliases/*"
id_prefix = "${input.scope}:"
require_all_aliases = true
[steps.json_inputs.items]
iterate = "input.files[]"
path = "state/${each.id}.json"
[steps.json_inputs.mapping]
path = "state/canonical-groups.json"
```

JSON selectors may contain `*` to traverse complete array entries, yielding
one-based indices that the partition validator can refer to. JSON Pointer
escaping (`~0`, `~1`) is supported.

## `json_validate_partition`

Check an exhaustive, disjoint partition of input groups into selected output
groups and explicitly excluded groups. Eligibility is the configured minimum
number of distinct values under a JSON selector. Each selected output must
contain the exact union of members from its referenced input groups. Input
`needs_attention` and warnings cannot be silently converted to success.

```toml
[[steps]]
id = "check_partition"
output = "state/validated.json"
[steps.json_operation]
kind = "json_validate_partition"
input = "proposal"
source_input = "batch"
source_path = "/items/*/groups/*"
reference_template = "i{0:04d}-g{1:04d}"
distinct_path = "/members/*/owner"
min_distinct = 2
selected_path = "/results/*"
references_path = "/source_refs/*"
excluded_path = "/excluded_refs/*"
source_members_path = "/members/*"
result_members_path = "/members/*"
result_id_path = "/id"
result_id_pattern = '^[a-z][a-z0-9-]* = "/file"
result_path_template = "output/{id}.json"
member_fields = ["/owner", "/path"]
[steps.json_operation.member_patterns]
"/owner" = '^[a-z0-9-]+$'
"/path" = '^[a-z0-9-]+/[a-z0-9-]+[.]json$'
[steps.json_inputs.proposal]
path = "state/proposal.json"
```

The selected input `source_input` is a declared subflow input object; the
result JSON is a completed Flow checkpoint.

The source status/warning records are **schema-independent**. By default,
`json_validate_partition` derives their location from the parent array of
`source_path`: `/records/*/groups/*` means status records at
`/records/*`, and `/payload/blocks/*/groups/*` means
`/payload/blocks/*`. For schemas whose status records live elsewhere,
set `source_records_path` explicitly (RFC 6901 JSON pointer with
`*` array selectors). Missing or malformed status records raise an error;
they must not be treated as successful.

`result_status_path` supports RFC 6901 escaping, such as
`/state~1status` for a top-level key named `state/status`, and
`/state~0status` for `state~status`. The validated status is written
back to that exact decoded field.

## Count-bounded array fragmentation

The existing `batch_arrays` operation now accepts
`max_fragment_items = N`. It limits the total number of complete entries
across the selected array paths per fragment, independently of byte limits.
There is **no truncation**: exceeding this limit splits a fragment, and an
indivisible oversized entry still fails.

## Uniform Markdown section enumeration

`markdown_sections` accepts the opt-in `always_sections=true`. For a
nonempty Markdown file shorter than the target character limit, it emits one
`sections` entry with `id="s001"` and complete 1-based line ranges.
The default remains unchanged (short files use `mode="direct"`), preserving
existing Golden and other Flow checkpoints.

# Optional: select the status records explicitly. Without this option,
# they are inferred as the parent collection of source_path.
source_records_path = "/items/*"
result_path_field = "/file"
result_path_template = "output/{id}.json"
member_fields = ["/owner", "/path"]
[steps.json_operation.member_patterns]
"/owner" = '^[a-z0-9-]+$'
"/path" = '^[a-z0-9-]+/[a-z0-9-]+[.]json$'
[steps.json_inputs.proposal]
path = "state/proposal.json"
```

The selected input `source_input` is a declared subflow input object; the
result JSON is a completed Flow checkpoint.

## Count-bounded array fragmentation

The existing `batch_arrays` operation now accepts
`max_fragment_items = N`. It limits the total number of complete entries
across the selected array paths per fragment, independently of byte limits.
There is **no truncation**: exceeding this limit splits a fragment, and an
indivisible oversized entry still fails.

## Uniform Markdown section enumeration

`markdown_sections` accepts the opt-in `always_sections=true`. For a
nonempty Markdown file shorter than the target character limit, it emits one
`sections` entry with `id="s001"` and complete 1-based line ranges.
The default remains unchanged (short files use `mode="direct"`), preserving
existing Golden and other Flow checkpoints.
