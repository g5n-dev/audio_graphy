import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  acceptReceptionProposal,
  analyzeTagInsights,
  deriveReceptionDialogueTags,
  discoverReceptionProposals,
  getReceptionTagInsights,
  getReceptionProvenance,
  getReceptionWorkspace,
  listReceptions,
  mergeDialogueUnits,
  segmentReception,
  splitDialogueUnit,
} from "./services";
import type {
  AnalyzeTagInsightsRequest,
  DeriveDialogueTagsRequest,
  ReceptionTagInsightsResponse,
  ReceptionDiscoveryRequest,
  ReceptionWorkspaceApiResponse,
} from "@/types/api";

vi.mock("./client", () => ({
  httpClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

import { httpClient } from "./client";

const mockedGet = httpClient.get as unknown as ReturnType<typeof vi.fn>;
const mockedPost = httpClient.post as unknown as ReturnType<typeof vi.fn>;

const WIRE_WORKSPACE: ReceptionWorkspaceApiResponse = {
  reception: {
    id: 9,
    tenant_id: "tenant-a",
    external_session_id: null,
    scenario: "automotive",
    store_id: "store-1",
    agent_name: "agent-1",
    customer_hash: null,
    status: "ready",
    merge_mode: "logical",
    merge_confidence: 0.95,
    started_at: "2026-07-23T01:00:00Z",
    ended_at: "2026-07-23T01:01:00Z",
    audio_url: "/api/v1/receptions/9/audio?playback_grant=signed",
    version: 2,
    created_at: "2026-07-23T01:00:00Z",
    updated_at: "2026-07-23T01:01:00Z",
  },
  recordings: [
    {
      id: 88,
      recording_id: 101,
      sequence_no: 0,
      timeline_start_sec: 0,
      timeline_end_sec: 12,
      source_start_sec: 0,
      source_end_sec: 12,
      gap_before_sec: 0,
      decision_source: "explicit",
      merge_confidence: 1,
      merge_reasons: {},
      source_recorded_at: "2026-07-23T01:00:00Z",
      audio_url:
        "/api/v1/receptions/9/recordings/101/audio?playback_grant=signed",
    },
  ],
  dialogue_units: [
    {
      id: 501,
      source_recording_id: 101,
      unit_index: 0,
      version: 1,
      start_sec: 0,
      end_sec: 12,
      topic: "车型需求",
      business_stage: "needs",
      summary: "客户说明预算。",
      boundary_confidence: 0.9,
      boundary_reasons: ["semantic_shift"],
      segment_refs: [
        {
          segment_id: 77,
          recording_id: 101,
          start_sec: 1,
          end_sec: 5,
        },
      ],
      speaker_refs: ["销售", "客户"],
      edit_status: "auto",
      tag_assignments: [
        {
          id: 701,
          reception_id: 9,
          dialogue_unit_id: 501,
          group_key: "stage",
          group_version: "v1",
          label_key: "needs",
          label_value: "pass",
          confidence: 0.9,
          source: "llm",
          priority: 10,
          evidence_refs: [
            {
              recording_id: 101,
              source_start_sec: 5,
              source_end_sec: 8,
              timeline_start_sec: 7,
              timeline_end_sec: 10,
              coordinate_space: "both",
            },
          ],
          model_run_id: "run-1",
          is_current: true,
          assigned_at: "2026-07-23T01:00:00Z",
        },
      ],
    },
  ],
  state_transitions: [],
  tag_assignments: [
    {
      id: 701,
      reception_id: 9,
      dialogue_unit_id: 501,
      group_key: "stage",
      group_version: "v1",
      label_key: "needs",
      label_value: "pass",
      confidence: 0.9,
      source: "llm",
      priority: 10,
      evidence_refs: [
        {
          recording_id: 101,
          source_start_sec: 5,
          source_end_sec: 8,
          timeline_start_sec: 7,
          timeline_end_sec: 10,
          coordinate_space: "both",
        },
      ],
      model_run_id: "run-1",
      is_current: true,
      assigned_at: "2026-07-23T01:00:00Z",
    },
  ],
  transcript_items: [
    {
      segment_id: 77,
      recording_id: 101,
      segment_index: 0,
      source_start_sec: 1,
      source_end_sec: 5,
      timeline_start_sec: 1,
      timeline_end_sec: 5,
      speaker: "销售",
      text: "请问您更关注哪类车型？",
      vad_confidence: 0.93,
    },
  ],
  provenance_events: [
    {
      id: 801,
      reception_id: 9,
      object_type: "dialogue_unit",
      object_ref: "501",
      event_type: "segmented",
      actor: "dialogue-hybrid-v1",
      algorithm_version: "dialogue-hybrid-v1",
      parent_refs: [{ type: "segment", id: 77 }],
      evidence_refs: [
        {
          ref_id: "segment-77",
          recording_id: 101,
          timeline_start_sec: 1,
          timeline_end_sec: 5,
          coordinate_space: "timeline",
        },
      ],
      payload: { reason: "semantic_shift" },
      occurred_at: "2026-07-23T01:00:30Z",
    },
  ],
  window: {
    start_sec: 0,
    end_sec: 12,
    size_sec: 600,
    reception_duration_sec: 12,
    truncated: false,
    has_previous: false,
    has_next: false,
    previous_start_sec: null,
    next_start_sec: null,
    total_dialogue_units: 1,
    protected_dialogue_units: 1,
    dialogue_units: {
      total: 1,
      returned: 1,
      limit: 100,
      truncated: false,
    },
    tag_assignments: {
      total: 1,
      returned: 1,
      limit: 200,
      truncated: false,
    },
    state_transitions: {
      total: 0,
      returned: 0,
      limit: 100,
      truncated: false,
    },
    transcript_items: {
      total: 1,
      returned: 1,
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

describe("reception and tag insight API services", () => {
  beforeEach(() => {
    mockedGet.mockReset();
    mockedPost.mockReset();
  });

  it("normalizes the complete workspace contract without deriving server paths", async () => {
    mockedGet.mockResolvedValueOnce({ data: WIRE_WORKSPACE });

    const result = await getReceptionWorkspace(9);

    expect(mockedGet).toHaveBeenCalledWith("/receptions/9/workspace", {
      params: { window_start_sec: 0, window_size_sec: 600 },
    });
    expect(result.reception.duration_sec).toBe(12);
    expect(result.reception.merged_audio_url).toContain(
      "playback_grant=signed",
    );
    expect(result.recordings[0]).toMatchObject({
      id: 101,
      name: "录音 #101",
      audio_url:
        "/api/v1/receptions/9/recordings/101/audio?playback_grant=signed",
    });
    expect(result.transcript_items[0]).toMatchObject({
      id: 77,
      dialogue_unit_id: 501,
      recording_id: 101,
      start_sec: 1,
      end_sec: 5,
      speaker_label: "销售",
      speaker_role: "agent",
      text: "请问您更关注哪类车型？",
    });
    expect(result.dialogue_units[0].speaker_refs).toEqual(["销售", "客户"]);
    expect(result.tag_assignments[0].evidence_refs[0]).toMatchObject({
      recording_id: 101,
      start_ms: 5_000,
      end_ms: 8_000,
      source_start_ms: 5_000,
      timeline_start_ms: 7_000,
      coordinate_space: "both",
    });
    expect(result.audit_events[0]).toMatchObject({
      id: 801,
      object_type: "dialogue_unit",
      object_ref: "501",
      action: "segmented",
      actor: "dialogue-hybrid-v1",
      parent_refs: [{ type: "segment", id: 77 }],
      detail: { reason: "semantic_shift" },
    });
    expect(result.audit_events[0].evidence_refs?.[0]).toMatchObject({
      ref_id: "segment-77",
      timeline_start_ms: 1_000,
      timeline_end_ms: 5_000,
      coordinate_space: "timeline",
    });
    expect(result.window).toMatchObject({
      start_sec: 0,
      end_sec: 12,
      truncated: false,
      protected_dialogue_units: 1,
    });
  });

  it("requests an explicit bounded workspace time window", async () => {
    mockedGet.mockResolvedValueOnce({
      data: {
        ...WIRE_WORKSPACE,
        dialogue_units: [],
        tag_assignments: [],
        transcript_items: [],
        provenance_events: [],
        window: {
          ...WIRE_WORKSPACE.window,
          start_sec: 600,
          end_sec: 612,
          has_previous: true,
          previous_start_sec: 0,
          dialogue_units: {
            ...WIRE_WORKSPACE.window.dialogue_units,
            returned: 0,
          },
          tag_assignments: {
            ...WIRE_WORKSPACE.window.tag_assignments,
            returned: 0,
          },
          state_transitions: {
            ...WIRE_WORKSPACE.window.state_transitions,
          },
          transcript_items: {
            ...WIRE_WORKSPACE.window.transcript_items,
            returned: 0,
          },
          provenance_events: {
            ...WIRE_WORKSPACE.window.provenance_events,
            returned: 0,
          },
        },
      },
    });

    const result = await getReceptionWorkspace(9, {
      window_start_sec: 600,
      window_size_sec: 600,
    });

    expect(mockedGet).toHaveBeenCalledWith("/receptions/9/workspace", {
      params: { window_start_sec: 600, window_size_sec: 600 },
    });
    expect(result.window.start_sec).toBe(600);
  });

  it("uses the exact optimistic-locking split and adjacent-merge contracts", async () => {
    mockedPost.mockResolvedValue({ data: { reception_id: 9 } });
    const splitBody = {
      split_at_sec: 6,
      expected_reception_version: 2,
      expected_unit_version: 1,
      reason: "人工复核边界",
    };
    await splitDialogueUnit(9, 501, splitBody);
    expect(mockedPost).toHaveBeenNthCalledWith(
      1,
      "/receptions/9/dialogue-units/501/split",
      splitBody,
    );

    const mergeBody = {
      other_unit_id: 502,
      expected_reception_version: 3,
      expected_unit_version: 2,
      expected_other_unit_version: 1,
      reason: "语义连续",
    };
    await mergeDialogueUnits(9, 501, mergeBody);
    expect(mockedPost).toHaveBeenNthCalledWith(
      2,
      "/receptions/9/dialogue-units/501/merge",
      mergeBody,
    );
  });

  it("submits the explicit auto-segmentation replacement contract", async () => {
    mockedPost.mockResolvedValueOnce({
      data: {
        reception_id: 9,
        reception_version: 3,
        dialogue_units: [],
      },
    });
    const body = {
      expected_version: 2,
      replace_auto: true,
      algorithm_version: "dialogue-hybrid-v1",
    };

    await segmentReception(9, body);

    expect(mockedPost).toHaveBeenCalledWith("/receptions/9/segment", body);
  });

  it("loads the reception provenance chain through the dedicated endpoint", async () => {
    mockedGet.mockResolvedValueOnce({
      data: {
        object_type: "reception",
        object_ref: "9",
        items: [
          {
            id: 1,
            reception_id: 9,
            object_type: "reception",
            object_ref: "9",
            event_type: "created",
            actor: "inspector",
            algorithm_version: "manual-v1",
            parent_refs: [{ type: "recording", id: 101 }],
            evidence_refs: [
              {
                recording_id: 101,
                timeline_start_sec: 2,
                timeline_end_sec: 4,
                coordinate_space: "reception_timeline",
              },
            ],
            payload: { source: "manual" },
            occurred_at: "2026-07-23T01:00:00Z",
          },
        ],
      },
    });

    const events = await getReceptionProvenance(9);

    expect(mockedGet).toHaveBeenCalledWith("/provenance/reception/9");
    expect(events[0]).toMatchObject({
      object_type: "reception",
      object_ref: "9",
      action: "created",
      actor: "inspector",
      algorithm_version: "manual-v1",
      parent_refs: [{ type: "recording", id: 101 }],
      evidence_refs: [
        expect.objectContaining({
          recording_id: 101,
          timeline_start_ms: 2_000,
          timeline_end_ms: 4_000,
        }),
      ],
      detail: { source: "manual" },
    });
  });

  it("loads the paginated reception work queue with server filters", async () => {
    mockedGet.mockResolvedValueOnce({
      data: {
        items: [WIRE_WORKSPACE.reception],
        total: 1,
        page: 2,
        page_size: 20,
      },
    });

    const result = await listReceptions({
      page: 2,
      page_size: 20,
      store_id: "store-1",
      status: "needs_review",
    });

    expect(mockedGet).toHaveBeenCalledWith("/receptions", {
      params: {
        page: 2,
        page_size: 20,
        store_id: "store-1",
        status: "needs_review",
      },
    });
    expect(result.items[0].id).toBe(9);
    expect(result.total).toBe(1);
  });

  it("submits the bounded discovery window without deriving candidates locally", async () => {
    const body: ReceptionDiscoveryRequest = {
      scenario: "gold",
      store_id: "store-1",
      recorded_from: "2026-07-22T00:00:00.000Z",
      recorded_to: "2026-07-23T00:00:00.000Z",
      short_recording_max_sec: 300,
      limit: 200,
    };
    mockedPost.mockResolvedValueOnce({
      data: { items: [], total: 0, scanned_recordings: 0, truncated: false },
    });

    await discoverReceptionProposals(body);

    expect(mockedPost).toHaveBeenCalledWith(
      "/receptions/proposals/discover",
      body,
    );
  });

  it("accepts only the server-owned proposal identity and returns the new reception", async () => {
    const body = {
      scenario: "automotive" as const,
      recording_ids: [101, 102],
      merge_mode: "logical" as const,
    };
    mockedPost.mockResolvedValueOnce({
      data: {
        ...WIRE_WORKSPACE.reception,
        recordings: WIRE_WORKSPACE.recordings,
      },
    });

    const result = await acceptReceptionProposal(body);

    expect(mockedPost).toHaveBeenCalledWith(
      "/receptions/proposals/accept",
      body,
    );
    if ("candidate_type" in result) {
      throw new Error("expected merge-group reception response");
    }
    expect(result.id).toBe(9);
  });

  it("submits multi-group snapshots to the existing analysis endpoint", async () => {
    const body: AnalyzeTagInsightsRequest = {
      tenant_id: "tenant-a",
      merge_strategy: "union",
      groups: [
        {
          group_key: "model",
          version: "v1",
          source: "llm",
          priority: 1,
        },
      ],
      assignments: [
        {
          group_key: "model",
          target_id: "reception-9",
          window: { start_ms: 0, end_ms: 1_000 },
          label_key: "stage",
          value: "greeting",
          confidence: 0.8,
          evidence_refs: [],
          is_manual: false,
          occurred_at: null,
          store_id: null,
          agent_id: null,
        },
      ],
      trend_granularity: "day",
      top_n_co_occurrences: 20,
    };
    mockedPost.mockResolvedValueOnce({ data: { tenant_id: "tenant-a" } });

    await analyzeTagInsights(body);

    expect(mockedPost).toHaveBeenCalledWith("/tag-insights/analyze", body);
  });

  it("derives versioned target dialogue tags through the persisted reception endpoint", async () => {
    const body: DeriveDialogueTagsRequest = {
      group_key: "reception-rules",
      group_version: "rules-v2",
      target_labels: [
        "stage",
        "intent",
        "objection",
        "next_step",
        "compliance_risk",
      ],
      priority: 0,
    };
    mockedPost.mockResolvedValueOnce({
      data: {
        reception_id: 9,
        group_key: body.group_key,
        group_version: body.group_version,
        requested_labels: body.target_labels,
        assignment_count: 1,
        superseded_count: 0,
        no_op: false,
        assignments: [],
        missing: [],
      },
    });

    await deriveReceptionDialogueTags(9, body);

    expect(mockedPost).toHaveBeenCalledWith(
      "/receptions/9/dialogue-tags/derive",
      body,
    );
  });

  it("loads persisted insights with repeated multi-value filters and bounded pagination", async () => {
    const response: ReceptionTagInsightsResponse = {
      tenant_id: "tenant-a",
      page: 2,
      page_size: 20,
      total_receptions: 31,
      returned_reception_ids: [9],
      total_assignments: 2,
      assignment_count: 2,
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
      selected_group_ids: ["reception-rules@rules-v1"],
      merge_strategy: "manual_wins",
      trend_granularity: "week",
      insights: null,
      evidence_summary: [],
      generated_at: "2026-07-23T02:00:00Z",
    };
    mockedGet.mockResolvedValueOnce({ data: response });

    const result = await getReceptionTagInsights({
      store_id: ["store-1", "store-2"],
      agent_name: ["顾问甲"],
      scenario: ["gold", "automotive"],
      started_from: "2026-07-01T00:00:00Z",
      started_to: "2026-08-01T00:00:00Z",
      reception_id: [9, 10],
      group_key: ["reception-rules", "review"],
      page: 2,
      page_size: 20,
      assignment_limit: 1_000,
      matrix_limit: 64,
      difference_limit: 32,
      evidence_summary_limit: 128,
      merge_strategy: "manual_wins",
      trend_granularity: "week",
      top_n_co_occurrences: 50,
    });

    const [url] = mockedGet.mock.calls[0] as [string];
    const parsed = new URL(url, "https://audio-graphy.local");
    expect(parsed.pathname).toBe("/reception-tag-insights");
    expect(parsed.searchParams.getAll("store_id")).toEqual([
      "store-1",
      "store-2",
    ]);
    expect(parsed.searchParams.getAll("scenario")).toEqual([
      "gold",
      "automotive",
    ]);
    expect(parsed.searchParams.getAll("reception_id")).toEqual(["9", "10"]);
    expect(parsed.searchParams.getAll("group_key")).toEqual([
      "reception-rules",
      "review",
    ]);
    expect(parsed.searchParams.get("page")).toBe("2");
    expect(parsed.searchParams.get("matrix_limit")).toBe("64");
    expect(parsed.searchParams.get("difference_limit")).toBe("32");
    expect(parsed.searchParams.get("evidence_summary_limit")).toBe("128");
    expect(result).toEqual(response);
  });

  it("serializes exact historical group versions as repeated group_id filters", async () => {
    mockedGet.mockResolvedValueOnce({
      data: {
        tenant_id: "tenant-a",
        page: 1,
        page_size: 20,
        total_receptions: 1,
        returned_reception_ids: [9],
        total_assignments: 2,
        assignment_count: 2,
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
        selection_mode: "exact_versions",
        selected_group_ids: ["review@v1", "review@v2"],
        merge_strategy: "union",
        trend_granularity: "day",
        insights: null,
        evidence_summary: [],
        generated_at: "2026-07-23T02:00:00Z",
      } satisfies ReceptionTagInsightsResponse,
    });

    await getReceptionTagInsights({
      group_id: ["review@v1", "review@v2"],
      merge_strategy: "union",
    });

    const [url] = mockedGet.mock.calls[0] as [string];
    const parsed = new URL(url, "https://audio-graphy.local");
    expect(parsed.searchParams.getAll("group_id")).toEqual([
      "review@v1",
      "review@v2",
    ]);
    expect(parsed.searchParams.has("group_key")).toBe(false);
  });
});
