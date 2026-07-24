import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type {
  DialogueEvidenceRef,
  ReceptionWorkspaceResponse,
} from "@/types/api";
import { EvidenceAuditPanel } from "./EvidenceAuditPanel";

const EMPTY_COLLECTION = {
  total: 0,
  returned: 0,
  limit: 100,
  truncated: false,
};

function makeWorkspace(
  evidenceRefs: DialogueEvidenceRef[],
): ReceptionWorkspaceResponse {
  return {
    reception: {
      id: 42,
      tenant_id: "tenant-a",
      scenario: "gold",
      store_id: "store-1",
      agent_name: "销售小林",
      status: "ready",
      merge_mode: "logical",
      merge_confidence: 0.92,
      started_at: "2026-07-24T08:00:00Z",
      ended_at: "2026-07-24T08:05:00Z",
      duration_sec: 300,
      merged_audio_url: null,
      version: 1,
    },
    recordings: [],
    dialogue_units: [],
    transcript_items: [],
    tag_assignments: [
      {
        id: 1,
        dialogue_unit_id: 10,
        group_key: "service",
        group_version: "v1",
        label_key: "stage.greeting",
        label_value: "pass",
        confidence: 0.95,
        source: "model",
        is_manual: false,
        evidence_refs: evidenceRefs,
      },
    ],
    state_transitions: [],
    audit_events: [],
    window: {
      start_sec: 0,
      end_sec: 300,
      size_sec: 300,
      reception_duration_sec: 300,
      truncated: false,
      has_previous: false,
      has_next: false,
      previous_start_sec: null,
      next_start_sec: null,
      total_dialogue_units: 0,
      protected_dialogue_units: 0,
      dialogue_units: EMPTY_COLLECTION,
      tag_assignments: {
        ...EMPTY_COLLECTION,
        total: 1,
        returned: 1,
      },
      state_transitions: EMPTY_COLLECTION,
      transcript_items: EMPTY_COLLECTION,
      provenance_events: EMPTY_COLLECTION,
    },
  };
}

describe("EvidenceAuditPanel", () => {
  it("disables evidence without a time coordinate and preserves zero as a valid position", () => {
    const missingTimecode: DialogueEvidenceRef = {
      ref_id: "missing-timecode",
      kind: "text",
      recording_id: 101,
      start_ms: null,
      end_ms: null,
      source_start_ms: null,
      timeline_start_ms: null,
      text_excerpt: "欢迎光临",
    };
    const startsAtZero: DialogueEvidenceRef = {
      ref_id: "starts-at-zero",
      kind: "audio",
      recording_id: 101,
      start_ms: 0,
      end_ms: 1_000,
      timeline_start_ms: null,
    };
    const onSeekEvidence = vi.fn();

    render(
      <EvidenceAuditPanel
        workspace={makeWorkspace([missingTimecode, startsAtZero])}
        onSeekEvidence={onSeekEvidence}
      />,
    );

    const unavailableButton = screen.getByRole("button", {
      name: "证据 stage.greeting 缺少时间码",
    });
    expect(unavailableButton).toBeDisabled();
    expect(unavailableButton).toHaveTextContent("缺少时间码");
    fireEvent.click(unavailableButton);
    expect(onSeekEvidence).not.toHaveBeenCalled();

    const zeroButton = screen.getByRole("button", {
      name: "定位证据 stage.greeting 0.0 秒",
    });
    expect(zeroButton).toBeEnabled();
    fireEvent.click(zeroButton);
    expect(onSeekEvidence).toHaveBeenCalledWith(startsAtZero);
  });
});
