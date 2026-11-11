#!/usr/bin/env sh
# Creates .venv in this folder, installs dqvouch into it and says how to start.  Usage: sh install.sh
set -e
python3 -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e .
echo "dqvouch is installed. To try the example:"
echo "  docker compose up -d postgres"
echo "  .venv/bin/python examples/orders/load.py"
echo "  .venv/bin/dqvouch ui --config examples/orders/dqvouch.toml"
