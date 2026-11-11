"""Thesis regression checks (column functions, cross-asset filters/composite keys, signals, validator,
SQL guarantees), ported from the original prototype. The asserts run at import, as in the original."""
from dqvouch.validator import validate_dimension_output, validate_test_specifications
from dqvouch.render_sql import (
    ASSERTION_RENDERERS, _describe_assertion, render_cross_asset_missing_rows, render_row_predicate,
)

CTX = {
    "db.events": {"columns": [{"name": n, "type": t} for n, t in
                              [("uri", "Array(String)"), ("domain", "Array(String)"), ("sources.uri", "Array(String)"),
                               ("page_url", "String"), ("ts", "DateTime"), ("hash", "UInt64")]]},
    "db.findings": {"columns": [{"name": n, "type": t} for n, t in
                                [("finding", "String"), ("session_hash", "UInt64"), ("brand", "Nullable(String)")]]},
}


def spec(assertion, asset="db.events", evidence="columns.uri.type"):
    return {"id": "t", "asset": asset, "assertion": assertion, "evidence": [{"path": evidence}],
            "rationale": "r", "status": "proposed",
            "threshold": {"metric": "failed_rows", "operator": "equals", "value": 0}}


def accepted(s):
    return bool(validate_test_specifications([s], CTX, ASSERTION_RENDERERS.keys()).accepted)


# 1. column functions render, describe, validate
same_len = {"type": "row_predicate", "condition": {
    "op": "eq", "left": {"function": "length", "column": "uri"}, "right": {"function": "length", "column": "domain"}}}
assert "countIf(NOT (length(uri) = length(domain)))" in render_row_predicate(spec(same_len))
assert "length(uri) == length(domain)" in _describe_assertion(spec(same_len))
assert accepted(spec(same_len))
assert accepted(spec(same_len, evidence="columns.sources.uri.type"))  # nested (dotted) column as evidence
assert accepted(spec(same_len, evidence="columns.uri. type"))  # stray space in the path
domain = {"type": "row_predicate", "condition": {
    "op": "neq", "left": {"function": "domain", "column": "page_url"}, "right": {"value": ""}}}
assert "domain(page_url) != ''" in render_row_predicate(spec(domain)) and accepted(spec(domain))
not_future = {"type": "row_predicate", "condition": {"op": "lte", "left": {"column": "ts"}, "right": {"function": "now"}}}
assert "ts <= now()" in _describe_assertion(spec(not_future))  # was "ts <= None"
assert not accepted(spec({"type": "row_predicate", "condition": {
    "op": "gt", "left": {"function": "sqrt", "column": "hash"}, "right": {"value": 0}}}))  # not whitelisted
assert not accepted(spec({"type": "row_predicate", "condition": {
    "op": "gt", "left": {"function": "length"}, "right": {"value": 0}}}))  # column function without column

# 2. cross-asset: composite key with different names on each side + filters
missing = {"type": "cross_asset_missing_rows", "tables": ["db.findings", "db.events"],
           "join_key": [["session_hash", "hash"]],
           "filters": {"db.findings": {"op": "eq", "left": {"column": "finding"}, "right": {"value": "code_x"}},
                       "db.events": {"op": "neq", "left": {"function": "domain", "column": "page_url"}, "right": {"value": ""}}}}
sql = render_cross_asset_missing_rows(spec(missing, asset="db.findings"))
assert "LEFT ANTI JOIN" in sql and "ON a.session_hash = b.hash" in sql
assert "WHERE finding = 'code_x'" in sql and "WHERE domain(page_url) != ''" in sql
assert accepted(spec(missing, asset="db.findings", evidence="columns.finding.type"))
bad_filter = dict(missing, filters={"db.findings": {"op": "is_not_null", "left": {"column": "page_url"}}})
assert not accepted(spec(bad_filter, asset="db.findings", evidence="columns.finding.type"))  # wrong table's column
bad_key = dict(missing, join_key=["hash"])  # 'hash' does not exist in db.findings
assert not accepted(spec(bad_key, asset="db.findings", evidence="columns.finding.type"))

# 3. Dimension Selector: an invented column is dropped, the recommendation survives
report = validate_dimension_output({"tables": [{"table": "db.events", "dimensions": [
    {"dimension": "Validity", "columns": ["ts", "invented_col"], "rationale": "r"}]}]}, CTX, ["Validity"])
assert report.accepted["tables"][0]["dimensions"][0]["columns"] == ["ts"]
assert report.rejected[0]["detail"] == ["invented_col"]

# 4. 2026-10-04 fixes: nested-column aliases, composite tuple key, explicit dimension
from dqvouch.profiling import gen_uniqueness
from dqvouch.render_sql import _infer_dimension, _describe_condition
sql = gen_uniqueness("db.t", ["a", "sources.uri"], "r")
assert "AS sources_uri_duplicate_count" in sql and "uniqExact(tuple(a, sources.uri))" in sql
assert _infer_dimension({"id": "x_unique", "dimension": "Uniqueness", "assertion": {"type": "unique"}}) == "uniqueness"
assert _infer_dimension({"id": "x_completeness_account_id", "assertion": {"type": "row_predicate"}}) == "completeness"
assert _describe_condition({"op": "lte", "left": {"column": "ts"}, "right": {"function": "now"}}) == "ts <= now()"

# 5. validator format repairs (recorded in validator_repairs) + unrenderable specs rejected
def validate_one(s):
    r = validate_test_specifications([s], CTX, ASSERTION_RENDERERS.keys())
    return (r.accepted or [None])[0], r.rejected

bare = spec({"op": "is_not_null", "left": {"column": "page_url"}})              # no row_predicate wrapper
ok, _ = validate_one(bare)
assert ok and ok["assertion"]["type"] == "row_predicate" and ok["validator_repairs"]
no_thr = spec({"type": "unique", "columns": ["hash"]}); del no_thr["threshold"]   # unique without threshold:
ok, rej = validate_one(no_thr)                                                      # still rejected, not repaired
assert ok is None and "no executable pass/fail" in rej[0]["reason"]
or_key = spec({"type": "row_predicate", "condition": {"or": [                    # {"or": [...]} instead of op/clauses
    {"op": "is_null", "left": {"column": "page_url"}}, {"op": "neq", "left": {"column": "page_url"}, "right": {"value": ""}}]}})
ok, _ = validate_one(or_key)
assert ok and "OR" in render_row_predicate(ok)
ok, rej = validate_one(spec({"type": "row_predicate", "condition": {"op": "weird", "left": {"column": "page_url"}}}))
assert ok is None and rej[0]["reason"] == "specification cannot be rendered to SQL"

# 6. cross-table profiling: chosen join key, only shared columns compared, no fan-out join
from dqvouch.profiling import gen_cross_table_check
chk = {"tables_involved": ["db.a", "db.b"], "columns_involved": ["account", "msg", "only_in_a"],
       "join_key": "msg", "shared_columns": ["account", "msg"], "join_key_identifies_rows": True}
sql = gen_cross_table_check(chk)
assert "LEFT ANTI JOIN db.b AS b ON a.msg = b.msg" in sql and "(SELECT msg, account FROM db.b LIMIT 1 BY msg)" in sql
assert "mismatched_values" not in gen_cross_table_check(dict(chk, join_key_identifies_rows=False))

# 7. cross_asset_row_count without a threshold is accepted (observation-only), as the prompt asks
rc = spec({"type": "cross_asset_row_count", "tables": ["db.events", "db.findings"]}); del rc["threshold"]
ok, rej = validate_one(rc)
assert ok and not rej

# 8. dq_signals / downstream_usage derived from schema + SQL, citable as evidence
from dqvouch.signals import add_downstream_usage_and_signals, without_downstream_sql
col = lambda n, t="String": {"name": n, "type": t, "semantic_roles": []}
A = {
    "db.src": {"columns": [col("id"), col("v"), col("url"), col("flag", "Bool"), col("d", "Date"), col("x.a", "Array(String)"), col("x.b", "Array(Int8)")],
               "lineage": {"upstream_assets": "unknown"}, "transformation": {"raw_sql": "unknown"}},
    "db.copy": {"columns": [col("id"), col("v")], "lineage": {"upstream_assets": ["db.src"]},
                "transformation": {"source_file": "c.sql", "raw_sql":
                    "INSERT INTO db.copy SELECT id, if((v IS NULL OR v = ''), 'N/A', v) AS v FROM db.src WHERE d = {load_date:Date};"}},
    "db.use": {"columns": [col("id")], "lineage": {"upstream_assets": ["db.src"]},
               "transformation": {"source_file": "u.sql", "raw_sql":
                    "INSERT INTO db.use SELECT id FROM db.src s PREWHERE d = 'EXECUTION_DATE' AND flag = false "
                    "WHERE domain(s.url) != '' GROUP BY id;"}},
}
add_downstream_usage_and_signals(A, with_sql=True)
src, cp, use = (A[t]["dq_signals"] for t in ("db.src", "db.copy", "db.use"))
assert src["parallel_arrays_x"]["columns"] == ["x.a", "x.b"]
assert cp["row_preserving_copy_of_db_src"]["tables"] == ["db.src", "db.copy"]       # copy: only the load-date filter
assert cp["fallback_value_v"]["fallback"] == "N/A"
assert "row_preserving_copy_of_db_src" not in use                                  # GROUP BY changes rows
assert src["downstream_filter_db_use"]["condition"] == "flag = false AND domain(s.url) != ''"
assert use["source_filter_db_src"]["upstream"] == "db.src" and "downstream_function_domain_url" in src
assert sorted(A["db.src"]["downstream_usage"]) == ["db_copy", "db_use"]
assert without_downstream_sql(A)["db.src"]["downstream_usage"] == ["db_copy", "db_use"]  # no SQL in the DS prompt
cite = {"id": "t", "asset": "db.copy", "rationale": "r", "status": "proposed",
        "assertion": {"type": "row_predicate", "condition": {"op": "neq", "left": {"column": "v"}, "right": {"value": ""}}},
        "threshold": {"metric": "failed_rows", "operator": "equals", "value": 0},
        "evidence": [{"path": "dq_signals.fallback_value_v"}]}
assert validate_test_specifications([cite], A, ASSERTION_RENDERERS.keys()).accepted
cite2 = dict(cite, asset="db.src", assertion={"type": "row_predicate", "condition": {"op": "is_not_null", "left": {"column": "url"}}},
             evidence=[{"path": "downstream_usage.db_use.raw_sql"}])
assert validate_test_specifications([cite2], A, ASSERTION_RENDERERS.keys()).accepted

# 9. fallback literal stated as a fact (no separate owner rule); downstream compared values;
#    checklist items list the signals of the SAME accepted table + dimension only
key = {"name": "owner_id", "type": "String", "semantic_roles": ["candidate_business_key"]}
B = {
    "db.src": {"columns": [col("owner_id"), col("kind"), col("sub_kind"), col("day", "Date"), col("x.a", "Array(String)"), col("x.b", "Array(Int8)")],
               "lineage": {"upstream_assets": "unknown"}, "transformation": {"raw_sql": "unknown"}},
    "db.out": {"columns": [key, col("kind")], "lineage": {"upstream_assets": ["db.src"]},
               "transformation": {"source_file": "o.sql", "raw_sql":
                   "INSERT INTO db.out SELECT if((owner_id IS NULL OR owner_id = ''), 'N/A', owner_id) AS owner_id, kind "
                   "FROM db.src s WHERE s.day = {d:Date} AND (s.kind = 'add' OR lower(kind) IN ('edit', 'replace') "
                   "OR s.sub_kind = 'zzz') GROUP BY owner_id, kind;"}},
}
add_downstream_usage_and_signals(B, with_sql=True)
fb = B["db.out"]["dq_signals"]["fallback_value_owner_id"]
assert fb["fallback"] == "N/A" and "'N/A'" in fb["fact"]                    # a fact; which rule follows is the LLM's call
assert "identifier_fallback_owner_id" not in B["db.out"]["dq_signals"]
assert B["db.src"]["dq_signals"]["downstream_values_kind"]["values"] == ["add", "edit", "replace"]  # not 'zzz' (sub_kind)
assert "downstream_values_day" not in B["db.src"]["dq_signals"]                                     # date = load window
from dqvouch.specifier import _build_recommendation_checklist
accepted = {"tables": [{"table": "db.src", "dimensions": [{"dimension": "Consistency", "columns": ["x.a"], "rationale": "r"}]}]}
text, n = _build_recommendation_checklist(accepted, B)
assert n == 1 and "parallel_arrays_x" in text and "downstream_values_kind" not in text  # Validity was not accepted

# 10. column-level lineage: plain renames (a AS b) become join_key pairs; downstream rules two layers
#     away reach the first table through the rename ("via")
L = {
    "db.land": {"columns": [col("ev_ts", "DateTime"), col("op"), col("j")], "lineage": {"upstream_assets": "unknown"},
                "transformation": {"raw_sql": "unknown"}},
    "db.stg": {"columns": [col("event_ts", "DateTime"), col("operation"), col("j"), col("first_ts", "DateTime"), col("last_ts", "DateTime")],
               "lineage": {"upstream_assets": ["db.land"]}, "transformation": {"source_file": "s.sql", "raw_sql":
                   "INSERT INTO db.stg SELECT e.ev_ts AS event_ts, e.op AS operation, j, "
                   "e.ev_ts AS first_ts, e.ev_ts AS last_ts FROM db.land e;"}},
    "db.rk": {"columns": [col("j")], "lineage": {"upstream_assets": ["db.stg"]}, "transformation": {"source_file": "r.sql",
              "raw_sql": "INSERT INTO db.rk SELECT j FROM db.stg WHERE operation IN ('add', 'edit');"}},
}
add_downstream_usage_and_signals(L, with_sql=True)
assert L["db.stg"]["dq_signals"]["column_mapping_from_db_land"]["pairs"] == [["op", "operation"]]  # ev_ts -> 3 cols: dropped
inherited = L["db.land"]["dq_signals"]["downstream_values_operation_via_db_stg"]
assert inherited["columns"] == ["op"] and inherited["values"] == ["add", "edit"] and inherited["via"] == "db.stg"
assert inherited["fact"] == "(via db.stg, which carries these columns unchanged) db.rk compares op with the literals ['add', 'edit']"

# 11. validator vs the observed profile of the same columns (cited or not), reversed join pairs, duplicates
P = {
    "completeness:db.events:uri,page_url": {"dimension": "completeness", "column": "uri,page_url", "check_type": "",
        "rows": [{"total_rows": 100, "uri_nulls": 0, "page_url_nulls": 100}]},
    "validity:db.events:page_url": {"dimension": "validity", "column": "page_url", "check_type": "",
        "rows": [{"value": "a", "occurrences": 90, "pct_of_total": 90.0}, {"value": "z", "occurrences": 10, "pct_of_total": 10.0}]},
    "uniqueness:db.events:hash": {"dimension": "uniqueness", "column": "hash", "check_type": "",
        "rows": [{"total_rows": 100, "hash_duplicate_count": 60}]},
    "join_key_stats:db.findings:brand": {"dimension": "join_key_stats", "column": "brand", "check_type": "join_key_stats",
        "rows": [{"total_rows": 100, "nulls": 40}]},
}
def vp(s): r = validate_test_specifications([s], CTX, ASSERTION_RENDERERS.keys(), profiling_summary=P); return (r.accepted or [None])[0], r.rejected
both = spec({"type": "row_predicate", "condition": {"op": "and", "clauses": [
    {"op": "is_not_null", "left": {"column": "uri"}}, {"op": "is_not_null", "left": {"column": "page_url"}}]}})
# data that contradicts a rule never deletes the test: verdict suspended, reviewer warned
flagged = lambda ok, text: ok and "threshold" not in ok and ok["suspended_threshold"] and text in " ".join(ok["review_warnings"])
ok, _ = vp(both)                                     # page_url is NULL in every row -> nothing cut, reviewer decides
assert ok["assertion"]["condition"] == both["assertion"]["condition"] and flagged(ok, "every profiled row")
ok, _ = vp(spec({"type": "row_predicate", "condition": {"op": "in", "left": {"column": "page_url"}, "values": ["a"]}}))
assert flagged(ok, "10.0%")                          # 'z' is 10% of rows: a filter taken for a domain, or bad data?
ok, _ = vp(spec({"type": "row_predicate", "condition": {"op": "in", "left": {"column": "page_url"}, "values": ["a", "z"]}}))
assert ok and ok["threshold"] and "review_warnings" not in ok  # the full observed domain is kept as is
ok, _ = vp(spec({"type": "unique", "columns": ["hash"]}))
assert flagged(ok, "60/100")                         # contradicted even though the spec cites no profile
ok, _ = vp(spec({"type": "cross_asset_missing_rows", "tables": ["db.events", "db.findings"], "join_key": [["brand", "hash"]]}, evidence="columns.uri.type"))
assert flagged(ok, "db.findings.brand")             # pair swapped to table order, then the NULL key flagged
ok["threshold"] = ok.pop("suspended_threshold")      # reviewer: the rule is right, the data is wrong
ok2, _ = vp(ok)
assert ok2 and ok2["threshold"] == ok["threshold"]   # flagged once: the reviewer's choice is kept
ok, _ = vp(spec({"type": "unique", "columns": ["hash"]}))
assert vp(ok)[0]                                     # a flagged unique stays valid without its threshold
r = validate_test_specifications([dict(same_len_spec := spec(same_len), id="a"), dict(spec(same_len), id="b")], CTX, ASSERTION_RENDERERS.keys())
assert len(r.accepted) == 1 and "duplicate of specification a" in r.rejected[0]["reason"]

# 12. UNION ALL branches: which target rows come from which source (a literal stamped by a branch);
#     rows silently dropped by a downstream IS NOT NULL; union of sources; uniqueness per load partition
U = {
    "db.ev": {"columns": [col("id"), col("blocked", "Bool"), col("brand")], "lineage": {"upstream_assets": "unknown"},
              "transformation": {"raw_sql": "unknown"}},
    "db.sc": {"columns": [col("id"), col("brand"), col("page"), col("url")], "lineage": {"upstream_assets": "unknown"},
              "transformation": {"raw_sql": "unknown"}},
    "db.rk": {"columns": [col("kind"), col("id"), col("p", "Date")], "lineage": {"upstream_assets": ["db.ev", "db.sc"]},
              "asset": {"partition_key": "toYYYYMMDD(p)"}, "transformation": {"source_file": "rf.sql", "raw_sql":
                  "INSERT INTO db.rk SELECT pair.1 AS kind, id, p FROM (SELECT * FROM db.ev WHERE p = 'EXECUTION_DATE' "
                  "AND blocked = false AND brand IS NOT NULL) UNION ALL SELECT 'ext' AS kind, id, p FROM db.sc "
                  "PREWHERE p = 'EXECUTION_DATE' WHERE brand IS NOT NULL AND domain(page) != domain(url);"}},
}
add_downstream_usage_and_signals(U, with_sql=True)
rf = U["db.rk"]["dq_signals"]
assert rf["union_branch_from_db_ev"]["condition"] == "kind NOT IN ('ext')"
assert rf["union_branch_from_db_sc"]["condition"] == "kind = 'ext'"
assert rf["source_filter_db_ev"]["consumer_condition"] == "kind NOT IN ('ext')"
assert rf["union_of_sources"]["tables"] == ["db.ev", "db.sc"]
assert rf["union_of_sources"]["unfiltered_sources"] == []                  # both branches filter their source
assert rf["union_of_sources"]["fact"].endswith("read in full: none; read through a filter: db.ev, db.sc")
assert rf["source_filter_db_ev"]["fact"].startswith("db.rk reads db.ev only where")  # no table order prescribed
assert rf["loaded_per_partition_p"]["dimension"] == "Uniqueness"
req = U["db.sc"]["dq_signals"]["downstream_required_brand_db_rk"]
assert req["dimension"] == "Completeness" and req["condition"] == "domain(page) != domain(url)"
assert req["fact"] == "db.rk reads the rows of db.sc where domain(page) != domain(url), and of those only the ones where brand IS NOT NULL"

# 13. values with their guard;
#     columns the consumer joins/groups/partitions by; no date-only join keys; uniques measured exactly
G = {
    "db.ev": {"columns": [col("account"), col("op"), col("cross", "Bool"), col("ts", "DateTime")],
              "lineage": {"upstream_assets": "unknown"}, "transformation": {"raw_sql": "unknown"}},
    "db.oc": {"columns": [col("account"), col("op"), col("p", "Date")], "lineage": {"upstream_assets": ["db.ev"]},
              "asset": {"partition_key": "toYYYYMMDD(p)"}, "transformation": {"source_file": "o.sql", "raw_sql":
                  "INSERT INTO db.oc SELECT account, if(op = 'add' AND cross = true, 'x', if(op IN ('edit') AND cross = true, 'y', '')) AS op, "
                  "date(ts) AS p FROM db.ev e GROUP BY account, op, ts;"}},
}
add_downstream_usage_and_signals(G, with_sql=True)
ev = G["db.ev"]["dq_signals"]
assert ev["downstream_values_when_op_1"]["condition"] == "cross = true" and ev["downstream_values_when_op_1"]["values"] == ["add", "edit"]
assert {"downstream_key_account_db_oc", "downstream_key_ts_db_oc"} <= set(ev)  # grouped by account; partition from date(ts)
TS = {"db.a": {"columns": [{"name": "id", "type": "String"}, {"name": "ts", "type": "DateTime"}]},
      "db.b": {"columns": [{"name": "id", "type": "String"}, {"name": "event_ts", "type": "DateTime"}]}}
r = validate_test_specifications([spec({"type": "cross_asset_missing_rows", "tables": ["db.a", "db.b"], "join_key": [["ts", "event_ts"]]},
                                       asset="db.a", evidence="columns.ts.type")], TS, ASSERTION_RENDERERS.keys())
assert not r.accepted and "date/time" in r.rejected[0]["reason"]
P2 = {"uniqueness:db.events:hash,ts:measured": {"dimension": "uniqueness", "column": "hash,ts", "check_type": "composite_key",
                                                "rows": [{"total_rows": 100, "composite_duplicate_count": 90}]}}
r = validate_test_specifications([spec({"type": "unique", "columns": ["hash", "ts"]})], CTX, ASSERTION_RENDERERS.keys(), profiling_summary=P2)
assert flagged(r.accepted[0], "90/100")

# 14. one signal per fact (no weak lower()/plain duplicates; one per Nested column), facts listed in
#     extraction order, source->target rejected on a filtered source, `in` lists judged on measured values
from dqvouch.signals import _condense
from dqvouch.specifier import _signals_for
raw = {"parallel_arrays_a": {"dimension": "Consistency", "columns": ["a.x", "a.y"]},
       "parallel_arrays_b": {"dimension": "Consistency", "columns": ["b.x", "b.y"]},
       "downstream_values_op": {"dimension": "Validity", "columns": ["op"], "values": ["add"]},
       "downstream_values_when_op_1": {"dimension": "Validity", "columns": ["op", "cross"], "condition": "cross = true", "values": ["add"]},
       "downstream_values_when_op_1_via_db_s": {"dimension": "Validity", "columns": ["op", "cross"], "condition": "cross = true",
                                                "values": ["edit"], "via": "db.s"},
       "downstream_function_lower_op": {"dimension": "Validity", "columns": ["op"], "function": "lower"},
       "downstream_function_domain_url": {"dimension": "Validity", "columns": ["url"], "function": "domain"}}
cs = _condense(raw)
assert set(cs) == {"parallel_arrays_a", "parallel_arrays_b", "downstream_values_when_op", "downstream_function_domain_url"}
assert cs["downstream_values_when_op"]["cases"] == [{"condition": "cross = true", "values": ["add", "edit"]}]
assert _signals_for({"db.t": {"dq_signals": cs}}, ["db.t"], "Validity") == ["downstream_values_when_op", "downstream_function_domain_url"]
SF = {"db.src": {"columns": [{"name": "id", "type": "String"}, {"name": "flag", "type": "Bool"}]},
      "db.tgt": {"columns": [{"name": "id", "type": "String"}],
                 "dq_signals": {"source_filter_db_src": {"upstream": "db.src", "condition": "flag = false"}}}}
fwd = spec({"type": "cross_asset_missing_rows", "tables": ["db.src", "db.tgt"], "join_key": [["id", "id"]]}, asset="db.tgt", evidence="columns.id.type")
r = validate_test_specifications([fwd], SF, ASSERTION_RENDERERS.keys())
assert not r.accepted and 'tables ["db.tgt", "db.src"]' in r.rejected[0]["reason"]
fwd_filtered = spec({**fwd["assertion"], "filters": {"db.src": {"op": "eq", "left": {"column": "flag"}, "right": {"value": False}}}},
                    asset="db.tgt", evidence="columns.id.type")
assert validate_test_specifications([fwd_filtered], SF, ASSERTION_RENDERERS.keys()).accepted  # filtered like the SQL: allowed
UN = {"db.tgt": {"columns": [{"name": "id", "type": "String"}], "dq_signals": {"union_of_sources": {"tables": ["db.src", "db.oth"]}}},
      "db.src": {"columns": [{"name": "id", "type": "String"}]}}
back = spec({"type": "cross_asset_missing_rows", "tables": ["db.tgt", "db.src"], "join_key": [["id", "id"]]}, asset="db.tgt", evidence="columns.id.type")
r = validate_test_specifications([back], UN, ASSERTION_RENDERERS.keys())
assert not r.accepted and "UNION ALL" in r.rejected[0]["reason"]  # db.oth's rows would all be "missing"
P3 = {"validity:db.events:page_url:measured": {"dimension": "validity", "column": "page_url", "check_type": "",
                                               "rows": [{"value": "a", "occurrences": 20, "pct_of_total": 20.0},
                                                        {"value": "b", "occurrences": 80, "pct_of_total": 80.0}]}}
r = validate_test_specifications([spec({"type": "row_predicate", "condition": {"op": "in", "left": {"column": "page_url"}, "values": ["a"]}})],
                                 CTX, ASSERTION_RENDERERS.keys(), profiling_summary=P3)
assert flagged(r.accepted[0], "80.0%")

# 15. implies(if, col in [...]) judged on the rows where `if` holds; column comparison needs a key that
#     identifies the reference rows
import json as _json
cond_if = {"op": "eq", "left": {"column": "uri"}, "right": {"value": "x"}}
P4 = {"validity:db.events:page_url:measured:when": {"dimension": "validity", "column": "page_url", "check_type": "", "when": cond_if,
      "rows": [{"value": "a", "occurrences": 20, "pct_of_total": 20.0}, {"value": "b", "occurrences": 80, "pct_of_total": 80.0}]}}
two = spec({"type": "row_predicate", "condition": {"op": "and", "clauses": [
    {"op": "implies", "if": cond_if, "then": {"op": "in", "left": {"column": "page_url"}, "values": ["a"]}},
    {"op": "is_not_null", "left": {"column": "hash"}}]}})
r = validate_test_specifications([two], CTX, ASSERTION_RENDERERS.keys(), profiling_summary=P4)
assert flagged(r.accepted[0], "where the `if` holds") and r.accepted[0]["assertion"] == two["assertion"]  # nothing cut
P5 = {"uniqueness:db.findings:session_hash:measured": {"dimension": "uniqueness", "column": "session_hash", "check_type": "composite_key",
      "rows": [{"total_rows": 100, "composite_duplicate_count": 70}]}}
eq = spec({"type": "cross_asset_column_equality", "tables": ["db.events", "db.findings"], "join_key": [["hash", "session_hash"]],
           "columns": ["page_url", "finding"]})
r = validate_test_specifications([eq], CTX, ASSERTION_RENDERERS.keys(), profiling_summary=P5)
assert not r.accepted and "repeats in db.findings" in r.rejected[0]["reason"]

# 16. domain() with the guard the SQL applies it under; coverage pass picks the uncovered rule signals
H = {
    "db.sc": {"columns": [col("brand"), col("page"), col("url")], "lineage": {"upstream_assets": "unknown"},
              "transformation": {"raw_sql": "unknown"}},
    "db.rk": {"columns": [col("v")], "lineage": {"upstream_assets": ["db.sc"]}, "transformation": {"source_file": "r.sql",
              "raw_sql": "INSERT INTO db.rk SELECT brand AS v FROM db.sc LEFT ARRAY JOIN [('p', domain(page))] AS m "
                         "WHERE brand IS NOT NULL AND domain(page) != domain(url);"}},
}
add_downstream_usage_and_signals(H, with_sql=True)
fw = {k: s for k, s in H["db.sc"]["dq_signals"].items() if k.startswith("downstream_function")}
assert {s["condition"] for s in fw.values()} == {"brand IS NOT NULL"} and len(fw) == 2  # page and url, guarded; plain dropped

# 17. a mostly-NULL column is flagged (optional by design, or never filled?); a key whose non-date part does not
#     identify rows (e.g. a single-value account) counts as a date-only key
P6 = {"completeness:db.events:page_url:measured": {"dimension": "completeness", "column": "page_url", "check_type": "",
      "rows": [{"total_rows": 100, "nulls": 80}]}}
both17 = spec({"type": "row_predicate", "condition": {"op": "and", "clauses": [
    {"op": "is_not_null", "left": {"column": "page_url"}}, {"op": "is_not_null", "left": {"column": "hash"}}]}})
r = validate_test_specifications([both17], CTX, ASSERTION_RENDERERS.keys(), profiling_summary=P6)
assert flagged(r.accepted[0], "80/100")
TS2 = {"db.a": {"columns": [{"name": "account", "type": "String"}, {"name": "ts", "type": "DateTime"}]},
       "db.b": {"columns": [{"name": "account", "type": "String"}, {"name": "event_ts", "type": "DateTime"}]}}
P7 = {"uniqueness:db.a:account:measured": {"dimension": "uniqueness", "column": "account", "check_type": "composite_key",
      "rows": [{"total_rows": 1000, "composite_duplicate_count": 999}]}}
mixed = spec({"type": "cross_asset_missing_rows", "tables": ["db.a", "db.b"], "join_key": [["ts", "event_ts"], ["account", "account"]]},
             asset="db.a", evidence="columns.ts.type")
r = validate_test_specifications([mixed], TS2, ASSERTION_RENDERERS.keys(), profiling_summary=P7)
assert not r.accepted and "date/time" in r.rejected[0]["reason"]

# 18. a signal states a fact, never the test: no assertion type, threshold, table order or condition tree;
#     DQ_SIGNALS=0 keeps the raw context (downstream SQL) and adds no facts
import re as _re
for built in (A, B, L, U, G, H):
    for t, ctx in built.items():
        for k, sig in (ctx.get("dq_signals") or {}).items():
            assert "suggested_check" not in sig and sig["fact"], (t, k)
            assert not _re.search(r"row_predicate|cross_asset|threshold|equals 0|implies\(|tables \[|unique\[", sig["fact"]), (t, k, sig["fact"])
off = {t: {**ctx, "lineage": dict(ctx["lineage"])} for t, ctx in A.items()}
for ctx in off.values():
    ctx.pop("dq_signals", None)
    ctx.pop("downstream_usage", None)
    ctx["lineage"].pop("downstream_assets", None)
add_downstream_usage_and_signals(off, with_sql=True, with_signals=False)
assert not any("dq_signals" in ctx for ctx in off.values()) and off["db.src"]["downstream_usage"]

# 19. Invariant Extractor: guarantees validated against the SQL they quote; checklist items per guarantee
#     (a two-table guarantee creates the pair the DS did not propose); SQL facts hidden from prompts but
#     still used by the validator; `sql_invariants.<id>` resolves as evidence
from dqvouch.validator import validate_invariants
from dqvouch.rules import with_invariant_items, build_invariant_extractor_prompt
from dqvouch.context import _for_prompt
V = {
    "db.src": {"columns": [col("id"), col("flag", "Bool"), col("kind")], "lineage": {"upstream_assets": "unknown"},
               "transformation": {"raw_sql": "unknown"}},
    "db.out": {"columns": [col("id"), col("kind")], "lineage": {"upstream_assets": ["db.src"]},
               "transformation": {"source_file": "o.sql", "raw_sql":
                   "INSERT INTO db.out SELECT id, if(kind = '', 'none', kind) AS kind FROM db.src WHERE flag = false;"}},
}
raw = [
    {"category": "row_provenance", "tables": ["db.src", "db.out"], "columns": ["flag"], "condition": "flag = false",
     "sql_quote": "WHERE  flag = FALSE"},                                                  # target second, spacing/case
    {"category": "presence", "tables": ["db.out"], "columns": ["kind"], "sql_quote": "if(kind = '', 'none', kind) AS kind"},
    {"category": "presence", "tables": ["db.out"], "columns": ["kind"], "sql_quote": "coalesce(kind, 'x')"},  # not in SQL
    {"category": "grain", "tables": ["db.out"], "columns": ["nope"], "sql_quote": "WHERE flag = false"},      # bad column
    {"category": "row_provenance", "tables": ["db.out", "db.other"], "columns": [], "sql_quote": "WHERE flag = false"},
    {"category": "vibes", "tables": ["db.out"], "columns": [], "sql_quote": "WHERE flag = false"},
    {"category": "presence", "tables": ["db.out"], "columns": ["kind"], "sql_quote": "kind"},               # too short
]
acc, rej = validate_invariants(raw, "db.out", V)
assert [a["id"] for a in acc] == ["db_out_1", "db_out_2"] and acc[0]["tables"] == ["db.out", "db.src"]
assert [r["reason"][:20] for r in rej] == ["quoted SQL excerpt n", "unknown column(s) ['", "table not read by th",
                                         "unknown category", "quoted SQL excerpt m"]
V["db.out"]["sql_invariants"] = {a["id"]: a for a in acc}
dims = with_invariant_items({"tables": [], "cross_table_checks": []}, V)
assert dims["tables"] == [{"table": "db.out", "dimensions": [{"dimension": "Completeness", "columns": ["kind"],
                                                             "rationale": dims["tables"][0]["dimensions"][0]["rationale"]}]}]
assert dims["cross_table_checks"][0]["tables_involved"] == ["db.out", "db.src"]          # pair created without the DS
assert dims["cross_table_checks"][0]["rationale"].startswith("SQL guarantee sql_invariants.db_out_1 (row_provenance)")
from dqvouch.specifier import _group_cross_table_checks_by_pair
assert list(_group_cross_table_checks_by_pair(dims["cross_table_checks"])) == [("db.out", "db.src")]
prov = spec({"type": "cross_asset_missing_rows", "tables": ["db.out", "db.src"], "join_key": [["id", "id"]],
             "filters": {"db.src": {"op": "eq", "left": {"column": "flag"}, "right": {"value": False}}}},
            asset="db.out", evidence="sql_invariants.db_out_1")
assert validate_test_specifications([prov], V, ASSERTION_RENDERERS.keys()).accepted   # evidence path resolves
assert "kind" in build_invariant_extractor_prompt("db.out", V) and "db.src" in build_invariant_extractor_prompt("db.out", V)
X = {t: {**c, "lineage": dict(c["lineage"])} for t, c in A.items()}
for c in X.values():
    for k in ("dq_signals", "downstream_usage", "_sql_facts"):
        c.pop(k, None)
    c["lineage"].pop("downstream_assets", None)
add_downstream_usage_and_signals(X, with_sql=True, sql_signals_in_prompt=False)
assert "parallel_arrays_x" in X["db.src"]["dq_signals"]                                   # schema facts stay visible
assert "source_filter_db_src" in X["db.use"]["_sql_facts"] and "dq_signals" not in X["db.use"]
assert "_sql_facts" not in _for_prompt(X)["db.use"]                                       # never shown to the model
fwd_hidden = spec({"type": "cross_asset_missing_rows", "tables": ["db.src", "db.use"], "join_key": [["id", "id"]]},
                  asset="db.use", evidence="columns.id.type")
r = validate_test_specifications([fwd_hidden], X, ASSERTION_RENDERERS.keys())
assert not r.accepted and "reads db.src only where" in r.rejected[0]["reason"]           # validator still uses them
listed = spec({**fwd_hidden["assertion"], "filters": [{"db.src": {"op": "is_not_null", "left": {"column": "id"}}}]},
              asset="db.use", evidence="columns.id.type")
r = validate_test_specifications([listed], X, ASSERTION_RENDERERS.keys())           # was an AttributeError crash
assert not r.accepted and r.rejected[0]["reason"] == "invalid cross-asset filters"

# 20. column pairs on two-table guarantees (a wrong pair is dropped, the guarantee kept) reach the checklist;
#     repair pass: fixable rejections grouped by the scope that produced them, duplicates and repairs left out
pairs_raw = [{"category": "row_provenance", "tables": ["db.out", "db.src"], "columns": ["flag"], "sql_quote": "WHERE flag = false",
              "column_pairs": [["id", "id"], ["kind", "nope"], "bad"]}]
acc, _ = validate_invariants(pairs_raw, "db.out", V)
assert acc[0]["column_pairs"] == [["id", "id"]]
V["db.out"]["sql_invariants"] = {a["id"]: a for a in acc}
item = with_invariant_items({"tables": [], "cross_table_checks": []}, V)["cross_table_checks"][0]
assert "same row on both sides: id = id ([db.out column, db.src column])" in item["rationale"]
from dqvouch.specifier import _repairable, _scope_inputs
specs20 = {"db.out+db.src__a": {"id": "db.out+db.src__a"}, "db.out__b": {"id": "db.out__b"},
           "db.out__c": {"id": "db.out__c"}, "db.out__repair__d": {"id": "db.out__repair__d"}}
rej20 = [{"scope": "db.out+db.src__a", "reason": "join_key not present as a column in both involved tables"},
         {"scope": "db.out__b", "reason": "invalid cross-asset filters"},
         {"scope": "db.out__c", "reason": "duplicate of specification db.out__b"},
         {"scope": "db.out__repair__d", "reason": "invalid cross-asset filters"}]
grouped = _repairable(rej20, specs20)
assert {k: [s["id"] for s, _ in v] for k, v in grouped.items()} == {"db.out+db.src": ["db.out+db.src__a"], "db.out": ["db.out__b"]}
ctx20, dims20, _ = _scope_inputs("db.out+db.src", V, with_invariant_items({"tables": [], "cross_table_checks": []}, V), None)
assert set(ctx20) == {"db.out", "db.src"} and dims20["cross_table_checks"] and dims20["tables"] == []
ctx20, dims20, _ = _scope_inputs("db.out", V, {"tables": []}, None)
assert set(ctx20) == {"db.out"} and dims20 == {"tables": [{"table": "db.out", "dimensions": []}]}

# 21. key pairs never date/time (a Nullable type stays: nulls are measured on the data); derivations stay context (no checklist item); a date column
#     compared with a non-date string (a template's run parameter) is rejected, a real date is not
W = {
    "db.src": {"columns": [col("id"), col("opt", "Nullable(String)"), col("ts", "DateTime"), col("d", "Date")],
               "lineage": {"upstream_assets": "unknown"}, "transformation": {"raw_sql": "unknown"}},
    "db.out": {"columns": [col("id"), col("opt", "Nullable(String)"), col("ts", "DateTime"), col("d", "Date")],
               "lineage": {"upstream_assets": ["db.src"]},
               "transformation": {"source_file": "w.sql", "raw_sql": "INSERT INTO db.out SELECT id, opt, ts, d FROM db.src WHERE d = 'EXECUTION_DATE' AND id != '';"}},
}
acc, _ = validate_invariants([
    {"category": "row_provenance", "tables": ["db.out", "db.src"], "columns": ["id"], "sql_quote": "WHERE d = 'EXECUTION_DATE'",
     "column_pairs": [["id", "id"], ["opt", "opt"], ["ts", "ts"]]},
    {"category": "derivation", "tables": ["db.out", "db.src"], "columns": ["ts"], "sql_quote": "SELECT id, opt, ts, d FROM db.src"},
], "db.out", W)
assert acc[0]["column_pairs"] == [["id", "id"], ["opt", "opt"]] and len(acc) == 2   # date/time dropped; a Nullable type is not NULL data
W["db.out"]["sql_invariants"] = {a["id"]: a for a in acc}
items = with_invariant_items({"tables": [], "cross_table_checks": []}, W)
assert len(items["cross_table_checks"]) == 1 and items["tables"] == []                  # the derivation is not an item
param = spec({"type": "row_predicate", "condition": {"op": "eq", "left": {"column": "d"}, "right": {"value": "EXECUTION_DATE"}}},
             asset="db.out", evidence="columns.d.type")
r = validate_test_specifications([param], W, ASSERTION_RENDERERS.keys())
assert not r.accepted and "EXECUTION_DATE" in r.rejected[0]["detail"][0]
dated = spec({"type": "row_predicate", "condition": {"op": "gte", "left": {"column": "d"}, "right": {"value": "2026-01-01"}}},
             asset="db.out", evidence="columns.d.type")
assert validate_test_specifications([dated], W, ASSERTION_RENDERERS.keys()).accepted
filt = spec({"type": "cross_asset_missing_rows", "tables": ["db.out", "db.src"], "join_key": [["id", "id"]],
             "filters": {"db.src": {"op": "eq", "left": {"column": "d"}, "right": {"value": "EXECUTION_DATE"}}}},
            asset="db.out", evidence="columns.id.type")
r = validate_test_specifications([filt], W, ASSERTION_RENDERERS.keys())
assert not r.accepted and r.rejected[0]["reason"] == "invalid cross-asset filters"
prompt21 = build_invariant_extractor_prompt("db.out", W)
assert "SMALLEST set" in prompt21 and "{load_date:Date}" in prompt21

# 22. a UNION ALL output: each provenance item carries the condition of its branch (its own marker, or not the
#     other branches'); input requirements are validated against the tables the SQL reads and stored on the input
from dqvouch.rules import _branch_note, _store_invariants
U2 = {
    "db.a": {"columns": [col("id"), col("kind")], "lineage": {"upstream_assets": "unknown"}, "transformation": {"raw_sql": "unknown"}},
    "db.b": {"columns": [col("id"), col("url")], "lineage": {"upstream_assets": "unknown"}, "transformation": {"raw_sql": "unknown"}},
    "db.u": {"columns": [col("id"), col("mark")], "lineage": {"upstream_assets": ["db.a", "db.b"]},
             "transformation": {"source_file": "u.sql", "raw_sql":
                 "INSERT INTO db.u SELECT id, 'x' AS mark FROM db.a WHERE kind IN ('k1', 'k2') "
                 "UNION ALL SELECT id, 'ext' AS mark FROM db.b WHERE domain(url) != '';"}},
}
acc, rej = validate_invariants([
    {"category": "branch_source", "tables": ["db.u", "db.b"], "columns": ["mark"], "condition": "mark = 'ext'", "sql_quote": "'ext' AS mark FROM db.b"},
    {"category": "row_provenance", "tables": ["db.u", "db.a"], "columns": ["kind"], "condition": "kind IN ('k1', 'k2')",
     "sql_quote": "FROM db.a WHERE kind IN ('k1', 'k2')", "column_pairs": [["id", "id"]]},
    {"category": "row_provenance", "tables": ["db.u", "db.b"], "columns": [], "sql_quote": "FROM db.b WHERE domain(url) != ''"},
    {"category": "input_values", "tables": ["db.a"], "columns": ["kind"], "values": ["k1", "k2"], "sql_quote": "kind IN ('k1', 'k2')"},
    {"category": "input_function", "tables": ["db.b"], "columns": ["url"], "condition": "domain(url)", "sql_quote": "domain(url) != ''"},
    {"category": "input_values", "tables": ["db.u"], "columns": ["mark"], "sql_quote": "'x' AS mark FROM db.a"},     # the output, not an input
    {"category": "input_presence", "tables": ["db.a", "db.b"], "columns": ["id"], "sql_quote": "SELECT id, 'x' AS mark"},  # two tables
], "db.u", U2)
assert [a["category"] for a in acc] == ["branch_source", "row_provenance", "row_provenance", "input_values", "input_function"]
assert [r["reason"][:20] for r in rej] == ["an input requirement"] * 2
_store_invariants(U2, acc)
assert set(U2["db.u"]["sql_invariants"]) == {a["id"] for a in acc[:3]}                    # output guarantees on the output
assert [i["category"] for i in U2["db.a"]["sql_invariants"].values()] == ["input_values"]   # requirements on the input
assert [i["category"] for i in U2["db.b"]["sql_invariants"].values()] == ["input_function"]
assert _branch_note(acc[2], U2["db.u"]).endswith("rows from db.b are those where mark = 'ext'")          # own marker
assert _branch_note(acc[1], U2["db.u"]).endswith("rows from db.a are those where NOT (mark = 'ext')")    # not the others'
items = with_invariant_items({"tables": [], "cross_table_checks": []}, U2)
assert {t["table"] for t in items["tables"]} == {"db.a", "db.b"}                           # input requirements -> inputs
assert any("NOT (mark = 'ext')" in c["rationale"] for c in items["cross_table_checks"])
assert "REQUIRES of the tables it reads" in build_invariant_extractor_prompt("db.u", U2, "input")
assert "input_values" not in build_invariant_extractor_prompt("db.u", U2) and "can be NULL" not in build_invariant_extractor_prompt("db.u", U2)

acc, rej = validate_invariants([{"category": "row_provenance", "tables": ["db.u", "db.a"], "columns": ["KIND"],
    "sql_quote": "FROM db.a WHERE kind IN ('k1', 'k2')", "column_pairs": [["ID", "Id"]]}], "db.u", U2)
assert acc[0]["columns"] == ["kind"] and acc[0]["column_pairs"] == [["id", "id"]] and not rej   # wrong case fixed


def test_regression_module_ran():
    pass  # the module-level asserts above ran at collection
