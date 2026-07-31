import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ReceptionWorkspacePage from "./index";
import type { ReceptionWorkspaceResponse } from "@/types/api";

vi.mock("@/api/services", () => ({
  cancelReceptionAudioOperation: vi.fn(),
  createTagJob: vi.fn(),
  createTagReviewBatch: vi.fn(),
  createReceptionAudioOperation: vi.fn(),
  createReceptionAudioPlan: vi.fn(),
  decideTagReview: vi.fn(),
  deriveReceptionDialogueTags: vi.fn(),
  getReceptionAutomation: vi.fn(),
  getReceptionAudioOperation: vi.fn(),
  getReceptionWorkspace: vi.fn(),
  getTagFactLineage: vi.fn(),
  listTagSchemas: vi.fn(),
  listTaggerVersions: vi.fn(),
  mergeReceptionRecordings: vi.fn(),
  segmentReception: vi.fn(),
  splitDialogueUnit: vi.fn(),
  mergeDialogueUnits: vi.fn(),
  runReceptionAutomation: vi.fn(),
}));

import {
  cancelReceptionAudioOperation,
  createTagJob,
  createTagReviewBatch,
  createReceptionAudioOperation,
  createReceptionAudioPlan,
  decideTagReview,
  deriveReceptionDialogueTags,
  getReceptionAutomation,
  getReceptionAudioOperation,
  getReceptionWorkspace,
  getTagFactLineage,
  listTagSchemas,
  listTaggerVersions,
  mergeReceptionRecordings,
  mergeDialogueUnits,
  segmentReception,
  splitDialogueUnit,
  runReceptionAutomation,
} from "@/api/services";
import { useAuthStore } from "@/stores/auth";

const mockedGetWorkspace = getReceptionWorkspace as unknown as ReturnType<
  typeof vi.fn
>;
const mockedGetLineage =
  getTagFactLineage as unknown as ReturnType<typeof vi.fn>;
const mockedCreateTagJob = createTagJob as unknown as ReturnType<typeof vi.fn>;
const mockedCreateReview =
  createTagReviewBatch as unknown as ReturnType<typeof vi.fn>;
const mockedDecideReview =
  decideTagReview as unknown as ReturnType<typeof vi.fn>;
const mockedListSchemas = listTagSchemas as unknown as ReturnType<typeof vi.fn>;
const mockedListTaggers =
  listTaggerVersions as unknown as ReturnType<typeof vi.fn>;
const mockedDeriveTags = deriveReceptionDialogueTags as unknown as ReturnType<
  typeof vi.fn
>;
const mockedMergeRecordings = mergeReceptionRecordings as unknown as ReturnType<
  typeof vi.fn
>;
const mockedSegmentReception = segmentReception as unknown as ReturnType<
  typeof vi.fn
>;
const mockedGetAutomation =
  getReceptionAutomation as unknown as ReturnType<typeof vi.fn>;
const mockedCreateAudioPlan =
  createReceptionAudioPlan as unknown as ReturnType<typeof vi.fn>;
const mockedCreateAudioOperation =
  createReceptionAudioOperation as unknown as ReturnType<typeof vi.fn>;
const mockedGetAudioOperation =
  getReceptionAudioOperation as unknown as ReturnType<typeof vi.fn>;
const mockedCancelAudioOperation =
  cancelReceptionAudioOperation as unknown as ReturnType<typeof vi.fn>;
const mockedRunAutomation =
  runReceptionAutomation as unknown as ReturnType<typeof vi.fn>;
const mockedSplitUnit = splitDialogueUnit as unknown as ReturnType<typeof vi.fn>;
const mockedMergeUnits = mergeDialogueUnits as unknown as ReturnType<typeof vi.fn>;

const WORKSPACE: ReceptionWorkspaceResponse = {
  reception: {
    id: "reception-1",
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
    merged_audio_url: "/audio/reception-1.wav",
    playback_expires_at: "2026-07-23T01:05:00Z",
    version: 3,
  },
  recordings: [
    {
      id: "recording-1",
      mapping_id: "mapping-1",
      recording_id: "recording-1",
      name: "片段一.wav",
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
      audio_url: "/audio/recording-1.wav",
      playback_expires_at: "2026-07-23T01:05:00Z",
      decision_source: "explicit",
      merge_confidence: 1,
    },
    {
      id: "recording-2",
      mapping_id: "mapping-2",
      recording_id: "recording-2",
      name: "片段二.wav",
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
      audio_url: "/audio/recording-2.wav",
      playback_expires_at: "2026-07-23T01:05:00Z",
      decision_source: "auto",
      merge_confidence: 0.91,
    },
  ],
  dialogue_units: [
    {
      id: "unit-1",
      unit_index: 0,
      version: 1,
      start_sec: 0,
      end_sec: 35,
      topic: "到店欢迎",
      business_stage: "greeting",
      summary: "销售顾问向客户问候。",
      boundary_confidence: 0.94,
      boundary_reasons: ["pause", "topic_change"],
      edit_status: "auto",
    },
  ],
  transcript_items: [
    {
      id: "segment-1",
      dialogue_unit_id: "unit-1",
      recording_id: "recording-1",
      start_sec: 10,
      end_sec: 14,
      speaker_label: "销售",
      speaker_role: "agent",
      text: "您好，欢迎光临。",
    },
  ],
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
      model_run_id: "fact:701",
      evidence_refs: [
        {
          ref_id: "ev-1",
          kind: "audio",
          recording_id: "recording-1",
          start_ms: 12_500,
          end_ms: 14_000,
        },
      ],
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
  ],
  audit_events: [],
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
      total: 1,
      returned: 1,
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
      total: 0,
      returned: 0,
      limit: 100,
      truncated: false,
    },
  },
  capabilities: {
    can_manage_audio: true,
    can_run_segmentation: true,
    can_edit_dialogue: true,
    can_edit_tags: true,
    supports_audio_plans: false,
    supports_audio_operations: false,
    can_cancel_audio_operation: false,
  },
};

function renderWorkspace(
  initialEntry = "/receptions/reception-1/workspace",
): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route
            path="/receptions/:id/workspace"
            element={<ReceptionWorkspacePage />}
          />
          <Route
            path="/receptions/:id/graph"
            element={<div>已进入接待关系图谱</div>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ReceptionWorkspacePage", () => {
  beforeEach(() => {
    useAuthStore.setState({
      token: "test-token",
      refreshToken: "test-refresh",
      user: {
        id: 2,
        name: "质检员",
        email: "inspector@example.com",
        role: "inspector",
        tenant_id: "tenant-a",
      },
      isAuthenticated: true,
    });
    mockedCreateReview.mockReset();
    mockedDecideReview.mockReset();
    mockedDeriveTags.mockReset();
    mockedCreateTagJob.mockReset();
    mockedListSchemas.mockReset();
    mockedListTaggers.mockReset();
    mockedGetWorkspace.mockReset();
    mockedGetLineage.mockReset();
    mockedGetAutomation.mockReset();
    mockedCreateAudioPlan.mockReset();
    mockedCreateAudioOperation.mockReset();
    mockedGetAudioOperation.mockReset();
    mockedCancelAudioOperation.mockReset();
    mockedMergeRecordings.mockReset();
    mockedSplitUnit.mockReset();
    mockedMergeUnits.mockReset();
    mockedSegmentReception.mockReset();
    mockedRunAutomation.mockReset();
    mockedGetWorkspace.mockResolvedValue(WORKSPACE);
    mockedGetLineage.mockResolvedValue({
      fact: {
        id: 701,
        source: "llm",
        tag_key: "stage.greeting",
        tag_value: "pass",
        input_hash: "sha256:workspace",
        evidence_refs: [],
      },
      is_current: true,
      schema_version: null,
      tagger_version: null,
      model_version: null,
      extraction_run: null,
      job: null,
      deployment: null,
    });
    mockedListSchemas.mockResolvedValue({
      items: [
        {
          id: 1,
          key: "sales-dialogue",
          name: "销售接待标签",
          status: "published",
          active_version_id: 11,
          versions: [
            {
              id: 11,
              schema_id: 1,
              version: "2.1.0",
              status: "published",
              definitions: [
                {
                  key: "promise.follow_up",
                  name: "服务跟进承诺",
                  category: "promise",
                  value_type: "boolean",
                  allowed_values: [],
                  subject_types: ["reception"],
                  scenarios: ["gold", "automotive"],
                  evidence_required: true,
                  critical: false,
                  required: false,
                  threshold: 0.7,
                },
                {
                  key: "intent.purchase",
                  name: "客户购买意向",
                  category: "intent",
                  value_type: "enum",
                  allowed_values: ["low", "high"],
                  subject_types: ["dialogue_unit"],
                  scenarios: ["gold", "automotive"],
                  evidence_required: true,
                  critical: true,
                  required: true,
                  threshold: 0.75,
                },
              ],
            },
          ],
        },
      ],
      total: 1,
    });
    mockedListTaggers.mockResolvedValue({
      items: [
        {
          id: 42,
          schema_version_id: 11,
          version: "tagger-2.1",
          status: "qualified",
          engine: "hybrid",
          model_version: "model-a",
          thresholds: {},
        },
      ],
      total: 1,
    });
    mockedCreateTagJob.mockResolvedValue({
      id: 88,
      job_type: "recompute",
      status: "queued",
      scope: {},
      tagger_version_id: 42,
    });
    mockedCreateReview.mockResolvedValue({
      batch_id: "workspace-correction",
      created_count: 1,
      items: [{ id: 501 }],
    });
    mockedDecideReview.mockResolvedValue({
      task: { id: 501, status: "resolved" },
      decision: { id: 601, resulting_fact_id: 701 },
      fact: {
        id: 701,
        source: "manual",
        tag_key: "stage.greeting",
        tag_value: "fail",
      },
    });
    mockedMergeRecordings.mockResolvedValue(WORKSPACE);
    mockedSplitUnit.mockResolvedValue({
      reception_id: "reception-1",
      reception_version: 4,
      dialogue_units: [],
    });
    mockedMergeUnits.mockResolvedValue({
      reception_id: "reception-1",
      reception_version: 4,
      dialogue_units: [],
    });
    mockedCreateAudioPlan.mockResolvedValue({
      plan_token: "signed-plan",
      timeline_revision: 4,
      total_duration_ms: 120_000,
      physical_eligible: true,
      warnings: [],
      sources: [],
    });
    mockedCreateAudioOperation.mockResolvedValue({
      id: "audio-op-1",
      reception_id: "reception-1",
      status: "queued",
      mode: "both",
      progress: 0,
      error: null,
      created_at: "2026-07-23T01:00:00Z",
      updated_at: "2026-07-23T01:00:00Z",
    });
    mockedGetAudioOperation.mockResolvedValue({
      id: "audio-op-1",
      reception_id: "reception-1",
      status: "succeeded",
      mode: "both",
      progress: 1,
      error: null,
      created_at: "2026-07-23T01:00:00Z",
      updated_at: "2026-07-23T01:00:01Z",
    });
    mockedSegmentReception.mockResolvedValue({
      reception_id: "reception-1",
      reception_version: 4,
      dialogue_units: [],
    });
    mockedGetAutomation.mockResolvedValue({
      id: 5,
      reception_id: 1,
      status: "ready",
      stage: "ready",
      attempt_count: 1,
      checkpoints: {},
      segmentation_algorithm: "dialogue-hybrid-v1",
      tag_group_key: "reception-rules",
      tag_group_version: "rules-v1",
      target_labels: [
        "stage",
        "intent",
        "objection",
        "next_step",
        "compliance_risk",
      ],
      tag_priority: 0,
      last_error_code: null,
      last_error_message: null,
      created_at: "2026-07-23T01:00:00Z",
      updated_at: "2026-07-23T01:01:00Z",
      finished_at: "2026-07-23T01:01:00Z",
    });
    mockedRunAutomation.mockResolvedValue({
      id: 5,
      reception_id: 1,
      status: "ready",
      stage: "ready",
      attempt_count: 2,
      checkpoints: {},
      segmentation_algorithm: "dialogue-hybrid-v1",
      tag_group_key: "reception-rules",
      tag_group_version: "rules-v1",
      target_labels: [
        "stage",
        "intent",
        "objection",
        "next_step",
        "compliance_risk",
      ],
      tag_priority: 0,
      last_error_code: null,
      last_error_message: null,
      created_at: "2026-07-23T01:00:00Z",
      updated_at: "2026-07-23T01:02:00Z",
      finished_at: "2026-07-23T01:02:00Z",
    });
    mockedDeriveTags.mockResolvedValue({
      reception_id: 1,
      group_key: "reception-rules",
      group_version: "rules-v1",
      requested_labels: [
        "stage",
        "intent",
        "objection",
        "next_step",
        "compliance_risk",
      ],
      assignment_count: 4,
      superseded_count: 1,
      no_op: false,
      assignments: [
        {
          id: 901,
          reception_id: 1,
          dialogue_unit_id: 1,
          group_key: "reception-rules",
          group_version: "rules-v1",
          label_key: "intent",
          label_value: "purchase",
          confidence: 0.92,
          source: "rule",
          priority: 0,
          evidence_refs: [],
          model_run_id: null,
          is_current: true,
          assigned_at: "2026-07-23T01:00:00Z",
        },
      ],
      missing: [
        {
          dialogue_unit_id: 1,
          unit_index: 0,
          label_key: "objection",
          reason: "no_rule_match",
        },
      ],
    });
  });

  it("renders the three evidence-workbench regions", async () => {
    renderWorkspace();

    expect(await screen.findByText("接待调听工作台")).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "接待与短录音队列" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "多轨时间轴与转写" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "证据标签与审计" }),
    ).toBeInTheDocument();
    expect(mockedGetWorkspace).toHaveBeenCalledWith("reception-1", {
      window_start_sec: 0,
      window_size_sec: 600,
    });
  });

  it("edits a selected timeline tag with optimistic locking and retained evidence", async () => {
    const user = userEvent.setup();
    mockedGetWorkspace.mockResolvedValueOnce({
      ...WORKSPACE,
      reception: { ...WORKSPACE.reception, id: 101 },
      dialogue_units: [
        { ...WORKSPACE.dialogue_units[0], id: 77 },
      ],
      transcript_items: [
        { ...WORKSPACE.transcript_items[0], dialogue_unit_id: 77 },
      ],
      tag_assignments: [
        {
          ...WORKSPACE.tag_assignments[0],
          id: 701,
          dialogue_unit_id: 77,
        },
      ],
    });
    mockedListSchemas.mockResolvedValueOnce({
      items: [
        {
          id: 1,
          key: "sales-dialogue",
          name: "销售接待标签",
          status: "published",
          active_version_id: 11,
          versions: [
            {
              id: 11,
              schema_id: 1,
              version: "2.1.0",
              status: "published",
              definitions: [
                {
                  key: "stage.greeting",
                  name: "迎宾阶段完成度",
                  category: "stage",
                  value_type: "enum",
                  allowed_values: ["pass", "fail"],
                  subject_types: ["dialogue_unit"],
                  scenarios: ["gold"],
                  evidence_required: true,
                  critical: true,
                  required: true,
                  threshold: 0.75,
                },
              ],
            },
          ],
        },
      ],
      total: 1,
    });
    renderWorkspace();

    await user.click(
      await screen.findByRole("button", {
        name: /编辑标签 stage\.greeting: pass/,
      }),
    );
    expect(
      screen.getByRole("heading", { name: "编辑标签 · 迎宾阶段完成度" }),
    ).toBeInTheDocument();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "标签值" }),
      "fail",
    );
    await user.type(
      screen.getByRole("textbox", { name: "标签编辑原因" }),
      "复听后确认未完成标准迎宾",
    );
    await user.click(screen.getByRole("button", { name: "保存人工更正" }));

    await waitFor(() => {
      expect(mockedCreateReview).toHaveBeenCalledWith({
        reason: "critical",
        subjects: [
          expect.objectContaining({
            subject_type: "dialogue_unit",
            subject_id: 77,
            reception_id: 101,
            tag_key: "stage.greeting",
            proposed_value: "pass",
            proposed_fact_id: 701,
            schema_version_id: 11,
          }),
        ],
      });
      expect(
        mockedCreateReview.mock.calls.at(-1)?.[0]?.subjects?.[0],
      ).not.toHaveProperty("tagger_version_id");
      expect(mockedDecideReview).toHaveBeenCalledWith(
        501,
        expect.objectContaining({
          action: "correct",
          corrected_value: "fail",
          reason_code: "manual_workspace_correction",
          note: "复听后确认未完成标准迎宾",
          evidence_refs: [
            expect.objectContaining({
              ref_id: "ev-1",
              start_sec: 12.5,
              end_sec: 14,
            }),
          ],
        }),
      );
    });
    expect(
      await screen.findByText("标签人工更正已写入治理事实，时间轴、证据、图谱与洞察正在同步。"),
    ).toBeInTheDocument();
  });

  it("opens canonical fact lineage and keeps a retry state on failure", async () => {
    mockedGetLineage.mockRejectedValueOnce(new Error("溯源服务不可用"));
    const user = userEvent.setup();
    renderWorkspace();

    await user.click(
      await screen.findByRole("button", {
        name: /编辑标签 stage\.greeting: pass/,
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "查看事实 #701 溯源" }),
    );

    await waitFor(() => {
      expect(mockedGetLineage).toHaveBeenCalledWith(701);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "溯源服务不可用",
    );
    expect(
      screen.getByRole("button", { name: "重新加载" }),
    ).toBeInTheDocument();
  });

  it("switches between reception detail tabs without losing the reception id", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    expect(await screen.findByText("接待调听工作台")).toBeInTheDocument();
    const detailTabs = screen.getByRole("navigation", {
      name: "接待详情视图",
    });
    const listeningTab = within(detailTabs).getByRole("link", {
      name: "调听与切分",
    });
    const relationshipTab = within(detailTabs).getByRole("link", {
      name: "关系与溯源",
    });
    expect(listeningTab).toHaveAttribute("aria-current", "page");
    expect(relationshipTab).toHaveAttribute(
      "href",
      "/receptions/reception-1/graph",
    );

    await user.click(relationshipTab);
    expect(
      await screen.findByText("已进入接待关系图谱"),
    ).toBeInTheDocument();
  });

  it("browses a long reception through bounded previous and next windows", async () => {
    const user = userEvent.setup();
    const firstWindow: ReceptionWorkspaceResponse = {
      ...WORKSPACE,
      reception: {
        ...WORKSPACE.reception,
        duration_sec: 1_200,
      },
      window: {
        ...WORKSPACE.window,
        end_sec: 600,
        reception_duration_sec: 1_200,
        has_next: true,
        next_start_sec: 600,
        total_dialogue_units: 2,
      },
    };
    const secondWindow: ReceptionWorkspaceResponse = {
      ...WORKSPACE,
      reception: {
        ...WORKSPACE.reception,
        duration_sec: 1_200,
      },
      dialogue_units: [
        {
          ...WORKSPACE.dialogue_units[0],
          id: "unit-2",
          unit_index: 1,
          start_sec: 600,
          end_sec: 635,
          topic: "后续窗口",
        },
      ],
      transcript_items: [
        {
          ...WORKSPACE.transcript_items[0],
          id: "segment-2",
          dialogue_unit_id: "unit-2",
          start_sec: 610,
          end_sec: 614,
          text: "后续窗口转写",
        },
      ],
      tag_assignments: [],
      state_transitions: [],
      audit_events: [],
      window: {
        ...WORKSPACE.window,
        start_sec: 600,
        end_sec: 1_200,
        reception_duration_sec: 1_200,
        has_previous: true,
        has_next: false,
        previous_start_sec: 0,
        next_start_sec: null,
        total_dialogue_units: 2,
        tag_assignments: {
          ...WORKSPACE.window.tag_assignments,
          total: 0,
          returned: 0,
        },
        state_transitions: {
          ...WORKSPACE.window.state_transitions,
          total: 0,
          returned: 0,
        },
        provenance_events: {
          ...WORKSPACE.window.provenance_events,
          total: 0,
          returned: 0,
        },
      },
    };
    mockedGetWorkspace.mockReset();
    mockedGetWorkspace
      .mockResolvedValueOnce(firstWindow)
      .mockResolvedValueOnce(secondWindow)
      .mockResolvedValueOnce(firstWindow);

    renderWorkspace();
    await screen.findByText("您好，欢迎光临。");
    await user.click(screen.getByRole("button", { name: "下一时间窗口" }));

    expect(await screen.findByText("后续窗口转写")).toBeInTheDocument();
    expect(mockedGetWorkspace).toHaveBeenNthCalledWith(2, "reception-1", {
      window_start_sec: 600,
      window_size_sec: 600,
    });

    await user.click(screen.getByRole("button", { name: "上一时间窗口" }));
    expect(await screen.findByText("您好，欢迎光临。")).toBeInTheDocument();
    expect(mockedGetWorkspace).toHaveBeenNthCalledWith(3, "reception-1", {
      window_start_sec: 0,
      window_size_sec: 600,
    });
  });

  it("seeks the audio player to the evidence timestamp", async () => {
    const view = renderWorkspace();
    await screen.findByText("接待调听工作台");
    const audio = view.container.querySelector("audio");
    expect(audio).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /定位证据.*12\.5 秒/ }));

    expect(audio?.currentTime).toBe(12.5);
  });

  it("maps source-audio playback time back onto the reception timeline", async () => {
    const user = userEvent.setup();
    const view = renderWorkspace();
    await screen.findByText("接待调听工作台");

    await user.click(
      screen.getByRole("button", { name: /片段二\.wav.*01:00/ }),
    );
    const audio = view.container.querySelector("audio");
    expect(audio).not.toBeNull();
    if (!audio) return;

    audio.currentTime = 10;
    fireEvent.timeUpdate(audio);

    expect(screen.getByText("01:10 / 02:00")).toBeInTheDocument();
  });

  it("hard-stops a clipped source at source_end and switches to the next source slice", async () => {
    const clippedWorkspace: ReceptionWorkspaceResponse = {
      ...WORKSPACE,
      reception: {
        ...WORKSPACE.reception,
        merged_audio_url: null,
        duration_sec: 20,
      },
      recordings: [
        {
          ...WORKSPACE.recordings[0],
          timeline_start_sec: 0,
          timeline_end_sec: 10,
          source_start_sec: 10,
          source_end_sec: 20,
        },
        {
          ...WORKSPACE.recordings[1],
          timeline_start_sec: 10,
          timeline_end_sec: 20,
          source_start_sec: 30,
          source_end_sec: 40,
        },
      ],
      window: {
        ...WORKSPACE.window,
        end_sec: 20,
        reception_duration_sec: 20,
      },
    };
    mockedGetWorkspace.mockResolvedValueOnce(clippedWorkspace);
    const view = renderWorkspace();
    await screen.findByText("接待调听工作台");
    const firstAudio = view.container.querySelector("audio");
    expect(firstAudio).not.toBeNull();
    if (!firstAudio) return;

    firstAudio.currentTime = 20.25;
    fireEvent.timeUpdate(firstAudio);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /片段二\.wav.*00:10/ }),
      ).toHaveAttribute("aria-pressed", "true");
    });
    const secondAudio = view.container.querySelector("audio");
    expect(secondAudio).not.toBeNull();
    if (!secondAudio) return;
    fireEvent.loadedMetadata(secondAudio);
    expect(secondAudio.currentTime).toBe(30);
    expect(screen.getByText("00:10 / 00:20")).toBeInTheDocument();
  });

  it("advances through a real logical silence gap before switching sources", async () => {
    const animationFrames: FrameRequestCallback[] = [];
    const requestFrame = vi.fn((callback: FrameRequestCallback) => {
      animationFrames.push(callback);
      return animationFrames.length;
    });
    vi.stubGlobal("requestAnimationFrame", requestFrame);
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const nowSpy = vi.spyOn(performance, "now").mockReturnValue(1_000);
    const pauseSpy = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
    const playSpy = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue();
    const gapWorkspace: ReceptionWorkspaceResponse = {
      ...WORKSPACE,
      reception: {
        ...WORKSPACE.reception,
        merged_audio_url: null,
        duration_sec: 22,
      },
      recordings: [
        {
          ...WORKSPACE.recordings[0],
          timeline_start_sec: 0,
          timeline_end_sec: 10,
          source_start_sec: 10,
          source_end_sec: 20,
        },
        {
          ...WORKSPACE.recordings[1],
          timeline_start_sec: 12,
          timeline_end_sec: 22,
          source_start_sec: 30,
          source_end_sec: 40,
          gap_before_sec: 2,
        },
      ],
      window: {
        ...WORKSPACE.window,
        end_sec: 22,
        reception_duration_sec: 22,
      },
    };
    mockedGetWorkspace.mockResolvedValueOnce(gapWorkspace);
    const view = renderWorkspace();
    await screen.findByText("接待调听工作台");
    const firstAudio = view.container.querySelector("audio");
    expect(firstAudio).not.toBeNull();
    if (!firstAudio) return;

    firstAudio.currentTime = 20;
    fireEvent.ended(firstAudio);

    expect(await screen.findByText("静音空档 00:10–00:12")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /片段一\.wav.*00:00/ }),
    ).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(animationFrames.length).toBeGreaterThan(0));

    act(() => {
      animationFrames.shift()?.(2_000);
    });
    expect(screen.getByText("00:11 / 00:22")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /片段一\.wav.*00:00/ }),
    ).toHaveAttribute("aria-pressed", "true");

    act(() => {
      animationFrames.shift()?.(3_000);
    });
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /片段二\.wav.*00:12/ }),
      ).toHaveAttribute("aria-pressed", "true");
    });
    const secondAudio = view.container.querySelector("audio");
    expect(secondAudio).not.toBeNull();
    if (secondAudio) {
      fireEvent.loadedMetadata(secondAudio);
      expect(secondAudio.currentTime).toBe(30);
    }
    vi.unstubAllGlobals();
    nowSpy.mockRestore();
    pauseSpy.mockRestore();
    playSpy.mockRestore();
  });

  it("automatically loads the 600-second window containing the playhead", async () => {
    const longWorkspace: ReceptionWorkspaceResponse = {
      ...WORKSPACE,
      reception: { ...WORKSPACE.reception, duration_sec: 1_200 },
      window: {
        ...WORKSPACE.window,
        end_sec: 600,
        reception_duration_sec: 1_200,
        has_next: true,
        next_start_sec: 600,
      },
    };
    const secondWindow: ReceptionWorkspaceResponse = {
      ...longWorkspace,
      window: {
        ...longWorkspace.window,
        start_sec: 600,
        end_sec: 1_200,
        has_previous: true,
        has_next: false,
        previous_start_sec: 0,
        next_start_sec: null,
      },
    };
    mockedGetWorkspace.mockReset();
    mockedGetWorkspace
      .mockResolvedValueOnce(longWorkspace)
      .mockResolvedValueOnce(secondWindow);
    const view = renderWorkspace();
    await screen.findByText("接待调听工作台");
    const audio = view.container.querySelector("audio");
    expect(audio).not.toBeNull();
    if (!audio) return;

    audio.currentTime = 605;
    fireEvent.timeUpdate(audio);

    await waitFor(() => {
      expect(mockedGetWorkspace).toHaveBeenNthCalledWith(
        2,
        "reception-1",
        {
          window_start_sec: 600,
          window_size_sec: 600,
        },
      );
    });
  });

  it("opens a deep-linked source timestamp in its containing workspace window", async () => {
    const longSource = {
      ...WORKSPACE.recordings[0],
      id: "recording-long",
      mapping_id: "mapping-long",
      recording_id: "recording-long",
      timeline_start_sec: 600,
      timeline_end_sec: 1_200,
      source_start_sec: 100,
      source_end_sec: 700,
    };
    const firstWindow: ReceptionWorkspaceResponse = {
      ...WORKSPACE,
      reception: { ...WORKSPACE.reception, duration_sec: 1_200 },
      recordings: [longSource],
      window: {
        ...WORKSPACE.window,
        end_sec: 600,
        reception_duration_sec: 1_200,
        has_next: true,
        next_start_sec: 600,
      },
    };
    const secondWindow: ReceptionWorkspaceResponse = {
      ...firstWindow,
      window: {
        ...firstWindow.window,
        start_sec: 600,
        end_sec: 1_200,
        has_previous: true,
        has_next: false,
        previous_start_sec: 0,
        next_start_sec: null,
      },
    };
    mockedGetWorkspace.mockReset();
    mockedGetWorkspace
      .mockResolvedValueOnce(firstWindow)
      .mockResolvedValueOnce(secondWindow);
    const view = renderWorkspace(
      "/receptions/reception-1/workspace?recording=recording-long&at=120000",
    );

    await waitFor(() => {
      expect(mockedGetWorkspace).toHaveBeenNthCalledWith(
        2,
        "reception-1",
        {
          window_start_sec: 600,
          window_size_sec: 600,
        },
      );
    });
    expect(view.container.querySelector("audio")?.currentTime).toBe(620);
  });

  it("labels unavailable waveform peaks as a loading skeleton", async () => {
    renderWorkspace();
    await screen.findByText("接待调听工作台");

    expect(
      screen.getByRole("status", { name: "尚未生成音频波形" }),
    ).toHaveTextContent("未生成波形");
  });

  it("renders real waveform peaks when the workspace provides them", async () => {
    mockedGetWorkspace.mockResolvedValueOnce({
      ...WORKSPACE,
      waveform_peaks: [0.2, 0.8, 0.4, 1],
    });
    renderWorkspace();

    expect(
      await screen.findByRole("button", {
        name: "真实音频波形，点击定位",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("status", { name: "音频峰值尚未加载" }),
    ).not.toBeInTheDocument();
  });

  it("runs automatic dialogue segmentation with optimistic locking", async () => {
    const user = userEvent.setup();
    mockedGetWorkspace.mockResolvedValueOnce({
      ...WORKSPACE,
      dialogue_units: [],
      tag_assignments: [],
      window: {
        ...WORKSPACE.window,
        total_dialogue_units: 0,
        protected_dialogue_units: 0,
        dialogue_units: {
          ...WORKSPACE.window.dialogue_units,
          total: 0,
          returned: 0,
        },
        tag_assignments: {
          ...WORKSPACE.window.tag_assignments,
          total: 0,
          returned: 0,
        },
      },
    });
    renderWorkspace();

    await user.click(
      await screen.findByRole("button", { name: "运行自动对话切分" }),
    );

    await waitFor(() => {
      expect(mockedSegmentReception).toHaveBeenCalledWith("reception-1", {
        expected_version: 3,
        replace_auto: false,
        algorithm_version: "dialogue-hybrid-v2",
      });
    });
  });

  it("resumes failed automation from its persisted checkpoint", async () => {
    const user = userEvent.setup();
    mockedGetAutomation.mockResolvedValueOnce({
      id: 5,
      reception_id: 1,
      status: "failed",
      stage: "tagging",
      attempt_count: 1,
      checkpoints: {},
      segmentation_algorithm: "dialogue-hybrid-v1",
      tag_group_key: "reception-rules",
      tag_group_version: "rules-v1",
      target_labels: [
        "stage",
        "intent",
        "objection",
        "next_step",
        "compliance_risk",
      ],
      tag_priority: 0,
      last_error_code: "NO_RULE_MATCH",
      last_error_message: "规则数据待补全",
      created_at: "2026-07-23T01:00:00Z",
      updated_at: "2026-07-23T01:01:00Z",
      finished_at: "2026-07-23T01:01:00Z",
    });
    renderWorkspace();

    await user.click(
      await screen.findByRole("button", {
        name: "手工从检查点重试（兼容）",
      }),
    );

    await waitFor(() => {
      expect(mockedRunAutomation).toHaveBeenCalledWith("reception-1");
    });
    expect(
      await screen.findByText(/兼容自动处理完成.*旧规则标签/),
    ).toBeInTheDocument();
  });

  it("polls persisted automation while the background pipeline is running", async () => {
    mockedGetAutomation.mockResolvedValue({
      id: 5,
      reception_id: 1,
      status: "running",
      stage: "tagging",
      attempt_count: 1,
      checkpoints: { merge: "completed", segmentation: "completed" },
      segmentation_algorithm: "dialogue-hybrid-v1",
      tag_group_key: "reception-rules",
      tag_group_version: "rules-v1",
      target_labels: ["intent"],
      tag_priority: 0,
      last_error_code: null,
      last_error_message: null,
      created_at: "2026-07-23T01:00:00Z",
      updated_at: "2026-07-23T01:01:00Z",
      finished_at: null,
    });
    vi.useFakeTimers();
    try {
      renderWorkspace();
      await vi.waitFor(() => {
        expect(mockedGetAutomation).toHaveBeenCalledTimes(1);
      });

      await vi.advanceTimersByTimeAsync(3_000);

      await vi.waitFor(() => {
        expect(mockedGetAutomation.mock.calls.length).toBeGreaterThanOrEqual(2);
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("drives canonical label dimensions from the published schema and creates a durable job", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    expect(await screen.findByText("客户购买意向")).toBeInTheDocument();
    expect(screen.getByText("服务跟进承诺")).toBeInTheDocument();
    expect(
      screen.queryByRole("checkbox", { name: "派生阶段标签" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "达标抽取版本" }),
    ).not.toBeInTheDocument();
    expect(mockedListTaggers).not.toHaveBeenCalled();
    await user.click(
      screen.getByRole("button", { name: "创建标签重算任务" }),
    );

    await waitFor(() => {
      expect(mockedCreateTagJob).toHaveBeenCalledWith(
        {
          job_type: "recompute",
          scope: {
            reception_ids: ["reception-1"],
            label_keys: ["intent.purchase", "promise.follow_up"],
            schema_version_id: 11,
            trigger: "manual_workspace_rerun",
          },
        },
        expect.stringMatching(/^workspace-tag-/),
      );
      expect(mockedCreateTagJob.mock.calls.at(-1)?.[0]).not.toHaveProperty(
        "tagger_version_id",
      );
    });
    expect(
      await screen.findByRole("link", { name: "查看标签任务 #88" }),
    ).toHaveAttribute("href", "/tag-runs/88");
    expect(screen.getByText("服务端已绑定 Tagger #42")).toBeInTheDocument();
    expect(mockedDeriveTags).not.toHaveBeenCalled();
  });

  it("keeps the semantic idempotency key stable when Tagger versions change", async () => {
    mockedListTaggers
      .mockResolvedValueOnce({
        items: [{ id: 42, schema_version_id: 11, status: "qualified" }],
        total: 1,
      })
      .mockResolvedValueOnce({
        items: [{ id: 99, schema_version_id: 11, status: "qualified" }],
        total: 1,
      });

    const firstUser = userEvent.setup();
    const firstView = renderWorkspace();
    await firstUser.click(
      await screen.findByRole("button", { name: "创建标签重算任务" }),
    );
    await waitFor(() => {
      expect(mockedCreateTagJob).toHaveBeenCalledTimes(1);
    });
    const firstRequest = mockedCreateTagJob.mock.calls[0]?.[0];
    const firstKey = mockedCreateTagJob.mock.calls[0]?.[1];
    firstView.unmount();

    const secondUser = userEvent.setup();
    renderWorkspace();
    await secondUser.click(
      await screen.findByRole("button", { name: "创建标签重算任务" }),
    );
    await waitFor(() => {
      expect(mockedCreateTagJob).toHaveBeenCalledTimes(2);
    });
    const secondRequest = mockedCreateTagJob.mock.calls[1]?.[0];
    const secondKey = mockedCreateTagJob.mock.calls[1]?.[1];

    expect(secondRequest).toEqual(firstRequest);
    expect(secondKey).toBe(firstKey);
    expect(mockedListTaggers).not.toHaveBeenCalled();
  });

  it("labels the fixed five-dimension path as a legacy fallback only", async () => {
    mockedListSchemas.mockResolvedValueOnce({ items: [], total: 0 });
    const user = userEvent.setup();
    renderWorkspace();

    expect(await screen.findByText("旧规则兼容模式")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "使用旧规则派生（兼容）" }),
    );

    await waitFor(() => {
      expect(mockedDeriveTags).toHaveBeenCalledWith(
        "reception-1",
        expect.objectContaining({
          target_labels: [
            "stage",
            "intent",
            "objection",
            "next_step",
            "compliance_risk",
          ],
        }),
      );
    });
  });

  it("refreshes an expired playback grant and keeps the source position", async () => {
    const refreshedWorkspace: ReceptionWorkspaceResponse = {
      ...WORKSPACE,
      reception: {
        ...WORKSPACE.reception,
        merged_audio_url: "/audio/reception-1.wav?playback_grant=new",
      },
    };
    mockedGetWorkspace.mockReset();
    mockedGetWorkspace
      .mockResolvedValueOnce(WORKSPACE)
      .mockResolvedValueOnce(refreshedWorkspace);
    const view = renderWorkspace();
    await screen.findByText("接待调听工作台");
    const initialAudio = view.container.querySelector("audio");
    expect(initialAudio).not.toBeNull();
    if (!initialAudio) return;

    initialAudio.currentTime = 18;
    fireEvent.error(initialAudio);

    await waitFor(() => {
      expect(mockedGetWorkspace).toHaveBeenCalledTimes(2);
      expect(view.container.querySelector("audio")?.src).toContain(
        "playback_grant=new",
      );
    });
    const refreshedAudio = view.container.querySelector("audio");
    expect(refreshedAudio).not.toBeNull();
    if (!refreshedAudio) return;
    fireEvent.loadedMetadata(refreshedAudio);
    expect(refreshedAudio.currentTime).toBe(18);
  });

  it("always submits the complete ordered source map when rebuilding a merge", async () => {
    const user = userEvent.setup();
    mockedGetWorkspace.mockResolvedValueOnce({
      ...WORKSPACE,
      reception: {
        ...WORKSPACE.reception,
        duration_sec: 180,
      },
      recordings: [
        ...WORKSPACE.recordings,
        {
          ...WORKSPACE.recordings[1],
          id: "recording-3",
          mapping_id: "mapping-3",
          recording_id: "recording-3",
          name: "片段三.wav",
          sequence_no: 2,
          timeline_start_sec: 120,
          timeline_end_sec: 180,
          audio_url: "/audio/recording-3.wav",
        },
      ],
    });
    renderWorkspace();
    await screen.findByRole("button", { name: /片段三\.wav.*02:00/ });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "录音合并模式" }),
      "both",
    );
    await user.click(
      screen.getByRole("button", { name: "将片段三.wav上移" }),
    );
    expect(screen.getByText("预计时间线 03:00")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "按当前顺序重新合并全部 3 段" }),
    );

    await waitFor(() => {
      expect(mockedMergeRecordings).toHaveBeenCalledWith("reception-1", {
        recording_ids: ["recording-1", "recording-3", "recording-2"],
        mode: "both",
        expected_version: 3,
      });
    });
  });

  it("serializes geometry mutations and preserves the reason after a 409 refresh", async () => {
    const user = userEvent.setup();
    mockedGetWorkspace.mockResolvedValue({
      ...WORKSPACE,
      window: {
        ...WORKSPACE.window,
        protected_dialogue_units: 0,
      },
    });
    let releaseMerge: (() => void) | undefined;
    mockedMergeRecordings.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseMerge = () => resolve(WORKSPACE);
        }),
    );
    renderWorkspace();
    await screen.findByText("接待调听工作台");

    await user.click(
      screen.getByRole("button", {
        name: "按当前顺序重新合并全部 2 段",
      }),
    );
    expect(
      screen.getByRole("button", { name: "重新运行自动切分" }),
    ).toBeDisabled();
    releaseMerge?.();
    await waitFor(() => {
      expect(mockedMergeRecordings).toHaveBeenCalledTimes(1);
    });

    mockedSplitUnit.mockRejectedValueOnce(
      Object.assign(new Error("version conflict"), {
        response: { status: 409 },
      }),
    );
    const audio = document.querySelector("audio");
    expect(audio).not.toBeNull();
    if (!audio) return;
    audio.currentTime = 20;
    fireEvent.timeUpdate(audio);
    const reasonInput = screen.getByRole("textbox", {
      name: "对话编辑原因",
    });
    await user.type(reasonInput, "保留这份人工边界草稿");
    await user.click(
      screen.getByRole("button", { name: "在当前播放点切分" }),
    );

    expect(
      await screen.findByText(/版本冲突.*草稿已保留.*最新版本/),
    ).toBeInTheDocument();
    expect(reasonInput).toHaveValue("保留这份人工边界草稿");
    await waitFor(() => {
      expect(mockedGetWorkspace.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it("obeys server-owned capabilities even for an inspector account", async () => {
    mockedGetWorkspace.mockResolvedValueOnce({
      ...WORKSPACE,
      capabilities: {
        can_manage_audio: false,
        can_run_segmentation: false,
        can_edit_dialogue: false,
        can_edit_tags: false,
        supports_audio_plans: false,
        supports_audio_operations: false,
        can_cancel_audio_operation: false,
      },
    });
    renderWorkspace();
    await screen.findByText("接待调听工作台");

    expect(
      screen.queryByRole("combobox", { name: "录音合并模式" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重新运行自动切分" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "在当前播放点切分" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("当前账号仅可调听与查看证据")).toBeInTheDocument();
  });

  it("merges the server-provided adjacent unit across a workspace boundary", async () => {
    const user = userEvent.setup();
    const nextUnit = {
      ...WORKSPACE.dialogue_units[0],
      id: "unit-next-window",
      unit_index: 1,
      start_sec: 600,
      end_sec: 640,
      version: 2,
    };
    mockedGetWorkspace.mockResolvedValueOnce({
      ...WORKSPACE,
      neighbors: {
        previous_dialogue_unit: null,
        next_dialogue_unit: nextUnit,
      },
      window: {
        ...WORKSPACE.window,
        end_sec: 600,
        has_next: true,
        next_start_sec: 600,
        protected_dialogue_units: 0,
      },
    });
    renderWorkspace();
    await user.type(
      await screen.findByRole("textbox", { name: "对话编辑原因" }),
      "跨窗口语义连续",
    );
    await user.click(
      screen.getByRole("button", { name: "选择相邻单元 #2" }),
    );
    await user.click(
      screen.getByRole("button", { name: "合并相邻对话" }),
    );

    await waitFor(() => {
      expect(mockedMergeUnits).toHaveBeenCalledWith(
        "reception-1",
        "unit-1",
        {
          other_unit_id: "unit-next-window",
          expected_reception_version: 3,
          expected_unit_version: 1,
          expected_other_unit_version: 2,
          reason: "跨窗口语义连续",
        },
      );
    });
  });

  it("previews and submits the asynchronous audio operation when advertised", async () => {
    const user = userEvent.setup();
    mockedGetWorkspace.mockResolvedValueOnce({
      ...WORKSPACE,
      capabilities: {
        ...WORKSPACE.capabilities,
        supports_audio_plans: true,
        supports_audio_operations: true,
        can_cancel_audio_operation: true,
      },
    });
    renderWorkspace();
    const gapInput = await screen.findByLabelText(
      "片段二.wav前静音空档（毫秒）",
    );
    await user.clear(gapInput);
    await user.type(gapInput, "1500");
    expect(screen.getByText("预计时间线 02:01")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "生成合并预览" }),
    );
    await waitFor(() => {
      expect(mockedCreateAudioPlan).toHaveBeenCalledWith("reception-1", {
        sources: [
          { mapping_id: "mapping-1", gap_before_ms: 0 },
          { mapping_id: "mapping-2", gap_before_ms: 1_500 },
        ],
        expected_version: 3,
      });
    });
    expect(await screen.findByText("计划总时长 02:00")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "提交音频任务" }),
    );
    await waitFor(() => {
      expect(mockedCreateAudioOperation).toHaveBeenCalledWith(
        "reception-1",
        {
          plan_token: "signed-plan",
          mode: "both",
          expected_version: 3,
        },
        expect.stringMatching(/^workspace-audio-/),
      );
    });
  });

  it("resumes polling the server-selected active audio operation after reload", async () => {
    mockedGetWorkspace.mockResolvedValueOnce({
      ...WORKSPACE,
      capabilities: {
        ...WORKSPACE.capabilities,
        supports_audio_plans: true,
        supports_audio_operations: true,
        can_cancel_audio_operation: true,
      },
      active_audio_operation: {
        id: "audio-op-resume",
        reception_id: "reception-1",
        status: "assembling",
        mode: "both",
        progress: 0.5,
        error: null,
        created_at: "2026-07-23T01:00:00Z",
        updated_at: "2026-07-23T01:00:01Z",
      },
    });

    renderWorkspace();

    await waitFor(() => {
      expect(mockedGetAudioOperation).toHaveBeenCalledWith(
        "reception-1",
        "audio-op-resume",
      );
    });
    expect(
      await screen.findByText("任务 #audio-op-resume · succeeded · 100%"),
    ).toBeInTheDocument();
  });

  it("rebuilds physical audio from the committed timeline without applying a draft reorder", async () => {
    const user = userEvent.setup();
    mockedGetWorkspace.mockResolvedValueOnce({
      ...WORKSPACE,
      capabilities: {
        ...WORKSPACE.capabilities,
        supports_audio_plans: true,
        supports_audio_operations: true,
      },
    });
    renderWorkspace();
    await user.click(
      await screen.findByRole("button", { name: "将片段二.wav上移" }),
    );
    await user.selectOptions(
      screen.getByLabelText("录音合并模式"),
      "physical",
    );

    expect(
      screen.getByRole("button", { name: "将片段一.wav下移" }),
    ).toBeDisabled();
    expect(
      screen.getByText(
        "仅按当前已提交时间线重建物理产物；来源顺序与空档在此模式下不可修改。",
      ),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", {
        name: "验证当前时间线物理产物",
      }),
    );

    await waitFor(() => {
      expect(mockedCreateAudioPlan).toHaveBeenCalledWith("reception-1", {
        sources: [
          { mapping_id: "mapping-1", gap_before_ms: 0 },
          { mapping_id: "mapping-2", gap_before_ms: 0 },
        ],
        expected_version: 3,
      });
    });
  });
});
