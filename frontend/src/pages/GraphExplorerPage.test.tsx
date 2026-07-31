import { StrictMode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExploreResponse, EntityDetailResponse } from "@/types/api";
import GraphExplorerPage, {
  MAX_RENDERED_GRAPH_EDGES,
} from "./GraphExplorerPage";

const apiMocks = vi.hoisted(() => ({
  exploreGraph: vi.fn(),
  getEntity: vi.fn(),
}));

const graphMocks = vi.hoisted(() => {
  const renderQueue: Array<() => Promise<void>> = [];
  const instances: Array<{
    on: ReturnType<typeof vi.fn>;
    off: ReturnType<typeof vi.fn>;
    setData: ReturnType<typeof vi.fn>;
    setLayout: ReturnType<typeof vi.fn>;
    render: ReturnType<typeof vi.fn>;
    setSize: ReturnType<typeof vi.fn>;
    resize: ReturnType<typeof vi.fn>;
    stopLayout: ReturnType<typeof vi.fn>;
    destroy: ReturnType<typeof vi.fn>;
  }> = [];

  const Graph = vi.fn(function MockGraph() {
    const instance = {
      on: vi.fn(),
      off: vi.fn(),
      setData: vi.fn(),
      setLayout: vi.fn(),
      render: vi.fn(
        () => renderQueue.shift()?.() ?? Promise.resolve(),
      ),
      setSize: vi.fn(),
      resize: vi.fn(),
      stopLayout: vi.fn(),
      destroy: vi.fn(),
    };
    instances.push(instance);
    return instance;
  });

  return { Graph, instances, renderQueue };
});

vi.mock("@/api/services", () => apiMocks);
vi.mock("@antv/g6", () => ({ Graph: graphMocks.Graph }));
vi.mock("./CommunityExplorer", () => ({
  default: () => (
    <section data-testid="topic-cluster-panel">topic-cluster-panel</section>
  ),
}));

function GraphHistoryHarness() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate("/graph")}>
        URL 切到实体
      </button>
      <button
        type="button"
        onClick={() => navigate("/graph?view=clusters")}
      >
        URL 切到主题
      </button>
      <GraphExplorerPage />
    </>
  );
}

const EMPTY_GRAPH: ExploreResponse = {
  nodes: [],
  edges: [],
  total_nodes: 0,
  total_edges: 0,
  edge_window: {
    total: 0,
    returned: 0,
    truncated: false,
    render_budget: MAX_RENDERED_GRAPH_EDGES,
  },
};

function largeGraph(nodeCount: number): ExploreResponse {
  return {
    nodes: Array.from({ length: nodeCount }, (_, index) => ({
      id: `node-${index}`,
      label: `节点 ${index}`,
      type: "客户",
      description: "",
      degree: index % 8,
      source_ids: [],
      recording_ids: [],
      recorded_at_range: null,
    })),
    edges: [],
    total_nodes: nodeCount,
    total_edges: 0,
    edge_window: {
      total: 0,
      returned: 0,
      truncated: false,
      render_budget: MAX_RENDERED_GRAPH_EDGES,
    },
  };
}

interface ResizeObserverRecord {
  callback: ResizeObserverCallback;
  observe: ReturnType<typeof vi.fn>;
  unobserve: ReturnType<typeof vi.fn>;
  disconnect: ReturnType<typeof vi.fn>;
}

const resizeObservers: ResizeObserverRecord[] = [];

class ResizeObserverMock {
  readonly observe = vi.fn();
  readonly unobserve = vi.fn();
  readonly disconnect = vi.fn();

  constructor(readonly callback: ResizeObserverCallback) {
    resizeObservers.push(this);
  }
}

function renderPage({
  strict = false,
  initialGraphData,
  initialEntry = "/graph",
}: {
  strict?: boolean;
  initialGraphData?: ExploreResponse;
  initialEntry?: string;
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
      },
    },
  });
  if (initialGraphData) {
    queryClient.setQueryData(
      ["graph", "explore", "", 0, 200],
      initialGraphData,
    );
  }
  const page = (
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={queryClient}>
        <GraphExplorerPage />
      </QueryClientProvider>
    </MemoryRouter>
  );
  return render(
    strict ? <StrictMode>{page}</StrictMode> : page,
  );
}

describe("GraphExplorerPage performance lifecycle", () => {
  beforeEach(() => {
    apiMocks.exploreGraph.mockReset();
    apiMocks.exploreGraph.mockResolvedValue(EMPTY_GRAPH);
    apiMocks.getEntity.mockReset();
    graphMocks.Graph.mockClear();
    graphMocks.instances.length = 0;
    graphMocks.renderQueue.length = 0;
    resizeObservers.length = 0;
    vi.stubGlobal(
      "ResizeObserver",
      ResizeObserverMock as unknown as typeof ResizeObserver,
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("debounces rapid filter input while retaining a single G6 instance", async () => {
    renderPage();

    await waitFor(() => expect(apiMocks.exploreGraph).toHaveBeenCalledTimes(1));
    expect(graphMocks.Graph).toHaveBeenCalledTimes(1);

    const input = screen.getByPlaceholderText("节点类型筛选");
    fireEvent.change(input, { target: { value: "客" } });
    fireEvent.change(input, { target: { value: "客户" } });

    await new Promise((resolve) => window.setTimeout(resolve, 80));
    expect(apiMocks.exploreGraph).toHaveBeenCalledTimes(1);

    await waitFor(
      () => expect(apiMocks.exploreGraph).toHaveBeenCalledTimes(2),
      { timeout: 700 },
    );
    expect(apiMocks.exploreGraph).toHaveBeenLastCalledWith({
      node_type: "客户",
      min_degree: 0,
      limit: 200,
      edge_limit: MAX_RENDERED_GRAPH_EDGES,
    });
    expect(graphMocks.Graph).toHaveBeenCalledTimes(1);
  });

  it("mounts exactly one graph tab panel and supports keyboard switching", async () => {
    renderPage();

    const entityTab = screen.getByRole("tab", { name: "实体关系" });
    const topicTab = screen.getByRole("tab", { name: "主题聚类" });

    expect(entityTab).toHaveAttribute("aria-selected", "true");
    expect(topicTab).toHaveAttribute("aria-selected", "false");
    expect(screen.getByTestId("graph-explorer-canvas")).toBeInTheDocument();
    expect(screen.queryByTestId("topic-cluster-panel")).not.toBeInTheDocument();

    fireEvent.keyDown(entityTab, { key: "ArrowRight" });

    expect(topicTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("topic-cluster-panel")).toBeInTheDocument();
    expect(
      screen.queryByTestId("graph-explorer-canvas"),
    ).not.toBeInTheDocument();
    await waitFor(() =>
      expect(graphMocks.instances[0]?.destroy).toHaveBeenCalledTimes(1),
    );

    fireEvent.keyDown(topicTab, { key: "Home" });
    expect(entityTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("graph-explorer-canvas")).toBeInTheDocument();
    expect(screen.queryByTestId("topic-cluster-panel")).not.toBeInTheDocument();
  });

  it("opens the cluster panel from the compatibility query view", () => {
    renderPage({ initialEntry: "/graph?view=clusters" });

    expect(
      screen.getByRole("tab", { name: "主题聚类" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("topic-cluster-panel")).toBeInTheDocument();
    expect(
      screen.queryByTestId("graph-explorer-canvas"),
    ).not.toBeInTheDocument();
  });

  it("keeps the active panel synchronized with URL history changes", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    render(
      <MemoryRouter initialEntries={["/graph"]}>
        <QueryClientProvider client={queryClient}>
          <GraphHistoryHarness />
        </QueryClientProvider>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "URL 切到主题" }));
    await waitFor(() =>
      expect(
        screen.getByRole("tab", { name: "主题聚类" }),
      ).toHaveAttribute("aria-selected", "true"),
    );

    fireEvent.click(screen.getByRole("button", { name: "URL 切到实体" }));
    await waitFor(() =>
      expect(
        screen.getByRole("tab", { name: "实体关系" }),
      ).toHaveAttribute("aria-selected", "true"),
    );
  });

  it("hides the cluster tab and falls back to entities when disabled", () => {
    vi.stubEnv("VITE_TOPIC_CLUSTERS_ENABLED", "false");
    renderPage({ initialEntry: "/graph?view=clusters" });

    expect(
      screen.queryByRole("tab", { name: "主题聚类" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "实体关系" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("graph-explorer-canvas")).toBeInTheDocument();
  });

  it("never stops an uninitialized layout during StrictMode cleanup", async () => {
    apiMocks.exploreGraph.mockImplementation(() => new Promise(() => undefined));

    const view = renderPage({ strict: true });

    await waitFor(() => expect(graphMocks.instances).toHaveLength(2));
    const [discardedGraph, activeGraph] = graphMocks.instances;

    expect(discardedGraph.stopLayout).not.toHaveBeenCalled();
    expect(discardedGraph.destroy).toHaveBeenCalledTimes(1);
    expect(activeGraph.stopLayout).not.toHaveBeenCalled();

    view.unmount();

    expect(activeGraph.stopLayout).not.toHaveBeenCalled();
    expect(activeGraph.destroy).toHaveBeenCalledTimes(1);
  });

  it("does not start rendering the discarded StrictMode probe instance when data is cached", async () => {
    renderPage({
      strict: true,
      initialGraphData: largeGraph(2),
    });

    await waitFor(() => expect(graphMocks.instances).toHaveLength(2));
    const [discardedGraph, activeGraph] = graphMocks.instances;

    await waitFor(() => expect(activeGraph.render).toHaveBeenCalledTimes(1));
    expect(discardedGraph.render).not.toHaveBeenCalled();
    expect(discardedGraph.destroy).toHaveBeenCalledTimes(1);
  });

  it("persists an observed size before first render and releases graph resources", async () => {
    const view = renderPage();

    await waitFor(() => expect(graphMocks.instances).toHaveLength(1));
    const graph = graphMocks.instances[0];
    const canvas = screen.getByTestId("graph-explorer-canvas");
    const observer = resizeObservers.find((candidate) =>
      candidate.observe.mock.calls.some(([target]) => target === canvas),
    );
    expect(observer).toBeDefined();

    act(() => {
      observer?.callback(
        [
          {
            target: canvas,
            contentRect: {
              width: 860,
              height: 600,
            },
          } as unknown as ResizeObserverEntry,
        ],
        observer as unknown as ResizeObserver,
      );
    });
    expect(graph.setSize).toHaveBeenLastCalledWith(860, 600);

    const clickHandler = graph.on.mock.calls.find(
      ([eventName]) => eventName === "node:click",
    )?.[1];
    view.unmount();

    expect(graph.off).toHaveBeenCalledWith("node:click", clickHandler);
    expect(observer?.disconnect).toHaveBeenCalledTimes(1);
    expect(graph.stopLayout).not.toHaveBeenCalled();
    expect(graph.destroy).toHaveBeenCalledTimes(1);
  });

  it("stops the previous layout only after a successful render", async () => {
    apiMocks.exploreGraph
      .mockResolvedValueOnce(largeGraph(2))
      .mockResolvedValueOnce(largeGraph(3));
    renderPage();

    await waitFor(() => {
      expect(graphMocks.instances[0]?.render).toHaveBeenCalledTimes(1);
    });
    const graph = graphMocks.instances[0];
    expect(graph.stopLayout).not.toHaveBeenCalled();

    fireEvent.change(screen.getByPlaceholderText("节点类型筛选"), {
      target: { value: "客户" },
    });

    await waitFor(
      () => expect(apiMocks.exploreGraph).toHaveBeenCalledTimes(2),
      { timeout: 700 },
    );
    await waitFor(() => expect(graph.render).toHaveBeenCalledTimes(2));
    expect(graph.stopLayout).toHaveBeenCalledTimes(1);
  });

  it("updates data on the existing graph and selects the bounded large-graph layout", async () => {
    apiMocks.exploreGraph.mockResolvedValueOnce(largeGraph(2_000));
    renderPage();

    await waitFor(() => {
      expect(graphMocks.instances[0]?.setData).toHaveBeenCalledTimes(1);
    });

    const graph = graphMocks.instances[0];
    expect(graph.setLayout).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "grid",
        animation: false,
      }),
    );
    expect(graphMocks.Graph).toHaveBeenCalledTimes(1);
  });

  it("caps dense relationship DOM work before handing data to G6", async () => {
    const denseGraph = largeGraph(2);
    denseGraph.edges = Array.from(
      { length: MAX_RENDERED_GRAPH_EDGES + 1 },
      (_, index) => ({
        source: "node-0",
        target: "node-1",
        relation: `关系-${index}`,
        weight: 1,
        confidence: "EXTRACTED",
        confidence_score: 0.99,
        source_ids: [],
        recording_ids: [],
        recorded_at_range: null,
      }),
    );
    denseGraph.total_edges = denseGraph.edges.length;
    denseGraph.edge_window = {
      total: denseGraph.edges.length,
      returned: denseGraph.edges.length,
      truncated: false,
      render_budget: MAX_RENDERED_GRAPH_EDGES,
    };
    apiMocks.exploreGraph.mockResolvedValueOnce(denseGraph);
    renderPage();

    await waitFor(() =>
      expect(graphMocks.instances[0]?.setData).toHaveBeenCalledTimes(1),
    );
    const renderedData = graphMocks.instances[0]?.setData.mock.calls[0]?.[0] as {
      edges: unknown[];
    };
    expect(renderedData.edges).toHaveLength(MAX_RENDERED_GRAPH_EDGES);
    expect(
      screen.getByText(
        /当前画布展示 5,000 \/ 5,001 条筛选关系（服务端性能预算 5,000/,
      ),
    ).toBeInTheDocument();
  });

  it("surfaces server-side induced-edge truncation instead of silently dropping relations", async () => {
    const boundedGraph = largeGraph(2);
    boundedGraph.edges = [
      {
        source: "node-0",
        target: "node-1",
        relation: "关系-0",
        weight: 1,
        confidence: "EXTRACTED",
        confidence_score: 0.99,
        source_ids: [],
      },
    ];
    boundedGraph.total_edges = 9_000;
    boundedGraph.edge_window = {
      total: 7_500,
      returned: 1,
      truncated: true,
      render_budget: 1,
    };
    apiMocks.exploreGraph.mockResolvedValueOnce(boundedGraph);

    renderPage();

    expect(
      await screen.findByText(
        /当前画布展示 1 \/ 7,500 条筛选关系（服务端性能预算 1/,
      ),
    ).toBeInTheDocument();
  });

  it("shows a recoverable query error and reloads graph data", async () => {
    apiMocks.exploreGraph
      .mockRejectedValueOnce(new Error("network unavailable"))
      .mockResolvedValueOnce(EMPTY_GRAPH);
    renderPage();

    expect(
      await screen.findByText("图谱数据加载失败"),
    ).toBeInTheDocument();
    expect(graphMocks.instances[0]?.render).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));

    await waitFor(() =>
      expect(apiMocks.exploreGraph).toHaveBeenCalledTimes(2),
    );
    expect(await screen.findByText("暂无图谱数据")).toBeInTheDocument();
  });

  it("shows a recoverable render error and retries without stopping an unrendered layout", async () => {
    apiMocks.exploreGraph.mockResolvedValueOnce(largeGraph(1));
    graphMocks.renderQueue.push(
      () => Promise.reject(new Error("canvas initialization failed")),
    );
    renderPage();

    expect(await screen.findByText("图谱渲染失败")).toBeInTheDocument();
    const graph = graphMocks.instances[0];
    expect(graph.stopLayout).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "重试渲染" }));

    await waitFor(() => expect(graph.render).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByText("图谱渲染失败")).not.toBeInTheDocument(),
    );
    expect(graph.stopLayout).not.toHaveBeenCalled();
  });

  // ── GD-001 / GD-008: Drilldown closed-loop tests ──

  it("renders a type-bound jump button in the entity detail panel", async () => {
    const entityNode = {
      id: "产品:milestone",
      label: "里程碑",
      type: "产品",
      description: "关键产品节点",
      degree: 3,
      source_ids: [],
      recording_ids: [],
      recorded_at_range: null,
    };
    const graphData: ExploreResponse = {
      nodes: [entityNode],
      edges: [],
      total_nodes: 1,
      total_edges: 0,
      edge_window: {
        total: 0,
        returned: 0,
        truncated: false,
        render_budget: MAX_RENDERED_GRAPH_EDGES,
      },
    };
    const entityDetail: EntityDetailResponse = {
      node: entityNode,
      neighbors: [],
      relation_counts: {},
    };
    apiMocks.exploreGraph.mockResolvedValueOnce(graphData);
    apiMocks.getEntity.mockResolvedValueOnce(entityDetail);
    renderPage();

    await waitFor(() => expect(apiMocks.exploreGraph).toHaveBeenCalled());

    // Simulate node click via captured G6 handler
    const graph = graphMocks.instances[0];
    const clickHandler = graph.on.mock.calls.find(
      (call: unknown[]) => call[0] === "node:click",
    )?.[1] as ((evt: { target: { id: string } }) => void) | undefined;
    expect(clickHandler).toBeDefined();

    act(() => {
      clickHandler?.({ target: { id: "产品:milestone" } });
    });

    await waitFor(() =>
      expect(apiMocks.getEntity).toHaveBeenCalledWith("产品:milestone"),
    );
    expect(await screen.findByText(/聚焦/)).toBeInTheDocument();
  });

  it("does not render a jump button for unmapped node types (未知)", async () => {
    const entityNode = {
      id: "unknown-node",
      label: "未知实体",
      type: "未知",
      description: "",
      degree: 1,
      source_ids: [],
      recording_ids: [],
      recorded_at_range: null,
    };
    const graphData: ExploreResponse = {
      nodes: [entityNode],
      edges: [],
      total_nodes: 1,
      total_edges: 0,
      edge_window: {
        total: 0,
        returned: 0,
        truncated: false,
        render_budget: MAX_RENDERED_GRAPH_EDGES,
      },
    };
    const entityDetail: EntityDetailResponse = {
      node: entityNode,
      neighbors: [],
      relation_counts: {},
    };
    apiMocks.exploreGraph.mockResolvedValueOnce(graphData);
    apiMocks.getEntity.mockResolvedValueOnce(entityDetail);
    renderPage();

    await waitFor(() => expect(apiMocks.exploreGraph).toHaveBeenCalled());
    const graph = graphMocks.instances[0];
    const clickHandler = graph.on.mock.calls.find(
      (call: unknown[]) => call[0] === "node:click",
    )?.[1] as ((evt: { target: { id: string } }) => void) | undefined;

    act(() => {
      clickHandler?.({ target: { id: "unknown-node" } });
    });

    await waitFor(() =>
      expect(apiMocks.getEntity).toHaveBeenCalledWith("unknown-node"),
    );
    await screen.findByText("未知实体");
    // "在图谱中聚焦" should NOT appear for 未知 type
    expect(screen.queryByText(/在图谱中聚焦/)).not.toBeInTheDocument();
  });

  it("consumes the focus URL param and shows an Alert when the node is found", async () => {
    const entityNode = {
      id: "alice",
      label: "Alice",
      type: "客户",
      description: "",
      degree: 2,
      source_ids: [],
      recording_ids: [],
      recorded_at_range: null,
    };
    const graphData: ExploreResponse = {
      nodes: [entityNode],
      edges: [],
      total_nodes: 1,
      total_edges: 0,
      edge_window: {
        total: 0,
        returned: 0,
        truncated: false,
        render_budget: MAX_RENDERED_GRAPH_EDGES,
      },
    };
    apiMocks.exploreGraph.mockResolvedValueOnce(graphData);
    renderPage({ initialEntry: "/graph?focus=%E5%AE%A2%E6%88%B7:alice" });

    await waitFor(() => expect(apiMocks.exploreGraph).toHaveBeenCalled());
    expect(
      await screen.findByText(/已聚焦到 Alice/),
    ).toBeInTheDocument();
  });

  it("shows a warning Alert when the focus param node is not in filtered results", async () => {
    const graphData: ExploreResponse = {
      nodes: [
        {
          id: "other-node",
          label: "Other",
          type: "产品",
          description: "",
          degree: 1,
          source_ids: [],
          recording_ids: [],
          recorded_at_range: null,
        },
      ],
      edges: [],
      total_nodes: 1,
      total_edges: 0,
      edge_window: {
        total: 0,
        returned: 0,
        truncated: false,
        render_budget: MAX_RENDERED_GRAPH_EDGES,
      },
    };
    apiMocks.exploreGraph.mockResolvedValueOnce(graphData);
    renderPage({ initialEntry: "/graph?focus=%E4%BA%A7%E5%93%81:missing" });

    await waitFor(() => expect(apiMocks.exploreGraph).toHaveBeenCalled());
    expect(
      await screen.findByText(/不在当前筛选范围内/),
    ).toBeInTheDocument();
  });
});
