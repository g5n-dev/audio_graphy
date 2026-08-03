import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReceptionWorkspaceResponse } from "@/types/api";
import { MultiTrackTimeline } from "./MultiTrackTimeline";

const EMPTY_COLLECTION = {
  total: 0,
  returned: 0,
  limit: 100,
  truncated: false,
};

const WORKSPACE: ReceptionWorkspaceResponse = {
  reception: {
    id: 9,
    tenant_id: "tenant-a",
    scenario: "automotive",
    store_id: "store-1",
    agent_name: "顾问小林",
    status: "ready",
    merge_mode: "logical",
    merge_confidence: 0.93,
    started_at: "2026-07-25T08:00:00Z",
    ended_at: "2026-07-25T08:20:00Z",
    duration_sec: 1_200,
    merged_audio_url: null,
    playback_expires_at: null,
    version: 3,
  },
  recordings: [
    {
      id: 101,
      name: "接待录音.wav",
      sequence_no: 0,
      timeline_start_sec: 600,
      timeline_end_sec: 1_200,
      source_start_sec: 600,
      source_end_sec: 1_200,
      source_start_ms: 600_000,
      source_end_ms: 1_200_000,
      timeline_start_ms: 600_000,
      timeline_end_ms: 1_200_000,
      gap_before_ms: 0,
      time_origin_ms: 0,
      legal_source_start_ms: 600_000,
      legal_source_end_ms: 1_200_000,
      gap_before_sec: 0,
      audio_url: null,
      playback_expires_at: null,
      decision_source: "explicit",
      merge_confidence: 1,
    },
  ],
  dialogue_units: [
    {
      id: 501,
      unit_index: 4,
      version: 1,
      start_sec: 630,
      end_sec: 690,
      topic: "报价沟通",
      business_stage: "quotation",
      summary: "客户讨论落地价。",
      boundary_confidence: 0.9,
      boundary_reasons: ["topic_change"],
      edit_status: "auto",
    },
  ],
  transcript_items: [],
  tag_assignments: [
    {
      id: 701,
      dialogue_unit_id: 501,
      group_key: "sales",
      group_version: "v2",
      label_key: "objection.price",
      label_value: "high",
      confidence: 0.88,
      source: "llm",
      is_manual: false,
      evidence_refs: [
        {
          ref_id: "segment:77",
          kind: "audio",
          recording_id: 101,
          start_ms: 30_000,
          end_ms: 40_000,
          timeline_start_ms: 630_000,
          timeline_end_ms: 640_000,
        },
      ],
    },
    {
      id: 702,
      dialogue_unit_id: 501,
      group_key: "sales",
      group_version: "v2",
      label_key: "intent.test_drive",
      label_value: "true",
      confidence: 0.91,
      source: "llm",
      is_manual: false,
      evidence_refs: [
        {
          ref_id: "segment:77",
          kind: "audio",
          recording_id: 101,
          start_ms: 30_000,
          end_ms: 40_000,
          timeline_start_ms: 630_000,
          timeline_end_ms: 640_000,
        },
      ],
    },
  ],
  state_transitions: [],
  audit_events: [],
  window: {
    start_sec: 600,
    end_sec: 1_200,
    size_sec: 600,
    reception_duration_sec: 1_200,
    truncated: false,
    has_previous: true,
    has_next: false,
    previous_start_sec: 0,
    next_start_sec: null,
    total_dialogue_units: 5,
    protected_dialogue_units: 1,
    dialogue_units: { ...EMPTY_COLLECTION, total: 5, returned: 1 },
    tag_assignments: { ...EMPTY_COLLECTION, total: 2, returned: 2 },
    state_transitions: EMPTY_COLLECTION,
    transcript_items: EMPTY_COLLECTION,
    provenance_events: EMPTY_COLLECTION,
  },
};

describe("MultiTrackTimeline", () => {
  it("uses the active workspace window, zooms one synchronized canvas and selects a tag", () => {
    const onSeek = vi.fn();
    const onToggleUnit = vi.fn();
    const onSelectTag = vi.fn();
    const { container } = render(
      <MultiTrackTimeline
        workspace={WORKSPACE}
        currentTime={635}
        selectedUnitIds={new Set()}
        selectedTagId={null}
        onSeek={onSeek}
        onToggleUnit={onToggleUnit}
        onSelectTag={onSelectTag}
      />,
    );

    expect(screen.getByText("10:00")).toBeInTheDocument();
    expect(screen.getByText("20:00")).toBeInTheDocument();
    expect(
      screen.getByText("拖动或触控横向浏览，标签时间来自证据所属对话单元"),
    ).toBeInTheDocument();

    const canvas = container.querySelector<HTMLElement>(".ag-timeline__canvas");
    expect(canvas).toHaveStyle({ "--ag-timeline-lane-width": "1200px" });

    fireEvent.change(screen.getByRole("slider", { name: "时间轴缩放" }), {
      target: { value: "2" },
    });
    expect(canvas).toHaveStyle({ "--ag-timeline-lane-width": "2400px" });
    expect(screen.getByText("2×")).toBeInTheDocument();

    const priceTag = screen.getByRole("button", {
      name: /编辑标签 objection\.price: high/,
    });
    fireEvent.click(priceTag);
    expect(onSeek).toHaveBeenCalledWith(630);
    expect(onSelectTag).toHaveBeenCalledWith(WORKSPACE.tag_assignments[0]);
    expect(onToggleUnit).not.toHaveBeenCalled();

    const tagLane = screen
      .getByRole("group", { name: "标签轨道" })
      .querySelector<HTMLElement>(".ag-track__lane");
    expect(tagLane?.dataset.laneCount).toBe("2");
  });

  it("labels speaker blocks with the resolved speaker and flags weak merges", () => {
    // Segments only carry "spk_0"; without the resolved map the timeline can
    // show neither who this is nor that the attribution is provisional.
    const workspace = {
      ...WORKSPACE,
      transcript_items: [
        {
          id: 9001,
          dialogue_unit_id: 501,
          recording_id: 42,
          start_sec: 631,
          end_sec: 640,
          speaker_label: "spk_0",
          speaker_role: "unknown" as const,
          text: "这个价格还是超预算。",
        },
        {
          id: 9002,
          dialogue_unit_id: 501,
          recording_id: 42,
          start_sec: 641,
          end_sec: 650,
          speaker_label: "spk_1",
          speaker_role: "unknown" as const,
          text: "我帮您算一下金融方案。",
        },
      ],
    };
    const speakerByLabel = new Map([
      [
        "42:spk_0",
        {
          source_speaker_label: "spk_0",
          speaker_node_id: 11,
          display_name: "客户A",
          speaker_role: "customer" as const,
          ambiguity_tag: "AMBIGUOUS" as const,
          merge_confidence: 0.58,
          cosine_similarity: 0.58,
          strategy: "voiceprint",
        },
      ],
      [
        "42:spk_1",
        {
          source_speaker_label: "spk_1",
          speaker_node_id: 12,
          display_name: "坐席B",
          speaker_role: "agent" as const,
          ambiguity_tag: null,
          merge_confidence: 0.93,
          cosine_similarity: 0.93,
          strategy: "voiceprint",
        },
      ],
    ]);

    render(
      <MultiTrackTimeline
        workspace={workspace}
        currentTime={635}
        selectedUnitIds={new Set()}
        selectedTagId={null}
        onSeek={vi.fn()}
        onToggleUnit={vi.fn()}
        onSelectTag={vi.fn()}
        speakerByLabel={speakerByLabel}
      />,
    );

    expect(screen.getByText("⚠ 客户A")).toBeInTheDocument();
    expect(screen.getByText("坐席B")).toBeInTheDocument();
    expect(screen.queryByText("spk_0")).not.toBeInTheDocument();
  });

  it("keeps the raw labels when voiceprint linking has not run", () => {
    const workspace = {
      ...WORKSPACE,
      transcript_items: [
        {
          id: 9003,
          dialogue_unit_id: 501,
          recording_id: 42,
          start_sec: 631,
          end_sec: 640,
          speaker_label: "spk_0",
          speaker_role: "unknown" as const,
          text: "这个价格还是超预算。",
        },
      ],
    };

    render(
      <MultiTrackTimeline
        workspace={workspace}
        currentTime={635}
        selectedUnitIds={new Set()}
        selectedTagId={null}
        onSeek={vi.fn()}
        onToggleUnit={vi.fn()}
        onSelectTag={vi.fn()}
      />,
    );

    // Inventing an identity would be worse than showing the raw label.
    expect(screen.getByText("spk_0")).toBeInTheDocument();
  });
});
