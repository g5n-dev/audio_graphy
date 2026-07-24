import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type {
  AnalyzeTagInsightsResponse,
  TagInsightAssignment,
  TagInsightGroup,
  TagInsightMatrixRow,
} from "@/types/api";
import {
  TAG_INSIGHT_GRAPH_NODE_BUDGET,
  TagInsightGraph,
} from "./TagInsightGraph";

const GROUPS: TagInsightGroup[] = [
  {
    group_key: "model",
    version: "v1",
    group_id: "model@v1",
    source: "model",
    priority: 10,
  },
  {
    group_key: "review",
    version: "v2",
    group_id: "review@v2",
    source: "manual",
    priority: 20,
  },
];

function assignment(
  group: TagInsightGroup,
  labelKey: string,
  value: string,
  confidence: number,
  evidenceIndex?: number,
): TagInsightAssignment {
  return {
    group_key: group.group_key,
    group_version: group.version,
    group_id: group.group_id,
    target_id: "reception:88/unit:1",
    window: { start_ms: 0, end_ms: 10_000 },
    label_key: labelKey,
    value,
    confidence,
    evidence_refs:
      evidenceIndex === undefined
        ? []
        : [
            {
              ref_id: `evidence-${evidenceIndex}`,
              kind: evidenceIndex === 1 ? "audio" : "text",
              recording_id: "rec-1",
              start_ms: evidenceIndex * 1_000,
              end_ms: evidenceIndex * 1_000 + 1_200,
              text_excerpt:
                evidenceIndex === 1 ? "销售完成迎宾并询问需求" : "客户仍未被称呼",
            },
          ],
    is_manual: group.source === "manual",
    occurred_at: "2026-07-23T10:00:00Z",
    store_id: "store-1",
    agent_id: "agent-1",
  };
}

function matrixRow(
  labelKey: string,
  values: [string, string],
  rowIndex = 1,
): TagInsightMatrixRow {
  const left = assignment(
    GROUPS[0],
    labelKey,
    values[0],
    values[0] === values[1] ? 0.94 : 0.91,
    rowIndex === 1 ? 1 : undefined,
  );
  const right = assignment(
    GROUPS[1],
    labelKey,
    values[1],
    values[0] === values[1] ? 0.89 : 0.64,
    rowIndex === 1 ? 2 : undefined,
  );
  const conflict = values[0] !== values[1];
  return {
    target_id: `reception:88/unit:${rowIndex}`,
    window: { start_ms: (rowIndex - 1) * 10_000, end_ms: rowIndex * 10_000 },
    label_key: labelKey,
    store_ids: ["store-1"],
    agent_ids: ["agent-1"],
    cells: [
      { group: GROUPS[0], assignments: [left], missing: false },
      { group: GROUPS[1], assignments: [right], missing: false },
    ],
    merged: {
      strategy: "manual_wins",
      values: [values[1]],
      selected_group_keys: [GROUPS[1].group_key],
      confidence: right.confidence,
      evidence_refs: right.evidence_refs,
    },
    conflict,
    missing_group_keys: [],
  };
}

const RESPONSE: AnalyzeTagInsightsResponse = {
  tenant_id: "tenant-a",
  merge_strategy: "manual_wins",
  groups: GROUPS,
  truncated: false,
  matrix_truncated: false,
  difference_truncated: false,
  evidence_truncated: false,
  output_budget: {
    matrix_limit: 96,
    matrix_total_rows: 2,
    matrix_returned_rows: 2,
    difference_limit: 128,
    difference_total_items: 1,
    difference_returned_items: 1,
    distribution_limit: 512,
    distribution_total_items: 0,
    distribution_returned_items: 0,
    trend_limit: 512,
    trend_total_items: 0,
    trend_returned_items: 0,
    dimension_limit: 256,
    dimension_total_items: 0,
    dimension_returned_items: 0,
    evidence_ref_limit: 512,
    evidence_ref_count: 2,
    evidence_text_byte_limit: 32_768,
    evidence_text_bytes: 48,
  },
  overview: {
    group_count: 2,
    assignment_count: 4,
    total_cells: 2,
    complete_cells: 2,
    incomplete_cells: 0,
    conflict_cells: 1,
    conflict_rate: 0.5,
  },
  matrix: [
    matrixRow("stage.greeting", ["完成", "待改进"]),
    matrixRow("intent.level", ["高意向", "高意向"], 2),
  ],
  coverage: [],
  pairwise: [
    {
      left_group_key: "model",
      right_group_key: "review",
      comparable_cells: 2,
      agreements: 1,
      differences: 1,
      agreement_rate: 0.5,
      left_only_cells: 0,
      right_only_cells: 0,
      overlap_rate: 1,
      difference_items: [
        {
          target_id: "reception:88/unit:1",
          window: { start_ms: 0, end_ms: 10_000 },
          label_key: "stage.greeting",
          left_value: "完成",
          right_value: "待改进",
          left_evidence_count: 1,
          right_evidence_count: 1,
          left_evidence_refs:
            matrixRow("stage.greeting", ["完成", "待改进"]).cells[0]
              .assignments[0].evidence_refs,
          right_evidence_refs:
            matrixRow("stage.greeting", ["完成", "待改进"]).cells[1]
              .assignments[0].evidence_refs,
        },
      ],
      difference_items_truncated: false,
    },
  ],
  distributions: [],
  trends: [],
  co_occurrences: [
    {
      group_key: "model@v1",
      left_label: "stage.greeting=完成",
      right_label: "intent.level=高意向",
      count: 2,
    },
  ],
  confidence: [],
  dimension_comparisons: [],
};

function renderGraph(
  result: AnalyzeTagInsightsResponse = RESPONSE,
  receptionId?: string | number,
) {
  return render(
    <MemoryRouter>
      <TagInsightGraph result={result} receptionId={receptionId} />
    </MemoryRouter>,
  );
}

describe("TagInsightGraph", () => {
  it("renders an explicit empty state when there are no comparable cells", () => {
    renderGraph({ ...RESPONSE, matrix: [] });

    expect(screen.getByRole("status")).toHaveTextContent(
      "暂无可构建的标签关系",
    );
    expect(
      screen.queryByRole("group", {
        name: /多标签组对比与溯源图/,
      }),
    ).not.toBeInTheDocument();
  });

  it("renders light communities and all three traceable edge semantics", () => {
    renderGraph();

    const graph = screen.getByRole("group", {
      name: /多标签组对比与溯源图/,
    });
    expect(graph).toHaveAttribute("data-graph-mode", "tag-comparison");
    expect(graph.querySelector('[data-node-kind="focus"]')).not.toBeNull();
    expect(
      graph.querySelector(
        '[data-node-kind="value"][data-node-status="conflict"]',
      ),
    ).not.toBeNull();
    expect(
      graph.querySelector(
        '[data-node-kind="value"][data-node-status="agreement"]',
      ),
    ).not.toBeNull();

    const edgeKinds = new Set(
      [...graph.querySelectorAll("[data-edge-kind]")].map((edge) =>
        edge.getAttribute("data-edge-kind"),
      ),
    );
    expect(edgeKinds).toEqual(
      new Set(["evidence-ownership", "co-occurrence", "difference"]),
    );

    const nodeIds = new Set(
      [...graph.querySelectorAll("[data-node-id]")].map((node) =>
        node.getAttribute("data-node-id"),
      ),
    );
    graph.querySelectorAll("[data-edge-kind]").forEach((edge) => {
      expect(nodeIds.has(edge.getAttribute("data-source"))).toBe(true);
      expect(nodeIds.has(edge.getAttribute("data-target"))).toBe(true);
    });

    expect(graph).toHaveAttribute("data-layout", "radial-community");
    expect(graph.querySelector("[data-central-target]")).not.toBeNull();
    expect(
      graph.querySelectorAll("ellipse[data-community-field]").length,
    ).toBe(2);
    expect(screen.getByLabelText("图谱关系图例")).toBeInTheDocument();
    expect(screen.getByLabelText("图谱缩放控制")).toBeInTheDocument();
  });

  it("filters communities and conflict rows without losing accessible controls", () => {
    renderGraph();

    const communityFilter = screen.getByRole("combobox", {
      name: "标签社区筛选",
    });
    fireEvent.change(communityFilter, {
      target: { value: "stage.greeting" },
    });

    const graph = screen.getByRole("group", {
      name: /多标签组对比与溯源图/,
    });
    expect(
      graph.querySelector('[data-community="stage.greeting"]'),
    ).not.toBeNull();
    expect(
      graph.querySelector('[data-community="intent.level"]'),
    ).toBeNull();

    const conflictFilter = screen.getByRole("button", {
      name: "只看冲突标签",
    });
    expect(conflictFilter).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(conflictFilter);
    expect(conflictFilter).toHaveAttribute("aria-pressed", "true");
    expect(
      graph.querySelector('[data-node-status="agreement"]'),
    ).toBeNull();
  });

  it("supports zoom controls and exposes a hover intelligence preview", () => {
    renderGraph();

    const graph = screen.getByRole("group", {
      name: /多标签组对比与溯源图/,
    });
    expect(graph).toHaveAttribute("data-zoom", "100%");

    fireEvent.click(
      screen.getByRole("button", { name: "放大标签图谱" }),
    );
    expect(graph).toHaveAttribute("data-zoom", "110%");

    const valueNode = graph.querySelector('[data-node-kind="value"]');
    expect(valueNode).not.toBeNull();
    fireEvent.mouseEnter(valueNode as Element);
    expect(
      screen.getByRole("status", { name: "节点悬浮信息" }),
    ).toHaveTextContent("组版本");
    fireEvent.mouseLeave(valueNode as Element);
    expect(
      screen.queryByRole("status", { name: "节点悬浮信息" }),
    ).not.toBeInTheDocument();
  });

  it("opens a keyboard-accessible narrow detail panel with exact versions and evidence links", () => {
    renderGraph(RESPONSE, 88);

    const graph = screen.getByRole("group", {
      name: /多标签组对比与溯源图/,
    });
    const conflictNode = graph.querySelector(
      '[data-node-kind="value"][data-node-status="conflict"]',
    );
    expect(conflictNode).not.toBeNull();

    fireEvent.keyDown(conflictNode as Element, { key: "Enter" });

    const detail = screen.getByRole("complementary", {
      name: "标签节点详情",
    });
    expect(detail).toHaveAttribute("data-panel-width", "narrow");
    expect(within(detail).getByText("stage.greeting")).toBeInTheDocument();
    expect(within(detail).getByText("model@v1")).toBeInTheDocument();
    expect(within(detail).getByText("review@v2")).toBeInTheDocument();
    expect(within(detail).getByText("完成")).toBeInTheDocument();
    expect(within(detail).getByText("待改进")).toBeInTheDocument();
    expect(within(detail).getByText("91%")).toBeInTheDocument();
    expect(within(detail).getByText("00:01")).toBeInTheDocument();
    expect(
      within(detail).getAllByRole("link", { name: /到调听工作台/ })[0],
    ).toHaveAttribute(
      "href",
      "/receptions/88/workspace?recording=rec-1&at=1000",
    );

    fireEvent.keyDown(conflictNode as Element, { key: " " });
    expect(conflictNode).toHaveAttribute("aria-pressed", "true");
  });

  it("supports pointer selection and does not describe incomplete rows as agreements", () => {
    const incompleteRow = matrixRow("needs.followup", ["需要", "需要"]);
    incompleteRow.cells[1] = {
      group: GROUPS[1],
      assignments: [],
      missing: true,
    };
    incompleteRow.missing_group_keys = ["review"];
    renderGraph({ ...RESPONSE, matrix: [incompleteRow] });

    const graph = screen.getByRole("group", {
      name: /多标签组对比与溯源图/,
    });
    const focusNode = graph.querySelector('[data-node-kind="focus"]');
    expect(focusNode).toHaveAttribute("data-node-status", "neutral");

    fireEvent.click(focusNode as Element);
    const detail = screen.getByRole("complementary", {
      name: "标签节点详情",
    });
    expect(within(detail).getByText("needs.followup")).toBeInTheDocument();
    expect(within(detail).getByText("缺失")).toBeInTheDocument();
  });

  it("places larger community sets around the central target with compact node glyphs", () => {
    renderGraph({
      ...RESPONSE,
      matrix: Array.from({ length: 8 }, (_, index) =>
        matrixRow(`community.${index + 1}`, ["一致", "一致"], index + 1),
      ),
      co_occurrences: [],
    });

    const graph = screen.getByRole("group", {
      name: /多标签组对比与溯源图/,
    });
    const centralBody = graph.querySelector<SVGCircleElement>(
      ".tig-central-target__body",
    );
    expect(centralBody).not.toBeNull();
    const rootX = Number(centralBody?.getAttribute("cx"));
    const rootY = Number(centralBody?.getAttribute("cy"));
    const communityCenters = [
      ...graph.querySelectorAll<SVGEllipseElement>(
        "ellipse[data-community-field]",
      ),
    ].map((region) => ({
      x: Number(region.getAttribute("cx")),
      y: Number(region.getAttribute("cy")),
    }));

    expect(communityCenters).toHaveLength(8);
    expect(communityCenters.some((center) => center.x < rootX - 120)).toBe(
      true,
    );
    expect(communityCenters.some((center) => center.x > rootX + 120)).toBe(
      true,
    );
    expect(communityCenters.some((center) => center.y < rootY - 120)).toBe(
      true,
    );
    expect(communityCenters.some((center) => center.y > rootY + 120)).toBe(
      true,
    );
    expect(
      graph.querySelectorAll("circle[data-node-glyph]").length,
    ).toBeGreaterThan(0);
  });

  it("keeps nodes in a dense same-label community from overlapping", () => {
    renderGraph({
      ...RESPONSE,
      matrix: Array.from({ length: 6 }, (_, index) =>
        matrixRow("stage.shared", ["完成", "待改进"], index + 1),
      ),
      co_occurrences: [],
    });

    const graph = screen.getByRole("group", {
      name: /多标签组对比与溯源图/,
    });
    const boxes = [
      ...graph.querySelectorAll<SVGGElement>(
        '[data-community="stage.shared"][data-node-id]',
      ),
    ].map((node) => ({
      x: Number(node.dataset.x),
      y: Number(node.dataset.y),
      width: Number(node.dataset.nodeWidth),
      height: Number(node.dataset.nodeHeight),
    }));

    boxes.forEach((left, leftIndex) => {
      expect(Object.values(left).every(Number.isFinite)).toBe(true);
      boxes.slice(leftIndex + 1).forEach((right) => {
        const overlapsX =
          Math.abs(left.x - right.x) <
          (left.width + right.width) / 2 + 6;
        const overlapsY =
          Math.abs(left.y - right.y) <
          (left.height + right.height) / 2 + 6;
        expect(overlapsX && overlapsY).toBe(false);
      });
    });
  });

  it("enforces a deterministic node budget without emitting dangling edges", () => {
    const largeResult: AnalyzeTagInsightsResponse = {
      ...RESPONSE,
      matrix: Array.from({ length: 80 }, (_, index) =>
        matrixRow(
          `label.${index}`,
          index % 2 === 0 ? ["是", "否"] : ["是", "是"],
          index + 1,
        ),
      ),
      co_occurrences: Array.from({ length: 79 }, (_, index) => ({
        group_key: "model",
        left_label: `label.${index}`,
        right_label: `label.${index + 1}`,
        count: 1,
      })),
    };
    renderGraph(largeResult);

    const graph = screen.getByRole("group", {
      name: /多标签组对比与溯源图/,
    });
    const nodes = [...graph.querySelectorAll("[data-node-id]")];
    expect(graph).toHaveAttribute(
      "data-node-budget",
      String(TAG_INSIGHT_GRAPH_NODE_BUDGET),
    );
    expect(nodes.length).toBeLessThanOrEqual(TAG_INSIGHT_GRAPH_NODE_BUDGET);
    expect(screen.getByText(/最多展示/)).toHaveTextContent(
      String(TAG_INSIGHT_GRAPH_NODE_BUDGET),
    );

    const nodeIds = new Set(nodes.map((node) => node.getAttribute("data-node-id")));
    graph.querySelectorAll("[data-edge-kind]").forEach((edge) => {
      expect(nodeIds.has(edge.getAttribute("data-source"))).toBe(true);
      expect(nodeIds.has(edge.getAttribute("data-target"))).toBe(true);
    });
  });

  it("does not describe the matrix as complete when its rows are truncated", () => {
    renderGraph({
      ...RESPONSE,
      truncated: true,
      matrix_truncated: true,
    });

    const limitNote = screen.getByRole("note");
    expect(limitNote).toHaveTextContent(
      "标签矩阵也已达到返回上限，请缩小筛选范围后重新分析。",
    );
    expect(limitNote).not.toHaveTextContent(
      "完整返回数据仍可在标签矩阵中查看。",
    );
  });
});
