# AGENTS

## Purpose
- Standalone Hyperliquid quantitative DeFi research, paper-trading, execution, and telemetry suite (`src/hl_quant/`).
- Zero dependencies on `BackTesterv2`, `ResearchLAB`, or DuckDB.

## Git & PR Workflow (Required)
- `main` is protected by a GitHub Ruleset (`Protect main (PR + CI)`): direct pushes, force-pushes, and branch deletions are blocked.
- All changes must go through a feature/fix branch and Pull Request into `main`:
  1. Branch from up-to-date `main` (`feat/...`, `fix/...`, `chore/...`, `docs/...`).
  2. Run `make lint && make test` locally before pushing.
  3. Open a PR via `gh pr create` and wait for the required `tests` CI check to pass.
  4. Merge via squash (`gh pr merge --squash --delete-branch`); GitHub automatically deletes merged head branches.

## Layout & Conventions
- `src/hl_quant/`: reusable strategy, microstructure, scanner, telemetry, and execution modules.
- `scripts/analytics/` & `scripts/ops/`: thin CLI entrypoints and lock-aware cron wrappers; keep reusable logic in `src/hl_quant/`.
- `docs/wiki/`: mirror of the GitHub Wiki (sync via `scripts/ops/publish_wiki.sh`).
- `data/`, `.env`, and `.neira/` are local-only and gitignored.

## Safety & Security
- Never commit `.env`, API secrets, private keys, or internal `.neira/` notes.
- Live order execution stays hard-pinned to `https://api.hyperliquid-testnet.xyz` unless explicitly redesigned and approved.
