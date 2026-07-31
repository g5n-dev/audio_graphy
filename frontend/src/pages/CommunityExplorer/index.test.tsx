import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CommunityExplorerPage, {
  TOPIC_CLUSTER_MEMBER_RENDER_LIMIT,
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

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
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

  it("offers a recoverable error state", async () => {
    apiMocks.getTopicClusters.mockRejectedValueOnce(new Error("offline"));
    renderPage();

    expect(
      await screen.findByText("主题聚类加载失败"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));

    expect((await screen.findAllByText("成交阻力")).length).toBeGreaterThan(0);
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

  // GD-005: Community → Graph jump button
  it("renders a jump-to-graph button in the community detail panel", async () => {
    renderPage();

    await screen.findByText("Leiden #901");
    expect(
      screen.getByText("在图谱中查看此社区 →"),
    ).toBeInTheDocument();
  });
});
