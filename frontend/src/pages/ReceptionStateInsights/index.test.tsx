import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReceptionStateInsightsResponse } from "@/types/api";
import ReceptionStateInsightsPage from "./index";

vi.mock("@/api/services", () => ({
  getReceptionStateInsights: vi.fn(),
}));

import { getReceptionStateInsights } from "@/api/services";

const mockedGetInsights = getReceptionStateInsights as unknown as ReturnType<
  typeof vi.fn
>;

const RESPONSE: ReceptionStateInsightsResponse = {
  tenant_id: "tenant-a",
  total_receptions: 386,
  total_transitions: 742,
  returned_transitions: 3,
  transition_limit: 100,
  truncated: false,
  stages: [
    {
      state: "初次接触",
      count: 312,
      reception_count: 312,
      incoming_count: 0,
      outgoing_count: 286,
      average_confidence: 0.94,
    },
    {
      state: "需求发现",
      count: 286,
      reception_count: 286,
      incoming_count: 286,
      outgoing_count: 214,
      average_confidence: 0.88,
    },
    {
      state: "异议处理",
      count: 146,
      reception_count: 146,
      incoming_count: 132,
      outgoing_count: 98,
      average_confidence: 0.79,
    },
  ],
  transitions: [
    {
      from_state: "初次接触",
      to_state: "需求发现",
      count: 286,
      average_confidence: 0.92,
      evidence_count: 190,
      top_triggers: [
        { trigger: "客户询问预算", count: 132 },
        { trigger: "销售开放式提问", count: 98 },
      ],
      sample_reception_ids: [101, 102],
    },
    {
      from_state: "需求发现",
      to_state: "异议处理",
      count: 132,
      average_confidence: 0.82,
      evidence_count: 88,
      top_triggers: [{ trigger: "价格异议", count: 76 }],
      sample_reception_ids: [103],
    },
    {
      from_state: "异议处理",
      to_state: "需求发现",
      count: 11,
      average_confidence: 0.23,
      evidence_count: 7,
      top_triggers: [{ trigger: "跳转异常", count: 11 }],
      sample_reception_ids: [104],
    },
  ],
  generated_at: "2026-07-24T09:00:00Z",
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ReceptionStateInsightsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ReceptionStateInsightsPage", () => {
  beforeEach(() => {
    mockedGetInsights.mockReset();
    mockedGetInsights.mockResolvedValue(RESPONSE);
  });

  it("renders the aggregate state path and opens evidence-backed drilldown", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(
      await screen.findByRole("heading", { name: "跨接待状态流" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("group", { name: /聚合状态图谱，共 3 个阶段、3 条路径/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("386")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", {
        name: "查看 初次接触 到 需求发现 的聚合证据",
      }),
    );

    expect(
      screen.getByRole("complementary", { name: "路径证据" }),
    ).toHaveTextContent("客户询问预算");
    expect(
      screen.getByRole("link", { name: "查看接待 101 详情" }),
    ).toHaveAttribute(
      "href",
      "/receptions/101/graph?mode=state&from=%E5%88%9D%E6%AC%A1%E6%8E%A5%E8%A7%A6&to=%E9%9C%80%E6%B1%82%E5%8F%91%E7%8E%B0",
    );
    await user.click(screen.getByRole("button", { name: "重置" }));
    expect(
      screen.queryByRole("complementary", { name: "路径证据" }),
    ).not.toBeInTheDocument();
  });

  it("opens stage-level evidence and can continue into a connected path", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "跨接待状态流" });
    await user.click(
      screen.getByRole("button", { name: "查看 需求发现 阶段证据" }),
    );

    const nodeEvidence = screen.getByRole("complementary", {
      name: "节点证据",
    });
    expect(nodeEvidence).toHaveTextContent("接待覆盖");
    expect(nodeEvidence).toHaveTextContent("预算多少");

    await user.click(
      screen.getByRole("button", { name: "初次接触 → 需求发现" }),
    );
    expect(
      screen.getByRole("complementary", { name: "路径证据" }),
    ).toHaveTextContent("销售开放式提问");
  });

  it("submits bounded store, agent, scenario and time filters", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "跨接待状态流" });

    await user.type(screen.getByRole("textbox", { name: "门店 ID" }), "S1,S2");
    await user.type(screen.getByRole("textbox", { name: "销售姓名" }), "小林");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "业务场景" }),
      "gold",
    );
    await user.type(
      screen.getByLabelText("开始时间"),
      "2026-07-01T00:00",
    );
    await user.type(
      screen.getByLabelText("结束时间"),
      "2026-07-24T23:59",
    );
    await user.click(screen.getByRole("button", { name: "应用聚合筛选" }));

    await waitFor(() => {
      expect(mockedGetInsights).toHaveBeenLastCalledWith({
        store_id: ["S1", "S2"],
        agent_name: ["小林"],
        scenario: ["gold"],
        started_from: new Date("2026-07-01T00:00").toISOString(),
        started_to: new Date("2026-07-24T23:59").toISOString(),
        transition_limit: 100,
      });
    });
  });

  it("rejects an invalid time range before calling the aggregate API", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "跨接待状态流" });
    mockedGetInsights.mockClear();

    await user.type(
      screen.getByLabelText("开始时间"),
      "2026-07-24T23:59",
    );
    await user.type(
      screen.getByLabelText("结束时间"),
      "2026-07-01T00:00",
    );
    await user.click(screen.getByRole("button", { name: "应用聚合筛选" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "结束时间必须晚于开始时间。",
    );
    expect(screen.getByLabelText("开始时间")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByLabelText("结束时间")).toHaveAttribute(
      "aria-describedby",
      "ag-state-filter-error",
    );
    expect(mockedGetInsights).not.toHaveBeenCalled();
  });

  it("labels bounded aggregate data without overstating completeness", async () => {
    mockedGetInsights.mockResolvedValueOnce({
      ...RESPONSE,
      returned_stages: RESPONSE.stages.length,
      stage_limit: 64,
      returned_transitions: RESPONSE.transitions.length,
      transition_limit: 3,
      truncated: true,
    });
    renderPage();

    await screen.findByRole("heading", { name: "跨接待状态流" });
    expect(screen.getByText("聚合结果已按预算截断")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "返回路径" })).toBeInTheDocument();
    expect(screen.getByText("可见关键路径占比")).toBeInTheDocument();
    expect(screen.getAllByText("场景参考话句").length).toBeGreaterThan(0);
    expect(screen.queryByText("完成率", { exact: false })).not.toBeInTheDocument();
  });

  it("offers an explicit retry state when aggregate data is unavailable", async () => {
    const user = userEvent.setup();
    mockedGetInsights.mockRejectedValueOnce(new Error("offline"));
    renderPage();

    expect(
      await screen.findByRole("alert", { name: "聚合状态图暂不可用" }),
    ).toBeInTheDocument();
    mockedGetInsights.mockResolvedValueOnce(RESPONSE);
    await user.click(screen.getByRole("button", { name: "重新加载" }));
    expect(
      await screen.findByRole("heading", { name: "跨接待状态流" }),
    ).toBeInTheDocument();
  });

  it("never renders an impossible path share above 100 percent", async () => {
    mockedGetInsights.mockResolvedValueOnce({
      ...RESPONSE,
      total_transitions: 1,
      transitions: [
        {
          ...RESPONSE.transitions[0],
          count: 2,
          average_confidence: 0.9,
        },
      ],
      returned_transitions: 1,
    });
    renderPage();

    await screen.findByRole("heading", { name: "跨接待状态流" });
    expect(screen.getByText("100.0%")).toBeInTheDocument();
    expect(screen.queryByText("200.0%")).not.toBeInTheDocument();
  });

  it("keeps the aggregate SVG bounded when many stages are returned", async () => {
    mockedGetInsights.mockResolvedValueOnce({
      ...RESPONSE,
      stages: Array.from({ length: 24 }, (_, index) => ({
        state: `阶段 ${index + 1}`,
        count: 24 - index,
        reception_count: 24 - index,
        incoming_count: index,
        outgoing_count: Math.max(0, 23 - index),
        average_confidence: 0.8,
      })),
      transitions: [],
      returned_transitions: 0,
    });

    renderPage();

    const graph = await screen.findByRole("group", {
      name: "聚合状态图谱，共 6 个阶段、0 条路径",
    });
    expect(graph).toBeInTheDocument();
    expect(screen.getByText(/前 6 个阶段/)).toBeInTheDocument();
    const stagePositions = [
      ...graph.querySelectorAll<SVGGElement>(".ag-state-community"),
    ].map((stage) => Number(stage.dataset.x));
    expect(stagePositions.every(Number.isFinite)).toBe(true);
    stagePositions.slice(1).forEach((position, index) => {
      expect(position - stagePositions[index]).toBeGreaterThanOrEqual(150);
    });
  });

  it("shows an explicit empty result instead of a blank graph", async () => {
    mockedGetInsights.mockResolvedValueOnce({
      ...RESPONSE,
      total_receptions: 0,
      total_transitions: 0,
      stages: [],
      transitions: [],
      returned_transitions: 0,
    });

    renderPage();

    expect(
      await screen.findByRole("status", {
        name: "暂无符合条件的状态路径",
      }),
    ).toHaveTextContent("调整门店、销售、场景或时间范围");
  });
});
