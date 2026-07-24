import { describe, expect, it } from "vitest";
import {
  FORCE_LAYOUT_NODE_LIMIT,
  MAX_FORCE_LAYOUT_ITERATIONS,
  MAX_GRID_COLUMNS,
  createBoundedGraphLayout,
} from "./graphExplorerPerformance";

describe("createBoundedGraphLayout", () => {
  it("keeps force layout work explicitly bounded for an interactive graph", () => {
    const layout = createBoundedGraphLayout(FORCE_LAYOUT_NODE_LIMIT);

    expect(layout.type).toBe("d3-force");
    expect(layout.animation).toBe(false);
    if (layout.type !== "d3-force") {
      throw new Error("expected bounded force layout");
    }
    expect(layout.iterations).toBeLessThanOrEqual(
      MAX_FORCE_LAYOUT_ITERATIONS,
    );
  });

  it("degrades a 2000-node graph to a bounded non-iterative grid", () => {
    const layout = createBoundedGraphLayout(2_000);

    expect(layout.type).toBe("grid");
    expect(layout.animation).toBe(false);
    if (layout.type !== "grid") {
      throw new Error("expected bounded grid layout");
    }
    expect(layout.cols).toBeLessThanOrEqual(MAX_GRID_COLUMNS);
  });

  it("normalizes invalid node counts instead of producing an invalid layout", () => {
    expect(createBoundedGraphLayout(Number.NaN)).toMatchObject({
      type: "d3-force",
      iterations: MAX_FORCE_LAYOUT_ITERATIONS,
    });
    expect(createBoundedGraphLayout(-10)).toMatchObject({
      type: "d3-force",
      iterations: MAX_FORCE_LAYOUT_ITERATIONS,
    });
  });
});
