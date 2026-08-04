import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EvolutionPanel } from "./EvolutionPanel";

vi.mock("@/api/services", () => ({
  cancelTagOptimizationRun: vi.fn(),
  compareTagOptimizationTrials: vi.fn(),
  createTagOptimizationRun: vi.fn(),
  getTagEvolutionOverview: vi.fn(),
  getTagOptimizationRun: vi.fn(),
  listTagBadcases: vi.fn(),
  listTagOptimizationRuns: vi.fn(),
}));

import {
  cancelTagOptimizationRun,
  compareTagOptimizationTrials,
  createTagOptimizationRun,
  getTagEvolutionOverview,
  getTagOptimizationRun,
  listTagBadcases,
  listTagOptimizationRuns,
} from "@/api/services";

const mocks = {
  cancel: cancelTagOptimizationRun as unknown as ReturnType<typeof vi.fn>,
  compare: compareTagOptimizationTrials as unknown as ReturnType<typeof vi.fn>,
  create: createTagOptimizationRun as unknown as ReturnType<typeof vi.fn>,
  overview: getTagEvolutionOverview as unknown as ReturnType<typeof vi.fn>,
  runDetail: getTagOptimizationRun as unknown as ReturnType<typeof vi.fn>,
  badcases: listTagBadcases as unknown as ReturnType<typeof vi.fn>,
  runs: listTagOptimizationRuns as unknown as ReturnType<typeof vi.fn>,
};

const TRIALS = [
  {
    id: 11,
    optimization_run_id: 71,
    ordinal: 1,
    status: "completed" as const,
    phase: "validation" as const,
    mutation: { description: "保持基线", dimension: "context" },
    candidate_tagger_version_id: null,
  },
  {
    id: 12,
    optimization_run_id: 71,
    ordinal: 2,
    status: "pruned" as const,
    phase: "validation" as const,
    mutation: { description: "扩大示例窗口", dimension: "context" },
    candidate_tagger_version_id: null,
  },
  {
    id: 13,
    optimization_run_id: 71,
    ordinal: 3,
    status: "completed" as const,
    phase: "validation" as const,
    mutation: { description: "引入弱到强 critic", dimension: "orchestration" },
    candidate_tagger_version_id: 44,
  },
];

function renderPanel(
  props: Partial<React.ComponentProps<typeof EvolutionPanel>> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <EvolutionPanel isAdmin {...props} />
    </QueryClientProvider>,
  );
}

describe("EvolutionPanel", () => {
  beforeEach(() => {
    Object.values(mocks).forEach((mock) => mock.mockReset());
    mocks.overview.mockResolvedValue({
      production_harness: {
        id: 42,
        version: "harness-2.3",
        status: "production",
      },
      recommended_gold_set_version_id: 7,
      recommended_gold_set_label: "销售对话完整金标 · 2026.07",
      quality: {
        unbiased_macro_f1: 0.884,
        critical_recall_lcb: 0.956,
        evidence_iou: 0.81,
        worst_slice_f1: 0.79,
      },
      feedback: {
        eligible_count: 236,
        new_since_last_run: 68,
        representative_audit_count: 120,
        adjudicated_count: 44,
        coverage_rate: 0.92,
        next_run_eligible: true,
        blockers: [],
      },
      drift: {
        status: "watch",
        input_psi: 0.12,
        output_jsd: 0.046,
        affected_slices: ["automotive / S1"],
      },
      release: {
        stage: "canary_25",
        served_count: 5_480,
        paired_count: 1_260,
        audited_count: 516,
        adjudicated_count: 88,
        waiting_reasons: ["等待 48 小时时间门禁"],
        promotion_paused: false,
      },
    });
    mocks.badcases.mockResolvedValue({
      items: [
        {
          id: 9,
          tag_key: "intent",
          failure_stage: "tag_reasoning",
          failure_mode: "购买意向被识别为随便看看",
          status: "open",
          occurrence_count: 37,
          root_cause: {
            affected_slices: ["automotive"],
            representative_excerpt: "今天价格合适就签",
          },
          regression_result: { status: "pending" },
        },
      ],
      total: 1,
    });
    mocks.runs.mockResolvedValue({
      items: [
        {
          id: 71,
          status: "running",
          phase: "validation",
          baseline_tagger_version_id: 42,
          baseline_version: "harness-2.3",
          candidate_tagger_version_id: 44,
          candidate_version: "harness-2.4-c1",
          gold_set_version_id: 7,
          cohort: { source: "scheduled" },
          objective: { policy: "balanced" },
          search_budget: {
            max_trials: 24,
            sealed_holdout_queries: 1,
          },
          trigger: "scheduled",
          summary: {
            completed_trials: 18,
            trial_count: 24,
            candidate_comparison: {
              dimensions: [
                {
                  dimension: "orchestration",
                  before: "weak_llm",
                  after: "weak_to_strong_critic",
                },
              ],
              metric_deltas: {
                macro_f1: 0.024,
                review_rate: -0.031,
                p95_latency_ms: 84,
              },
              improved_badcase_count: 31,
              regressed_badcase_count: 2,
            },
          },
          created_at: "2026-07-25T06:00:00Z",
          updated_at: "2026-07-25T06:20:00Z",
        },
      ],
      total: 1,
    });
    mocks.create.mockResolvedValue({
      id: 72,
      status: "queued",
      phase: "prepare",
      baseline_tagger_version_id: 42,
      gold_set_version_id: 7,
      cohort: { source: "tag_insights" },
      objective: { policy: "quality_first" },
      search_budget: { max_trials: 16, sealed_holdout_queries: 1 },
      trigger: "insight",
      summary: { completed_trials: 0, trial_count: 16 },
      created_at: "2026-07-25T07:00:00Z",
      updated_at: "2026-07-25T07:00:00Z",
    });
    mocks.cancel.mockResolvedValue({
      id: 71,
      status: "cancelled",
      phase: "validation",
    });
    mocks.runDetail.mockResolvedValue({
      id: 71,
      status: "running",
      phase: "validation",
      baseline_tagger_version_id: 42,
      candidate_tagger_version_id: 44,
      gold_set_version_id: 7,
      cohort: { source: "scheduled" },
      objective: { policy: "balanced" },
      search_budget: { max_trials: 24, sealed_holdout_queries: 1 },
      trigger: "scheduled",
      summary: {},
      trials: TRIALS,
      created_at: "2026-07-25T06:00:00Z",
      updated_at: "2026-07-25T06:20:00Z",
    });
    mocks.compare.mockResolvedValue({
      dimensions: [
        {
          dimension: "orchestration",
          before: "weak_llm",
          after: "weak_to_strong_critic",
        },
      ],
      metric_deltas: { macro_f1: 0.024, p95_latency_ms: 84 },
      reward_deltas: { quality_delta: 0.02, cost_delta: -0.4 },
      improved_badcase_count: 12,
      regressed_badcase_count: 1,
      recommendation: {
        trial_id: 13,
        basis: "feasibility_then_quality_review_latency_cost",
      },
      status: "success",
      summary: "trial 13 ranks first",
      next_actions: ["evaluate_recommended_candidate"],
      artifacts: ["tag_optimization_trial:11", "tag_optimization_trial:13"],
    });
  });

  it("shows unbiased quality, feedback health, badcases and release truth support", async () => {
    renderPanel();

    expect(await screen.findByText("harness-2.3")).toBeVisible();
    expect(screen.getByText("88.4%")).toBeVisible();
    expect(screen.getByText("购买意向被识别为随便看看")).toBeVisible();
    expect(screen.getByText("5,480")).toBeVisible();
    expect(screen.getByText("1,260")).toBeVisible();
    expect(screen.getByText("516")).toBeVisible();
    expect(screen.getByText("88")).toBeVisible();
    expect(screen.getByText("等待 48 小时时间门禁")).toBeVisible();
  });

  it("shows bounded run progress and a six-dimension candidate diff", async () => {
    renderPanel();

    expect(await screen.findByText("运行 #71")).toBeVisible();
    expect(
      screen.getByRole("progressbar", { name: "优化运行 71 进度" }),
    ).toHaveAttribute("aria-valuenow", "18");
    expect(
      screen.getByRole("list", { name: "候选 Harness 六维差异" }),
    ).toBeVisible();
    expect(screen.getByText("上下文与示例")).toBeVisible();
    expect(screen.getByText("工具与模型")).toBeVisible();
    expect(screen.getByText("生成策略")).toBeVisible();
    expect(screen.getByText("编排 DAG")).toBeVisible();
    expect(screen.getByText("经验检索")).toBeVisible();
    expect(screen.getByText("输出校验与回退")).toBeVisible();
    expect(screen.getByText("weak_to_strong_critic")).toBeVisible();
    expect(screen.getAllByText("本次未变更")).toHaveLength(5);
    expect(screen.getByText("改善 31")).toBeVisible();
    expect(screen.getByText("退化 2")).toBeVisible();
  });

  it("renders the sealed-holdout verdict and a deployment CTA on passed completed runs", async () => {
    mocks.runs.mockResolvedValue({
      items: [
        {
          id: 74,
          status: "completed",
          phase: "completed",
          baseline_tagger_version_id: 42,
          baseline_version: "harness-2.3",
          candidate_tagger_version_id: 45,
          winner_tagger_version_id: 45,
          candidate_version: "harness-2.4",
          gold_set_version_id: 7,
          cohort: { source: "eligible_feedback" },
          objective: { policy: "balanced" },
          search_budget: { max_trials: 24, sealed_holdout_queries: 1 },
          trigger: "manual",
          summary: {
            completed_trials: 24,
            trial_count: 24,
            evaluation_run_id: 91,
            holdout_passed: true,
          },
          next_actions: ["start_shadow_deployment"],
          created_at: "2026-07-25T06:00:00Z",
          updated_at: "2026-07-25T08:00:00Z",
        },
      ],
      total: 1,
    });
    renderPanel();

    expect(await screen.findByText("Sealed Holdout 通过")).toBeVisible();
    expect(screen.getByText("评估 #91")).toBeVisible();
    expect(
      screen.getByRole("link", { name: "创建影子部署" }),
    ).toHaveAttribute(
      "href",
      "#/tag-governance?tab=deployments&deploy_evaluation_id=91",
    );
    expect(
      screen.getByRole("link", { name: "harness-2.4" }),
    ).toHaveAttribute("href", "#/tag-governance?tab=taggers");
  });

  it("explains a failed sealed holdout and reopens the optimizer with the same cohort", async () => {
    const user = userEvent.setup();
    mocks.runs.mockResolvedValue({
      items: [
        {
          id: 75,
          status: "completed",
          phase: "completed",
          baseline_tagger_version_id: 42,
          candidate_tagger_version_id: 46,
          winner_tagger_version_id: null,
          candidate_version: "harness-2.5-c2",
          gold_set_version_id: 7,
          cohort: {
            source: "tag_insights",
            filters: { store_ids: ["S9"] },
          },
          objective: { policy: "balanced" },
          search_budget: { max_trials: 24, sealed_holdout_queries: 1 },
          trigger: "insight",
          summary: {
            completed_trials: 24,
            trial_count: 24,
            evaluation_run_id: 92,
            holdout_passed: false,
          },
          next_actions: ["inspect_regressions", "create_new_optimization_run"],
          failure_reason: "关键召回下降超出硬门禁",
          created_at: "2026-07-25T06:00:00Z",
          updated_at: "2026-07-25T08:00:00Z",
        },
      ],
      total: 1,
    });
    renderPanel();

    expect(await screen.findByText("Sealed Holdout 未通过")).toBeVisible();
    expect(screen.getByText("关键召回下降超出硬门禁")).toBeVisible();
    expect(
      screen.queryByRole("link", { name: "创建影子部署" }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "基于运行 75 重新优化" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "启动自进化优化",
    });
    // 重新优化沿用失败运行的 cohort，让重跑针对同一批反馈样本。
    expect(dialog).toHaveTextContent("来自标签洞察");
    expect(within(dialog).getByText("门店 S9")).toBeVisible();
  });

  it("compares two trials of a run and shows the metric-by-metric verdict", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(
      await screen.findByRole("button", { name: "对比运行 71 的 Trial" }),
    );

    const dialog = await screen.findByRole("dialog", {
      name: "对比运行 #71 的 Trial",
    });
    await waitFor(() => expect(mocks.runDetail).toHaveBeenCalledWith(71));
    // 默认摆出「基线 vs 本轮选中的候选」，也就是运行卡片上已展示的那一对。
    expect(
      within(dialog).getByRole("combobox", { name: "左侧 Trial" }),
    ).toHaveValue("11");
    expect(
      within(dialog).getByRole("combobox", { name: "右侧 Trial" }),
    ).toHaveValue("13");
    expect(mocks.compare).not.toHaveBeenCalled();

    await user.selectOptions(
      within(dialog).getByRole("combobox", { name: "左侧 Trial" }),
      "12",
    );
    await user.click(within(dialog).getByRole("button", { name: "对比 Trial" }));

    await waitFor(() => expect(mocks.compare).toHaveBeenCalledWith(71, 12, 13));
    const result = await screen.findByRole("region", {
      name: "运行 71 的 Trial 对比结果",
    });
    expect(
      within(result).getByText(/推荐 Trial 3 · 编排 DAG · 引入弱到强 critic/),
    ).toBeVisible();
    const metrics = within(result).getByRole("group", {
      name: "Trial 指标差异",
    });
    expect(within(metrics).getByText("Macro F1")).toBeVisible();
    expect(within(metrics).getByText("+2.4%")).toBeVisible();
    expect(within(metrics).getByText("P95 时延")).toBeVisible();
    expect(within(metrics).getByText("+84 ms")).toBeVisible();
    const rewards = within(result).getByRole("group", {
      name: "Trial 奖励向量差异",
    });
    expect(within(rewards).getByText("质量奖励")).toBeVisible();
    expect(within(rewards).getByText("+2%")).toBeVisible();
    expect(within(rewards).getByText("成本奖励")).toBeVisible();
    expect(within(rewards).getByText("-0.4")).toBeVisible();
    expect(within(result).getByText("改善 12")).toBeVisible();
    expect(within(result).getByText("退化 1")).toBeVisible();
    expect(
      within(result).getByRole("list", { name: "Trial 对比六维差异" }),
    ).toBeVisible();
  });

  it("refuses to compare a trial with itself, matching the endpoint's own rule", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(
      await screen.findByRole("button", { name: "对比运行 71 的 Trial" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "对比运行 #71 的 Trial",
    });
    await user.selectOptions(
      await within(dialog).findByRole("combobox", { name: "左侧 Trial" }),
      "13",
    );

    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "两侧必须选择不同的 Trial",
    );
    expect(
      within(dialog).getByRole("button", { name: "对比 Trial" }),
    ).toBeDisabled();
    expect(mocks.compare).not.toHaveBeenCalled();
  });

  it("surfaces a failed trial comparison inside the dialog", async () => {
    const user = userEvent.setup();
    mocks.compare.mockRejectedValueOnce(new Error("优化运行不存在"));
    renderPanel();

    await user.click(
      await screen.findByRole("button", { name: "对比运行 71 的 Trial" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "对比运行 #71 的 Trial",
    });
    await user.click(
      await within(dialog).findByRole("button", { name: "对比 Trial" }),
    );

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "优化运行不存在",
    );
    expect(
      screen.queryByRole("region", { name: "运行 71 的 Trial 对比结果" }),
    ).not.toBeInTheDocument();
  });

  it("asks for explicit confirmation before cancelling an active run", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(
      await screen.findByRole("button", { name: "取消优化运行 71" }),
    );

    const dialog = await screen.findByRole("dialog", {
      name: "确认取消优化运行 #71",
    });
    expect(dialog).toHaveTextContent("关联任务");
    expect(mocks.cancel).not.toHaveBeenCalled();

    await user.click(
      within(dialog).getByRole("button", { name: "继续运行" }),
    );
    expect(
      screen.queryByRole("dialog", { name: "确认取消优化运行 #71" }),
    ).not.toBeInTheDocument();
    expect(mocks.cancel).not.toHaveBeenCalled();
  });

  it("cancels only after the admin confirms the destructive action", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(
      await screen.findByRole("button", { name: "取消优化运行 71" }),
    );
    await user.click(
      within(
        screen.getByRole("dialog", {
          name: "确认取消优化运行 #71",
        }),
      ).getByRole("button", { name: "确认取消运行" }),
    );

    await waitFor(() => expect(mocks.cancel).toHaveBeenCalledWith(71));
    expect(await screen.findByText("优化运行 #71 已取消")).toBeVisible();
  });

  it("starts a bounded optimization from a carried insight cohort without raw ids", async () => {
    const user = userEvent.setup();
    renderPanel({
      initialDialog: "optimize",
      initialCohort: {
        source: "tag_insights",
        filters: { store_ids: ["S1"], scenarios: ["automotive"] },
        conflict_only: true,
      },
    });

    const dialog = await screen.findByRole("dialog", {
      name: "启动自进化优化",
    });
    expect(dialog).toHaveTextContent("来自标签洞察");
    expect(within(dialog).getByText("门店 S1")).toBeVisible();
    expect(within(dialog).getByText("场景 automotive")).toBeVisible();
    expect(within(dialog).getByText("仅冲突 / 缺失样本")).toBeVisible();
    expect(await within(dialog).findByText("harness-2.3")).toBeVisible();
    expect(
      screen.queryByRole("spinbutton", { name: /版本 ID/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", { name: "错误样本 JSON" }),
    ).not.toBeInTheDocument();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "优化目标" }),
      "quality_first",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "搜索预算" }),
      "16",
    );
    await user.click(screen.getByRole("button", { name: "启动优化运行" }));

    await waitFor(() => {
      expect(mocks.create).toHaveBeenCalledWith({
        cohort: {
          source: "tag_insights",
          filters: { store_ids: ["S1"], scenarios: ["automotive"] },
          conflict_only: true,
        },
        target_policy: { policy: "quality_first" },
        search_budget: {
          max_trials: 16,
          sealed_holdout_queries: 1,
        },
      });
      expect(mocks.create.mock.calls.at(-1)?.[0]).not.toHaveProperty(
        "gold_set_version_id",
      );
      expect(mocks.create.mock.calls.at(-1)?.[0]).not.toHaveProperty(
        "objective",
      );
      expect(mocks.create.mock.calls.at(-1)?.[0]).not.toHaveProperty(
        "trigger",
      );
    });
    expect(await screen.findByText("优化运行 #72 已进入队列")).toBeVisible();
  });

  it("shows create failures inside the optimizer dialog and can retry there", async () => {
    const user = userEvent.setup();
    mocks.create.mockRejectedValueOnce(new Error("优化服务暂不可用"));
    renderPanel({ initialDialog: "optimize" });

    const dialog = await screen.findByRole("dialog", {
      name: "启动自进化优化",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "启动优化运行" }),
    );

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "优化服务暂不可用",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "重试启动" }),
    );

    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("优化运行 #72 已进入队列")).toBeVisible();
  });

  it("explains when trusted-feedback coverage blocks an optimization run", async () => {
    const user = userEvent.setup();
    mocks.create.mockResolvedValueOnce({
      id: 73,
      status: "completed",
      phase: "prepare",
      baseline_tagger_version_id: 42,
      gold_set_version_id: 7,
      cohort: { source: "eligible_feedback" },
      objective: { policy: "balanced" },
      search_budget: { max_trials: 24, sealed_holdout_queries: 1 },
      trigger: "manual",
      summary: {
        completed_trials: 0,
        trial_count: 0,
        new_feedback_count: 12,
        feedback_by_tag: { intent: 12 },
        coverage_gate_passed: false,
        coverage_blockers: [
          "new_t2_t3_feedback_below_200",
          "tag_support_below_30:intent",
        ],
        diagnostic_only: true,
      },
      created_at: "2026-07-25T07:00:00Z",
      updated_at: "2026-07-25T07:00:00Z",
    });
    renderPanel({ initialDialog: "optimize" });

    const dialog = await screen.findByRole("dialog", {
      name: "启动自进化优化",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "启动优化运行" }),
    );

    expect(
      await screen.findByText("优化运行 #73 未启动：可信反馈覆盖不足"),
    ).toBeVisible();
    expect(
      screen.queryByText("优化运行 #73 已完成（演示数据）"),
    ).not.toBeInTheDocument();
  });

  it("blocks optimization when the server has no complete frozen gold set", async () => {
    mocks.overview.mockResolvedValueOnce({
      production_harness: null,
      recommended_gold_set_version_id: null,
      recommended_gold_set_label: null,
      quality: {},
      feedback: {
        eligible_count: 236,
        new_since_last_run: 68,
        representative_audit_count: 120,
        adjudicated_count: 44,
        next_run_eligible: false,
        blockers: ["complete_gold_set_missing"],
      },
      drift: {
        status: "stable",
        affected_slices: [],
      },
      release: null,
    });

    renderPanel({ initialDialog: "optimize" });

    expect(
      await screen.findByRole("alert", {
        name: "",
      }),
    ).toHaveTextContent("没有可发布的完整金标");
    expect(
      screen.getByRole("button", { name: "启动优化运行" }),
    ).toBeDisabled();
    expect(mocks.create).not.toHaveBeenCalled();
  });
});
