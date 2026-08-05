import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TimeTravelPage from "./index";

vi.mock("@/api/advancedGraph", () => ({
  timeTravelEdges: vi.fn(),
  edgeHistory: vi.fn(),
}));
vi.mock("@/api/services", () => ({
  listRecordings: vi.fn(),
}));

import { edgeHistory, timeTravelEdges } from "@/api/advancedGraph";
import { listRecordings } from "@/api/services";

const mocks = {
  edges: timeTravelEdges as unknown as ReturnType<typeof vi.fn>,
  history: edgeHistory as unknown as ReturnType<typeof vi.fn>,
  recordings: listRecordings as unknown as ReturnType<typeof vi.fn>,
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TimeTravelPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TimeTravelPage", () => {
  beforeEach(() => {
    Object.values(mocks).forEach((mock) => mock.mockReset());
    mocks.edges.mockResolvedValue({ edges: [] });
    mocks.history.mockResolvedValue([]);
    mocks.recordings.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
    });
  });

  it("explains an empty timestamp instead of implying a failure", async () => {
    renderPage();

    // 双时态图谱里「这一刻没有生效的边」是一个有意义的答案,不是错误——
    // 空态必须说清是时间点的问题,并告诉操作员往哪调。
    expect(
      await screen.findByText("该时间点没有生效中的关系边"),
    ).toBeInTheDocument();
    expect(screen.getByText(/把时间基准调到录音已索引之后/)).toBeInTheDocument();
  });

  it("labels the workbench in Chinese like every other page", async () => {
    mocks.edges.mockResolvedValue({
      edges: [
        {
          source: "客户A",
          relation: "询问",
          target: "长安CS75",
          confidence: "EXTRACTED",
          valid_at: "2026-07-24T09:00:00Z",
          invalid_at: null,
        },
      ],
    });
    renderPage();

    expect(await screen.findByText("起点")).toBeInTheDocument();
    for (const header of ["关系", "终点", "置信度", "生效于", "失效于"]) {
      expect(screen.getByText(header)).toBeInTheDocument();
    }
    // 这一页曾整页英文表头,与其余页面完全脱节。
    expect(screen.queryByText("Source")).not.toBeInTheDocument();
    expect(screen.queryByText("Relation")).not.toBeInTheDocument();
  });
});
