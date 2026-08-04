/**
 * VoiceprintQualityDrawer tests.
 *
 * Covers:
 *   - Renders policy sections (status alert, sampling, merge rules)
 *   - Shows honest "未启用" warning when enable_voiceprint=false
 *   - Renders global pending queue rows
 *   - Hides review actions for viewer role, shows them for inspector
 *   - Announces a failed queue / policy fetch instead of drawing it as
 *     "nothing to review" or as empty policy sections
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { VoiceprintQualityDrawer } from "./VoiceprintQualityDrawer";
import { useAuthStore } from "@/stores/auth";
import type { UserInfo } from "@/types/api";

vi.mock("@/api/speakers", () => ({
  getVoiceprintPolicy: vi.fn(),
}));

vi.mock("@/api/advancedGraph", () => ({
  listSpeakerMergePending: vi.fn(),
  confirmSpeakerMerge: vi.fn(),
  rejectSpeakerMerge: vi.fn(),
}));

import { getVoiceprintPolicy } from "@/api/speakers";
import { listSpeakerMergePending } from "@/api/advancedGraph";

const mockedGetPolicy = getVoiceprintPolicy as unknown as ReturnType<
  typeof vi.fn
>;
const mockedListPending = listSpeakerMergePending as unknown as ReturnType<
  typeof vi.fn
>;

const MOCK_POLICY = {
  enable_voiceprint: false,
  adapter_voiceprint_mode: "mock",
  layer1: { cosine_threshold: 0.5, ambiguous_threshold: 0.7 },
  layer2: {
    enabled: true,
    fuzzy_inferred_threshold: 0.6,
    fuzzy_ambiguous_threshold: 0.85,
    voiceprint_reconfirm_cosine: 0.7,
  },
  sampling: {
    strategy: "weighted_mean",
    min_segment_sec: 0.5,
    min_total_sec: 3,
    max_segments_per_speaker: 8,
    diarization_min_segment_sec: 0.5,
    max_speakers: 10,
    embedding_dim: 192,
  },
  retention_cascade: true,
};

const MOCK_PENDING_ROW = {
  id: 11,
  recording_id: 101,
  candidate_name: "王小姐",
  matched_speaker_node_id: 7,
  fuzzy_score: 0.82,
  status: "pending",
  voiceprint_score: 0.65,
  resolved_by: null,
  resolved_at: null,
  notes: null,
  created_at: null,
};

function setUser(role: string | null): void {
  if (role === null) {
    useAuthStore.setState({ user: null });
    return;
  }
  const user: UserInfo = {
    id: 1,
    name: "tester",
    email: "t@example.com",
    role,
    tenant_id: "t1",
  };
  useAuthStore.setState({ user });
}

function renderDrawer(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <VoiceprintQualityDrawer visible onClose={() => undefined} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("VoiceprintQualityDrawer", () => {
  beforeEach(() => {
    mockedGetPolicy.mockReset();
    mockedGetPolicy.mockResolvedValue(MOCK_POLICY);
    mockedListPending.mockReset();
    // Each tab issues its own status-filtered request.
    mockedListPending.mockImplementation(
      (params?: { status?: string | string[] }) => {
        const isPending = params?.status === "pending";
        return Promise.resolve({
          items: isPending ? [MOCK_PENDING_ROW] : [],
          total: isPending ? 1 : 0,
          page: 1,
          page_size: 20,
        });
      },
    );
  });

  afterEach(() => {
    setUser(null);
  });

  it("renders title and honest disabled-pipeline warning", async () => {
    renderDrawer();
    expect(await screen.findByText("声纹质量中心")).toBeInTheDocument();
    expect(
      await screen.findByText("声纹链路当前未启用"),
    ).toBeInTheDocument();
  });

  it("renders sampling strategy and merge-rule thresholds from policy", async () => {
    renderDrawer();
    expect(
      await screen.findByText(/逐段提取后按时长加权平均/),
    ).toBeInTheDocument();
    expect(screen.getByText("采样策略")).toBeInTheDocument();
    expect(screen.getByText("合并判定规则")).toBeInTheDocument();
    // Layer1 thresholds are interpolated from the policy payload.
    expect(screen.getByText(/cos ≥ 0.7 直接合并/)).toBeInTheDocument();
  });

  it("renders the sampling quality gates from policy", async () => {
    renderDrawer();
    // Await a row, not the section title: Arco renders the title before the
    // policy query resolves, so the title alone proves nothing.
    expect(
      await screen.findByText(/不足则该说话人不建立跨录音声纹/),
    ).toBeInTheDocument();
    expect(screen.getByText(/8 段 \/ 录音/)).toBeInTheDocument();
  });

  it("renders the global pending queue rows", async () => {
    renderDrawer();
    expect(await screen.findByText("王小姐")).toBeInTheDocument();
    expect(screen.getByText(/待复核（1）/)).toBeInTheDocument();
  });

  it("fetches pending and resolved separately with server-side status filters", async () => {
    renderDrawer();
    await screen.findByText("王小姐");
    // A single mixed page would let resolved rows crowd out older pending
    // ones and desync the tab count from the header badge.
    expect(mockedListPending).toHaveBeenCalledWith({
      status: "pending",
      limit: 20,
      offset: 0,
    });
    expect(mockedListPending).toHaveBeenCalledWith({
      status: ["resolved_inferred", "resolved_rejected"],
      limit: 20,
      offset: 0,
    });
  });

  it("uses the server total for the pending tab count, not the page length", async () => {
    mockedListPending.mockImplementation(
      (params?: { status?: string | string[] }) => {
        const isPending = params?.status === "pending";
        return Promise.resolve({
          items: isPending ? [MOCK_PENDING_ROW] : [],
          // 42 pending rows in total, only one on this page.
          total: isPending ? 42 : 0,
          page: 1,
          page_size: 20,
        });
      },
    );
    renderDrawer();
    expect(await screen.findByText(/待复核（42）/)).toBeInTheDocument();
  });

  it("hides review buttons for viewer role", async () => {
    setUser("viewer");
    renderDrawer();
    expect(await screen.findByText("王小姐")).toBeInTheDocument();
    expect(screen.queryByText("确认")).not.toBeInTheDocument();
    expect(screen.queryByText("驳回")).not.toBeInTheDocument();
  });

  it("shows review buttons for inspector role", async () => {
    setUser("inspector");
    renderDrawer();
    expect(await screen.findByText("王小姐")).toBeInTheDocument();
    expect(screen.getByText("确认")).toBeInTheDocument();
    expect(screen.getByText("驳回")).toBeInTheDocument();
  });

  it("announces a failed pending queue instead of claiming nothing to review", async () => {
    const user = userEvent.setup();
    mockedListPending.mockImplementation(
      (params?: { status?: string | string[] }) =>
        params?.status === "pending"
          ? Promise.reject(
              Object.assign(new Error("Request failed with status code 500"), {
                response: {
                  status: 500,
                  data: {
                    error: {
                      code: "internal_error",
                      message: "复核队列服务不可用",
                    },
                  },
                },
              }),
            )
          : Promise.resolve({ items: [], total: 0, page: 1, page_size: 20 }),
    );

    renderDrawer();

    // findAllByRole: the pipeline-status banner is an alert too, so the queue
    // failure has to be located by its own text.
    const alerts = await screen.findAllByRole("alert");
    const queueAlert = alerts.find((el) =>
      el.textContent?.includes("复核队列服务不可用"),
    );
    expect(queueAlert).toBeDefined();
    expect(queueAlert).toHaveTextContent("数据加载失败");
    // The whole point of the queue is catching merges that should not have
    // happened; "无待处理项" would send the reviewer away.
    expect(screen.queryByText("无待处理项")).not.toBeInTheDocument();
    expect(screen.queryByText(/待复核（0）/)).not.toBeInTheDocument();

    // The failure must stay recoverable from inside the drawer.
    const callsBeforeRetry = mockedListPending.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "重新加载" }));
    expect(mockedListPending.mock.calls.length).toBeGreaterThan(
      callsBeforeRetry,
    );
  });

  it("never spells an unknown queue size as zero in the tab titles", async () => {
    mockedListPending.mockRejectedValue(new Error("boom"));
    renderDrawer();
    expect(await screen.findByText(/待复核（—）/)).toBeInTheDocument();
    expect(screen.getByText(/已处理（—）/)).toBeInTheDocument();
    expect(screen.queryByText(/待复核（0）/)).not.toBeInTheDocument();
    expect(screen.queryByText(/已处理（0）/)).not.toBeInTheDocument();
  });

  it("announces a failed policy fetch instead of empty policy sections", async () => {
    mockedGetPolicy.mockRejectedValue(
      Object.assign(new Error("Request failed with status code 503"), {
        response: {
          status: 503,
          data: {
            error: { code: "unavailable", message: "策略服务暂不可用" },
          },
        },
      }),
    );

    renderDrawer();

    expect(await screen.findByText("策略服务暂不可用")).toBeInTheDocument();
    // Titled-but-empty sections read as "no thresholds configured", and the
    // pipeline-status alert used to vanish without a word.
    expect(screen.queryByText("采样策略")).not.toBeInTheDocument();
    expect(screen.queryByText("合并判定规则")).not.toBeInTheDocument();
    expect(screen.queryByText("声纹链路当前未启用")).not.toBeInTheDocument();
    expect(screen.queryByText("声纹链路已启用")).not.toBeInTheDocument();
    // The queue below still works — one failure must not blank the drawer.
    expect(await screen.findByText("王小姐")).toBeInTheDocument();
  });
});
