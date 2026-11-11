"""Stage 2, Dimension Selector: one LLM call over every table proposes which DQ dimensions to measure where."""

from dqvouch.context import build_transformations_text, format_asset_contexts_for_prompt
from dqvouch.llm import extract_dq_json


def build_dimension_selector_prompt(formatted_schema: str, transformations_text: str, engine: str = "ClickHouse") -> str:
    # Constructing your exact prompt template
    prompt = f"""You are a data quality expert specializing in analytical pipelines. Your task is to analyze a set of {engine} table schemas and determine which Data Quality (DQ) dimensions are applicable to each table, grounded strictly in evidence from the schema (and, when provided, the transformation logic that produces each layer).

## Context
The schemas belong to a multi-layer data pipeline. Tables are usually grouped by layer through their database name (e.g. landing/raw ingestion → staging/transformation → analytic/consumption), and the same logical entities flow through several layers. The pipeline may contain more than one independent flow: work out which tables belong together from the shared columns, the lineage and, when provided, the transformation logic -- do not assume every table belongs to the same flow, and give every flow the same attention. Consider each table's role in its flow when assessing DQ relevance.

## Optional Input: Transformations
You may also be given the SQL (or pseudo-SQL) transformation logic that derives one layer from another (e.g. staging INSERT SELECT FROM landing, analytic INSERT SELECT FROM staging). If provided, use it to:
- Confirm or strengthen cross-layer Consistency checks (e.g. a JOIN or lookup condition reveals which columns are expected to match across layers).
- Surface Accuracy/Validity signals implied by transformation logic (e.g. a CASE WHEN that maps raw codes to classifications, a CAST/rounding operation, a filter that enforces a range).
- Surface Completeness signals from filtering logic (e.g. WHERE col IS NOT NULL upstream implies the downstream column should never be null).
- Surface Uniqueness signals from deduplication logic (e.g. GROUP BY, DISTINCT, ROW_NUMBER() OVER (PARTITION BY ...) implies the partitioned columns are meant to be unique downstream).
- Identify derived/computed columns not obviously visible from type alone (e.g. a staging column computed via arithmetic or string manipulation from multiple landing columns), which are Accuracy or Consistency candidates.

If transformations are not provided, proceed using schema signals alone exactly as before -- do not skip or hedge the analysis.

## DQ Dimensions
Assess only the following six dimensions. Use these exact names in your output:
| Dimension    | Definition |
|--------------|------------|
| Completeness | Required fields are populated; nullable columns hold values when expected by business logic |
| Uniqueness   | Key columns or combinations are free of duplicates within the table |
| Validity     | Values conform to expected formats, ranges, or enumerated domain sets -- including a plausible-range or non-negativity check on a numeric column (e.g. a size, count, or score staying within its known domain). This covers the great majority of "does this value look right in isolation" checks. |
| Consistency  | Values are logically coherent within a row (intra-row) or across pipeline layers (cross-layer), including a computed/derived column's agreement with the upstream values its own transformation logic derives it from |
| Freshness    | Timestamp/date columns reflect sufficiently recent data given the table's purpose and TTL |
| Accuracy     | Values agree with an INDEPENDENT, authoritative reference or an expert-confirmed outcome external to this table's own schema/transformation (e.g. reconciliation against a source-of-truth system, a labelled/expert-approved dataset). Do NOT propose Accuracy for a plausible-range, non-negativity, or distributional-outlier check with no independent reference cited -- that is Validity, not Accuracy, regardless of the column's type. If no independent reference is identifiable for a table, Accuracy should simply not be recommended for it. |

## Signal Inventory

**Precomputed facts (`dq_signals`)**: an AssetContext may carry a `dq_signals` object -- facts extracted by code from the schema (e.g. the sub-columns of one Nested column) and, when transformation logic is given, from the SQL that produces or reads the table (a row-preserving copy, a fallback value, a filter or a function the downstream SQL applies). Each entry states a `fact`, the columns (or two `tables`) involved and the `dimension` it is most related to. A fact is evidence, not a recommendation: recommend a dimension only if you judge, from the fact and the rest of the context, that a data defect there could matter.

**Schema signals** (always available, including when NO table/column descriptions or transformation logic are provided -- these are the only signals a structure-only run has to work with, so use all of them deliberately rather than defaulting to Completeness/null-checks alone):
- **Nullability**: `Nullable(T)` columns → Completeness candidates
- **Non-Nullable String identifiers**: a `String` / `LowCardinality(String)` column that is NOT Nullable and identifies an owner or entity (`*_id`, `*_key`, `candidate_business_key` role) → Completeness candidate too: the type cannot hold NULL, so a missing value can only arrive as an empty string `''`
- **Typed enumerations**: `Enum8(...)` / `Bool` columns → Validity candidates -- the column's own declared type IS the domain here (an Enum's member list, or Bool's true/false), no description needed to know this. Treat this as a LOWER-PRIORITY Validity signal than the open-string signal below: {engine} already rejects an out-of-domain value for these types at insert time, so a dedicated test mostly re-checks a guarantee the schema already gives you. Only propose one when there's a specific reason to distrust that enforcement (e.g. the column is populated through a path that bypasses schema validation).
- **Open-string columns feeding conditional/matching logic**: a `String` / `LowCardinality(String)` column (i.e. NOT `Enum8`/`Bool`) whose value is compared against a fixed set of literals in a `CASE WHEN`, `WHERE ... IN (...)`, or similar matching condition in the transformation logic → Validity candidate, and a HIGHER-PRIORITY one than a typed enumeration: the type system does not constrain this column, so a value outside the set the matching logic expects simply fails to match, silently, with no error anywhere. When no transformation logic is available, still flag a `String`/`LowCardinality(String)` column as a lower-confidence Validity candidate if its name or description implies a small fixed vocabulary (e.g. a `*_type`, `*_status` or `*_category` column typed as String rather than Enum).
- **Timestamps / Dates**: `DateTime` / `Date` columns → Freshness candidates
- **Semantic identifiers**: columns named `*_hash`, `*_id`, `*_url` → Uniqueness candidates
- **ID/label pairs**: an `*_id` (or `*_key`) column alongside a same-prefix `*_name`/`*_label`/`*_title` column in the SAME table (e.g. `customer_id` + `customer_name`) → Consistency candidate (that pairing is expected to stay 1:1 -- flag both columns together).
- **Boolean-style naming**: columns named `is_*`, `has_*`, `should_*`, `can_*` → Validity candidate (expected domain: true/false or 0/1), even without a Bool type or description confirming it.
- **Non-negative measure naming**: columns named `*_count`, `*_total`, `*_amount`, `*_size`, `*_qty`, `*_length` → Validity candidate (a negative value is almost always a defect for this kind of column, but this is a plausible-range check against the column's own domain, not a reconciliation against an independent reference -- see the Accuracy definition above; do NOT label this Accuracy) -- flag the dimension; the exact threshold still needs supporting evidence (see the Test Specifier's rules on combining this with observed profiling data).
- **Ratio/score-style naming**: columns named `*_pct`, `*_percent`, `*_rate`, `*_ratio`, `*_score`, `*_probability` → Validity candidate -- the actual bound (0-1 vs 0-100, etc.) is NOT knowable from the name alone, so only flag the dimension; do not assert a specific range unless a description or transformation confirms one.
- **Derived/redundant columns**: e.g. two columns whose names suggest one is derived from the other (an "original_X" alongside "X", or a boolean flag alongside a status column that logically depends on it) → intra-row Consistency candidates
- **Cross-layer shared columns**: same column name and semantics across landing, staging, analytic → cross-layer Consistency candidates
- **Bounded numeric fields with a DOCUMENTED range**: e.g. a column description or table comment states "value between 0 and 1" → Validity candidate (this one specifically needs a description or transformation to exist -- do not assume a numeric column is bounded just because it looks like a score/rate by name; use the ratio/score naming signal above for that instead, which correctly stops at flagging the dimension rather than guessing the bound). Only flag Accuracy instead if the description/transformation explicitly ties the column to an independent reference or reconciliation source -- a documented range on its own is still Validity.

**Transformation signals** (only when `<TRANSFORMATIONS>` is provided):
- **CASE WHEN / mapping logic**: reveals enumerated domains or classification rules → Validity candidates (Accuracy only if the mapping is reconciling against a documented independent reference, not just classifying)
- **JOIN / lookup conditions**: reveals the exact key(s) that must align across layers → cross-layer Consistency candidates
- **WHERE / filter clauses**: reveals implicit completeness or freshness guarantees carried into the next layer → Completeness or Freshness candidates
- **GROUP BY / DISTINCT / window dedup logic**: reveals intended uniqueness keys → Uniqueness candidates
- **Arithmetic / string transformations**: reveals computed columns whose correctness depends on upstream values → Consistency candidates (a computed column agreeing with its OWN transformation logic is Consistency; only call it Accuracy if it's additionally being reconciled against a source outside this pipeline)

## Rules
- Do NOT recommend a dimension unless at least one specific column provides a concrete signal for it, from the schema and/or the transformation logic.
- Do NOT invent columns, attributes, or transformation steps not present in the provided input.
- For cross-layer checks, only flag them when the same logical column appears in at least two different pipeline layers, or when transformation logic explicitly links columns across layers.
- Each dimension entry must reference at least one column by its exact name as it appears in the schema.
- In each rationale, indicate the source of the signal: append `[schema]`, `[transformation]`, or `[schema+transformation]` at the end of the sentence.
- COVERAGE: go through every column of every table individually rather than stopping after the first one or two obvious signals per table. It is normal and expected for a table to end up with several dimension entries, and for the same column to appear under more than one dimension when it genuinely has more than one signal (e.g. a Nullable, low-cardinality column can be both a Completeness AND a Validity candidate). Do not merge unrelated columns into one dimension entry just to keep the output short -- only group columns together when the same check genuinely applies to all of them at once (e.g. a composite key, or several columns replaced by the same COALESCE/default rule). Two signal types that are easy to miss and worth checking deliberately for every table: (a) an ID/key column paired with a name/label column that should always map together (e.g. `customer_id` <-> `customer_name`, `product_id` <-> `product`) -> Consistency; (b) any Array-typed column (a tag/attribute list) -> Completeness (is it ever empty?) and Validity (what's the observed element vocabulary?).

## Output Format
Respond ONLY with the following structure — a brief plain-text summary followed by a single JSON code block. No additional prose outside this structure.

For each table, provide a one-line plain-text summary, then the full JSON:

**Summary:**
- `<layer>.<table_name>`: <one sentence describing which dimensions apply and why they matter for this table>
- ...

**DQ Dimension Map:**
```json
{{
  "tables": [
    {{
      "table": "<layer>.<table_name>",
      "dimensions": [
        {{
          "dimension": "<dimension name>",
          "columns": ["<col1>", "<col2>"],
          "rationale": "<one sentence explaining the concrete signal that justifies this dimension, ending with [schema], [transformation], or [schema+transformation]>"
        }}
      ]
    }}
  ],
  "cross_table_checks": [
    {{
      "dimension": "<dimension name>",
      "tables_involved": ["<table1>", "<table2>"],
      "columns_involved": ["<col>"],
      "rationale": "<one sentence explaining why this cross-table check is warranted, ending with [schema], [transformation], or [schema+transformation]>"
    }}
  ]
}}
```

## Schema
<SCHEMA>
{formatted_schema}
</SCHEMA>

## Transformations
<TRANSFORMATIONS>
{transformations_text}
</TRANSFORMATIONS>"""
    return prompt


_NO_TRANSFORMATIONS = "No transformation logic provided. Evaluate based on schema signals alone."


def select_dimensions(asset_contexts: dict, context_level: str, llm, engine: str = "ClickHouse"):
    """Builds the Dimension Selector prompt and calls the model; returns (dq_json, raw_response, prompt).
    A response whose JSON does not parse is retried once (malformed JSON is a random sampling slip and there is
    no partial result to keep); a second failure raises ValueError."""
    transformations_text = build_transformations_text(asset_contexts) if context_level == "C3" else _NO_TRANSFORMATIONS
    prompt = build_dimension_selector_prompt(format_asset_contexts_for_prompt(asset_contexts), transformations_text,
                                             engine)
    raw = llm(prompt)
    try:
        return extract_dq_json(raw), raw, prompt
    except ValueError as first_error:
        print(f"WARNING: Dimension Selector response could not be parsed ({first_error}) -- retrying once.")
    raw = llm(prompt)
    return extract_dq_json(raw), raw, prompt
