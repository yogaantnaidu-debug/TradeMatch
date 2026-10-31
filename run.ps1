# Windows one-command run:  .\run.ps1
# Use -NoDocker if Postgres is already running somewhere and DATABASE_URL points to it.
param([switch]$NoDocker)
$ErrorActionPreference = "Stop"

if (-not $NoDocker) {
    # --wait blocks until the healthcheck in docker-compose.yml passes.
    docker compose up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed" }
}

if (-not (Test-Path .venv)) { py -3.11 -m venv .venv }
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install -q -r requirements.txt

# Stop at the first failing step so a broken stage is never followed by a misleading report.
foreach ($step in @(@("generate_data.py"), @("-m", "tradesync.pipeline"), @("evaluate.py"))) {
    & $py @step
    if ($LASTEXITCODE -ne 0) { throw "step failed: $step" }
}
