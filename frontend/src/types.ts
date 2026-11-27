export interface BuildInfo {
  service: string;
  sha: string;
  built_at: string;
}

export interface ArtifactFreshness {
  exists: boolean;
  path: string;
  updated_at_utc: string | null;
  age_minutes: number | null;
}

export interface WorkerJobStatus {
  name: string;
  description: string;
  cron_expr: string;
  readonly: boolean;
  enabled: boolean;
  status_reason: string;
  state: string;
  last_started_at_utc: string | null;
  last_finished_at_utc: string | null;
  last_exit_code: number | null;
  last_duration_s: number | null;
  runs_ok: number;
  runs_failed: number;
  next_run_utc: string;
}

export interface WorkerStatus {
  service: string;
  build_sha: string;
  started_at_utc: string;
  heartbeat_utc: string;
  telegram_enabled: boolean;
  testnet_execution_enabled: boolean;
  jobs: WorkerJobStatus[];
}

export interface Hip3PersistenceRow {
  base_symbol: string;
  short_symbol: string;
  long_symbol: string;
  ticks_seen: number;
  active_in_latest_tick?: boolean;
  ticks_since_last_seen?: number;
  raw_apr_streak: number;
  clean_streak: number;
  direction_flips?: number;
  latest_apr_pct: number;
  median_apr_pct: number;
  median_oracle_div_bps: number;
  both_growth_mode: boolean;
  any_only_isolated: boolean;
}

export interface PaperPositionRow {
  coin: string;
  hl_weight: number;
  cex_weight: number;
  cex_venue: string;
  last_hl_px: number | null;
  l2_spread_bps: number | null;
  cum_pnl_usd: number;
}

export interface TestnetAuditRecord {
  ts_ms: number;
  observed_at_utc: string | null;
  status: string;
  violations: string[];
  cancelled_count: number;
  placed_count: number;
  total_notional_usd: number;
  high_water_mark_usd: number;
  blocked_isolated_coins: string[];
}

export interface CoinAttributionRow {
  coin: string;
  position_sz: number;
  position_usd: number;
  entry_px: number;
  current_mid: number;
  leverage_mode: string;
  liq_px: number | null;
  liq_distance_pct: number | null;
  fills_count: number;
  fill_volume_usd: number;
  maker_share_pct: number;
  net_pnl_usd: number;
  vw_markout_bps: number | null;
  adl_rank_score: number | null;
}

export interface Hip3Opp {
  base_symbol: string;
  short_symbol: string;
  long_symbol: string;
  spread_apr_pct: number;
  net_apr_split_margin_pct?: number;
  oracle_divergence_bps: number;
  oracle_risk_warning: boolean;
  both_growth_mode: boolean;
  l2_slip_5k_bps: number;
  payback_hours_5k: number;
  min_growth_lock_days_left?: number | null;
}

export interface BorosMarket {
  market_id: number;
  name: string;
  symbol: string;
  underlying: string;
  is_spread_market: boolean;
  days_to_maturity: number;
  best_bid_apr_pct: number;
  best_ask_apr_pct: number;
  floating_apr_pct: number;
  fixed_minus_float_bid_pct: number;
  recommended_action: string;
}

export interface YieldStrategyRow {
  name: string;
  rate_type: string;
  gross_stacked_apr_pct: number;
  capital_efficiency_pct: number;
  net_deposit_apr_pct: number;
  spread_vs_hlp_pct: number;
  perp_leverage: number;
  liq_buffer_pct: number;
  key_risks: string;
}

export interface CrossDexOpp {
  coin: string;
  other_dex: string;
  direction: string;
  hl_apr_pct: number;
  other_apr_pct: number;
  spread_apr_pct: number;
  favorable_basis_bps: number;
  breakeven_hours: number;
  hl_vol_m?: number;
}

export interface CrossDexPosition {
  coin: string;
  direction: string;
  leg_notional_usd: number;
  hours_held: number;
  entry_spread_apr_pct: number;
  live_spread_apr_pct: number;
  entry_l2_slip_bps: number;
  entry_exec_basis_bps: number;
}

export interface PaperTick {
  tick_hour_ms: number;
  equity_usd: number;
  net_pnl_usd: number;
  fund_pnl_usd: number;
  cost_pnl_usd: number;
  turnover: number;
}

export interface SettlementStat {
  strategy: string;
  bucket: string;
  split: string;
  events: number;
  mean_gross_bps: number;
  t_stat: number;
  win_rate_pct: number;
  net_maker_bps: number;
  net_taker_bps: number;
}

export interface CalibrationPoint {
  delta_bps: number;
  lambda_per_sec: number;
  fitted_lambda: number;
}

export interface CoinCalibration {
  coin: string;
  trades_count: number;
  empirical_sigma_tau_bps: number;
  kappa_per_bp: number;
  arrival_a_per_sec: number;
  r2_fit: number;
  implied_min_half_spread_bps: number;
  depth_curve: CalibrationPoint[];
}

export interface OverviewPayload {
  generated_at_utc: string;
  build_info?: BuildInfo;
  worker_status?: WorkerStatus | null;
  artifact_freshness?: Record<string, ArtifactFreshness>;
  attribution?: Record<string, unknown> | null;
  paper?: {
    ticks: number;
    starting_capital_usd: number;
    equity_usd: number;
    return_pct: number;
    active_positions: number;
  } | null;
  paper_ticks?: PaperTick[];
  paper_positions?: PaperPositionRow[];
  testnet_audit_recent?: TestnetAuditRecord[];
  latency_bench?: {
    iterations: number;
    p50_us: number;
    p95_us: number;
    p99_us: number;
    throughput_ticks_per_sec: number;
  } | null;
  settlement_stats?: SettlementStat[];
  settlement_meta?: {
    span_days?: number;
    total_settlement_events?: number;
    coins_collected?: number;
  } | null;
  frontier_history?: {
    observed_at_utc: string;
    ny_session: string;
    hlp_apr_pct: number;
    total_logged: Record<string, number>;
    hip3_persistence: Hip3PersistenceRow[];
  } | null;
  hip3?: {
    observed_at_ms: number;
    ny_session: string;
    builders_discovered: number;
    active_markets_count: number;
    total_hip3_day_vol_usd: number;
    cross_builder_opps: Hip3Opp[];
  } | null;
  boros?: {
    active_markets_count: number;
    hl_and_spread_count: number;
    markets: BorosMarket[];
  } | null;
  yield_stack?: {
    inputs?: Record<string, unknown>;
    strategies: YieldStrategyRow[];
  } | null;
  cross_dex_paper?: {
    starting_capital_usd: number;
    equity_usd: number;
    cum_funding_usd: number;
    cum_basis_mtm_usd: number;
    cum_costs_usd: number;
    positions: Record<string, CrossDexPosition>;
    closed_pairs_count: number;
    payback_achieved_count: number;
  } | null;
  cross_dex_all?: CrossDexOpp[];
  onchain_events?: {
    observed_at_utc: string;
    hlp_tvl_usd: number;
    hlp_mm_pnl?: Record<string, unknown>;
    alerts_count?: Record<string, number>;
    total_logged?: Record<string, number>;
    any_alert: boolean;
    active_alert_keys?: string[];
    new_alert_keys?: string[];
  } | null;
  liq_funding?: {
    whales_scanned: number;
    whales_with_positions: number;
    clusters: Array<Record<string, unknown>>;
    funding_micro: Array<Record<string, unknown>>;
  } | null;
  hyperevm?: {
    chain?: Record<string, unknown>;
    l1_read_oracles?: Array<Record<string, unknown>>;
    evm_arb?: Array<Record<string, unknown>>;
    vaults?: Array<Record<string, unknown>>;
  } | null;
  mm_calibration?: Record<string, CoinCalibration> | null;
}
