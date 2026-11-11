"""SQL Rule Extractor: two LLM calls per transformation list what its SQL guarantees about the output and what it
requires of its inputs, each tied to the SQL excerpt that enforces it and validated against that SQL."""
import json

from dqvouch.llm import extract_dq_json
from dqvouch.validator import INPUT_CATEGORIES, INVARIANT_CATEGORIES, validate_invariants


# ---------------------------------------------------------------------------
# Invariant Extractor (sql_rules on, C3): asks "what does this
# transformation guarantee?" instead of "which dimensions fit this column?".
# The categories are what each SQL operator guarantees -- generic, not taken
# from any test set; the model finds the guarantees, the validator only checks
# that each one quotes SQL that is really there (validate_invariants).
# ---------------------------------------------------------------------------
INVARIANT_DIMENSION = {"row_provenance": "Consistency", "row_completeness": "Consistency", "branch_source": "Consistency",
                       "derivation": "Consistency", "value_domain": "Validity", "presence": "Completeness", "grain": "Uniqueness",
                       "input_values": "Validity", "input_function": "Validity", "input_presence": "Completeness"}


assert set(INVARIANT_DIMENSION) == set(INVARIANT_CATEGORIES + INPUT_CATEGORIES)


_OUTPUT_CATEGORIES_TEXT = """- row_provenance: a WHERE / JOIN / IN-subquery keeps only input rows meeting a condition, so every output row comes from an input row meeting it. tables = [output, input]; condition = that condition.
- row_completeness: input rows reach the output without filtering, grouping or de-duplication (other than selecting the load date or partition), so no input row is lost. tables = [output, input].
- value_domain: a CASE / multiIf / if / IN / literal list makes a column, or a combination of columns, take only certain values -- possibly only under a condition. tables = [output]; values = the allowed values or combinations; condition if any.
- presence: COALESCE / ifNull / if(... IS NULL ...) / IS NOT NULL / a fallback literal makes a column never NULL or empty in the output, or equal to the fallback when the input value was missing. tables = [output], or [output, input] when it depends on the input; condition if any.
- grain: GROUP BY / DISTINCT / ROW_NUMBER / LIMIT BY leaves one output row per key (per load window when the SQL loads one at a time). tables = [output]; columns = the key.
- branch_source: in a UNION ALL, a branch stamps a marker value (a literal written into a column) on its rows, so output rows carrying that marker come from that branch's input. tables = [output, input]; condition = the marker test on the output.
- derivation: an output column is computed from input columns by an expression. tables = [output] or [output, input]; condition = the expression."""


_INPUT_CATEGORIES_TEXT = """- input_values: the SQL compares an input column with literal values (CASE / multiIf / IN / =), possibly only under a condition, so other values silently match nothing. tables = [that input]; values = the literals; condition if any. One entry per column and condition.
- input_function: the SQL applies a function to an input column that needs a usable value (e.g. a URL whose domain is taken, a string turned into a date), possibly only under a condition. tables = [that input]; condition = the function call, and its guard if any.
- input_presence: the SQL keeps, joins or groups input rows only when a column is present (IS NOT NULL, a join key, a GROUP BY key), so rows without it are lost. tables = [that input]; condition if any."""


def build_invariant_extractor_prompt(target: str, asset_contexts: dict, part: str = "output") -> str:
    """part="output": what the SQL GUARANTEES about its output; part="input": what it REQUIRES of the tables it
    reads. Two focused calls work better for a small model than one prompt asking for both."""
    ctx = asset_contexts[target]
    ups = ctx.get("lineage", {}).get("upstream_assets")
    ups = [u for u in ups if u in asset_contexts] if isinstance(ups, list) else []
    columns = {t: [f"{c.get('name')} {c.get('type', '')}" for c in asset_contexts[t].get("columns", [])] for t in [target] + ups}
    if part == "input":
        task = ("List what it REQUIRES of the tables it reads: input rows that do not meet a requirement are dropped, "
                "fall through or break silently. Only what the SQL itself relies on -- no business assumptions, no "
                "guesses about the data, no tests yet.")
        categories = _INPUT_CATEGORIES_TEXT
        pairs_rule = ""
        example = ('{"invariants": [{"category": "input_values", "tables": ["db.orders_raw"], "columns": ["status"], '
                   '"condition": "channel = \'web\'", "values": ["paid", "refunded"], '
                   '"sql_quote": "CASE WHEN status IN (\'paid\', \'refunded\')"}]}')
    else:
        task = ("List what it GUARANTEES about its output table, relative to the tables it reads: the rules every output "
                "row satisfies BECAUSE of this SQL. Only what the SQL itself enforces -- no business assumptions, no "
                "guesses about the data, no tests yet.")
        categories = _OUTPUT_CATEGORIES_TEXT
        pairs_rule = ("- column_pairs (only for a guarantee between two tables): the SMALLEST set of [output column, input "
                      "column] pairs that identifies the same row on both sides -- identifier or hash columns (*_id, *_hash, "
                      "*_key) carried through, also when renamed. Not every column that is copied, and never a date/time "
                      "column. A test comparing the two tables would join on them.\n")
        example = ('{"invariants": [{"category": "row_provenance", "tables": ["db.orders_clean", "db.orders_raw"], '
                   '"columns": ["status"], "condition": "status != \'test\'", "values": null, '
                   '"column_pairs": [["order_key", "order_id"]], "sql_quote": "WHERE status != \'test\'"}]}')
    return f"""You are a data engineer reading ONE SQL transformation. {task}

Output table: {target}
Tables it reads: {", ".join(ups) or "(none known)"}

## Columns
{json.dumps(columns, indent=1)}

## Transformation SQL
{ctx["transformation"]["raw_sql"]}

## Categories -- use only these; one entry per item; skip a category that does not apply
{categories}

## Rules
- sql_quote: copy, verbatim, the shortest excerpt of the SQL above that shows it (it is checked against the SQL text).
- columns: the column names involved, exactly as listed under Columns.
{pairs_rule}- Write conditions with column names only, without table aliases.
- A comparison that only picks the load window -- a date/time column against a run parameter such as {{load_date:Date}} or a placeholder such as 'EXECUTION_DATE' -- is how the job is scheduled, not a rule on the rows: leave it out of conditions, and never report it on its own.
- Prefer few, precise items over many vague ones.

## Output -- one ```json block, nothing else
```json
{example}
```"""


def run_invariant_extractor(asset_contexts: dict, llm, progress=None):
    """Two calls per transformation (output guarantees, input requirements); validated together, then stored on
    the table each one is about (cited as evidence `sql_invariants.<id>`). Returns {calls, accepted, rejected}."""
    progress = progress or (lambda message: None)
    record = {"calls": [], "accepted": [], "rejected": []}
    tables = [t for t, ctx in asset_contexts.items()
              if (ctx.get("transformation") or {}).get("raw_sql") not in (None, "", "unknown")]
    n, i = 2 * len(tables), 0
    for table in tables:
        raw_all = []
        for part in ("output", "input"):
            i += 1
            progress(f"SQL Rule Extractor: {table} ({part}), call {i} of {n}")
            prompt = build_invariant_extractor_prompt(table, asset_contexts, part)
            call, raw = {"scope": f"{table} ({part})", "prompt": prompt}, []
            for attempt in (1, 2):  # one retry on malformed JSON, as in the other stages
                call["raw_response"] = llm(prompt)
                try:
                    raw = extract_dq_json(call["raw_response"]).get("invariants", [])
                    break
                except (ValueError, AttributeError) as e:
                    call[f"parse_error_{attempt}"] = str(e)
                    print(f"WARNING: Invariant Extractor response for [{table} ({part})] could not be parsed ({e})"
                          + (" -- retrying once." if attempt == 1 else " -- nothing from this call."))
            record["calls"].append(call)
            raw_all += raw if isinstance(raw, list) else []
        accepted, rejected = validate_invariants(raw_all, table, asset_contexts)
        _store_invariants(asset_contexts, accepted)
        record["accepted"] += accepted
        record["rejected"] += rejected
        print(f"Invariant Extractor [{table}]: {len(accepted)} accepted, {len(rejected)} rejected")
    return record


def _store_invariants(asset_contexts: dict, accepted: list) -> None:
    """Each guarantee goes to the AssetContext of the table it is about: the output, or -- for an input_*
    requirement -- the input table, so its Test Specifier call (and evidence path) is that table's."""
    for inv in accepted:
        asset_contexts[inv["tables"][0]].setdefault("sql_invariants", {})[inv["id"]] = inv


def _branch_note(inv: dict, ctx: dict) -> str:
    """For a two-table guarantee whose output is a UNION ALL (it has branch_source guarantees): the condition that
    selects the output rows coming from this input -- its own branch marker, or not any other branch's marker."""
    branches = [b for b in (ctx.get("sql_invariants") or {}).values() if b["category"] == "branch_source" and b.get("condition")]
    if not branches or len(inv["tables"]) != 2 or inv["category"] == "branch_source":
        return ""
    mine = [b["condition"] for b in branches if b["tables"][-1] == inv["tables"][1]]
    others = [b["condition"] for b in branches if b["tables"][-1] != inv["tables"][1]]
    cond = " AND ".join(mine) if mine else " AND ".join(f"NOT ({c})" for c in others)
    return f"; the output is a UNION ALL -- its rows from {inv['tables'][1]} are those where {cond}" if cond else ""


def with_invariant_items(dim_accepted: dict, asset_contexts: dict) -> dict:
    """The accepted recommendations plus one checklist item per SQL guarantee: a one-table guarantee joins its
    table's items (the table gets a Test Specifier call even if the DS accepted nothing for it); a two-table one
    becomes a cross-table item, creating that pair's call when the DS did not propose the pair."""
    out = {"tables": [dict(t, dimensions=list(t.get("dimensions", []))) for t in dim_accepted.get("tables", [])],
           "cross_table_checks": list(dim_accepted.get("cross_table_checks", []) or [])}
    by_table = {t.get("table"): t for t in out["tables"]}
    for table, ctx in asset_contexts.items():
        for inv in (ctx.get("sql_invariants") or {}).values():
            if inv["category"] == "derivation":  # context only: an expression rarely fits the catalog, a guess fails
                continue
            why = (f"SQL guarantee sql_invariants.{inv['id']} ({inv['category']})"
                   + (f": {inv['condition']}" if inv.get("condition") else "")
                   + (f"; values {inv['values']}" if inv.get("values") else "")
                   + (f"; same row on both sides: {', '.join(f'{a} = {b}' for a, b in inv['column_pairs'])} "
                      f"([{inv['tables'][0]} column, {inv['tables'][1]} column])" if inv.get("column_pairs") else "")
                   + _branch_note(inv, ctx)
                   + f" -- enforced by: {inv['sql_quote']}")
            dim = INVARIANT_DIMENSION[inv["category"]]
            if len(inv["tables"]) == 1:
                if table not in by_table:
                    by_table[table] = {"table": table, "dimensions": []}
                    out["tables"].append(by_table[table])
                by_table[table]["dimensions"].append({"dimension": dim, "columns": inv["columns"], "rationale": why})
            else:
                out["cross_table_checks"].append({"tables_involved": inv["tables"], "dimension": dim,
                                                  "columns_involved": inv["columns"], "rationale": why})
    return out
