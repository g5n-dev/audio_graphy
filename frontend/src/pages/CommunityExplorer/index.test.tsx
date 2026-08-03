import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CommunityExplorerPage, {
  TOPIC_CLUSTER_MEMBER_RENDER_LIMIT,
  TOPIC_CLUSTER_PAGE_SIZE,
} from ".";

const apiMocks = vi.hoisted(() => ({
  getTopicClusters: vi.fn(),
}));

vi.mock("@/api/advancedGraph", () => apiMocks);

const SNAPSHOT = {
  job: {
    id: 901,
    status: "succeeded",
    job_type: "full",
    modularity: 0.72,
    finished_at: "2026-07-24T09:00:00Z",
  },
  available_jobs: [
    {
      id: 901,
      status: "succeeded",
      job_type: "full",
      modularity: 0.72,
      finished_at: "2026-07-24T09:00:00Z",
    },
  ],
  level: 0,
  clusters: [
    {
      community_id: 4,
      level: 0,
      title: "成交阻力",
      summary: "预算、审批与比价形成的主题社区",
      member_count: 3,
      member_node_ids: ["价格超预算", "预算审批", "需要再比较"],
    },
  ],
  total_clusters: 1,
  total_members: 3,
  generated_at: "2026-07-24T09:00:00Z",
};

function renderPage({ initialEntry = "/graph?view=clusters" } = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <CommunityExplorerPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CommunityExplorer topic cluster panel", () => {
  beforeEach(() => {
    apiMocks.getTopicClusters.mockReset();
    apiMocks.getTopicClusters.mockResolvedValue(SNAPSHOT);
  });

  it("renders a light job-bound cluster graph and switches hierarchy level", async () => {
    renderPage();

    expect((await screen.findAllByText("成交阻力")).length).toBeGreaterThan(0);
    expect(screen.getByText("Leiden #901")).toBeInTheDocument();
    const canvas = screen.getByTestId("topic-cluster-canvas");
    expect(canvas).toHaveTextContent("价格超预算");
    expect(canvas).toHaveClass(
      "ag-topic-cluster-canvas",
    );
    expect(apiMocks.getTopicClusters).toHaveBeenCalledWith({
      job_id: undefined,
      level: 0,
      query: undefined,
    });

    fireEvent.click(screen.getByRole("button", { name: "层级 1" }));

    await waitFor(() =>
      expect(apiMocks.getTopicClusters).toHaveBeenLastCalledWith({
        job_id: 901,
        level: 1,
        query: undefined,
      }),
    );
  });

  it("delegates search to the job-bound server snapshot", async () => {
    renderPage();

    await screen.findByText("Leiden #901");
    fireEvent.change(screen.getByRole("textbox", { name: "搜索主题或成员" }), {
      target: { value: "预算审批" },
    });

    await waitFor(
      () =>
        expect(apiMocks.getTopicClusters).toHaveBeenLastCalledWith({
          job_id: 901,
          level: 0,
          query: "预算审批",
        }),
      { timeout: 1_000 },
    );
  });

  it("offers a recoverable error state without unmounting the toolbar", async () => {
    apiMocks.getTopicClusters.mockRejectedValueOnce(new Error("offline"));
    renderPage();

    expect(await screen.findByText("数据加载失败")).toBeInTheDocument();
    expect(screen.getByText("offline")).toBeInTheDocument();
    // The level selector must survive a failed level: it is the only control
    // that can move the user off it, and the level lives in component state.
    expect(
      screen.getByRole("button", { name: "层级 0" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("textbox", { name: "搜索主题或成员" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));

    expect((await screen.findAllByText("成交阻力")).length).toBeGreaterThan(0);
  });

  it("recovers from a failed level by switching level from the toolbar", async () => {
    apiMocks.getTopicClusters.mockRejectedValueOnce(new Error("offline"));
    renderPage();

    await screen.findByText("数据加载失败");
    fireEvent.click(screen.getByRole("button", { name: "层级 1" }));

    expect((await screen.findAllByText("成交阻力")).length).toBeGreaterThan(0);
    expect(apiMocks.getTopicClusters).toHaveBeenCalledWith({
      job_id: undefined,
      level: 1,
      query: undefined,
    });
  });

  it("distinguishes a job-bound summary that is still being generated", async () => {
    apiMocks.getTopicClusters.mockRejectedValue({
      response: {
        data: {
          error: {
            code: "SUMMARY_NOT_READY",
          },
        },
      },
    });
    renderPage();

    expect(
      await screen.findByText("该层级摘要尚未就绪"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/不会使用当前图谱重建旧任务结果/),
    ).toBeInTheDocument();
  });

  it("bounds the detailed member DOM for very large communities", async () => {
    const memberNodeIds = Array.from(
      { length: TOPIC_CLUSTER_MEMBER_RENDER_LIMIT + 20 },
      (_, index) => `成员-${index + 1}`,
    );
    apiMocks.getTopicClusters.mockResolvedValueOnce({
      ...SNAPSHOT,
      clusters: [
        {
          ...SNAPSHOT.clusters[0],
          member_count: memberNodeIds.length,
          member_node_ids: memberNodeIds,
        },
      ],
      total_members: memberNodeIds.length,
    });
    renderPage();

    await screen.findByText("Leiden #901");
    expect(
      screen.getByText("为保持交互流畅，仅展示前 48 个 · 另有 20 个成员"),
    ).toBeInTheDocument();
    expect(screen.queryByText("成员-49")).not.toBeInTheDocument();
  });

  // GD-005: the jump target resolved back to this very panel, so the control
  // was removed rather than kept as a link that appears to do nothing.
  it("does not offer a jump that resolves back to this panel", async () => {
    renderPage();

    await screen.findByText("Leiden #901");
    expect(screen.queryByText("在图谱中查看此社区 →")).not.toBeInTheDocument();
  });

  it("pages through every cluster the KPI counts", async () => {
    const clusters = Array.from(
      { length: TOPIC_CLUSTER_PAGE_SIZE + 2 },
      (_, index) => ({
        community_id: index + 1,
        level: 0,
        title: `社区-${index + 1}`,
        summary: `摘要-${index + 1}`,
        member_count: 1,
        member_node_ids: [`成员-${index + 1}`],
      }),
    );
    apiMocks.getTopicClusters.mockResolvedValue({
      ...SNAPSHOT,
      clusters,
      total_clusters: clusters.length,
      total_members: clusters.length,
    });
    renderPage();

    const canvas = await screen.findByTestId("topic-cluster-canvas");
    expect(canvas).toHaveTextContent("社区-1");
    expect(canvas).not.toHaveTextContent("社区-10");
    expect(
      screen.getByText(/已展示第 1–8 个社区 · 共 10 个/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByText("2"));

    await waitFor(() =>
      expect(screen.getByTestId("topic-cluster-canvas")).toHaveTextContent(
        "社区-10",
      ),
    );
    expect(screen.getByTestId("topic-cluster-canvas")).not.toHaveTextContent(
      "社区-8",
    );
    expect(
      screen.getByText(/已展示第 9–10 个社区 · 共 10 个/),
    ).toBeInTheDocument();
  });

  it("pages to a community arriving through focus_community", async () => {
    const clusters = Array.from(
      { length: TOPIC_CLUSTER_PAGE_SIZE + 2 },
      (_, index) => ({
        community_id: index + 1,
        level: 0,
        title: `社区-${index + 1}`,
        summary: `摘要-${index + 1}`,
        member_count: 1,
        member_node_ids: [`成员-${index + 1}`],
      }),
    );
    apiMocks.getTopicClusters.mockResolvedValue({
      ...SNAPSHOT,
      clusters,
      total_clusters: clusters.length,
      total_members: clusters.length,
    });
    renderPage({ initialEntry: "/graph?view=clusters&focus_community=10" });

    await waitFor(() =>
      expect(screen.getByTestId("topic-cluster-canvas")).toHaveTextContent(
        "社区-10",
      ),
    );
  });
});
