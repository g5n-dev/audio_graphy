import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReceptionWorkspaceResponse } from "@/types/api";
import ReceptionGraphPage from "./GraphView";

vi.mock("@/api/services", () => ({
  getReceptionWorkspace: vi.fn(),
}));

vi.mock("@/components/dialogue/DialogueGraph", () => ({
  DialogueGraph: ({
    mode,
    highlightedTransition,
    onNodeDetails,
  }: {
    mode: string;
    highlightedTransition?: {
      fromState: string;
      toState: string;
    } | null;
    onNodeDetails?: (node: {
      id: string;
      label: string;
      kind: string;
      community: string;
    }) => void;
  }) => (
    <div>
      <span>{`graph-mode-${mode}`}</span>
      <output aria-label="图谱高亮路径">
        {highlightedTransition
          ? `${highlightedTransition.fromState}-${highlightedTransition.toState}`
          : "none"}
      </output>
      <button
        type="button"
        onClick={() =>
          onNodeDetails?.({
            id: "unit-12",
            label: "需求确认",
            kind: "unit",
            community: "dialogue-units",
          })
        }
      >
        选择图谱节点
      </button>
    </div>
  ),
}));

import { getReceptionWorkspace } from "@/api/services";

const mockedGetWorkspace = getReceptionWorkspace as unknown as ReturnType<
  typeof vi.fn
>;

const WORKSPACE = {
  reception: {
    id: "101",
    version: 3,
    scenario: "gold",
    store_id: "S1",
    agent_name: "顾问小林",
  },
  recordings: [{ id: "r1" }, { id: "r2" }],
  dialogue_units: [{ id: "u1" }, { id: "u2" }, { id: "u3" }],
  tag_assignments: [{ id: "t1" }, { id: "t2" }],
  state_transitions: [{ id: "s1" }],
  audit_events: [{ id: "a1" }],
  window: {
    start_sec: 0,
    end_sec: 120,
    size_sec: 600,
    reception_duration_sec: 120,
    truncated: false,
    has_previous: false,
    has_next: false,
    previous_start_sec: null,
    next_start_sec: null,
    total_dialogue_units: 3,
    protected_dialogue_units: 3,
    dialogue_units: { total: 3, returned: 3, limit: 100, truncated: false },
    tag_assignments: { total: 2, returned: 2, limit: 200, truncated: false },
    state_transitions: {
      total: 1,
      returned: 1,
      limit: 100,
      truncated: false,
    },
    transcript_items: {
      total: 0,
      returned: 0,
      limit: 300,
      truncated: false,
    },
    provenance_events: {
      total: 1,
      returned: 1,
      limit: 100,
      truncated: false,
    },
  },
} as unknown as ReceptionWorkspaceResponse;

function renderPage(path = "/receptions/101/graph") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/receptions/:id/graph"
            element={<ReceptionGraphPage />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ReceptionGraphPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetWorkspace.mockResolvedValue(WORKSPACE);
  });

  it("从聚合路径深链打开详情状态图，并保留返回聚合入口", async () => {
    renderPage(
      "/receptions/101/graph?mode=state&from=%E5%88%9D%E6%AC%A1%E6%8E%A5%E8%A7%A6&to=%E9%9C%80%E6%B1%82%E5%8F%91%E7%8E%B0",
    );

    expect(await screen.findByText("graph-mode-state")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /状态转移/ }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("初次接触 → 需求发现")).toBeInTheDocument();
    const windowSummary = screen.getByRole("status", {
      name: "当前图谱数据窗口",
    });
    expect(within(windowSummary).getByText("当前窗口")).toBeInTheDocument();
    expect(
      within(windowSummary).getByText("00:00–02:00 / 总时长 02:00"),
    ).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "图谱高亮路径" })).toHaveTextContent(
      "初次接触-需求发现",
    );
    expect(
      screen.getByRole("link", { name: "查看跨接待状态路径" }),
    ).toHaveAttribute("href", "/reception-flow");
  });

  it("切换图谱模式并在节点激活后展示详情", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("graph-mode-relation")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /溯源 DAG/ }));
    expect(screen.getByText("graph-mode-provenance")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "选择图谱节点" }));
    expect(
      screen.getByRole("complementary", { name: "节点详情" }),
    ).toHaveTextContent("需求确认");
    expect(
      screen.getByRole("complementary", { name: "节点详情" }),
    ).toHaveTextContent("dialogue-units");
    expect(screen.getAllByText("0 个意向信号").length).toBeGreaterThan(0);
  });

  it("展示当前时间偏移、窗口真实总量和集合截断上限", async () => {
    mockedGetWorkspace.mockResolvedValueOnce({
      ...WORKSPACE,
      window: {
        ...WORKSPACE.window,
        start_sec: 600,
        end_sec: 1200,
        size_sec: 600,
        reception_duration_sec: 2700,
        truncated: true,
        has_previous: true,
        has_next: true,
        previous_start_sec: 0,
        next_start_sec: 1200,
        total_dialogue_units: 520,
        protected_dialogue_units: 17,
        dialogue_units: {
          total: 135,
          returned: 100,
          limit: 100,
          truncated: true,
        },
        tag_assignments: {
          total: 350,
          returned: 200,
          limit: 200,
          truncated: true,
        },
        state_transitions: {
          total: 60,
          returned: 60,
          limit: 100,
          truncated: false,
        },
        provenance_events: {
          total: 101,
          returned: 100,
          limit: 100,
          truncated: true,
        },
      },
    });

    renderPage();

    const windowSummary = await screen.findByRole("status", {
      name: "当前图谱数据窗口",
    });
    expect(within(windowSummary).getByText("当前窗口")).toBeInTheDocument();
    expect(
      within(windowSummary).getByText("10:00–20:00 / 总时长 45:00"),
    ).toBeInTheDocument();
    expect(
      within(windowSummary).getByText("100 / 135 · 上限 100"),
    ).toBeInTheDocument();
    expect(
      within(windowSummary).getByText("200 / 350 · 上限 200"),
    ).toBeInTheDocument();
    expect(
      within(windowSummary).getByText("60 / 60 · 上限 100"),
    ).toBeInTheDocument();
    expect(
      within(windowSummary).getByText("100 / 101 · 上限 100"),
    ).toBeInTheDocument();
    expect(windowSummary).toHaveTextContent("整场对话单元 520");
    expect(windowSummary).toHaveTextContent("当前仅展示完整接待的一个时间窗口");
    expect(windowSummary).toHaveTextContent("当前窗口内部分数据已截断");

    const legend = screen.getByRole("complementary", { name: "图谱图例" });
    expect(within(legend).getByText("100 / 135")).toBeInTheDocument();
    expect(within(legend).getByText("200 / 350")).toBeInTheDocument();
    expect(within(legend).getByText("60 / 60")).toBeInTheDocument();
  });
});
