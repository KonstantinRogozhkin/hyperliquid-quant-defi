import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  EquityCurveChart,
  KappaCalibrationChart,
  MultiSeriesChart,
  OlsScatterChart,
  clipOlsLineToBox,
} from "../components/Charts";
import type { CoinCalibration } from "../types";

describe("Charts", () => {
  it("shows waiting placeholder when EquityCurveChart has fewer than 2 ticks", () => {
    render(<EquityCurveChart values={[10000]} />);
    expect(screen.getByText(/Waiting for at least 2 equity ticks/i)).toBeInTheDocument();
  });

  it("renders EquityCurveChart SVG, respects initialValue, and centers flat series vertically", () => {
    const { container, rerender } = render(<EquityCurveChart values={[10050, 10125]} initialValue={10000} />);
    expect(screen.getByRole("img", { name: "Equity curve" })).toBeInTheDocument();
    expect(container.querySelectorAll("circle")).toHaveLength(2);
    // Computed against initialValue=10000 (+1.250%), not values[0]=10050 (+0.746%)
    expect(screen.getByText(/\+1\.250% \(2 ticks\)/)).toBeInTheDocument();
    expect(screen.getByText(/Min \$10,050/)).toBeInTheDocument();
    expect(screen.getByText(/Max \$10,125/)).toBeInTheDocument();

    // Flat series centers circles vertically at height / 2 = 65
    rerender(<EquityCurveChart values={[10000, 10000, 10000]} height={130} />);
    const circles = container.querySelectorAll("circle");
    expect(circles).toHaveLength(3);
    expect(Number(circles[0].getAttribute("cy"))).toBeCloseTo(65, 1);
  });

  it("shows empty placeholder for KappaCalibrationChart with no points", () => {
    const emptyCal: CoinCalibration = {
      coin: "ETH",
      trades_count: 0,
      empirical_sigma_tau_bps: 0,
      kappa_per_bp: 0,
      arrival_a_per_sec: 0,
      r2_fit: 0,
      implied_min_half_spread_bps: 0,
      depth_curve: [],
    };
    render(<KappaCalibrationChart cal={emptyCal} />);
    expect(screen.getByText(/No calibration points yet/i)).toBeInTheDocument();
  });

  it("renders KappaCalibrationChart SVG with fitted polyline and depth circles", () => {
    const cal: CoinCalibration = {
      coin: "BTC",
      trades_count: 420,
      empirical_sigma_tau_bps: 3.1,
      kappa_per_bp: 0.48,
      arrival_a_per_sec: 1.9,
      r2_fit: 0.942,
      implied_min_half_spread_bps: 2.1,
      depth_curve: [
        { delta_bps: 1.0, lambda_per_sec: 1.2, fitted_lambda: 1.15 },
        { delta_bps: 2.5, lambda_per_sec: 0.6, fitted_lambda: 0.58 },
        { delta_bps: 5.0, lambda_per_sec: 0.2, fitted_lambda: 0.22 },
      ],
    };
    const { container } = render(<KappaCalibrationChart cal={cal} />);
    expect(screen.getByRole("img", { name: "BTC kappa curve" })).toBeInTheDocument();
    expect(container.querySelectorAll("circle")).toHaveLength(3);
    expect(screen.getByText("5.0 bps")).toBeInTheDocument();
  });

  it("renders MultiSeriesChart with multiple polylines and avoids -0.0% on flat series", () => {
    const { container, rerender } = render(
      <MultiSeriesChart
        dates={["2026-10-01", "2026-10-02", "2026-10-03"]}
        series={[
          { id: "ap", name: "Autopilot", color: "#10b981", values: [0, 0.5, 1.2] },
          { id: "btc", name: "BTC", color: "#f59e0b", values: [0, -2.0, -4.5] },
        ]}
      />,
    );
    expect(screen.getByRole("img", { name: /Multi-strategy cumulative performance chart/i })).toBeInTheDocument();
    expect(container.querySelectorAll("polyline")).toHaveLength(2);

    rerender(
      <MultiSeriesChart
        dates={["2026-10-01", "2026-10-02"]}
        series={[{ id: "flat", name: "Flat", color: "#10b981", values: [0, 0] }]}
      />,
    );
    expect(container.textContent).not.toContain("-0.0%");
    expect(container.textContent).toContain("+0.0%");
  });

  it("right-aligns shorter series to the end of MultiSeriesChart date window", () => {
    const { container } = render(
      <MultiSeriesChart
        dates={["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]}
        series={[
          { id: "long", name: "Long", color: "#10b981", values: [0, 1, 2, 3] },
          { id: "short", name: "Short", color: "#f59e0b", values: [0, 1] },
        ]}
      />,
    );
    const lines = container.querySelectorAll("polyline");
    expect(lines).toHaveLength(2);
    const longFirstX = Number(lines[0].getAttribute("points")!.split(" ")[0].split(",")[0]);
    const shortFirstX = Number(lines[1].getAttribute("points")!.split(" ")[0].split(",")[0]);
    expect(shortFirstX).toBeGreaterThan(longFirstX);
  });

  it("aligns multi-resolution 4h strategy ticks against 1d benchmarks by timestamp", () => {
    const { container } = render(
      <MultiSeriesChart
        dates={["2026-11-20", "2026-11-27"]}
        series={[
          {
            id: "btc",
            name: "BTC",
            color: "#f59e0b",
            values: [0, -10],
            dates: ["2026-11-20", "2026-11-27T16:00:00Z"],
          },
          {
            id: "sp",
            name: "Spot+Perp",
            color: "#10b981",
            values: [0, 0.2, 0.4],
            dates: ["2026-11-27T08:00:00Z", "2026-11-27T12:00:00Z", "2026-11-27T16:00:00Z"],
          },
        ]}
      />,
    );
    const lines = container.querySelectorAll("polyline");
    expect(lines).toHaveLength(2);
    const btcStartX = Number(lines[0].getAttribute("points")!.split(" ")[0].split(",")[0]);
    const btcEndX = Number(lines[0].getAttribute("points")!.split(" ")[1].split(",")[0]);
    const spStartX = Number(lines[1].getAttribute("points")!.split(" ")[0].split(",")[0]);
    expect(spStartX).toBeGreaterThan(btcStartX + 0.85 * (btcEndX - btcStartX));
  });

  it("renders OlsScatterChart and clips steep regression lines with exact mathematical slope", () => {
    // Pure Liang-Barsky unit check: y = 0.1 + 2.0 * x inside [-2, 2] x [-1, 1]
    const clipped = clipOlsLineToBox(0.1, 2.0, 2.0, 1.0);
    expect(clipped).not.toBeNull();
    expect(clipped!.y1).toBeCloseTo(-1.0, 6);
    expect(clipped!.y2).toBeCloseTo(1.0, 6);
    expect(clipped!.x1).toBeCloseTo(-0.55, 6);
    expect(clipped!.x2).toBeCloseTo(0.45, 6);
    expect((clipped!.y2 - clipped!.y1) / (clipped!.x2 - clipped!.x1)).toBeCloseTo(2.0, 6);

    const { container } = render(
      <OlsScatterChart
        factorName="BTC"
        strategyName="Directional"
        beta={2.0}
        alphaAnnPct={0}
        points={[
          { date: "2026-10-01", factor_ret_pct: -2.0, strat_ret_pct: -0.8 },
          { date: "2026-10-02", factor_ret_pct: 2.0, strat_ret_pct: 1.0 },
          { date: "2026-10-03", factor_ret_pct: 0.0, strat_ret_pct: 0.0 },
        ]}
      />,
    );
    expect(screen.getByRole("img", { name: /OLS regression scatter Directional vs BTC/i })).toBeInTheDocument();
    expect(container.querySelectorAll("circle")).toHaveLength(3);
    const regLine = screen.getByTestId("ols-regression-line");
    // X-range is [-2, 2] mapped to [46, 404]; clipped x in [-0.5, 0.5] must sit inside the inner quarter, not at edges 46/404
    const x1 = Number(regLine.getAttribute("x1"));
    const x2 = Number(regLine.getAttribute("x2"));
    expect(x1).toBeGreaterThan(150);
    expect(x2).toBeLessThan(300);
  });
});
