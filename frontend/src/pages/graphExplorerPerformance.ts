import { useEffect, useState } from "react";

export const GRAPH_FILTER_DEBOUNCE_MS = 300;
export const FORCE_LAYOUT_NODE_LIMIT = 350;
export const MAX_FORCE_LAYOUT_ITERATIONS = 120;
export const MAX_GRID_COLUMNS = 48;

interface BoundedForceLayout extends Record<string, unknown> {
  type: "d3-force";
  animation: false;
  iterations: number;
  linkDistance: number;
  nodeStrength: number;
  edgeStrength: number;
  preventOverlap: true;
  nodeSize: number;
}

interface BoundedGridLayout extends Record<string, unknown> {
  type: "grid";
  animation: false;
  cols: number;
  preventOverlap: true;
  nodeSize: number;
  sortBy: "degree";
}

export type BoundedGraphLayout = BoundedForceLayout | BoundedGridLayout;

function normalizedNodeCount(nodeCount: number): number {
  if (!Number.isFinite(nodeCount) || nodeCount < 0) return 0;
  return Math.floor(nodeCount);
}

/**
 * Keeps interactive force work bounded and moves dense result sets onto an
 * O(n) grid. This preserves all API-returned nodes while avoiding an
 * unbounded 2,000-node force simulation on the main thread.
 */
export function createBoundedGraphLayout(
  nodeCount: number,
): BoundedGraphLayout {
  const count = normalizedNodeCount(nodeCount);

  if (count > FORCE_LAYOUT_NODE_LIMIT) {
    return {
      type: "grid",
      animation: false,
      cols: Math.min(
        MAX_GRID_COLUMNS,
        Math.max(1, Math.ceil(Math.sqrt(count))),
      ),
      preventOverlap: true,
      nodeSize: 24,
      sortBy: "degree",
    };
  }

  return {
    // G6's `force` layout uses an all-pairs repulsion pass. `d3-force`
    // preserves the force-directed interaction while using Barnes-Hut
    // approximation for the many-body force, keeping the default 200-node
    // result responsive on the main thread.
    type: "d3-force",
    animation: false,
    iterations: MAX_FORCE_LAYOUT_ITERATIONS,
    linkDistance: 80,
    nodeStrength: -50,
    edgeStrength: 0.1,
    preventOverlap: true,
    nodeSize: 30,
  };
}

export function useDebouncedValue<T>(
  value: T,
  delayMs = GRAPH_FILTER_DEBOUNCE_MS,
): T {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedValue(value);
    }, delayMs);

    return () => window.clearTimeout(timer);
  }, [delayMs, value]);

  return debouncedValue;
}
