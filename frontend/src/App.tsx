import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnalyticsRiskTab } from "./components/AnalyticsRiskTab";
import { EquityCurveChart, KappaCalibrationChart } from "./components/Charts";
import { KpiLabel, SectionTitle, Th, Tip } from "./components/Tooltip";
import { LangProvider, LangToggle, useLang, type Lang } from "./i18n";
import type { AnalyticsPayload, BuildInfo, CoinAttributionRow, OverviewPayload } from "./types";

type TabId = "overview" | "analytics" | "frontier" | "crossdex" | "onchain" | "execution" | "ops";

const TABS: Array<{ id: TabId; en: string; ru: string }> = [
  { id: "overview", en: "Overview & Metamodel", ru: "Обзор и метамодель" },
  { id: "analytics", en: "Analytics & Risk", ru: "Аналитика и риски" },
  { id: "frontier", en: "HIP-3 · Boros · Yield", ru: "HIP-3 · Boros · Доходность" },
  { id: "crossdex", en: "5-DEX & Paper", ru: "5-DEX и paper" },
  { id: "onchain", en: "On-Chain Alpha", ru: "On-chain альфа" },
  { id: "execution", en: "Testnet & MM", ru: "Testnet и MM" },
  { id: "ops", en: "Scheduler & Ops", ru: "Планировщик и Ops" },
];

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function fx(v: unknown, digits = 2): string {
  const n = num(v);
  return n === null ? "—" : n.toFixed(digits);
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

const CASCADE_BIAS_LABELS: Record<string, Record<Lang, string>> = {
  downside_cascade: { en: "Downside Cascade (long liquidations)", ru: "Каскад вниз (ликвидации лонгов)" },
  upside_squeeze: { en: "Upside Squeeze (short liquidations)", ru: "Сквиз вверх (ликвидации шортов)" },
  balanced: { en: "Balanced", ru: "Сбалансировано" },
};

function cascadeBiasLabel(bias: unknown, lang: Lang): string {
  if (bias === null || bias === undefined || bias === "") return "—";
  const key = String(bias);
  return CASCADE_BIAS_LABELS[key]?.[lang] ?? key;
}

export default function App() {
  return (
    <LangProvider>
      <Desk />
    </LangProvider>
  );
}

function Desk() {
  const { lang, t, p, g } = useLang();
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
  const reqSeqRef = useRef<number>(0);

  const loadAll = useCallback(async () => {
    const seq = ++reqSeqRef.current;
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
      // Optional payloads: a non-JSON body (e.g. Vite's SPA index.html fallback for /build-info.json in dev)
      // must not take down the whole dashboard.
      const anJson = anRes && anRes.ok ? ((await anRes.json().catch(() => null)) as AnalyticsPayload | null) : null;
      const feJson = feRes && feRes.ok ? ((await feRes.json().catch(() => null)) as BuildInfo | null) : null;
      if (seq !== reqSeqRef.current) return;
      setData(ovJson);
      if (anJson) {
        setAnalytics(anJson);
      }
      if (feJson) {
        setFeBuild(feJson);
      } else {
        setFeBuild({
          service: "hl-frontend",
          sha: (import.meta.env.VITE_BUILD_SHA as string | undefined) || "dev",
          built_at: (import.meta.env.VITE_BUILT_AT as string | undefined) || "",
        });
      }
    } catch (err) {
      if (seq !== reqSeqRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (seq === reqSeqRef.current) {
        setLoading(false);
      }
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
    return rows
      .filter((r) => {
        const sp = String(r.split ?? "").trim().toUpperCase();
        if (settlementSplit === "OOS") return sp === "OOS" || sp === "HOLDOUT";
        if (settlementSplit === "IS") return sp === "IS" || sp === "DEV";
        return sp === settlementSplit;
      })
      .slice(0, 20);
  }, [data, settlementSplit]);

  const beSha = data?.build_info?.sha ?? "dev";
  const feSha = feBuild?.sha ?? "dev";
  const shaAligned = beSha === feSha || feSha === "dev" || beSha === "dev";
  const anyAlert = Boolean(data?.onchain_events?.any_alert);
  const topHip3 = data?.frontier_history?.hip3_persistence?.[0];
  const paperValues = (data?.paper_ticks ?? []).map((t) => t.equity_usd).filter((n) => Number.isFinite(n));
  const hip3OpenPairs = Object.keys(data?.hip3_paper?.positions ?? {}).length;
  const crossDexOpenPairs = Object.keys(data?.cross_dex_paper?.positions ?? {}).length;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <h1>Hyperliquid Quant DeFi · Live Desk</h1>
          <div className="brand-sub">
            {data
              ? `${t("API snapshot", "Снимок API")}: ${data.generated_at_utc}`
              : t("Connecting to backend API...", "Подключение к API бэкенда...")}
            {data?.frontier_history?.ny_session ? ` · NY: ${data.frontier_history.ny_session}` : ""}
          </div>
        </div>
        <div className="controls">
          <LangToggle />
          <Tip text={g("pillAlerts").tip} className={`pill ${anyAlert ? "pill-warn" : "pill-ok"}`}>
            {anyAlert ? t("⚡ On-Chain Alerts Active", "⚡ Активны on-chain алерты") : t("● Calm Regime", "● Спокойный режим")}
          </Tip>
          <Tip
            text={`${g("pillSha").tip} (FE ${feSha} / BE ${beSha})`}
            className={`pill ${shaAligned ? "pill-info" : "pill-warn"}`}
          >
            FE {shortSha(feSha)} · BE {shortSha(beSha)}
          </Tip>
          <select
            className="select"
            aria-label={t("Auto refresh interval", "Интервал автообновления")}
            value={refreshSec}
            onChange={(e) => setRefreshSec(Number(e.target.value))}
          >
            <option value={30}>{t("Auto 30s", "Авто 30с")}</option>
            <option value={60}>{t("Auto 60s", "Авто 60с")}</option>
            <option value={0}>{t("Manual", "Вручную")}</option>
          </select>
          <button type="button" className="btn" onClick={() => void loadAll()}>
            {t("Refresh", "Обновить")}
          </button>
        </div>
      </header>

      {error && (
        <div className="error-box">
          <strong>{t("Backend unreachable:", "Бэкенд недоступен:")}</strong> {error}
        </div>
      )}

      <nav className="tabs" aria-label={t("Dashboard sections", "Разделы дашборда")}>
        {TABS.map((tb) => (
          <button
            key={tb.id}
            type="button"
            className={`tab ${tab === tb.id ? "tab-active" : ""}`}
            onClick={() => setTab(tb.id)}
          >
            {lang === "ru" ? tb.ru : tb.en}
          </button>
        ))}
      </nav>

      {loading && !data ? (
        <div className="card muted">
          {t("Loading Hyperliquid live desk telemetry...", "Загрузка телеметрии Hyperliquid live desk...")}
        </div>
      ) : (
        <>
          {tab === "analytics" && <AnalyticsRiskTab analytics={analytics} overview={data} />}

          {tab === "overview" && (
            <div className="stack">
              <section className="kpi-grid">
                <div className="card">
                  <KpiLabel k="kpiMetaEquity" />
                  <div className="kpi-value">{fmtUsd(data?.paper?.equity_usd)}</div>
                  <div className="kpi-sub">
                    {fmtPct(data?.paper?.return_pct)} · {data?.paper?.active_positions ?? 0} {t("pos", "поз.")} ·{" "}
                    {data?.paper?.ticks ?? 0} {p(data?.paper?.ticks ?? 0, "tick", "ticks", "тик", "тика", "тиков")}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiTestnetEquity" />
                  <div className="kpi-value">{fmtUsd(data?.attribution?.equity_usd)}</div>
                  <div className="kpi-sub">
                    Maker {fmtPct(data?.attribution?.maker_volume_share_pct, 1, false)} · ALO Fill{" "}
                    {fmtPct(data?.attribution?.real_alo_fill_rate_pct, 1, false)}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiCrossDexEquity" />
                  <div className="kpi-value">{fmtUsd(data?.cross_dex_paper?.equity_usd)}</div>
                  <div className="kpi-sub">
                    {t("Funding", "Фандинг")} {fmtUsd(data?.cross_dex_paper?.cum_funding_usd)} ·{" "}
                    {crossDexOpenPairs}{" "}
                    {p(crossDexOpenPairs, "open pair", "open pairs", "открытая пара", "открытые пары", "открытых пар")}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiHip3Leader" />
                  <div className="kpi-value">
                    {topHip3 ? `${topHip3.base_symbol} ${fmtPct(topHip3.latest_apr_pct, 1)}` : "—"}
                  </div>
                  <div className="kpi-sub">
                    {topHip3
                      ? `Clean streak ${topHip3.clean_streak} · Flips ${topHip3.direction_flips ?? 0} · Div ${fmtBps(topHip3.median_oracle_div_bps)}`
                      : t("Awaiting persistence tick", "Ожидание тика устойчивости")}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiHlp" />
                  <div className="kpi-value">{fmtPct(data?.frontier_history?.hlp_apr_pct)}</div>
                  <div className="kpi-sub">TVL {fmtUsd(data?.onchain_events?.hlp_tvl_usd)}</div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiLatency" />
                  <div className="kpi-value">
                    {data?.latency_bench ? `${fx(data.latency_bench.p50_us, 1)} µs` : "—"}
                  </div>
                  <div className="kpi-sub">
                    {data?.latency_bench
                      ? `p99 ${fx(data.latency_bench.p99_us, 1)} µs · ${Math.round(data.latency_bench.throughput_ticks_per_sec).toLocaleString()} tps`
                      : t("Benchmark not logged", "Бенчмарк не записан")}
                  </div>
                </div>
              </section>

              <div className="grid-2">
                <section className="card">
                  <div className="section-header">
                    <SectionTitle k="secEquityCurve" />
                    <span className="section-sub">{t("HL × Bybit Dollar-Neutral", "HL × Bybit, долларово-нейтрально")}</span>
                  </div>
                  <EquityCurveChart values={paperValues} />
                </section>

                <section className="card">
                  <div className="section-header">
                    <SectionTitle k="secSettlement" />
                    <div className="filter-row">
                      <span className="section-sub">
                        {data?.settlement_meta?.span_days ?? "—"}
                        {t("d", "д")} · {data?.settlement_meta?.total_settlement_events?.toLocaleString() ?? "—"}{" "}
                        {p(data?.settlement_meta?.total_settlement_events ?? 0, "event", "events", "событие", "события", "событий")}
                      </span>
                      <select
                        className="select"
                        value={settlementSplit}
                        onChange={(e) => setSettlementSplit(e.target.value)}
                      >
                        <option value="OOS">{t("OOS only", "Только OOS")}</option>
                        <option value="IS">{t("IS only", "Только IS")}</option>
                        <option value="ALL">{t("All splits", "Все выборки")}</option>
                      </select>
                    </div>
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <Th k="colSettleStrategy" />
                          <Th k="colBucket" />
                          <Th k="colEvents" num />
                          <Th k="colGross" num />
                          <Th k="colTStat" num />
                          <Th k="colNetMaker" num />
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
                            <td className="num">{fx(row.t_stat, 2)}</td>
                            <td className={`num ${row.net_maker_bps >= 0 ? "pos" : "neg"}`}>
                              {fmtBps(row.net_maker_bps, 2)}
                            </td>
                          </tr>
                        ))}
                        {settlementFiltered.length === 0 && (
                          <tr>
                            <td colSpan={6} className="muted">
                              {t("No settlement study rows loaded yet.", "Строки исследования расчётов пока не загружены.")}
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
                    <SectionTitle k="secMetaPositions" />
                    <div className="section-sub">
                      {data?.paper_positions?.length ?? 0}{" "}
                      {t(
                        "active HL × CEX positions sorted by |HL weight|",
                        `${p(data?.paper_positions?.length ?? 0, "", "", "активная позиция", "активные позиции", "активных позиций")} HL × CEX, отсортировано по |весу HL|`,
                      )}
                    </div>
                  </div>
                  <input
                    className="input"
                    placeholder={t("Filter coin (e.g. BTC, ETH)", "Фильтр по монете (напр. BTC, ETH)")}
                    value={posQuery}
                    onChange={(e) => setPosQuery(e.target.value)}
                  />
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <Th k="colCoin" />
                        <Th k="colHlWeight" num />
                        <Th k="colCexVenue" />
                        <Th k="colCexWeight" num />
                        <Th k="colLastHlPx" num />
                        <Th k="colL2Spread" num />
                        <Th k="colCumPnl" num />
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
                            {t(
                              "No active metamodel positions matching filter.",
                              "Нет активных позиций метамодели под этот фильтр.",
                            )}
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
                    <SectionTitle k="secHip3Paper" />
                    <div className="section-sub">
                      {t(
                        "Zero-bridge cross-builder L2 paper (Growth Mode taker fees, independent DEX margin, Oracle Div ≤ 15 bps, half-sized onlyIsolated)",
                        "Paper между билдерами без мостов (taker-комиссии Growth Mode, независимая маржа на каждой DEX, расхождение оракулов ≤ 15 bps, onlyIsolated — половинный размер)",
                      )}
                    </div>
                  </div>
                  <div className="mono muted">
                    {data?.hip3_paper?.observed_at_utc ?? t("awaiting first tick", "ожидание первого тика")}
                    {data?.hip3_paper?.ny_session ? ` · ${data.hip3_paper.ny_session}` : ""}
                  </div>
                </div>
                <div className="kpi-grid">
                  <div className="card">
                    <KpiLabel k="kpiHip3Equity" />
                    <div className="kpi-value">{fmtUsd(data?.hip3_paper?.equity_usd)}</div>
                    <div
                      className={`kpi-sub ${(data?.hip3_paper?.return_pct ?? 0) >= 0 ? "pos" : "neg"}`}
                    >
                      {data?.hip3_paper ? fmtPct(data.hip3_paper.return_pct, 3) : "—"}
                    </div>
                  </div>
                  <div className="card">
                    <KpiLabel k="kpiHip3Margin" />
                    <div className="kpi-value">{fmtUsd(data?.hip3_paper?.margin_locked_usd)}</div>
                    <div className="kpi-sub">
                      {data?.hip3_paper
                        ? `${fx(data.hip3_paper.margin_utilization_pct, 1)}% util · ${hip3OpenPairs} ${p(hip3OpenPairs, "open pair", "open pairs", "открытая пара", "открытые пары", "открытых пар")}`
                        : "—"}
                    </div>
                  </div>
                  <div className="card">
                    <KpiLabel k="kpiHip3Pnl" />
                    <div className="kpi-value">{fmtUsd(data?.hip3_paper?.cum_funding_usd)}</div>
                    <div className="kpi-sub">
                      {t("Basis", "Базис")} {fmtUsd(data?.hip3_paper?.cum_basis_mtm_usd)} · {t("Fees", "Комиссии")} −
                      {fmtUsd(data?.hip3_paper?.cum_costs_usd)}
                    </div>
                  </div>
                  <div className="card">
                    <KpiLabel k="kpiHip3Closed" />
                    <div className="kpi-value">{data?.hip3_paper?.closed_pairs_count ?? 0}</div>
                    <div className="kpi-sub">
                      {t("Payback achieved", "Окупилось")}: {data?.hip3_paper?.payback_achieved_count ?? 0}
                    </div>
                  </div>
                </div>
                <div className="table-wrap" style={{ marginTop: "0.75rem" }}>
                  <table>
                    <thead>
                      <tr>
                        <Th k="colTicker" />
                        <Th k="colLegs" />
                        <Th k="colMode" />
                        <Th k="colLegNotional" num />
                        <Th k="colLiveApr" num />
                        <Th k="colOracleDiv" num />
                        <Th k="colHours" num />
                        <Th k="colFunding" num />
                        <Th k="colBasisMtm" num />
                        <Th k="colNetPnl" num />
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
                          <td className="num">
                            {fx(p.hours_held, 1)}
                            {t("h", "ч")}
                          </td>
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
                      {hip3OpenPairs === 0 && (
                        <tr>
                          <td colSpan={10} className="muted">
                            {t(
                              "No open HIP-3 paper pairs yet (waiting for clean_streak ≥ 2 with Oracle Div ≤ 15 bps).",
                              "Открытых paper-пар HIP-3 пока нет (ждём clean_streak ≥ 2 при расхождении оракулов ≤ 15 bps).",
                            )}
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
                    <SectionTitle k="secHip3Persist" />
                    <div className="section-sub">
                      {t(
                        "Clean streak requires APR ≥ 20%, Oracle Div ≤ 25 bps, unchanged legs, and no missing ticks",
                        "Чистая серия требует APR ≥ 20%, расхождение оракулов ≤ 25 bps, неизменные ноги и отсутствие пропущенных тиков",
                      )}
                    </div>
                  </div>
                  <div className="filter-row">
                    <input
                      className="input"
                      placeholder={t("Filter ticker (e.g. AAPL, NVDA)", "Фильтр по тикеру (напр. AAPL, NVDA)")}
                      value={hip3Query}
                      onChange={(e) => setHip3Query(e.target.value)}
                    />
                    <label className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={activeOnly}
                        onChange={(e) => setActiveOnly(e.target.checked)}
                      />
                      {t("Active only", "Только активные")}
                    </label>
                    <label className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={steadyOnly}
                        onChange={(e) => setSteadyOnly(e.target.checked)}
                      />
                      {t("Only steady (0 flips)", "Только стабильные (0 разворотов)")}
                    </label>
                  </div>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <Th k="colTicker" />
                        <Th k="colStatus" />
                        <Th k="colLegs" />
                        <Th k="colTicks" num />
                        <Th k="colRawStreak" num />
                        <Th k="colCleanStreak" num />
                        <Th k="colFlips" num />
                        <Th k="colLatestApr" num />
                        <Th k="colMedianApr" num />
                        <Th k="colMedianOracleDiv" num />
                        <Th k="colMode" />
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
                            {t("No HIP-3 persistence rows matching filters.", "Нет строк HIP-3 под эти фильтры.")}
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
                    <SectionTitle k="secBoros" />
                    <span className="section-sub">
                      {data?.boros?.hl_and_spread_count ?? 0}{" "}
                      {p(data?.boros?.hl_and_spread_count ?? 0, "HL/Spread market", "HL/Spread markets", "рынок HL/спред", "рынка HL/спред", "рынков HL/спред")}
                    </span>
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <Th k="colMarket" />
                          <Th k="colDtm" num />
                          <Th k="colFixedBid" num />
                          <Th k="colFloat" num />
                          <Th k="colAction" />
                        </tr>
                      </thead>
                      <tbody>
                        {(data?.boros?.markets ?? []).slice(0, 10).map((m) => (
                          <tr key={m.market_id}>
                            <td className="mono">{m.symbol}</td>
                            <td className="num">
                              {fx(m.days_to_maturity, 0)}
                              {t("d", "д")}
                            </td>
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
                    <SectionTitle k="secYieldStack" />
                    <span className="section-sub">{t("vs HLP Benchmark", "против бенчмарка HLP")}</span>
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <Th k="colYieldStrategy" />
                          <Th k="colRateType" />
                          <Th k="colNetApr" num />
                          <Th k="colVsHlp" num />
                          <Th k="colLiqBuf" num />
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
                            <td className="num">{fx(s.liq_buffer_pct, 0)}%</td>
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
                  <KpiLabel k="kpiDualEquity" />
                  <div className="kpi-value">{fmtUsd(data?.cross_dex_paper?.equity_usd)}</div>
                  <div className="kpi-sub">
                    {t("Start", "Старт")} {fmtUsd(data?.cross_dex_paper?.starting_capital_usd)}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiCumFunding" />
                  <div className="kpi-value pos">{fmtUsd(data?.cross_dex_paper?.cum_funding_usd)}</div>
                  <div className="kpi-sub">Basis MTM {fmtUsd(data?.cross_dex_paper?.cum_basis_mtm_usd)}</div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiExecCosts" />
                  <div className="kpi-value warn">{fmtUsd(data?.cross_dex_paper?.cum_costs_usd)}</div>
                  <div className="kpi-sub">
                    {t("Closed", "Закрыто")} {data?.cross_dex_paper?.closed_pairs_count ?? 0} · {t("Payback", "Окупилось")}{" "}
                    {data?.cross_dex_paper?.payback_achieved_count ?? 0}
                  </div>
                </div>
              </section>

              <section className="card">
                <div className="section-header">
                  <SectionTitle k="secDualPositions" />
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <Th k="colCoin" />
                        <Th k="colDirection" />
                        <Th k="colLegSize" num />
                        <Th k="colHoursHeld" num />
                        <Th k="colEntryApr" num />
                        <Th k="colLiveAprDual" num />
                        <Th k="colEntryBasis" num />
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
                          <td className="num">
                            {fx(p.hours_held, 1)}
                            {t("h", "ч")}
                          </td>
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
                            {t("No open dual-leg paper positions.", "Нет открытых двуногих paper-позиций.")}
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="card">
                <div className="section-header">
                  <SectionTitle k="secDexOpps" />
                  <div className="filter-row">
                    <span className="section-sub">{t("Min Spread APR:", "Мин. спред APR:")}</span>
                    <select
                      className="select"
                      value={crossDexMinApr}
                      onChange={(e) => setCrossDexMinApr(Number(e.target.value))}
                    >
                      <option value={0}>{t("All", "Все")}</option>
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
                        <Th k="colCoin" />
                        <Th k="colVenue" />
                        <Th k="colDirection" />
                        <Th k="colHlApr" num />
                        <Th k="colOtherApr" num />
                        <Th k="colSpreadApr" num />
                        <Th k="colFavBasis" num />
                        <Th k="colBreakeven" num />
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
                          <td className="num">
                            {fx(o.breakeven_hours, 1)}
                            {t("h", "ч")}
                          </td>
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
                  <strong>
                    {t("Active On-Chain Alerts", "Активные on-chain алерты")} ({data?.onchain_events?.observed_at_utc}):
                  </strong>{" "}
                  <span className="mono">
                    {(data?.onchain_events?.active_alert_keys ?? []).join(" · ") ||
                      t("Threshold active", "Порог превышен")}
                  </span>
                </div>
              )}

              <section className="kpi-grid">
                <div className="card">
                  <KpiLabel k="kpiHlpTvl" />
                  <div className="kpi-value">{fmtUsd(data?.onchain_events?.hlp_tvl_usd)}</div>
                  <div className="kpi-sub">
                    {t("Source", "Источник")}: {String(data?.onchain_events?.hlp_mm_pnl?.pnl_source ?? "—")}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiHlpDelta" />
                  <div className="kpi-value">
                    {fmtUsd(data?.onchain_events?.hlp_mm_pnl?.hlp_pnl_delta_usd)}
                  </div>
                  <div className="kpi-sub">
                    {t("Regime", "Режим")}: {String(data?.onchain_events?.hlp_mm_pnl?.mm_pnl_regime ?? "—")}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiEvmChain" />
                  <div className="kpi-value">
                    {data?.hyperevm?.chain?.block_number
                      ? `#${Number(data.hyperevm.chain.block_number).toLocaleString()}`
                      : "—"}
                  </div>
                  <div className="kpi-sub">
                    {t("USDT0 peg disc", "Дисконт USDT0")} {fmtBps(data?.hyperevm?.chain?.usdt0_peg_discount_bps, 2)}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiWhales" />
                  <div className="kpi-value">{data?.liq_funding?.whales_scanned ?? "—"}</div>
                  <div className="kpi-sub">
                    {t("With positions", "С позициями")}: {data?.liq_funding?.whales_with_positions ?? "—"}
                  </div>
                </div>
              </section>

              <div className="grid-2">
                <section className="card">
                  <div className="section-header">
                    <SectionTitle k="secLiqClusters" />
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <Th k="colCoin" />
                          <Th k="colMark" num />
                          <Th k="colTotal010" num />
                          <Th k="colNearestL" num />
                          <Th k="colNearestS" num />
                          <Th k="colBias" />
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
                              <span className="badge">{cascadeBiasLabel(c.cascade_bias, lang)}</span>
                            </td>
                          </tr>
                        ))}
                        {(data?.liq_funding?.clusters ?? []).length === 0 && (
                          <tr>
                            <td colSpan={6} className="muted">
                              {t(
                                "No dense liquidation walls detected in current scan.",
                                "В текущем скане плотных стен ликвидаций не найдено.",
                              )}
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </section>

                <section className="card">
                  <div className="section-header">
                    <SectionTitle k="secL1Read" />
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <Th k="colCoin" />
                          <Th k="colL1ReadMark" num />
                          <Th k="colRestMark" num />
                          <Th k="colDrift" num />
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
                  <KpiLabel k="kpiTnEquity" />
                  <div className="kpi-value">{fmtUsd(data?.attribution?.equity_usd)}</div>
                  <div className="kpi-sub">
                    {t("Open pos", "Открыто поз.")}: {String(data?.attribution?.open_positions_count ?? "—")} ·{" "}
                    {t("Fills", "Исполнений")}: {String(data?.attribution?.total_fills ?? "—")}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiMakerAlo" />
                  <div className="kpi-value pos">
                    {fmtPct(data?.attribution?.maker_volume_share_pct, 1, false)}
                  </div>
                  <div className="kpi-sub">
                    ALO fill {fmtPct(data?.attribution?.real_alo_fill_rate_pct, 1, false)} · {t("Fee", "Комиссия")}{" "}
                    {fmtBps(data?.attribution?.effective_fee_bps, 2)}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiNetAttr" />
                  <div className="kpi-value">{fmtUsd(data?.attribution?.net_attributed_pnl_usd)}</div>
                  <div className="kpi-sub">
                    VW Markout {fmtBps(data?.attribution?.vw_markout_bps, 2)}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiApiQuota" />
                  <div className="kpi-value">
                    {String(data?.attribution?.api_quota_health ?? "—")}
                  </div>
                  <div className="kpi-sub">
                    {t("Min liq dist", "Мин. до ликв.")} {fmtPct(data?.attribution?.min_liq_distance_pct, 1, false)} ·{" "}
                    {t("Volume", "Объём")} {fmtUsd(data?.attribution?.total_volume_usd)}
                  </div>
                </div>
              </section>

              <section className="card">
                <div className="section-header">
                  <SectionTitle k="secPerCoin" />
                  <span className="section-sub">
                    {perCoinAttribution.length}{" "}
                    {p(perCoinAttribution.length, "tracked coin", "tracked coins", "отслеживаемая монета", "отслеживаемые монеты", "отслеживаемых монет")}
                  </span>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <Th k="colCoin" />
                        <Th k="colPositionUsd" num />
                        <Th k="colLeverage" />
                        <Th k="colFills" num />
                        <Th k="colMakerPct" num />
                        <Th k="colNetPnl" num />
                        <Th k="colVwMarkout" num />
                        <Th k="colLiqDist" num />
                        <Th k="colAdlRank" num />
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
                                {fx(c.adl_rank_score, 2)}
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
                            {t("No per-coin attribution snapshot yet.", "Снимка атрибуции по монетам пока нет.")}
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="card">
                <div className="section-header">
                  <SectionTitle k="secAudit" />
                  <span className="section-sub">
                    {t("Newest first from hl_testnet_audit.jsonl", "Сначала новые, из hl_testnet_audit.jsonl")}
                  </span>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <Th k="colTimeUtc" />
                        <Th k="colAuditStatus" />
                        <Th k="colCancelled" num />
                        <Th k="colPlacedAlo" num />
                        <Th k="colNotional" num />
                        <Th k="colHwm" num />
                        <Th k="colViolations" />
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
                                ? `${t("skipped (no BBO)", "пропущено (нет BBO)")}: ${rec.skipped_unquoted_coins.join(", ")}`
                                : "",
                              rec.orphan_positions && Object.keys(rec.orphan_positions).length
                                ? `${t("orphans", "сироты")}: ${Object.keys(rec.orphan_positions).join(", ")}`
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
                            {t("No rebalance audit records yet.", "Записей аудита ребалансов пока нет.")}
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
                      <SectionTitle k="secKappa" suffix={` (${cal.coin})`} />
                      <span className="badge badge-cyan">R² = {fx(cal.r2_fit, 3)}</span>
                    </div>
                    <KappaCalibrationChart cal={cal} />
                    <div className="kpi-sub" style={{ marginTop: 8 }}>
                      κ = {fx(cal.kappa_per_bp, 3)}/bp · σ_τ = {fx(cal.empirical_sigma_tau_bps, 2)} bps ·{" "}
                      {t("Min half-spread", "Мин. полуспред")} = {fx(cal.implied_min_half_spread_bps, 2)} bps ·{" "}
                      {cal.trades_count} {p(cal.trades_count, "trade", "trades", "сделка", "сделки", "сделок")}
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
                  <KpiLabel k="kpiFeSha" />
                  <div className="kpi-value">{shortSha(feSha)}</div>
                  <div className="kpi-sub">{feBuild?.built_at || "local / dev"}</div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiBeSha" />
                  <div className="kpi-value">{shortSha(beSha)}</div>
                  <div className="kpi-sub">{data?.build_info?.built_at || "local / dev"}</div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiHeartbeat" />
                  <div className="kpi-value">
                    {data?.worker_status ? "ONLINE" : "EXTERNAL / CRON"}
                  </div>
                  <div className="kpi-sub">
                    {data?.worker_status?.heartbeat_utc ??
                      t(
                        "Run hl-worker container to report heartbeat",
                        "Запустите контейнер hl-worker, чтобы появился пульс",
                      )}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiExecGate" />
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
                  <SectionTitle k="secJobs" />
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <Th k="colJob" />
                        <Th k="colCron" />
                        <Th k="colState" />
                        <Th k="colLastFinished" />
                        <Th k="colDuration" num />
                        <Th k="colOkFail" num />
                        <Th k="colNextRun" />
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
                            {num(j.last_duration_s) !== null ? `${fx(j.last_duration_s, 1)}${t("s", "с")}` : "—"}
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
                            {t(
                              "No container worker heartbeat yet (expected when previewing local data without worker).",
                              "Пульса от воркера пока нет (это нормально при просмотре локальных данных без воркера).",
                            )}
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="card">
                <div className="section-header">
                  <SectionTitle k="secFreshness" />
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <Th k="colArtifact" />
                        <Th k="colPath" />
                        <Th k="colFileStatus" />
                        <Th k="colUpdated" />
                        <Th k="colAgeMin" num />
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
                          <td className="num">{num(info.age_minutes) !== null ? fx(info.age_minutes, 1) : "—"}</td>
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
