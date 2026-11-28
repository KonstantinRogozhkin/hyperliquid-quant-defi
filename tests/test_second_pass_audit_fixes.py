from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hl_quant import hl_analytics_risk as ar
from hl_quant import hl_autopilot as ap
from hl_quant import hl_portfolio_dashboard as pdash
from hl_quant import hl_post_trade_attribution as pta
from hl_quant import hl_testnet_executor as hlex
from hl_quant import metrics as prom
from hl_quant import scheduler as sched


def test_venue_concentration_looks_through_idle_sleeves_matching_tier_concentration(tmp_path: Path) -> None:
    """Idle HIP-3 (0 positions) and partially deployed Cross-DEX must look through to Cash / USDC."""
    (tmp_path / "hl_spot_perp_paper").mkdir()
    (tmp_path / "hl_spot_perp_paper" / "state.json").write_text(
        json.dumps(
            {
                "equity_usd": 100_000.0,
                "starting_capital_usd": 100_000.0,
                "free_cash_usd": 65_000.0,
                "positions": {"HYPE": {"spot_notional_usd": 20_000.0, "perp_margin_usd": 5_000.0}},
                "risk_telemetry": {
                    "gross_leverage": 0.50,
                    "venue_breakdown_usd": {
                        "HyperCore Spot": 20_000.0,
                        "HyperCore Perp Margin": 5_000.0,
                        "HyperCore HLP Vault": 10_000.0,
                        "Free USDC Reserve": 65_000.0,
                    },
                    "asset_tier_breakdown_usd": {
                        "Crypto - Large Cap": 25_000.0,
                        "HLP Vault Reserve": 10_000.0,
                        "Free USDC Buffer": 65_000.0,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    (tmp_path / "hl_hip3_paper").mkdir()
    (tmp_path / "hl_hip3_paper" / "state.json").write_text(
        json.dumps({"equity_usd": 50_000.0, "starting_capital_usd": 50_000.0, "positions": {}}),
        encoding="utf-8",
    )

    (tmp_path / "hl_cross_dex_paper").mkdir()
    (tmp_path / "hl_cross_dex_paper" / "state.json").write_text(
        json.dumps(
            {
                "equity_usd": 50_000.0,
                "starting_capital_usd": 50_000.0,
                "positions": {"ETH": {"leg_notional_usd": 5_000.0, "margin_locked_usd": 10_000.0}},
            }
        ),
        encoding="utf-8",
    )

    (tmp_path / "hl_autopilot").mkdir()
    (tmp_path / "hl_autopilot" / "state.json").write_text(
        json.dumps(
            {
                "gross_equity_usd": 250_000.0,
                "sleeve_allocations_usd": {
                    "spot_perp_paper": 75_000.0,
                    "cross_dex_paper": 75_000.0,
                    "hip3_paper": 50_000.0,
                    "hlp_savings_reserve": 37_500.0,
                    "crisis_cta": 12_500.0,
                },
                "cta_state": {"weights": {"BTC": -0.4, "PAXG": 0.2}},
                "risk_telemetry": {
                    "gross_leverage": 0.65,
                    "venue_breakdown_usd": {"HIP-3 Builders": 50_000.0},
                },
            }
        ),
        encoding="utf-8",
    )

    risk = ar.build_consolidated_risk_exposure(tmp_path)
    venues = {r["label"]: r["usd"] for r in risk["venue_concentration"]}
    tiers = {r["label"]: r["usd"] for r in risk["asset_tier_concentration"]}

    # HIP-3 has 0 open positions -> $0 deployed on HIP-3 Builders in BOTH tables!
    assert "HIP-3 Builders" not in venues
    assert "US Equities / HIP-3 Builders" not in tiers
    # Cross-DEX is 20% deployed ($10k / $50k) -> $15,000 of its $75,000 Autopilot allocation
    assert venues["HL x Lighter/Extended"] == 15_000.0
    assert tiers["Crypto - Cross-DEX Neutral"] == 15_000.0
    # Cash / USDC matches between venue_concentration and asset_tier_concentration
    assert abs(venues["Cash / USDC"] - tiers["Cash / USDC"]) < 0.05
    assert abs(sum(venues.values()) - sum(tiers.values())) < 0.05


def test_autopilot_tick_venue_breakdown_looks_through_idle_hip3(tmp_path: Path) -> None:
    (tmp_path / "hl_hip3_paper").mkdir()
    (tmp_path / "hl_hip3_paper" / "state.json").write_text(
        json.dumps({"equity_usd": 50_000.0, "starting_capital_usd": 50_000.0, "positions": {}}),
        encoding="utf-8",
    )
    closes = {c: [100.0 * (1.005**i) for i in range(50)] for c in ap.CTA_UNIVERSE}
    res = ap.run_autopilot_tick(data_root=tmp_path, now_ms=1_795_700_000_000, daily_closes=closes)
    vb = res["state"]["risk_telemetry"]["venue_breakdown_usd"]
    assert "HIP-3 Builders" not in vb
    assert vb.get("Cash / USDC", 0.0) > 0.0


def test_attribution_ignores_pre_audit_log_fills_and_includes_observed_at_utc(tmp_path: Path) -> None:
    audit_path = tmp_path / "hl_testnet_audit.jsonl"
    mm_audit_path = tmp_path / "mm_audit.jsonl"
    t_audit_start = 1_795_800_000_000

    audit_path.write_text(
        json.dumps(
            {
                "ts_ms": t_audit_start,
                "mode": "testnet_live",
                "status": "ok",
                "cancelled_count": 0,
                "orders": [
                    {
                        "mode": "testnet_live",
                        "response": {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 501}}]}}},
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    mm_audit_path.write_text(
        json.dumps({"ts_ms": t_audit_start + 10_000, "mode": "testnet_live", "oid": 777}) + "\n",
        encoding="utf-8",
    )

    base_snap: dict[str, Any] = {
        "account_address": "0xabc",
        "observed_at_ms": t_audit_start + 120_000,
        "user_abstraction": "unifiedAccount",
        "spot": {"balances": [{"coin": "USDC", "total": "1000.0", "hold": "50.0"}]},
        "clearinghouse": {"marginSummary": {"accountValue": "150.0"}, "assetPositions": []},
        "funding": [],
        "open_orders": [],
        "all_mids": {"BTC": 90_000.0},
        "fills": [
            # Large pre-audit historical fill (2 days before audit log existed) -> must NOT contaminate!
            {"coin": "BTC", "px": "90000", "sz": "0.02", "side": "B", "fee": "0.2", "closedPnl": "0", "crossed": False, "oid": 100, "time": t_audit_start - 172_800_000},
            # In-window audited executor fill ($900)
            {"coin": "BTC", "px": "90000", "sz": "0.01", "side": "B", "fee": "0.1", "closedPnl": "0", "crossed": False, "oid": 501, "time": t_audit_start + 5_000},
        ],
    }
    attr_clean = pta.compute_post_trade_attribution(base_snap, audit_log_path=audit_path, mm_audit_log_path=mm_audit_path)
    assert attr_clean.mixed_account_warning == ""
    as_dict = attr_clean.to_dict()
    assert as_dict["observed_at_utc"] is not None and as_dict["observed_at_utc"].endswith("UTC")

    # Concurrent unknown fill inside the audit window still triggers contamination warning
    snap_contam = {
        **base_snap,
        "fills": [
            *base_snap["fills"],
            {"coin": "BTC", "px": "90000", "sz": "0.01", "side": "S", "fee": "0.3", "closedPnl": "0", "crossed": True, "oid": 999, "time": t_audit_start + 15_000},
        ],
    }
    attr_contam = pta.compute_post_trade_attribution(snap_contam, audit_log_path=audit_path, mm_audit_log_path=mm_audit_path)
    assert "not from audited executor orders" in attr_contam.mixed_account_warning

    # Concurrent fill logged in mm_audit.jsonl is identified specifically as MM order flow
    snap_mm = {
        **base_snap,
        "fills": [
            *base_snap["fills"],
            {"coin": "BTC", "px": "90000", "sz": "0.01", "side": "S", "fee": "0.1", "closedPnl": "0", "crossed": False, "oid": 777, "time": t_audit_start + 15_000},
        ],
    }
    attr_mm = pta.compute_post_trade_attribution(snap_mm, audit_log_path=audit_path, mm_audit_log_path=mm_audit_path)
    assert "MM orders sharing this address" in attr_mm.mixed_account_warning


def test_executor_normalizes_stale_cross_leverage_on_held_positions_within_deadband() -> None:
    now_ms = 1_795_800_000_000
    account = hlex.AccountSnapshot(
        equity_usd=1_000.0,
        perps_equity_usd=300.0,
        free_margin_usd=850.0,
        positions_sz={"LIT": 100.0, "TAO": -0.25},
        position_notional_usd={"LIT": 100.0, "TAO": -100.0},
        position_leverage={"LIT": ("cross", 5), "TAO": ("cross", 10)},
        open_orders=[],
        observed_at_ms=now_ms,
    )
    meta = {
        "LIT": hlex.AssetSpec(coin="LIT", sz_decimals=0, max_leverage=5, only_isolated=False),
        "TAO": hlex.AssetSpec(coin="TAO", sz_decimals=2, max_leverage=10, only_isolated=False),
    }
    bbo = {
        "LIT": (0.999, 1.001, now_ms),
        "TAO": (399.5, 400.5, now_ms),
    }
    # Target weights match current positions (0 orders needed), yet LIT=cross:5 and TAO=cross:10 != default 3x
    adapter = hlex.DryRunAdapter()
    res = hlex.execute_rebalance_cycle(
        account,
        {"LIT": 0.10, "TAO": -0.10},
        meta,
        bbo,
        adapter,
        limits=hlex.RiskLimits(default_cross_leverage=3),
        now_ms=now_ms,
    )
    assert res["status"] == "ok"
    assert res["placed_count"] == 0
    lev_by_coin = {u["coin"]: (u["leverage"], u["is_cross"]) for u in adapter.leverage_updates}
    assert lev_by_coin == {"LIT": (3, True), "TAO": (3, True)}


def test_legacy_autopilot_state_drift_and_bootstrap_degraded_refresh(tmp_path: Path) -> None:
    paper_dir = tmp_path / "paper"
    ap_dir = paper_dir / "hl_autopilot"
    ap_dir.mkdir(parents=True)
    # Pre-PR30 state: rebalanced_this_tick=True, sleeves drift=0, but max_weight_drift_pct still has pre-rebalance 6.4
    (ap_dir / "state.json").write_text(
        json.dumps(
            {
                "gross_equity_usd": 250_000.0,
                "net_equity_usd": 250_000.0,
                "sleeves": [
                    {"name": "spot_perp_paper", "category": "Market Neutral", "target_weight_pct": 30.0, "actual_weight_pct": 30.0, "drift_pct": 0.0, "allocation_usd": 75_000.0},
                    {"name": "hip3_paper", "category": "Market Neutral", "target_weight_pct": 21.0, "actual_weight_pct": 21.0, "drift_pct": 0.0, "allocation_usd": 52_500.0},
                ],
                "risk_telemetry": {
                    "gross_leverage": 0.72,
                    "leverage_30d_max": 0.85,
                    "max_weight_drift_pct": 6.4,
                    "rebalanced_this_tick": True,
                },
            }
        ),
        encoding="utf-8",
    )

    payload = pdash.collect_dashboard_payload(paper_dir)
    assert payload["autopilot"]["risk_telemetry"]["max_weight_drift_pct"] == 0.0
    assert payload["autopilot"]["risk_telemetry"]["pre_rebalance_max_drift_pct"] == 6.4

    # Direct metrics call on un-normalized legacy dict also emits 0
    raw_overview = {
        "autopilot": json.loads((ap_dir / "state.json").read_text(encoding="utf-8")),
    }
    prom_text = prom.render_prometheus_metrics(raw_overview)
    assert "hl_autopilot_max_drift_pct 0\n" in prom_text

    # Scheduler bootstrap detects degraded settlement_study (15/35 < 80%) and schema-stale attribution.json
    ran_cmds: list[list[str]] = []

    def fake_runner(cmd: list[str], _env: dict[str, str], _timeout: int) -> int:
        ran_cmds.append(cmd)
        return 0

    ws = sched.WorkerScheduler(root_dir=sched.ROOT_DIR, data_dir=tmp_path, runner=fake_runner)
    for j in ws.jobs:
        sentinel = ws._bootstrap_sentinel_for(j.name)
        if sentinel is not None:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("{}", encoding="utf-8")

    # Write degraded settlement meta (15/35) and legacy attribution without observed_at_utc
    (paper_dir / "hl_funding_settlement_study" / "meta.json").write_text(
        json.dumps({"coins_requested": 35, "coins_collected": 15, "degraded": True}),
        encoding="utf-8",
    )
    (paper_dir / "hl_testnet_attribution" / "attribution.json").write_text(
        json.dumps({"equity_usd": 980.0}),
        encoding="utf-8",
    )
    ws.jobs = [
        sched.JobSpec(
            name=j.name,
            description=j.description,
            cron_expr=j.cron_expr,
            minutes=j.minutes,
            hours=j.hours,
            timeout_s=j.timeout_s,
            readonly=j.readonly,
            bootstrap_eligible=j.bootstrap_eligible,
            build_command=j.build_command,
            is_enabled=lambda: (True, "enabled"),
            has_required_credentials=lambda: (True, "ok"),
        )
        for j in ws.jobs
        if j.name in ("settlement_study", "testnet_attribution", "hip3_paper")
    ]
    ws.run_bootstrap_readonly()
    joined = [" ".join(c) for c in ran_cmds]
    assert any("run_hl_funding_settlement_study.py" in s for s in joined)
    assert any("run_hl_testnet_attribution.py" in s and "--mm-audit-log" in s for s in joined)
    assert not any("hip3" in s for s in joined)


def test_intraday_inception_anchor_uses_four_hour_step_not_one_second() -> None:
    dates, gross, net = ar._with_inception_anchor(
        ["2026-11-27T02:00:00Z", "2026-11-27T06:00:00Z"],
        [0.9995, 1.0012],
        [0.9995, 1.0009],
    )
    assert dates[0] == "2026-11-26T22:00:00Z"
    assert gross[0] == 1.0
    assert net is not None and net[0] == 1.0


def test_filter_unquoted_skips_all_when_fewer_than_two_quoted_coins_without_outage() -> None:
    now = 1_795_800_000_000
    targets = {"BTC": 0.20, "JUP": -0.10, "WLFI": -0.10}
    bbo = {"BTC": (60_000.0, 60_002.0, now)}
    # All books were read (`observed_ms == now`), yet only 1 coin has a two-sided Testnet BBO:
    # skip new BBO entries (`{}`) without returning raw targets that would trip a false kill-switch.
    tops = {
        "BTC": (60_000.0, 60_002.0, now),
        "JUP": (0.25, None, now),
        "WLFI": (None, None, now),
    }
    filtered, skipped = hlex.filter_unquoted_targets(targets, bbo, max_missing_fraction=0.35, book_tops=tops)
    assert filtered == {}
    assert skipped == ["BTC", "JUP", "WLFI"]

    # If books failed to read (`observed_ms == 0`), still fail closed so check_kill_switch halts!
    tops_outage = {"BTC": (60_000.0, 60_002.0, now), "JUP": (None, None, 0), "WLFI": (None, None, 0)}
    out_f, out_s = hlex.filter_unquoted_targets(targets, bbo, max_missing_fraction=0.35, book_tops=tops_outage)
    assert out_f == targets
    assert out_s == []


def test_metrics_clears_pre_pr30_missing_quote_orphan_unwind_when_live_probe_is_healthy() -> None:
    t_audit = 1_795_824_616_226
    overview = {
        "attribution": {"equity_usd": 989.77, "liq_warnings": [], "adl_warnings": [], "observed_at_ms": t_audit + 3_000_000},
        "testnet_audit_recent": [
            {
                "ts_ms": t_audit,
                "mode": "testnet_live",
                "status": "kill_switch_triggered",
                "derisk_mode": "orphan_unwind_only",
                "violations": ["missing_quote:JUP", "missing_quote:WLFI"],
                "placed_count": 4,
                "high_water_mark_usd": 1006.55,
            }
        ],
        "testnet_risk": {
            "observed_at_ms": t_audit + 4_900_000,
            "equity_usd": 989.63,
            "drawdown_pct": 1.68,
            "kill_switch_drawdown_pct": 5.0,
            "unknown_books": 0,
            "unmanaged_orphan_positions": 0,
            "orphan_positions": {"VVV": {"szi": 1.71, "unmanaged": False}},
        },
    }
    txt = prom.render_prometheus_metrics(overview)
    assert "hl_testnet_last_cycle_ok 1\n" in txt
    assert 'hl_strategy_status_ok{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} 1\n' in txt

    # Account-risk drawdown violation NEVER clears even with a fresher probe!
    overview_dd = {
        **overview,
        "testnet_audit_recent": [
            {
                **overview["testnet_audit_recent"][0],
                "derisk_mode": "flatten",
                "violations": ["drawdown_limit:5.20%>=5.00%"],
            }
        ],
    }
    txt_dd = prom.render_prometheus_metrics(overview_dd)
    assert "hl_testnet_last_cycle_ok 0\n" in txt_dd
    assert 'hl_strategy_status_ok{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} 0\n' in txt_dd


def test_autopilot_route_and_mm_calibration_bootstrap_refresh(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from hl_quant.api.app import create_app

    paper_dir = tmp_path / "paper"
    ap_dir = paper_dir / "hl_autopilot"
    ap_dir.mkdir(parents=True)
    (ap_dir / "state.json").write_text(
        json.dumps(
            {
                "gross_equity_usd": 250_000.0,
                "sleeves": [{"name": "spot_perp_paper", "drift_pct": 0.0}],
                "risk_telemetry": {"max_weight_drift_pct": 6.4, "rebalanced_this_tick": True},
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=paper_dir))
    r = client.get("/api/v1/autopilot")
    assert r.status_code == 200
    assert r.json()["state"]["risk_telemetry"]["max_weight_drift_pct"] == 0.0

    # Scheduler bootstrap refreshes pre-PR29 mm_calibration lacking calibration_ok
    cal_file = paper_dir / "hl_mm_calibration" / "calibration.json"
    cal_file.parent.mkdir(parents=True, exist_ok=True)
    cal_file.write_text(json.dumps({"BTC": {"kappa_per_bp": 0.53}}), encoding="utf-8")
    ws = sched.WorkerScheduler(root_dir=sched.ROOT_DIR, data_dir=tmp_path, runner=lambda *_: 0)
    assert ws._bootstrap_needs_refresh("mm_calibration", cal_file) is True
    cal_file.write_text(json.dumps({"BTC": {"kappa_per_bp": 0.53, "calibration_ok": True}}), encoding="utf-8")
    assert ws._bootstrap_needs_refresh("mm_calibration", cal_file) is False
