import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ReceptionEntryPage from "./index";
import type {
  ReceptionDiscoveryResponse,
  ReceptionListResponse,
  ReceptionResponseApi,
} from "@/types/api";

vi.mock("@/api/services", () => ({
  acceptReceptionProposal: vi.fn(),
  discoverReceptionProposals: vi.fn(),
  listReceptions: vi.fn(),
  runReceptionAutomation: vi.fn(),
}));

import {
  acceptReceptionProposal,
  discoverReceptionProposals,
  listReceptions,
  runReceptionAutomation,
} from "@/api/services";

const mockedAccept =
  acceptReceptionProposal as unknown as ReturnType<typeof vi.fn>;
const mockedDiscover =
  discoverReceptionProposals as unknown as ReturnType<typeof vi.fn>;
const mockedList = listReceptions as unknown as ReturnType<typeof vi.fn>;
const mockedRunAutomation =
  runReceptionAutomation as unknown as ReturnType<typeof vi.fn>;

const RECEPTION = {
  id: 42,
  tenant_id: "tenant-1",
  external_session_id: null,
  scenario: "gold" as const,
  store_id: "store-1",
  agent_name: "销售甲",
  customer_hash: null,
  status: "needs_review" as const,
  merge_mode: "logical" as const,
  merge_confidence: 0.82,
  started_at: "2026-07-23T01:00:00Z",
  ended_at: "2026-07-23T01:12:00Z",
  audio_url: null,
  version: 1,
  created_at: "2026-07-23T01:12:00Z",
  updated_at: "2026-07-23T01:12:00Z",
};

const QUEUE: ReceptionListResponse = {
  items: [RECEPTION],
  total: 21,
  page: 1,
  page_size: 20,
};

const DISCOVERY: ReceptionDiscoveryResponse = {
  scanned_recordings: 5,
  total: 2,
  truncated: false,
  items: [
    {
      candidate_type: "merge_group",
      recording_ids: [101, 102],
      decision: "merge",
      confidence: 0.91,
      reasons: [
        {
          code: "temporal_gap",
          contribution: 0.43,
          detail: "两段录音间隔 18 秒",
          hard_constraint: false,
        },
      ],
      store_id: "store-1",
      started_at: "2026-07-23T02:00:00Z",
      ended_at: "2026-07-23T02:08:00Z",
      duration_status: "available",
      split_at_sec: null,
      at_segment_id: null,
      proposal_token: null,
      proposal_expires_at: null,
    },
    {
      candidate_type: "merge_group",
      recording_ids: [103],
      decision: "merge",
      confidence: 1,
      reasons: [
        {
          code: "single_recording_reception",
          contribution: 1,
          detail: "单段录音可创建独立接待",
          hard_constraint: true,
        },
      ],
      store_id: "store-1",
      started_at: "2026-07-23T03:00:00Z",
      ended_at: "2026-07-23T03:04:00Z",
      duration_status: "available",
      split_at_sec: null,
      at_segment_id: null,
      proposal_token: null,
      proposal_expires_at: null,
    },
    {
      candidate_type: "recording_split",
      recording_ids: [201],
      decision: "needs_review",
      confidence: 0.76,
      reasons: [
        {
          code: "long_silence",
          contribution: 0.6,
          detail: "检测到新的客户接待边界",
          hard_constraint: false,
        },
      ],
      store_id: "store-1",
      started_at: "2026-07-23T04:00:00Z",
      ended_at: "2026-07-23T05:00:00Z",
      duration_status: "available",
      split_at_sec: 1200,
      at_segment_id: 777,
      proposal_token: "signed-split-proposal-token-00000000000000000000",
      proposal_expires_at: "2026-07-23T04:15:00Z",
    },
    {
      candidate_type: "duration_review",
      recording_ids: [301],
      decision: "needs_review",
      confidence: 0,
      reasons: [
        {
          code: "duration_unavailable",
          contribution: -1,
          detail: "尚无可用分段时长",
          hard_constraint: true,
        },
      ],
      store_id: "store-1",
      started_at: "2026-07-23T06:00:00Z",
      ended_at: null,
      duration_status: "unavailable",
      split_at_sec: null,
      at_segment_id: null,
      proposal_token: null,
      proposal_expires_at: null,
    },
  ],
};

function renderEntry() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/receptions"]}>
        <Routes>
          <Route path="/receptions" element={<ReceptionEntryPage />} />
          <Route
            path="/receptions/:id/workspace"
            element={<div>已进入接待工作台</div>}
          />
          <Route
            path="/tag-insights"
            element={<div>已进入标签洞察</div>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ReceptionEntryPage", () => {
  beforeEach(() => {
    mockedAccept.mockReset();
    mockedDiscover.mockReset();
    mockedList.mockReset();
    mockedRunAutomation.mockReset();
    mockedList.mockResolvedValue(QUEUE);
    mockedRunAutomation.mockResolvedValue({
      id: 9,
      reception_id: 88,
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
      created_at: "2026-07-23T01:12:00Z",
      updated_at: "2026-07-23T01:13:00Z",
      finished_at: "2026-07-23T01:13:00Z",
    });
  });

  it("keeps direct positive-id navigation as a fast path", async () => {
    const user = userEvent.setup();
    renderEntry();

    await user.type(screen.getByLabelText("接待 ID"), "abc");
    await user.click(screen.getByRole("button", { name: "打开调听工作台" }));

    expect(screen.getByRole("alert")).toHaveTextContent("有效的正整数");
    await user.clear(screen.getByLabelText("接待 ID"));
    await user.type(screen.getByLabelText("接待 ID"), "42");
    await user.click(screen.getByRole("button", { name: "打开调听工作台" }));

    expect(await screen.findByText("已进入接待工作台")).toBeInTheDocument();
  });

  it("offers a direct handoff from the reception queue into tag insights", async () => {
    const user = userEvent.setup();
    renderEntry();

    expect(await screen.findByText("销售甲")).toBeInTheDocument();
    const insightLink = screen.getByRole("link", { name: "进入标签洞察" });
    expect(insightLink).toHaveAttribute("href", "/tag-insights");

    await user.click(insightLink);
    expect(await screen.findByText("已进入标签洞察")).toBeInTheDocument();
  });

  it("loads the real paginated queue and applies store/status filters", async () => {
    const user = userEvent.setup();
    renderEntry();

    expect(await screen.findByText("销售甲")).toBeInTheDocument();
    expect(screen.getByText("共 21 个接待")).toBeInTheDocument();
    expect(mockedList).toHaveBeenCalledWith({
      page: 1,
      page_size: 20,
      store_id: undefined,
      status: undefined,
    });

    await user.type(screen.getByLabelText("门店筛选"), "store-2");
    await user.selectOptions(screen.getByLabelText("状态筛选"), "ready");
    await user.click(screen.getByRole("button", { name: "查询工作队列" }));

    await waitFor(() => {
      expect(mockedList).toHaveBeenLastCalledWith({
        page: 1,
        page_size: 20,
        store_id: "store-2",
        status: "ready",
      });
    });

    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => {
      expect(mockedList).toHaveBeenLastCalledWith({
        page: 2,
        page_size: 20,
        store_id: "store-2",
        status: "ready",
      });
    });
  });

  it("shows explicit loading and empty queue states", async () => {
    mockedList.mockImplementationOnce(
      () => new Promise<ReceptionListResponse>(() => undefined),
    );
    const firstRender = renderEntry();

    expect(
      screen.getByRole("status", { name: "" }),
    ).toHaveTextContent("正在加载接待队列");
    firstRender.unmount();

    mockedList.mockResolvedValueOnce({
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
    });
    renderEntry();

    expect(
      await screen.findByText("当前筛选条件下暂无真实接待"),
    ).toBeInTheDocument();
  });

  it("scans a bounded window and renders explainable candidates without fake data", async () => {
    const user = userEvent.setup();
    mockedDiscover.mockResolvedValueOnce(DISCOVERY);
    renderEntry();

    expect(
      screen.getByText(/设置真实门店与时间窗后开始扫描/),
    ).toBeInTheDocument();

    await user.type(screen.getByLabelText("候选门店"), "store-1");
    await user.selectOptions(screen.getByLabelText("业务场景"), "gold");
    await user.clear(screen.getByLabelText("开始时间"));
    await user.type(
      screen.getByLabelText("开始时间"),
      "2026-07-22T00:00",
    );
    await user.clear(screen.getByLabelText("结束时间"));
    await user.type(
      screen.getByLabelText("结束时间"),
      "2026-07-23T00:00",
    );
    await user.click(screen.getByRole("button", { name: "扫描候选" }));

    expect(await screen.findByText("短录音接待组合")).toBeInTheDocument();
    expect(screen.getByText("长录音切分建议")).toBeInTheDocument();
    expect(screen.getByText("时长待补全")).toBeInTheDocument();
    expect(screen.getByText("置信度 91%")).toBeInTheDocument();
    expect(screen.getByText("两段录音间隔 18 秒")).toBeInTheDocument();
    expect(
      screen.getByText(/源录音保持不变，并原子创建前后两个接待/),
    ).toBeInTheDocument();
    expect(screen.getByText(/先完成索引时长/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "接受并创建接待" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "创建单录音接待" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "执行切分并自动分析" }),
    ).toBeInTheDocument();
    expect(mockedDiscover).toHaveBeenCalledWith({
      scenario: "gold",
      store_id: "store-1",
      recorded_from: new Date("2026-07-22T00:00").toISOString(),
      recorded_to: new Date("2026-07-23T00:00").toISOString(),
      short_recording_max_sec: 300,
      limit: 200,
    });
  });

  it("accepts a merge candidate then enters the created workspace", async () => {
    const user = userEvent.setup();
    mockedDiscover.mockResolvedValueOnce(DISCOVERY);
    mockedAccept.mockResolvedValueOnce({
      ...RECEPTION,
      id: 88,
      status: "confirmed",
      recordings: [],
    } satisfies ReceptionResponseApi);
    renderEntry();

    await user.type(screen.getByLabelText("候选门店"), "store-1");
    await user.clear(screen.getByLabelText("开始时间"));
    await user.type(
      screen.getByLabelText("开始时间"),
      "2026-07-22T00:00",
    );
    await user.clear(screen.getByLabelText("结束时间"));
    await user.type(
      screen.getByLabelText("结束时间"),
      "2026-07-23T00:00",
    );
    await user.click(screen.getByRole("button", { name: "扫描候选" }));
    await user.click(
      await screen.findByRole("button", { name: "接受并创建接待" }),
    );

    expect(mockedAccept).toHaveBeenCalledWith({
      scenario: "automotive",
      recording_ids: [101, 102],
      merge_mode: "logical",
    });
    expect(mockedRunAutomation).toHaveBeenCalledWith(88);
    expect(await screen.findByText("已进入接待工作台")).toBeInTheDocument();
  });

  it("atomically accepts a signed long-recording split and automates both children", async () => {
    const user = userEvent.setup();
    mockedDiscover.mockResolvedValueOnce(DISCOVERY);
    mockedAccept.mockResolvedValueOnce({
      candidate_type: "recording_split",
      recording_id: 201,
      split_at_sec: 1200,
      at_segment_id: 777,
      source_duration_sec: 3600,
      receptions: [
        {
          ...RECEPTION,
          id: 91,
          status: "confirmed",
          recordings: [],
        },
        {
          ...RECEPTION,
          id: 92,
          status: "confirmed",
          recordings: [],
        },
      ],
      provenance_event_ids: [501, 502, 503],
    });
    mockedRunAutomation.mockReset();
    mockedRunAutomation.mockImplementation(async (receptionId: number) => ({
      id: receptionId + 100,
      reception_id: receptionId,
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
      created_at: "2026-07-23T01:12:00Z",
      updated_at: "2026-07-23T01:13:00Z",
      finished_at: "2026-07-23T01:13:00Z",
    }));
    renderEntry();

    await user.type(screen.getByLabelText("候选门店"), "store-1");
    await user.clear(screen.getByLabelText("开始时间"));
    await user.type(
      screen.getByLabelText("开始时间"),
      "2026-07-22T00:00",
    );
    await user.clear(screen.getByLabelText("结束时间"));
    await user.type(
      screen.getByLabelText("结束时间"),
      "2026-07-23T00:00",
    );
    await user.click(screen.getByRole("button", { name: "扫描候选" }));
    await user.click(
      await screen.findByRole("button", {
        name: "执行切分并自动分析",
      }),
    );

    expect(mockedAccept).toHaveBeenCalledWith({
      scenario: "automotive",
      recording_ids: [201],
      merge_mode: "logical",
      candidate_type: "recording_split",
      split_at_sec: 1200,
      at_segment_id: 777,
      proposal_token: "signed-split-proposal-token-00000000000000000000",
    });
    expect(mockedRunAutomation).toHaveBeenCalledWith(91);
    expect(mockedRunAutomation).toHaveBeenCalledWith(92);
    expect(await screen.findByText("已进入接待工作台")).toBeInTheDocument();
  });

  it("shows a real empty discovery result after a completed scan", async () => {
    const user = userEvent.setup();
    mockedDiscover.mockResolvedValueOnce({
      items: [],
      scanned_recordings: 0,
      total: 0,
      truncated: false,
    });
    renderEntry();

    await user.type(screen.getByLabelText("候选门店"), "store-empty");
    await user.clear(screen.getByLabelText("开始时间"));
    await user.type(
      screen.getByLabelText("开始时间"),
      "2026-07-22T00:00",
    );
    await user.clear(screen.getByLabelText("结束时间"));
    await user.type(
      screen.getByLabelText("结束时间"),
      "2026-07-23T00:00",
    );
    await user.click(screen.getByRole("button", { name: "扫描候选" }));

    expect(
      await screen.findByText("该时间窗内没有可复核候选"),
    ).toBeInTheDocument();
    expect(screen.getByText("已扫描 0 段录音")).toBeInTheDocument();
  });

  it("keeps the candidate visible when acceptance fails", async () => {
    const user = userEvent.setup();
    mockedDiscover.mockResolvedValueOnce(DISCOVERY);
    mockedAccept.mockRejectedValueOnce(new Error("录音已被其他接待占用"));
    renderEntry();

    await user.type(screen.getByLabelText("候选门店"), "store-1");
    await user.clear(screen.getByLabelText("开始时间"));
    await user.type(
      screen.getByLabelText("开始时间"),
      "2026-07-22T00:00",
    );
    await user.clear(screen.getByLabelText("结束时间"));
    await user.type(
      screen.getByLabelText("结束时间"),
      "2026-07-23T00:00",
    );
    await user.click(screen.getByRole("button", { name: "扫描候选" }));
    await user.click(
      await screen.findByRole("button", { name: "接受并创建接待" }),
    );

    expect(
      await screen.findByText("录音已被其他接待占用"),
    ).toBeInTheDocument();
    expect(screen.getByText("候选未写入，可刷新扫描结果后重试。")).toBeInTheDocument();
    expect(screen.getByText("两段录音间隔 18 秒")).toBeInTheDocument();
  });

  it("shows independent queue and discovery error states with recovery actions", async () => {
    const user = userEvent.setup();
    mockedList.mockRejectedValueOnce(new Error("队列服务不可用"));
    mockedDiscover.mockRejectedValueOnce(new Error("扫描超时"));
    renderEntry();

    expect(await screen.findByText("队列服务不可用")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新加载工作队列" }),
    ).toBeInTheDocument();

    await user.type(screen.getByLabelText("候选门店"), "store-1");
    await user.clear(screen.getByLabelText("开始时间"));
    await user.type(
      screen.getByLabelText("开始时间"),
      "2026-07-22T00:00",
    );
    await user.clear(screen.getByLabelText("结束时间"));
    await user.type(
      screen.getByLabelText("结束时间"),
      "2026-07-23T00:00",
    );
    await user.click(screen.getByRole("button", { name: "扫描候选" }));

    expect(await screen.findByText("扫描超时")).toBeInTheDocument();
  });
});
