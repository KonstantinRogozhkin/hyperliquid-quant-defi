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
  last_error?: string | null;
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
  cex_legs?: Record<string, number>;
  last_hl_px: number | null;
  l2_spread_bps: number | null;
  cum_pnl_usd: number;
}

export type MetamodelPosition = PaperPositionRow;

export interface TestnetAuditRecord {
  ts_ms: number;
  observed_at_utc: string | null;
  status: string;
  mode?: string;
  derisk_mode?: string | null;
  violations: string[];
  cancelled_count: number;
  placed_count: number;
  total_notional_usd: number;
  high_water_mark_usd: number;
  equity_usd?: number | null;
  free_margin_usd?: number | null;
  drawdown_pct?: number | null;
  orphan_notional_usd?: number | null;
  orphan_unwind_count?: number;
  blocked_isolated_coins: string[];
  blocked_leverage_coins?: string[];
  foreign_coins?: string[];
  skipped_unquoted_coins?: string[];
  orphan_positions?: Record<string, number>;
}

export interface TestnetRiskSnapshot {
  observed_at_ms?: number;
  observed_at_utc?: string | null;
  equity_usd: number;
  free_margin_usd: number;
  high_water_mark_usd: number;
  drawdown_pct: number;
  kill_switch_drawdown_pct?: number;
  drawdown_headroom_pct?: number;
  open_positions: number;
  gross_notional_usd: number;
  gross_leverage: number;
  unrealized_pnl_mark_usd?: number;
  unrealized_pnl_oracle_usd?: number;
  mark_oracle_pnl_gap_usd?: number;
  mark_oracle_pnl_gap_pct?: number;
  max_mark_oracle_dev_pct: number;
  max_mark_oracle_dev_coin?: string | null;
  orphan_positions: number;
  orphan_notional_usd: number;
  orphan_unrealized_pnl_usd?: number;
  open_orders?: number;
  per_coin?: Array<Record<string, unknown>>;
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
  l2_slip_5k_bps: number | null;
  payback_hours_5k: number | null;
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
  breakeven_hours: number | null;
  exec_basis_10k_bps?: number | null;
  l2_slip_10k_bps?: number | null;
  l2_slip_50k_bps?: number | null;
  depth_be_10k_h?: number | null;
  depth_50k_ok?: boolean;
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

export interface Hip3PaperPosition {
  base_symbol: string;
  short_symbol: string;
  long_symbol: string;
  short_dex: string;
  long_dex: string;
  both_growth_mode: boolean;
  any_only_isolated: boolean;
  margin_mode: string;
  leg_notional_usd: number;
  margin_locked_usd: number;
  hours_held: number;
  entry_spread_apr_pct: number;
  live_spread_apr_pct: number;
  live_oracle_div_bps: number;
  entry_l2_slip_bps: number;
  entry_exec_basis_bps: number;
  cum_funding_usd: number;
  cum_basis_mtm_usd: number;
  cum_cost_usd: number;
  net_pnl_usd: number;
  clean_streak: number;
}

export interface Hip3PaperState {
  starting_capital_usd: number;
  equity_usd: number;
  return_pct: number;
  margin_locked_usd: number;
  margin_utilization_pct: number;
  cum_funding_usd: number;
  cum_basis_mtm_usd: number;
  cum_costs_usd: number;
  observed_at_utc?: string;
  ny_session?: string;
  positions: Record<string, Hip3PaperPosition>;
  closed_pairs_count: number;
  payback_achieved_count: number;
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
  calibration_ok?: boolean;
  calibration_reason?: string;
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
  testnet_risk?: TestnetRiskSnapshot | null;
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
  hip3_paper?: Hip3PaperState | null;
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
    h2_events?: Array<Record<string, unknown>>;
    h4_events?: Array<Record<string, unknown>>;
    h5_events?: Array<Record<string, unknown>>;
    h6_events?: Array<Record<string, unknown>>;
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
  spot_perp_paper?: SpotPerpPaperState | null;
  spot_perp_scan?: SpotPerpScanSnapshot | null;
  autopilot?: AutopilotState | null;
}

export interface SpotPerpOpportunity {
  base_symbol: string;
  spot_pair: string;
  spot_token: string;
  asset_tier: string;
  is_proxy_hedge: boolean;
  spot_day_vol_usd: number;
  funding_apr_pct: number;
  net_deposit_apr_pct: number;
  exec_basis_bps: number | null;
  mark_basis_bps: number;
  entry_l2_slip_bps: number | null;
  payback_hours: number | null;
  boros_fixed_apr_pct: number | null;
  eligible: boolean;
  reject_reason: string;
}

export interface SpotPerpScanSnapshot {
  observed_at_utc: string;
  hlp_apr_pct: number;
  hlp_tvl_usd: number;
  clip_usd: number;
  opportunities: SpotPerpOpportunity[];
}

export interface SpotPerpPosition {
  base_symbol: string;
  spot_pair: string;
  spot_token: string;
  asset_tier: string;
  spot_notional_usd: number;
  perp_notional_usd: number;
  effective_perp_leverage: number;
  perp_liq_distance_pct: number;
  live_funding_apr_pct: number;
  cum_funding_usd: number;
  net_pnl_usd: number;
  hours_held: number;
}

export interface SpotPerpPaperState {
  starting_capital_usd: number;
  equity_usd: number;
  share_price: number;
  return_pct: number;
  hlp_reserve_usd: number;
  hlp_reserve_share_pct: number;
  hlp_apr_pct: number;
  free_cash_usd: number;
  blended_portfolio_apr_pct: number;
  cum_funding_usd: number;
  cum_hlp_yield_usd: number;
  cum_basis_mtm_usd: number;
  cum_costs_usd: number;
  observed_at_utc?: string;
  positions: Record<string, SpotPerpPosition>;
  risk_telemetry?: {
    gross_leverage: number;
    leverage_30d_avg: number;
    leverage_30d_max: number;
    active_assets: number;
    net_delta_pct: number;
    min_perp_liq_distance_pct: number;
    venue_breakdown_usd?: Record<string, number>;
    asset_tier_breakdown_usd?: Record<string, number>;
    nav_guard_triggered?: boolean;
  };
}

export interface AutopilotSleeveRow {
  name: string;
  category: string;
  target_weight_pct: number;
  actual_weight_pct: number;
  drift_pct: number;
  allocation_usd: number;
  tick_pnl_usd: number;
}

export interface AutopilotCtaAsset {
  coin: string;
  last_close: number;
  tsmom_signal: number;
  ann_vol_pct: number;
  weight: number;
  target_weight: number;
}

export interface AutopilotState {
  starting_capital_usd: number;
  gross_equity_usd: number;
  net_equity_usd: number;
  gross_share_price: number;
  net_share_price: number;
  hwm_net_share_price: number;
  gross_return_pct: number;
  net_return_pct: number;
  cum_mgmt_fee_usd: number;
  cum_perf_fee_usd: number;
  cum_rebalance_cost_usd: number;
  observed_at_utc?: string;
  sleeves: AutopilotSleeveRow[];
  cta_state?: {
    gross_leverage?: number;
    net_exposure?: number;
    cum_pnl_usd?: number;
    assets?: Record<string, AutopilotCtaAsset>;
  };
  risk_telemetry?: {
    gross_leverage: number;
    leverage_30d_avg: number;
    leverage_30d_max: number;
    net_directional_exposure_pct: number;
    directional_sleeve_pct: number;
    reserve_sleeve_pct: number;
    max_weight_drift_pct: number;
    rebalanced_this_tick: boolean;
    nav_guard_triggered?: boolean;
  };
}

export interface HorizonMetrics {
  "7d": number | null;
  "30d": number | null;
  ytd: number | null;
  all_time: number | null;
}

export interface MonthlyReturnRow {
  year: number;
  months: Record<string, number>;
  ytd: number | null;
}

export interface LeaderboardRow {
  id: string;
  name: string;
  category: string;
  is_benchmark: boolean;
  fee_schedule: string;
  equity_usd: number | null;
  share_price_gross: number | null;
  share_price_net: number | null;
  spot_price_usd?: number | null;
  days_tracked: number;
  gross_returns: HorizonMetrics;
  gross_apy: HorizonMetrics;
  net_returns: HorizonMetrics;
  net_apy: HorizonMetrics;
  sharpe_gross: number | null;
  sortino_gross: number | null;
  sharpe_net: number | null;
  sortino_net: number | null;
  max_dd_gross_pct: number;
  max_dd_net_pct: number;
  /** Longest completed peak-to-recovery span in days (null if no drawdown has recovered yet). */
  dd_recovery_days: number | null;
  /** Length of the still-open drawdown in days (0 at a high-water mark). */
  current_underwater_days?: number;
  dd_recovery_net_days?: number | null;
  current_underwater_net_days?: number;
  positive_days_pct: number | null;
  positive_days_net_pct?: number | null;
  longest_win_streak: number;
  longest_loss_streak: number;
  longest_win_streak_net?: number;
  longest_loss_streak_net?: number;
  monthly_gross: MonthlyReturnRow[];
  monthly_net: MonthlyReturnRow[];
}

export type AnalyticsLeaderboardRow = LeaderboardRow;

export interface AnalyticsCurve {
  id: string;
  name: string;
  category: string;
  is_benchmark: boolean;
  dates: string[];
  gross_shares: number[];
  net_shares: number[];
}

export interface OlsScatterPoint {
  date: string;
  factor_ret_pct: number;
  strat_ret_pct: number;
}

export interface OlsExposureRow {
  id: string;
  name: string;
  beta: number | null;
  r2_pct: number | null;
  alpha_ann_pct: number | null;
  days: number;
  scatter: OlsScatterPoint[];
}

export interface RiskConcentrationRow {
  label: string;
  usd: number;
  share_pct: number;
}

export interface StrategyLeverageRow {
  strategy: string;
  current_leverage: number;
  leverage_30d_avg: number;
  leverage_30d_max: number;
  net_exposure_pct: number;
  nav_guard_triggers: number;
}

export interface AnalyticsPayload {
  generated_at_utc: string;
  methodology?: Record<string, unknown>;
  leaderboard: LeaderboardRow[];
  curves: Record<string, AnalyticsCurve>;
  correlation: {
    ids: string[];
    labels?: Record<string, string>;
    matrix: Array<Array<number | null>>;
    overlap_days: number;
  };
  market_exposure: Record<
    string,
    {
      factor: string;
      factor_days: number;
      rows: OlsExposureRow[];
    }
  >;
  risk_exposure: {
    leverage_by_strategy: StrategyLeverageRow[];
    venue_concentration: RiskConcentrationRow[];
    asset_tier_concentration: RiskConcentrationRow[];
    total_nav_guard_triggers: number;
  };
}
