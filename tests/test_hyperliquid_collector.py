from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path

import httpx
import pandas as pd
import pytest
from typer.testing import CliRunner

from hl_quant import hyperliquid as hl
from hl_quant import publication as pub

HOUR = 3_600_000
DAY = 86_400_000
NOW = 1_795_000_000_000 - (1_795_000_000_000 % HOUR)
LARGE_TID = (1 << 53) + 12345


def _client(handler, **kwargs) -> hl.HyperliquidInfoClient:
    kwargs.setdefault("base_url", "https://hl.test/info")
    kwargs.setdefault("sleep", lambda _s: None)
    kwargs.setdefault("rng", lambda: 0.0)
    return hl.HyperliquidInfoClient(
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def _load_cli_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "analytics" / "collect_hyperliquid_market_data.py"
    spec = importlib.util.spec_from_file_location("collect_hyperliquid_market_data", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------- client


def test_client_retries_rate_limit_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"ok": True})

    assert _client(handler).info({"type": "meta"}) == {"ok": True}
    assert len(calls) == 3


def test_client_honors_retry_after_and_request_delay():
    sleeps: list[float] = []
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, text="rate limited")
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, backoff_s=1.0, request_delay_s=0.25, sleep=sleeps.append, rng=lambda: 0.5)
    assert client.info({"type": "meta"}) == {"ok": True}
    assert client.info({"type": "meta"}) == {"ok": True}
    # First retry respects Retry-After=7s (> backoff 1.125s), second call paces with request_delay_s=0.25s.
    assert sleeps == [7.0, 0.25]


def test_client_does_not_retry_client_error():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(422, text="Failed to deserialize")

    with pytest.raises(hl.HyperliquidError, match="HTTP 422"):
        _client(handler).info({"type": "bogus"})
    assert len(calls) == 1


def test_client_gives_up_after_retry_budget():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(hl.HyperliquidError, match="3 attempts"):
        _client(handler, max_retries=2).info({"type": "meta"})


# ----------------------------------------------------------------- funding


def _funding_handler(times: list[int], coin: str = "BTC"):
    seen = []

    def handler(request):
        body = _body(request)
        seen.append(body)
        page = [t for t in times if body["startTime"] <= t <= body["endTime"]][: hl.FUNDING_PAGE_LIMIT]
        return httpx.Response(
            200,
            json=[{"coin": coin, "fundingRate": "0.0000125", "premium": "-0.0003", "time": t} for t in page],
        )

    return handler, seen


def test_funding_paginates_and_dedupes():
    start = NOW - 1200 * HOUR
    times = [start + i * HOUR for i in range(1200)]
    handler, seen = _funding_handler(times)
    collected = hl.collect_funding(_client(handler), "BTC", start, NOW, now_ms=NOW)
    assert len(seen) == 3
    assert collected.rows["time_ms"].tolist() == times
    assert collected.status == hl.STATUS_COMPLETE
    assert collected.notes["first_time_ms"] == start
    assert (collected.rows["available_at"] == collected.rows["time"]).all()


def test_funding_marks_partial_history_when_start_missing():
    start = NOW - 10 * HOUR
    times = [NOW - 3 * HOUR, NOW - 2 * HOUR, NOW - HOUR]
    handler, _ = _funding_handler(times)
    collected = hl.collect_funding(_client(handler), "BTC", start, NOW, now_ms=NOW)
    assert collected.status == hl.STATUS_PARTIAL_HISTORY
    assert collected.notes["first_time_ms"] == NOW - 3 * HOUR
    assert len(collected.rows) == 3


def test_funding_empty_is_explicit():
    handler, _ = _funding_handler([])
    collected = hl.collect_funding(_client(handler), "BTC", NOW - HOUR, NOW, now_ms=NOW)
    assert collected.status == hl.STATUS_EMPTY
    assert collected.notes["first_time_ms"] is None
    assert collected.rows.empty


def test_funding_rejects_future_window_and_wrong_coin():
    handler, _ = _funding_handler([NOW - HOUR], coin="ETH")
    with pytest.raises(hl.HyperliquidError, match="future"):
        hl.collect_funding(_client(handler), "BTC", NOW - HOUR, NOW + HOUR, now_ms=NOW)
    with pytest.raises(hl.HyperliquidError, match="coin"):
        hl.collect_funding(_client(handler), "BTC", NOW - 2 * HOUR, NOW, now_ms=NOW)


# ----------------------------------------------------------------- candles


def _candle(t: int, coin="BTC", interval="1h"):
    return {"t": t, "T": t + HOUR - 1, "s": coin, "i": interval, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "n": 3}


def _candle_handler(available_from: int):
    def handler(request):
        req = _body(request)["req"]
        first = max(req["startTime"], available_from)
        first += (-first) % HOUR
        opens = list(range(first, req["endTime"] + 1, HOUR))[: hl.CANDLE_PAGE_LIMIT]
        return httpx.Response(200, json=[_candle(t) for t in opens])

    return handler


def test_candles_drop_unclosed_and_set_available_at():
    observed = NOW + HOUR // 2  # last candle (open at NOW) is still running
    collected = hl.collect_candles(
        _client(_candle_handler(0)), "BTC", "1h", NOW - 3 * HOUR, NOW, now_ms=observed
    )
    assert collected.rows["open_time_ms"].tolist() == [NOW - 3 * HOUR, NOW - 2 * HOUR, NOW - HOUR]
    assert collected.notes["dropped_unclosed"] == 1
    assert (collected.rows["available_at"] > collected.rows["open_time"]).all()
    assert collected.status == hl.STATUS_COMPLETE


def test_candles_mark_partial_history_without_padding():
    collected = hl.collect_candles(
        _client(_candle_handler(NOW - 5 * HOUR)), "BTC", "1h", NOW - 100 * HOUR, NOW - HOUR, now_ms=NOW
    )
    assert collected.status == hl.STATUS_PARTIAL_HISTORY
    assert collected.notes["first_open_ms"] == NOW - 5 * HOUR
    assert len(collected.rows) == 5


def test_candles_paginate_past_page_limit():
    start = NOW - 6000 * HOUR
    collected = hl.collect_candles(_client(_candle_handler(0)), "BTC", "1h", start, NOW - HOUR, now_ms=NOW)
    assert collected.notes["page_count"] == 2
    assert len(collected.rows) == 6000
    assert collected.rows["open_time_ms"].is_monotonic_increasing


def test_candles_reject_unknown_interval():
    with pytest.raises(hl.HyperliquidError, match="interval"):
        hl.collect_candles(_client(_candle_handler(0)), "BTC", "7m", NOW - HOUR, NOW, now_ms=NOW)


# ----------------------------------------------------------------- snapshot


def _snapshot_handler(request, *, trade_side: str = "B", l2_coin_override: str | None = None):
    body = _body(request)
    kind = body["type"]
    if kind == "metaAndAssetCtxs":
        return httpx.Response(
            200,
            json=[
                {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}, {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]},
                [
                    {"funding": "0.00001", "openInterest": "100", "prevDayPx": "1", "dayNtlVlm": "5", "premium": "0", "oraclePx": "10", "markPx": "10.1", "midPx": "10.05", "impactPxs": ["10", "10.2"]},
                    {"funding": "0.00002", "openInterest": "50", "prevDayPx": "1", "dayNtlVlm": "5", "premium": "0", "oraclePx": "2", "markPx": "2", "midPx": "2", "impactPxs": None},
                ],
            ],
        )
    if kind == "predictedFundings":
        return httpx.Response(
            200,
            json=[
                ["BTC", [["HlPerp", {"fundingRate": "0.0000125", "nextFundingTime": NOW, "fundingIntervalHours": 1}], ["BinPerp", None]]],
                ["ETH", [["BybitPerp", {"fundingRate": "0.00005", "nextFundingTime": NOW, "fundingIntervalHours": 4}]]],
            ],
        )
    if kind == "l2Book":
        return httpx.Response(
            200,
            json={
                "coin": l2_coin_override or body["coin"],
                "time": NOW,
                "levels": [[{"px": "10", "sz": "1", "n": 2}], [{"px": "10.1", "sz": "3", "n": 1}]],
            },
        )
    if kind == "recentTrades":
        return httpx.Response(
            200,
            json=[
                {"coin": body["coin"], "side": trade_side, "px": "10", "sz": "0.5", "time": NOW - 5, "hash": "0xabc", "tid": LARGE_TID, "users": ["0x1", "0x2"]},
                {"coin": body["coin"], "side": "A", "px": "10.1", "sz": "0.2", "time": NOW - 2, "hash": "0xdef", "tid": LARGE_TID + 1, "users": ["0x3", "0x4"]},
            ],
        )
    return httpx.Response(422)


def test_snapshot_writes_typed_tables_strips_addresses_and_preserves_int64_tid(tmp_path):
    clock = iter(range(NOW, NOW + 100))
    collected = hl.collect_snapshot(_client(_snapshot_handler), ["BTC"], now_ms=lambda: next(clock))
    counts = collected.notes["record_counts"]
    assert counts == {"asset_ctx": 1, "predicted_funding": 1, "l2": 2, "trades": 2}
    assert "0x1" not in json.dumps(collected.pages)
    assert set(collected.tables) == {"asset_ctx", "predicted_funding", "l2", "trades"}
    assert "users" not in collected.tables["trades"].columns
    assert collected.tables["asset_ctx"].iloc[0]["impact_ask_px"] == pytest.approx(10.2)
    assert collected.tables["trades"]["side"].tolist() == ["buy", "sell"]
    assert collected.tables["trades"]["tid"].dtype == "int64"
    assert collected.tables["trades"]["tid"].tolist() == [LARGE_TID, LARGE_TID + 1]

    res = hl.publish(collected, tmp_path, base_url="https://hl.test/info")
    assert res.row_count == 6
    manifest = hl.verify_run(res.run_dir)
    assert {"asset_ctx.parquet", "predicted_funding.parquet", "l2.parquet", "trades.parquet"} <= set(manifest["files"])
    reloaded_trades = pd.read_parquet(res.run_dir / "trades.parquet")
    assert reloaded_trades["tid"].tolist() == [LARGE_TID, LARGE_TID + 1]


def test_snapshot_rejects_unknown_coin_side_and_mismatched_l2_coin():
    with pytest.raises(hl.HyperliquidError, match="universe"):
        hl.collect_snapshot(_client(_snapshot_handler), ["DOGE"])
    with pytest.raises(hl.HyperliquidError, match="unexpected side"):
        hl.collect_snapshot(_client(lambda req: _snapshot_handler(req, trade_side="X")), ["BTC"])
    with pytest.raises(hl.HyperliquidError, match="l2Book returned coin"):
        hl.collect_snapshot(_client(lambda req: _snapshot_handler(req, l2_coin_override="ETH")), ["BTC"])


# ----------------------------------------------------------------- publication


def _published(tmp_path: Path, *, base_url: str = "https://hl.test/info") -> tuple[hl.Collected, hl.PublishResult]:
    handler, _ = _funding_handler([NOW - 2 * HOUR, NOW - HOUR])
    collected = hl.collect_funding(_client(handler, base_url=base_url), "BTC", NOW - 2 * HOUR, NOW, now_ms=NOW)
    return collected, hl.publish(collected, tmp_path, base_url=base_url)


def test_publish_writes_verified_manifest_and_is_idempotent(tmp_path):
    collected, first = _published(tmp_path)
    assert not first.reused and first.row_count == 2
    manifest = hl.verify_run(first.run_dir)
    assert manifest["status"] == hl.STATUS_COMPLETE
    assert manifest["research_only"] is True
    assert manifest["provider"] == "hyperliquid"
    assert set(manifest["files"]) == {"raw/page-0000.json", "rows.parquet"}
    assert pd.read_parquet(first.run_dir / "rows.parquet")["time_ms"].tolist() == [NOW - 2 * HOUR, NOW - HOUR]
    assert not list(tmp_path.glob("funding/.staging-*"))

    again = hl.publish(collected, tmp_path, base_url="https://hl.test/info")
    assert again.reused and again.run_dir == first.run_dir


def test_mainnet_and_testnet_do_not_collide_or_reuse_each_other(tmp_path):
    _, testnet_res = _published(tmp_path, base_url=hl.TESTNET_INFO_URL)
    _, mainnet_res = _published(tmp_path, base_url=hl.MAINNET_INFO_URL)
    assert testnet_res.run_id != mainnet_res.run_id
    assert testnet_res.run_dir != mainnet_res.run_dir
    assert not mainnet_res.reused
    assert hl.verify_run(testnet_res.run_dir)["endpoint"] == hl.TESTNET_INFO_URL
    assert hl.verify_run(mainnet_res.run_dir)["endpoint"] == hl.MAINNET_INFO_URL


def test_publish_supports_custom_provider_and_rejects_tampering(tmp_path):
    collected, first = _published(tmp_path)
    binance_collected = pub.Collected(
        dataset=collected.dataset,
        params=collected.params,
        pages=collected.pages,
        rows=collected.rows,
        status=collected.status,
        observed_at_ms=collected.observed_at_ms,
        notes=collected.notes,
        provider="binance",
        endpoint="https://fapi.binance.com",
    )
    binance_res = pub.publish(binance_collected, tmp_path, base_url="https://fapi.binance.com")
    assert binance_res.run_id != first.run_id
    assert pub.verify_run(binance_res.run_dir)["provider"] == "binance"

    # Tamper rows.parquet
    (first.run_dir / "rows.parquet").write_bytes(b"corrupt")
    with pytest.raises(hl.ManifestCorruptError, match="hash mismatch"):
        hl.publish(collected, tmp_path, base_url="https://hl.test/info")

    # Corrupt manifest JSON
    (binance_res.run_dir / "manifest.json").write_text("{not-json")
    with pytest.raises(hl.ManifestCorruptError, match="unreadable manifest"):
        pub.verify_run(binance_res.run_dir)


def test_publish_fails_closed_on_tampered_raw_page(tmp_path):
    collected, first = _published(tmp_path)
    (first.run_dir / "raw" / "page-0000.json").write_text("[]")
    with pytest.raises(hl.ManifestCorruptError, match="hash mismatch"):
        hl.publish(collected, tmp_path, base_url="https://hl.test/info")


def test_publish_cleans_staging_on_failure(tmp_path, monkeypatch):
    handler, _ = _funding_handler([NOW - HOUR])
    collected = hl.collect_funding(_client(handler), "BTC", NOW - HOUR, NOW, now_ms=NOW)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", boom)
    with pytest.raises(OSError):
        hl.publish(collected, tmp_path, base_url="https://hl.test/info")
    assert not any((tmp_path / "funding").iterdir())


def test_run_id_is_deterministic_for_history():
    handler, _ = _funding_handler([])
    a = hl.collect_funding(_client(handler), "BTC", NOW - HOUR, NOW, now_ms=NOW)
    b = hl.collect_funding(_client(handler), "BTC", NOW - HOUR, NOW, now_ms=NOW + 999)
    assert hl.run_id_for(a) == hl.run_id_for(b)


# ----------------------------------------------------------------- CLI


def test_cli_window_clamping_funding_and_weekly_candles():
    cli = _load_cli_module()
    # 2026-11-25 is Wednesday; 14:25 UTC -> funding settlement at 14:00:00.000 is included.
    wed_1425 = int(datetime(2026, 11, 25, 14, 25, tzinfo=timezone.utc).timestamp() * 1000)
    wed_1400 = int(datetime(2026, 11, 25, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert cli._end_ms_funding("2026-11-25", wed_1425) == wed_1400
    assert cli._end_ms_candles("2026-11-25", wed_1425, "1h") == wed_1400 - 1

    # Weekly candle boundary must align to Monday 2026-11-23 00:00 UTC - 1ms (Sunday 23:59:59.999).
    mon_0000 = int(datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert cli._end_ms_candles("2026-11-25", wed_1425, "1w") == mon_0000 - 1


def test_cli_reuses_existing_run_without_http_requests(tmp_path, monkeypatch):
    cli = _load_cli_module()
    fixed_now = int(datetime(2026, 11, 25, 14, 25, tzinfo=timezone.utc).timestamp() * 1000)
    start_ms = int(datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime(2026, 11, 25, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
    handler, _ = _funding_handler([start_ms, start_ms + HOUR, end_ms])
    collected = hl.collect_funding(
        _client(handler, base_url=hl.MAINNET_INFO_URL), "BTC", start_ms, end_ms, now_ms=fixed_now
    )
    hl.publish(collected, tmp_path, base_url=hl.MAINNET_INFO_URL)

    def fail_network(*_args, **_kwargs):
        raise AssertionError("CLI should reuse verified run without calling Hyperliquid API")

    monkeypatch.setattr(hl, "current_time_ms", lambda: fixed_now)
    monkeypatch.setattr(hl, "collect_funding", fail_network)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "funding",
            "--coins",
            "BTC",
            "--start",
            "2026-11-25T12:00:00Z",
            "--end",
            "2026-11-25",
            "--output-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["reused"] is True
    assert payload["rows"] == 3
