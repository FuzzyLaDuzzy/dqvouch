"""Stage 1, context: one AssetContext per table, built from the warehouse metadata and the transformation SQL.
Facts that are not known are written as "unknown" rather than omitted."""
import json
import os
import re

from dqvouch.signals import add_downstream_usage_and_signals, without_downstream_sql


# ---------------------------------------------------------------------------
# AssetContext construction (context acquisition).
#
# Each table gets one normalized document: context_version, asset info,
# columns, lineage, transformation, business_context. Facts we don't actually
# know are marked "unknown" rather than omitted (so the LLM can't confuse
# "not asked" with "confirmed absent") or invented.
# ---------------------------------------------------------------------------

_SEMANTIC_ROLE_PATTERNS = [
    (re.compile(r'(_id|_hash|_key)$', re.IGNORECASE), "candidate_business_key"),
    (re.compile(r'(_date|_at|_timestamp)$', re.IGNORECASE), "temporal"),
    # Generic, domain-agnostic naming conventions that give the Dimension
    # Selector something to work with from the column NAME alone (what C1 has
    # left). Deliberately weak: a role flags a CANDIDATE for a dimension or a
    # profiling pass; it never supplies a threshold on its own -- see the
    # "NOTE on semantic_roles as evidence" rule in build_test_specifier_prompt.
    (re.compile(r'^(is_|has_|should_|can_)', re.IGNORECASE), "boolean_flag"),
    (re.compile(r'(_count|_total|_amount|_size|_qty|_quantity|_length)$', re.IGNORECASE), "non_negative_measure"),
    (re.compile(r'(_pct|_percent|_rate|_ratio|_score|_probability)$', re.IGNORECASE), "bounded_ratio_candidate"),
]


def infer_semantic_roles(column_name: str):
    """
    Lightweight, purely name-based heuristic (never invents meaning from
    data), e.g. candidate_business_key on order_id.

    A column can match more than one pattern (e.g. "conversion_rate" is a
    plain name, not a boolean/id pattern, so just bounded_ratio_candidate;
    but a column could in principle be both temporal AND a business key --
    all matching roles are returned, not just the first).
    """
    roles = []
    for pattern, role in _SEMANTIC_ROLE_PATTERNS:
        if pattern.search(column_name):
            roles.append(role)
    return roles


def build_asset_contexts(metadata_rows, transformations_by_asset, context_level="C3", signals=True, sql_rules=True):
    """
    Builds one AssetContext-shaped dict per table (keyed by "database.table").
    `context_level` controls what's included:
      C1 -- structure only (names, types); C2 -- C1 + table/column descriptions;
      C3 -- C2 + transformation SQL and lineage.
    `signals=False` leaves the dq_signals out (ablation). With `sql_rules` on at
    C3, the SQL-derived signals leave the prompts (the model reads the SQL
    itself) and are kept only for the validator.
    """
    assets = {}
    for row in metadata_rows:
        table_key = f"{row['database']}.{row['table']}"
        if table_key not in assets:
            include_descriptions = context_level in ("C2", "C3")
            assets[table_key] = {
                "context_version": "v1",
                "asset": {
                    "name": table_key,
                    "kind": "table",
                    "engine": row.get("engine") or "unknown",
                    "partition_key": row.get("partition_key") or "unknown",
                    "description": (row.get("table_description") or "unknown") if include_descriptions else "unknown",
                },
                "columns": [],
                "lineage": {"source": "unknown", "upstream_assets": "unknown"},
                "transformation": {"source_file": "unknown", "raw_sql": "unknown"},
                # Not collected yet -- kept explicit and "unknown" rather than
                # omitted: unknown facts must be represented, not left for
                # the LLM to guess.
                "business_context": {"owner": "unknown", "criticality": "unknown", "update_frequency": "unknown"},
            }

        include_descriptions = context_level in ("C2", "C3")
        assets[table_key]["columns"].append({
            "name": row["column_name"],
            "type": row["data_type"],
            "description": (row.get("column_description") or "unknown") if include_descriptions else "unknown",
            "semantic_roles": infer_semantic_roles(row["column_name"]),
        })

    if context_level == "C3":
        for table_key, ctx in assets.items():
            match = (
                transformations_by_asset.get(table_key)
                or transformations_by_asset.get(table_key.split(".", 1)[-1])
            )
            if not match:
                continue
            ctx["transformation"] = match
            ctx["lineage"]["source"] = "manual"
            # Upstream assets: any OTHER known asset whose full name is
            # textually referenced inside this asset's own transformation SQL.
            # A one-level, table-level heuristic, not full lineage discovery.
            upstream = [
                other for other in assets.keys()
                if other != table_key and other in match["raw_sql"]
            ]
            ctx["lineage"]["upstream_assets"] = upstream if upstream else "unknown"

    # downstream_usage (SQL of the assets that read each table, C3 only) and
    # dq_signals (facts extracted by code from schema and SQL) -- see signals.py.
    add_downstream_usage_and_signals(assets, with_sql=context_level == "C3", with_signals=signals,
                                     sql_signals_in_prompt=not (sql_rules and context_level == "C3"))
    return assets


def _for_prompt(asset_contexts: dict) -> dict:
    """AssetContexts as the model sees them: keys starting with '_' (facts kept for the validator only) left out."""
    return {t: {k: v for k, v in ctx.items() if not k.startswith("_")} for t, ctx in asset_contexts.items()}


def format_asset_contexts_for_prompt(asset_contexts: dict) -> str:
    # Dimension Selector prompt: every asset's own SQL is already there, so
    # downstream_usage keeps only the consumer names (no duplicated SQL).
    return json.dumps(_for_prompt(without_downstream_sql(asset_contexts)), indent=2)


def build_transformations_text(asset_contexts: dict) -> str:
    """
    Derives the <TRANSFORMATIONS> prompt block from the per-asset
    transformation.raw_sql already resolved in asset_contexts, instead of
    blindly concatenating every .sql file under a folder. This means only
    transformations that were actually matched to a known asset (via
    INSERT INTO <target> detection) reach the LLM, which keeps the "evidence"
    referenced in rationales traceable back to a specific AssetContext field.
    """
    seen_files = set()
    parts = []
    for ctx in asset_contexts.values():
        transformation = ctx.get("transformation", {})
        raw_sql = transformation.get("raw_sql")
        source_file = transformation.get("source_file")
        if not raw_sql or raw_sql == "unknown":
            continue
        if source_file in seen_files:
            continue
        seen_files.add(source_file)
        parts.append(f"-- File: {source_file}\n{raw_sql}")

    if not parts:
        return "No transformation logic provided. Evaluate based on schema signals alone."
    return "\n\n".join(parts)


_INSERT_INTO = re.compile(r'INSERT\s+INTO\s+([a-zA-Z0-9_.]+)', re.IGNORECASE)
# CREATE TABLE <t> ... AS SELECT / AS ( -- a plain DDL CREATE TABLE produces no data and is not matched.
_CREATE_TABLE_AS = re.compile(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z0-9_.]+)[^;]*?\bAS\s*(?:\(|SELECT\b)',
                              re.IGNORECASE)
# CREATE [OR REPLACE] [MATERIALIZED] VIEW <v> [TO <t>]: a materialized view with TO writes into <t>.
_CREATE_VIEW = re.compile(r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?'
                          r'([a-zA-Z0-9_.]+)(?:\s+TO\s+([a-zA-Z0-9_.]+))?', re.IGNORECASE)


def _produced_tables(sql: str) -> list:
    """Every table a SQL file writes: INSERT INTO, CREATE TABLE ... AS SELECT, CREATE [MATERIALIZED] VIEW [TO]."""
    return (_INSERT_INTO.findall(sql) + _CREATE_TABLE_AS.findall(sql)
            + [to or view for view, to in _CREATE_VIEW.findall(sql)])


def load_transformations_by_asset(directory_path):
    """
    Recursively scans `directory_path` for .sql files and, for each one,
    detects which asset(s) it PRODUCES (INSERT INTO <t>, CREATE TABLE <t> AS
    SELECT, CREATE [MATERIALIZED] VIEW <t> [TO <t>]). The file name does not
    matter.

    Returns a dict {"database.table" or "table": {"source_file": rel_path,
    "raw_sql": content}}, i.e. per-asset transformations, so "transformation"
    evidence in the LLM's rationale stays traceable to the table it is about.

    Both the fully-qualified key ("db.table") and the bare table name are
    registered, so lookups from build_asset_contexts succeed regardless of
    how the target was written in the SQL file.
    """
    transformations_by_asset = {}

    if not directory_path or not os.path.isdir(directory_path):
        print("No valid directory provided. Proceeding without transformations.")
        return transformations_by_asset

    sql_files = []
    for root, dirs, files in os.walk(directory_path):
        for file in files:
            if file.endswith('.sql'):
                sql_files.append(os.path.join(root, file))

    if not sql_files:
        print(f"No .sql files found in '{directory_path}' or its subfolders.")
        return transformations_by_asset

    print(f"Found {len(sql_files)} SQL file(s) across subdirectories. Matching to assets...")
    unmatched = []

    for file_path in sql_files:
        rel_path = os.path.relpath(file_path, directory_path)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            print(f"Warning: Could not read {rel_path}. Error: {e}")
            continue

        targets = _produced_tables(content)
        if not targets:
            unmatched.append(rel_path)
            continue

        for target in targets:
            key = target.strip()
            entry = {"source_file": rel_path, "raw_sql": content}
            transformations_by_asset[key] = entry
            if "." in key:
                transformations_by_asset[key.split(".", 1)[1]] = entry

    if unmatched:
        print(f"Note: {len(unmatched)} SQL file(s) produce no detectable table (INSERT INTO / CREATE ... AS / "
              f"CREATE VIEW) and were not matched to any asset: {', '.join(unmatched)}")

    return transformations_by_asset
