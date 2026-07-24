import { StrictMode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExploreResponse } from "@/types/api";
import GraphExplorerPage from "./GraphExplorerPage";

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

const EMPTY_GRAPH: ExploreResponse = {
  nodes: [],
  edges: [],
  total_nodes: 0,
  total_edges: 0,
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
    })),
    edges: [],
    total_nodes: nodeCount,
    total_edges: 0,
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
}: {
  strict?: boolean;
  initialGraphData?: ExploreResponse;
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
    <QueryClientProvider client={queryClient}>
      <GraphExplorerPage />
    </QueryClientProvider>
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
    });
    expect(graphMocks.Graph).toHaveBeenCalledTimes(1);
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
});
