import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  AnalyzeTagInsightsResponse,
  ReceptionTagInsightsRequest,
  ReceptionTagInsightsResponse,
} from "@/types/api";
import { GovernanceActions } from "./GovernanceActions";

vi.mock("@/api/services", () => ({
  createTagJob: vi.fn(),
  createTagReviewBatch: vi.fn(),
}));

import {
  createTagJob,
  createTagReviewBatch,
} from "@/api/services";

const mockedCreateJob = createTagJob as unknown as ReturnType<typeof vi.fn>;
const mockedCreateReview =
  createTagReviewBatch as unknown as ReturnType<typeof vi.fn>;

const RESULT = {
  groups: [
    {
      group_key: "model",
      group_id: "model@v2",
      version: "v2",
      source: "llm",
      priority: 10,
    },
    {
      group_key: "review",
      group_id: "review@v1",
      version: "v1",
      source: "manual",
      priority: 20,
    },
  ],
  overview: {
    group_count: 2,
    assignment_count: 2,
    total_cells: 1,
    complete_cells: 1,
    incomplete_cells: 0,
    conflict_cells: 1,
    conflict_rate: 1,
  },
  matrix: [
    {
      target_id: "reception:101/unit:77",
      window: { start_ms: 3_100, end_ms: 5_800 },
      label_key: "intent",
      store_ids: ["S1"],
      agent_ids: ["A1"],
      cells: [],
      merged: {
        strategy: "manual_wins",
        values: [],
        selected_group_keys: [],
        confidence: null,
        evidence_refs: [],
      },
      conflict: true,
      missing_group_keys: [],
    },
  ],
} as unknown as AnalyzeTagInsightsResponse;

const PERSISTED = {
  returned_reception_ids: [101],
  selected_group_ids: ["model@v2", "review@v1"],
} as ReceptionTagInsightsResponse;

const REQUEST: ReceptionTagInsightsRequest = {
  store_id: ["S1"],
  reception_id: [101],
  group_id: ["model@v2", "review@v1"],
};

function renderActions({
  persisted = PERSISTED,
  request = REQUEST,
}: {
  persisted?: ReceptionTagInsightsResponse;
  request?: ReceptionTagInsightsRequest;
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <GovernanceActions
          result={RESULT}
          persisted={persisted}
          request={request}
          onCompareVersions={vi.fn()}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("GovernanceActions", () => {
  beforeEach(() => {
    mockedCreateJob.mockReset();
    mockedCreateReview.mockReset();
    mockedCreateReview.mockResolvedValue({
      batch_id: "batch-1",
      created_count: 1,
      items: [{ id: 501 }],
    });
    mockedCreateJob.mockResolvedValue({ id: 88, status: "queued" });
  });

  it("turns conflicts into a traceable review batch", async () => {
    const user = userEvent.setup();
    renderActions();
    await user.click(screen.getByRole("button", { name: "创建复核批次" }));

    await waitFor(() => {
      expect(mockedCreateReview).toHaveBeenCalledWith({
        reason: "conflict",
        review_bundle_id: expect.stringMatching(/^insight-/),
        subjects: [
          {
            subject_type: "dialogue_unit",
            subject_id: 77,
            reception_id: 101,
            tag_key: "intent",
          },
        ],
      });
    });
    expect(await screen.findByText("已创建 1 个复核任务")).toBeVisible();
    expect(screen.getByRole("link", { name: "进入复核工作台" })).toHaveAttribute(
      "href",
      "/tag-review",
    );
  });

  it("surfaces the server-issued batch id so gold-set freezing can reference it", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    renderActions();
    await user.click(screen.getByRole("button", { name: "创建复核批次" }));

    // 金标冻结要求填写复核批次 ID，而这个 ID 只在创建响应里返回一次，
    // 因此成功反馈必须把它展示出来并支持复制。
    expect(await screen.findByText("batch-1")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "复制批次 ID" }));
    expect(writeText).toHaveBeenCalledWith("batch-1");
  });

  it("creates an idempotent scoped rerun and links to progress", async () => {
    const user = userEvent.setup();
    renderActions();
    await user.click(screen.getByRole("button", { name: "范围重跑" }));

    await waitFor(() => {
      expect(mockedCreateJob).toHaveBeenCalledWith(
        {
          job_type: "recompute",
          scope: {
            store_ids: ["S1"],
            reception_ids: [101],
            group_ids: ["model@v2", "review@v1"],
          },
        },
        expect.stringMatching(/^insight-rerun-/),
      );
    });
    expect(screen.getByRole("link", { name: "查看运行 #88" })).toHaveAttribute(
      "href",
      "/tag-runs/88",
    );
  });

  it("routes candidate creation through the governed optimizer and keeps version comparison scope", async () => {
    const user = userEvent.setup();
    renderActions();
    const candidateLink = screen.getByRole("link", { name: "创建候选" });
    const href = candidateLink.getAttribute("href");
    expect(href).toContain("/tag-governance?tab=evolution&mode=optimize");
    const params = new URLSearchParams(href?.split("?")[1]);
    expect(JSON.parse(params.get("cohort") ?? "{}")).toEqual({
      source: "tag_insights",
      filters: {
        store_ids: ["S1"],
        reception_ids: [101],
      },
      group_ids: ["model@v2", "review@v1"],
      conflict_only: true,
    });

    await user.click(screen.getByRole("button", { name: "版本对比" }));
    expect(screen.getByRole("dialog", { name: "版本对比范围" })).toHaveTextContent(
      "model@v2",
    );
    expect(screen.getByRole("dialog", { name: "版本对比范围" })).toHaveTextContent(
      "review@v1",
    );
  });

  it("carries the full persisted insight cohort into governed optimization", () => {
    renderActions({
      persisted: {
        ...PERSISTED,
        returned_reception_ids: [101, 102],
      },
      request: {
        store_id: ["S1"],
        agent_name: ["小林"],
        scenario: ["automotive"],
        reception_id: [],
        group_key: ["model", "review"],
        started_from: "2026-07-01T00:00:00Z",
        started_to: "2026-07-25T23:59:59Z",
      },
    });

    const href = screen
      .getByRole("link", { name: "创建候选" })
      .getAttribute("href");
    const params = new URLSearchParams(href?.split("?")[1]);
    expect(JSON.parse(params.get("cohort") ?? "{}")).toEqual({
      source: "tag_insights",
      filters: {
        store_ids: ["S1"],
        agent_names: ["小林"],
        reception_ids: [101, 102],
        scenarios: ["automotive"],
        group_keys: ["model", "review"],
        started_from: "2026-07-01T00:00:00Z",
        started_to: "2026-07-25T23:59:59Z",
      },
      group_ids: ["model@v2", "review@v1"],
      conflict_only: true,
    });
  });

  it("blocks reruns that have no concrete reception ids", async () => {
    renderActions({
      persisted: {
        ...PERSISTED,
        returned_reception_ids: [],
      },
      request: {
        ...REQUEST,
        reception_id: [],
      },
    });

    expect(screen.getByRole("button", { name: "范围重跑" })).toBeDisabled();
    expect(
      screen.getByText("范围重跑需要明确的接待 ID，请先缩小洞察筛选范围。"),
    ).toBeInTheDocument();
  });
});
