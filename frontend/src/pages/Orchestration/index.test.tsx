import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import OrchestrationPage from "./index";

vi.mock("@/api/services", () => ({
  getOrchestrationTopology: vi.fn(),
  listTagJobs: vi.fn(),
}));

import { getOrchestrationTopology, listTagJobs } from "@/api/services";

const mocks = {
  topology: getOrchestrationTopology as unknown as ReturnType<typeof vi.fn>,
  jobs: listTagJobs as unknown as ReturnType<typeof vi.fn>,
};

const TOPOLOGY = {
  stages: [
    {
      id: "ingest",
      name: "录音接入",
      service: "ingestion",
      adapter_mode: null,
      state: "ok" as const,
      queue: 0,
      note: "写入 Recording。",
      config: [["处理并发", "1", "PIPELINE_CONCURRENCY"]] as [
        string,
        string,
        string,
      ][],
      in_schema: ["audio: bytes"],
      out_schema: ["recording_id: int"],
    },
    {
      id: "extract",
      name: "标签抽取",
      service: "tag_worker",
      adapter_mode: "mock",
      state: "mock" as const,
      queue: 3,
      note: "按 Schema 抽取标签事实。",
      config: [["LLM 模式", "mock", "ADAPTER_LLM_MODE"]] as [
        string,
        string,
        string,
      ][],
      in_schema: ["reception_id: int"],
      out_schema: ["tag_fact_id: int"],
    },
  ],
  links: [["ingest", "extract"]] as [string, string][],
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <OrchestrationPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("OrchestrationPage", () => {
  beforeEach(() => {
    Object.values(mocks).forEach((mock) => mock.mockReset());
    mocks.topology.mockResolvedValue(TOPOLOGY);
    mocks.jobs.mockResolvedValue({ items: [], total: 0 });
  });

  it("renders every stage with its真实 backlog, never invented throughput", async () => {
    renderPage();

    const ingest = await screen.findByRole("button", { name: /录音接入/ });
    expect(within(ingest).getByText("无积压")).toBeInTheDocument();
    const extract = screen.getByRole("button", { name: /标签抽取/ });
    expect(within(extract).getByText("积压 3")).toBeInTheDocument();

    // 原型画的吞吐/P95/成本本系统不采集,渲染出来就是编的。
    expect(screen.queryByText(/条\/日|P95|¥/)).not.toBeInTheDocument();
  });

  it("opens a stage and shows config as read-only env keys", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /标签抽取/ }));
    const detail = screen.getByRole("complementary", {
      name: "标签抽取 阶段详情",
    });

    // 配置是 env 驱动的:展示「值 + 键」,不给一个提交后什么也不发生的表单。
    expect(within(detail).getByText("ADAPTER_LLM_MODE")).toBeInTheDocument();
    expect(
      within(detail).queryByRole("button", { name: /提交|保存/ }),
    ).not.toBeInTheDocument();
    expect(within(detail).getByText("reception_id: int")).toBeInTheDocument();
  });

  it("says out loud when a stage runs on a mock adapter", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /标签抽取/ }));
    expect(screen.getByText(/mock adapter/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /录音接入/ }));
    // 没有 adapter 开关的阶段不该被贴上 mock 警告。
    expect(screen.queryByText(/mock adapter/)).not.toBeInTheDocument();
  });

  it("keeps the canvas usable when the job ledger fails", async () => {
    mocks.jobs.mockRejectedValue(new Error("boom"));
    renderPage();

    expect(
      await screen.findByRole("button", { name: /录音接入/ }),
    ).toBeInTheDocument();
    expect(await screen.findByText("数据加载失败")).toBeInTheDocument();
  });
});
