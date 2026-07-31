import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReceptionWorkspaceResponse } from "@/types/api";
import { DialogueGraph, type DialogueGraphMode } from "./DialogueGraph";

function makeWorkspace(): ReceptionWorkspaceResponse {
  return {
    reception: {
      id: "r-1",
      tenant_id: "tenant-a",
      scenario: "gold",
      store_id: "store-8",
      agent_name: "顾问小林",
      status: "ready",
      merge_mode: "both",
      merge_confidence: 0.96,
      started_at: "2026-07-23T01:00:00Z",
      ended_at: "2026-07-23T01:02:00Z",
      duration_sec: 120,
      merged_audio_url: "/audio/r-1.wav",
      playback_expires_at: "2026-07-23T01:05:00Z",
      version: 3,
    },
    recordings: [
      {
        id: "rec-1",
        name: "迎宾录音.wav",
        sequence_no: 0,
        timeline_start_sec: 0,
        timeline_end_sec: 60,
        source_start_sec: 0,
        source_end_sec: 60,
        source_start_ms: 0,
        source_end_ms: 60_000,
        timeline_start_ms: 0,
        timeline_end_ms: 60_000,
        gap_before_ms: 0,
        time_origin_ms: 0,
        legal_source_start_ms: 0,
        legal_source_end_ms: 60_000,
        gap_before_sec: 0,
        audio_url: "/audio/rec-1.wav",
        playback_expires_at: "2026-07-23T01:05:00Z",
        decision_source: "explicit",
        merge_confidence: 1,
      },
      {
        id: "rec-2",
        name: "需求沟通.wav",
        sequence_no: 1,
        timeline_start_sec: 60,
        timeline_end_sec: 120,
        source_start_sec: 0,
        source_end_sec: 60,
        source_start_ms: 0,
        source_end_ms: 60_000,
        timeline_start_ms: 60_000,
        timeline_end_ms: 120_000,
        gap_before_ms: 0,
        time_origin_ms: 60_000,
        legal_source_start_ms: 0,
        legal_source_end_ms: 60_000,
        gap_before_sec: 0,
        audio_url: "/audio/rec-2.wav",
        playback_expires_at: "2026-07-23T01:05:00Z",
        decision_source: "auto",
        merge_confidence: 0.91,
      },
    ],
    dialogue_units: [
      {
        id: "unit-1",
        unit_index: 0,
        version: 2,
        start_sec: 0,
        end_sec: 35,
        topic: "到店欢迎",
        business_stage: "greeting",
        summary: "销售顾问向客户问候。",
        boundary_confidence: 0.94,
        boundary_reasons: ["pause", "topic_change"],
        edit_status: "manual_edited",
      },
      {
        id: "unit-2",
        unit_index: 1,
        version: 1,
        start_sec: 35,
        end_sec: 75,
        topic: "需求确认",
        business_stage: "discovery",
        summary: "询问预算和佩戴场景。",
        boundary_confidence: 0.91,
        boundary_reasons: ["topic_change"],
        edit_status: "auto",
      },
      {
        id: "unit-3",
        unit_index: 2,
        version: 3,
        start_sec: 75,
        end_sec: 120,
        topic: "方案推荐",
        business_stage: "recommendation",
        summary: "推荐足金手镯。",
        boundary_confidence: 0.89,
        boundary_reasons: ["topic_change"],
        edit_status: "locked",
      },
    ],
    transcript_items: [],
    tag_assignments: [
      {
        id: "tag-1",
        dialogue_unit_id: "unit-1",
        group_key: "service-stage",
        group_version: "v2",
        label_key: "stage.greeting",
        label_value: "pass",
        confidence: 0.93,
        source: "model",
        is_manual: false,
        evidence_refs: [],
      },
      {
        id: "tag-2",
        dialogue_unit_id: "unit-2",
        group_key: "opportunity",
        group_version: "v1",
        label_key: "intent.level",
        label_value: "high",
        confidence: 0.86,
        source: "model",
        is_manual: false,
        evidence_refs: [],
      },
      {
        id: "tag-3",
        dialogue_unit_id: "unit-3",
        group_key: "service-stage",
        group_version: "v2",
        label_key: "stage.recommendation",
        label_value: "pass",
        confidence: 0.9,
        source: "model",
        is_manual: false,
        evidence_refs: [],
      },
    ],
    state_transitions: [
      {
        id: "transition-1",
        sequence_no: 0,
        from_state: "start",
        to_state: "greeting",
        trigger: "detected_stage",
        confidence: 0.93,
        evidence_refs: [],
      },
      {
        id: "transition-2",
        sequence_no: 1,
        from_state: "greeting",
        to_state: "discovery",
        trigger: "topic_change",
        confidence: 0.89,
        evidence_refs: [],
      },
    ],
    audit_events: [
      {
        id: "audit-1",
        object_type: "tag_assignment",
        object_ref: "tag-1",
        action: "derive_tags",
        actor: "model",
        algorithm_version: "rules-v2",
        parent_refs: [
          { type: "dialogue_unit", id: "unit-1", version: 2 },
        ],
        evidence_refs: [
          {
            ref_id: "ev-1",
            kind: "audio",
            recording_id: "rec-1",
            start_ms: 12_500,
            end_ms: 14_000,
            text_excerpt: "您好，欢迎光临",
          },
        ],
        occurred_at: "2026-07-23T01:03:00Z",
        detail: {},
      },
    ],
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
      dialogue_units: {
        total: 3,
        returned: 3,
        limit: 100,
        truncated: false,
      },
      tag_assignments: {
        total: 3,
        returned: 3,
        limit: 200,
        truncated: false,
      },
      state_transitions: {
        total: 2,
        returned: 2,
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
  };
}

function nodePositions(container: HTMLElement): Record<string, string> {
  return Object.fromEntries(
    [...container.querySelectorAll<SVGGElement>("[data-node-id]")].map(
      (node) => [
        node.dataset.nodeId ?? "",
        `${node.dataset.x},${node.dataset.y}`,
      ],
    ),
  );
}

describe("DialogueGraph", () => {
  it("在关系模式按浅色社区轨道确定性布局，不受接口数组顺序影响", () => {
    const workspace = makeWorkspace();
    const { container, rerender } = render(
      <DialogueGraph workspace={workspace} mode="relation" />,
    );

    expect(container.querySelector(".ag-dialogue-graph--relation")).not.toBeNull();
    expect(screen.getByText("源录音")).toBeInTheDocument();
    expect(screen.getByText("对话单元")).toBeInTheDocument();
    expect(screen.getByText("目标标签")).toBeInTheDocument();
    expect(
      container.querySelector('[data-node-id="reception-r-1"]'),
    ).toHaveAttribute("data-community", "reception-core");
    expect(
      container.querySelector('[data-node-id="recording-rec-1"]'),
    ).toHaveAttribute("data-community", "recordings");
    expect(
      container.querySelector('[data-node-id="unit-unit-1"]'),
    ).toHaveAttribute("data-community", "dialogue-units");
    expect(
      container.querySelector('[data-node-id="tag-tag-1"]'),
    ).toHaveAttribute("data-community", "tags-stage-greeting");
    expect(
      [...container.querySelectorAll(".ag-graph-edge-label")].map(
        (label) => label.textContent,
      ),
    ).toEqual(["包含原音", "包含原音"]);

    const firstPositions = nodePositions(container);
    rerender(
      <DialogueGraph
        workspace={{
          ...workspace,
          recordings: [...workspace.recordings].reverse(),
          dialogue_units: [...workspace.dialogue_units].reverse(),
          tag_assignments: [...workspace.tag_assignments].reverse(),
        }}
        mode="relation"
      />,
    );
    expect(nodePositions(container)).toEqual(firstPositions);
  });

  it("四种模式使用各自语义明确且确定的布局", () => {
    const workspace = makeWorkspace();
    const modes: DialogueGraphMode[] = [
      "relation",
      "temporal",
      "state",
      "provenance",
    ];
    const { container, rerender } = render(
      <DialogueGraph workspace={workspace} mode="relation" />,
    );

    for (const mode of modes) {
      rerender(<DialogueGraph workspace={workspace} mode={mode} />);
      const svg = screen.getByRole("group", {
        name: new RegExp(`${mode} 对话图谱`),
      });
      expect(svg).toHaveAttribute("data-layout-mode", mode);
      expect(container.querySelectorAll("[data-node-id]").length).toBeGreaterThan(
        0,
      );
      expect(
        [...container.querySelectorAll("[data-node-id]")].every(
          (node) =>
            node.hasAttribute("data-x") &&
            node.hasAttribute("data-y") &&
            node.hasAttribute("data-kind") &&
            node.hasAttribute("data-community"),
        ),
      ).toBe(true);
    }

    rerender(<DialogueGraph workspace={workspace} mode="temporal" />);
    expect(
      Number(
        container
          .querySelector('[data-node-id="audit-audit-1"]')
          ?.getAttribute("data-x"),
      ),
    ).toBeLessThan(
      Number(
        container
          .querySelector('[data-node-id="reception-v3"]')
          ?.getAttribute("data-x"),
      ),
    );

    rerender(<DialogueGraph workspace={workspace} mode="state" />);
    expect(
      Number(
        container
          .querySelector('[data-node-id="state-start"]')
          ?.getAttribute("data-x"),
      ),
    ).toBeLessThan(
      Number(
        container
          .querySelector('[data-node-id="state-discovery"]')
          ?.getAttribute("data-x"),
      ),
    );

    rerender(<DialogueGraph workspace={workspace} mode="provenance" />);
    const sourceX = Number(
      container
        .querySelector('[data-node-id="evidence-audit-1-ev-1"]')
        ?.getAttribute("data-x"),
    );
    const eventX = Number(
      container
        .querySelector('[data-node-id="event-audit-1"]')
        ?.getAttribute("data-x"),
    );
    const objectX = Number(
      container
        .querySelector('[data-node-id="tag_assignment-tag-1"]')
        ?.getAttribute("data-x"),
    );
    expect(sourceX).toBeLessThan(eventX);
    expect(eventX).toBeLessThan(objectX);
  });

  it("四种模式共享同心社区、跨社区虚线与紧凑缩放控制", () => {
    const workspace = makeWorkspace();
    const modes: DialogueGraphMode[] = [
      "relation",
      "temporal",
      "state",
      "provenance",
    ];
    const { container, rerender } = render(
      <DialogueGraph workspace={workspace} mode="relation" />,
    );

    for (const mode of modes) {
      rerender(<DialogueGraph workspace={workspace} mode={mode} />);
      const communities = container.querySelectorAll(".ag-graph-community");
      const contours = container.querySelectorAll(
        ".ag-graph-community__contour",
      );
      expect(communities.length).toBeGreaterThan(0);
      expect(contours).toHaveLength(communities.length * 3);
      expect(
        [...container.querySelectorAll(".ag-graph-community__surface")].every(
          (surface) => surface.tagName.toLowerCase() === "ellipse",
        ),
      ).toBe(true);
      expect(screen.getByRole("toolbar", { name: "图谱缩放控制" })).toBeVisible();
    }

    rerender(<DialogueGraph workspace={workspace} mode="relation" />);
    expect(
      container.querySelector(".ag-graph-edge--cross-community"),
    ).not.toBeNull();
    expect(
      container.querySelectorAll(".ag-graph-edge--primary-bridge"),
    ).toHaveLength(2);
    expect(
      container.querySelector(".ag-graph-edge--secondary-bridge"),
    ).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "放大图谱" }));
    expect(container.querySelector(".ag-dialogue-graph")).toHaveAttribute(
      "data-zoom",
      "1.1",
    );
    expect(screen.getByText("110%")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重置图谱缩放" }));
    expect(container.querySelector(".ag-dialogue-graph")).toHaveAttribute(
      "data-zoom",
      "1.0",
    );
  });

  it("在状态图中以非颜色单一提示高亮聚合页传入的目标转移", () => {
    const { container } = render(
      <DialogueGraph
        workspace={makeWorkspace()}
        mode="state"
        highlightedTransition={{
          fromState: "greeting",
          toState: "discovery",
        }}
      />,
    );

    const graph = screen.getByRole("group", {
      name: /已高亮 greeting 到 discovery/,
    });
    const highlightedEdge = container.querySelector(
      '[data-source="state-greeting"][data-target="state-discovery"]',
    );
    const otherEdge = container.querySelector(
      '[data-source="state-start"][data-target="state-greeting"]',
    );

    expect(graph).toBeInTheDocument();
    expect(highlightedEdge).toHaveAttribute("data-highlighted", "true");
    expect(highlightedEdge).toHaveAttribute("stroke-dasharray", "none");
    expect(highlightedEdge).toHaveAttribute("stroke-width", "4");
    expect(otherEdge).toHaveAttribute("data-highlighted", "false");
    expect(
      container.querySelector(".ag-graph-edge-label--highlighted"),
    ).toHaveTextContent("当前路径");
  });

  it("即使状态节点超过预算，也保留深链指定路径的两个端点和边", () => {
    const workspace = makeWorkspace();
    workspace.state_transitions = Array.from({ length: 40 }, (_, index) => ({
      ...workspace.state_transitions[0],
      id: `transition-${index}`,
      sequence_no: index,
      from_state: `state-${index}`,
      to_state: `state-${index + 1}`,
    }));

    const { container } = render(
      <DialogueGraph
        workspace={workspace}
        mode="state"
        highlightedTransition={{
          fromState: "state-39",
          toState: "state-40",
        }}
      />,
    );

    expect(container.querySelectorAll("[data-node-id]")).toHaveLength(36);
    expect(
      container.querySelector(
        '[data-source="state-state-39"][data-target="state-state-40"]',
      ),
    ).toHaveAttribute("data-highlighted", "true");
    expect(
      container.querySelector('[data-node-id="state-state-39"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('[data-node-id="state-state-40"]'),
    ).not.toBeNull();
  });

  it("支持鼠标和键盘选择节点，并通过可选回调提供详情", () => {
    const onNodeDetails = vi.fn();
    const { container } = render(
      <DialogueGraph
        workspace={makeWorkspace()}
        mode="relation"
        onNodeDetails={onNodeDetails}
      />,
    );
    const unit = screen.getByRole("button", { name: /到店欢迎/ });
    const tag = screen.getByRole("button", { name: /intent\.level: high/ });

    expect(unit).toHaveAttribute("tabindex", "0");
    expect(unit).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(unit);
    expect(unit).toHaveAttribute("aria-pressed", "true");
    expect(unit).toHaveClass("ag-graph-node--selected");
    expect(onNodeDetails).toHaveBeenLastCalledWith(
      expect.objectContaining({
        id: "unit-unit-1",
        kind: "unit",
        community: "dialogue-units",
      }),
    );

    fireEvent.keyDown(tag, { key: "Enter" });
    expect(tag).toHaveAttribute("aria-pressed", "true");
    expect(unit).toHaveAttribute("aria-pressed", "false");
    fireEvent.keyDown(tag, { key: " " });
    expect(onNodeDetails).toHaveBeenCalledTimes(3);
    expect(
      container.querySelectorAll(".ag-graph-node--selected"),
    ).toHaveLength(1);
  });

  it("最多渲染 36 个节点，并移除裁剪后产生的悬空边", () => {
    const workspace = makeWorkspace();
    workspace.recordings = [];
    workspace.tag_assignments = [];
    workspace.dialogue_units = Array.from({ length: 45 }, (_, index) => ({
      ...workspace.dialogue_units[0],
      id: `large-unit-${index}`,
      unit_index: index,
      topic: `对话单元 ${index + 1}`,
    }));
    const { container } = render(
      <DialogueGraph workspace={workspace} mode="relation" />,
    );

    const nodeIds = new Set(
      [...container.querySelectorAll<SVGGElement>("[data-node-id]")].map(
        (node) => node.dataset.nodeId,
      ),
    );
    expect(nodeIds).toHaveLength(36);
    expect(
      [...container.querySelectorAll<SVGLineElement>(".ag-graph-edge")].every(
        (edge) =>
          nodeIds.has(edge.dataset.source) && nodeIds.has(edge.dataset.target),
      ),
    ).toBe(true);
    expect(
      container.querySelectorAll(".ag-graph-node__label-card").length,
    ).toBeLessThan(nodeIds.size);
    expect(screen.getByText(/最多显示 36 个节点/)).toBeInTheDocument();
  });

  it("标签社区超过六组时合并到可见社区且所有节点都有坐标", () => {
    const workspace = makeWorkspace();
    workspace.tag_assignments = Array.from({ length: 10 }, (_, index) => ({
      ...workspace.tag_assignments[0],
      id: `tag-many-${index}`,
      dialogue_unit_id: workspace.dialogue_units[index % 3].id,
      label_key: `custom.label-${index}`,
      label_value: `value-${index}`,
    }));

    const { container } = render(
      <DialogueGraph workspace={workspace} mode="relation" />,
    );

    const nodes = [...container.querySelectorAll("[data-node-id]")];
    expect(nodes).toHaveLength(16);
    expect(
      nodes.every(
        (node) =>
          node.hasAttribute("data-x") && node.hasAttribute("data-y"),
      ),
    ).toBe(true);
    expect(screen.getByText("其他目标标签（5 组）")).toBeInTheDocument();
  });

  it("空状态保留可感知的原因说明", () => {
    const workspace = makeWorkspace();
    workspace.state_transitions = [];
    render(<DialogueGraph workspace={workspace} mode="state" />);

    expect(screen.getByRole("status")).toHaveTextContent(
      "暂无对话状态转移数据",
    );
  });
});
