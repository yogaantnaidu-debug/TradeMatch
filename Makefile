# Linux/macOS/CI entry point. On Windows use run.ps1.
PY ?= python

.PHONY: run db install data pipeline evaluate test benchmark down

run: db install data pipeline evaluate

db:
	docker compose up -d --wait

install:
	$(PY) -m pip install -q -r requirements.txt

data:
	$(PY) generate_data.py

pipeline:
	$(PY) -m tradesync.pipeline

evaluate:
	$(PY) evaluate.py

test:
	$(PY) -m pytest --cov=tradesync --cov=generate_data --cov=evaluate --cov-report=term-missing

benchmark:
	$(PY) benchmark_explain.py

down:
	docker compose down
