import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", () => ({
  httpClient: {
    get: vi.fn(),
  },
}));

import { httpClient } from "./client";
import { exploreGraph, getSubgraph } from "./services";

const mockedGet = httpClient.get as unknown as ReturnType<typeof vi.fn>;

describe("graph services", () => {
  beforeEach(() => {
    mockedGet.mockReset();
    mockedGet.mockResolvedValue({
      data: {
        nodes: [],
        edges: [],
        total_nodes: 0,
        total_edges: 0,
        edge_window: {
          total: 0,
          returned: 0,
          truncated: false,
          render_budget: 5000,
        },
      },
    });
  });

  it("passes a caller-bounded induced-edge budget to graph explore", async () => {
    await exploreGraph({
      node_type: "客户",
      min_degree: 2,
      limit: 200,
      edge_limit: 750,
    });

    expect(mockedGet).toHaveBeenCalledWith("/graph/explore", {
      params: {
        node_type: "客户",
        min_degree: 2,
        limit: 200,
        edge_limit: 750,
      },
    });
  });

  it("passes an edge budget to N-hop subgraph requests", async () => {
    await getSubgraph("客户A", 2, 100, 400);

    expect(mockedGet).toHaveBeenCalledWith("/graph/subgraph", {
      params: {
        entity: "客户A",
        max_hops: 2,
        limit: 100,
        edge_limit: 400,
      },
    });
  });
});
