import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "../App";
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
    return new Response("Not found", { status: 404 });
  });
}

describe("App", () => {
  it("renders overview telemetry, FE/BE SHA badge, and filters Metamodel Open Positions", async () => {
    mockFetch(makeOverview());
    const user = userEvent.setup();

    render(<App />);

    expect(await screen.findByText("Hyperliquid Quant DeFi · Live Desk")).toBeInTheDocument();
    expect(screen.getByText("⚡ On-Chain Alerts Active")).toBeInTheDocument();
    const shaPill = screen.getByText("FE 3b4d8f21 · BE 3b4d8f21");
    expect(shaPill).toHaveClass("pill-info");

    // Metamodel positions table shows both BTC and SOL initially
    const posSection = screen.getByText("Metamodel Open Positions (Live Paper Ledger)").closest("section")!;
    expect(within(posSection).getByText("BTC")).toBeInTheDocument();
    expect(within(posSection).getByText("SOL")).toBeInTheDocument();
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
    expect(within(settleSection).queryByText("620")).not.toBeInTheDocument();
    await user.selectOptions(within(settleSection).getByRole("combobox"), "IS");
    expect(within(settleSection).getByText("620")).toBeInTheDocument();
  });

  it("flags FE/BE SHA mismatch with warning pill", async () => {
    mockFetch(makeOverview(), {
      service: "hl-frontend",
      sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      built_at: "2026-11-26T21:00:00Z",
    });

    render(<App />);

    const shaPill = await screen.findByText("FE aaaaaaaa · BE 3b4d8f21");
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
      })
    );
    const user = userEvent.setup();

    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    await user.click(screen.getByRole("button", { name: "HIP-3 · Boros · Yield" }));

    const hip3PaperSection = screen.getByText("HIP-3 Split-Margin Paper Ledger").closest("section")!;
    expect(within(hip3PaperSection).getByText("$50,042.50")).toBeInTheDocument();
    expect(within(hip3PaperSection).getByText("MU")).toBeInTheDocument();
    expect(within(hip3PaperSection).getByText("Isolated")).toHaveClass("badge-warn");

    const hip3Section = screen.getByText("HIP-3 Cross-Builder Spread Persistence").closest("section")!;
    // Active only is checked by default: AAPL and NVDA shown, stale TSLA hidden
    expect(within(hip3Section).getByText("AAPL")).toBeInTheDocument();
    expect(within(hip3Section).getByText("NVDA")).toBeInTheDocument();
    expect(within(hip3Section).queryByText("TSLA")).not.toBeInTheDocument();

    // Uncheck Active only -> stale TSLA appears with Stale (-3t) badge
    await user.click(screen.getByLabelText("Active only"));
    expect(within(hip3Section).getByText("TSLA")).toBeInTheDocument();
    expect(within(hip3Section).getByText("Stale (-3t)")).toBeInTheDocument();

    // Check Only steady (0 flips) -> hides flipped NVDA and stale TSLA (clean_streak=0), keeps AAPL
    await user.click(screen.getByLabelText("Only steady (0 flips)"));
    expect(within(hip3Section).getByText("AAPL")).toBeInTheDocument();
    expect(within(hip3Section).queryByText("NVDA")).not.toBeInTheDocument();
    expect(within(hip3Section).queryByText("TSLA")).not.toBeInTheDocument();
  });

  it("renders Testnet & MM per-coin attribution, ADL badges, audit cycles, and kappa chart", async () => {
    mockFetch(makeOverview());
    const user = userEvent.setup();

    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    await user.click(screen.getByRole("button", { name: "Testnet & MM" }));

    expect(screen.getByText("HEALTHY")).toBeInTheDocument();
    const attrSection = screen.getByText("Per-Coin Testnet Attribution, Liq Distance & ADL Rank").closest("section")!;
    expect(within(attrSection).getByText("BTC")).toBeInTheDocument();
    expect(within(attrSection).getByText("ETH")).toBeInTheDocument();
    expect(within(attrSection).getByText("2.35")).toHaveClass("badge-warn");
    expect(within(attrSection).getByText("0.85")).toHaveClass("badge-ok");

    const auditSection = screen.getByText("Recent Testnet ALO Rebalance Cycles").closest("section")!;
    expect(within(auditSection).getByText("$380.34")).toBeInTheDocument();
    expect(within(auditSection).getByText("drawdown_kill_switch")).toBeInTheDocument();
    expect(within(auditSection).getByText("halted")).toHaveClass("badge-danger");

    expect(screen.getByRole("img", { name: "BTC kappa curve" })).toBeInTheDocument();
  });

  it("renders 5-DEX filter, On-Chain Alpha alerts, and Scheduler & Ops worker telemetry", async () => {
    mockFetch(makeOverview());
    const user = userEvent.setup();

    render(<App />);
    await screen.findByText("Hyperliquid Quant DeFi · Live Desk");

    // 5-DEX tab
    await user.click(screen.getByRole("button", { name: "5-DEX & Paper" }));
    const dexSection = screen.getByText("5-DEX Live Funding & Basis Opportunities").closest("section")!;
    expect(within(dexSection).getByText("HYPE")).toBeInTheDocument();
    expect(within(dexSection).getByText("ARB")).toBeInTheDocument();
    await user.selectOptions(within(dexSection).getByRole("combobox"), "30");
    expect(within(dexSection).getByText("HYPE")).toBeInTheDocument();
    expect(within(dexSection).queryByText("ARB")).not.toBeInTheDocument();

    // On-Chain tab
    await user.click(screen.getByRole("button", { name: "On-Chain Alpha" }));
    expect(screen.getByText(/H2_BTC_LIQ_WALL/)).toBeInTheDocument();
    expect(screen.getByText("#1,842,310")).toBeInTheDocument();

    // Scheduler & Ops tab
    await user.click(screen.getByRole("button", { name: "Scheduler & Ops" }));
    expect(screen.getByText("ONLINE")).toBeInTheDocument();
    expect(screen.getByText("READ-ONLY")).toBeInTheDocument();
    expect(screen.getByText("snapshot")).toBeInTheDocument();
    expect(screen.getByText("testnet_executor")).toBeInTheDocument();
    expect(screen.getByText("hl_testnet_audit.jsonl")).toBeInTheDocument();
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
});
