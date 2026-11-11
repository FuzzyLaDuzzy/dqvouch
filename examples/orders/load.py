"""Loads the fictional orders example into the demo database (docker compose up -d first).

Usage: python examples/orders/load.py [CONFIG]   (default: examples/orders/dqvouch.toml, PostgreSQL)

The landing tables (orders, customers, and returns from two sources) get generated rows with planted defects; the
SQL in sql/ then fills staging and analytic, the way a real pipeline would; finally a few late orders arrive in
landing after the transformations ran. This script writes to the database. dqvouch itself never does.
"""
import datetime
import pathlib
import random
import sys
import time

import clickhouse_connect
import psycopg

from dqvouch.config import load_settings

HERE = pathlib.Path(__file__).resolve().parent
SCHEMAS = ("landing", "staging", "analytic")
READER = "dq_reader"  # the SELECT-only user the tests use (load.py creates it)
STATUSES = ["pending", "shipped", "delivered", "cancelled"]
CHANNELS = ["web", "store", "app"]
COUNTRIES = ["PT", "ES", "FR", "DE", "IT"]
SEGMENTS = ["retail", "business", "vip"]
REASONS = ["damaged", "wrong_item", "late", "other"]
RETURN_COLUMNS = ["return_id", "order_id", "return_date", "reason", "quantity"]
COLUMNS = {
    "landing.orders_raw": ["order_id", "status", "quantity", "unit_price", "order_date", "customer_email",
                           "customer_id", "channel"],
    "landing.customers_raw": ["customer_id", "email", "country", "segment", "signup_date"],
    "landing.returns_web": RETURN_COLUMNS,
    "landing.returns_store": RETURN_COLUMNS,
}
TABLES = ["landing.orders_raw", "landing.customers_raw", "landing.returns_web", "landing.returns_store",
          "staging.orders_trans", "staging.customers", "staging.returns",
          "analytic.fact_orders", "analytic.daily_sales", "analytic.sales_by_country"]


def _order(rng, order_id, status):
    return {
        "order_id": order_id,
        "status": status,
        "quantity": rng.randint(1, 5),
        "unit_price": round(rng.uniform(5, 100), 2),
        "order_date": datetime.date(2026, 10, 1) + datetime.timedelta(days=rng.randrange(30)),
        "customer_email": f"customer{rng.randrange(1, 80)}@example.com",
    }


def _customer_and_channel(rows, rng):
    for r in rows:
        r["customer_id"] = rng.randint(1, 80)
        r["channel"] = rng.choice(CHANNELS)


def generate_orders(seed: int = 42) -> list[dict]:
    """200 orders (ids 1-200) plus planted defects: 2 'shiped' typos, 4 'test' orders, 3 empty statuses and 2
    negative quantities among them; 3 extra rows repeating ids 10, 20 and 30; 2 rows without an id; 3 orders of
    customer 999, who does not exist; 2 orders whose channel is 'WEB' instead of 'web'."""
    rng = random.Random(seed)
    rows = [_order(rng, i, rng.choice(STATUSES)) for i in range(1, 201)]
    picked = rng.sample(range(200), 9)
    for i in picked[:2]:
        rows[i]["status"] = "shiped"
    for i in picked[2:6]:
        rows[i]["status"] = "test"
    for i in picked[6:9]:
        rows[i]["status"] = ""
    for i in rng.sample(range(200), 2):
        rows[i]["quantity"] = -rows[i]["quantity"]
    rows += [_order(rng, i, "pending") for i in (10, 20, 30)]
    rows += [_order(rng, None, "pending") for _ in range(2)]
    # The customer and channel come from their own random stream, so the columns above keep their values.
    extra = random.Random(seed + 1)
    _customer_and_channel(rows, extra)
    plain = [i for i in range(200) if rows[i]["status"] in STATUSES and rows[i]["quantity"] > 0
             and rows[i]["order_id"] not in (10, 20, 30)]
    orphans = extra.sample(plain, 3)
    for i in orphans:
        rows[i]["customer_id"] = 999
    for i in extra.sample([i for i in plain if i not in orphans], 2):
        rows[i]["channel"] = "WEB"
    return rows


def late_orders(seed: int = 7) -> list[dict]:
    """Orders that reach landing after the transformations ran, so staging and analytic do not have them."""
    rng = random.Random(seed)
    rows = [_order(rng, i, "pending") for i in range(1001, 1006)]
    _customer_and_channel(rows, rng)
    return rows


def _customer(rng, customer_id):
    return {
        "customer_id": customer_id,
        "email": f"customer{customer_id}@example.com",
        "country": rng.choice(COUNTRIES),
        "segment": rng.choice(SEGMENTS),
        "signup_date": datetime.date(2025, 1, 1) + datetime.timedelta(days=rng.randrange(600)),
    }


def generate_customers(seed: int = 11) -> list[dict]:
    """80 customers plus planted defects: an e-mail without '@', an empty one and one that needs trimming and lower
    case; 2 invalid countries; customers 5 and 15 sent twice (the newer row wins in staging); 1 row without an id."""
    rng = random.Random(seed)
    rows = [_customer(rng, i) for i in range(1, 81)]
    rows[11]["email"] = "customer12example.com"
    rows[59]["email"] = ""
    rows[39]["email"] = " Customer40@Example.COM "
    rows[20]["country"] = "Portugal"
    rows[65]["country"] = "XX"
    for customer_id in (5, 15):
        again = _customer(rng, customer_id)
        again["email"] = f"customer{customer_id}.new@example.com"
        again["signup_date"] = rows[customer_id - 1]["signup_date"] + datetime.timedelta(days=30)
        rows.append(again)
    walk_in = _customer(rng, None)
    walk_in["email"] = "walk-in@example.com"
    rows.append(walk_in)
    return rows


def _return(rng, return_id):
    return {
        "return_id": return_id,
        "order_id": rng.randint(1, 200),
        "return_date": datetime.date(2026, 10, 5) + datetime.timedelta(days=rng.randrange(30)),
        "reason": rng.choice(REASONS),
        "quantity": rng.randint(1, 2),
    }


def generate_returns(seed: int = 13) -> tuple[list[dict], list[dict]]:
    """(web returns, store returns), unioned in staging. Planted: return id 7 in both sources, a web return of order
    5000 (which does not exist) and a store return without a reason."""
    rng = random.Random(seed)
    web = [_return(rng, i) for i in range(1, 13)]
    store = [_return(rng, i) for i in range(101, 109)]
    store[2]["return_id"] = 7
    web[4]["order_id"] = 5000
    store[5]["reason"] = ""
    return web, store


def _statements(path: pathlib.Path) -> list[str]:
    return [s.strip() for s in path.read_text(encoding="utf-8").split(";") if s.strip()]


class _ClickHouse:
    def __init__(self, db):
        self.client = clickhouse_connect.get_client(host=db.host, port=db.port, username=db.user, password=db.password)
        self.client.query("SELECT 1")

    def recreate_schema(self, schema):
        self.client.command(f"DROP DATABASE IF EXISTS {schema}")
        self.client.command(f"CREATE DATABASE {schema}")

    def execute(self, statement):
        self.client.command(statement)

    def insert(self, table, rows):
        columns = COLUMNS[table]
        self.client.insert(table, [[r[c] for c in columns] for r in rows], column_names=columns)

    def count(self, table):
        return self.client.query(f"SELECT count() FROM {table}").result_rows[0][0]

    def create_reader(self):
        self.client.command(f"CREATE USER IF NOT EXISTS {READER} IDENTIFIED WITH plaintext_password BY '{READER}' "
                            f"SETTINGS readonly = 2")
        for schema in SCHEMAS:
            self.client.command(f"GRANT SELECT ON {schema}.* TO {READER}")


class _Postgres:
    def __init__(self, db):
        # A normal (writable) connection: loading the example is the one place that writes.
        self.conn = psycopg.connect(host=db.host, port=db.port, user=db.user, password=db.password,
                                    dbname=db.database, autocommit=True, connect_timeout=5)

    def recreate_schema(self, schema):
        self.conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        self.conn.execute(f"CREATE SCHEMA {schema}")

    def execute(self, statement):
        self.conn.execute(statement)

    def insert(self, table, rows):
        columns = COLUMNS[table]
        with self.conn.cursor() as cur:
            cur.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
                            [[r[c] for c in columns] for r in rows])

    def count(self, table):
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def create_reader(self):
        self.conn.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{READER}') THEN "
                          f"CREATE ROLE {READER} LOGIN PASSWORD '{READER}'; END IF; END $$")
        self.conn.execute(f"GRANT USAGE ON SCHEMA {', '.join(SCHEMAS)} TO {READER}")
        self.conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {', '.join(SCHEMAS)} TO {READER}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    config = pathlib.Path(argv[0]).resolve() if argv else HERE / "dqvouch.toml"
    if config.parent != HERE:
        print(f"load.py drops and recreates the schemas {', '.join(SCHEMAS)}, so it only takes the example's own "
              f"configs in {HERE} (dqvouch.toml or dqvouch.clickhouse.toml), never one that may point at your data.")
        return 2
    db = load_settings(config).database
    engine = {"clickhouse": _ClickHouse, "postgres": _Postgres}[db.engine]
    for _ in range(30):  # the container needs a few seconds after `docker compose up`
        try:
            target = engine(db)
            break
        except Exception as e:
            print(f"waiting for {db.engine} at {db.host}:{db.port} ({e.__class__.__name__})")
            time.sleep(2)
    else:
        print(f"{db.engine} did not answer; is `docker compose up -d` running?")
        return 1

    for schema in SCHEMAS:
        target.recreate_schema(schema)
    for statement in _statements(HERE / f"ddl_{db.engine}.sql"):
        target.execute(statement)
    web, store = generate_returns()
    target.insert("landing.orders_raw", generate_orders())
    target.insert("landing.customers_raw", generate_customers())
    target.insert("landing.returns_web", web)
    target.insert("landing.returns_store", store)
    for path in sorted((HERE / "sql").glob("*.sql")):  # the same transformation SQL on both engines
        for statement in _statements(path):
            target.execute(statement)
    target.insert("landing.orders_raw", late_orders())
    for table in TABLES:
        print(f"{table}: {target.count(table)} rows")
    target.create_reader()
    print(f"read-only user: {READER} (password {READER})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
