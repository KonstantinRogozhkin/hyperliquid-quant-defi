.PHONY: lint test api frontend-dev frontend-test frontend-build docker-up dashboard scan-hip3 scan-spot-perp run-autopilot scan-boros scan-evm bench-mm

PYTHON := .venv/bin/python
PYTEST := .venv/bin/pytest
RUFF := .venv/bin/ruff
UVICORN := .venv/bin/uvicorn

lint:
	$(RUFF) check src scripts tests

test:
	$(PYTEST) -q

api:
	PYTHONPATH=src $(UVICORN) hl_quant.api.app:app --reload --host 127.0.0.1 --port 8000

frontend-dev:
	cd frontend && npm run dev

frontend-test:
	cd frontend && npm test

frontend-build:
	cd frontend && npm run build

docker-up:
	docker compose up --build

dashboard:
	$(PYTHON) scripts/analytics/build_hl_portfolio_dashboard.py

scan-hip3:
	$(PYTHON) scripts/analytics/run_hl_hip3_arb_scan.py

scan-spot-perp:
	$(PYTHON) scripts/analytics/run_hl_spot_perp_scan.py

run-autopilot:
	$(PYTHON) scripts/analytics/run_hl_autopilot_tick.py

scan-boros:
	$(PYTHON) scripts/analytics/run_hl_boros_scan.py

scan-evm:
	$(PYTHON) scripts/analytics/run_hl_hyperevm_defi_scan.py

bench-mm:
	$(PYTHON) scripts/analytics/run_hl_mm_testnet.py --benchmark-latency
