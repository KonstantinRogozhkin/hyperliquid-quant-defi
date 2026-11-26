from __future__ import annotations

import json
from pathlib import Path

import pytest

from hl_quant import hl_tear_sheet as ts
from hl_quant import paper_tracker as pt


def test_alo_fill_rate_throttle_scales_down_xs_rev_below_85_pct():
    sleeves = pt.default_frozen_metamodel()
    # Above 85% -> untouched
    s_ok = pt.apply_alo_fill_rate_throttle(sleeves, 92.0)
    assert [s.weight for s in s_ok] == [s.weight for s in sleeves]

    # At 77.5% (halfway between 85% and 70%) -> xs_rev/xs_mom scaled by 0.5 and renormalized -> carry weight grows
    s_mid = pt.apply_alo_fill_rate_throttle(sleeves, 77.5)
    by_name_mid = {s.spec.name: s.weight for s in s_mid}
    assert sum(by_name_mid.values()) == pytest.approx(1.0, abs=1e-4)
    assert by_name_mid["carry_best_cex_L168_in5"] > 0.50

    # At 65% (<= 70% halt) -> high-turnover price reversal sleeves fully throttled out, 100% carry!
    s_low = pt.apply_alo_fill_rate_throttle(sleeves, 65.0)
    assert len(s_low) == 1
    assert s_low[0].spec.name == "carry_best_cex_L168_in5"
    assert s_low[0].weight == pytest.approx(1.0)


def test_tear_sheet_collection_and_rendering(tmp_path: Path):
    (tmp_path / "hl_testnet_attribution").mkdir(parents=True)
    (tmp_path / "hl_testnet_attribution" / "attribution.json").write_text(
        json.dumps(
            {
                "equity_usd": 998.82,
                "total_fills": 30,
                "total_volume_usd": 490.86,
                "maker_volume_share_pct": 100.0,
                "effective_fee_bps": 1.5,
                "real_alo_fill_rate_pct": 92.3,
                "audit_orders_filled": 12,
                "audit_orders_placed": 13,
                "vw_markout_bps": -0.7,
                "liq_warnings": [],
            }
        )
    )
    data = ts.collect_tear_sheet_data(tmp_path)
    assert data["latency_bench"]["p50_us"] > 0
    md = ts.format_tear_sheet_markdown(data)
    rich = ts.format_tear_sheet_telegram_rich_html(data)
    assert "Executive Trading Tear-Sheet" in md
    assert "92.3%" in md
    assert '<table bordered striped>' in rich
