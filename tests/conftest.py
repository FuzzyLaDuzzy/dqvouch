import dataclasses
import os
import pathlib

import pytest

from dqvouch.config import load_settings
from dqvouch.warehouse import connect

EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / "examples" / "orders"


DEMO_CONFIGS = {"postgres": "dqvouch.toml", "clickhouse": "dqvouch.clickhouse.toml"}


@pytest.fixture(params=list(DEMO_CONFIGS))
def demo_config_path(request):
    """The orders example's config; every test that uses the demo database runs on both engines."""
    return EXAMPLE / DEMO_CONFIGS[request.param]


@pytest.fixture
def demo_settings(demo_config_path):
    return load_settings(demo_config_path)


@pytest.fixture
def wh(demo_settings):
    try:
        warehouse = connect(demo_settings.database)
        warehouse.query("SELECT 1")
    except Exception as e:
        engine = demo_settings.database.engine
        load = "python examples/orders/load.py" + ("" if engine == "postgres" else f" examples/orders/{DEMO_CONFIGS[engine]}")
        message = f"demo {engine} not running ({e}): docker compose up -d {engine} && {load}"
        if os.getenv("DQVOUCH_REQUIRE_DB") == "1":
            pytest.fail(message)
        pytest.skip(message)
    yield warehouse
    warehouse.close()


@pytest.fixture
def reader_wh(demo_settings, wh):
    """The same demo database, as the SELECT-only user load.py creates."""
    reader = connect(dataclasses.replace(demo_settings.database, user="dq_reader", password="dq_reader"))
    yield reader
    reader.close()
