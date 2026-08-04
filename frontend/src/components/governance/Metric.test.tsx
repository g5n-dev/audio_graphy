import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Metric } from "./Metric";

function track(container: HTMLElement): HTMLElement {
  const bar = container.querySelector(".ag-quality-metric__track > span");
  if (!(bar instanceof HTMLElement)) throw new Error("progress bar not rendered");
  return bar;
}

describe("Metric", () => {
  it("shows the value as a percentage next to its label", () => {
    render(<Metric label="Macro F1" value={0.842} />);

    expect(screen.getByText("Macro F1")).toBeInTheDocument();
    expect(screen.getByText("84.2%")).toBeInTheDocument();
  });

  it("colours a high value as healthy and a low one as a warning", () => {
    const { container: high } = render(<Metric label="覆盖率" value={0.95} />);
    expect(track(high)).toHaveStyle({ background: "#00a870" });

    const { container: low } = render(<Metric label="覆盖率" value={0.4} />);
    expect(track(low)).toHaveStyle({ background: "#e5a100" });
  });

  it("inverts the colour scale for metrics where lower is better", () => {
    // 5% 错误率是健康的，尽管数值本身很低。
    const { container } = render(<Metric label="错误率" value={0.05} inverse />);

    expect(track(container)).toHaveStyle({ background: "#00a870" });
  });

  it("distinguishes a missing value from a measured zero", () => {
    const { container } = render(<Metric label="Macro F1" value={undefined} />);

    expect(screen.getByText("—")).toBeInTheDocument();
    expect(track(container)).toHaveStyle({ width: "0%" });
  });

  it("clamps out-of-range values so the bar cannot overflow", () => {
    const { container: over } = render(<Metric label="越界" value={1.4} />);
    expect(track(over)).toHaveStyle({ width: "100%" });

    const { container: under } = render(<Metric label="越界" value={-0.3} />);
    expect(track(under)).toHaveStyle({ width: "0%" });
  });

  it("hides the decorative track from assistive technology", () => {
    const { container } = render(<Metric label="Macro F1" value={0.8} />);

    expect(container.querySelector(".ag-quality-metric__track")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });
});
