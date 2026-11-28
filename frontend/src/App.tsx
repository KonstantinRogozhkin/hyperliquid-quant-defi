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

export function fx(v: unknown, digits = 2, suffix = ""): string {
  const n = num(v);
  if (n === null) return "—";
  const norm = Math.abs(n) < 0.5 * Math.pow(10, -digits) ? 0 : n;
  return `${norm.toFixed(digits)}${suffix}`;
}

export function fmtUsd(v: unknown, digits = 2): string {
  const n = num(v);
  if (n === null) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 && abs >= 0.5 * Math.pow(10, -digits) ? "-" : "";
  if (abs >= 1_000_000_000) return `${sign}$${(abs / 1_000_000_000).toFixed(2)}B`;
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
  return `${sign}$${abs.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function fmtPct(v: unknown, digits = 2, signed = true): string {
  const n = num(v);
  if (n === null) return "—";
  const norm = Math.abs(n) < 0.5 * Math.pow(10, -digits) ? 0 : n;
  return `${signed && norm >= 0 ? "+" : ""}${norm.toFixed(digits)}%`;
}

export function fmtBps(v: unknown, digits = 1, signed = false): string {
  const n = num(v);
  if (n === null) return "—";
  const norm = Math.abs(n) < 0.5 * Math.pow(10, -digits) ? 0 : n;
  return `${signed && norm >= 0 ? "+" : ""}${norm.toFixed(digits)} bps`;
}

function signCls(v: unknown): string {
  const n = num(v);
  if (n === null) return "";
  return n > 0 ? "pos" : n < 0 ? "neg" : "";
}

function shortSha(sha?: string | null): string {
  if (!sha) return "dev";
  return sha.length > 8 ? sha.slice(0, 8) : sha;
}

const CASCADE_BIAS_LABELS: Record<string, Record<Lang, string>> = {
  downside_cascade: { en: "Downside Cascade (long liquidations)", ru: "Каскад вниз (ликвидации лонгов)" },
  upside_squeeze: { en: "Upside Squeeze (short liquidations)", ru: "Сквиз вверх (ликвидации шортов)" },
  balanced: { en: "Balanced", ru: "Сбалансировано" },
  LONG_SQUEEZE: { en: "LONG_SQUEEZE", ru: "Лонг-сквиз" },
  SHORT_SQUEEZE: { en: "SHORT_SQUEEZE", ru: "Шорт-сквиз" },
  NEUTRAL: { en: "Balanced", ru: "Сбалансировано" },
};

function cascadeBiasLabel(bias: unknown, lang: Lang): string {
  if (bias === null || bias === undefined || bias === "") return "—";
  const key = String(bias);
  return CASCADE_BIAS_LABELS[key]?.[lang] ?? key;
}

function normalizeSplitLabel(split: unknown): { label: string; isOos: boolean } {
  const sp = String(split ?? "").trim().toUpperCase();
  if (sp === "OOS" || sp === "HOLDOUT") return { label: "OOS", isOos: true };
  if (sp === "IS" || sp === "DEV") return { label: "IS", isOos: false };
  return { label: sp || "—", isOos: false };
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
  const [analyticsError, setAnalyticsError] = useState<string | null>(null);
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
  const abortRef = useRef<AbortController | null>(null);

  const loadAll = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const { signal } = controller;
    const seq = ++reqSeqRef.current;

    try {
      const [ovRes, feRes, anOutcome] = await Promise.all([
        fetch("/api/v1/overview", { cache: "no-store", signal }),
        fetch("/build-info.json", { cache: "no-store", signal }).catch(() => null),
        fetch("/api/v1/analytics", { cache: "no-store", signal })
          .then(async (res) => {
            if (!res.ok) {
              return { payload: null, error: `Backend /api/v1/analytics returned HTTP ${res.status}` };
            }
            const parsed = (await res.json()) as AnalyticsPayload;
            return { payload: parsed, error: null };
          })
          .catch((anErr: unknown) => {
            if (signal.aborted || (anErr instanceof DOMException && anErr.name === "AbortError")) {
              return { payload: null, error: null, aborted: true };
            }
            return { payload: null, error: anErr instanceof Error ? anErr.message : String(anErr) };
          }),
      ]);

      if (signal.aborted || seq !== reqSeqRef.current) return;

      if (!ovRes.ok) {
        throw new Error(`Backend /api/v1/overview returned HTTP ${ovRes.status}`);
      }
      const ovJson = (await ovRes.json()) as OverviewPayload;
      const feJson = feRes && feRes.ok ? ((await feRes.json().catch(() => null)) as BuildInfo | null) : null;

      if (signal.aborted || seq !== reqSeqRef.current) return;

      // Clear previous error banners only AFTER a successful fetch so banners never flicker during outages.
      setError(null);
      setData(ovJson);

      if (!("aborted" in anOutcome && anOutcome.aborted)) {
        if (anOutcome.payload) {
          setAnalytics(anOutcome.payload);
          setAnalyticsError(null);
        } else if (anOutcome.error) {
          setAnalyticsError(anOutcome.error);
        }
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
      if (signal.aborted || (err instanceof DOMException && err.name === "AbortError") || seq !== reqSeqRef.current) {
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!signal.aborted && seq === reqSeqRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void loadAll();
    return () => {
      abortRef.current?.abort();
    };
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
    const filtered =
      settlementSplit === "ALL"
        ? rows
        : rows.filter((r) => {
            const sp = String(r.split ?? "").trim().toUpperCase();
            if (settlementSplit === "OOS") return sp === "OOS" || sp === "HOLDOUT";
            if (settlementSplit === "IS") return sp === "IS" || sp === "DEV";
            return sp === settlementSplit;
          });
    return [...filtered]
      .sort(
        (a, b) =>
          (num(b.net_maker_bps) ?? -Infinity) - (num(a.net_maker_bps) ?? -Infinity) ||
          (num(b.t_stat) ?? -Infinity) - (num(a.t_stat) ?? -Infinity),
      )
      .slice(0, 16);
  }, [data, settlementSplit]);

  const beSha = data?.build_info?.sha ?? "dev";
  const feSha = feBuild?.sha ?? "dev";
  const shaAligned = (feSha === "dev" && beSha === "dev") || (feSha !== "dev" && beSha === feSha);
  const anyAlert = Boolean(data?.onchain_events?.any_alert);
  const topHip3 = (data?.frontier_history?.hip3_persistence ?? []).find(
    (r) => r.active_in_latest_tick !== false && r.clean_streak >= 1,
  );
  const paperValues = (data?.paper_ticks ?? []).map((tk) => tk.equity_usd).filter((n) => Number.isFinite(n));
  const hip3OpenPairs = Object.keys(data?.hip3_paper?.positions ?? {}).length;
  const crossDexOpenPairs = Object.keys(data?.cross_dex_paper?.positions ?? {}).length;
  const metaPosCount = data?.paper_positions?.length ?? 0;

  const liveH2Events = data?.onchain_events?.h2_events ?? [];
  const useLiveH2 = liveH2Events.length > 0;
  const h2Rows = useLiveH2 ? liveH2Events : (data?.liq_funding?.clusters ?? []);
  const h4Events = data?.onchain_events?.h4_events ?? [];
  const h5Events = data?.onchain_events?.h5_events ?? [];
  const h6Events = data?.onchain_events?.h6_events ?? [];

  const mixedAccountWarn =
    typeof data?.attribution?.mixed_account_warning === "string" && data.attribution.mixed_account_warning.trim()
      ? data.attribution.mixed_account_warning.trim()
      : "";
  const liqWarnings = Array.isArray(data?.attribution?.liq_warnings)
    ? (data.attribution.liq_warnings as string[]).filter(Boolean)
    : [];
  const adlWarnings = Array.isArray(data?.attribution?.adl_warnings)
    ? (data.attribution.adl_warnings as string[]).filter(Boolean)
    : [];
  const tnRisk = data?.testnet_risk ?? null;

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
        <div className="error-box" role="alert">
          <strong>{t("Backend unreachable:", "Бэкенд недоступен:")}</strong> {error}
        </div>
      )}

      <div className="tabs" role="tablist" aria-label={t("Dashboard sections", "Разделы дашборда")}>
        {TABS.map((tb) => (
          <button
            key={tb.id}
            type="button"
            role="tab"
            aria-selected={tab === tb.id}
            className={`tab ${tab === tb.id ? "tab-active" : ""}`}
            onClick={() => setTab(tb.id)}
          >
            {lang === "ru" ? tb.ru : tb.en}
          </button>
        ))}
      </div>

      {loading && !data ? (
        <div className="card muted">
          {t("Loading Hyperliquid live desk telemetry...", "Загрузка телеметрии Hyperliquid live desk...")}
        </div>
      ) : (
        <>
          {tab === "analytics" && (
            <AnalyticsRiskTab analytics={analytics} overview={data} analyticsError={analyticsError} />
          )}

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
                    {t("Maker", "Мейкер")} {fmtPct(data?.attribution?.maker_volume_share_pct, 1, false)} ·{" "}
                    {t("ALO Fill", "Исп. ALO")} {fmtPct(data?.attribution?.real_alo_fill_rate_pct, 1, false)}
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
                      ? `${t("Clean streak", "Чистая серия")} ${topHip3.clean_streak} · ${t("Flips", "Развороты")} ${topHip3.direction_flips ?? 0} · ${t("Div", "Расх.")} ${fmtBps(topHip3.median_oracle_div_bps)}`
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
                  <div className="kpi-value">{fx(data?.latency_bench?.p50_us, 1, " µs")}</div>
                  <div className="kpi-sub">
                    {data?.latency_bench
                      ? `p99 ${fx(data.latency_bench.p99_us, 1, " µs")} · ${Math.round(data.latency_bench.throughput_ticks_per_sec).toLocaleString()} tps`
                      : t("Benchmark not logged", "Бенчмарк не записан")}
                  </div>
                </div>
              </section>

              <div className="grid-2">
                <section className="card">
                  <div className="section-header">
                    <SectionTitle k="secEquityCurve" />
                    <span className="section-sub">
                      {t(
                        "Multi-CEX (Bybit/Binance/OKX) + HL XS · Observation-Only (5.0 bps)",
                        "Мульти-CEX (Bybit/Binance/OKX) + HL XS · наблюдение (5.0 bps)",
                      )}
                    </span>
                  </div>
                  <EquityCurveChart values={paperValues} initialValue={data?.paper?.starting_capital_usd} />
                </section>

                <section className="card">
                  <div className="section-header">
                    <SectionTitle k="secSettlement" />
                    <div className="filter-row">
                      <span className="section-sub">
                        {fx(data?.settlement_meta?.span_days, 0, t("d", "д"))} ·{" "}
                        {data?.settlement_meta?.total_settlement_events?.toLocaleString() ?? "—"}{" "}
                        {p(data?.settlement_meta?.total_settlement_events ?? 0, "event", "events", "событие", "события", "событий")}
                      </span>
                      <select
                        className="select"
                        aria-label={t("Settlement study split", "Выборка исследования расчётов")}
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
                          <Th k="colSettleSplit" />
                          <Th k="colEvents" num />
                          <Th k="colGross" num />
                          <Th k="colTStat" num />
                          <Th k="colNetMaker" num />
                        </tr>
                      </thead>
                      <tbody>
                        {settlementFiltered.map((row, idx) => {
                          const sp = normalizeSplitLabel(row.split);
                          return (
                            <tr key={idx}>
                              <td>{row.strategy}</td>
                              <td className="mono">{row.bucket}</td>
                              <td>
                                <span className={`badge ${sp.isOos ? "badge-ok" : "badge-cyan"}`}>{sp.label}</span>
                              </td>
                              <td className="num">{row.events}</td>
                              <td className={`num ${signCls(row.mean_gross_bps)}`}>
                                {fmtBps(row.mean_gross_bps, 2)}
                              </td>
                              <td className="num">{fx(row.t_stat, 2)}</td>
                              <td className={`num ${signCls(row.net_maker_bps)}`}>
                                {fmtBps(row.net_maker_bps, 2)}
                              </td>
                            </tr>
                          );
                        })}
                        {settlementFiltered.length === 0 && (
                          <tr>
                            <td colSpan={7} className="muted">
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
                      {metaPosCount}{" "}
                      {p(
                        metaPosCount,
                        "active HL × CEX position sorted by |HL weight|",
                        "active HL × CEX positions sorted by |HL weight|",
                        "активная позиция HL × CEX, отсортировано по |весу HL|",
                        "активные позиции HL × CEX, отсортировано по |весу HL|",
                        "активных позиций HL × CEX, отсортировано по |весу HL|",
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
                      {paperPositionsFiltered.map((pos) => {
                        const legEntries = pos.cex_legs ? Object.entries(pos.cex_legs) : [];
                        const showCexWeight =
                          num(pos.cex_weight) !== null &&
                          (Math.abs(pos.cex_weight) > 0 || Boolean(pos.cex_venue) || legEntries.length > 0);
                        return (
                          <tr key={pos.coin}>
                            <td>
                              <strong>{pos.coin}</strong>
                            </td>
                            <td className={`num ${signCls(pos.hl_weight)}`}>{fmtPct(pos.hl_weight * 100, 2)}</td>
                            <td>
                              {pos.cex_venue ? (
                                <span className="badge badge-cyan">{pos.cex_venue}</span>
                              ) : (
                                <span className="muted">{t("HL-only", "Только HL")}</span>
                              )}
                              {legEntries.length > 1 && (
                                <div className="section-sub mono">
                                  {legEntries.map(([v, w]) => `${v} ${fmtPct(w * 100, 1)}`).join(" · ")}
                                </div>
                              )}
                            </td>
                            <td className="num">{showCexWeight ? fmtPct(pos.cex_weight * 100, 2) : "—"}</td>
                            <td className="num">{fmtUsd(pos.last_hl_px, 4)}</td>
                            <td className="num">{fmtBps(pos.l2_spread_bps, 2)}</td>
                            <td className={`num ${signCls(pos.cum_pnl_usd)}`}>{fmtUsd(pos.cum_pnl_usd)}</td>
                          </tr>
                        );
                      })}
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
                    <div className={`kpi-sub ${signCls(data?.hip3_paper?.return_pct)}`}>
                      {data?.hip3_paper ? fmtPct(data.hip3_paper.return_pct, 3) : "—"}
                    </div>
                  </div>
                  <div className="card">
                    <KpiLabel k="kpiHip3Margin" />
                    <div className="kpi-value">{fmtUsd(data?.hip3_paper?.margin_locked_usd)}</div>
                    <div className="kpi-sub">
                      {data?.hip3_paper
                        ? `${fx(data.hip3_paper.margin_utilization_pct, 1, "%")} ${t("util", "исп.")} · ${hip3OpenPairs} ${p(hip3OpenPairs, "open pair", "open pairs", "открытая пара", "открытые пары", "открытых пар")}`
                        : "—"}
                    </div>
                  </div>
                  <div className="card">
                    <KpiLabel k="kpiHip3Pnl" />
                    <div className="kpi-value">{fmtUsd(data?.hip3_paper?.cum_funding_usd)}</div>
                    <div className="kpi-sub">
                      {t("Basis", "Базис")} {fmtUsd(data?.hip3_paper?.cum_basis_mtm_usd)} · {t("Fees", "Комиссии")}{" "}
                      {num(data?.hip3_paper?.cum_costs_usd) !== null
                        ? fmtUsd(-Math.abs(data!.hip3_paper!.cum_costs_usd))
                        : "—"}
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
                      {Object.values(data?.hip3_paper?.positions ?? {}).map((pos) => (
                        <tr key={pos.base_symbol}>
                          <td>
                            <strong>{pos.base_symbol}</strong>
                          </td>
                          <td className="mono">{`S ${pos.short_symbol} / L ${pos.long_symbol}`}</td>
                          <td>
                            <span className={`badge ${pos.both_growth_mode ? "badge-cyan" : ""}`}>
                              {pos.both_growth_mode ? "⚡ GM" : t("Std", "Станд.")}
                            </span>{" "}
                            <span className={`badge ${pos.any_only_isolated ? "badge-warn" : "badge-ok"}`}>
                              {pos.any_only_isolated ? t("Isolated", "Изол.") : t("Cross", "Кросс")}
                            </span>
                          </td>
                          <td className="num">{fmtUsd(pos.leg_notional_usd)}</td>
                          <td className={`num ${signCls(pos.live_spread_apr_pct)}`}>{fmtPct(pos.live_spread_apr_pct, 1)}</td>
                          <td className={`num ${pos.live_oracle_div_bps > 15 ? "warn" : ""}`}>
                            {fmtBps(pos.live_oracle_div_bps)}
                          </td>
                          <td className="num">{fx(pos.hours_held, 1, t("h", "ч"))}</td>
                          <td className={`num ${signCls(pos.cum_funding_usd)}`}>{fmtUsd(pos.cum_funding_usd)}</td>
                          <td className={`num ${signCls(pos.cum_basis_mtm_usd)}`}>{fmtUsd(pos.cum_basis_mtm_usd)}</td>
                          <td className={`num ${signCls(pos.net_pnl_usd)}`}>
                            <strong>{fmtUsd(pos.net_pnl_usd)}</strong>
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
                                ? t(`Stale (-${r.ticks_since_last_seen ?? 1}t)`, `Устар. (-${r.ticks_since_last_seen ?? 1}т)`)
                                : t("Active", "Активна")}
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
                          <td className={`num ${signCls(r.latest_apr_pct)}`}>{fmtPct(r.latest_apr_pct, 1)}</td>
                          <td className="num">{fmtPct(r.median_apr_pct, 1)}</td>
                          <td className={`num ${r.median_oracle_div_bps > 15 ? "warn" : ""}`}>
                            {fmtBps(r.median_oracle_div_bps)}
                          </td>
                          <td>
                            <span className={`badge ${r.both_growth_mode ? "badge-cyan" : ""}`}>
                              {r.both_growth_mode ? "⚡ GM" : t("Std", "Станд.")}
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
                      {p(
                        data?.boros?.hl_and_spread_count ?? 0,
                        "HL/Spread market",
                        "HL/Spread markets",
                        "рынок HL/спред",
                        "рынка HL/спред",
                        "рынков HL/спред",
                      )}
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
                            <td>
                              <div>
                                <strong>{m.name || m.symbol}</strong>{" "}
                                {m.is_spread_market && (
                                  <span className="badge badge-cyan">{t("SPREAD", "СПРЕД")}</span>
                                )}
                              </div>
                              {m.name && m.symbol && m.name !== m.symbol && (
                                <div className="section-sub mono">{m.symbol}</div>
                              )}
                            </td>
                            <td className="num">{fx(m.days_to_maturity, 0, t("d", "д"))}</td>
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
                            <td className={`num ${signCls(s.net_deposit_apr_pct)}`}>
                              {fmtPct(s.net_deposit_apr_pct, 2)}
                            </td>
                            <td className={`num ${signCls(s.spread_vs_hlp_pct)}`}>
                              {fmtPct(s.spread_vs_hlp_pct, 2)}
                            </td>
                            <td className="num">{fx(s.liq_buffer_pct, 0, "%")}</td>
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
                  <div className={`kpi-value ${signCls(data?.cross_dex_paper?.cum_funding_usd)}`}>
                    {fmtUsd(data?.cross_dex_paper?.cum_funding_usd)}
                  </div>
                  <div className="kpi-sub">
                    {t("Basis MTM", "Базис MTM")} {fmtUsd(data?.cross_dex_paper?.cum_basis_mtm_usd)}
                  </div>
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
                        <Th k="colVenue" />
                        <Th k="colDirection" />
                        <Th k="colLegSize" num />
                        <Th k="colHoursHeld" num />
                        <Th k="colEntryApr" num />
                        <Th k="colLiveAprDual" num />
                        <Th k="colEntryBasis" num />
                        <Th k="colFunding" num />
                        <Th k="colBasisMtm" num />
                        <Th k="colNetPnl" num />
                      </tr>
                    </thead>
                    <tbody>
                      {Object.values(data?.cross_dex_paper?.positions ?? {}).map((pos) => {
                        const venueLabel =
                          pos.other_dex ??
                          (pos.direction.toLowerCase().includes("extended") ? "extended" : "lighter");
                        return (
                          <tr key={pos.coin}>
                            <td>
                              <strong>{pos.coin}</strong>
                            </td>
                            <td className="mono">{venueLabel}</td>
                            <td className="mono">{pos.direction}</td>
                            <td className="num">{fmtUsd(pos.leg_notional_usd, 0)}</td>
                            <td className="num">{fx(pos.hours_held, 1, t("h", "ч"))}</td>
                            <td className="num">{fmtPct(pos.entry_spread_apr_pct, 1)}</td>
                            <td className={`num ${pos.live_spread_apr_pct >= 10 ? "pos" : pos.live_spread_apr_pct < 0 ? "neg" : "warn"}`}>
                              {fmtPct(pos.live_spread_apr_pct, 1)}
                            </td>
                            <td className="num">{fmtBps(pos.entry_exec_basis_bps, 2)}</td>
                            <td className={`num ${signCls(pos.cum_funding_usd)}`}>{fmtUsd(pos.cum_funding_usd)}</td>
                            <td className={`num ${signCls(pos.cum_basis_mtm_usd)}`}>{fmtUsd(pos.cum_basis_mtm_usd)}</td>
                            <td className={`num ${signCls(pos.net_pnl_usd)}`}>{fmtUsd(pos.net_pnl_usd)}</td>
                          </tr>
                        );
                      })}
                      {Object.keys(data?.cross_dex_paper?.positions ?? {}).length === 0 && (
                        <tr>
                          <td colSpan={11} className="muted">
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
                      aria-label={t("Minimum Spread APR filter", "Фильтр минимального спреда APR")}
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
                        <Th k="colDexL2Slip10k" num />
                        <Th k="colBreakeven" num />
                      </tr>
                    </thead>
                    <tbody>
                      {crossDexFiltered.map((o, idx) => {
                        const execBasis = o.exec_basis_10k_bps ?? o.favorable_basis_bps;
                        const beHours = o.depth_be_10k_h !== undefined ? o.depth_be_10k_h : o.breakeven_hours;
                        return (
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
                            <td className={`num ${signCls(o.spread_apr_pct)}`}>{fmtPct(o.spread_apr_pct, 1)}</td>
                            <td className={`num ${signCls(execBasis)}`}>{fmtBps(execBasis, 1, true)}</td>
                            <td className={`num ${(num(o.l2_slip_10k_bps) ?? 0) > 10 ? "warn" : ""}`}>
                              {fmtBps(o.l2_slip_10k_bps, 1)}
                            </td>
                            <td className="num">{fx(beHours, 1, t("h", "ч"))}</td>
                          </tr>
                        );
                      })}
                      {crossDexFiltered.length === 0 && (
                        <tr>
                          <td colSpan={9} className="muted">
                            {t("No cross-DEX opportunities matching filter.", "Нет возможностей 5-DEX под этот фильтр.")}
                          </td>
                        </tr>
                      )}
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
                  <div className={`kpi-value ${signCls(data?.onchain_events?.hlp_mm_pnl?.hlp_pnl_delta_usd)}`}>
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
                    <span className="section-sub">
                      {useLiveH2
                        ? `${t("15m live feed", "15м поток")} (${data?.onchain_events?.observed_at_utc ?? ""})`
                        : t("4h deep whale scan", "4ч скан китов")}
                    </span>
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <Th k="colCoin" />
                          <Th k="colMark" num />
                          {useLiveH2 ? <Th k="colTotal05" num /> : null}
                          <Th k="colTotal010" num />
                          {useLiveH2 ? (
                            <Th k="colNearestWall" num />
                          ) : (
                            <>
                              <Th k="colNearestL" num />
                              <Th k="colNearestS" num />
                            </>
                          )}
                          <Th k="colBias" />
                        </tr>
                      </thead>
                      <tbody>
                        {h2Rows.map((c, idx) => (
                          <tr key={idx}>
                            <td>
                              <strong>{String(c.coin)}</strong>{" "}
                              {Boolean(c.alert) && (
                                <span className="badge badge-danger">{t("ALERT", "АЛЕРТ")}</span>
                              )}
                            </td>
                            <td className="num">{fmtUsd(c.mark_px)}</td>
                            {useLiveH2 ? <td className="num">{fmtUsd(c.total_0_5_usd, 0)}</td> : null}
                            <td className="num warn">{fmtUsd(c.total_0_10_usd, 0)}</td>
                            {useLiveH2 ? (
                              <td className="num">{fmtPct(c.nearest_wall_pct, 1, false)}</td>
                            ) : (
                              <>
                                <td className="num">{fmtPct(c.nearest_long_dist_pct, 1, false)}</td>
                                <td className="num">{fmtPct(c.nearest_short_dist_pct, 1, false)}</td>
                              </>
                            )}
                            <td>
                              <span className="badge">{cascadeBiasLabel(c.cascade_bias, lang)}</span>
                            </td>
                          </tr>
                        ))}
                        {h2Rows.length === 0 && (
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

              {(h5Events.length > 0 || h6Events.length > 0) && (
                <div className="grid-2">
                  {h5Events.length > 0 && (
                    <section className="card">
                      <div className="section-header">
                        <SectionTitle k="secH5Skew" />
                      </div>
                      <div className="table-wrap">
                        <table>
                          <thead>
                            <tr>
                              <Th k="colCoin" />
                              <Th k="colHlpSide" />
                              <Th k="colHlpNetUsd" num />
                              <Th k="colHlpTvlShare" num />
                              <Th k="colVaultsCount" num />
                              <Th k="colAlert" />
                            </tr>
                          </thead>
                          <tbody>
                            {h5Events.map((ev, idx) => (
                              <tr key={idx}>
                                <td>
                                  <strong>{String(ev.coin)}</strong>
                                </td>
                                <td>
                                  <span
                                    className={`badge ${String(ev.hlp_side) === "HLP_LONG" ? "badge-ok" : "badge-warn"}`}
                                  >
                                    {String(ev.hlp_side ?? "—")}
                                  </span>
                                </td>
                                <td className={`num ${signCls(ev.hlp_net_usd)}`}>{fmtUsd(ev.hlp_net_usd, 0)}</td>
                                <td className="num">{fmtPct(ev.hlp_tvl_share_pct, 3, false)}</td>
                                <td className="num">{num(ev.vaults_holding) ?? "—"}</td>
                                <td>
                                  {Boolean(ev.alert) ? (
                                    <span className="badge badge-danger">{t("ALERT", "АЛЕРТ")}</span>
                                  ) : (
                                    <span className="muted">—</span>
                                  )}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </section>
                  )}

                  {h6Events.length > 0 && (
                    <section className="card">
                      <div className="section-header">
                        <SectionTitle k="secH6Stress" />
                      </div>
                      <div className="table-wrap">
                        <table>
                          <thead>
                            <tr>
                              <Th k="colCoin" />
                              <Th k="colMidMarkBps" num />
                              <Th k="colMarkOracleBps" num />
                              <Th k="colFundingApr" num />
                              <Th k="colStressType" />
                            </tr>
                          </thead>
                          <tbody>
                            {h6Events.map((ev, idx) => {
                              const st = String(ev.stress_type ?? "—");
                              const badgeCls =
                                st === "CRASH_BOOK_STRESS"
                                  ? "badge-danger"
                                  : st === "ORACLE_DECOUPLING"
                                    ? "badge-warn"
                                    : "badge-cyan";
                              return (
                                <tr key={idx}>
                                  <td>
                                    <strong>{String(ev.coin)}</strong>{" "}
                                    {Boolean(ev.alert) && (
                                      <span className="badge badge-danger">{t("ALERT", "АЛЕРТ")}</span>
                                    )}
                                  </td>
                                  <td className={`num ${Math.abs(num(ev.mid_vs_mark_bps) ?? 0) >= 50 ? "neg" : ""}`}>
                                    {fmtBps(ev.mid_vs_mark_bps, 2, true)}
                                  </td>
                                  <td className={`num ${Math.abs(num(ev.mark_vs_oracle_bps) ?? 0) >= 50 ? "warn" : ""}`}>
                                    {fmtBps(ev.mark_vs_oracle_bps, 2, true)}
                                  </td>
                                  <td className={`num ${signCls(ev.funding_apr_pct)}`}>
                                    {fmtPct(ev.funding_apr_pct, 1)}
                                  </td>
                                  <td>
                                    <span className={`badge ${badgeCls}`}>{st}</span>
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    </section>
                  )}
                </div>
              )}

              {h4Events.length > 0 && (
                <section className="card">
                  <div className="section-header">
                    <SectionTitle k="secH4Disloc" />
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <Th k="colDex" />
                          <Th k="colPool" />
                          <Th k="colPegAdjSpread" num />
                          <Th k="colNetArb" num />
                          <Th k="colSlip25k" num />
                          <Th k="colV3Depth" num />
                          <Th k="colAlert" />
                        </tr>
                      </thead>
                      <tbody>
                        {h4Events.slice(0, 8).map((ev, idx) => (
                          <tr key={idx}>
                            <td>
                              <span className="badge badge-cyan">{String(ev.dex ?? "—")}</span>
                            </td>
                            <td className="mono">
                              <strong>{String(ev.pool ?? "—")}</strong>
                            </td>
                            <td className={`num ${signCls(ev.peg_adjusted_spread_bps)}`}>
                              {fmtBps(ev.peg_adjusted_spread_bps, 2, true)}
                            </td>
                            <td className={`num ${signCls(ev.net_arb_bps)}`}>
                              {fmtBps(ev.net_arb_bps, 2, true)}
                            </td>
                            <td className="num">{fmtBps(ev.slippage_25k_bps, 1)}</td>
                            <td className="num">{fmtUsd(ev.virtual_depth_usd, 0)}</td>
                            <td>
                              {Boolean(ev.alert) ? (
                                <span className="badge badge-danger">{t("ALERT", "АЛЕРТ")}</span>
                              ) : (
                                <span className="muted">—</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              )}
            </div>
          )}

          {tab === "execution" && (
            <div className="stack">
              {(mixedAccountWarn || liqWarnings.length > 0 || adlWarnings.length > 0) && (
                <div className="alert-banner" role="alert">
                  {mixedAccountWarn && (
                    <div>
                      <strong>{t("Account Hygiene:", "Гигиена аккаунта:")}</strong> {mixedAccountWarn}
                    </div>
                  )}
                  {liqWarnings.length > 0 && (
                    <div>
                      <strong>{t("Liquidation Warnings:", "Риск ликвидации:")}</strong> {liqWarnings.join(" · ")}
                    </div>
                  )}
                  {adlWarnings.length > 0 && (
                    <div>
                      <strong>{t("ADL Queue Warnings:", "Очередь ADL:")}</strong> {adlWarnings.join(" · ")}
                    </div>
                  )}
                </div>
              )}

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
                    {t("ALO fill", "Исп. ALO")} {fmtPct(data?.attribution?.real_alo_fill_rate_pct, 1, false)} ·{" "}
                    {t("Fee", "Комиссия")} {fmtBps(data?.attribution?.effective_fee_bps, 2)}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiNetAttr" />
                  <div className={`kpi-value ${signCls(data?.attribution?.net_attributed_pnl_usd)}`}>
                    {fmtUsd(data?.attribution?.net_attributed_pnl_usd)}
                  </div>
                  <div className="kpi-sub">
                    {t("VW Markout", "VW маркаут")} {fmtBps(data?.attribution?.vw_markout_bps, 2)}
                  </div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiApiQuota" />
                  <div className="kpi-value">{String(data?.attribution?.api_quota_health ?? "—")}</div>
                  <div className="kpi-sub">
                    {t("Min liq dist", "Мин. до ликв.")} {fmtPct(data?.attribution?.min_liq_distance_pct, 1, false)} ·{" "}
                    {t("Volume", "Объём")} {fmtUsd(data?.attribution?.total_volume_usd)}
                  </div>
                </div>
              </section>

              {tnRisk && (
                <section className="kpi-grid">
                  <div className="card">
                    <KpiLabel k="kpiTnDrawdown" />
                    <div className={`kpi-value ${(num(tnRisk.drawdown_pct) ?? 0) >= 3 ? "neg" : ""}`}>
                      {fmtPct(tnRisk.drawdown_pct, 2, false)}
                    </div>
                    <div className="kpi-sub">
                      HWM {fmtUsd(tnRisk.high_water_mark_usd)}
                      {num(tnRisk.kill_switch_drawdown_pct) !== null
                        ? ` · ${t("Limit", "Лимит")} ${fmtPct(tnRisk.kill_switch_drawdown_pct, 1, false)}`
                        : ""}
                    </div>
                  </div>
                  <div className="card">
                    <KpiLabel k="kpiTnFreeMargin" />
                    <div className="kpi-value">{fmtUsd(tnRisk.free_margin_usd)}</div>
                    <div className="kpi-sub">
                      {t("Gross lev", "Валовое плечо")} {fx(tnRisk.gross_leverage, 2, "x")}
                    </div>
                  </div>
                  <div className="card">
                    <KpiLabel k="kpiTnOrphanRisk" />
                    <div className={`kpi-value ${(num(tnRisk.orphan_notional_usd) ?? 0) > 0 ? "warn" : ""}`}>
                      {fmtUsd(tnRisk.orphan_notional_usd)}
                    </div>
                    <div className="kpi-sub">
                      {t("Orphan pos", "Сирот")}:{" "}
                      {typeof tnRisk.orphan_positions === "number"
                        ? tnRisk.orphan_positions
                        : tnRisk.orphan_positions && typeof tnRisk.orphan_positions === "object"
                          ? Object.keys(tnRisk.orphan_positions).length
                          : 0}
                    </div>
                  </div>
                  <div className="card">
                    <KpiLabel k="kpiTnMarkOracle" />
                    <div className={`kpi-value ${Math.abs(num(tnRisk.max_mark_oracle_dev_pct) ?? 0) >= 2.0 ? "warn" : ""}`}>
                      {fmtPct(tnRisk.max_mark_oracle_dev_pct, 2, false)}
                    </div>
                    <div className="kpi-sub">
                      {tnRisk.max_mark_oracle_dev_coin
                        ? `${t("Coin", "Монета")}: ${tnRisk.max_mark_oracle_dev_coin}`
                        : t("All marks aligned", "Марк и оракул синхронны")}
                    </div>
                  </div>
                </section>
              )}

              <section className="card">
                <div className="section-header">
                  <SectionTitle k="secPerCoin" />
                  <span className="section-sub">
                    {perCoinAttribution.length}{" "}
                    {p(
                      perCoinAttribution.length,
                      "tracked coin",
                      "tracked coins",
                      "отслеживаемая монета",
                      "отслеживаемые монеты",
                      "отслеживаемых монет",
                    )}
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
                      {perCoinAttribution.map((c) => {
                        const isFlat = Math.abs(c.position_sz) <= 1e-12;
                        const isLong = c.position_sz > 1e-12;
                        return (
                          <tr key={c.coin}>
                            <td>
                              <strong>{c.coin}</strong>
                            </td>
                            <td className={`num ${isFlat ? "muted" : isLong ? "pos" : "neg"}`}>
                              {isFlat
                                ? t("Flat", "Закрыта")
                                : `${isLong ? "L " : "S "}${fmtUsd(c.position_usd)}`}
                            </td>
                            <td className="mono">{c.leverage_mode}</td>
                            <td className="num">{c.fills_count}</td>
                            <td className={`num ${c.fills_count === 0 ? "muted" : ""}`}>
                              {c.fills_count === 0 ? "—" : fmtPct(c.maker_share_pct, 0, false)}
                            </td>
                            <td className={`num ${signCls(c.net_pnl_usd)}`}>{fmtUsd(c.net_pnl_usd)}</td>
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
                        );
                      })}
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
                            <span
                              className={`badge ${
                                rec.status === "ok"
                                  ? "badge-ok"
                                  : rec.derisk_mode === "orphan_unwind_only" &&
                                      rec.violations.length > 0 &&
                                      rec.violations.every((v) => v.startsWith("missing_quote:"))
                                    ? "badge-warn"
                                    : "badge-danger"
                              }`}
                            >
                              {rec.status}
                            </span>
                            {rec.derisk_mode && rec.derisk_mode !== "none" && (
                              <>
                                {" "}
                                <span className="badge badge-warn">{rec.derisk_mode}</span>
                              </>
                            )}
                          </td>
                          <td className="num">{rec.cancelled_count}</td>
                          <td className="num">{rec.placed_count}</td>
                          <td className="num">{fmtUsd(rec.total_notional_usd)}</td>
                          <td className="num">{fmtUsd(rec.high_water_mark_usd)}</td>
                          <td className="mono">
                            {[
                              rec.violations.length ? rec.violations.join(", ") : "",
                              rec.blocked_isolated_coins?.length
                                ? `${t("isolated blocked", "блок isolated")}: ${rec.blocked_isolated_coins.join(", ")}`
                                : "",
                              rec.blocked_leverage_coins?.length
                                ? `${t("leverage blocked", "блок плеча")}: ${rec.blocked_leverage_coins.join(", ")}`
                                : "",
                              rec.skipped_unquoted_coins?.length
                                ? `${t("skipped (no BBO)", "пропущено (нет BBO)")}: ${rec.skipped_unquoted_coins.join(", ")}`
                                : "",
                              rec.orphan_positions && Object.keys(rec.orphan_positions).length
                                ? `${t("orphans", "сироты")}: ${Object.keys(rec.orphan_positions).join(", ")}`
                                : "",
                              rec.foreign_coins?.length
                                ? `${t("MM/foreign", "MM/чужие")}: ${rec.foreign_coins.join(", ")}`
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
                      <div className="filter-row">
                        {cal.calibration_ok === false && (
                          <span className="badge badge-warn">
                            {t("Low sample / fallback", "Малая выборка")}
                          </span>
                        )}
                        <span className="badge badge-cyan">R² = {fx(cal.r2_fit, 3)}</span>
                      </div>
                    </div>
                    <KappaCalibrationChart cal={cal} />
                    <div className="kpi-sub" style={{ marginTop: 8 }}>
                      κ = {fx(cal.kappa_per_bp, 3, "/bp")} · σ_τ = {fmtBps(cal.empirical_sigma_tau_bps, 2)} ·{" "}
                      {t("Min half-spread", "Мин. полуспред")} = {fmtBps(cal.implied_min_half_spread_bps, 2)} ·{" "}
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
                  <div className="kpi-sub">{feBuild?.built_at || t("local / dev", "локально / dev")}</div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiBeSha" />
                  <div className="kpi-value">{shortSha(beSha)}</div>
                  <div className="kpi-sub">{data?.build_info?.built_at || t("local / dev", "локально / dev")}</div>
                </div>
                <div className="card">
                  <KpiLabel k="kpiHeartbeat" />
                  <div className="kpi-value">
                    {data?.worker_status ? t("ONLINE", "ОНЛАЙН") : t("EXTERNAL / CRON", "ВНЕШНИЙ / CRON")}
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
                    {data?.worker_status?.testnet_execution_enabled
                      ? t("ENABLED", "ВКЛЮЧЕН")
                      : t("READ-ONLY", "ТОЛЬКО ЧТЕНИЕ")}
                  </div>
                  <div className="kpi-sub">
                    Telegram: {data?.worker_status?.telegram_enabled ? t("ON", "ВКЛ") : t("OFF", "ВЫКЛ")}
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
                            {j.last_error && (
                              <div className="section-sub neg mono" style={{ whiteSpace: "normal", maxWidth: 320 }}>
                                {j.last_error}
                              </div>
                            )}
                          </td>
                          <td className="mono">{j.last_finished_at_utc ?? "—"}</td>
                          <td className="num">{fx(j.last_duration_s, 1, t("s", "с"))}</td>
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
                              {info.exists ? t("present", "есть") : t("pending", "ожидание")}
                            </span>
                          </td>
                          <td className="mono">{info.updated_at_utc ?? "—"}</td>
                          <td className="num">{fx(info.age_minutes, 1)}</td>
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
