"""dq_signals: facts code extracts from the schema and the transformation SQL (never test recipes)."""
import re


_ROW_CHANGING = re.compile(r"\bGROUP\s+BY\b|\bDISTINCT\b|\bJOIN\b|\bUNION\b|\brow_number\s*\(|\bLIMIT\b", re.IGNORECASE)


_TEST_FUNCTIONS = ("domain", "lower", "trim", "length")  # the row_predicate column functions


_CLAUSE = re.compile(
    r"\b(?:PREWHERE|WHERE)\b(.*?)(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bSETTINGS\b|\bPREWHERE\b"
    r"|\bWHERE\b|\bARRAY\s+JOIN\b|\bLEFT\b|\bINNER\b|\n\s*\)|$)", re.IGNORECASE | re.DOTALL)


def _key(*parts) -> str:
    return "_".join(re.sub(r"\W+", "_", str(p)).strip("_") for p in parts)


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql or "")


def _segments_reading(sql: str, table: str) -> list:
    """SELECT statements (split on ';' and UNION ALL) that read `table`."""
    ref = re.compile(rf"\b(?:FROM|JOIN)\s+{re.escape(table)}\b", re.IGNORECASE)
    segs = [p for stmt in _strip_comments(sql).split(";") for p in re.split(r"\bUNION\s+ALL\b", stmt, flags=re.IGNORECASE)]
    return [s for s in segs if ref.search(s)]


def _aliases(segment: str, table: str) -> set:
    found = re.findall(rf"\b(?:FROM|JOIN)\s+{re.escape(table)}(?:\s+(?:AS\s+)?(\w+))?", segment, re.IGNORECASE)
    stop = {"where", "prewhere", "left", "inner", "array", "group", "order", "limit", "union", "on", "settings", "final", "as"}
    return {a for a in found if a and a.lower() not in stop}


def _mentions(text: str, col: str) -> bool:
    """`col` appears in `text`, bare or behind a table alias (`e.col`)."""
    return re.search(rf"(?<![\w.`])(?:\w+\.)?`?{re.escape(col)}`?(?![\w`])", text) is not None


def _temporal(*ctxs) -> set:
    return {c["name"] for ctx in ctxs for c in ctx.get("columns", []) if "Date" in str(c.get("type", ""))}


def _is_load_window(part: str, columns: set, temporal: set) -> bool:
    """A filter on date/time columns only (one load date, a recent window...), whatever
    literal or parameter it compares to: it selects WHICH load, not which rows qualify."""
    used = {c for c in columns if _mentions(part, c)}
    return bool(used) and used <= temporal


def _trim_parens(text: str) -> str:
    """Drop only UNBALANCED leading '(' / trailing ')' left over from splitting on AND."""
    text = " ".join(text.split())
    while text.startswith("(") and text.count("(") > text.count(")"):
        text = text[1:].strip()
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1].strip()
    return text


def _filter_conditions(segment: str, columns: list, temporal: set) -> list:
    """AND-parts of the segment's WHERE/PREWHERE clauses that mention one of
    `columns` and are not just a load-window (date/time-only) filter."""
    parts = []
    for clause in _CLAUSE.findall(segment):
        for part in re.split(r"\bAND\b", clause, flags=re.IGNORECASE):
            part = _trim_parens(part)
            if part and any(_mentions(part, c) for c in columns) and not _is_load_window(part, set(columns), temporal):
                parts.append(part)
    return parts


def _functions_applied(segment: str, columns: list, aliases: set) -> list:
    found = []
    for fn, alias, col in re.findall(rf"\b({'|'.join(_TEST_FUNCTIONS)})\(\s*(?:(\w+)\.)?`?([\w.]+?)`?\s*\)", segment):
        if col in columns and (not alias or alias in aliases) and (fn, col) not in found:
            found.append((fn, col))
    return found


def _compared_values(segment: str, columns: list, aliases: set, temporal: set) -> dict:
    """{col: [literals]} for `col = 'x'` / `col IN ('x', 'y')` (optionally lower(col)) in the segment.
    Date/time columns are skipped: a literal there selects the load window, not a domain."""
    found = {}
    for col in (c for c in columns if c not in temporal):
        ref = rf"(?:lower\(\s*)?(?<![\w`.])(?:(\w+)\.)?`?{re.escape(col)}`?\s*\)?"
        for alias, rhs in re.findall(ref + r"\s*(?:=|\bIN\b)\s*(\(\s*'[^)]*\)|'[^']*')", segment, re.IGNORECASE):
            if alias and alias not in aliases:
                continue
            for v in re.findall(r"'([^']*)'", rhs):
                if v and v not in found.setdefault(col, []):
                    found[col].append(v)
    return found


def _renamed_columns(segments: list, up_cols: set, cols: set, aliases: set) -> list:
    """[(upstream_col, this_col)] for `[alias.]a AS b` that only renames a column between layers
    (a bare column, not an expression) -- the pairs a cross-layer join_key needs."""
    pairs = []
    for s in segments:
        for alias, a, b in re.findall(r"(?<![\w`.(])(?:(\w+)\.)?`?(\w+)`?\s+AS\s+`?(\w+)`?", s, re.IGNORECASE):
            if a != b and a in up_cols and b in cols and (not alias or alias in aliases) and (a, b) not in pairs:
                pairs.append((a, b))
    # one source column renamed into several columns is not a plain rename (e.g. MIN/MAX of it)
    sources = [a for a, _ in pairs]
    return [(a, b) for a, b in pairs if sources.count(a) == 1]


def _rename_in(text: str, mapping: dict) -> str:
    for old, new in mapping.items():
        if old != new:
            text = re.sub(rf"(?<![\w`.])(\w+\.)?{re.escape(old)}(?![\w`])", lambda m: (m.group(1) or "") + new, text)
    return text


def _union_branches(sql: str) -> list:
    """For each statement with UNION ALL: its branches (one list per statement). Splitting at any
    depth is enough here: each fragment still holds the FROM and the `... AS col` of its branch."""
    out = []
    for stmt in _strip_comments(sql).split(";"):
        parts = re.split(r"\bUNION\s+ALL\b", stmt, flags=re.IGNORECASE)
        if len(parts) > 1:
            out.append(parts)
    return out


def _branch_condition(sql: str, table: str, out_cols: set) -> str:
    """When `table` feeds only some branches of a UNION ALL and the other branches stamp a literal on
    an output column ('x' AS kind), the rows that come from `table` are the ones without that literal
    (or with its own literal). Returns that condition, '' if there is none."""
    ref = re.compile(rf"\b(?:FROM|JOIN)\s+{re.escape(table)}\b", re.IGNORECASE)
    lit = lambda frag: {c: v for v, c in re.findall(r"'([^']*)'\s+AS\s+`?(\w+)`?", frag, re.IGNORECASE) if c in out_cols}
    conds = []
    for branches in _union_branches(sql):
        mine = [b for b in branches if ref.search(b)]
        others = [b for b in branches if not ref.search(b)]
        if not mine or not others:
            continue
        own = {k: v for b in mine for k, v in lit(b).items()}
        other = {}
        for b in others:
            for c, v in lit(b).items():
                other.setdefault(c, set()).add(v)
        for c, vals in other.items():
            if c in own and own[c] not in vals:
                conds.append(f"{c} = '{own[c]}'")
            elif c not in own and any(re.search(rf"\bAS\s+`?{re.escape(c)}`?\b", b, re.IGNORECASE) for b in mine):
                conds.append(f"{c} NOT IN ({', '.join(repr(v) for v in sorted(vals))})")
        # own literal, while the other branches compute that column: the literal marks this branch
        for c, v in own.items():
            if c not in other and all(re.search(rf"\bAS\s+`?{re.escape(c)}`?\b", b, re.IGNORECASE) for b in others):
                conds.append(f"{c} = '{v}'")
    return " AND ".join(dict.fromkeys(conds))


# the column list of a GROUP BY / the condition of a JOIN ... ON, up to the next clause or closing parenthesis
_KEY_CLAUSE = re.compile(r"\b(?:GROUP\s+BY|ON)\b(.*?)(?=\bHAVING\b|\bORDER\b|\bLIMIT\b|\bWHERE\b|\bPREWHERE\b|\bSETTINGS\b"
                         r"|\bLEFT\b|\bINNER\b|\bRIGHT\b|\bFULL\b|\bJOIN\b|\bARRAY\b|\bUNION\b|\bSELECT\b|\)|;|$)",
                         re.IGNORECASE | re.DOTALL)


def _guard(text: str, start: int, end: int, col: str) -> str:
    """The other AND-parts of the boolean chain around text[start:end] (bounded by the enclosing
    parenthesis or by a ',' at the same depth): the condition under which that comparison applies."""
    depth, i = 0, start - 1
    while i >= 0:
        ch = text[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i -= 1
    depth, j = 0, end
    while j < len(text):
        ch = text[j]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        j += 1
    # the chain also ends at a clause keyword (e.g. an ARRAY JOIN list followed by PREWHERE ... WHERE ...)
    before = list(_CLAUSE_KEYWORD.finditer(text, i + 1, start))
    lo = before[-1].end() if before else i + 1
    after = _CLAUSE_KEYWORD.search(text, end, j)
    hi = after.start() if after else j
    parts = [_trim_parens(p) for p in re.split(r"\bAND\b", text[lo:hi], flags=re.IGNORECASE)]
    return " AND ".join(p for p in parts if p and not _mentions(p, col))


_CLAUSE_KEYWORD = re.compile(r"\b(?:WHERE|PREWHERE|HAVING|ON|WHEN|THEN|ELSE|SELECT|FROM|GROUP\s+BY|ORDER\s+BY|LIMIT|JOIN|AS)\b|;",
                             re.IGNORECASE)


def _conditional_values(segment: str, columns: list, aliases: set, temporal: set) -> dict:
    """{(col, guard): [literals]} for `col = 'x'` / `col IN (...)` that sit inside an AND chain with
    other conditions on this table's columns (the guard) -- values allowed only for some rows."""
    found = {}
    for col in (c for c in columns if c not in temporal):
        ref = rf"(?:lower\(\s*)?(?<![\w`.])(?:(\w+)\.)?`?{re.escape(col)}`?\s*\)?"
        for m in re.finditer(ref + r"\s*(?:=|\bIN\b)\s*(\(\s*'[^)]*\)|'[^']*')", segment, re.IGNORECASE):
            if m.group(1) and m.group(1) not in aliases:
                continue
            guard = _guard(segment, m.start(), m.end(), col)
            guard_cols = [c for c in columns if _mentions(guard, c)]
            if not guard or not guard_cols or _is_load_window(guard, set(columns), temporal):
                continue
            vals = found.setdefault((col, guard), [])
            vals.extend(v for v in re.findall(r"'([^']*)'", m.group(2)) if v and v not in vals)
    return found


def _guarded_functions(segment: str, columns: list, aliases: set, temporal: set) -> dict:
    """{(fn, col, guard): True} for domain()/length() applied to a column inside an AND chain with other
    conditions on this table's columns -- e.g. `WHERE owner IS NOT NULL AND domain(a) != domain(b)`:
    the function only has to work on the rows the guard selects."""
    found = {}
    for m in re.finditer(r"\b(domain|length)\(\s*(?:(\w+)\.)?`?([\w.]+?)`?\s*\)", segment):
        fn, alias, col = m.group(1), m.group(2), m.group(3)
        if col not in columns or (alias and alias not in aliases):
            continue
        guard = _guard(segment, m.start(), m.end(), col)
        if guard and [c for c in columns if _mentions(guard, c)] and not _is_load_window(guard, set(columns), temporal):
            found[(fn, col, guard)] = True
    return found


_NOT_NULL = re.compile(r"^(?:isNotNull\(\s*(?:\w+\.)?`?(\w+)`?\s*\)|(?:\w+\.)?`?(\w+)`?\s+IS\s+NOT\s+NULL)$", re.IGNORECASE)


_PROPAGATED = ("downstream_filter", "downstream_function", "downstream_values", "downstream_required", "downstream_key")


def _propagate_upstream(assets: dict, signals: dict, renames: dict, hops: int = 2) -> None:
    """Carry downstream_* signals from a table to the table it is built from, when every column
    the signal is about reaches it unchanged (same name, or a plain `a AS b` rename) -- so a rule
    applied two layers downstream still reaches the landing table. Marked with `via`."""
    for _ in range(hops):
        for t, tctx in assets.items():
            ups = tctx.get("lineage", {}).get("upstream_assets")
            for u in ups if isinstance(ups, list) else []:
                if u not in assets:
                    continue
                ucols = {c["name"] for c in assets[u].get("columns", [])}
                to_up = {c["name"]: c["name"] for c in tctx.get("columns", []) if c["name"] in ucols}
                to_up.update({b: a for a, b in renames.get((u, t), [])})
                for key, sig in list(signals.get(t, {}).items()):
                    cols = sig.get("columns") or []
                    if not key.startswith(_PROPAGATED) or key.count("_via_") >= hops or not cols \
                            or not all(c in to_up for c in cols):
                        continue
                    new_key = f"{key}_via_{_key(t)}"
                    if new_key in signals.setdefault(u, {}):
                        continue
                    m = {c: to_up[c] for c in cols}
                    new = {**sig, "columns": [m[c] for c in cols], "via": t,
                           "fact": f"(via {t}, which carries these columns unchanged) " + _rename_in(sig["fact"], m)}
                    if "condition" in sig:
                        new["condition"] = _rename_in(sig["condition"], m)
                    signals[u][new_key] = new


def _parallel_array_signals(ctx: dict) -> dict:
    groups = {}
    for c in ctx.get("columns", []):
        if "." in c["name"] and str(c.get("type", "")).startswith("Array("):
            groups.setdefault(c["name"].rsplit(".", 1)[0], []).append(c["name"])
    return {
        _key("parallel_arrays", prefix): {
            "source": "schema", "dimension": "Consistency", "columns": cols,
            "fact": f"{', '.join(cols)} are the sub-columns of one Nested column {prefix}",
        }
        for prefix, cols in groups.items() if len(cols) >= 2
    }


def _own_sql_signals(table: str, ctx: dict, assets: dict) -> dict:
    sql = _strip_comments(ctx.get("transformation", {}).get("raw_sql", ""))
    if not sql or sql == "unknown":
        return {}
    cols = {c["name"] for c in ctx.get("columns", [])}
    signals = {}

    upstream = ctx.get("lineage", {}).get("upstream_assets")
    if isinstance(upstream, list) and len(upstream) == 1 and not _ROW_CHANGING.search(sql):
        up = assets.get(upstream[0], {})
        known = cols | {c["name"] for c in up.get("columns", [])}
        clauses = [p for c in _CLAUSE.findall(sql) for p in re.split(r"\bAND\b", c, flags=re.IGNORECASE) if p.strip()]
        if all(_is_load_window(p, known, _temporal(ctx, up)) for p in clauses):
            signals[_key("row_preserving_copy_of", upstream[0])] = {
                "source": "transformation", "dimension": "Consistency", "tables": [upstream[0], table],
                "fact": f"the SQL of {table} reads only {upstream[0]} and does not filter (other than the load "
                        "date), group, deduplicate or join its rows",
            }

    def add(kind, out, detail, fact):
        if out in cols:
            signals[_key(kind, out)] = {"source": "transformation", "dimension": "Validity", "columns": [out],
                                        **detail, "fact": fact}

    for src, lit, out in re.findall(r"\bif\(\s*\(?\s*(?:\w+\.)?(\w+)\s+IS\s+NULL\s+OR\s+(?:\w+\.)?\1\s*=\s*''\s*\)?\s*,"
                                    r"\s*'([^']*)'\s*,\s*(?:\w+\.)?\1\s*\)\s+AS\s+(\w+)", sql, re.IGNORECASE):
        add("fallback_value", out, {"fallback": lit}, f"the SQL writes '{lit}' into {out} when {src} is NULL or empty")
    for null_src, src, lit, out in re.findall(
            r"\bCASE\s+WHEN\s+\(?\s*(?:(?:\w+\.)?(\w+)\s+IS\s+NULL\s+OR\s+)?(?:\w+\.)?(\w+)\s*=\s*''\s*\)?\s+"
            r"THEN\s+'([^']*)'\s+ELSE\s+(?:\w+\.)?\2\s+END\s+AS\s+(\w+)", sql, re.IGNORECASE):
        if null_src in ("", src):
            when = "is NULL or empty" if null_src else "is empty"
            add("fallback_value", out, {"fallback": lit}, f"the SQL writes '{lit}' into {out} when {src} {when}")
    for src, lit, out in re.findall(r"\b(?:coalesce|ifNull)\(\s*(?:\w+\.)?(\w+)\s*,\s*'([^']*)'\s*\)\s+AS\s+(\w+)", sql, re.IGNORECASE):
        add("fallback_value", out, {"fallback": lit}, f"the SQL writes '{lit}' into {out} when {src} is NULL")
    for a, b, out in (re.findall(r"\b(?:coalesce|ifNull)\(\s*(?:\w+\.)?(\w+)\s*,\s*(?:\w+\.)?(\w+)\s*\)\s+AS\s+(\w+)", sql, re.IGNORECASE)
                      + [(x, y, o) for x, y, o in re.findall(r"\bif\(\s*isNull\(\s*(?:\w+\.)?(\w+)\s*\)\s*,\s*(?:\w+\.)?(\w+)\s*,"
                                                            r"\s*(?:\w+\.)?\1\s*\)\s+AS\s+(\w+)", sql, re.IGNORECASE)]):
        add("fallback_column", out, {"fallback_from": [a, b]}, f"the SQL computes {out} as {a}, or {b} when {a} is NULL")
    for col in re.findall(r"\bif\(\s*empty\(\s*(?:\w+\.)?(\w+)\s*\)\s*,\s*NULL\s*,\s*(?:\w+\.)?\1\s*\)", sql, re.IGNORECASE):
        add("empty_to_null", col, {}, f"the SQL turns an empty {col} into NULL")

    part_key = str(ctx.get("asset", {}).get("partition_key", ""))
    temporal = _temporal(ctx, *(assets.get(u, {}) for u in (upstream if isinstance(upstream, list) else [])))
    known = cols | {c["name"] for u in (upstream if isinstance(upstream, list) else []) for c in assets.get(u, {}).get("columns", [])}
    loads_window = any(_is_load_window(p, known, temporal)
                       for c in _CLAUSE.findall(sql) for p in re.split(r"\bAND\b", c, flags=re.IGNORECASE) if p.strip())
    for col in (c for c in sorted(cols & _temporal(ctx)) if _mentions(part_key, c)) if loads_window else []:
        signals[_key("loaded_per_partition", col)] = {
            "source": "transformation", "dimension": "Uniqueness", "columns": [col],
            "fact": f"{table} is partitioned by {col} and its SQL loads one {col} window per run"}
    return signals


def add_downstream_usage_and_signals(assets: dict, with_sql: bool, with_signals: bool = True,
                                     sql_signals_in_prompt: bool = True) -> None:
    """In place: `lineage.downstream_assets` + `downstream_usage` (C3 only, needs SQL)
    and `dq_signals` (schema signals at every level, SQL signals when with_sql).
    with_signals=False keeps the raw context and adds no dq_signals at all (ablation).
    sql_signals_in_prompt=False (Invariant Extractor on: the model reads the SQL itself) moves the
    SQL-derived signals to `_sql_facts`, which prompts never show and the validator still uses."""
    signals = {t: _parallel_array_signals(ctx) for t, ctx in assets.items()}
    renames = {}  # (upstream, consumer) -> [(upstream_col, consumer_col)]
    if with_sql:
        for table, ctx in assets.items():
            signals[table].update(_own_sql_signals(table, ctx, assets))
        for consumer, cctx in assets.items():
            sql = cctx.get("transformation", {}).get("raw_sql", "")
            upstream = cctx.get("lineage", {}).get("upstream_assets")
            filtered = set()  # sources this consumer reads through a (non load-window) filter
            for table in upstream if isinstance(upstream, list) else []:
                tctx = assets.get(table)
                if not tctx:
                    continue
                tctx["lineage"].setdefault("downstream_assets", []).append(consumer)
                tctx.setdefault("downstream_usage", {})[_key(consumer)] = {
                    "consumer": consumer, "source_file": cctx["transformation"].get("source_file"), "raw_sql": sql}
                cols = [c["name"] for c in tctx.get("columns", [])]
                segs = _segments_reading(sql, table)
                pairs = _renamed_columns(segs, set(cols), {c["name"] for c in cctx.get("columns", [])},
                                         {a for s in segs for a in _aliases(s, table)})
                if pairs:
                    renames[(table, consumer)] = pairs  # propagation still uses every rename
                    # a date/time rename is the same value but not a key: many rows share a timestamp
                    pairs = [(a, b) for a, b in pairs if not (a in _temporal(tctx) and b in _temporal(cctx))]
                if pairs:
                    signals[consumer][_key("column_mapping_from", table)] = {
                        "source": "transformation", "dimension": "Consistency", "upstream": table,
                        "pairs": [list(p) for p in pairs],
                        "fact": f"{consumer} takes these columns of {table} under another name: "
                                f"{', '.join(f'{a} -> {b}' for a, b in pairs)}"}
                conds = list(dict.fromkeys(p for s in segs for p in _filter_conditions(s, cols, _temporal(tctx))))
                branch = _branch_condition(sql, table, {c["name"] for c in cctx.get("columns", [])})
                if branch:
                    signals[consumer][_key("union_branch_from", table)] = {
                        "source": "transformation", "dimension": "Consistency", "upstream": table, "condition": branch,
                        "fact": f"{consumer} is a UNION ALL; its rows where {branch} come from {table}"}
                required = [m.group(1) or m.group(2) for p in conds for m in [_NOT_NULL.match(p)] if m]
                rest = [p for p in conds if not _NOT_NULL.match(p)]
                for col in (c for c in required if c in cols) if rest else []:
                    when = " AND ".join(rest)[:300]
                    signals[table][_key("downstream_required", col, consumer)] = {
                        "source": "transformation", "dimension": "Completeness", "consumer": consumer, "condition": when,
                        "columns": [col] + [c for c in cols if c != col and _mentions(when, c)],
                        "fact": f"{consumer} reads the rows of {table} where {when}, and of those only the ones "
                                f"where {col} IS NOT NULL"}
                if conds:
                    cond = " AND ".join(conds)[:400]
                    signals[table][_key("downstream_filter", consumer)] = {
                        "source": "transformation", "dimension": "Validity", "consumer": consumer, "condition": cond,
                        "columns": [c for c in cols if _mentions(cond, c)],
                        "fact": f"{consumer} reads only the rows of {table} where {cond}"}
                    signals[consumer][_key("source_filter", table)] = {
                        "source": "transformation", "dimension": "Consistency", "upstream": table, "condition": cond,
                        **({"consumer_condition": branch} if branch else {}),
                        "fact": f"{consumer} reads {table} only where {cond}"
                                + (f"; its rows from {table} are those where {branch}" if branch else "")}
                    filtered.add(table)
                values = {}
                for s in segs:
                    for col, vals in _compared_values(s, cols, _aliases(s, table), _temporal(tctx)).items():
                        values.setdefault(col, []).extend(v for v in vals if v not in values.get(col, []))
                for col, vals in values.items():
                    signals[table][_key("downstream_values", col)] = {
                        "source": "transformation", "dimension": "Validity", "consumer": consumer, "columns": [col],
                        "values": vals,
                        "fact": f"{consumer} compares {col} with the literals {vals}"}
                conditional = {}
                for s in segs:
                    for (col, guard), vals in _conditional_values(s, cols, _aliases(s, table), _temporal(tctx)).items():
                        conditional.setdefault((col, guard), []).extend(v for v in vals if v not in conditional.get((col, guard), []))
                by_col = {}
                for (col, guard), vals in conditional.items():
                    by_col.setdefault(col, []).append((guard, vals))
                for col, groups in by_col.items():
                    # same guard text -> one rule; values written under it are the allowed domain for those rows
                    for n, (guard, vals) in enumerate(groups, 1):
                        signals[table][_key("downstream_values_when", col, n)] = {
                            "source": "transformation", "dimension": "Validity", "consumer": consumer,
                            "columns": [col] + [c for c in cols if c != col and _mentions(guard, c)],
                            "condition": guard[:300], "values": vals,
                            "fact": f"{consumer} compares {col} with the literals {vals} where {guard[:300]}"}
                key_text = " ".join(t for s in segs for t in _KEY_CLAUSE.findall(s))
                keys_used = {c for c in cols if c not in _temporal(tctx) and _mentions(key_text, c)}
                part_cols = {c for c in cols if c in _temporal(tctx) and any(
                    re.search(rf"\b(?:date|toDate|toStartOfDay)\(\s*(?:\w+\.)?`?{re.escape(c)}`?\s*\)\s+AS\s+`?{re.escape(p)}`?",
                              s, re.IGNORECASE)
                    for s in segs for p in _temporal(cctx) if _mentions(str(cctx.get("asset", {}).get("partition_key", "")), p))}
                for col in sorted(keys_used | part_cols):
                    why = (f"derives its load partition from {col}" if col in part_cols
                           else f"joins or groups rows by {col}")
                    signals[table][_key("downstream_key", col, consumer)] = {
                        "source": "transformation", "dimension": "Completeness", "consumer": consumer, "columns": [col],
                        "fact": f"{consumer} {why}"}
                guarded = {}
                for s in segs:
                    guarded.update(_guarded_functions(s, cols, _aliases(s, table), _temporal(tctx)))
                for n, (fn, col, guard) in enumerate(guarded, 1):
                    signals[table][_key("downstream_function_when", fn, col, n)] = {
                        "source": "transformation", "dimension": "Validity", "consumer": consumer, "function": fn,
                        "columns": [col] + [c for c in cols if c != col and _mentions(guard, c)], "condition": guard[:300],
                        "fact": f"{consumer} applies {fn}() to {col} where {guard[:300]}"}
                for fn, col in dict.fromkeys(f for s in segs for f in _functions_applied(s, cols, _aliases(s, table))):
                    signals[table][_key("downstream_function", fn, col)] = {
                        "source": "transformation", "dimension": "Validity", "consumer": consumer, "columns": [col], "function": fn,
                        "fact": f"{consumer} applies {fn}() to {col}"}
            ups = [u for u in (upstream if isinstance(upstream, list) else []) if u in assets]
            for branches in _union_branches(sql):
                fed = [u for u in ups if any(re.search(rf"\b(?:FROM|JOIN)\s+{re.escape(u)}\b", b, re.IGNORECASE) for b in branches)
                       and not all(re.search(rf"\b(?:FROM|JOIN)\s+{re.escape(u)}\b", b, re.IGNORECASE) for b in branches)]
                if len(fed) >= 2:
                    # a branch condition on a computed alias (not a source column) still drops source rows
                    for u in fed:
                        ucols = {c["name"] for c in assets[u].get("columns", [])}
                        parts = [p for b in branches if re.search(rf"\b(?:FROM|JOIN)\s+{re.escape(u)}\b", b, re.IGNORECASE)
                                 for c in _CLAUSE.findall(b) for p in re.split(r"\bAND\b", c, flags=re.IGNORECASE) if p.strip()]
                        if any(not _is_load_window(p, ucols, _temporal(assets[u])) for p in parts):
                            filtered.add(u)
                    unfiltered = [u for u in fed if u not in filtered]
                    signals[consumer][_key("union_of_sources")] = {
                        "source": "transformation", "dimension": "Consistency", "tables": fed,
                        "unfiltered_sources": unfiltered,
                        "fact": f"{consumer} is the UNION ALL of rows from {', '.join(fed)}; read in full: "
                                f"{', '.join(unfiltered) or 'none'}; read through a filter: "
                                f"{', '.join(u for u in fed if u in filtered) or 'none'}"}
                    break
        _propagate_upstream(assets, signals, renames)
    for table, sig in signals.items() if with_signals else ():
        sig = _condense(sig)
        hidden = {} if sql_signals_in_prompt else {k: s for k, s in sig.items() if s.get("source") != "schema"}
        sig = {k: s for k, s in sig.items() if k not in hidden}
        if sig:
            assets[table]["dq_signals"] = sig
        if hidden:
            assets[table]["_sql_facts"] = hidden


def _condense(sig: dict) -> dict:
    """One signal per fact. The same fact reaches a table through several consumers and `_via_`
    paths; duplicates and weak facts crowd the prompt (seen: 35 Validity signals on one table).
    Single-member groups keep their key, so evidence paths stay stable."""
    if not sig:
        return sig
    out, groups = {}, {}
    weak_fn = {"lower", "trim"}  # a downstream lower()/trim() says little about valid values
    when_cols = {s["columns"][0] for k, s in sig.items() if k.startswith("downstream_values_when_") and s.get("columns")}
    when_fns = {(s.get("function"), s["columns"][0]) for k, s in sig.items() if k.startswith("downstream_function_when_") and s.get("columns")}
    for key, s in sig.items():
        col = (s.get("columns") or [None])[0]
        if key.startswith("downstream_function_") and s.get("function") in weak_fn:
            continue
        if key.startswith("downstream_function_") and not key.startswith("downstream_function_when_") \
                and (s.get("function"), col) in when_fns:
            continue  # the guarded version says on which rows the function must work
        if key.startswith("downstream_values_when_"):
            groups.setdefault(("downstream_values_when", col), []).append((key, s))
        elif key.startswith("downstream_values_"):
            if col not in when_cols:  # the conditional version says strictly more
                groups.setdefault(("downstream_values", col), []).append((key, s))
        elif key.startswith(("downstream_key_", "downstream_function_", "downstream_required_", "downstream_filter_")):
            kind = key.split("_")[0] + "_" + key.split("_")[1]
            groups.setdefault((kind, col, s.get("function"), s.get("condition") if kind != "downstream_key" else None), []).append((key, s))
        else:
            out[key] = s
    for g, members in groups.items():
        members.sort(key=lambda m: ("via" in m[1], len(m[0])))  # direct consumer first
        if len(members) == 1:
            out[members[0][0]] = members[0][1]
            continue
        first = dict(members[0][1])
        if g[0] == "downstream_values_when":
            cases = []
            for _, s in members:
                case = next((c for c in cases if c["condition"] == s["condition"]), None)
                if case:
                    case["values"] += [v for v in s["values"] if v not in case["values"]]
                else:
                    cases.append({"condition": s["condition"], "values": list(s["values"])})
            first.update(cases=cases, columns=list(dict.fromkeys(c for _, s in members for c in s["columns"])),
                         fact=f"downstream SQL compares {g[1]} with different literals depending on the row: "
                              + "; ".join(f"where {c['condition']}: {c['values']}" for c in cases))
            first.pop("condition", None)
            first.pop("values", None)
            out[f"downstream_values_when_{_key(g[1])}"] = first
        else:
            if g[0] == "downstream_values":
                first["values"] = list(dict.fromkeys(v for _, s in members for v in s["values"]))
            first["consumers"] = sorted({s.get("consumer") or s.get("via") for _, s in members} - {None})
            out[members[0][0]] = first
    return out


def without_downstream_sql(assets: dict) -> dict:
    """Copy for prompts that already include every asset's own SQL (the Dimension
    Selector): keep who reads each table, drop the duplicated SQL text."""
    return {t: ({**ctx, "downstream_usage": sorted(ctx["downstream_usage"])} if "downstream_usage" in ctx else ctx)
            for t, ctx in assets.items()}
