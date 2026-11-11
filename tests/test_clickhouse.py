import importlib.util
from collections import Counter

from conftest import EXAMPLE

_spec = importlib.util.spec_from_file_location("orders_load", EXAMPLE / "load.py")
orders_load = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orders_load)

KEYS = {"database", "table", "engine", "partition_key", "table_description", "column_name", "data_type",
        "column_description", "position"}


def test_generate_orders_plants_defects():
    rows = orders_load.generate_orders(42)
    assert len(rows) == 205
    assert sum(r["order_id"] is None for r in rows) == 2
    ids = Counter(r["order_id"] for r in rows if r["order_id"] is not None)
    assert {i for i, n in ids.items() if n > 1} == {10, 20, 30} and all(ids[i] == 2 for i in (10, 20, 30))
    statuses = Counter(r["status"] for r in rows)
    assert statuses["shiped"] == 2 and statuses["test"] == 4 and statuses[""] == 3
    assert sum(r["quantity"] < 0 for r in rows) == 2
    assert orders_load.generate_orders(42) == orders_load.generate_orders(42)
    # orders of a customer that does not exist (the fact table's JOIN drops them) and a badly cased channel
    orphans = [r for r in rows if r["customer_id"] == 999]
    assert len(orphans) == 3 and all(r["status"] in orders_load.STATUSES and r["order_id"] for r in orphans)
    assert all(1 <= r["customer_id"] <= 80 for r in rows if r["customer_id"] != 999)
    assert Counter(r["channel"] for r in rows)["WEB"] == 2


def test_generate_customers_plants_defects():
    rows = orders_load.generate_customers()
    assert len(rows) == 83
    ids = Counter(r["customer_id"] for r in rows if r["customer_id"] is not None)
    assert {i for i, n in ids.items() if n > 1} == {5, 15}
    assert sum(r["customer_id"] is None for r in rows) == 1
    assert sum(r["country"] not in ("PT", "ES", "FR", "DE", "IT") for r in rows) == 2
    assert sum("@" not in r["email"] for r in rows) == 2
    assert any(r["email"] and r["email"] != r["email"].strip().lower() for r in rows)  # staging normalises it
    assert orders_load.generate_customers() == orders_load.generate_customers()


def test_generate_returns_plants_defects():
    web, store = orders_load.generate_returns()
    assert (len(web), len(store)) == (12, 8)
    assert len({r["return_id"] for r in web} & {r["return_id"] for r in store}) == 1  # one id in both sources
    assert sum(r["order_id"] == 5000 for r in web + store) == 1                       # a return of no order
    assert sum(r["reason"] == "" for r in web + store) == 1


def test_fetch_metadata_returns_demo_tables(wh, demo_settings):
    rows = wh.fetch_metadata(demo_settings.database.schemas)
    assert {f"{r['database']}.{r['table']}" for r in rows} == {
        "landing.orders_raw", "landing.customers_raw", "landing.returns_web", "landing.returns_store",
        "staging.orders_trans", "staging.customers", "staging.returns",
        "analytic.fact_orders", "analytic.daily_sales", "analytic.sales_by_country"}
    assert all(set(r) == KEYS for r in rows)
    assert any(r["table"] == "fact_orders" and r["table_description"] for r in rows)


def test_query_returns_columns_and_rows(wh):
    assert wh.query("SELECT 1 AS x") == (["x"], [(1,)])


def test_load_refuses_a_config_outside_the_example(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orders_load.time, "sleep", lambda s: None)
    cfg = tmp_path / "dqvouch.toml"
    cfg.write_text((EXAMPLE / "dqvouch.toml").read_text(encoding="utf-8").replace("port = 15432", "port = 1"),
                   encoding="utf-8")
    assert orders_load.main([str(cfg)]) == 2  # it drops schemas: never on a database the example does not own
    assert "drops and recreates" in capsys.readouterr().out
