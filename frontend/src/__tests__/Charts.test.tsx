import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EquityCurveChart, KappaCalibrationChart } from "../components/Charts";
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
});
