import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EquityCurveChart, KappaCalibrationChart, MultiSeriesChart, OlsScatterChart } from "../components/Charts";
import type { CoinCalibration } from "../types";

describe("Charts", () => {
  it("shows waiting placeholder when EquityCurveChart has fewer than 2 ticks", () => {
    render(<EquityCurveChart values={[10000]} />);
    expect(screen.getByText(/Waiting for at least 2 equity ticks/i)).toBeInTheDocument();
  });

  it("renders EquityCurveChart SVG and summary metrics for multi-tick series", () => {
    const { container } = render(<EquityCurveChart values={[10000, 10050, 10125]} />);
    expect(screen.getByRole("img", { name: "Equity curve" })).toBeInTheDocument();
    expect(container.querySelectorAll("circle")).toHaveLength(3);
    expect(screen.getByText(/\+1\.250% \(3 ticks\)/)).toBeInTheDocument();
    expect(screen.getByText(/Min \$10,000/)).toBeInTheDocument();
    expect(screen.getByText(/Max \$10,125/)).toBeInTheDocument();
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

  it("renders MultiSeriesChart with multiple polylines", () => {
    const { container } = render(
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
    // Spot+Perp started on Nov 27 (last ~10% of the 7-day window), so its X start must be near the right edge
    expect(spStartX).toBeGreaterThan(btcStartX + 0.85 * (btcEndX - btcStartX));
  });

  it("renders OlsScatterChart with scatter points and fitted regression line", () => {
    const { container } = render(
      <OlsScatterChart
        factorName="BTC"
        strategyName="Spot+Perp"
        beta={0.002}
        alphaAnnPct={9.5}
        points={[
          { date: "2026-10-01", factor_ret_pct: -2.1, strat_ret_pct: 0.03 },
          { date: "2026-10-02", factor_ret_pct: 1.5, strat_ret_pct: 0.04 },
          { date: "2026-10-03", factor_ret_pct: -0.8, strat_ret_pct: 0.02 },
        ]}
      />,
    );
    expect(screen.getByRole("img", { name: /OLS regression scatter Spot\+Perp vs BTC/i })).toBeInTheDocument();
    expect(container.querySelectorAll("circle")).toHaveLength(3);
  });
});
