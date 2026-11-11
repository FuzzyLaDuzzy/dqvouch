"""Translate dqvouch's internal SQL (ClickHouse dialect) to PostgreSQL.

sqlglot does the heavy lifting (countIf, dateDiff, ANTI JOIN, real division);
the rules below cover the ClickHouse functions it leaves untranslated.
"""
import sqlglot
from sqlglot import exp

# Host part of a URL or e-mail, like ClickHouse's domain(): optional scheme and user@, then a dotted host name that
# ends the value or is followed by a port, path, query or fragment. Anything else ('N/A', '12345', 'a b.com') is ''.
_DOMAIN_RE = r"^(?:(?:[A-Za-z][A-Za-z0-9+.-]*:)?//)?(?:[^/?#@:\s]*@)?([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+)(?=$|[:/?#])"
_INFORMATION_SCHEMA = {"database": "table_schema", "table": "table_name", "name": "column_name", "type": "data_type"}


def check_one_select(sql: str, dialect: str = "clickhouse") -> None:
    """Refuses text that parses as anything but exactly one query (a second statement, INSERT, DROP, SET...).
    Text sqlglot cannot parse is let through: the database itself runs every dqvouch query read-only."""
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except sqlglot.errors.SqlglotError:
        return
    if len(trees) != 1 or not isinstance(trees[0], exp.Query):
        kinds = ", ".join(type(t).__name__ for t in trees) or "nothing"
        raise ValueError(f"expected one SELECT query, got {kinds}")


def to_postgres(sql: str) -> str:
    try:
        tree = sqlglot.parse_one(sql, read="clickhouse")
    except sqlglot.errors.SqlglotError as e:
        raise ValueError(str(e)) from e
    # dqvouch only ever sends one SELECT. Refusing anything else (a second statement, INSERT, SET) is what keeps
    # text that reached the SQL from switching the session out of read-only.
    if not isinstance(tree, exp.Query):
        raise ValueError(f"expected one SELECT query, got {type(tree).__name__}")
    # Reversed DFS visits every node after its descendants, so a rule sees already-translated arguments.
    for node in reversed(list(tree.walk(bfs=False))):
        new = _rule(node)
        if new is not node:
            if node is tree:
                tree = new
            else:
                node.replace(new)
    # PostgreSQL < 16 requires an alias on every subquery in FROM.
    subqueries = [s for s in tree.find_all(exp.Subquery) if isinstance(s.parent, (exp.From, exp.Join)) and not s.alias]
    for i, sq in enumerate(subqueries, 1):
        sq.set("alias", exp.TableAlias(this=exp.to_identifier(f"_sq{i}")))
    # ponytail: length() stays LENGTH(), text only; arrays need CARDINALITY, add when a PostgreSQL user profiles array columns
    return tree.sql(dialect="postgres", identify=True)


def _rule(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Count) and node.this is None:
        node.set("this", exp.Star())
    elif isinstance(node, exp.CountIf):  # SUM(CASE ...) would be NULL over no rows; ClickHouse's countIf is 0
        return exp.Filter(this=exp.Count(this=exp.Star()), expression=exp.Where(this=node.this))
    elif isinstance(node, exp.Div):  # x / 0 is an error on PostgreSQL, nan/inf on ClickHouse: NULL here
        node.set("expression", exp.Nullif(this=node.expression, expression=exp.Literal.number(0)))
    elif isinstance(node, exp.Quantile):
        return exp.WithinGroup(this=exp.PercentileCont(this=node.args["quantile"]),
                               expression=exp.Order(expressions=[exp.Ordered(this=node.this)]))
    elif isinstance(node, exp.Round) and node.args.get("decimals") is not None:
        node.set("this", exp.cast(node.this, "NUMERIC"))  # PostgreSQL only has round(numeric, int)
    elif isinstance(node, exp.Select):
        _limit_by(node)
        _system_columns(node)
    elif isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc)):
        args = node.expressions
        name = node.name.lower()
        if name == "uniqexact":
            arg = args[0] if len(args) == 1 else exp.Tuple(expressions=args)
            if isinstance(arg, exp.Struct):
                arg = exp.Tuple(expressions=arg.expressions)
            return exp.Count(this=exp.Distinct(expressions=[arg]))
        if name == "tostring":
            return exp.cast(args[0], "TEXT")
        if name == "todatetime":
            return exp.cast(args[0], "TIMESTAMP")
        if name == "empty":
            return exp.Paren(this=exp.EQ(this=exp.Anonymous(this="CARDINALITY", expressions=[args[0]]),
                                         expression=exp.Literal.number(0)))
        if name == "stddevpop":
            return exp.StddevPop(this=args[0])
        if name == "today":
            return exp.CurrentDate()
        if name == "domain":
            return exp.Coalesce(this=exp.Substring(this=args[0], start=exp.Literal.string(_DOMAIN_RE)),
                                expressions=[exp.Literal.string("")])
        if name == "isnull":
            return exp.Paren(this=exp.Is(this=args[0], expression=exp.Null()))
    return node


def _limit_by(select: exp.Select) -> None:
    """`LIMIT 1 BY k` -> `SELECT DISTINCT ON (k)`; both keep one arbitrary row per key."""
    limit = select.args.get("limit")
    if limit is not None and limit.expressions and limit.expression.name == "1":
        select.set("distinct", exp.Distinct(on=exp.Tuple(expressions=limit.expressions)))
        select.set("limit", None)


def _system_columns(select: exp.Select) -> None:
    """The completeness metadata query reads system.columns; PostgreSQL has information_schema.columns."""
    frm = select.find(exp.From)
    table = frm.this if frm else None
    if not (isinstance(table, exp.Table) and table.db == "system" and table.name == "columns"):
        return
    for like in list(select.find_all(exp.Like)):
        if like.this.name == "type":
            like.replace(exp.EQ(this=exp.column("is_nullable"), expression=exp.Literal.string("YES")))
    for col in select.find_all(exp.Column):
        if col.name in _INFORMATION_SCHEMA:
            col.set("this", exp.to_identifier(_INFORMATION_SCHEMA[col.name]))
    table.replace(exp.table_("columns", db="information_schema"))
