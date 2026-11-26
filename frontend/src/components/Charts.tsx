import type { CoinCalibration } from "../types";

export function EquityCurveChart({
  values,
  stroke = "#10b981",
  height = 130,
}: {
  values: number[];
  stroke?: string;
  height?: number;
}) {
  if (values.length < 2) {
    return <div className="muted">Waiting for at least 2 equity ticks...</div>;
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
          Min ${minVal.toLocaleString("en-US", { maximumFractionDigits: 2 })} · Max $
          {maxVal.toLocaleString("en-US", { maximumFractionDigits: 2 })}
        </span>
        <span className={`mono ${pct >= 0 ? "pos" : "neg"}`}>
          {pct >= 0 ? "+" : ""}
          {pct.toFixed(3)}% ({values.length} ticks)
        </span>
      </div>
    </div>
  );
}

export function KappaCalibrationChart({ cal }: { cal: CoinCalibration }) {
  const curve = cal.depth_curve ?? [];
  if (curve.length === 0) {
    return <div className="muted">No calibration points yet.</div>;
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
