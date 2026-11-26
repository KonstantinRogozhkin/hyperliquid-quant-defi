.PHONY: lint test dashboard scan-hip3 scan-boros scan-evm bench-mm

PYTHON := .venv/bin/python
PYTEST := .venv/bin/pytest
RUFF := .venv/bin/ruff

lint:
	$(RUFF) check src scripts tests

test:
	$(PYTEST) -q

dashboard:
	$(PYTHON) scripts/analytics/build_hl_portfolio_dashboard.py

scan-hip3:
	$(PYTHON) scripts/analytics/run_hl_hip3_arb_scan.py

scan-boros:
	$(PYTHON) scripts/analytics/run_hl_boros_scan.py

scan-evm:
	$(PYTHON) scripts/analytics/run_hl_hyperevm_defi_scan.py

bench-mm:
	$(PYTHON) scripts/analytics/run_hl_mm_testnet.py --benchmark-latency
