import { useCallback, useEffect, useMemo, useState } from "react";
import { AnalyticsRiskTab } from "./components/AnalyticsRiskTab";
import { EquityCurveChart, KappaCalibrationChart } from "./components/Charts";
import type { AnalyticsPayload, BuildInfo, CoinAttributionRow, OverviewPayload } from "./types";

type TabId = "overview" | "analytics" | "frontier" | "crossdex" | "onchain" | "execution" | "ops";

const TABS: Array<{ id: TabId; label: string }> = [
  { id: "overview", label: "Overview & Metamodel" },
  { id: "analytics", label: "Analytics & Risk" },
  { id: "frontier", label: "HIP-3 · Boros · Yield" },
  { id: "crossdex", label: "5-DEX & Paper" },
  { id: "onchain", label: "On-Chain Alpha" },
  { id: "execution", label: "Testnet & MM" },
  { id: "ops", label: "Scheduler & Ops" },
];

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function fmtUsd(v: unknown, digits = 2): string {
  const n = num(v);
  if (n === null) return "—";
  if (Math.abs(n) >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(2)}B`;
  if (Math.abs(n) >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function fmtPct(v: unknown, digits = 2, signed = true): string {
  const n = num(v);
  if (n === null) return "—";
  return `${signed && n >= 0 ? "+" : ""}${n.toFixed(digits)}%`;
}

function fmtBps(v: unknown, digits = 1): string {
  const n = num(v);
  if (n === null) return "—";
  return `${n.toFixed(digits)} bps`;
}

function shortSha(sha?: string | null): string {
  if (!sha) return "dev";
  return sha.length > 8 ? sha.slice(0, 8) : sha;
}

export default function App() {
  const [tab, setTab] = useState<TabId>("overview");
  const [data, setData] = useState<OverviewPayload | null>(null);
  const [analytics, setAnalytics] = useState<AnalyticsPayload | null>(null);
  const [feBuild, setFeBuild] = useState<BuildInfo | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshSec, setRefreshSec] = useState<number>(60);

  // Interactive filters
  const [steadyOnly, setSteadyOnly] = useState<boolean>(false);
  const [activeOnly, setActiveOnly] = useState<boolean>(true);
  const [hip3Query, setHip3Query] = useState<string>("");
  const [posQuery, setPosQuery] = useState<string>("");
  const [crossDexMinApr, setCrossDexMinApr] = useState<number>(0);
  const [settlementSplit, setSettlementSplit] = useState<string>("OOS");

  const loadAll = useCallback(async () => {
    try {
      setError(null);
      const [ovRes, feRes, anRes] = await Promise.all([
        fetch("/api/v1/overview", { cache: "no-store" }),
        fetch("/build-info.json", { cache: "no-store" }).catch(() => null),
        fetch("/api/v1/analytics", { cache: "no-store" }).catch(() => null),
      ]);
      if (!ovRes.ok) {
        throw new Error(`Backend /api/v1/overview returned HTTP ${ovRes.status}`);
      }
      const ovJson = (await ovRes.json()) as OverviewPayload;
      setData(ovJson);
      if (anRes && anRes.ok) {
        setAnalytics((await anRes.json()) as AnalyticsPayload);
      }
      if (feRes && feRes.ok) {
        setFeBuild((await feRes.json()) as BuildInfo);
      } else {
        setFeBuild({
          service: "hl-frontend",
          sha: (import.meta.env.VITE_BUILD_SHA as string | undefined) || "dev",
          built_at: (import.meta.env.VITE_BUILT_AT as string | undefined) || "",
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  useEffect(() => {
    if (refreshSec <= 0) return;
    const id = window.setInterval(() => {
      void loadAll();
    }, refreshSec * 1000);
    return () => window.clearInterval(id);
  }, [refreshSec, loadAll]);

  const hip3PersistenceFiltered = useMemo(() => {
    const rows = data?.frontier_history?.hip3_persistence ?? [];
    const q = hip3Query.trim().toUpperCase();
    return rows.filter((r) => {
      if (activeOnly && r.active_in_latest_tick === false) return false;
      if (steadyOnly && ((r.direction_flips ?? 0) > 0 || r.clean_streak < 1)) return false;
      if (q && !r.base_symbol.toUpperCase().includes(q)) return false;
      return true;
    });
  }, [data, activeOnly, steadyOnly, hip3Query]);

  const paperPositionsFiltered = useMemo(() => {
    const rows = data?.paper_positions ?? [];
    const q = posQuery.trim().toUpperCase();
    return q ? rows.filter((r) => r.coin.toUpperCase().includes(q)) : rows;
  }, [data, posQuery]);

  const perCoinAttribution = useMemo(() => {
    const raw = data?.attribution?.per_coin;
    return Array.isArray(raw) ? (raw as CoinAttributionRow[]) : [];
  }, [data]);

  const crossDexFiltered = useMemo(() => {
    return (data?.cross_dex_all ?? []).filter((r) => r.spread_apr_pct >= crossDexMinApr);
  }, [data, crossDexMinApr]);

  const settlementFiltered = useMemo(() => {
    const rows = data?.settlement_stats ?? [];
    if (settlementSplit === "ALL") return rows.slice(0, 20);
    return rows.filter((r) => String(r.split).toUpperCase() === settlementSplit).slice(0, 20);
  }, [data, settlementSplit]);

  const beSha = data?.build_info?.sha ?? "dev";
  const feSha = feBuild?.sha ?? "dev";
  const shaAligned = beSha === feSha || feSha === "dev" || beSha === "dev";
  const anyAlert = Boolean(data?.onchain_events?.any_alert);
  const topHip3 = data?.frontier_history?.hip3_persistence?.[0];
  const paperValues = (data?.paper_ticks ?? []).map((t) => t.equity_usd).filter((n) => Number.isFinite(n));

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <h1>Hyperliquid Quant DeFi · Live Desk</h1>
          <div className="brand-sub">
            {data ? `API snapshot: ${data.generated_at_utc}` : "Connecting to backend API..."}
            {data?.frontier_history?.ny_session ? ` · NY: ${data.frontier_history.ny_session}` : ""}
          </div>
        </div>
        <div className="controls">
          <span className={`pill ${anyAlert ? "pill-warn" : "pill-ok"}`}>
            {anyAlert ? "⚡ On-Chain Alerts Active" : "● Calm Regime"}
          </span>
          <span className={`pill ${shaAligned ? "pill-info" : "pill-warn"}`} title={`FE ${feSha} / BE ${beSha}`}>
            FE {shortSha(feSha)} · BE {shortSha(beSha)}
          </span>
          <select
            className="select"
            aria-label="Auto refresh interval"
            value={refreshSec}
            onChange={(e) => setRefreshSec(Number(e.target.value))}
          >
            <option value={30}>Auto 30s</option>
            <option value={60}>Auto 60s</option>
            <option value={0}>Manual</option>
          </select>
          <button type="button" className="btn" onClick={() => void loadAll()}>
            Refresh
          </button>
        </div>
      </header>

      {error && (
        <div className="error-box">
          <strong>Backend unreachable:</strong> {error}
        </div>
      )}

      <nav className="tabs" aria-label="Dashboard sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`tab ${tab === t.id ? "tab-active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {loading && !data ? (
        <div className="card muted">Loading Hyperliquid live desk telemetry...</div>
      ) : (
        <>
          {tab === "analytics" && <AnalyticsRiskTab analytics={analytics} overview={data} />}

          {tab === "overview" && (
            <div className="stack">
              <section className="kpi-grid">
                <div className="card">
                  <div className="kpi-label">Metamodel Paper Equity</div>
                  <div className="kpi-value">{fmtUsd(data?.paper?.equity_usd)}</div>
                  <div className="kpi-sub">
                    {fmtPct(data?.paper?.return_pct)} · {data?.paper?.active_positions ?? 0} pos ·{" "}
                    {data?.paper?.ticks ?? 0} ticks
                  </div>
                </div>
                <div className="card">
                  <div className="kpi-label">Testnet Live Equity</div>
                  <div className="kpi-value">{fmtUsd(data?.attribution?.equity_usd)}</div>
                  <div className="kpi-sub">
                    Maker {fmtPct(data?.attribution?.maker_volume_share_pct, 1, false)} · ALO Fill{" "}
                    {fmtPct(data?.attribution?.real_alo_fill_rate_pct, 1, false)}
                  </div>
                </div>
                <div className="card">
                  <div className="kpi-label">5-DEX Dual-Leg Paper</div>
                  <div className="kpi-value">{fmtUsd(data?.cross_dex_paper?.equity_usd)}</div>
                  <div className="kpi-sub">
                    Funding {fmtUsd(data?.cross_dex_paper?.cum_funding_usd)} ·{" "}
                    {Object.keys(data?.cross_dex_paper?.positions ?? {}).length} open pairs
                  </div>
                </div>
                <div className="card">
                  <div className="kpi-label">HIP-3 Clean Leader</div>
                  <div className="kpi-value">
                    {topHip3 ? `${topHip3.base_symbol} ${fmtPct(topHip3.latest_apr_pct, 1)}` : "—"}
                  </div>
                  <div className="kpi-sub">
                    {topHip3
                      ? `Clean streak ${topHip3.clean_streak} · Flips ${topHip3.direction_flips ?? 0} · Div ${fmtBps(topHip3.median_oracle_div_bps)}`
                      : "Awaiting persistence tick"}
                  </div>
                </div>
                <div className="card">
                  <div className="kpi-label">HLP Benchmark & TVL</div>
                  <div className="kpi-value">{fmtPct(data?.frontier_history?.hlp_apr_pct)}</div>
                  <div className="kpi-sub">TVL {fmtUsd(data?.onchain_events?.hlp_tvl_usd)}</div>
                </div>
                <div className="card">
                  <div className="kpi-label">AS-MM Engine Latency</div>
                  <div className="kpi-value">
                    {data?.latency_bench ? `${data.latency_bench.p50_us.toFixed(1)} µs` : "—"}
                  </div>
                  <div className="kpi-sub">
                    {data?.latency_bench
                      ? `p99 ${data.latency_bench.p99_us.toFixed(1)} µs · ${Math.round(data.latency_bench.throughput_ticks_per_sec).toLocaleString()} tps`
                      : "Benchmark not logged"}
                  </div>
                </div>
              </section>

              <div className="grid-2">
                <section className="card">
                  <div className="section-header">
                    <h2 className="section-title">Metamodel L2 Paper Equity Curve</h2>
                    <span className="section-sub">HL × Bybit Dollar-Neutral</span>
                  </div>
                  <EquityCurveChart values={paperValues} />
                </section>

                <section className="card">
                  <div className="section-header">
                    <h2 className="section-title">:00 Funding Settlement OOS Study</h2>
                    <div className="filter-row">
                      <span className="section-sub">
                        {data?.settlement_meta?.span_days ?? "—"}d ·{" "}
                        {data?.settlement_meta?.total_settlement_events?.toLocaleString() ?? "—"} events
                      </span>
                      <select
                        className="select"
                        value={settlementSplit}
                        onChange={(e) => setSettlementSplit(e.target.value)}
                      >
                        <option value="OOS">OOS only</option>
                        <option value="IS">IS only</option>
                        <option value="ALL">All splits</option>
                      </select>
                    </div>
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Strategy</th>
                          <th>Bucket</th>
                          <th className="num">Events</th>
                          <th className="num">Gross</th>
                          <th className="num">t-stat</th>
                          <th className="num">Net Maker</th>
                        </tr>
                      </thead>
                      <tbody>
                        {settlementFiltered.slice(0, 8).map((row, idx) => (
                          <tr key={idx}>
                            <td>{row.strategy}</td>
                            <td className="mono">{row.bucket}</td>
                            <td className="num">{row.events}</td>
                            <td className={`num ${row.mean_gross_bps >= 0 ? "pos" : "neg"}`}>
                              {fmtBps(row.mean_gross_bps, 2)}
                            </td>
                            <td className="num">{row.t_stat.toFixed(2)}</td>
                            <td className={`num ${row.net_maker_bps >= 0 ? "pos" : "neg"}`}>
                              {fmtBps(row.net_maker_bps, 2)}
                            </td>
                          </tr>
                        ))}
                        {settlementFiltered.length === 0 && (
                          <tr>
                            <td colSpan={6} className="muted">
                              No settlement study rows loaded yet.
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </section>
              </div>

              <section className="card">
                <div className="section-header">
                  <div>
                    <h2 className="section-title">Metamodel Open Positions (Live Paper Ledger)</h2>
                    <div className="section-sub">
                      {data?.paper_positions?.length ?? 0} active HL × CEX positions sorted by |HL weight|
                    </div>
                  </div>
                  <input
                    className="input"
                    placeholder="Filter coin (e.g. BTC, ETH)"
                    value={posQuery}
                    onChange={(e) => setPosQuery(e.target.value)}
                  />
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Coin</th>
                        <th className="num">HL Weight</th>
                        <th>CEX Venue</th>
                        <th className="num">CEX Weight</th>
                        <th className="num">Last HL Px</th>
                        <th className="num">L2 Spread</th>
                        <th className="num">Cum PnL</th>
                      </tr>
                    </thead>
                    <tbody>
                      {paperPositionsFiltered.map((p) => (
                        <tr key={p.coin}>
                          <td>
                            <strong>{p.coin}</strong>
                          </td>
                          <td className={`num ${p.hl_weight >= 0 ? "pos" : "neg"}`}>
                            {fmtPct(p.hl_weight * 100, 2)}
                          </td>
                          <td>
                            {p.cex_venue ? <span className="badge badge-cyan">{p.cex_venue}</span> : <span className="muted">HL-only</span>}
                          </td>
                          <td className="num">{p.cex_weight ? fmtPct(p.cex_weight * 100, 2) : "—"}</td>
                          <td className="num">{fmtUsd(p.last_hl_px, 4)}</td>
                          <td className="num">{fmtBps(p.l2_spread_bps, 2)}</td>
                          <td className={`num ${p.cum_pnl_usd >= 0 ? "pos" : "neg"}`}>
                            {fmtUsd(p.cum_pnl_usd)}
                          </td>
                        </tr>
                      ))}
                      {paperPositionsFiltered.length === 0 && (
                        <tr>
                          <td colSpan={7} className="muted">
                            No active metamodel positions matching filter.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </section>
            </div>
          )}

          {tab === "frontier" && (
            <div className="stack">
              <section className="card">
                <div className="section-header">
                  <div>
                    <h2 className="section-title">HIP-3 Split-Margin Paper Ledger</h2>
                    <div className="section-sub">
                      Zero-bridge cross-builder L2 paper (Growth Mode taker fees, independent DEX margin, Oracle Div ≤ 15 bps, half-sized onlyIsolated)
                    </div>
                  </div>
                  <div className="mono muted">
                    {data?.hip3_paper?.observed_at_utc ?? "awaiting first tick"}
                    {data?.hip3_paper?.ny_session ? ` · ${data.hip3_paper.ny_session}` : ""}
                  </div>
                </div>
                <div className="kpi-grid">
                  <div className="card">
                    <div className="kpi-label">HIP-3 Paper Equity</div>
                    <div className="kpi-value">{fmtUsd(data?.hip3_paper?.equity_usd)}</div>
                    <div
                      className={`kpi-sub ${(data?.hip3_paper?.return_pct ?? 0) >= 0 ? "pos" : "neg"}`}
                    >
                      {data?.hip3_paper ? fmtPct(data.hip3_paper.return_pct, 3) : "—"}
                    </div>
                  </div>
                  <div className="card">
                    <div className="kpi-label">Split-Margin Locked</div>
                    <div className="kpi-value">{fmtUsd(data?.hip3_paper?.margin_locked_usd)}</div>
                    <div className="kpi-sub">
                      {data?.hip3_paper
                        ? `${data.hip3_paper.margin_utilization_pct.toFixed(1)}% util · ${Object.keys(data.hip3_paper.positions ?? {}).length} open pairs`
                        : "—"}
                    </div>
                  </div>
                  <div className="card">
                    <div className="kpi-label">Funding / Basis / Fees</div>
                    <div className="kpi-value">{fmtUsd(data?.hip3_paper?.cum_funding_usd)}</div>
                    <div className="kpi-sub">
                      Basis {fmtUsd(data?.hip3_paper?.cum_basis_mtm_usd)} · Fees −
                      {fmtUsd(data?.hip3_paper?.cum_costs_usd)}
                    </div>
                  </div>
                  <div className="card">
                    <div className="kpi-label">Closed &amp; Payback</div>
                    <div className="kpi-value">{data?.hip3_paper?.closed_pairs_count ?? 0}</div>
                    <div className="kpi-sub">
                      Payback achieved: {data?.hip3_paper?.payback_achieved_count ?? 0}
                    </div>
                  </div>
                </div>
                <div className="table-wrap" style={{ marginTop: "0.75rem" }}>
                  <table>
                    <thead>
                      <tr>
                        <th>Ticker</th>
                        <th>Legs</th>
                        <th>Mode</th>
                        <th className="num">Leg Notional</th>
                        <th className="num">Live APR</th>
                        <th className="num">Oracle Div</th>
                        <th className="num">Hours</th>
                        <th className="num">Funding</th>
                        <th className="num">Basis MTM</th>
                        <th className="num">Net PnL</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.values(data?.hip3_paper?.positions ?? {}).map((p) => (
                        <tr key={p.base_symbol}>
                          <td>
                            <strong>{p.base_symbol}</strong>
                          </td>
                          <td className="mono">{`S ${p.short_symbol} / L ${p.long_symbol}`}</td>
                          <td>
                            <span className={`badge ${p.both_growth_mode ? "badge-cyan" : ""}`}>
                              {p.both_growth_mode ? "⚡ GM" : "Std"}
                            </span>{" "}
                            <span className={`badge ${p.any_only_isolated ? "badge-warn" : "badge-ok"}`}>
                              {p.any_only_isolated ? "Isolated" : "Cross"}
                            </span>
                          </td>
                          <td className="num">{fmtUsd(p.leg_notional_usd)}</td>
                          <td className="num pos">{fmtPct(p.live_spread_apr_pct, 1)}</td>
                          <td className={`num ${p.live_oracle_div_bps > 15 ? "warn" : ""}`}>
                            {fmtBps(p.live_oracle_div_bps)}
                          </td>
                          <td className="num">{p.hours_held.toFixed(1)}h</td>
                          <td className={`num ${p.cum_funding_usd >= 0 ? "pos" : "neg"}`}>
                            {fmtUsd(p.cum_funding_usd)}
                          </td>
                          <td className={`num ${p.cum_basis_mtm_usd >= 0 ? "pos" : "neg"}`}>
                            {fmtUsd(p.cum_basis_mtm_usd)}
                          </td>
                          <td className={`num ${p.net_pnl_usd >= 0 ? "pos" : "neg"}`}>
                            <strong>{fmtUsd(p.net_pnl_usd)}</strong>
                          </td>
                        </tr>
                      ))}
                      {Object.keys(data?.hip3_paper?.positions ?? {}).length === 0 && (
                        <tr>
                          <td colSpan={10} className="muted">
                            No open HIP-3 paper pairs yet (waiting for clean_streak ≥ 2 with Oracle Div ≤ 15 bps).
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="card">
                <div className="section-header">
                  <div>
                    <h2 className="section-title">HIP-3 Cross-Builder Spread Persistence</h2>
                    <div className="section-sub">
                      Clean streak requires APR ≥ 20%, Oracle Div ≤ 25 bps, unchanged legs, and no missing ticks
                    </div>
                  </div>
                  <div className="filter-row">
                    <input
                      className="input"
                      placeholder="Filter ticker (e.g. AAPL, NVDA)"
                      value={hip3Query}
                      onChange={(e) => setHip3Query(e.target.value)}
                    />
                    <label className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={activeOnly}
                        onChange={(e) => setActiveOnly(e.target.checked)}
                      />
                      Active only
                    </label>
                    <label className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={steadyOnly}
                        onChange={(e) => setSteadyOnly(e.target.checked)}
                      />
                      Only steady (0 flips)
                    </label>
                  </div>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Ticker</th>
                        <th>Status</th>
                        <th>Legs</th>
                        <th className="num">Ticks</th>
                        <th className="num">Raw ≥20%</th>
                        <th className="num">Clean Streak</th>
                        <th className="num">Flips</th>
                        <th className="num">Latest APR</th>
                        <th className="num">Median APR</th>
                        <th className="num">Oracle Div</th>
                        <th>Mode</th>
                      </tr>
                    </thead>
                    <tbody>
                      {hip3PersistenceFiltered.map((r) => (
                        <tr key={r.base_symbol}>
                          <td>
                            <strong>{r.base_symbol}</strong>
                          </td>
                          <td>
                            <span className={`badge ${r.active_in_latest_tick === false ? "badge-warn" : "badge-ok"}`}>
                              {r.active_in_latest_tick === false
                                ? `Stale (-${r.ticks_since_last_seen ?? 1}t)`
                                : "Active"}
                            </span>
                          </td>
                          <td className="mono">{`S ${r.short_symbol} / L ${r.long_symbol}`}</td>
                          <td className="num">{r.ticks_seen}</td>
                          <td className="num">{r.raw_apr_streak}</td>
                          <td className="num">
                            <span className={`badge ${r.clean_streak >= 2 ? "badge-ok" : ""}`}>
                              {r.clean_streak}
                            </span>
                          </td>
                          <td className="num">
                            <span className={`badge ${(r.direction_flips ?? 0) > 0 ? "badge-warn" : "badge-ok"}`}>
                              {r.direction_flips ?? 0}
                            </span>
                          </td>
                          <td className="num pos">{fmtPct(r.latest_apr_pct, 1)}</td>
                          <td className="num">{fmtPct(r.median_apr_pct, 1)}</td>
                          <td className={`num ${r.median_oracle_div_bps > 15 ? "warn" : ""}`}>
                            {fmtBps(r.median_oracle_div_bps)}
                          </td>
                          <td>
                            <span className={`badge ${r.both_growth_mode ? "badge-cyan" : ""}`}>
                              {r.both_growth_mode ? "⚡ GM" : "Std"}
                            </span>
                          </td>
                        </tr>
                      ))}
                      {hip3PersistenceFiltered.length === 0 && (
                        <tr>
                          <td colSpan={11} className="muted">
                            No HIP-3 persistence rows matching filters.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </section>

              <div className="grid-2">
                <section className="card">
                  <div className="section-header">
                    <h2 className="section-title">Pendle Boros Fixed vs Floating Markets</h2>
                    <span className="section-sub">
                      {data?.boros?.hl_and_spread_count ?? 0} HL/Spread markets
                    </span>
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Market</th>
                          <th className="num">DTM</th>
                          <th className="num">Fixed Bid</th>
                          <th className="num">Float</th>
                          <th>Action</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(data?.boros?.markets ?? []).slice(0, 10).map((m) => (
                          <tr key={m.market_id}>
                            <td className="mono">{m.symbol}</td>
                            <td className="num">{m.days_to_maturity.toFixed(0)}d</td>
                            <td className="num">{fmtPct(m.best_bid_apr_pct, 1)}</td>
                            <td className="num">{fmtPct(m.floating_apr_pct, 1)}</td>
                            <td>
                              <span className="badge">{m.recommended_action}</span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>

                <section className="card">
                  <div className="section-header">
                    <h2 className="section-title">HyperEVM Composable Yield Stack</h2>
                    <span className="section-sub">vs HLP Benchmark</span>
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Strategy</th>
                          <th>Rate</th>
                          <th className="num">Net APR</th>
                          <th className="num">vs HLP</th>
                          <th className="num">Liq Buf</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(data?.yield_stack?.strategies ?? []).map((s, idx) => (
                          <tr key={idx}>
                            <td>{s.name}</td>
                            <td>
                              <span className="badge">{s.rate_type}</span>
                            </td>
                            <td className="num pos">{fmtPct(s.net_deposit_apr_pct, 2)}</td>
                            <td className={`num ${s.spread_vs_hlp_pct >= 0 ? "pos" : "neg"}`}>
                              {fmtPct(s.spread_vs_hlp_pct, 2)}
                            </td>
                            <td className="num">{s.liq_buffer_pct.toFixed(0)}%</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              </div>
            </div>
          )}

          {tab === "crossdex" && (
            <div className="stack">
              <section className="kpi-grid">
                <div className="card">
                  <div className="kpi-label">Dual-Leg Paper Equity</div>
                  <div className="kpi-value">{fmtUsd(data?.cross_dex_paper?.equity_usd)}</div>
                  <div className="kpi-sub">Start {fmtUsd(data?.cross_dex_paper?.starting_capital_usd)}</div>
                </div>
                <div className="card">
                  <div className="kpi-label">Cumulative Funding</div>
                  <div className="kpi-value pos">{fmtUsd(data?.cross_dex_paper?.cum_funding_usd)}</div>
                  <div className="kpi-sub">Basis MTM {fmtUsd(data?.cross_dex_paper?.cum_basis_mtm_usd)}</div>
                </div>
                <div className="card">
                  <div className="kpi-label">Execution Costs</div>
                  <div className="kpi-value warn">{fmtUsd(data?.cross_dex_paper?.cum_costs_usd)}</div>
                  <div className="kpi-sub">
                    Closed {data?.cross_dex_paper?.closed_pairs_count ?? 0} · Payback{" "}
                    {data?.cross_dex_paper?.payback_achieved_count ?? 0}
                  </div>
                </div>
              </section>

              <section className="card">
                <div className="section-header">
                  <h2 className="section-title">Open Dual-Leg Paper Positions</h2>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Coin</th>
                        <th>Direction</th>
                        <th className="num">Leg Size</th>
                        <th className="num">Hours Held</th>
                        <th className="num">Entry APR</th>
                        <th className="num">Live APR</th>
                        <th className="num">Entry Basis</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.values(data?.cross_dex_paper?.positions ?? {}).map((p) => (
                        <tr key={p.coin}>
                          <td>
                            <strong>{p.coin}</strong>
                          </td>
                          <td className="mono">{p.direction}</td>
                          <td className="num">{fmtUsd(p.leg_notional_usd, 0)}</td>
                          <td className="num">{p.hours_held.toFixed(1)}h</td>
                          <td className="num">{fmtPct(p.entry_spread_apr_pct, 1)}</td>
                          <td className={`num ${p.live_spread_apr_pct >= 10 ? "pos" : "warn"}`}>
                            {fmtPct(p.live_spread_apr_pct, 1)}
                          </td>
                          <td className="num">{fmtBps(p.entry_exec_basis_bps, 2)}</td>
                        </tr>
                      ))}
                      {Object.keys(data?.cross_dex_paper?.positions ?? {}).length === 0 && (
                        <tr>
                          <td colSpan={7} className="muted">
                            No open dual-leg paper positions.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="card">
                <div className="section-header">
                  <h2 className="section-title">5-DEX Live Funding & Basis Opportunities</h2>
                  <div className="filter-row">
                    <span className="section-sub">Min Spread APR:</span>
                    <select
                      className="select"
                      value={crossDexMinApr}
                      onChange={(e) => setCrossDexMinApr(Number(e.target.value))}
                    >
                      <option value={0}>All</option>
                      <option value={15}>≥ 15%</option>
                      <option value={30}>≥ 30%</option>
                      <option value={50}>≥ 50%</option>
                    </select>
                  </div>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Coin</th>
                        <th>Venue</th>
                        <th>Direction</th>
                        <th className="num">HL APR</th>
                        <th className="num">Other APR</th>
                        <th className="num">Spread APR</th>
                        <th className="num">Fav Basis</th>
                        <th className="num">Breakeven</th>
                      </tr>
                    </thead>
                    <tbody>
                      {crossDexFiltered.map((o, idx) => (
                        <tr key={idx}>
                          <td>
                            <strong>{o.coin}</strong>
                          </td>
                          <td>
                            <span className="badge badge-cyan">{o.other_dex}</span>
                          </td>
                          <td className="mono">{o.direction}</td>
                          <td className="num">{fmtPct(o.hl_apr_pct, 1)}</td>
                          <td className="num">{fmtPct(o.other_apr_pct, 1)}</td>
                          <td className="num pos">{fmtPct(o.spread_apr_pct, 1)}</td>
                          <td className="num">{fmtBps(o.favorable_basis_bps, 1)}</td>
                          <td className="num">{o.breakeven_hours.toFixed(1)}h</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            </div>
          )}

          {tab === "onchain" && (
            <div className="stack">
              {anyAlert && (
                <div className="alert-banner">
                  <strong>Active On-Chain Alerts ({data?.onchain_events?.observed_at_utc}):</strong>{" "}
                  <span className="mono">
                    {(data?.onchain_events?.active_alert_keys ?? []).join(" · ") || "Threshold active"}
                  </span>
                </div>
              )}

              <section className="kpi-grid">
                <div className="card">
                  <div className="kpi-label">HLP Vault TVL</div>
                  <div className="kpi-value">{fmtUsd(data?.onchain_events?.hlp_tvl_usd)}</div>
                  <div className="kpi-sub">
                    Source: {String(data?.onchain_events?.hlp_mm_pnl?.pnl_source ?? "—")}
                  </div>
                </div>
                <div className="card">
                  <div className="kpi-label">HLP Deposit-Clean ΔPnL</div>
                  <div className="kpi-value">
                    {fmtUsd(data?.onchain_events?.hlp_mm_pnl?.hlp_pnl_delta_usd)}
                  </div>
                  <div className="kpi-sub">
                    Regime: {String(data?.onchain_events?.hlp_mm_pnl?.mm_pnl_regime ?? "—")}
                  </div>
                </div>
                <div className="card">
                  <div className="kpi-label">HyperEVM Chain 999</div>
                  <div className="kpi-value">
                    {data?.hyperevm?.chain?.block_number
                      ? `#${Number(data.hyperevm.chain.block_number).toLocaleString()}`
                      : "—"}
                  </div>
                  <div className="kpi-sub">
                    USDT0 peg disc {fmtBps(data?.hyperevm?.chain?.usdt0_peg_discount_bps, 2)}
                  </div>
                </div>
                <div className="card">
                  <div className="kpi-label">Whales Scanned (H2)</div>
                  <div className="kpi-value">{data?.liq_funding?.whales_scanned ?? "—"}</div>
                  <div className="kpi-sub">
                    With positions: {data?.liq_funding?.whales_with_positions ?? "—"}
                  </div>
                </div>
              </section>

              <div className="grid-2">
                <section className="card">
                  <div className="section-header">
                    <h2 className="section-title">H2 Whale Liquidation Clusters</h2>
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Coin</th>
                          <th className="num">Mark</th>
                          <th className="num">Total 0–10%</th>
                          <th className="num">Nearest L</th>
                          <th className="num">Nearest S</th>
                          <th>Bias</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(data?.liq_funding?.clusters ?? []).map((c, idx) => (
                          <tr key={idx}>
                            <td>
                              <strong>{String(c.coin)}</strong>
                            </td>
                            <td className="num">{fmtUsd(c.mark_px)}</td>
                            <td className="num warn">{fmtUsd(c.total_0_10_usd, 0)}</td>
                            <td className="num">{fmtPct(c.nearest_long_dist_pct, 1, false)}</td>
                            <td className="num">{fmtPct(c.nearest_short_dist_pct, 1, false)}</td>
                            <td>
                              <span className="badge">{String(c.cascade_bias ?? "—")}</span>
                            </td>
                          </tr>
                        ))}
                        {(data?.liq_funding?.clusters ?? []).length === 0 && (
                          <tr>
                            <td colSpan={6} className="muted">
                              No dense liquidation walls detected in current scan.
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </section>

                <section className="card">
                  <div className="section-header">
                    <h2 className="section-title">H4 HyperEVM L1Read Oracles</h2>
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Coin</th>
                          <th className="num">L1Read Mark</th>
                          <th className="num">REST Mark</th>
                          <th className="num">Drift</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(data?.hyperevm?.l1_read_oracles ?? []).map((o, idx) => (
                          <tr key={idx}>
                            <td>
                              <strong>{String(o.coin)}</strong>
                            </td>
                            <td className="num">{fmtUsd(o.l1read_mark_px)}</td>
                            <td className="num">{fmtUsd(o.rest_mark_px)}</td>
                            <td className={`num ${Math.abs(num(o.drift_bps) ?? 0) > 15 ? "warn" : "pos"}`}>
                              {fmtBps(o.drift_bps, 2)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              </div>
            </div>
          )}

          {tab === "execution" && (
            <div className="stack">
              <section className="kpi-grid">
                <div className="card">
                  <div className="kpi-label">Testnet Equity</div>
                  <div className="kpi-value">{fmtUsd(data?.attribution?.equity_usd)}</div>
                  <div className="kpi-sub">
                    Open pos: {String(data?.attribution?.open_positions_count ?? "—")} · Fills:{" "}
                    {String(data?.attribution?.total_fills ?? "—")}
                  </div>
                </div>
                <div className="card">
                  <div className="kpi-label">Maker Share & ALO Fill</div>
                  <div className="kpi-value pos">
                    {fmtPct(data?.attribution?.maker_volume_share_pct, 1, false)}
                  </div>
                  <div className="kpi-sub">
                    ALO fill {fmtPct(data?.attribution?.real_alo_fill_rate_pct, 1, false)} · Fee{" "}
                    {fmtBps(data?.attribution?.effective_fee_bps, 2)}
                  </div>
                </div>
                <div className="card">
                  <div className="kpi-label">Net Attributed PnL</div>
                  <div className="kpi-value">{fmtUsd(data?.attribution?.net_attributed_pnl_usd)}</div>
                  <div className="kpi-sub">
                    VW Markout {fmtBps(data?.attribution?.vw_markout_bps, 2)}
                  </div>
                </div>
                <div className="card">
                  <div className="kpi-label">API Quota & Min Liq Dist</div>
                  <div className="kpi-value">
                    {String(data?.attribution?.api_quota_health ?? "—")}
                  </div>
                  <div className="kpi-sub">
                    Min liq dist {fmtPct(data?.attribution?.min_liq_distance_pct, 1, false)} · Volume{" "}
                    {fmtUsd(data?.attribution?.total_volume_usd)}
                  </div>
                </div>
              </section>

              <section className="card">
                <div className="section-header">
                  <h2 className="section-title">Per-Coin Testnet Attribution, Liq Distance & ADL Rank</h2>
                  <span className="section-sub">{perCoinAttribution.length} tracked coins</span>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Coin</th>
                        <th className="num">Position USD</th>
                        <th>Leverage</th>
                        <th className="num">Fills</th>
                        <th className="num">Maker %</th>
                        <th className="num">Net PnL</th>
                        <th className="num">VW Markout</th>
                        <th className="num">Liq Dist</th>
                        <th className="num">ADL Rank</th>
                      </tr>
                    </thead>
                    <tbody>
                      {perCoinAttribution.map((c) => (
                        <tr key={c.coin}>
                          <td>
                            <strong>{c.coin}</strong>
                          </td>
                          <td className={`num ${c.position_sz >= 0 ? "pos" : "neg"}`}>
                            {c.position_sz >= 0 ? "L " : "S "}
                            {fmtUsd(c.position_usd)}
                          </td>
                          <td className="mono">{c.leverage_mode}</td>
                          <td className="num">{c.fills_count}</td>
                          <td className="num">{fmtPct(c.maker_share_pct, 0, false)}</td>
                          <td className={`num ${c.net_pnl_usd >= 0 ? "pos" : "neg"}`}>{fmtUsd(c.net_pnl_usd)}</td>
                          <td className="num">{fmtBps(c.vw_markout_bps, 1)}</td>
                          <td className={`num ${(c.liq_distance_pct ?? 999) < 20 ? "warn" : ""}`}>
                            {c.liq_distance_pct !== null ? fmtPct(c.liq_distance_pct, 1, false) : "—"}
                          </td>
                          <td className="num">
                            {c.adl_rank_score !== null ? (
                              <span className={`badge ${c.adl_rank_score >= 2.0 ? "badge-warn" : "badge-ok"}`}>
                                {c.adl_rank_score.toFixed(2)}
                              </span>
                            ) : (
                              "—"
                            )}
                          </td>
                        </tr>
                      ))}
                      {perCoinAttribution.length === 0 && (
                        <tr>
                          <td colSpan={9} className="muted">
                            No per-coin attribution snapshot yet.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="card">
                <div className="section-header">
                  <h2 className="section-title">Recent Testnet ALO Rebalance Cycles</h2>
                  <span className="section-sub">Newest first from hl_testnet_audit.jsonl</span>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Time (UTC)</th>
                        <th>Status</th>
                        <th className="num">Cancelled</th>
                        <th className="num">Placed ALO</th>
                        <th className="num">Notional</th>
                        <th className="num">HWM</th>
                        <th>Violations</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(data?.testnet_audit_recent ?? []).map((rec, idx) => (
                        <tr key={idx}>
                          <td className="mono">{rec.observed_at_utc ?? "—"}</td>
                          <td>
                            <span className={`badge ${rec.status === "ok" ? "badge-ok" : "badge-danger"}`}>
                              {rec.status}
                            </span>
                          </td>
                          <td className="num">{rec.cancelled_count}</td>
                          <td className="num">{rec.placed_count}</td>
                          <td className="num">{fmtUsd(rec.total_notional_usd)}</td>
                          <td className="num">{fmtUsd(rec.high_water_mark_usd)}</td>
                          <td className="mono">
                            {[
                              rec.violations.length ? rec.violations.join(", ") : "",
                              rec.skipped_unquoted_coins?.length
                                ? `skipped (no BBO): ${rec.skipped_unquoted_coins.join(", ")}`
                                : "",
                            ]
                              .filter(Boolean)
                              .join(" · ") || "—"}
                          </td>
                        </tr>
                      ))}
                      {(data?.testnet_audit_recent ?? []).length === 0 && (
                        <tr>
                          <td colSpan={7} className="muted">
                            No rebalance audit records yet.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </section>

              <div className="grid-2">
                {Object.values(data?.mm_calibration ?? {}).map((cal) => (
                  <section className="card" key={cal.coin}>
                    <div className="section-header">
                      <h2 className="section-title">
                        Avellaneda–Stoikov Empirical κ Calibration ({cal.coin})
                      </h2>
                      <span className="badge badge-cyan">R² = {cal.r2_fit.toFixed(3)}</span>
                    </div>
                    <KappaCalibrationChart cal={cal} />
                    <div className="kpi-sub" style={{ marginTop: 8 }}>
                      κ = {cal.kappa_per_bp.toFixed(3)}/bp · σ_τ = {cal.empirical_sigma_tau_bps.toFixed(2)} bps ·
                      Min half-spread = {cal.implied_min_half_spread_bps.toFixed(2)} bps · {cal.trades_count} trades
                    </div>
                  </section>
                ))}
              </div>
            </div>
          )}

          {tab === "ops" && (
            <div className="stack">
              <section className="kpi-grid">
                <div className="card">
                  <div className="kpi-label">Frontend Build SHA</div>
                  <div className="kpi-value">{shortSha(feSha)}</div>
                  <div className="kpi-sub">{feBuild?.built_at || "local / dev"}</div>
                </div>
                <div className="card">
                  <div className="kpi-label">Backend Build SHA</div>
                  <div className="kpi-value">{shortSha(beSha)}</div>
                  <div className="kpi-sub">{data?.build_info?.built_at || "local / dev"}</div>
                </div>
                <div className="card">
                  <div className="kpi-label">Worker Heartbeat</div>
                  <div className="kpi-value">
                    {data?.worker_status ? "ONLINE" : "EXTERNAL / CRON"}
                  </div>
                  <div className="kpi-sub">
                    {data?.worker_status?.heartbeat_utc ?? "Run hl-worker container to report heartbeat"}
                  </div>
                </div>
                <div className="card">
                  <div className="kpi-label">Testnet Execution Gate</div>
                  <div className="kpi-value">
                    {data?.worker_status?.testnet_execution_enabled ? "ENABLED" : "READ-ONLY"}
                  </div>
                  <div className="kpi-sub">
                    Telegram: {data?.worker_status?.telegram_enabled ? "ON" : "OFF"}
                  </div>
                </div>
              </section>

              <section className="card">
                <div className="section-header">
                  <h2 className="section-title">Docker Worker Scheduled Jobs (UTC)</h2>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Job</th>
                        <th>Cron (UTC)</th>
                        <th>State</th>
                        <th>Last Finished</th>
                        <th className="num">Duration</th>
                        <th className="num">OK / Fail</th>
                        <th>Next Run</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(data?.worker_status?.jobs ?? []).map((j) => (
                        <tr key={j.name}>
                          <td>
                            <strong>{j.name}</strong>
                            <div className="section-sub">{j.description}</div>
                          </td>
                          <td className="mono">{j.cron_expr}</td>
                          <td>
                            <span
                              className={`badge ${
                                j.state === "ok"
                                  ? "badge-ok"
                                  : j.state === "failed"
                                    ? "badge-danger"
                                    : j.state === "running"
                                      ? "badge-cyan"
                                      : "badge-warn"
                              }`}
                            >
                              {j.state}
                            </span>
                            {!j.enabled && <div className="section-sub">{j.status_reason}</div>}
                          </td>
                          <td className="mono">{j.last_finished_at_utc ?? "—"}</td>
                          <td className="num">
                            {j.last_duration_s !== null ? `${j.last_duration_s.toFixed(1)}s` : "—"}
                          </td>
                          <td className="num">
                            {j.runs_ok} / {j.runs_failed}
                          </td>
                          <td className="mono">{j.next_run_utc}</td>
                        </tr>
                      ))}
                      {(data?.worker_status?.jobs ?? []).length === 0 && (
                        <tr>
                          <td colSpan={7} className="muted">
                            No container worker heartbeat yet (expected when previewing local data without worker).
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="card">
                <div className="section-header">
                  <h2 className="section-title">Shared Volume Artifact Freshness (/data/paper)</h2>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Artifact</th>
                        <th>Path</th>
                        <th>Status</th>
                        <th>Updated (UTC)</th>
                        <th className="num">Age (min)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(data?.artifact_freshness ?? {}).map(([key, info]) => (
                        <tr key={key}>
                          <td>
                            <strong>{key}</strong>
                          </td>
                          <td className="mono">{info.path}</td>
                          <td>
                            <span className={`badge ${info.exists ? "badge-ok" : "badge-warn"}`}>
                              {info.exists ? "present" : "pending"}
                            </span>
                          </td>
                          <td className="mono">{info.updated_at_utc ?? "—"}</td>
                          <td className="num">{info.age_minutes !== null ? info.age_minutes.toFixed(1) : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            </div>
          )}
        </>
      )}
    </div>
  );
}
