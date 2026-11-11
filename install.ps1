# Creates .venv in this folder, installs dqvouch into it and says how to start.
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"
python -m venv .venv
.\.venv\Scripts\python -m pip install --quiet --upgrade pip
.\.venv\Scripts\python -m pip install --quiet -e .
Write-Host "dqvouch is installed. To try the example:"
Write-Host "  docker compose up -d postgres"
Write-Host "  .\.venv\Scripts\python examples\orders\load.py"
Write-Host "  .\.venv\Scripts\dqvouch ui --config examples\orders\dqvouch.toml"
