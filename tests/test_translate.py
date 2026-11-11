import re

import pytest

from dqvouch.translate import to_postgres


def norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).upper().replace('"', "")


CASES = [
    ("SELECT count() AS n FROM db.t", ["COUNT(*)"], []),
    ("SELECT uniqExact(a) FROM db.t", ["COUNT(DISTINCT A)"], ["UNIQEXACT"]),
    ("SELECT count() - uniqExact(tuple(a, b)) FROM db.t", ["COUNT(DISTINCT"], ["UNIQEXACT", "TUPLE", "STRUCT"]),
    ("SELECT trim(toString(a)) FROM db.t", ["CAST(A AS TEXT)"], ["TOSTRING"]),
    ("SELECT countIf(empty(a)) FROM db.t", ["CARDINALITY(A) = 0"], ["EMPTY(", "COUNTIF"]),
    ("SELECT countIf(toDateTime(a) > now()) FROM db.t", ["CAST(A AS TIMESTAMP)"], ["TODATETIME"]),
    ("SELECT quantile(0.5)(a) FROM db.t", ["PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY A)"], ["QUANTILE("]),
    ("SELECT round(stddevPop(a), 4), stddevPop(a) OVER () FROM db.t",
     # sqlglot writes NUMERIC as DECIMAL; PostgreSQL treats them as the same type.
     ["ROUND(CAST(STDDEV_POP(A) AS DECIMAL), 4)", "STDDEV_POP(A) OVER ()"], ["STDDEVPOP"]),
    ("SELECT countIf(NOT (d <= today())) FROM db.t", ["CURRENT_DATE"], ["TODAY("]),
    # COALESCE: ClickHouse's domain() gives '' (never NULL) when there is no host.
    ("SELECT countIf(NOT (domain(u) != '')) FROM db.t", ["COALESCE(SUBSTRING(U FROM"], ["DOMAIN("]),
    ("SELECT countIf(NOT (trimBoth(a) != '')) FROM db.t", ["TRIM(A)"], ["TRIMBOTH"]),
    ("SELECT countIf(isNull(`k`)) FROM db.t", ["K IS NULL"], ["ISNULL("]),
    ("SELECT count() FROM db.a AS a INNER JOIN (SELECT k, v FROM db.b LIMIT 1 BY k) AS b ON a.k = b.k",
     ["DISTINCT ON (K)"], ["LIMIT 1 BY"]),
    ("SELECT count() AS m FROM db.a AS a LEFT ANTI JOIN db.b AS b ON a.k = b.k", ["NOT EXISTS"], ["ANTI JOIN"]),
    ("SELECT name AS declared_column_name, type AS declared_type, type LIKE 'Nullable%' AS is_nullable "
     "FROM system.columns WHERE database = 'landing' AND table = 'orders_raw' AND name IN ('order_id')",
     ["INFORMATION_SCHEMA.COLUMNS", "TABLE_SCHEMA = 'LANDING'", "IS_NULLABLE = 'YES'"], ["SYSTEM.COLUMNS"]),
    ("SELECT round(countIf(a IS NULL) / count() * 100, 2) FROM db.t", ["DOUBLE PRECISION", "ROUND(CAST("], ["COUNTIF"]),
    # countIf over no rows is 0 (not NULL), and x / 0 is NULL (not an error), as on ClickHouse.
    ("SELECT countIf(a IS NULL) / count() FROM db.t", ["COUNT(*) FILTER(WHERE A IS NULL)", "NULLIF(COUNT(*), 0)"],
     ["COUNTIF", "SUM(CASE"]),
]


@pytest.mark.parametrize("sql,must,must_not", CASES)
def test_rule(sql, must, must_not):
    out = norm(to_postgres(sql))
    for fragment in must:
        assert fragment in out, out
    for fragment in must_not:
        assert fragment not in out, out


def test_subquery_in_from_gets_an_alias():
    out = to_postgres("SELECT (SELECT count() FROM (SELECT a FROM db.t GROUP BY a HAVING uniqExact(b) > 1)) AS x")
    assert re.search(r'FROM \(SELECT .+\) AS "?_sq\d+"?', out), out


def test_mixed_case_identifiers_are_quoted():
    out = to_postgres("SELECT countIf(isNull(`MyCol`)) FROM db.MyTable")
    assert '"MyCol"' in out and '"MyTable"' in out, out


def test_unparseable_sql_raises_value_error():
    with pytest.raises(ValueError):
        to_postgres("SELEC nonsense ((")


@pytest.mark.parametrize("sql", [
    "SELECT count() AS n FROM db.t; SET default_transaction_read_only = off; SELECT 0 AS n",
    "INSERT INTO db.t VALUES (1)",
    "SET default_transaction_read_only = off",
])
def test_only_one_select_query_is_translated(sql):
    with pytest.raises(ValueError, match="one SELECT"):
        to_postgres(sql)
