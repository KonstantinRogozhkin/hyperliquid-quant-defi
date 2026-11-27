import { useLang } from "../i18n";
import type { CoinCalibration, OlsScatterPoint } from "../types";

export interface SeriesLine {
  id: string;
  name: string;
  color: string;
  values: number[];
  dates?: string[];
}

function parseDateMs(raw: string): number | null {
  if (!raw) return null;
  const iso = raw.includes("T") ? raw : `${raw}T00:00:00Z`;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

export function MultiSeriesChart({
  series,
  dates,
  height = 210,
}: {
  series: SeriesLine[];
  dates: string[];
  height?: number;
}) {
  const { t } = useLang();
  const valid = series.filter((s) => s.values.length >= 2);
  if (valid.length === 0) {
    return (
      <div className="muted">
        {t(
          "Waiting for at least 2 observations across selected strategies...",
          "Ждём минимум 2 наблюдения по выбранным стратегиям...",
        )}
      </div>
    );
  }

  const width = 760;
  const pl = 48;
  const pr = 18;
  const pt = 14;
  const pb = 26;

  const allVals = valid.flatMap((s) => s.values);
  const minVal = Math.min(...allVals);
  const maxVal = Math.max(...allVals);
  const span = Math.max(maxVal - minVal, 1e-4);
  const maxLen = Math.max(...valid.map((s) => s.values.length), 2);

  // Align multi-resolution series (4h strategy ticks vs 1d benchmarks) on true calendar timestamps
  // whenever per-series `dates` are present; fall back to index-based spacing otherwise.
  const parsedSeriesDates = valid.map((s) =>
    s.dates && s.dates.length === s.values.length ? s.dates.map(parseDateMs) : null,
  );
  const canUseTimeAxis = parsedSeriesDates.every(
    (arr) => arr !== null && arr.every((ms): ms is number => ms !== null),
  );
  let minTs = 0;
  let maxTs = 0;
  if (canUseTimeAxis) {
    const allTs = (parsedSeriesDates as number[][]).flat();
    minTs = Math.min(...allTs);
    maxTs = Math.max(...allTs);
  }
  const useTimeAxis = canUseTimeAxis && maxTs > minTs;

  const sx = (i: number, len: number) =>
    pl + ((width - pl - pr) * i) / Math.max(len - 1, 1);
  const sxTime = (ms: number) =>
    pl + ((width - pl - pr) * (ms - minTs)) / Math.max(maxTs - minTs, 1);
  const sy = (v: number) => height - pb - ((height - pt - pb) * (v - minVal)) / span;
  const zeroY = minVal <= 0 && maxVal >= 0 ? sy(0) : null;

  const firstDate = useTimeAxis
    ? new Date(minTs).toISOString().slice(0, 10)
    : (dates[0]?.slice(0, 10) ?? "");
  const lastDate = useTimeAxis
    ? new Date(maxTs).toISOString().slice(0, 10)
    : (dates[dates.length - 1]?.slice(0, 10) ?? "");

  return (
    <div>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="chart-svg"
        role="img"
        aria-label="Multi-strategy cumulative performance chart"
      >
        {[0, 0.25, 0.5, 0.75, 1].map((frac, idx) => {
          const y = pt + (height - pt - pb) * frac;
          const v = maxVal - span * frac;
          return (
            <g key={idx}>
              <line x1={pl} y1={y} x2={width - pr} y2={y} stroke="#1e293b" strokeWidth="1" />
              <text x={4} y={y + 3} fill="#94a3b8" fontSize="10" fontFamily="monospace">
                {v >= 0 ? `+${v.toFixed(1)}%` : `${v.toFixed(1)}%`}
              </text>
            </g>
          );
        })}
        {zeroY !== null && (
          <line x1={pl} y1={zeroY} x2={width - pr} y2={zeroY} stroke="#475569" strokeDasharray="4 4" strokeWidth="1" />
        )}
        {valid.map((s, sIdx) => {
          const tsArr = useTimeAxis ? (parsedSeriesDates[sIdx] as number[]) : null;
          const offset = maxLen - s.values.length;
          const pts = s.values
            .map((v, i) => {
              const xCoord = tsArr ? sxTime(tsArr[i]) : sx(i + offset, maxLen);
              return `${xCoord.toFixed(1)},${sy(v).toFixed(1)}`;
            })
            .join(" ");
          return (
            <polyline
              key={s.id}
              points={pts}
              fill="none"
              stroke={s.color}
              strokeWidth="2.2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          );
        })}
        <text x={pl} y={height - 6} fill="#94a3b8" fontSize="10" fontFamily="monospace">
          {firstDate || `t=0`}
        </text>
        <text x={width - pr - 72} y={height - 6} fill="#94a3b8" fontSize="10" fontFamily="monospace">
          {lastDate || `t=${maxLen - 1}`}
        </text>
      </svg>
    </div>
  );
}

export function OlsScatterChart({
  points,
  beta,
  alphaAnnPct,
  factorName,
  strategyName,
}: {
  points: OlsScatterPoint[];
  beta: number | null;
  alphaAnnPct: number | null;
  factorName: string;
  strategyName: string;
}) {
  const { t } = useLang();
  if (points.length < 3 || beta === null) {
    return (
      <div className="muted">
        {t(
          "Need at least 3 overlapping daily returns for OLS regression plot.",
          "Для графика регрессии OLS нужно минимум 3 общих дневных доходности.",
        )}
      </div>
    );
  }

  const width = 420;
  const height = 210;
  const pl = 46;
  const pr = 16;
  const pt = 14;
  const pb = 28;

  const xs = points.map((p) => p.factor_ret_pct);
  const ys = points.map((p) => p.strat_ret_pct);
  const maxAbsX = Math.max(Math.max(...xs.map(Math.abs)), 0.5);
  const maxAbsY = Math.max(Math.max(...ys.map(Math.abs)), 0.25);

  const sx = (x: number) => pl + ((width - pl - pr) * (x + maxAbsX)) / (2 * maxAbsX);
  const sy = (y: number) => height - pb - ((height - pt - pb) * (y + maxAbsY)) / (2 * maxAbsY);

  const alphaDailyPct = (alphaAnnPct ?? 0) / 365;
  const yLeft = alphaDailyPct + beta * -maxAbsX;
  const yRight = alphaDailyPct + beta * maxAbsX;

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="chart-svg"
      role="img"
      aria-label={`OLS regression scatter ${strategyName} vs ${factorName}`}
    >
      <line x1={pl} y1={sy(0)} x2={width - pr} y2={sy(0)} stroke="#334155" strokeWidth="1" />
      <line x1={sx(0)} y1={pt} x2={sx(0)} y2={height - pb} stroke="#334155" strokeWidth="1" />
      <line
        x1={sx(-maxAbsX)}
        y1={sy(Math.max(-maxAbsY, Math.min(maxAbsY, yLeft)))}
        x2={sx(maxAbsX)}
        y2={sy(Math.max(-maxAbsY, Math.min(maxAbsY, yRight)))}
        stroke="#f59e0b"
        strokeWidth="2"
      />
      {points.map((p, idx) => (
        <circle key={idx} cx={sx(p.factor_ret_pct)} cy={sy(p.strat_ret_pct)} r="3.2" fill="#38bdf8" fillOpacity="0.8" />
      ))}
      <text x={pl} y={height - 6} fill="#94a3b8" fontSize="10" fontFamily="monospace">
        {factorName} -{maxAbsX.toFixed(1)}%
      </text>
      <text x={width - pr - 64} y={height - 6} fill="#94a3b8" fontSize="10" fontFamily="monospace">
        +{maxAbsX.toFixed(1)}%
      </text>
      <text x={4} y={pt + 8} fill="#94a3b8" fontSize="10" fontFamily="monospace">
        +{maxAbsY.toFixed(2)}%
      </text>
      <text x={4} y={height - pb} fill="#94a3b8" fontSize="10" fontFamily="monospace">
        -{maxAbsY.toFixed(2)}%
      </text>
    </svg>
  );
}

export function EquityCurveChart({
  values,
  stroke = "#10b981",
  height = 130,
}: {
  values: number[];
  stroke?: string;
  height?: number;
}) {
  const { t, p } = useLang();
  if (values.length < 2) {
    return <div className="muted">{t("Waiting for at least 2 equity ticks...", "Ждём минимум 2 тика капитала...")}</div>;
  }
  const width = 540;
  const padX = 12;
  const padY = 14;
  const minVal = Math.min(...values);
  const maxVal = Math.max(...values);
  const span = Math.max(maxVal - minVal, 1e-6);

  const coords = values.map((v, i) => {
    const x = padX + ((width - 2 * padX) * i) / (values.length - 1);
    const y = height - padY - ((height - 2 * padY) * (v - minVal)) / span;
    return [x, y] as const;
  });

  const polyline = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const polygon = `${padX},${height - padY} ${polyline} ${width - padX},${height - padY}`;
  const first = values[0];
  const last = values[values.length - 1];
  const pct = first > 0 ? ((last - first) / first) * 100 : 0;

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} className="chart-svg" role="img" aria-label="Equity curve">
        <polygon points={polygon} fill={stroke} fillOpacity="0.14" />
        <polyline points={polyline} fill="none" stroke={stroke} strokeWidth="2.4" strokeLinecap="round" />
        {coords.map(([x, y], i) => (
          <circle key={i} cx={x} cy={y} r="3" fill={stroke} />
        ))}
      </svg>
      <div className="section-header" style={{ marginTop: 6, marginBottom: 0 }}>
        <span className="section-sub mono">
          {t("Min", "Мин")} ${minVal.toLocaleString("en-US", { maximumFractionDigits: 2 })} · {t("Max", "Макс")} $
          {maxVal.toLocaleString("en-US", { maximumFractionDigits: 2 })}
        </span>
        <span className={`mono ${pct >= 0 ? "pos" : "neg"}`}>
          {pct >= 0 ? "+" : ""}
          {pct.toFixed(3)}% ({values.length} {p(values.length, "tick", "ticks", "тик", "тика", "тиков")})
        </span>
      </div>
    </div>
  );
}

export function KappaCalibrationChart({ cal }: { cal: CoinCalibration }) {
  const { t } = useLang();
  const curve = cal.depth_curve ?? [];
  if (curve.length === 0) {
    return <div className="muted">{t("No calibration points yet.", "Точек калибровки пока нет.")}</div>;
  }
  const width = 420;
  const height = 165;
  const pl = 42;
  const pr = 14;
  const pt = 14;
  const pb = 26;
  const maxX = Math.max(...curve.map((c) => c.delta_bps), 1);
  const maxY = Math.max(...curve.flatMap((c) => [c.lambda_per_sec, c.fitted_lambda]), 1e-4);

  const sx = (x: number) => pl + ((width - pl - pr) * x) / maxX;
  const sy = (y: number) => height - pb - ((height - pt - pb) * y) / maxY;
  const fitPts = curve.map((c) => `${sx(c.delta_bps).toFixed(1)},${sy(c.fitted_lambda).toFixed(1)}`).join(" ");

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="chart-svg" role="img" aria-label={`${cal.coin} kappa curve`}>
      <line x1={pl} y1={height - pb} x2={width - pr} y2={height - pb} stroke="#334155" strokeWidth="1" />
      <line x1={pl} y1={pt} x2={pl} y2={height - pb} stroke="#334155" strokeWidth="1" />
      <polyline points={fitPts} fill="none" stroke="#38bdf8" strokeWidth="2.2" />
      {curve.map((c, idx) => (
        <circle key={idx} cx={sx(c.delta_bps)} cy={sy(c.lambda_per_sec)} r="3.6" fill="#10b981" />
      ))}
      <text x={pl} y={height - 6} fill="#94a3b8" fontSize="10" fontFamily="monospace">
        0 bps
      </text>
      <text x={width - pr - 36} y={height - 6} fill="#94a3b8" fontSize="10" fontFamily="monospace">
        {maxX.toFixed(1)} bps
      </text>
      <text x={4} y={pt + 8} fill="#94a3b8" fontSize="10" fontFamily="monospace">
        {maxY.toFixed(3)}
      </text>
    </svg>
  );
}
