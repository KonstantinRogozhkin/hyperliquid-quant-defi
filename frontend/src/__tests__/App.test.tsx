import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App, { FETCH_TIMEOUT_MS, fmtPct, fmtUsd, fx, parseUtcTs, signCls, workerLiveness } from "../App";
import { signCls as riskSignCls } from "../components/AnalyticsRiskTab";
import { ErrorBoundary } from "../components/ErrorBoundary";
import type { BuildInfo, OverviewPayload } from "../types";

const BASE_SHA = "3b4d8f21aa5fa13082fec5635eedd0252989f3a1";

function makeOverview(overrides: Partial<OverviewPayload> = {}): OverviewPayload {
  return {
    generated_at_utc: "2026-11-26T22:00:00Z",
    build_info: {
      service: "hl-backend",
      sha: BASE_SHA,
      built_at: "2026-11-26T21:55:00Z",
    },
    worker_status: {
      service: "hl-worker",
      build_sha: BASE_SHA,
      started_at_utc: "2026-11-26T20:00:00Z",
      heartbeat_utc: "2026-11-26T21:59:45Z",
      telegram_enabled: true,
      testnet_execution_enabled: false,
      jobs: [
        {
          name: "snapshot",
          description: "Hourly Hyperliquid funding snapshot",
          cron_expr: "50 * * * *",
          readonly: true,
          enabled: true,
          status_reason: "active",
          state: "ok",
          last_started_at_utc: "2026-11-26T21:50:00Z",
          last_finished_at_utc: "2026-11-26T21:50:12Z",
          last_exit_code: 0,
          last_duration_s: 12.4,
          runs_ok: 6,
          runs_failed: 0,
          next_run_utc: "2026-11-26T22:50:00Z",
        },
        {
          name: "testnet_executor",
          description: "Testnet ALO rebalancer",
          cron_expr: "10 */4 * * *",
          readonly: false,
          enabled: false,
          status_reason: "disabled (HL_ENABLE_TESTNET_EXECUTION=false)",
          state: "disabled",
          last_started_at_utc: null,
          last_finished_at_utc: null,
          last_exit_code: null,
          last_duration_s: null,
          runs_ok: 0,
          runs_failed: 0,
          next_run_utc: "2026-11-27T00:10:00Z",
        },
      ],
    },
    artifact_freshness: {
      metamodel_state: {
        exists: true,
        path: "hl_metamodel/state.json",
        updated_at_utc: "2026-11-26T20:05:10Z",
        age_minutes: 114.8,
      },
      testnet_audit: {
        exists: true,
        path: "hl_testnet_audit.jsonl",
        updated_at_utc: "2026-11-26T20:10:15Z",
        age_minutes: 109.7,
      },
    },
    paper: {
      ticks: 48,
      starting_capital_usd: 10000,
      equity_usd: 10245.5,
      return_pct: 2.455,
      active_positions: 2,
    },
    paper_ticks: [
      {
        tick_hour_ms: 1795636800000,
        equity_usd: 10000,
        net_pnl_usd: 0,
        fund_pnl_usd: 0,
        cost_pnl_usd: 0,
        turnover: 0.4,
      },
      {
        tick_hour_ms: 1795651200000,
        equity_usd: 10245.5,
        net_pnl_usd: 245.5,
        fund_pnl_usd: 260.0,
        cost_pnl_usd: -14.5,
        turnover: 0.1,
      },
    ],
    paper_positions: [
      {
        coin: "BTC",
        hl_weight: -0.18,
        cex_weight: 0.18,
        cex_venue: "bybit",
        last_hl_px: 91250.25,
        l2_spread_bps: 0.8,
        cum_pnl_usd: 42.15,
      },
      {
        coin: "SOL",
        hl_weight: 0.12,
        cex_weight: 0,
        cex_venue: "",
        last_hl_px: 214.5,
        l2_spread_bps: 1.9,
        cum_pnl_usd: -5.4,
      },
    ],
    settlement_meta: {
      span_days: 90,
      total_settlement_events: 12450,
      coins_collected: 30,
    },
    settlement_stats: [
      {
        strategy: "fade_post_00",
        bucket: "extreme_pos",
        split: "OOS",
        events: 310,
        mean_gross_bps: 6.4,
        t_stat: 3.85,
        win_rate_pct: 61.2,
        net_maker_bps: 3.4,
        net_taker_bps: -2.6,
      },
      {
        strategy: "fade_post_00",
        bucket: "extreme_pos",
        split: "IS",
        events: 620,
        mean_gross_bps: 7.1,
        t_stat: 4.9,
        win_rate_pct: 63.0,
        net_maker_bps: 4.1,
        net_taker_bps: -1.9,
      },
    ],
    frontier_history: {
      observed_at_utc: "2026-11-26T20:20:00Z",
      ny_session: "RTH",
      hlp_apr_pct: 14.25,
      total_logged: { hip3: 120 },
      hip3_persistence: [
        {
          base_symbol: "AAPL",
          short_symbol: "xyz:AAPL",
          long_symbol: "flx:AAPL",
          ticks_seen: 12,
          active_in_latest_tick: true,
          ticks_since_last_seen: 0,
          raw_apr_streak: 6,
          clean_streak: 5,
          direction_flips: 0,
          latest_apr_pct: 48.2,
          median_apr_pct: 41.5,
          median_oracle_div_bps: 6.4,
          both_growth_mode: true,
          any_only_isolated: false,
        },
        {
          base_symbol: "NVDA",
          short_symbol: "xyz:NVDA",
          long_symbol: "vntl:NVDA",
          ticks_seen: 9,
          active_in_latest_tick: true,
          ticks_since_last_seen: 0,
          raw_apr_streak: 3,
          clean_streak: 1,
          direction_flips: 2,
          latest_apr_pct: 32.0,
          median_apr_pct: 29.1,
          median_oracle_div_bps: 11.2,
          both_growth_mode: false,
          any_only_isolated: false,
        },
        {
          base_symbol: "TSLA",
          short_symbol: "xyz:TSLA",
          long_symbol: "flx:TSLA",
          ticks_seen: 5,
          active_in_latest_tick: false,
          ticks_since_last_seen: 3,
          raw_apr_streak: 0,
          clean_streak: 0,
          direction_flips: 0,
          latest_apr_pct: 25.0,
          median_apr_pct: 24.0,
          median_oracle_div_bps: 8.0,
          both_growth_mode: true,
          any_only_isolated: false,
        },
      ],
    },
    boros: {
      active_markets_count: 10,
      hl_and_spread_count: 2,
      markets: [
        {
          market_id: 1,
          name: "ETH-HL",
          symbol: "ETH-HL-MAR",
          underlying: "ETH",
          is_spread_market: false,
          days_to_maturity: 42,
          best_bid_apr_pct: 12.5,
          best_ask_apr_pct: 13.0,
          floating_apr_pct: 9.2,
          fixed_minus_float_bid_pct: 3.3,
          recommended_action: "SHORT_FIXED",
        },
      ],
    },
    yield_stack: {
      strategies: [
        {
          name: "kHYPE + Perp Short",
          rate_type: "Floating",
          gross_stacked_apr_pct: 19.5,
          capital_efficiency_pct: 85.0,
          net_deposit_apr_pct: 16.58,
          spread_vs_hlp_pct: 2.33,
          perp_leverage: 2,
          liq_buffer_pct: 42,
          key_risks: "LST depeg",
        },
      ],
    },
    cross_dex_paper: {
      starting_capital_usd: 10000,
      equity_usd: 10112.4,
      cum_funding_usd: 128.9,
      cum_basis_mtm_usd: 4.5,
      cum_costs_usd: 21.0,
      positions: {
        ETH: {
          coin: "ETH",
          direction: "SHORT_HL_LONG_LIGHTER",
          leg_notional_usd: 2000,
          hours_held: 14.5,
          entry_spread_apr_pct: 38.0,
          live_spread_apr_pct: 31.2,
          entry_l2_slip_bps: 1.5,
          entry_exec_basis_bps: 2.2,
        },
      },
      closed_pairs_count: 3,
      payback_achieved_count: 2,
    },
    cross_dex_all: [
      {
        coin: "HYPE",
        other_dex: "lighter",
        direction: "SHORT_HL_LONG_OTHER",
        hl_apr_pct: 45.0,
        other_apr_pct: 8.0,
        spread_apr_pct: 37.0,
        favorable_basis_bps: 3.2,
        breakeven_hours: 18.4,
      },
      {
        coin: "ARB",
        other_dex: "extended",
        direction: "LONG_HL_SHORT_OTHER",
        hl_apr_pct: 4.0,
        other_apr_pct: 22.0,
        spread_apr_pct: 18.0,
        favorable_basis_bps: 1.1,
        breakeven_hours: 42.0,
      },
    ],
    onchain_events: {
      observed_at_utc: "2026-11-26T21:45:00Z",
      hlp_tvl_usd: 412_500_000,
      hlp_mm_pnl: {
        pnl_source: "deposit_clean_vault_delta",
        hlp_pnl_delta_usd: 185_200,
        mm_pnl_regime: "CALM",
      },
      any_alert: true,
      active_alert_keys: ["H2_BTC_LIQ_WALL"],
    },
    liq_funding: {
      whales_scanned: 50,
      whales_with_positions: 34,
      clusters: [
        {
          coin: "BTC",
          mark_px: 91250,
          total_0_10_usd: 18_500_000,
          nearest_long_dist_pct: 3.4,
          nearest_short_dist_pct: 6.2,
          cascade_bias: "LONG_SQUEEZE",
        },
      ],
      funding_micro: [],
    },
    hyperevm: {
      chain: {
        block_number: 1842310,
        usdt0_peg_discount_bps: 1.25,
      },
      l1_read_oracles: [
        {
          coin: "HYPE",
          l1read_mark_px: 26.42,
          rest_mark_px: 26.41,
          drift_bps: 3.78,
        },
      ],
    },
    attribution: {
      equity_usd: 1018.45,
      open_positions_count: 7,
      total_fills: 41,
      maker_volume_share_pct: 96.5,
      real_alo_fill_rate_pct: 82.0,
      effective_fee_bps: 1.5,
      net_attributed_pnl_usd: 18.45,
      vw_markout_bps: 2.1,
      api_quota_health: "HEALTHY",
      min_liq_distance_pct: 18.4,
      total_volume_usd: 15240.0,
      per_coin: [
        {
          coin: "BTC",
          position_sz: -0.004,
          position_usd: 365.0,
          entry_px: 91200,
          current_mid: 91250,
          leverage_mode: "cross:3x",
          liq_px: 108000,
          liq_distance_pct: 18.4,
          fills_count: 14,
          fill_volume_usd: 6400,
          maker_share_pct: 100,
          net_pnl_usd: 9.25,
          vw_markout_bps: 1.8,
          adl_rank_score: 2.35,
        },
        {
          coin: "ETH",
          position_sz: 0.12,
          position_usd: 390.0,
          entry_px: 3250,
          current_mid: 3255,
          leverage_mode: "cross:3x",
          liq_px: 2100,
          liq_distance_pct: 35.5,
          fills_count: 9,
          fill_volume_usd: 4100,
          maker_share_pct: 92,
          net_pnl_usd: -2.1,
          vw_markout_bps: -0.4,
          adl_rank_score: 0.85,
        },
      ],
    },
    testnet_audit_recent: [
      {
        ts_ms: 1795651800000,
        observed_at_utc: "2026-11-26T20:10:00Z",
        status: "ok",
        violations: [],
        cancelled_count: 3,
        placed_count: 7,
        total_notional_usd: 380.34,
        high_water_mark_usd: 1020.0,
        blocked_isolated_coins: [],
      },
      {
        ts_ms: 1795637400000,
        observed_at_utc: "2026-11-26T16:10:00Z",
        status: "halted",
        violations: ["drawdown_kill_switch"],
        cancelled_count: 7,
        placed_count: 0,
        total_notional_usd: 0,
        high_water_mark_usd: 1020.0,
        blocked_isolated_coins: ["MEME"],
      },
    ],
    latency_bench: {
      iterations: 10000,
      p50_us: 14.2,
      p95_us: 21.0,
      p99_us: 28.7,
      throughput_ticks_per_sec: 68400,
    },
    mm_calibration: {
      BTC: {
        coin: "BTC",
        trades_count: 250,
        empirical_sigma_tau_bps: 2.8,
        kappa_per_bp: 0.42,
        arrival_a_per_sec: 1.5,
        r2_fit: 0.915,
        implied_min_half_spread_bps: 2.38,
        depth_curve: [
          { delta_bps: 1, lambda_per_sec: 1.1, fitted_lambda: 1.08 },
          { delta_bps: 3, lambda_per_sec: 0.45, fitted_lambda: 0.44 },
        ],
      },
    },
    ...overrides,
  };
}

function mockFetch(overview: OverviewPayload, feBuild?: BuildInfo) {
  const fe: BuildInfo = feBuild ?? {
    service: "hl-frontend",
    sha: BASE_SHA,
    built_at: "2026-11-26T21:55:00Z",
  };
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    if (url.includes("/api/v1/overview")) {
      return new Response(JSON.stringify(overview), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/build-info.json")) {
      return new Response(JSON.stringify(fe), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/api/v1/analytics")) {
      return new Response(
        JSON.stringify({
          generated_at_utc: "2026-11-26T22:00:00Z",
          leaderboard: [],
          curves: {},
          correlation: { ids: [], matrix: [], overlap_days: 0 },
          market_exposure: {},
          risk_exposure: {
            leverage_by_strategy: [],
            venue_concentration: [],
            asset_tier_concentration: [],
            total_nav_guard_triggers: 0,
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response("Not found", { status: 404 });
  });
}

describe("Formatting helpers", () => {
  it("formats signed USD values cleanly without $-5.40 or —h artifacts", () => {
    expect(fmtUsd(-5.4)).toBe("-$5.40");
    expect(fmtUsd(-1_250_000)).toBe("-$1.25M");
    expect(fmtUsd(-1_100_000_000)).toBe("-$1.10B");
    expect(fmtUsd(-0.0001, 2)).toBe("$0.00");
    expect(fmtUsd(null)).toBe("—");
    expect(fmtUsd(Number.NaN)).toBe("—");
    expect(fx(null, 1, "h")).toBe("—");
    expect(fx(undefined, 1, "ч")).toBe("—");
    expect(fx(18.4, 1, "h")).toBe("18.4h");
  });
});

describe("App", () => {
  it("renders overview telemetry, FE/BE SHA badge, -$5.40 signed PnL, and filters Metamodel Open Positions", async () => {
    mockFetch(makeOverview());
    const user = userEvent.setup();

    render(<App />);

    expect(await screen.findByText("Hyperliquid Quant DeFi · Live Desk")).toBeInTheDocument();
    expect(screen.getByText("⚡ On-Chain Alerts Active")).toBeInTheDocument();
    const shaPill = screen.getByText("FE 3b4d8f21 · BE 3b4d8f21");
    expect(shaPill).toHaveClass("pill-info");

    // Metamodel positions table shows both BTC and SOL (with -$5.40, never $-5.40)
    const posSection = screen.getByText("Metamodel Open Positions (Live Paper Ledger)").closest("section")!;
    expect(within(posSection).getByText("BTC")).toBeInTheDocument();
    expect(within(posSection).getByText("SOL")).toBeInTheDocument();
    expect(within(posSection).getByText("-$5.40")).toBeInTheDocument();
    expect(within(posSection).getByText("bybit")).toBeInTheDocument();
    expect(within(posSection).getByText("HL-only")).toBeInTheDocument();

    // Filter by ticker
    const search = screen.getByPlaceholderText(/Filter coin/i);
    await user.type(search, "btc");
    expect(within(posSection).getByText("BTC")).toBeInTheDocument();
    expect(within(posSection).queryByText("SOL")).not.toBeInTheDocument();

    // Settlement split selector switches between OOS (310 events) and IS (620 events)
    const settleSection = screen.getByText(":00 Funding Settlement OOS Study").closest("section")!;
    expect(within(settleSection).getByText("310")).toBeInTheDocument();
    expect(within(settleSection).getByText("OOS")).toHaveClass("badge-ok");
    expect(within(settleSection).queryByText("620")).not.toBeInTheDocument();
    await user.selectOptions(within(settleSection).getByRole("combobox"), "IS");
    expect(within(settleSection).getByText("620")).toBeInTheDocument();
    expect(within(settleSection).getByText("IS")).toHaveClass("badge-cyan");
  });

  it("flags FE/BE SHA mismatch with warning pill including when BE is dev and FE is pinned", async () => {
    mockFetch(makeOverview({ build_info: { service: "hl-backend", sha: "dev", built_at: "" } }), {
      service: "hl-frontend",
      sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      built_at: "2026-11-26T21:00:00Z",
    });

    render(<App />);

    const shaPill = await screen.findByText("FE aaaaaaaa · BE dev");
    expect(shaPill).toHaveClass("pill-warn");
  });

  it("filters HIP-3 persistence rows by Active only, Only steady (0 flips), and ticker search", async () => {
    mockFetch(
      makeOverview({
        hip3_paper: {
          starting_capital_usd: 50000,
          equity_usd: 50042.5,
          return_pct: 0.085,
          margin_locked_usd: 7500,
          margin_utilization_pct: 15.0,
          cum_funding_usd: 48.2,
          cum_basis_mtm_usd: 1.8,
          cum_costs_usd: 7.5,
          observed_at_utc: "2026-11-27 12:22 UTC",
          ny_session: "US_PREMARKET",
          closed_pairs_count: 1,
          payback_achieved_count: 1,
          positions: {
            MU: {
              base_symbol: "MU",
              short_symbol: "io:MU",
              long_symbol: "xyz:MU",
              short_dex: "io",
              long_dex: "xyz",
              both_growth_mode: true,
              any_only_isolated: true,
              margin_mode: "isolated",
              leg_notional_usd: 2500,
              margin_locked_usd: 2500,
              hours_held: 8.0,
              entry_spread_apr_pct: 80.0,
              live_spread_apr_pct: 106.5,
              live_oracle_div_bps: 4.2,
              entry_l2_slip_bps: 3.1,
              entry_exec_basis_bps: 12.4,
              cum_funding_usd: 4.5,
              cum_basis_mtm_usd: 1.2,
              cum_cost_usd: 1.2,
              net_pnl_usd: 4.5,
              clean_streak: 4,
            },
          },
        },
      }),
    );
    const user = userEvent.setup();

    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    await user.click(screen.getByRole("tab", { name: "HIP-3 · Boros · Yield" }));

    const hip3PaperSection = screen.getByText("HIP-3 Split-Margin Paper Ledger").closest("section")!;
    expect(within(hip3PaperSection).getByText("$50,042.50")).toBeInTheDocument();
    expect(within(hip3PaperSection).getByText(/Fees -\$7\.50/)).toBeInTheDocument();
    expect(within(hip3PaperSection).getByText("MU")).toBeInTheDocument();
    expect(within(hip3PaperSection).getByText("Isolated")).toHaveClass("badge-warn");

    const hip3Section = screen.getByText("HIP-3 Cross-Builder Spread Persistence").closest("section")!;
    expect(within(hip3Section).getByText("AAPL")).toBeInTheDocument();
    expect(within(hip3Section).getByText("NVDA")).toBeInTheDocument();
    expect(within(hip3Section).queryByText("TSLA")).not.toBeInTheDocument();

    await user.click(screen.getByLabelText("Active only"));
    expect(within(hip3Section).getByText("TSLA")).toBeInTheDocument();
    expect(within(hip3Section).getByText("Stale (-3t)")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Only steady (0 flips)"));
    expect(within(hip3Section).getByText("AAPL")).toBeInTheDocument();
    expect(within(hip3Section).queryByText("NVDA")).not.toBeInTheDocument();
    expect(within(hip3Section).queryByText("TSLA")).not.toBeInTheDocument();
  });

  it("renders Testnet & MM flat positions as Flat (never L $0.00), 0-fill Maker as —, 15m risk KPIs, and calibration warning", async () => {
    const base = makeOverview();
    mockFetch(
      makeOverview({
        attribution: {
          ...base.attribution,
          mixed_account_warning: "22% fill volume is from non-audited MM orders",
          per_coin: [
            ...(base.attribution?.per_coin as unknown[]),
            {
              coin: "ZRO",
              position_sz: 0,
              position_usd: 0,
              entry_px: 0,
              current_mid: 4.12,
              leverage_mode: "flat",
              liq_px: null,
              liq_distance_pct: null,
              fills_count: 0,
              fill_volume_usd: 0,
              maker_share_pct: 100,
              net_pnl_usd: -0.45,
              vw_markout_bps: null,
              adl_rank_score: null,
            },
          ],
        },
        testnet_risk: {
          equity_usd: 1018.45,
          free_margin_usd: 742.1,
          high_water_mark_usd: 1025.0,
          drawdown_pct: 0.64,
          kill_switch_drawdown_pct: 5.0,
          open_positions: 2,
          gross_notional_usd: 755.0,
          gross_leverage: 0.74,
          max_mark_oracle_dev_pct: -16.96,
          max_mark_oracle_dev_coin: "GRAM",
          orphan_positions: {
            GRAM: { szi: -30, notional_usd: -48.5, unrealized_pnl_usd: 0.01, book: "empty" },
          },
          orphan_notional_usd: 48.5,
        },
        mm_calibration: {
          BTC: {
            coin: "BTC",
            trades_count: 12,
            empirical_sigma_tau_bps: 2.8,
            kappa_per_bp: 0.42,
            arrival_a_per_sec: 1.5,
            r2_fit: 0.31,
            implied_min_half_spread_bps: 2.38,
            calibration_ok: false,
            depth_curve: [
              { delta_bps: 1, lambda_per_sec: 1.1, fitted_lambda: 1.08 },
              { delta_bps: 3, lambda_per_sec: 0.45, fitted_lambda: 0.44 },
            ],
          },
        },
      }),
    );
    const user = userEvent.setup();

    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    await user.click(screen.getByRole("tab", { name: "Testnet & MM" }));

    expect(screen.getByText(/22% fill volume is from non-audited MM orders/)).toBeInTheDocument();
    expect(screen.getByText("$742.10")).toBeInTheDocument();
    expect(screen.getByText("$48.50")).toBeInTheDocument();
    expect(screen.getByText("Orphan pos: 1")).toBeInTheDocument();
    expect(screen.getByText("-16.96%")).toHaveClass("warn");

    const attrSection = screen.getByText("Per-Coin Testnet Attribution, Liq Distance & ADL Rank").closest("section")!;
    const zroRow = within(attrSection).getByText("ZRO").closest("tr")!;
    expect(within(zroRow).getByText("Flat")).toHaveClass("muted");
    expect(within(zroRow).queryByText(/L \$0\.00/)).not.toBeInTheDocument();
    expect(within(zroRow).queryByText("100%")).not.toBeInTheDocument();
    expect(within(zroRow).getByText("-$0.45")).toHaveClass("neg");

    expect(screen.getByText("Low sample / fallback")).toHaveClass("badge-warn");
  });

  it("renders 5-DEX L2 slippage & depth_be_10k_h (with clean — on null), live 15m H2/H5/H6 events, and failed job last_error", async () => {
    const base = makeOverview();
    mockFetch(
      makeOverview({
        cross_dex_all: [
          {
            coin: "HYPE",
            other_dex: "lighter",
            direction: "SHORT_HL_LONG_OTHER",
            hl_apr_pct: 45.0,
            other_apr_pct: 8.0,
            spread_apr_pct: 37.0,
            favorable_basis_bps: 3.2,
            exec_basis_10k_bps: -1.8,
            l2_slip_10k_bps: 2.4,
            depth_be_10k_h: 11.6,
            breakeven_hours: 8.0,
          },
          {
            coin: "BTC",
            other_dex: "paradex",
            direction: "LONG_HL_SHORT_OTHER",
            hl_apr_pct: 4.0,
            other_apr_pct: 16.5,
            spread_apr_pct: 12.5,
            favorable_basis_bps: -119.0,
            exec_basis_10k_bps: -119.0,
            l2_slip_10k_bps: 88.0,
            depth_be_10k_h: null,
            breakeven_hours: null,
          },
        ],
        onchain_events: {
          observed_at_utc: "2026-11-26T21:45:00Z",
          hlp_tvl_usd: 412_500_000,
          hlp_mm_pnl: {
            pnl_source: "vault_pnlHistory_deposit_clean",
            hlp_pnl_delta_usd: 185_200,
            mm_pnl_regime: "CALM",
          },
          any_alert: true,
          active_alert_keys: ["H2:ETH"],
          h2_events: [
            {
              coin: "ETH",
              mark_px: 3320,
              total_0_5_usd: 14_200_000,
              total_0_10_usd: 52_000_000,
              nearest_wall_pct: 2.1,
              cascade_bias: "downside_cascade",
              alert: true,
            },
          ],
          h5_events: [
            {
              coin: "SOL",
              hlp_side: "HLP_SHORT",
              hlp_net_usd: -420_000,
              hlp_tvl_share_pct: 0.102,
              vaults_holding: 2,
              alert: true,
            },
          ],
          h6_events: [
            {
              coin: "INIT",
              mid_vs_mark_bps: -4.2,
              mark_vs_oracle_bps: -110.9,
              funding_apr_pct: -840.0,
              stress_type: "CROWDED_FUNDING_BASIS",
              alert: false,
            },
          ],
        },
        worker_status: {
          ...base.worker_status!,
          jobs: [
            ...base.worker_status!.jobs,
            {
              name: "cross_dex_scan",
              description: "5-DEX Funding & L2 Depth Scan",
              cron_expr: "25 */4 * * *",
              readonly: true,
              enabled: true,
              status_reason: "active",
              state: "failed",
              last_started_at_utc: "2026-11-26T20:25:00Z",
              last_finished_at_utc: "2026-11-26T20:25:09Z",
              last_exit_code: 1,
              last_duration_s: 9.0,
              last_error: "httpx.ReadTimeout: Paradex orderbook timeout",
              runs_ok: 4,
              runs_failed: 1,
              next_run_utc: "2026-11-27T00:25:00Z",
            },
          ],
        },
      }),
    );
    const user = userEvent.setup();

    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    // 5-DEX tab: shows depth_be_10k_h=11.6h, l2_slip_10k_bps=88.0 bps, and clean "—" (never "—h") for null breakeven
    await user.click(screen.getByRole("tab", { name: "5-DEX & Paper" }));
    const dexSection = screen.getByText("5-DEX Live Funding & Basis Opportunities").closest("section")!;
    expect(within(dexSection).getByText("11.6h")).toBeInTheDocument();
    expect(within(dexSection).getByText("88.0 bps")).toBeInTheDocument();
    expect(dexSection.textContent).not.toContain("—h");

    // On-Chain Alpha tab: prefers 15m h2_events (ETH $52.00M) over stale 4h clusters, and renders H5 + H6 tables
    await user.click(screen.getByRole("tab", { name: "On-Chain Alpha" }));
    expect(screen.getByText("$52.00M")).toBeInTheDocument();
    expect(screen.getByText("HLP_SHORT")).toBeInTheDocument();
    expect(screen.getByText("-$420,000")).toBeInTheDocument();
    expect(screen.getByText("CROWDED_FUNDING_BASIS")).toBeInTheDocument();

    // Scheduler & Ops tab: surfaces last_error on failed job
    await user.click(screen.getByRole("tab", { name: "Scheduler & Ops" }));
    expect(screen.getByText(/Paradex orderbook timeout/)).toBeInTheDocument();
  });

  it("sorts :00 Settlement rows by net_maker_bps descending before slicing so star OOS buckets are top-ranked", async () => {
    const lowRows = Array.from({ length: 12 }, (_, idx) => ({
      strategy: `noise_rule_${idx}`,
      bucket: `bucket_${idx}`,
      split: "holdout",
      events: 100,
      mean_gross_bps: 1.0,
      t_stat: 0.5,
      win_rate_pct: 50,
      net_maker_bps: -2.0 - idx * 0.1,
      net_taker_bps: -8.0,
    }));
    const starRow = {
      strategy: "post_unwind_fade_15m",
      bucket: "APR<=-50% + pre15m>0",
      split: "holdout",
      events: 84,
      mean_gross_bps: 55.37,
      t_stat: 4.08,
      win_rate_pct: 71.4,
      net_maker_bps: 52.37,
      net_taker_bps: 46.37,
    };

    mockFetch(makeOverview({ settlement_stats: [...lowRows, starRow] }));
    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    const settleSection = screen.getByText(":00 Funding Settlement OOS Study").closest("section")!;
    const bodyRows = within(settleSection).getAllByRole("row").slice(1);
    expect(bodyRows[0]).toHaveTextContent("APR<=-50% + pre15m>0");
    expect(bodyRows[0]).toHaveTextContent("52.37 bps");
  });

  it("surfaces backend error state and recovers on manual Refresh", async () => {
    let calls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/v1/overview")) {
        calls += 1;
        if (calls === 1) {
          return new Response("upstream down", { status: 503 });
        }
        return new Response(JSON.stringify(makeOverview({ onchain_events: null })), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ service: "hl-frontend", sha: BASE_SHA, built_at: "" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByText(/Backend \/api\/v1\/overview returned HTTP 503/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => {
      expect(screen.queryByText(/Backend unreachable:/i)).not.toBeInTheDocument();
    });
    expect(screen.getByText("● Calm Regime")).toBeInTheDocument();
  });

  it("renders analyticsError banner on Analytics & Risk tab when /api/v1/analytics returns HTTP 500", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/v1/analytics")) {
        return new Response("analytics boom", { status: 500 });
      }
      if (url.includes("/api/v1/overview")) {
        return new Response(JSON.stringify(makeOverview()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ service: "hl-frontend", sha: BASE_SHA, built_at: "" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    await user.click(screen.getByRole("tab", { name: "Analytics & Risk" }));
    expect(screen.getByText(/Backend \/api\/v1\/analytics returned HTTP 500/i)).toBeInTheDocument();
    expect(screen.queryByText(/No strategy ticks persisted yet/i)).not.toBeInTheDocument();
  });

  it("renders Neutral-style Analytics & Risk tab with Net-mode streaks, Correlation guard, OLS keyboard selection, and 14d APY methodology", async () => {
    const overview = makeOverview({
      autopilot: {
        starting_capital_usd: 250000,
        gross_equity_usd: 254200,
        net_equity_usd: 253600,
        gross_share_price: 1.0168,
        net_share_price: 1.0144,
        hwm_net_share_price: 1.0144,
        gross_return_pct: 1.68,
        net_return_pct: 1.44,
        cum_mgmt_fee_usd: 120,
        cum_perf_fee_usd: 480,
        cum_rebalance_cost_usd: 15,
        sleeves: [
          {
            name: "spot_perp_paper",
            category: "Market Neutral",
            target_weight_pct: 35,
            actual_weight_pct: 35.8,
            drift_pct: 0.8,
            allocation_usd: 91000,
            tick_pnl_usd: 45,
          },
        ],
        risk_telemetry: {
          gross_leverage: 1.28,
          leverage_30d_avg: 1.25,
          leverage_30d_max: 1.41,
          net_directional_exposure_pct: -0.6,
          directional_sleeve_pct: 5.0,
          reserve_sleeve_pct: 15.0,
          max_weight_drift_pct: 0.8,
          rebalanced_this_tick: false,
        },
      },
      spot_perp_paper: {
        starting_capital_usd: 100000,
        equity_usd: 101850,
        share_price: 1.0185,
        return_pct: 1.85,
        hlp_reserve_usd: 10185,
        hlp_reserve_share_pct: 10.0,
        hlp_apr_pct: 14.37,
        free_cash_usd: 8000,
        blended_portfolio_apr_pct: 8.65,
        cum_funding_usd: 1650,
        cum_hlp_yield_usd: 240,
        cum_basis_mtm_usd: 10,
        cum_costs_usd: 50,
        positions: {},
        risk_telemetry: {
          gross_leverage: 1.31,
          leverage_30d_avg: 1.34,
          leverage_30d_max: 1.52,
          active_assets: 5,
          net_delta_pct: 0.012,
          min_perp_liq_distance_pct: 44.5,
        },
      },
    });
    const analyticsPayload = {
      generated_at_utc: "2026-11-27 14:30 UTC",
      leaderboard: [
        {
          id: "spot_perp",
          name: "HyperCore Spot+Perp (+10% HLP)",
          category: "Market Neutral",
          is_benchmark: false,
          fee_schedule: "1% mgmt / 20% HWM",
          equity_usd: 101850,
          share_price_gross: 1.0185,
          share_price_net: 1.0145,
          days_tracked: 45,
          gross_returns: { "7d": 0.25, "30d": 1.12, ytd: 1.85, all_time: 1.85 },
          gross_apy: { "7d": null, "30d": 14.5, ytd: 15.1, all_time: 15.1 },
          net_returns: { "7d": 0.19, "30d": 0.88, ytd: 1.45, all_time: 1.45 },
          net_apy: { "7d": null, "30d": 11.2, ytd: 11.8, all_time: 11.8 },
          sharpe_gross: 5.9,
          sortino_gross: 7.4,
          sharpe_net: 5.1,
          sortino_net: 6.5,
          max_dd_gross_pct: 0.21,
          max_dd_net_pct: 0.24,
          dd_recovery_days: 3,
          positive_days_pct: 82.0,
          positive_days_net_pct: 76.5,
          longest_win_streak: 14,
          longest_loss_streak: 2,
          longest_win_streak_net: 11,
          longest_loss_streak_net: 3,
          monthly_gross: [{ year: 2026, months: { "10": 0.95, "11": 0.89 }, ytd: 1.85 }],
          monthly_net: [{ year: 2026, months: { "10": 0.74, "11": 0.70 }, ytd: 1.45 }],
        },
      ],
      curves: {
        spot_perp: {
          id: "spot_perp",
          name: "HyperCore Spot+Perp (+10% HLP)",
          category: "Market Neutral",
          is_benchmark: false,
          dates: ["2026-10-01", "2026-10-15", "2026-11-01"],
          gross_shares: [1.0, 1.009, 1.0185],
          net_shares: [1.0, 1.007, 1.0145],
        },
      },
      correlation: {
        ids: ["spot_perp"],
        labels: { spot_perp: "HyperCore Spot+Perp (+10% HLP)" },
        matrix: [[1.0]],
        overlap_days: 45,
      },
      market_exposure: {
        BTC: {
          factor: "BTC",
          factor_days: 45,
          rows: [
            {
              id: "spot_perp",
              name: "HyperCore Spot+Perp (+10% HLP)",
              beta: 0.001,
              r2_pct: 0.1,
              alpha_ann_pct: 9.8,
              days: 45,
              scatter: [
                { date: "2026-10-01", factor_ret_pct: -2.5, strat_ret_pct: 0.04 },
                { date: "2026-10-02", factor_ret_pct: 1.8, strat_ret_pct: 0.03 },
                { date: "2026-10-03", factor_ret_pct: -1.1, strat_ret_pct: 0.05 },
              ],
            },
            {
              id: "hip3_paper",
              name: "HIP-3 Split-Margin Paper",
              beta: -0.004,
              r2_pct: 0.2,
              alpha_ann_pct: 14.2,
              days: 30,
              scatter: [
                { date: "2026-10-01", factor_ret_pct: -2.5, strat_ret_pct: 0.06 },
                { date: "2026-10-02", factor_ret_pct: 1.8, strat_ret_pct: 0.05 },
                { date: "2026-10-03", factor_ret_pct: -1.1, strat_ret_pct: 0.07 },
              ],
            },
          ],
        },
      },
      risk_exposure: {
        leverage_by_strategy: [
          {
            strategy: "HyperCore Spot+Perp (+10% HLP)",
            current_leverage: 1.31,
            leverage_30d_avg: 1.34,
            leverage_30d_max: 1.52,
            net_exposure_pct: 0.012,
            nav_guard_triggers: 0,
          },
        ],
        venue_concentration: [{ label: "HyperCore Spot", usd: 55000, share_pct: 84.6 }],
        asset_tier_concentration: [{ label: "Gold RWA", usd: 15000, share_pct: 20.0 }],
        total_nav_guard_triggers: 0,
      },
    };

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/v1/analytics")) {
        return new Response(JSON.stringify(analyticsPayload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/v1/overview")) {
        return new Response(JSON.stringify(overview), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ service: "hl-frontend", sha: BASE_SHA, built_at: "" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    await user.click(screen.getByRole("tab", { name: "Analytics & Risk" }));
    expect(screen.getByText("Performance vs Buy & Hold Leaderboard")).toBeInTheDocument();
    // Default Net mode uses net positive days (76.5%) and net streaks (11d / 3d)
    expect(screen.getByText("76.5%")).toBeInTheDocument();
    expect(screen.getByText("11d / 3d")).toBeInTheDocument();

    // 1x1 correlation matrix triggers the >= 2 strategies guard instead of rendering self-correlation
    expect(screen.getByText(/Waiting for at least 2 strategies with ≥ 3 overlapping days/i)).toBeInTheDocument();

    // Switch to Gross mode -> shows gross positive days (82.0%) and gross streaks (14d / 2d)
    await user.click(screen.getByRole("button", { name: "Gross" }));
    expect(screen.getByText("82.0%")).toBeInTheDocument();
    expect(screen.getByText("14d / 2d")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "APY" }));
    expect(screen.getAllByText("+15.10%").length).toBeGreaterThanOrEqual(1);
    await user.click(screen.getByRole("button", { name: "How calculated" }));
    expect(screen.getByText(/apy_min_days = 14/i)).toBeInTheDocument();

    // Keyboard Enter on OLS row switches scatter plot target
    const hip3OlsRow = screen.getByText("HIP-3 Split-Margin Paper").closest("tr")!;
    hip3OlsRow.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("img", { name: /OLS regression scatter HIP-3 Split-Margin Paper vs BTC/i })).toBeInTheDocument();
  });

  it("maps backend holdout/dev settlement splits to OOS/IS filters and renders Testnet orphan positions", async () => {
    const overview = makeOverview({
      settlement_stats: [
        {
          strategy: "post_unwind_fade_15m",
          bucket: "APR>=+50% (pos)",
          split: "holdout",
          events: 215,
          mean_gross_bps: 5.8,
          t_stat: 3.12,
          win_rate_pct: 59.0,
          net_maker_bps: 2.8,
          net_taker_bps: -3.2,
        },
        {
          strategy: "pre_crowding_15m",
          bucket: "|APR|>=25% (all)",
          split: "dev",
          events: 561,
          mean_gross_bps: 3.49,
          t_stat: 1.55,
          win_rate_pct: 53.1,
          net_maker_bps: 0.49,
          net_taker_bps: -5.51,
        },
      ],
      testnet_audit_recent: [
        {
          ts_ms: 1795700000000,
          observed_at_utc: "2026-11-27 16:10 UTC",
          status: "ok",
          violations: [],
          cancelled_count: 2,
          placed_count: 6,
          total_notional_usd: 320,
          high_water_mark_usd: 1006.55,
          blocked_isolated_coins: [],
          skipped_unquoted_coins: ["PONS"],
          orphan_positions: { SKY: -975, VVV: 1.71 },
        },
      ],
    });

    mockFetch(overview);

    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    expect(screen.getByText("post_unwind_fade_15m")).toBeInTheDocument();
    expect(screen.queryByText("pre_crowding_15m")).not.toBeInTheDocument();

    const splitSelect = screen.getByDisplayValue("OOS only");
    await user.selectOptions(splitSelect, "IS");
    expect(screen.getByText("pre_crowding_15m")).toBeInTheDocument();
    expect(screen.queryByText("post_unwind_fade_15m")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Testnet & MM" }));
    expect(screen.getByText(/orphans: SKY, VVV/)).toBeInTheDocument();
  });

  it("rebases benchmarks on 7D window, omits stale curves outside window, renders +0.00% hedged cex_weight, Boros SPREAD badge, and clean HIP-3 leader", async () => {
    const overview = makeOverview({
      paper_positions: [
        {
          coin: "ETH",
          hl_weight: 0.1,
          cex_weight: 0,
          cex_venue: "bybit+okx",
          cex_legs: { bybit: -0.05, okx: 0.05 },
          last_hl_px: 3310.0,
          l2_spread_bps: 0.5,
          cum_pnl_usd: 12.0,
        },
      ],
      frontier_history: {
        observed_at_utc: "2026-11-27T12:00:00Z",
        ny_session: "RTH",
        hlp_apr_pct: 14.25,
        total_logged: { hip3: 120 },
        hip3_persistence: [
          {
            base_symbol: "STALE_TOP",
            short_symbol: "xyz:STALE_TOP",
            long_symbol: "io:STALE_TOP",
            ticks_seen: 20,
            active_in_latest_tick: false,
            ticks_since_last_seen: 2,
            raw_apr_streak: 0,
            clean_streak: 0,
            direction_flips: 0,
            latest_apr_pct: 90.0,
            median_apr_pct: 50.0,
            median_oracle_div_bps: 5.0,
            both_growth_mode: true,
            any_only_isolated: false,
          },
          {
            base_symbol: "NVDA",
            short_symbol: "xyz:NVDA",
            long_symbol: "io:NVDA",
            ticks_seen: 14,
            active_in_latest_tick: true,
            ticks_since_last_seen: 0,
            raw_apr_streak: 5,
            clean_streak: 4,
            direction_flips: 0,
            latest_apr_pct: 41.3,
            median_apr_pct: 39.0,
            median_oracle_div_bps: 4.2,
            both_growth_mode: true,
            any_only_isolated: false,
          },
        ],
      },
      boros: {
        active_markets_count: 5,
        hl_and_spread_count: 1,
        markets: [
          {
            market_id: 7,
            name: "Hyperliquid ETH - Gate ETHUSDT",
            symbol: "HL-GATE-ETH-DEC",
            underlying: "ETH",
            is_spread_market: true,
            days_to_maturity: 28,
            best_bid_apr_pct: 2.3,
            best_ask_apr_pct: 2.5,
            floating_apr_pct: -4.1,
            fixed_minus_float_bid_pct: 6.4,
            recommended_action: "SHORT_FIXED",
          },
        ],
      },
    });

    const analyticsPayload = {
      generated_at_utc: "2026-11-27 14:30 UTC",
      leaderboard: [],
      curves: {
        btc: {
          id: "btc",
          name: "BTC Buy & Hold",
          category: "Benchmark",
          is_benchmark: true,
          dates: ["2025-11-27", "2026-11-20", "2026-11-27"],
          // 1.0 -> 2.0 (+100% over 1y), then 2.0 -> 2.02 (+1% over last 7D)
          gross_shares: [1.0, 2.0, 2.02],
          net_shares: [1.0, 2.0, 2.02],
        },
        spot_perp: {
          id: "spot_perp",
          name: "Spot+Perp",
          category: "Market Neutral",
          is_benchmark: false,
          dates: ["2026-11-21", "2026-11-27"],
          gross_shares: [1.0, 1.005],
          net_shares: [1.0, 1.004],
        },
        stale_strat: {
          id: "stale_strat",
          name: "Dormant Old Strat",
          category: "Market Neutral",
          is_benchmark: false,
          dates: ["2026-09-01", "2026-09-05"],
          gross_shares: [1.0, 1.02],
          net_shares: [1.0, 1.02],
        },
      },
      correlation: {
        ids: ["spot_perp", "btc"],
        labels: { spot_perp: "HyperCore Spot+Perp (+10% HLP)", btc: "BTC Buy & Hold" },
        matrix: [
          [1.0, null],
          [0.02, 1.0],
        ],
        overlap_days: 7,
      },
      market_exposure: {},
      risk_exposure: {
        leverage_by_strategy: [],
        venue_concentration: [],
        asset_tier_concentration: [],
        total_nav_guard_triggers: 0,
      },
    };

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/v1/analytics")) {
        return new Response(JSON.stringify(analyticsPayload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/v1/overview")) {
        return new Response(JSON.stringify(overview), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ service: "hl-frontend", sha: BASE_SHA, built_at: "" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    // Overview: HIP-3 Clean Leader skips STALE_TOP and picks active NVDA
    expect(screen.getByText("NVDA +41.3%")).toBeInTheDocument();
    // Overview: net-zero multi-venue hedge shows +0.00% (never "—") and singular "1 active HL × CEX position"
    const posSection = screen.getByText("Metamodel Open Positions (Live Paper Ledger)").closest("section")!;
    expect(within(posSection).getByText(/1 active HL × CEX position sorted/i)).toBeInTheDocument();
    expect(within(posSection).getByText("+0.00%")).toBeInTheDocument();

    // Frontier tab: Boros shows market name + SPREAD badge
    await user.click(screen.getByRole("tab", { name: "HIP-3 · Boros · Yield" }));
    const borosSection = screen.getByText("Pendle Boros Fixed vs Floating Markets").closest("section")!;
    expect(within(borosSection).getByText("Hyperliquid ETH - Gate ETHUSDT")).toBeInTheDocument();
    expect(within(borosSection).getByText("SPREAD")).toHaveClass("badge-cyan");

    // Analytics tab: 7D window omits stale_strat (2 polylines, not 3) and rebases BTC even if Rebase checkbox is unchecked
    await user.click(screen.getByRole("tab", { name: "Analytics & Risk" }));
    await user.click(screen.getByRole("button", { name: "7D" }));
    await user.click(screen.getByLabelText("Rebase to window"));
    const chartSvg = screen.getByRole("img", { name: /Multi-strategy cumulative performance chart/i });
    expect(chartSvg.querySelectorAll("polyline")).toHaveLength(2);
    // BTC rebased to 2.0 on 2026-11-20 -> max tick is around +1.0%, NOT +102.0%
    expect(chartSvg.textContent).not.toContain("+102.0%");

    // 2x2 correlation matrix with >= 3 overlap days renders shortened label headers
    const corrSection = screen.getByText("Correlation Between Strategies (Pearson ρ)").closest<HTMLElement>(".card")!;
    expect(within(corrSection).getByText("0.02")).toBeInTheDocument();
  });
});

describe("i18n & tooltips", () => {
  it("switches the UI between EN and RU and remembers the choice", async () => {
    mockFetch(makeOverview());
    const user = userEvent.setup();
    const { unmount } = render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    expect(screen.getByRole("button", { name: "EN" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Metamodel Open Positions (Live Paper Ledger)")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "RU" }));
    expect(screen.getByRole("button", { name: "RU" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Открытые позиции метамодели (paper-журнал)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Обновить" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Аналитика и риски" })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("ru");
    expect(window.localStorage.getItem("hl_desk_lang")).toBe("ru");

    unmount();
    render(<App />);
    expect(await screen.findByText("Открытые позиции метамодели (paper-журнал)")).toBeInTheDocument();
  });

  it("shows plain-language tooltips for table columns and blocks on hover and focus without adding tabIndex to th", async () => {
    mockFetch(makeOverview());
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    const posSection = screen.getByText("Metamodel Open Positions (Live Paper Ledger)").closest("section")!;
    const hlWeightHeader = within(posSection).getByText("HL Weight");
    expect(hlWeightHeader).not.toHaveAttribute("tabindex");
    await user.hover(hlWeightHeader);
    expect(screen.getByRole("tooltip")).toHaveTextContent(/Share of capital in the Hyperliquid leg/);
    await user.unhover(hlWeightHeader);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    const infoIcons = within(posSection).getAllByLabelText("What is this?");
    infoIcons[0].focus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent(/Positions the metamodel currently holds/);
    infoIcons[0].blur();

    await user.click(screen.getByRole("button", { name: "RU" }));
    await user.hover(within(posSection).getByText("Вес HL"));
    expect(screen.getByRole("tooltip")).toHaveTextContent(/Доля капитала в позиции на Hyperliquid/);
  });

  it("renders venue and per-position PnL columns in 5-DEX open dual-leg paper positions", async () => {
    const overview = makeOverview({
      cross_dex_paper: {
        starting_capital_usd: 50000,
        equity_usd: 49980.25,
        cum_funding_usd: 34.5,
        cum_basis_mtm_usd: -12.25,
        cum_costs_usd: 42.0,
        closed_pairs_count: 2,
        payback_achieved_count: 2,
        positions: {
          ETH: {
            coin: "ETH",
            other_dex: "extended",
            direction: "Short HL / Long extended",
            leg_notional_usd: 5000,
            hours_held: 18.2,
            entry_spread_apr_pct: 29.4,
            live_spread_apr_pct: 24.1,
            entry_l2_slip_bps: 1.5,
            entry_exec_basis_bps: 2.75,
            cum_funding_usd: 19.8,
            cum_basis_mtm_usd: -4.2,
            net_pnl_usd: 11.4,
          },
        },
      },
    });
    mockFetch(overview);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    await user.click(screen.getByRole("tab", { name: "5-DEX & Paper" }));
    const dualSec = screen.getByText("Open Dual-Leg Paper Positions").closest("section")!;
    expect(within(dualSec).getByText("extended")).toBeInTheDocument();
    expect(within(dualSec).getByText("$19.80")).toBeInTheDocument();
    expect(within(dualSec).getByText("-$4.20")).toBeInTheDocument();
    expect(within(dualSec).getByText("$11.40")).toBeInTheDocument();
  });
});

describe("Audit 2026-11-28 regressions", () => {
  it("signCls treats values that display as 0.00 as neutral (no red +0.00%)", () => {
    expect(signCls(-0.001)).toBe("");
    expect(signCls(0.004)).toBe("");
    expect(signCls(-0.006)).toBe("neg");
    expect(signCls(-0.04, 1)).toBe("");
    expect(signCls(-0.4, 0)).toBe("");
    expect(signCls(-0.6, 0)).toBe("neg");
    expect(signCls(0.0004, 3)).toBe("");
    expect(signCls(0.0006, 3)).toBe("pos");
    expect(riskSignCls(-0.001)).toBe("");
    expect(riskSignCls(-0.02, 1)).toBe("");
    expect(riskSignCls(-0.2, 1)).toBe("neg");
    expect(fmtPct(-0.001)).toBe("+0.00%");
  });

  it("parses worker heartbeat stamps and classifies liveness with the 300 s hl_worker_up threshold", () => {
    expect(parseUtcTs("2026-11-26 21:59:45 UTC")).toBe(Date.parse("2026-11-26T21:59:45Z"));
    expect(parseUtcTs("2026-11-26 22:00 UTC")).toBe(Date.parse("2026-11-26T22:00:00Z"));
    expect(parseUtcTs("2026-11-26T21:59:45Z")).toBe(Date.parse("2026-11-26T21:59:45Z"));
    expect(parseUtcTs("garbage")).toBeNull();
    expect(workerLiveness("2026-11-26 21:56:00 UTC", "2026-11-26 22:00 UTC")).toBe("online");
    expect(workerLiveness("2026-11-26 21:54:59 UTC", "2026-11-26 22:00 UTC")).toBe("stale");
    expect(workerLiveness(null, "2026-11-26 22:00 UTC")).toBe("stale");
    expect(workerLiveness(undefined, "2026-11-26 22:00 UTC")).toBe("external");
    // Falls back to the browser clock when the API snapshot time is unparseable.
    expect(workerLiveness("2026-11-26 21:59:00 UTC", "", Date.parse("2026-11-26T22:30:00Z"))).toBe("stale");
  });

  it("shows STALE instead of ONLINE when the worker heartbeat is older than 300 s", async () => {
    const base = makeOverview();
    mockFetch(
      makeOverview({
        generated_at_utc: "2026-11-26 22:00 UTC",
        worker_status: { ...base.worker_status!, heartbeat_utc: "2026-11-26 21:40:00 UTC" },
      }),
    );
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");
    await user.click(screen.getByRole("tab", { name: "Scheduler & Ops" }));
    expect(screen.getByText("STALE")).toBeInTheDocument();
    expect(screen.queryByText("ONLINE")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "RU" }));
    expect(screen.getByText("УСТАРЕЛ")).toBeInTheDocument();
  });

  it("shows ONLINE for a fresh worker heartbeat", async () => {
    mockFetch(makeOverview());
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");
    await user.click(screen.getByRole("tab", { name: "Scheduler & Ops" }));
    expect(screen.getByText("ONLINE")).toBeInTheDocument();
  });

  it("ErrorBoundary renders the persisted language (RU) outside LangProvider", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    window.localStorage.setItem("hl_desk_lang", "ru");
    function Boom(): never {
      throw new Error("kaboom");
    }
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Ошибка отрисовки дашборда");
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
  });

  it("skips polling while the tab is hidden and refreshes on becoming visible", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let visibility: DocumentVisibilityState = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
    try {
      const fetchSpy = mockFetch(makeOverview());
      render(<App />);
      await screen.findByText("Hyperliquid Quant DeFi · Live Desk");
      const overviewCalls = () =>
        fetchSpy.mock.calls.filter(([input]) => String(input).includes("/api/v1/overview")).length;
      const initial = overviewCalls();

      visibility = "hidden";
      await act(async () => {
        await vi.advanceTimersByTimeAsync(130_000);
      });
      expect(overviewCalls()).toBe(initial);

      visibility = "visible";
      await act(async () => {
        document.dispatchEvent(new Event("visibilitychange"));
        await vi.advanceTimersByTimeAsync(10);
      });
      expect(overviewCalls()).toBe(initial + 1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("aborts a hung refresh after the fetch timeout and surfaces an error", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.spyOn(globalThis, "fetch").mockImplementation(
        (_input, init) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
          }),
      );
      render(<App />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(FETCH_TIMEOUT_MS + 100);
      });
      expect(await screen.findByText(/Request timed out after 25 s/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
