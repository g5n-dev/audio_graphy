import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { AnalyzeTagInsightsResponse } from "@/types/api";
import { InsightVisuals } from "./InsightVisuals";

const RESULT: AnalyzeTagInsightsResponse = {
  tenant_id: "tenant-a",
  merge_strategy: "manual_wins",
  groups: [],
  truncated: false,
  matrix_truncated: false,
  difference_truncated: false,
  evidence_truncated: false,
  output_budget: {
    matrix_limit: 96,
    matrix_total_rows: 0,
    matrix_returned_rows: 0,
    difference_limit: 128,
    difference_total_items: 0,
    difference_returned_items: 0,
    distribution_limit: 512,
    distribution_total_items: 2,
    distribution_returned_items: 2,
    trend_limit: 512,
    trend_total_items: 4,
    trend_returned_items: 4,
    dimension_limit: 256,
    dimension_total_items: 0,
    dimension_returned_items: 0,
    evidence_ref_limit: 512,
    evidence_ref_count: 0,
    evidence_text_byte_limit: 32_768,
    evidence_text_bytes: 0,
  },
  overview: {
    group_count: 1,
    assignment_count: 20,
    total_cells: 20,
    complete_cells: 20,
    incomplete_cells: 0,
    conflict_cells: 0,
    conflict_rate: 0,
  },
  matrix: [],
  coverage: [],
  pairwise: [],
  distributions: [
    {
      group_key: "模型 A",
      label_key: "stage.greeting",
      value: "完成",
      count: 12,
      proportion: 0.6,
    },
    {
      group_key: "模型 A",
      label_key: "stage.requirement",
      value: "完成",
      count: 8,
      proportion: 0.4,
    },
  ],
  trends: [
    {
      bucket_key: "2026-07-21",
      group_key: "模型 A",
      label_key: "stage.greeting",
      value: "完成",
      count: 4,
    },
    {
      bucket_key: "2026-07-22",
      group_key: "模型 A",
      label_key: "stage.greeting",
      value: "完成",
      count: 8,
    },
    {
      bucket_key: "2026-07-23",
      group_key: "模型 A",
      label_key: "stage.greeting",
      value: "完成",
      count: 12,
    },
    {
      bucket_key: "2026-07-23",
      group_key: "模型 A",
      label_key: "intent.level",
      value: "高=意向",
      count: 6,
    },
  ],
  co_occurrences: [],
  confidence: [],
  dimension_comparisons: [],
};

function renderVisuals(result: AnalyzeTagInsightsResponse = RESULT) {
  return render(
    <MemoryRouter>
      <InsightVisuals result={result} />
    </MemoryRouter>,
  );
}

describe("InsightVisuals", () => {
  it("presents stage data as event volume instead of a conversion funnel", () => {
    renderVisuals();

    const card = screen
      .getByRole("heading", { name: "阶段事件量" })
      .closest("article");

    expect(card).not.toBeNull();
    expect(
      within(card as HTMLElement).getByText(
        "按已发生标签事件量降序；展示频次，不代表阶段到达率或转化率",
      ),
    ).toBeInTheDocument();
    expect(within(card as HTMLElement).getByText("12 次")).toBeInTheDocument();
    expect(within(card as HTMLElement).getByText("8 次")).toBeInTheDocument();
    expect(screen.queryByText("阶段漏斗")).not.toBeInTheDocument();
  });

  it("renders readable trend ticks and focusable points with exact values", () => {
    renderVisuals();

    const chart = screen.getByRole("group", { name: "标签趋势折线图" });
    expect(within(chart).getByText("0")).toBeInTheDocument();
    expect(within(chart).getByText("4")).toBeInTheDocument();
    expect(within(chart).getByText("8")).toBeInTheDocument();
    expect(within(chart).getByText("12")).toBeInTheDocument();

    const point = within(chart).getByRole("img", {
      name: "模型 A · stage.greeting=完成，2026-07-23：12",
    });
    expect(point).toHaveAttribute("tabindex", "0");
    expect(point.querySelector("title")).toHaveTextContent(
      "模型 A · stage.greeting=完成｜2026-07-23｜12 次",
    );

    expect(
      within(chart).getByRole("img", {
        name: "模型 A · intent.level=高=意向，2026-07-23：6",
      }),
    ).toBeInTheDocument();
  });

  it("summarizes coverage, agreement and conflict as actionable quality signals", () => {
    const result: AnalyzeTagInsightsResponse = {
      ...RESULT,
      overview: {
        ...RESULT.overview,
        total_cells: 20,
        complete_cells: 18,
        incomplete_cells: 2,
        conflict_cells: 3,
        conflict_rate: 0.15,
      },
      coverage: [
        {
          group_key: "模型 A",
          assigned_cells: 18,
          missing_cells: 2,
          coverage_rate: 0.9,
        },
      ],
      pairwise: [
        {
          left_group_key: "模型 A",
          right_group_key: "人工金标",
          comparable_cells: 18,
          agreements: 15,
          differences: 3,
          agreement_rate: 15 / 18,
          left_only_cells: 1,
          right_only_cells: 1,
          overlap_rate: 0.9,
          difference_items: [],
          difference_items_truncated: false,
        },
      ],
    };
    renderVisuals(result);

    const card = screen
      .getByRole("heading", { name: "质量闭环" })
      .closest("article");
    expect(card).not.toBeNull();
    expect(within(card as HTMLElement).getByText("平均覆盖率")).toBeVisible();
    expect(within(card as HTMLElement).getByText("90%")).toBeVisible();
    expect(within(card as HTMLElement).getByText("平均一致率")).toBeVisible();
    expect(within(card as HTMLElement).getByText("83.3%")).toBeVisible();
    expect(within(card as HTMLElement).getByText("冲突率")).toBeVisible();
    expect(within(card as HTMLElement).getByText("15%")).toBeVisible();
    expect(
      within(card as HTMLElement).getByRole("link", {
        name: "进入标签治理中心",
      }),
    ).toHaveAttribute("href", "/tag-governance");
  });
});
