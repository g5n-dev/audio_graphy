import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TagInsightsPage from "./index";
import type {
  AnalyzeTagInsightsRequest,
  AnalyzeTagInsightsResponse,
  ReceptionTagInsightsResponse,
} from "@/types/api";

vi.mock("@/api/services", () => ({
  analyzeTagInsights: vi.fn(),
  getReceptionTagInsights: vi.fn(),
}));

import { analyzeTagInsights, getReceptionTagInsights } from "@/api/services";

const mockedAnalyze = analyzeTagInsights as unknown as ReturnType<typeof vi.fn>;
const mockedGetPersistedInsights =
  getReceptionTagInsights as unknown as ReturnType<typeof vi.fn>;

const SNAPSHOT: AnalyzeTagInsightsRequest = {
  tenant_id: "tenant-a",
  merge_strategy: "manual_wins",
  groups: [
    {
      group_key: "model",
      version: "v1",
      source: "llm",
      priority: 10,
    },
    {
      group_key: "review",
      version: "v2",
      source: "manual",
      priority: 20,
    },
  ],
  assignments: [
    {
      group_key: "model",
      target_id: "reception-1",
      window: { start_ms: 0, end_ms: 10_000 },
      label_key: "stage.greeting",
      value: "fail",
      confidence: 0.61,
      evidence_refs: [],
      is_manual: false,
      occurred_at: "2026-07-23T01:00:00Z",
      store_id: "store-1",
      agent_id: "agent-1",
    },
    {
      group_key: "review",
      target_id: "reception-1",
      window: { start_ms: 0, end_ms: 10_000 },
      label_key: "stage.greeting",
      value: "pass",
      confidence: 1,
      evidence_refs: [
        {
          ref_id: "text-1",
          kind: "text",
          recording_id: "recording-1",
          start_ms: 100,
          end_ms: 1_000,
          text_excerpt: "您好，欢迎光临",
        },
      ],
      is_manual: true,
      occurred_at: "2026-07-23T01:00:00Z",
      store_id: "store-1",
      agent_id: "agent-1",
    },
  ],
  trend_granularity: "day",
  top_n_co_occurrences: 50,
};

const NORMALIZED_SNAPSHOT: AnalyzeTagInsightsRequest = {
  ...SNAPSHOT,
  groups: SNAPSHOT.groups.map((group) => ({
    ...group,
    group_id: `${group.group_key}@${group.version}`,
  })),
  assignments: SNAPSHOT.assignments.map((assignment) => {
    const group = SNAPSHOT.groups.find(
      (candidate) => candidate.group_key === assignment.group_key,
    )!;
    return {
      ...assignment,
      group_version: group.version,
      group_id: `${group.group_key}@${group.version}`,
    };
  }),
};

const RESPONSE: AnalyzeTagInsightsResponse = {
  tenant_id: "tenant-a",
  merge_strategy: "intersection",
  groups: SNAPSHOT.groups,
  truncated: false,
  matrix_truncated: false,
  difference_truncated: false,
  evidence_truncated: false,
  output_budget: {
    matrix_limit: 96,
    matrix_total_rows: 1,
    matrix_returned_rows: 1,
    difference_limit: 128,
    difference_total_items: 1,
    difference_returned_items: 0,
    distribution_limit: 512,
    distribution_total_items: 1,
    distribution_returned_items: 1,
    trend_limit: 512,
    trend_total_items: 1,
    trend_returned_items: 1,
    dimension_limit: 256,
    dimension_total_items: 1,
    dimension_returned_items: 1,
    evidence_ref_limit: 512,
    evidence_ref_count: 2,
    evidence_text_byte_limit: 32_768,
    evidence_text_bytes: 18,
  },
  overview: {
    group_count: 2,
    assignment_count: 2,
    total_cells: 1,
    complete_cells: 1,
    incomplete_cells: 0,
    conflict_cells: 1,
    conflict_rate: 1,
  },
  matrix: [
    {
      target_id: "reception-1",
      window: { start_ms: 0, end_ms: 10_000 },
      label_key: "stage.greeting",
      store_ids: ["store-1"],
      agent_ids: ["agent-1"],
      cells: [
        {
          group: SNAPSHOT.groups[0],
          assignments: [SNAPSHOT.assignments[0]],
          missing: false,
        },
        {
          group: SNAPSHOT.groups[1],
          assignments: [SNAPSHOT.assignments[1]],
          missing: false,
        },
      ],
      merged: {
        strategy: "intersection",
        values: [],
        selected_group_keys: [],
        confidence: null,
        evidence_refs: [],
      },
      conflict: true,
      missing_group_keys: [],
    },
  ],
  coverage: [
    {
      group_key: "model",
      assigned_cells: 1,
      missing_cells: 0,
      coverage_rate: 1,
    },
    {
      group_key: "review",
      assigned_cells: 1,
      missing_cells: 0,
      coverage_rate: 1,
    },
  ],
  pairwise: [
    {
      left_group_key: "model",
      right_group_key: "review",
      comparable_cells: 1,
      agreements: 0,
      differences: 1,
      agreement_rate: 0,
      left_only_cells: 0,
      right_only_cells: 0,
      overlap_rate: 1,
      difference_items: [],
      difference_items_truncated: false,
    },
  ],
  distributions: [
    {
      group_key: "model",
      label_key: "stage.greeting",
      value: "fail",
      count: 1,
      proportion: 1,
    },
  ],
  trends: [
    {
      bucket_key: "2026-07-23",
      group_key: "model",
      label_key: "stage.greeting",
      value: "fail",
      count: 1,
    },
  ],
  co_occurrences: [],
  confidence: [
    {
      group_key: "model",
      bucket: "0.6-0.7",
      count: 1,
      average_confidence: 0.61,
    },
  ],
  dimension_comparisons: [
    {
      dimension: "store",
      dimension_value: "store-1",
      group_key: "model",
      total_cells: 1,
      assignment_count: 1,
      missing_cells: 0,
      coverage_rate: 1,
      unique_targets: 1,
      average_confidence: 0.61,
      conflict_assignments: 1,
      conflict_rate: 1,
    },
  ],
};

const EMPTY_PERSISTED_RESPONSE: ReceptionTagInsightsResponse = {
  tenant_id: "tenant-a",
  page: 1,
  page_size: 20,
  total_receptions: 0,
  returned_reception_ids: [],
  total_assignments: 0,
  assignment_count: 0,
  assignment_limit: 1_000,
  truncated: false,
  assignment_truncated: false,
  group_truncated: false,
  difference_truncated: false,
  evidence_truncated: false,
  evidence_ref_limit: 1_024,
  evidence_ref_count: 0,
  evidence_summary_total: 0,
  evidence_summary_count: 0,
  evidence_summary_limit: 256,
  evidence_summary_truncated: false,
  selection_mode: "current",
  selected_group_ids: [],
  merge_strategy: "manual_wins",
  trend_granularity: "day",
  insights: null,
  evidence_summary: [],
  generated_at: "2026-07-23T02:00:00Z",
};

function renderPage(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TagInsightsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function importSnapshot(): Promise<void> {
  await userEvent.click(screen.getByText("高级：导入 JSON 标签快照"));
  fireEvent.change(screen.getByRole("textbox", { name: "标签快照 JSON" }), {
    target: { value: JSON.stringify(SNAPSHOT) },
  });
  await userEvent.click(screen.getByRole("button", { name: "载入标签快照" }));
}

describe("TagInsightsPage", () => {
  beforeEach(() => {
    mockedAnalyze.mockReset();
    mockedGetPersistedInsights.mockReset();
    mockedAnalyze.mockResolvedValue(RESPONSE);
    mockedGetPersistedInsights.mockResolvedValue(EMPTY_PERSISTED_RESPONSE);
  });

  it("loads persisted database insights first and shows an explicit no-data state", async () => {
    renderPage();
    expect(
      await screen.findByText("数据库中暂无符合条件的目标标签"),
    ).toBeInTheDocument();
    expect(mockedGetPersistedInsights).toHaveBeenCalledWith({
      page: 1,
      page_size: 20,
      assignment_limit: 1_000,
      matrix_limit: 96,
      difference_limit: 128,
      evidence_summary_limit: 256,
      merge_strategy: "manual_wins",
      trend_granularity: "day",
      top_n_co_occurrences: 50,
    });
  });

  it("renders the real matrix, truncation state, and persisted evidence summary", async () => {
    const persistedInsights: AnalyzeTagInsightsResponse = {
      ...RESPONSE,
      matrix: RESPONSE.matrix.map((row) => ({
        ...row,
        target_id: "reception:1/unit:10",
      })),
    };
    mockedGetPersistedInsights.mockResolvedValueOnce({
      ...EMPTY_PERSISTED_RESPONSE,
      total_receptions: 31,
      returned_reception_ids: [1],
      total_assignments: 5_001,
      assignment_count: 1_000,
      truncated: true,
      assignment_truncated: true,
      group_truncated: true,
      difference_truncated: true,
      evidence_truncated: true,
      evidence_ref_count: 1_024,
      evidence_summary_total: 1_000,
      evidence_summary_count: 256,
      evidence_summary_truncated: true,
      selection_mode: "exact_versions",
      selected_group_ids: ["review@v1", "review@v2"],
      insights: {
        ...persistedInsights,
        truncated: true,
        matrix_truncated: true,
        difference_truncated: true,
        evidence_truncated: true,
        output_budget: {
          ...persistedInsights.output_budget,
          matrix_total_rows: 800,
          matrix_returned_rows: 96,
          difference_total_items: 2_000,
          difference_returned_items: 128,
          evidence_ref_count: 512,
        },
      },
      evidence_summary: [
        {
          reception_id: 1,
          dialogue_unit_id: 10,
          group_id: "review@v2",
          label_key: "stage.greeting",
          label_value: "pass",
          confidence: 1,
          evidence_count: 1,
          evidence_refs: [
            {
              ref_id: "text-1",
              kind: "text",
              recording_id: 101,
              timeline_start_ms: 100,
              timeline_end_ms: 1_000,
              text_excerpt: "您好，欢迎光临",
            },
          ],
        },
      ],
    });
    renderPage();

    expect(
      await screen.findByRole("heading", {
        name: "多标签组对比与溯源图",
      }),
    ).toBeInTheDocument();
    expect(await screen.findByText("标签矩阵")).toBeInTheDocument();
    expect(screen.getByText("结果已截断")).toBeInTheDocument();
    expect(screen.getByText("洞察输出已按预算截断")).toBeInTheDocument();
    expect(screen.getByText(/历史版本精确对比/)).toHaveTextContent(
      "review@v1, review@v2",
    );
    expect(screen.getByText("持久化证据摘要")).toBeInTheDocument();
    expect(screen.getAllByText("您好，欢迎光临").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("link", { name: /到接待调听定位/ }),
    ).toHaveAttribute("href", "/receptions/1/workspace?recording=101&at=100");

    await userEvent.click(
      screen.getByRole("cell", {
        name: "查看 reception:1/unit:10 stage.greeting 的证据",
      }),
    );
    expect(
      screen.getAllByRole("link", { name: /到调听工作台定位/ })[0],
    ).toHaveAttribute(
      "href",
      "/receptions/1/workspace?recording=recording-1&at=100",
    );
  });

  it("submits store, agent, scenario, time, reception and group filters to the database endpoint", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("数据库中暂无符合条件的目标标签");

    await user.type(screen.getByRole("textbox", { name: "门店 ID" }), "S1, S2");
    await user.type(screen.getByRole("textbox", { name: "销售姓名" }), "小林");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "业务场景" }),
      "gold",
    );
    await user.type(screen.getByRole("textbox", { name: "接待 ID" }), "9,10");
    await user.type(
      screen.getByRole("textbox", { name: "标签组 key" }),
      "model,review",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "趋势粒度" }),
      "week",
    );
    await user.click(screen.getByRole("button", { name: "加载数据库洞察" }));

    await waitFor(() => {
      expect(mockedGetPersistedInsights).toHaveBeenLastCalledWith({
        store_id: ["S1", "S2"],
        agent_name: ["小林"],
        scenario: ["gold"],
        reception_id: [9, 10],
        group_key: ["model", "review"],
        page: 1,
        page_size: 20,
        assignment_limit: 1_000,
        matrix_limit: 96,
        difference_limit: 128,
        evidence_summary_limit: 256,
        merge_strategy: "manual_wins",
        trend_granularity: "week",
        top_n_co_occurrences: 50,
      });
    });
  });

  it("submits exact key@version selections for persisted historical comparison", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("数据库中暂无符合条件的目标标签");

    await user.type(
      screen.getByRole("textbox", { name: "精确标签组版本" }),
      "review@v1, review@v2",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "数据库合并策略" }),
      "union",
    );
    await user.click(screen.getByRole("button", { name: "加载数据库洞察" }));

    await waitFor(() => {
      expect(mockedGetPersistedInsights).toHaveBeenLastCalledWith({
        group_id: ["review@v1", "review@v2"],
        page: 1,
        page_size: 20,
        assignment_limit: 1_000,
        matrix_limit: 96,
        difference_limit: 128,
        evidence_summary_limit: 256,
        merge_strategy: "union",
        trend_granularity: "day",
        top_n_co_occurrences: 50,
      });
    });
  });

  it("rejects mixing broad group keys with exact historical versions", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("数据库中暂无符合条件的目标标签");

    await user.type(
      screen.getByRole("textbox", { name: "标签组 key" }),
      "review",
    );
    await user.type(
      screen.getByRole("textbox", { name: "精确标签组版本" }),
      "review@v1",
    );
    await user.click(screen.getByRole("button", { name: "加载数据库洞察" }));

    expect(
      await screen.findByText(
        "标签组 key 与精确 key@version 不能同时填写。",
      ),
    ).toBeInTheDocument();
    expect(mockedGetPersistedInsights).toHaveBeenCalledTimes(1);
  });

  it("keeps two versions of the same group as independent comparison columns", async () => {
    renderPage();
    await userEvent.click(await screen.findByText("高级：导入 JSON 标签快照"));
    const multiVersionSnapshot: AnalyzeTagInsightsRequest = {
      ...SNAPSHOT,
      groups: [
        {
          ...SNAPSHOT.groups[0],
          version: "v1",
        },
        {
          ...SNAPSHOT.groups[0],
          version: "v2",
        },
      ],
      assignments: [
        {
          ...SNAPSHOT.assignments[0],
          group_key: "model",
          group_version: "v1",
          value: "fail",
        },
        {
          ...SNAPSHOT.assignments[1],
          group_key: "model",
          group_version: "v2",
          value: "pass",
        },
      ],
    };
    fireEvent.change(screen.getByRole("textbox", { name: "标签快照 JSON" }), {
      target: { value: JSON.stringify(multiVersionSnapshot) },
    });
    await userEvent.click(screen.getByRole("button", { name: "载入标签快照" }));
    expect(
      screen.getByRole("checkbox", { name: "选择标签组 model@v1" }),
    ).toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: "选择标签组 model@v2" }),
    ).toBeChecked();

    await userEvent.click(
      screen.getByRole("button", { name: "运行合并与对比分析" }),
    );

    await waitFor(() => {
      expect(mockedAnalyze).toHaveBeenCalledWith({
        ...multiVersionSnapshot,
        groups: [
          { ...multiVersionSnapshot.groups[0], group_id: "model@v1" },
          { ...multiVersionSnapshot.groups[1], group_id: "model@v2" },
        ],
        assignments: [
          {
            ...multiVersionSnapshot.assignments[0],
            group_id: "model@v1",
          },
          {
            ...multiVersionSnapshot.assignments[1],
            group_id: "model@v2",
          },
        ],
      });
    });
  });

  it("submits selected groups and merge strategy to the analysis API", async () => {
    const user = userEvent.setup();
    renderPage();
    await importSnapshot();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "标签合并策略" }),
      "intersection",
    );
    await user.click(
      screen.getByRole("button", { name: "运行合并与对比分析" }),
    );

    await waitFor(() => {
      expect(mockedAnalyze).toHaveBeenCalledWith({
        ...NORMALIZED_SNAPSHOT,
        merge_strategy: "intersection",
      });
    });
  });

  it("renders conflict and group values in the tag matrix", async () => {
    renderPage();
    await importSnapshot();
    await userEvent.click(
      screen.getByRole("button", { name: "运行合并与对比分析" }),
    );

    expect(await screen.findByText("标签矩阵")).toBeInTheDocument();
    expect(screen.getAllByText("fail").length).toBeGreaterThan(0);
    expect(screen.getAllByText("pass").length).toBeGreaterThan(0);
    expect(screen.getByText("冲突")).toBeInTheDocument();
  });

  it("scrolls between insight views without hijacking the HashRouter route", async () => {
    mockedGetPersistedInsights.mockResolvedValueOnce({
      ...EMPTY_PERSISTED_RESPONSE,
      total_receptions: 1,
      returned_reception_ids: [1],
      total_assignments: 2,
      assignment_count: 2,
      insights: RESPONSE,
    });
    renderPage();

    await screen.findByRole("heading", { name: "多标签组对比与溯源图" });
    const chartSection = document.getElementById("tag-chart-insights");
    expect(chartSection).not.toBeNull();
    const scrollIntoView = vi.fn();
    Object.defineProperty(chartSection, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });

    const relationshipButton = screen.getByRole("button", {
      name: "关系图谱",
    });
    const chartButton = screen.getByRole("button", { name: "图表分析" });
    expect(relationshipButton).toHaveAttribute("aria-pressed", "true");
    expect(chartButton).toHaveAttribute("aria-pressed", "false");

    await userEvent.click(chartButton);

    expect(scrollIntoView).toHaveBeenCalledWith({
      behavior: "smooth",
      block: "start",
    });
    expect(relationshipButton).toHaveAttribute("aria-pressed", "false");
    expect(chartButton).toHaveAttribute("aria-pressed", "true");
  });
});
