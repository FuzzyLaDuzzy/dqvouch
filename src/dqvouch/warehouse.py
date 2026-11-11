"""Read-only connection to the analysed database. dqvouch only ever runs SELECT queries through it.

Inside dqvouch every SQL query and column type is in ClickHouse notation (what the thesis validated); each
connection translates at the boundary: types coming in, SQL going out (`render`).
"""
import re
from decimal import Decimal

import clickhouse_connect
import psycopg

from dqvouch.config import DatabaseSettings
from dqvouch.translate import check_one_select, to_postgres

WRITE_WARNING = ("This database user can write. dqvouch only ever sends read-only SELECT queries, but a user that "
                 "can only read is the guarantee that holds whatever happens: on PostgreSQL grant it SELECT only; on "
                 "ClickHouse give it `readonly = 2` (no writes, and dqvouch can still set its query time limit).")


def clickhouse_query_settings(readonly_level: int, timeout: int) -> dict:
    """The settings sent with every ClickHouse query: read-only enforced by the server (readonly = 2 still lets the
    query carry settings) and the time limit. A readonly = 1 user may not receive any setting at all."""
    if readonly_level == 1:
        return {}
    settings = {"readonly": 2} if readonly_level == 0 else {}
    if timeout:
        settings["max_execution_time"] = timeout
    return settings

_CLICKHOUSE_METADATA = """
SELECT
    c.database AS database,
    c.table AS table,
    t.engine AS engine,
    t.partition_key AS partition_key,
    t.comment AS table_description,
    c.name AS column_name,
    c.type AS data_type,
    c.comment AS column_description,
    c.position AS position
FROM system.columns c
LEFT JOIN system.tables t
    ON c.database = t.database AND c.table = t.name
WHERE c.database IN {schemas:Array(String)}
ORDER BY c.database, c.table, c.position
"""


class ClickHouseWarehouse:
    dialect = "clickhouse"

    def __init__(self, db: DatabaseSettings):
        self._db = db
        self._client = self._connect()
        self._readonly = int(self._client.query("SELECT getSetting('readonly')").result_rows[0][0])
        self._settings = clickhouse_query_settings(self._readonly, db.query_timeout_seconds)

    def _connect(self):
        timeout = self._db.query_timeout_seconds
        return clickhouse_connect.get_client(
            host=self._db.host, port=self._db.port, username=self._db.user, password=self._db.password,
            # the server stops a query at the limit; without one, heavy cross-table profiling joins can take minutes
            send_receive_timeout=timeout + 30 if timeout else 3600,
        )

    def fetch_metadata(self, schemas: list[str]) -> list[dict]:
        result = self._client.query(_CLICKHOUSE_METADATA, parameters={"schemas": list(schemas)})
        return [dict(zip(result.column_names, row)) for row in result.result_rows]

    def can_write(self, schemas: list[str]) -> bool:
        return self._readonly == 0

    def query(self, sql: str) -> tuple[list[str], list[tuple]]:
        check_one_select(sql)
        try:
            result = self._client.query(sql, settings=self._settings)
        except Exception:
            # A timed-out query keeps running server-side and locks the session; reconnect so the next one works.
            self._client.close()
            self._client = self._connect()
            raise
        return list(result.column_names), [tuple(row) for row in result.result_rows]

    def render(self, sql: str) -> str:
        """The SQL this connection actually runs for `sql`."""
        return sql

    def close(self) -> None:
        self._client.close()


_POSTGRES_METADATA = """
SELECT
    n.nspname AS database,
    c.relname AS "table",
    CASE c.relkind WHEN 'r' THEN 'PostgreSQL table' WHEN 'p' THEN 'PostgreSQL partitioned table'
        WHEN 'v' THEN 'PostgreSQL view' WHEN 'm' THEN 'PostgreSQL materialized view'
        ELSE 'PostgreSQL foreign table' END AS engine,
    pg_get_partkeydef(c.oid) AS partition_key,
    obj_description(c.oid, 'pg_class') AS table_description,
    a.attname AS column_name,
    format_type(a.atttypid, a.atttypmod) AS data_type,
    NOT a.attnotnull AS nullable,
    col_description(c.oid, a.attnum) AS column_description,
    a.attnum AS position
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition AND n.nspname = ANY(%s)
ORDER BY n.nspname, c.relname, a.attnum
"""

_PG_TYPES = {
    "smallint": "Int16", "integer": "Int32", "bigint": "Int64", "real": "Float32", "double precision": "Float64",
    "numeric": "Decimal", "boolean": "Bool", "text": "String", "citext": "String", "json": "String",
    "jsonb": "String", "uuid": "UUID", "date": "Date",
}


def canonical_type(pg_type: str, nullable: bool) -> str:
    """A PostgreSQL type (as format_type() prints it) in dqvouch's internal ClickHouse notation."""
    if pg_type.endswith("[]"):
        return f"Array({canonical_type(pg_type[:-2], False)})"
    numeric = re.fullmatch(r"numeric\((\d+),(\d+)\)", pg_type)
    if numeric:
        base = f"Decimal({numeric[1]}, {numeric[2]})"
    elif pg_type.startswith("character"):  # character varying(n), character(n)
        base = "String"
    elif pg_type.startswith("timestamp"):
        base = "DateTime"
    else:
        base = _PG_TYPES.get(pg_type, pg_type)
    return f"Nullable({base})" if nullable else base


class PostgresWarehouse:
    dialect = "postgres"

    def __init__(self, db: DatabaseSettings):
        self._db = db
        self._conn = self._connect()

    def _connect(self):
        # The server refuses writes in every transaction; autocommit means a failed query cannot abort the next.
        return psycopg.connect(
            host=self._db.host, port=self._db.port, user=self._db.user, password=self._db.password,
            dbname=self._db.database, autocommit=True, connect_timeout=10,
            options="-c default_transaction_read_only=on"
                    + (f" -c statement_timeout={self._db.query_timeout_seconds * 1000}"
                       if self._db.query_timeout_seconds else ""),
        )

    def can_write(self, schemas: list[str]) -> bool:
        """A superuser, or a user with a write privilege on any table of the schemas."""
        with self._conn.cursor() as cur:
            cur.execute("""
                SELECT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
                    OR COALESCE(bool_or(has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')), false)
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p')""", [list(schemas)])
            return bool(cur.fetchone()[0])

    def fetch_metadata(self, schemas: list[str]) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(_POSTGRES_METADATA, [list(schemas)])
            names = [d.name for d in cur.description]
            rows = [dict(zip(names, row)) for row in cur.fetchall()]
        for row in rows:
            row["data_type"] = canonical_type(row["data_type"], row.pop("nullable"))
        return rows

    def query(self, sql: str) -> tuple[list[str], list[tuple]]:
        rendered = self.render(sql)
        try:
            return self._run(rendered)
        except Exception:
            if not self._conn.broken:
                raise
        # The session was dropped (server restart, pooler, idle timeout during the review pause): reconnect and run
        # it once more, which is safe because it is a single read-only SELECT.
        self._conn = self._connect()
        return self._run(rendered)

    def _run(self, sql: str) -> tuple[list[str], list[tuple]]:
        with self._conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is None:
                return [], []
            columns = [d.name for d in cur.description]
            return columns, [tuple(float(v) if isinstance(v, Decimal) else v for v in row) for row in cur.fetchall()]

    def render(self, sql: str) -> str:
        """The SQL this connection actually runs for `sql`."""
        return to_postgres(sql)

    def close(self) -> None:
        self._conn.close()


def renderer(engine: str):
    """The SQL an engine runs for dqvouch's internal (ClickHouse-notation) SQL, without connecting to it."""
    if engine == "clickhouse":
        return lambda sql: sql
    if engine == "postgres":
        return to_postgres
    raise ValueError(f"unsupported database engine: {engine!r}")


def connect(db: DatabaseSettings) -> ClickHouseWarehouse | PostgresWarehouse:
    if db.engine == "clickhouse":
        return ClickHouseWarehouse(db)
    if db.engine == "postgres":
        return PostgresWarehouse(db)
    raise ValueError(f"unsupported database engine: {db.engine!r}")
