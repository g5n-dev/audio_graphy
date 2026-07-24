import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
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
  deriveReceptionDialogueTags: vi.fn(),
  getReceptionAutomation: vi.fn(),
  getReceptionWorkspace: vi.fn(),
  mergeReceptionRecordings: vi.fn(),
  segmentReception: vi.fn(),
  splitDialogueUnit: vi.fn(),
  mergeDialogueUnits: vi.fn(),
  runReceptionAutomation: vi.fn(),
}));

import {
  deriveReceptionDialogueTags,
  getReceptionAutomation,
  getReceptionWorkspace,
  mergeReceptionRecordings,
  segmentReception,
  runReceptionAutomation,
} from "@/api/services";

const mockedGetWorkspace = getReceptionWorkspace as unknown as ReturnType<
  typeof vi.fn
>;
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
const mockedRunAutomation =
  runReceptionAutomation as unknown as ReturnType<typeof vi.fn>;

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
    version: 3,
  },
  recordings: [
    {
      id: "recording-1",
      name: "片段一.wav",
      sequence_no: 0,
      timeline_start_sec: 0,
      timeline_end_sec: 60,
      source_start_sec: 0,
      source_end_sec: 60,
      gap_before_sec: 0,
      audio_url: "/audio/recording-1.wav",
      decision_source: "explicit",
      merge_confidence: 1,
    },
    {
      id: "recording-2",
      name: "片段二.wav",
      sequence_no: 1,
      timeline_start_sec: 60,
      timeline_end_sec: 120,
      source_start_sec: 0,
      source_end_sec: 60,
      gap_before_sec: 0,
      audio_url: "/audio/recording-2.wav",
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
};

function renderWorkspace(): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/receptions/reception-1/workspace"]}>
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
    mockedDeriveTags.mockReset();
    mockedGetWorkspace.mockReset();
    mockedGetAutomation.mockReset();
    mockedMergeRecordings.mockReset();
    mockedSegmentReception.mockReset();
    mockedRunAutomation.mockReset();
    mockedGetWorkspace.mockResolvedValue(WORKSPACE);
    mockedMergeRecordings.mockResolvedValue(WORKSPACE);
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
        algorithm_version: "dialogue-hybrid-v1",
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
      await screen.findByRole("button", { name: "从检查点重试" }),
    );

    await waitFor(() => {
      expect(mockedRunAutomation).toHaveBeenCalledWith("reception-1");
    });
    expect(
      await screen.findByText(/自动处理完成.*五维目标标签/),
    ).toBeInTheDocument();
  });

  it("derives all five target label dimensions and shows the persisted version result", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    await user.click(
      await screen.findByRole("button", { name: "派生目标标签" }),
    );

    await waitFor(() => {
      expect(mockedDeriveTags).toHaveBeenCalledWith("reception-1", {
        group_key: "reception-rules",
        group_version: "rules-v1",
        target_labels: [
          "stage",
          "intent",
          "objection",
          "next_step",
          "compliance_risk",
        ],
        priority: 0,
      });
    });
    expect(
      await screen.findByText("reception-rules@rules-v1"),
    ).toBeInTheDocument();
    expect(screen.getByText(/写入 4 个标签/)).toBeInTheDocument();
    expect(screen.getByText("intent = purchase")).toBeInTheDocument();
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
      screen.getByRole("button", { name: "按当前顺序重新合并全部 3 段" }),
    );

    await waitFor(() => {
      expect(mockedMergeRecordings).toHaveBeenCalledWith("reception-1", {
        recording_ids: ["recording-1", "recording-2", "recording-3"],
        mode: "both",
        expected_version: 3,
      });
    });
  });
});
