import { useMemo, useState } from "react";
import { MultiSeriesChart, OlsScatterChart, type SeriesLine } from "./Charts";
import { KpiLabel, SectionTitle, Th } from "./Tooltip";
import { useLang } from "../i18n";
import type { AnalyticsPayload, HorizonMetrics, LeaderboardRow, OverviewPayload } from "../types";

const SERIES_COLORS = ["#10b981", "#38bdf8", "#a855f7", "#f59e0b", "#ec4899", "#94a3b8", "#f97316", "#22d3ee"];
const MONTHS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"];
const MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTH_LABELS_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"];
const MONTH_NAMES_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const MONTH_NAMES_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"];

function fmtNum(v: number | null | undefined, digits = 2, suffix = "", signed = false): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const prefix = signed && v >= 0 ? "+" : "";
  return `${prefix}${v.toFixed(digits)}${suffix}`;
}

function fmtUsd(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  if (Math.abs(v) >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  return `$${v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function corrCellStyle(rho: number | null): { backgroundColor: string; color: string } {
  if (rho === null) return { backgroundColor: "transparent", color: "#475569" };
  if (rho >= 0.999) return { backgroundColor: "rgba(56, 189, 248, 0.22)", color: "#38bdf8" };
  if (rho > 0.25) return { backgroundColor: `rgba(239, 68, 68, ${Math.min(0.45, Math.abs(rho) * 0.55)})`, color: "#fca5a5" };
  if (rho < -0.15) return { backgroundColor: `rgba(16, 185, 129, ${Math.min(0.45, Math.abs(rho) * 0.65)})`, color: "#6ee7b7" };
  return { backgroundColor: "rgba(148, 163, 184, 0.10)", color: "#e2e8f0" };
}

export function AnalyticsRiskTab({
  analytics,
  overview,
}: {
  analytics: AnalyticsPayload | null;
  overview: OverviewPayload | null;
}) {
  const { lang, t, p } = useLang();
  const [feeMode, setFeeMode] = useState<"gross" | "net">("net");
  const [showMode, setShowMode] = useState<"returns" | "apy">("returns");
  const [categoryFilter, setCategoryFilter] = useState<string>("ALL");
  const [periodDays, setPeriodDays] = useState<number>(0); // 0 = All
  const [rebaseWindow, setRebaseWindow] = useState<boolean>(true);
  const [hiddenSeries, setHiddenSeries] = useState<Record<string, boolean>>({});
  const [showMethodology, setShowMethodology] = useState<boolean>(false);

  const factors = useMemo(() => Object.keys(analytics?.market_exposure ?? {}), [analytics]);
  const [selectedFactor, setSelectedFactor] = useState<string>("BTC");
  const activeFactor = factors.includes(selectedFactor) ? selectedFactor : (factors[0] ?? "BTC");
  const factorBlock = analytics?.market_exposure?.[activeFactor];

  const [selectedOlsStrat, setSelectedOlsStrat] = useState<string>("");
  const activeOlsRow = useMemo(() => {
    const rows = factorBlock?.rows ?? [];
    if (rows.length === 0) return null;
    return rows.find((r) => r.id === selectedOlsStrat) ?? rows[0];
  }, [factorBlock, selectedOlsStrat]);

  const strategyRows = useMemo(
    () => (analytics?.leaderboard ?? []).filter((r) => !r.is_benchmark),
    [analytics],
  );
  const [monthlyStratId, setMonthlyStratId] = useState<string>("");
  const activeMonthlyRow = useMemo(
    () => strategyRows.find((r) => r.id === monthlyStratId) ?? strategyRows[0] ?? null,
    [strategyRows, monthlyStratId],
  );

  const filteredLeaderboard = useMemo(() => {
    const rows = analytics?.leaderboard ?? [];
    if (categoryFilter === "ALL") return rows;
    return rows.filter((r) => r.category === categoryFilter);
  }, [analytics, categoryFilter]);

  const categories = useMemo(() => {
    const set = new Set((analytics?.leaderboard ?? []).map((r) => r.category));
    return ["ALL", ...Array.from(set)];
  }, [analytics]);

  const { chartLines, chartDates } = useMemo(() => {
    const curvesMap = analytics?.curves ?? {};
    const keys = Object.keys(curvesMap);
    const lines: SeriesLine[] = [];
    let longestDates: string[] = [];

    const parseMs = (raw: string): number | null => {
      if (!raw) return null;
      const ms = Date.parse(raw.includes("T") ? raw : `${raw}T00:00:00Z`);
      return Number.isFinite(ms) ? ms : null;
    };

    let globalMaxMs: number | null = null;
    if (periodDays > 0) {
      for (const id of keys) {
        if (hiddenSeries[id]) continue;
        const ds = curvesMap[id]?.dates ?? [];
        if (ds.length > 0) {
          const ms = parseMs(ds[ds.length - 1]);
          if (ms !== null && (globalMaxMs === null || ms > globalMaxMs)) {
            globalMaxMs = ms;
          }
        }
      }
    }
    const cutoffMs = periodDays > 0 && globalMaxMs !== null ? globalMaxMs - periodDays * 86_400_000 : null;

    keys.forEach((id, idx) => {
      if (hiddenSeries[id]) return;
      const c = curvesMap[id];
      const rawShares = feeMode === "gross" ? c.gross_shares : c.net_shares;
      let startIdx = 0;
      if (cutoffMs !== null && c.dates.length === rawShares.length && c.dates.length > 0) {
        const found = c.dates.findIndex((d) => {
          const ms = parseMs(d);
          return ms !== null && ms >= cutoffMs;
        });
        if (found === -1) {
          startIdx = Math.max(0, rawShares.length - 2);
        } else {
          // Include one anchor point at/just before cutoff when available so window rebasing is exact
          startIdx = found > 0 && (parseMs(c.dates[found]) ?? 0) > cutoffMs ? found - 1 : found;
        }
      } else if (periodDays > 0) {
        startIdx = Math.max(0, rawShares.length - periodDays);
      }
      const subShares = rawShares.slice(startIdx);
      const subDates = c.dates.slice(startIdx);
      if (subDates.length > longestDates.length) longestDates = subDates;
      const cleanShares: number[] = [];
      let lastValid = 1;
      for (const raw of subShares) {
        if (typeof raw === "number" && Number.isFinite(raw) && raw > 0) {
          lastValid = raw;
        }
        cleanShares.push(lastValid);
      }
      const base = rebaseWindow ? (cleanShares[0] || 1) : 1;
      const pctVals = cleanShares.map((s) => (base > 0 ? (s / base - 1) * 100 : 0));
      lines.push({
        id,
        name: c.name,
        color: SERIES_COLORS[idx % SERIES_COLORS.length],
        values: pctVals,
        dates: subDates,
      });
    });

    return { chartLines: lines, chartDates: longestDates };
  }, [analytics, feeMode, periodDays, rebaseWindow, hiddenSeries]);

  const getHorizons = (r: LeaderboardRow): HorizonMetrics => {
    if (feeMode === "gross") {
      return showMode === "returns" ? r.gross_returns : r.gross_apy;
    }
    return showMode === "returns" ? r.net_returns : r.net_apy;
  };

  const csvCell = (val: unknown): string => {
    if (val === null || val === undefined) return "";
    if (typeof val === "number") return Number.isFinite(val) ? String(val) : "";
    let s = String(val);
    if (/^[=+\t\r@]/.test(s) || (s.startsWith("-") && Number.isNaN(Number(s)))) {
      s = `'${s}`;
    }
    if (/[",\n\r]/.test(s)) {
      return `"${s.replace(/"/g, '""')}"`;
    }
    return s;
  };

  const exportCsv = () => {
    const header = ["Strategy", "Category", "7D", "30D", "YTD", "All-Time", "Sharpe90D", "Sortino90D", "MaxDD%", "PosDays%", "SharePrice"];
    const lines = filteredLeaderboard.map((r) => {
      const h = getHorizons(r);
      const sh = feeMode === "gross" ? r.sharpe_gross : r.sharpe_net;
      const so = feeMode === "gross" ? r.sortino_gross : r.sortino_net;
      const dd = feeMode === "gross" ? r.max_dd_gross_pct : r.max_dd_net_pct;
      const sp = feeMode === "gross" ? r.share_price_gross : r.share_price_net;
      return [r.name, r.category, h["7d"], h["30d"], h.ytd, h.all_time, sh, so, dd, r.positive_days_pct, sp]
        .map(csvCell)
        .join(",");
    });
    const csv = [header.join(","), ...lines].join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `hl_quant_analytics_${feeMode}_${showMode}.csv`;
    a.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  const autopilot = overview?.autopilot;
  const spotPerp = overview?.spot_perp_paper;
  const spotPerpScan = overview?.spot_perp_scan;
  const corr = analytics?.correlation;
  const risk = analytics?.risk_exposure;
  const dayUnit = t("d", "д");
  const modeLabel = feeMode === "gross" ? t("GROSS", "БРУТТО") : t("NET", "НЕТТО");

  return (
    <div className="stack">
      {/* 1. Master Portfolio & Spot+Perp Flagship KPI Strip */}
      <section className="kpi-grid">
        <div className="card">
          <KpiLabel k="kpiAutopilot" />
          <div className="kpi-value">{fmtUsd(autopilot?.gross_equity_usd)}</div>
          <div className="kpi-sub">
            Gross {fmtNum(autopilot?.gross_return_pct, 2, "%", true)} · Net HWM{" "}
            {fmtNum(autopilot?.net_return_pct, 2, "%", true)}
          </div>
        </div>
        <div className="card">
          <KpiLabel k="kpiAutopilotLev" />
          <div className="kpi-value">
            {autopilot?.risk_telemetry ? `${fmtNum(autopilot.risk_telemetry.gross_leverage, 2)}x` : "—"}
          </div>
          <div className="kpi-sub">
            CTA {fmtNum(autopilot?.risk_telemetry?.directional_sleeve_pct, 1, "%")} (≤5%) · {t("Drift", "Отклонение")}{" "}
            {fmtNum(autopilot?.risk_telemetry?.max_weight_drift_pct, 2, "%")}
          </div>
        </div>
        <div className="card">
          <KpiLabel k="kpiSpotPerp" />
          <div className="kpi-value">{fmtUsd(spotPerp?.equity_usd)}</div>
          <div className="kpi-sub">
            Blended APR {fmtNum(spotPerp?.blended_portfolio_apr_pct, 2, "%", true)} · HLP{" "}
            {fmtNum(spotPerp?.hlp_reserve_share_pct, 1, "%")}
          </div>
        </div>
        <div className="card">
          <KpiLabel k="kpiSpotPerpLev" />
          <div className="kpi-value">
            {spotPerp?.risk_telemetry ? `${fmtNum(spotPerp.risk_telemetry.gross_leverage, 2)}x` : "—"}
          </div>
          <div className="kpi-sub">
            {t("30D max", "Макс. 30Д")} {fmtNum(spotPerp?.risk_telemetry?.leverage_30d_max, 2, "x")} · Δ{" "}
            {fmtNum(spotPerp?.risk_telemetry?.net_delta_pct, 3, "%", true)}
          </div>
        </div>
        <div className="card">
          <KpiLabel k="kpiNavGuards" />
          <div className="kpi-value">{risk?.total_nav_guard_triggers ?? 0}</div>
          <div className="kpi-sub">
            {t("Circuit-breaker triggers across desk ledgers", "Срабатывания предохранителей во всех журналах")}
          </div>
        </div>
      </section>

      {/* 2. Neutral-Style Performance vs Buy & Hold Leaderboard */}
      <section className="card">
        <div className="section-header">
          <div>
            <SectionTitle k="secLeaderboard" />
            <div className="section-sub">
              {t(
                "Trailing 90D Sharpe & Sortino · HWM Net-of-Fee simulation · Negative returns never annualized",
                "Шарп и Сортино за последние 90Д · симуляция net после комиссий с HWM · отрицательная доходность не пересчитывается в годовую",
              )}
            </div>
          </div>
          <div className="filter-row">
            <div className="segmented" role="group" aria-label={t("Fee mode", "Учёт комиссий")}>
              <button
                type="button"
                className={`btn btn-sm ${feeMode === "gross" ? "tab-active" : ""}`}
                onClick={() => setFeeMode("gross")}
              >
                {t("Gross", "Брутто")}
              </button>
              <button
                type="button"
                className={`btn btn-sm ${feeMode === "net" ? "tab-active" : ""}`}
                onClick={() => setFeeMode("net")}
              >
                {t("Net (HWM)", "Нетто (HWM)")}
              </button>
            </div>
            <div className="segmented" role="group" aria-label={t("Display mode", "Режим отображения")}>
              <button
                type="button"
                className={`btn btn-sm ${showMode === "returns" ? "tab-active" : ""}`}
                onClick={() => setShowMode("returns")}
              >
                {t("Returns", "Доходность")}
              </button>
              <button
                type="button"
                className={`btn btn-sm ${showMode === "apy" ? "tab-active" : ""}`}
                onClick={() => setShowMode("apy")}
              >
                APY
              </button>
            </div>
            <select
              className="select"
              aria-label={t("Filter leaderboard category", "Фильтр по категории")}
              value={categoryFilter}
              onChange={(e) => setCategoryFilter(e.target.value)}
            >
              {categories.map((c) => (
                <option key={c} value={c}>
                  {c === "ALL" ? t("All Categories", "Все категории") : c}
                </option>
              ))}
            </select>
            <button type="button" className="btn btn-sm" onClick={() => setShowMethodology((v) => !v)}>
              {showMethodology ? t("Hide Methodology", "Скрыть методику") : t("How calculated", "Как считается")}
            </button>
            <button type="button" className="btn btn-sm" onClick={exportCsv}>
              {t("Export CSV", "Экспорт CSV")}
            </button>
          </div>
        </div>

        {showMethodology && (
          <div className="alert-banner">
            {lang === "ru" ? (
              <>
                <strong>Методика (стандарт Neutral Trade):</strong> Шарп и Сортино считаются по дневным
                лог-доходностям за последние 90 дней (годовой пересчёт на 365 дней). Net-значения учитывают постоянную
                годовую комиссию за управление и комиссию за результат по High-Water Mark (HWM) — только выше
                предыдущего пика net NAV. В режиме APY отрицательная доходность за период показывается как
                &laquo;—&raquo; (в годовую не пересчитывается; переключитесь на «Доходность», чтобы увидеть убытки).
                Плюсовые дни, серии побед/поражений и восстановление после просадки считаются по дневным закрытиям NAV.
              </>
            ) : (
              <>
                <strong>Methodology (Neutral Trade Standard):</strong> Sharpe &amp; Sortino use trailing 90-day daily log
                returns (365d annualization). Net figures apply continuous annual management fee + High-Water Mark (HWM)
                performance fee strictly above peak net NAV. Under APY view, negative period returns are shown as
                &ldquo;—&rdquo; (never annualized; switch to Returns for period losses). Positive days, win/loss streaks,
                and drawdown recovery days use daily NAV closes.
              </>
            )}
          </div>
        )}

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <Th k="colStratBench" />
                <Th k="colCategory" />
                <Th k="col7d" num />
                <Th k="col30d" num />
                <Th k="colYtd" num />
                <Th k="colAllTime" num />
                <Th k="colSharpe" num />
                <Th k="colSortino" num />
                <Th k="colMaxDd" num />
                <Th k="colDdRecov" num />
                <Th k="colPosDays" num />
                <Th k="colWinLoss" num />
                <Th k="colSharePx" num />
              </tr>
            </thead>
            <tbody>
              {filteredLeaderboard.map((r) => {
                const h = getHorizons(r);
                const sh = feeMode === "gross" ? r.sharpe_gross : r.sharpe_net;
                const so = feeMode === "gross" ? r.sortino_gross : r.sortino_net;
                const dd = feeMode === "gross" ? r.max_dd_gross_pct : r.max_dd_net_pct;
                const ddRec =
                  feeMode === "gross"
                    ? r.dd_recovery_days
                    : (r.dd_recovery_net_days !== undefined ? r.dd_recovery_net_days : r.dd_recovery_days);
                const ddUnder =
                  feeMode === "gross"
                    ? (r.current_underwater_days ?? 0)
                    : (r.current_underwater_net_days ?? r.current_underwater_days ?? 0);
                const sp = r.is_benchmark ? r.spot_price_usd : feeMode === "gross" ? r.share_price_gross : r.share_price_net;
                return (
                  <tr key={r.id}>
                    <td>
                      <strong>{r.name}</strong>
                      <div className="section-sub">{r.fee_schedule}</div>
                    </td>
                    <td>
                      <span className={`badge ${r.is_benchmark ? "badge-warn" : "badge-cyan"}`}>{r.category}</span>
                    </td>
                    {(["7d", "30d", "ytd", "all_time"] as const).map((k) => {
                      const val = h[k];
                      const cls = val === null ? "muted" : val >= 0 ? "pos" : "neg";
                      return (
                        <td key={k} className={`num ${cls}`}>
                          {fmtNum(val, 2, "%", true)}
                        </td>
                      );
                    })}
                    <td className="num">{fmtNum(sh, 2)}</td>
                    <td className="num">{fmtNum(so, 2)}</td>
                    <td className={`num ${dd > 5 ? "neg" : ""}`}>{fmtNum(dd, 2, "%")}</td>
                    <td className="num">
                      {ddRec !== null && ddRec !== undefined ? `${fmtNum(ddRec, 1)}${dayUnit}` : "—"}
                      {ddUnder > 0 ? (
                        <div className="section-sub neg">
                          {t("underwater", "под водой")} {fmtNum(ddUnder, 1)}
                          {dayUnit}
                        </div>
                      ) : null}
                    </td>
                    <td className="num">{fmtNum(r.positive_days_pct, 1, "%")}</td>
                    <td className="num">
                      {r.longest_win_streak}
                      {dayUnit} / {r.longest_loss_streak}
                      {dayUnit}
                    </td>
                    <td className="num">
                      {r.is_benchmark ? fmtUsd(sp, 2) : sp !== null && sp !== undefined ? `$${fmtNum(sp, 4)}` : "—"}
                    </td>
                  </tr>
                );
              })}
              {filteredLeaderboard.length === 0 && (
                <tr>
                  <td colSpan={13} className="muted">
                    {t(
                      "No strategy ticks persisted yet. Run Spot+Perp or Autopilot ticks to populate analytics.",
                      "Тиков стратегий пока нет. Запустите тики Spot+Perp или Autopilot, чтобы заполнить аналитику.",
                    )}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* 3. Multi-Strategy Rebased Equity Comparison Chart */}
      <section className="card">
        <div className="section-header">
          <div>
            <SectionTitle k="secCumPerf" suffix={` (${modeLabel})`} />
            <div className="section-sub">
              {t(
                "Toggle strategies & benchmarks · rebase to window start",
                "Включайте/выключайте стратегии и бенчмарки · отсчёт от начала окна",
              )}
            </div>
          </div>
          <div className="filter-row">
            {[
              { label: t("7D", "7Д"), days: 7 },
              { label: t("30D", "30Д"), days: 30 },
              { label: t("90D", "90Д"), days: 90 },
              { label: t("All", "Всё"), days: 0 },
            ].map((p) => (
              <button
                key={p.label}
                type="button"
                className={`btn btn-sm ${periodDays === p.days ? "tab-active" : ""}`}
                onClick={() => setPeriodDays(p.days)}
              >
                {p.label}
              </button>
            ))}
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={rebaseWindow}
                onChange={(e) => setRebaseWindow(e.target.checked)}
              />
              {t("Rebase to window", "Отсчёт от начала окна")}
            </label>
          </div>
        </div>

        <MultiSeriesChart series={chartLines} dates={chartDates} />

        <div className="filter-row" style={{ marginTop: 10 }}>
          {Object.values(analytics?.curves ?? {}).map((c, idx) => {
            const off = Boolean(hiddenSeries[c.id]);
            const color = SERIES_COLORS[idx % SERIES_COLORS.length];
            return (
              <button
                key={c.id}
                type="button"
                className="pill"
                style={{
                  opacity: off ? 0.4 : 1,
                  borderColor: off ? "var(--border)" : color,
                  cursor: "pointer",
                }}
                onClick={() => setHiddenSeries((prev) => ({ ...prev, [c.id]: !prev[c.id] }))}
              >
                <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, display: "inline-block" }} />
                {c.name}
              </button>
            );
          })}
        </div>
      </section>

      {/* 4. Risk & Correlation Grid: Pearson Heatmap + OLS Market Exposure */}
      <section className="grid-2">
        <div className="card">
          <div className="section-header">
            <div>
              <SectionTitle k="secCorrelation" />
              <div className="section-sub">
                {t("Lower-triangular daily log-return correlations", "Корреляции дневных лог-доходностей (нижний треугольник)")}{" "}
                ({corr?.overlap_days ?? 0}{" "}
                {p(corr?.overlap_days ?? 0, "overlapping day", "overlapping days", "общий день", "общих дня", "общих дней")})
              </div>
            </div>
          </div>
          {corr && corr.ids.length > 0 ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <Th k="colCorrStrategy" />
                    {corr.ids.map((id) => (
                      <Th
                        key={id}
                        num
                        label={id.slice(0, 10)}
                        tip={t(
                          `Correlation of each row strategy with "${corr.labels?.[id] ?? id}" (−1…+1).`,
                          `Корреляция стратегии из строки со стратегией «${corr.labels?.[id] ?? id}» (от −1 до +1).`,
                        )}
                      />
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {corr.ids.map((rowId, i) => (
                    <tr key={rowId}>
                      <td>
                        <strong>{corr.labels?.[rowId] ?? rowId}</strong>
                      </td>
                      {corr.ids.map((colId, j) => {
                        const val = corr.matrix[i]?.[j] ?? null;
                        const style = corrCellStyle(val);
                        return (
                          <td key={colId} className="num" style={style}>
                            {val === null ? "" : fmtNum(val, 2)}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="muted">
              {t(
                "Waiting for at least 3 overlapping days across desk strategies.",
                "Ждём минимум 3 общих дня у стратегий.",
              )}
            </div>
          )}
        </div>

        <div className="card">
          <div className="section-header">
            <div>
              <SectionTitle k="secOls" />
              <div className="section-sub">
                {t("Proof of market neutrality (β ≈ 0) against", "Проверка рыночной нейтральности (β ≈ 0) относительно")}{" "}
                {activeFactor} ({factorBlock?.factor_days ?? 0}
                {dayUnit})
              </div>
            </div>
            <div className="filter-row">
              {factors.map((f) => (
                <button
                  key={f}
                  type="button"
                  className={`btn btn-sm ${activeFactor === f ? "tab-active" : ""}`}
                  onClick={() => setSelectedFactor(f)}
                >
                  {f}
                </button>
              ))}
            </div>
          </div>
          {factorBlock && factorBlock.rows.length > 0 ? (
            <>
              <div className="table-wrap" style={{ marginBottom: 12 }}>
                <table>
                  <thead>
                    <tr>
                      <Th k="colOlsStrategy" />
                      <Th k="colBeta" num label={`β ${t("to", "к")} ${activeFactor}`} />
                      <Th k="colR2" num />
                      <Th k="colAlpha" num />
                      <Th k="colDays" num />
                    </tr>
                  </thead>
                  <tbody>
                    {factorBlock.rows.map((r) => {
                      const active = activeOlsRow?.id === r.id;
                      return (
                        <tr
                          key={r.id}
                          onClick={() => setSelectedOlsStrat(r.id)}
                          style={{
                            cursor: "pointer",
                            backgroundColor: active ? "rgba(56, 189, 248, 0.10)" : undefined,
                          }}
                        >
                          <td>
                            <strong>{r.name}</strong>
                          </td>
                          <td className="num">{fmtNum(r.beta, 3)}</td>
                          <td className="num">{fmtNum(r.r2_pct, 1, "%")}</td>
                          <td className={`num ${(r.alpha_ann_pct ?? 0) >= 0 ? "pos" : "neg"}`}>
                            {fmtNum(r.alpha_ann_pct, 1, "%", true)}
                          </td>
                          <td className="num">{r.days}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {activeOlsRow && (
                <OlsScatterChart
                  points={activeOlsRow.scatter}
                  beta={activeOlsRow.beta}
                  alphaAnnPct={activeOlsRow.alpha_ann_pct}
                  factorName={activeFactor}
                  strategyName={activeOlsRow.name}
                />
              )}
            </>
          ) : (
            <div className="muted">
              {t(
                "Run Autopilot tick to populate HyperCore BTC/ETH/HYPE benchmark history for OLS.",
                "Запустите тик Autopilot, чтобы накопить историю бенчмарков BTC/ETH/HYPE для OLS.",
              )}
            </div>
          )}
        </div>
      </section>

      {/* 5. Monthly Historical Returns Matrix */}
      <section className="card">
        <div className="section-header">
          <div>
            <SectionTitle k="secMonthly" suffix={` (${modeLabel})`} />
            <div className="section-sub">
              {t("Compound monthly return heatmap per strategy", "Тепловая карта сложной месячной доходности по стратегии")}
            </div>
          </div>
          <select
            className="select"
            aria-label={t("Select strategy for monthly returns", "Стратегия для месячной доходности")}
            value={activeMonthlyRow?.id ?? ""}
            onChange={(e) => setMonthlyStratId(e.target.value)}
          >
            {strategyRows.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </select>
        </div>
        {activeMonthlyRow && (feeMode === "gross" ? activeMonthlyRow.monthly_gross : activeMonthlyRow.monthly_net).length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <Th k="colYear" />
                  {MONTH_LABELS.map((m, i) => (
                    <Th
                      key={m}
                      num
                      label={lang === "ru" ? MONTH_LABELS_RU[i] : m}
                      tip={t(
                        `Strategy return for ${MONTH_NAMES_EN[i]} of that year.`,
                        `Доходность стратегии за ${MONTH_NAMES_RU[i]} этого года.`,
                      )}
                    />
                  ))}
                  <Th k="colYearYtd" num />
                </tr>
              </thead>
              <tbody>
                {(feeMode === "gross" ? activeMonthlyRow.monthly_gross : activeMonthlyRow.monthly_net).map((yr) => (
                  <tr key={yr.year}>
                    <td className="mono">
                      <strong>{yr.year}</strong>
                    </td>
                    {MONTHS.map((m) => {
                      const v = yr.months[m];
                      return (
                        <td
                          key={m}
                          className={`num ${v === undefined ? "muted" : v >= 0 ? "pos" : "neg"}`}
                        >
                          {v === undefined ? "—" : fmtNum(v, 2, "%", true)}
                        </td>
                      );
                    })}
                    <td className={`num ${(yr.ytd ?? 0) >= 0 ? "pos" : "neg"}`}>
                      <strong>{fmtNum(yr.ytd, 2, "%", true)}</strong>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="muted">
            {t(
              "Monthly matrix will populate once multi-day NAV closes accrue.",
              "Матрица заполнится, когда накопятся дневные закрытия NAV за несколько дней.",
            )}
          </div>
        )}
      </section>

      {/* 6. Live Risk & Exposure Telemetry + Autopilot & Spot+Perp Drilldown */}
      <section className="grid-2">
        <div className="card">
          <div className="section-header">
            <div>
              <SectionTitle k="secLiveRisk" />
              <div className="section-sub">
                {t(
                  "Current / 30D Avg / 30D Max Leverage · Venue & Market-Cap Concentration",
                  "Плечо: текущее / ср. 30Д / макс. 30Д · концентрация по площадкам и капитализации",
                )}
              </div>
            </div>
          </div>
          <div className="table-wrap" style={{ marginBottom: 14 }}>
            <table>
              <thead>
                <tr>
                  <Th k="colRiskStrategy" />
                  <Th k="colLevCur" num />
                  <Th k="colLev30Avg" num />
                  <Th k="colLev30Max" num />
                  <Th k="colNetExp" num />
                  <Th k="colNavGuard" num />
                </tr>
              </thead>
              <tbody>
                {(risk?.leverage_by_strategy ?? []).map((row) => (
                  <tr key={row.strategy}>
                    <td>
                      <strong>{row.strategy}</strong>
                    </td>
                    <td className="num">{fmtNum(row.current_leverage, 2)}x</td>
                    <td className="num">{fmtNum(row.leverage_30d_avg, 2)}x</td>
                    <td className="num">{fmtNum(row.leverage_30d_max, 2)}x</td>
                    <td className="num">{fmtNum(row.net_exposure_pct, 2, "%", true)}</td>
                    <td className="num">
                      <span className={`badge ${row.nav_guard_triggers > 0 ? "badge-warn" : "badge-ok"}`}>
                        {row.nav_guard_triggers}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="grid-2" style={{ marginBottom: 0 }}>
            <div>
              <KpiLabel k="blkVenueConc" />
              {(risk?.venue_concentration ?? []).map((v) => (
                <div key={v.label} className="bar-row">
                  <div className="bar-meta">
                    <span>{v.label}</span>
                    <span className="mono">
                      {fmtUsd(v.usd, 0)} ({fmtNum(v.share_pct, 1)}%)
                    </span>
                  </div>
                  <div className="bar-track">
                    <div className="bar-fill" style={{ width: `${Math.min(100, v.share_pct)}%` }} />
                  </div>
                </div>
              ))}
            </div>
            <div>
              <KpiLabel k="blkTierConc" />
              {(risk?.asset_tier_concentration ?? []).map((t) => (
                <div key={t.label} className="bar-row">
                  <div className="bar-meta">
                    <span>{t.label}</span>
                    <span className="mono">
                      {fmtUsd(t.usd, 0)} ({fmtNum(t.share_pct, 1)}%)
                    </span>
                  </div>
                  <div className="bar-track">
                    <div className="bar-fill bar-fill-green" style={{ width: `${Math.min(100, t.share_pct)}%` }} />
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="card">
          <div className="section-header">
            <div>
              <SectionTitle k="secSleeves" />
              <div className="section-sub">
                {t(
                  "Deadband 3.0% rebalance · Hard ≤5% directional sleeve cap",
                  "Ребаланс при отклонении 3,0% · жёсткий лимит направленного рукава ≤5%",
                )}
              </div>
            </div>
          </div>
          <div className="table-wrap" style={{ marginBottom: 14 }}>
            <table>
              <thead>
                <tr>
                  <Th k="colSleeve" />
                  <Th k="colSleeveCategory" />
                  <Th k="colTarget" num />
                  <Th k="colActual" num />
                  <Th k="colDriftPct" num />
                  <Th k="colAllocation" num />
                </tr>
              </thead>
              <tbody>
                {(autopilot?.sleeves ?? []).map((s) => (
                  <tr key={s.name}>
                    <td>
                      <strong>{s.name}</strong>
                    </td>
                    <td>{s.category}</td>
                    <td className="num">{fmtNum(s.target_weight_pct, 1)}%</td>
                    <td className="num">{fmtNum(s.actual_weight_pct, 1)}%</td>
                    <td className={`num ${Math.abs(s.drift_pct) >= 3 ? "warn" : ""}`}>
                      {fmtNum(s.drift_pct, 2, "%", true)}
                    </td>
                    <td className="num">{fmtUsd(s.allocation_usd, 0)}</td>
                  </tr>
                ))}
                {(autopilot?.sleeves ?? []).length === 0 && (
                  <tr>
                    <td colSpan={6} className="muted">
                      {t("Awaiting first Autopilot tick.", "Ожидание первого тика Autopilot.")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {spotPerpScan && (spotPerpScan.opportunities ?? []).length > 0 && (
            <>
              <KpiLabel
                k="blkSpotPerpScanner"
                style={{ marginTop: 10 }}
                suffix={` (HLP ${fmtNum(spotPerpScan.hlp_apr_pct, 2, "%", true)})`}
              />
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <Th k="colPair" />
                      <Th k="colTier" />
                      <Th k="colPerpApr" num />
                      <Th k="colNetDepApr" num />
                      <Th k="colExecBasis" num />
                      <Th k="colL2Slip" num />
                      <Th k="colScanStatus" />
                    </tr>
                  </thead>
                  <tbody>
                    {spotPerpScan.opportunities.slice(0, 8).map((o) => (
                      <tr key={o.base_symbol}>
                        <td>
                          <strong>
                            {o.spot_token}→{o.base_symbol}
                          </strong>{" "}
                          <span className="muted mono">{o.spot_pair}</span>
                        </td>
                        <td>{o.asset_tier}</td>
                        <td className="num pos">{fmtNum(o.funding_apr_pct, 2, "%", true)}</td>
                        <td className="num">{fmtNum(o.net_deposit_apr_pct, 2, "%", true)}</td>
                        <td className="num">{fmtNum(o.exec_basis_bps, 1, " bps", true)}</td>
                        <td className="num">{fmtNum(o.entry_l2_slip_bps, 1, " bps")}</td>
                        <td>
                          <span className={`badge ${o.eligible ? "badge-ok" : "badge-warn"}`}>
                            {o.eligible ? "ELIGIBLE" : o.reject_reason || "SKIP"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      </section>
    </div>
  );
}
