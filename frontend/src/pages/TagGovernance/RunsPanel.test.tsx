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
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RunsPanel } from "./RunsPanel";
import type { TagJob, TagOptimizationRun } from "@/types/api";

vi.mock("@/api/services", () => ({
  getTagOptimizationRun: vi.fn(),
  listTagJobs: vi.fn(),
  listTagOptimizationRuns: vi.fn(),
}));

import {
  getTagOptimizationRun,
  listTagJobs,
  listTagOptimizationRuns,
} from "@/api/services";

const mocks = {
  jobs: listTagJobs as unknown as ReturnType<typeof vi.fn>,
  runs: listTagOptimizationRuns as unknown as ReturnType<typeof vi.fn>,
  run: getTagOptimizationRun as unknown as ReturnType<typeof vi.fn>,
};

function job(overrides: Partial<TagJob> = {}): TagJob {
  return {
    id: 12,
    tenant_id: "t1",
    job_type: "extract",
    status: "running",
    scope: { reception_ids: [5] },
    tagger_version_id: 3,
    origin: "manual",
    total_items: 20,
    completed_items: 5,
    failed_items: 0,
    failed_subset: [],
    attempt_count: 1,
    max_attempts: 3,
    revision: 1,
    lease_owner: "worker-1",
    lease_expires_at: null,
    next_attempt_at: null,
    last_error_code: null,
    last_error_message: null,
    created_at: "2026-07-30T02:00:00Z",
    updated_at: "2026-07-30T02:01:00Z",
    finished_at: null,
    ...overrides,
  };
}

function optimizationRun(
  overrides: Partial<TagOptimizationRun> = {},
): TagOptimizationRun {
  return {
    id: 88,
    job_id: 31,
    status: "completed",
    phase: "completed",
    baseline_tagger_version_id: 3,
    gold_set_version_id: 7,
    cohort: { source: "eligible_feedback" },
    objective: { policy: "balanced" },
    search_budget: { max_trials: 8, sealed_holdout_queries: 1 },
    trigger: "manual",
    summary: {},
    created_at: "2026-07-30T02:00:00Z",
    updated_at: "2026-07-30T03:00:00Z",
    ...overrides,
  };
}

/** fake timers 下让首个查询解析：RTL 的 waitFor 会跟假时钟互相等待。 */
async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <RunsPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RunsPanel", () => {
  beforeEach(() => {
    Object.values(mocks).forEach((mock) => mock.mockReset());
    mocks.runs.mockResolvedValue({ items: [], total: 0 });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("indexes governance jobs with progress, attempts and a link to the run detail", async () => {
    mocks.jobs.mockResolvedValue({
      items: [
        job({ id: 12, completed_items: 5, total_items: 20, attempt_count: 2 }),
      ],
      total: 1,
    });
    renderPanel();

    const row = await screen.findByRole("row", { name: /#12/ });
    expect(within(row).getByText("标签抽取")).toBeInTheDocument();
    expect(within(row).getByText("运行中")).toBeInTheDocument();
    expect(within(row).getByText("5 / 20")).toBeInTheDocument();
    expect(within(row).getByText(/25%/)).toBeInTheDocument();
    expect(within(row).getByText("2 / 3")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "查看详情" })).toHaveAttribute(
      "href",
      "/tag-runs/12",
    );
  });

  it("filters by status and by job type without refetching the unfiltered endpoint", async () => {
    mocks.jobs.mockResolvedValue({
      items: [
        job({ id: 12, job_type: "extract", status: "running" }),
        job({
          id: 13,
          job_type: "evaluate",
          status: "failed",
          finished_at: "2026-07-30T02:30:00Z",
        }),
      ],
      total: 2,
    });
    renderPanel();
    await screen.findByRole("row", { name: /#12/ });

    await userEvent.selectOptions(
      screen.getByLabelText("按状态筛选运行"),
      "failed",
    );
    expect(screen.queryByRole("row", { name: /#12/ })).not.toBeInTheDocument();
    expect(screen.getByRole("row", { name: /#13/ })).toBeInTheDocument();

    await userEvent.selectOptions(
      screen.getByLabelText("按类型筛选运行"),
      "extract",
    );
    // failed + extract 没有交集：空态要说清是筛选筛没了，而不是根本没有运行。
    expect(await screen.findByText("没有符合条件的运行")).toBeInTheDocument();

    await userEvent.selectOptions(
      screen.getByLabelText("按状态筛选运行"),
      "all",
    );
    expect(await screen.findByRole("row", { name: /#12/ })).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /#13/ })).not.toBeInTheDocument();

    // 接口没有 status / job_type 查询参数，筛选只能在客户端做——切筛选不该再打一次请求。
    expect(mocks.jobs).toHaveBeenCalledTimes(1);
  });

  it("treats the succeeded alias as completed when filtering", async () => {
    mocks.jobs.mockResolvedValue({
      items: [
        job({ id: 20, status: "succeeded" }),
        job({ id: 21, status: "completed" }),
        job({ id: 22, status: "running" }),
      ],
      total: 3,
    });
    renderPanel();
    await screen.findByRole("row", { name: /#20/ });

    await userEvent.selectOptions(
      screen.getByLabelText("按状态筛选运行"),
      "completed",
    );
    expect(screen.getByRole("row", { name: /#20/ })).toBeInTheDocument();
    expect(screen.getByRole("row", { name: /#21/ })).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /#22/ })).not.toBeInTheDocument();
  });

  it("polls while a listed job is non-terminal and stops once every job is terminal", async () => {
    vi.useFakeTimers();
    mocks.jobs.mockResolvedValue({
      items: [job({ id: 12, status: "running" })],
      total: 1,
    });
    renderPanel();
    await settle();
    expect(mocks.jobs).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_100);
    });
    expect(mocks.jobs).toHaveBeenCalledTimes(2);

    mocks.jobs.mockResolvedValue({
      items: [
        job({
          id: 12,
          status: "completed",
          completed_items: 20,
          finished_at: "2026-07-30T02:30:00Z",
        }),
      ],
      total: 1,
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_100);
    });
    const callsWhenTerminal = mocks.jobs.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(mocks.jobs).toHaveBeenCalledTimes(callsWhenTerminal);
  });

  it("keeps polling when the running job is hidden by the active filter", async () => {
    vi.useFakeTimers();
    mocks.jobs.mockResolvedValue({
      items: [
        job({ id: 12, status: "running", job_type: "extract" }),
        job({ id: 13, status: "failed", job_type: "evaluate" }),
      ],
      total: 2,
    });
    renderPanel();
    await settle();
    expect(mocks.jobs).toHaveBeenCalledTimes(1);

    // fireEvent 而不是 userEvent：后者的默认延迟在 fake timers 下不会自己推进。
    fireEvent.change(screen.getByLabelText("按状态筛选运行"), {
      target: { value: "failed" },
    });
    expect(screen.queryByRole("row", { name: /#12/ })).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_100);
    });
    expect(mocks.jobs).toHaveBeenCalledTimes(2);
  });

  it("renders the panel error state with a retry when the index fails", async () => {
    mocks.jobs.mockRejectedValue(new Error("boom"));
    renderPanel();

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("数据加载失败")).toBeInTheDocument();

    mocks.jobs.mockResolvedValue({ items: [job({ id: 12 })], total: 1 });
    await userEvent.click(
      within(alert).getByRole("button", { name: "重新加载" }),
    );
    expect(await screen.findByRole("row", { name: /#12/ })).toBeInTheDocument();
  });

  it("shows the empty state when the tenant has no governance runs", async () => {
    mocks.jobs.mockResolvedValue({ items: [], total: 0 });
    renderPanel();

    expect(await screen.findByText("暂无治理运行")).toBeInTheDocument();
    expect(mocks.runs).not.toHaveBeenCalled();
  });

  it("drills an optimize job into its optimization run trials", async () => {
    mocks.jobs.mockResolvedValue({
      items: [
        job({
          id: 31,
          job_type: "optimize",
          status: "completed",
          finished_at: "2026-07-30T03:00:00Z",
        }),
      ],
      total: 1,
    });
    mocks.runs.mockResolvedValue({
      items: [optimizationRun({ id: 88, job_id: 31 })],
      total: 1,
    });
    mocks.run.mockResolvedValue(
      optimizationRun({
        id: 88,
        job_id: 31,
        winner_tagger_version_id: 9,
        trials: [
          {
            id: 501,
            ordinal: 1,
            status: "pruned",
            mutation_dimension: "generation",
            elimination_reason: "低于基线",
          },
          { id: 502, ordinal: 2, status: "completed" },
        ],
      }),
    );
    renderPanel();

    const trigger = await screen.findByRole("button", {
      name: "查看运行 31 的 Trial 明细",
    });
    await userEvent.click(trigger);

    await waitFor(() => expect(mocks.run).toHaveBeenCalledWith(88));
    expect(await screen.findByText("Trial 1")).toBeInTheDocument();
    expect(screen.getByText(/低于基线/)).toBeInTheDocument();
    expect(screen.getByText(/胜出候选 #9/)).toBeInTheDocument();
  });

  it("offers no trial drill-in when no optimization run points at the job", async () => {
    mocks.jobs.mockResolvedValue({
      items: [job({ id: 31, job_type: "optimize", status: "queued" })],
      total: 1,
    });
    mocks.runs.mockResolvedValue({
      items: [optimizationRun({ id: 88, job_id: 999 })],
      total: 1,
    });
    renderPanel();

    await screen.findByRole("row", { name: /#31/ });
    await waitFor(() => expect(mocks.runs).toHaveBeenCalledTimes(1));
    expect(
      screen.queryByRole("button", { name: /Trial 明细/ }),
    ).not.toBeInTheDocument();
    expect(mocks.run).not.toHaveBeenCalled();
  });
});
