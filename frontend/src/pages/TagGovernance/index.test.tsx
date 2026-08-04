import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TagGovernancePage from "./index";
import { useAuthStore } from "@/stores/auth";
import type { TagDeployment } from "@/types/api";

vi.mock("@/api/services", () => ({
  approveTagDeployment: vi.fn(),
  createTagDeployment: vi.fn(),
  createTagEvaluation: vi.fn(),
  createTagGoldSet: vi.fn(),
  createTagSchema: vi.fn(),
  createTagSchemaVersion: vi.fn(),
  createTaggerVersion: vi.fn(),
  createTagOptimizationRun: vi.fn(),
  freezeTagGoldSet: vi.fn(),
  getTagEvolutionOverview: vi.fn(),
  getTagOptimizationRun: vi.fn(),
  listTagAuditEvents: vi.fn(),
  listTagBadcases: vi.fn(),
  listTagJobs: vi.fn(),
  listTagDeploymentObservations: vi.fn(),
  listTagDeployments: vi.fn(),
  listTagEvaluations: vi.fn(),
  listTagGoldSets: vi.fn(),
  listTagOptimizationRuns: vi.fn(),
  listTagSchemas: vi.fn(),
  listTaggerVersions: vi.fn(),
  publishTagSchemaVersion: vi.fn(),
  resumeTagDeployment: vi.fn(),
  rollbackTagDeployment: vi.fn(),
}));

import {
  approveTagDeployment,
  createTagDeployment,
  createTagEvaluation,
  createTagGoldSet,
  createTagSchema,
  createTagSchemaVersion,
  createTaggerVersion,
  createTagOptimizationRun,
  freezeTagGoldSet,
  getTagEvolutionOverview,
  listTagAuditEvents,
  listTagBadcases,
  listTagJobs,
  listTagDeploymentObservations,
  listTagDeployments,
  listTagEvaluations,
  listTagGoldSets,
  listTagOptimizationRuns,
  listTagSchemas,
  listTaggerVersions,
  resumeTagDeployment,
  rollbackTagDeployment,
} from "@/api/services";

const mocks = {
  approveDeployment: approveTagDeployment as unknown as ReturnType<
    typeof vi.fn
  >,
  createDeployment: createTagDeployment as unknown as ReturnType<typeof vi.fn>,
  createEvaluation: createTagEvaluation as unknown as ReturnType<typeof vi.fn>,
  createGoldSet: createTagGoldSet as unknown as ReturnType<typeof vi.fn>,
  createSchema: createTagSchema as unknown as ReturnType<typeof vi.fn>,
  createSchemaVersion: createTagSchemaVersion as unknown as ReturnType<
    typeof vi.fn
  >,
  createTaggerVersion: createTaggerVersion as unknown as ReturnType<
    typeof vi.fn
  >,
  createOptimization:
    createTagOptimizationRun as unknown as ReturnType<typeof vi.fn>,
  freezeGoldSet: freezeTagGoldSet as unknown as ReturnType<typeof vi.fn>,
  audit: listTagAuditEvents as unknown as ReturnType<typeof vi.fn>,
  evolutionOverview:
    getTagEvolutionOverview as unknown as ReturnType<typeof vi.fn>,
  badcases: listTagBadcases as unknown as ReturnType<typeof vi.fn>,
  jobs: listTagJobs as unknown as ReturnType<typeof vi.fn>,
  optimizationRuns:
    listTagOptimizationRuns as unknown as ReturnType<typeof vi.fn>,
  deploymentObservations:
    listTagDeploymentObservations as unknown as ReturnType<typeof vi.fn>,
  deployments: listTagDeployments as unknown as ReturnType<typeof vi.fn>,
  evaluations: listTagEvaluations as unknown as ReturnType<typeof vi.fn>,
  goldSets: listTagGoldSets as unknown as ReturnType<typeof vi.fn>,
  resumeDeployment: resumeTagDeployment as unknown as ReturnType<typeof vi.fn>,
  rollbackDeployment: rollbackTagDeployment as unknown as ReturnType<
    typeof vi.fn
  >,
  schemas: listTagSchemas as unknown as ReturnType<typeof vi.fn>,
  taggers: listTaggerVersions as unknown as ReturnType<typeof vi.fn>,
};

function renderPage(initialEntry = "/tag-governance") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function LocationProbe() {
    const location = useLocation();
    return <output aria-label="当前路径">{location.pathname}</output>;
  }
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <TagGovernancePage />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function deploymentFixture(
  overrides: Partial<TagDeployment> = {},
): TagDeployment {
  return {
    id: 9,
    tenant_id: "tenant-a",
    tagger_version_id: 42,
    evaluation_run_id: 7,
    baseline_tagger_version_id: 41,
    status: "canary_25",
    traffic_percent: 25,
    revision: 7,
    promotion_paused: true,
    pause_reason: "distribution drift requires review",
    created_by: 1,
    approved_by: null,
    approved_at: null,
    rolled_back_by: null,
    rolled_back_at: null,
    rollback_reason: null,
    created_at: "2026-07-25T04:00:00Z",
    updated_at: "2026-07-25T04:00:00Z",
    ...overrides,
  };
}

describe("TagGovernancePage", () => {
  beforeEach(() => {
    useAuthStore.setState({
      token: "admin-token",
      refreshToken: "admin-refresh",
      user: {
        id: 1,
        name: "平台管理员",
        email: "admin@example.com",
        role: "admin",
        tenant_id: "tenant-a",
      },
      isAuthenticated: true,
    });
    Object.values(mocks).forEach((mock) => mock.mockReset());
    mocks.schemas.mockResolvedValue({
      items: [
        {
          id: 1,
          tenant_id: "tenant-a",
          key: "sales-dialogue",
          name: "销售对话标签",
          description: "统一金店与汽车销售语义",
          created_at: "2026-07-25T01:00:00Z",
          updated_at: "2026-07-25T02:00:00Z",
          versions: [
            {
              id: 11,
              schema_id: 1,
              version: "1.2.0",
              status: "published",
              checksum: "abc",
              definitions: [
                {
                  key: "intent",
                  name: "客户意图",
                  category: "sales",
                  value_type: "enum",
                  allowed_values: ["browse", "purchase"],
                  subject_types: ["dialogue_unit"],
                  scenarios: ["gold", "automotive"],
                  evidence_required: true,
                  critical: false,
                  threshold: 0.7,
                },
              ],
              created_at: "2026-07-25T01:00:00Z",
              published_at: "2026-07-25T02:00:00Z",
            },
          ],
        },
      ],
      total: 1,
    });
    mocks.taggers.mockResolvedValue({
      items: [
        {
          id: 41,
          tenant_id: "tenant-a",
          schema_version_id: 11,
          version: "tagger-2.2",
          engine: "hybrid",
          prompt_content: "extract",
          rule_bundle: {},
          model_version: "model-a",
          thresholds: { intent: 0.7 },
          checksum: "baseline",
          status: "qualified",
          created_at: "2026-07-24T02:00:00Z",
          updated_at: "2026-07-24T02:00:00Z",
        },
        {
          id: 42,
          tenant_id: "tenant-a",
          schema_version_id: 11,
          version: "tagger-2.3",
          engine: "hybrid",
          prompt_content: "extract",
          rule_bundle: {},
          model_version: "model-a",
          thresholds: { intent: 0.7 },
          checksum: "def",
          status: "qualified",
          created_at: "2026-07-25T02:00:00Z",
          updated_at: "2026-07-25T02:00:00Z",
        },
      ],
      total: 1,
    });
    mocks.evaluations.mockResolvedValue({
      items: [
        {
          id: 7,
          tenant_id: "tenant-a",
          tagger_version_id: 42,
          baseline_tagger_version_id: 41,
          gold_set_version_id: 3,
          status: "completed",
          passed: true,
          metrics: {
            macro_f1: 0.88,
            critical_recall: 0.97,
            evidence_coverage: 0.99,
            error_rate: 0.002,
          },
          baseline_metrics: { macro_f1: 0.86 },
          gates: [
            {
              code: "macro_f1",
              passed: true,
              actual: 0.88,
              threshold: 0.85,
              message: "通过",
            },
          ],
          created_at: "2026-07-25T03:00:00Z",
          updated_at: "2026-07-25T03:00:00Z",
        },
      ],
      total: 1,
    });
    mocks.goldSets.mockResolvedValue({
      items: [
        {
          id: 5,
          tenant_id: "tenant-a",
          key: "sales-holdout",
          name: "销售对话金标集",
          description: "人工复核样本",
          schema_version_id: 11,
          created_by: 1,
          created_at: "2026-07-25T03:00:00Z",
          updated_at: "2026-07-25T03:00:00Z",
        },
      ],
      total: 1,
    });
    mocks.deployments.mockResolvedValue({
      items: [
        {
          id: 9,
          tenant_id: "tenant-a",
          tagger_version_id: 42,
          evaluation_run_id: 7,
          baseline_tagger_version_id: 41,
          status: "canary_25",
          traffic_percent: 25,
          revision: 3,
          promotion_paused: false,
          pause_reason: null,
          created_by: 1,
          approved_by: null,
          approved_at: null,
          rolled_back_by: null,
          rolled_back_at: null,
          rollback_reason: null,
          created_at: "2026-07-25T04:00:00Z",
          updated_at: "2026-07-25T04:00:00Z",
        },
      ],
      total: 1,
    });
    mocks.deploymentObservations.mockResolvedValue({
      items: [
        {
          id: 901,
          tenant_id: "tenant-a",
          deployment_id: 9,
          stage: "canary_5",
          window_start: "2026-07-25T03:50:00Z",
          window_end: "2026-07-25T03:55:00Z",
          sample_count: 120,
          metrics: {
            error_rate: 0.018,
            drift_max_jsd: 0.072,
            drift_paired_sample_count: 120,
            drift_eligible_tag_count: 1,
            drift_affected_tags: [],
          },
          breach_codes: [],
          action: "observe",
          is_demo: true,
          data_source: "demo",
          created_at: "2026-07-25T03:55:00Z",
          updated_at: "2026-07-25T03:55:00Z",
        },
        {
          id: 902,
          tenant_id: "tenant-a",
          deployment_id: 9,
          stage: "canary_25",
          window_start: "2026-07-25T03:55:00Z",
          window_end: "2026-07-25T04:00:00Z",
          sample_count: 380,
          metrics: {
            error_rate: 0.012,
            drift_max_jsd: 0.046,
            drift_paired_sample_count: 380,
            drift_eligible_tag_count: 1,
            drift_affected_tags: [],
          },
          breach_codes: [],
          action: "observe",
          is_demo: true,
          data_source: "demo",
          created_at: "2026-07-25T04:00:00Z",
          updated_at: "2026-07-25T04:00:00Z",
        },
      ],
      total: 2,
    });
    mocks.audit.mockResolvedValue({
      items: [
        {
          id: 99,
          tenant_id: "tenant-a",
          action: "tagger.created",
          actor_user_id: 1,
          resource_type: "tagger_version",
          resource_id: "42",
          detail: { version: "tagger-2.3" },
          created_at: "2026-07-25T05:00:00Z",
        },
      ],
      total: 1,
    });
    mocks.jobs.mockResolvedValue({
      items: [
        {
          id: 77,
          tenant_id: "tenant-a",
          job_type: "extract",
          status: "running",
          scope: { reception_ids: [5] },
          tagger_version_id: 42,
          origin: "manual",
          total_items: 40,
          completed_items: 12,
          failed_items: 1,
          failed_subset: [],
          attempt_count: 1,
          max_attempts: 3,
          revision: 1,
          lease_owner: "worker-1",
          lease_expires_at: null,
          next_attempt_at: null,
          last_error_code: null,
          last_error_message: null,
          created_at: "2026-07-25T06:00:00Z",
          updated_at: "2026-07-25T06:05:00Z",
          finished_at: null,
        },
      ],
      total: 1,
    });
    mocks.evolutionOverview.mockResolvedValue({
      production_harness: {
        id: 42,
        version: "harness-2.3",
        status: "production",
      },
      recommended_gold_set_version_id: 7,
      recommended_gold_set_label: "销售对话完整金标 · 2026.07",
      quality: { unbiased_macro_f1: 0.884 },
      feedback: {
        eligible_count: 236,
        new_since_last_run: 68,
        representative_audit_count: 120,
        adjudicated_count: 44,
        coverage_rate: 0.92,
        next_run_eligible: true,
        blockers: [],
      },
      drift: { status: "stable", input_psi: 0.08, output_jsd: 0.03 },
      release: {
        stage: "shadow",
        served_count: 800,
        paired_count: 620,
        audited_count: 110,
        adjudicated_count: 30,
        waiting_reasons: ["还需 390 个服务样本"],
        promotion_paused: false,
      },
    });
    mocks.badcases.mockResolvedValue({ items: [], total: 0 });
    mocks.optimizationRuns.mockResolvedValue({ items: [], total: 0 });
  });

  it("opens the governed optimizer directly from an insight action deep link", async () => {
    renderPage(
      `/tag-governance?tab=evolution&mode=optimize&cohort=${encodeURIComponent(
        JSON.stringify({
          source: "tag_insights",
          filters: { store_ids: ["S1"] },
        }),
      )}`,
    );

    expect(
      await screen.findByRole("tab", { name: "自进化" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByRole("dialog", { name: "启动自进化优化" }),
    ).toBeVisible();
  });

  it("uses seven real, keyboard-operable tabs and mounts one panel at a time", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(
      await screen.findByRole("heading", { name: "标签治理中心" }),
    ).toBeInTheDocument();
    const tablist = screen.getByRole("tablist", { name: "标签治理视图" });
    expect(within(tablist).getAllByRole("tab")).toHaveLength(7);
    expect(
      within(tablist).getByRole("tab", { name: "标签体系" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("销售对话标签")).toBeInTheDocument();
    expect(screen.queryByText("tagger-2.3")).not.toBeInTheDocument();

    const taxonomyTab = within(tablist).getByRole("tab", {
      name: "标签体系",
    });
    taxonomyTab.focus();
    await user.keyboard("{ArrowRight}");

    expect(
      within(tablist).getByRole("tab", { name: "抽取版本" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("tagger-2.3")).toBeInTheDocument();
    expect(screen.queryByText("销售对话标签")).not.toBeInTheDocument();

    await user.keyboard("{End}");
    expect(
      within(tablist).getByRole("tab", { name: "审计" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("tagger.created")).toBeInTheDocument();
  });

  it("keeps inspector governance actions read-only outside review, gold and evaluation", async () => {
    const user = userEvent.setup();
    useAuthStore.setState({
      token: "inspector-token",
      refreshToken: "inspector-refresh",
      user: {
        id: 2,
        name: "质检员",
        email: "inspector@example.com",
        role: "inspector",
        tenant_id: "tenant-a",
      },
      isAuthenticated: true,
    });
    mocks.deployments.mockResolvedValue({
      items: [deploymentFixture()],
      total: 1,
    });
    renderPage("/tag-governance?tab=taxonomy");

    expect(await screen.findByText("销售对话标签")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "新建标签体系" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: "为销售对话标签创建版本",
      }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "抽取版本" }));
    expect(await screen.findByText("tagger-2.3")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "创建候选版本" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "自动优化候选" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "评估实验" }));
    expect(
      await screen.findByRole("button", { name: "运行评估" }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "新建金标集" }),
    ).toBeVisible();

    await user.click(screen.getByRole("tab", { name: "发布监控" }));
    expect(await screen.findByText("灰度流量 25%")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "创建影子部署" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: "推进部署 9 至管理员审批",
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "回滚部署 9" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: "完成复核并恢复部署 9",
      }),
    ).not.toBeInTheDocument();
  });

  it("does not open an admin-only optimizer from a deep link for inspectors", async () => {
    useAuthStore.setState({
      token: "inspector-token",
      refreshToken: "inspector-refresh",
      user: {
        id: 2,
        name: "质检员",
        email: "inspector@example.com",
        role: "inspector",
        tenant_id: "tenant-a",
      },
      isAuthenticated: true,
    });
    renderPage("/tag-governance?tab=evolution&mode=optimize");

    expect(await screen.findByText("harness-2.3")).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: "启动自进化优化" }),
    ).not.toBeInTheDocument();
  });

  it("creates a taxonomy and an immutable schema version through real mutations", async () => {
    const user = userEvent.setup();
    mocks.createSchema.mockResolvedValue({
      id: 2,
      key: "auto-sales",
      name: "汽车销售标签",
    });
    mocks.createSchemaVersion.mockResolvedValue({
      id: 12,
      schema_id: 1,
      version: "1.3.0",
      status: "draft",
    });
    renderPage();
    await screen.findByText("销售对话标签");

    await user.click(screen.getByRole("button", { name: "新建标签体系" }));
    await user.type(screen.getByRole("textbox", { name: "体系键" }), "auto-sales");
    await user.type(screen.getByRole("textbox", { name: "体系名称" }), "汽车销售标签");
    await user.click(screen.getByRole("button", { name: "保存标签体系" }));
    await waitFor(() => {
      expect(mocks.createSchema).toHaveBeenCalledWith({
        key: "auto-sales",
        name: "汽车销售标签",
        description: undefined,
      });
    });

    await user.click(
      screen.getByRole("button", { name: "为销售对话标签创建版本" }),
    );
    await user.type(screen.getByRole("textbox", { name: "体系版本号" }), "1.3.0");
    fireEvent.change(screen.getByRole("textbox", { name: "标签定义 JSON" }), {
      target: {
        value: JSON.stringify([
          {
            key: "intent",
            name: "客户意图",
            category: "sales",
            value_type: "enum",
            allowed_values: ["browse", "purchase"],
            subject_types: ["dialogue_unit"],
            scenarios: ["automotive"],
            evidence_required: true,
            critical: false,
            required: true,
            threshold: 0.75,
          },
        ]),
      },
    });
    await user.click(screen.getByRole("button", { name: "保存体系版本" }));
    await waitFor(() => {
      expect(mocks.createSchemaVersion).toHaveBeenCalledWith(1, {
        version: "1.3.0",
        definitions: [
          expect.objectContaining({
            key: "intent",
            threshold: 0.75,
          }),
        ],
      });
    });
  });

  it("shows evaluation quality gates and release traffic as operational evidence", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("销售对话标签");

    await user.click(screen.getByRole("tab", { name: "评估实验" }));
    expect(await screen.findByText("Macro F1")).toBeInTheDocument();
    expect(screen.getByText("88%")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "质量门禁通过" })).toBeVisible();

    await user.click(screen.getByRole("tab", { name: "发布监控" }));
    expect(await screen.findByText("灰度流量 25%")).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "部署 9 流量" }),
    ).toHaveAttribute("aria-valuenow", "25");
    expect(
      await screen.findByRole("img", {
        name: "部署 9 error_rate 5分钟趋势",
      }),
    ).toBeVisible();
    expect(
      screen.getByRole("img", {
        name: "部署 9 Jensen-Shannon 漂移趋势",
      }),
    ).toBeVisible();
    expect(screen.getByText("最新 JSD 0.046")).toBeVisible();
    expect(screen.getByText("演示数据 · 5 分钟窗口")).toBeVisible();
    expect(screen.getByText("380 样本")).toBeVisible();
  });

  it("loads observations eagerly only for actively monitored deployments", async () => {
    const user = userEvent.setup();
    mocks.deployments.mockResolvedValueOnce({
      items: [
        {
          id: 10,
          tenant_id: "tenant-a",
          tagger_version_id: 42,
          evaluation_run_id: 7,
          baseline_tagger_version_id: 41,
          status: "rolled_back",
          traffic_percent: 0,
          revision: 6,
          promotion_paused: true,
          pause_reason: "drift",
          created_by: 1,
          approved_by: null,
          approved_at: null,
          rolled_back_by: 1,
          rolled_back_at: "2026-07-25T04:05:00Z",
          rollback_reason: "关键指标回退",
          created_at: "2026-07-25T04:00:00Z",
          updated_at: "2026-07-25T04:05:00Z",
        },
      ],
      total: 1,
    });
    renderPage();
    await screen.findByText("销售对话标签");
    await user.click(screen.getByRole("tab", { name: "发布监控" }));

    const historyButton = await screen.findByRole("button", {
      name: "查看历史发布观测",
    });
    expect(mocks.deploymentObservations).not.toHaveBeenCalled();

    await user.click(historyButton);
    await waitFor(() => {
      expect(mocks.deploymentObservations).toHaveBeenCalledWith(10);
    });
  });

  it("starts an asynchronous evaluation and links to its observable run", async () => {
    const user = userEvent.setup();
    mocks.createEvaluation.mockResolvedValue({
      job_id: 88,
      evaluation: {
        id: 8,
        status: "queued",
        tagger_version_id: 42,
        baseline_tagger_version_id: 41,
        gold_set_version_id: 7,
      },
    });
    renderPage();
    await screen.findByText("销售对话标签");

    await user.click(screen.getByRole("tab", { name: "评估实验" }));
    await screen.findByText("Macro F1");
    await user.click(screen.getByRole("button", { name: "运行评估" }));
    await user.type(
      screen.getByRole("spinbutton", { name: "候选抽取版本 ID" }),
      "42",
    );
    await user.type(
      screen.getByRole("spinbutton", { name: "金标集版本 ID" }),
      "7",
    );
    await user.type(
      screen.getByRole("spinbutton", { name: "基线抽取版本 ID" }),
      "41",
    );
    await user.click(screen.getByRole("button", { name: "启动评估任务" }));

    await waitFor(() => {
      expect(mocks.createEvaluation).toHaveBeenCalledWith(
        {
          tagger_version_id: 42,
          gold_set_version_id: 7,
          baseline_tagger_version_id: 41,
        },
        expect.stringMatching(/^evaluation-/),
      );
    });
    expect(await screen.findByLabelText("当前路径")).toHaveTextContent(
      "/tag-runs/88",
    );
  });

  it("labels challenge-lane evaluations as non-deployable and routes to evolution", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("销售对话标签");

    await user.click(screen.getByRole("tab", { name: "评估实验" }));
    await screen.findByText("Macro F1");

    // 公开「运行评估」永远走 challenge 通道，后端会 409 拒绝其部署，
    // 卡片必须提前说明去向而不是留给部署时报错。
    expect(
      screen.getByText(/仅验证结果，不能直接用于部署/),
    ).toBeVisible();
    expect(
      screen.getByRole("link", { name: "前往自进化" }),
    ).toHaveAttribute("href", "/tag-governance?tab=evolution");
    expect(
      screen.queryByRole("link", { name: "创建影子部署" }),
    ).not.toBeInTheDocument();
  });

  it("offers a shadow-deployment CTA on passed sealed-holdout evaluations that prefills the id", async () => {
    const user = userEvent.setup();
    mocks.evaluations.mockResolvedValue({
      items: [
        {
          id: 7,
          tenant_id: "tenant-a",
          tagger_version_id: 42,
          baseline_tagger_version_id: 41,
          gold_set_version_id: 3,
          status: "completed",
          passed: true,
          metrics: {
            macro_f1: 0.88,
            critical_recall: 0.97,
            evidence_coverage: 0.99,
            error_rate: 0.002,
            evaluation_lane: "holdout",
            sealed_release: true,
          },
          baseline_metrics: { macro_f1: 0.86 },
          gates: [
            {
              code: "sealed_release",
              passed: true,
              actual: null,
              threshold: null,
              message: "Sealed Holdout 仅公开聚合结果",
            },
          ],
          created_at: "2026-07-25T03:00:00Z",
          updated_at: "2026-07-25T03:00:00Z",
        },
      ],
      total: 1,
    });
    renderPage("/tag-governance?tab=evaluations");
    await screen.findByText("Macro F1");

    expect(
      screen.queryByText(/仅验证结果，不能直接用于部署/),
    ).not.toBeInTheDocument();
    const cta = screen.getByRole("link", { name: "创建影子部署" });
    expect(cta).toHaveAttribute(
      "href",
      "/tag-governance?tab=deployments&deploy_evaluation_id=7",
    );

    await user.click(cta);
    const dialog = await screen.findByRole("dialog", {
      name: "创建影子部署",
    });
    expect(
      within(dialog).getByRole("spinbutton", { name: "部署评估 ID" }),
    ).toHaveValue(7);
  });

  it("shows the real sealed-holdout conflict instead of the stale-revision copy", async () => {
    const user = userEvent.setup();
    // 后端 create_deployment 对 challenge 评估的 409（api/tag_governance.py
    // `_domain` 统一映射为 TAG_GOVERNANCE_CONFLICT）。
    const conflict = Object.assign(
      new Error("Request failed with status code 409"),
      {
        response: {
          status: 409,
          data: {
            error: {
              code: "TAG_GOVERNANCE_CONFLICT",
              message:
                "deployment requires a release-service sealed holdout evaluation",
              detail: {},
            },
          },
        },
      },
    );
    mocks.createDeployment.mockRejectedValue(conflict);
    renderPage("/tag-governance?tab=deployments");
    await screen.findByText("灰度流量 25%");

    await user.click(screen.getByRole("button", { name: "创建影子部署" }));
    const dialog = screen.getByRole("dialog", { name: "创建影子部署" });
    await user.type(
      within(dialog).getByRole("spinbutton", { name: "部署抽取版本 ID" }),
      "42",
    );
    await user.type(
      within(dialog).getByRole("spinbutton", { name: "部署评估 ID" }),
      "7",
    );
    await user.type(
      within(dialog).getByRole("spinbutton", {
        name: "部署基线抽取版本 ID",
      }),
      "41",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "创建影子部署" }),
    );

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      /密封 Holdout|challenge 验证结果/,
    );
    expect(
      screen.queryByText(/部署已被其他操作更新/),
    ).not.toBeInTheDocument();
  });

  it("tells a reused-evaluation conflict apart from a wrong-lane one", async () => {
    const user = userEvent.setup();
    // 两种 409 都提到 sealed holdout，但补救动作相反：这一种要换一次评估，
    // 而不是去自进化面板产生新候选。只按 /sealed holdout/ 匹配会指错路。
    const conflict = Object.assign(
      new Error("Request failed with status code 409"),
      {
        response: {
          status: 409,
          data: {
            error: {
              code: "TAG_GOVERNANCE_CONFLICT",
              message: "a sealed holdout evaluation can start only one deployment",
              detail: {},
            },
          },
        },
      },
    );
    mocks.createDeployment.mockRejectedValue(conflict);
    renderPage("/tag-governance?tab=deployments");
    await screen.findByText("灰度流量 25%");

    await user.click(screen.getByRole("button", { name: "创建影子部署" }));
    const dialog = screen.getByRole("dialog", { name: "创建影子部署" });
    await user.type(
      within(dialog).getByRole("spinbutton", { name: "部署抽取版本 ID" }),
      "42",
    );
    await user.type(
      within(dialog).getByRole("spinbutton", { name: "部署评估 ID" }),
      "7",
    );
    await user.type(
      within(dialog).getByRole("spinbutton", { name: "部署基线抽取版本 ID" }),
      "41",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "创建影子部署" }),
    );

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/已经启动过一次部署/);
    expect(alert).not.toHaveTextContent(/自进化面板/);
  });

  it("freezes a server-resolved review cohort with every completeness guarantee", async () => {
    const user = userEvent.setup();
    mocks.freezeGoldSet.mockResolvedValue({
      id: 6,
      gold_set_id: 5,
      version: "2026.07",
      status: "frozen",
      item_count: 2,
    });
    renderPage();
    await screen.findByText("销售对话标签");
    await user.click(screen.getByRole("tab", { name: "评估实验" }));
    expect(await screen.findByText("销售对话金标集")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "冻结销售对话金标集版本" }),
    );
    await user.type(screen.getByRole("textbox", { name: "金标版本号" }), "2026.07");
    expect(
      screen.queryByRole("textbox", { name: "复核决策 ID" }),
    ).not.toBeInTheDocument();
    await user.type(
      screen.getByRole("textbox", { name: "复核批次 ID" }),
      "release-2026-07, audit-2026-07",
    );
    await user.click(
      screen.getByRole("checkbox", { name: "已覆盖所有适用标签矩阵" }),
    );
    await user.click(
      screen.getByRole("checkbox", { name: "已冻结输入快照" }),
    );
    await user.click(
      screen.getByRole("checkbox", { name: "已按接待隔离样本" }),
    );
    await user.click(
      screen.getByRole("checkbox", { name: "仅包含 T2/T3 真值" }),
    );
    await user.click(screen.getByRole("button", { name: "冻结金标版本" }));

    await waitFor(() => {
      expect(mocks.freezeGoldSet).toHaveBeenCalledWith(5, {
        version: "2026.07",
        cohort: {
          review_bundle_ids: ["release-2026-07", "audit-2026-07"],
          truth_tiers: ["t2", "t3"],
          subject_types: ["dialogue_unit", "reception"],
        },
        completeness_checklist: {
          full_applicable_matrix: true,
          frozen_input_snapshots: true,
          reception_level_isolation: true,
          t2_t3_truth_only: true,
        },
      });
    });
    expect(await screen.findByText("已冻结金标版本 #6，共 2 项")).toBeVisible();
  });

  it("leaves canary promotion to the trusted monitor and preserves rollback", async () => {
    const user = userEvent.setup();
    mocks.rollbackDeployment.mockResolvedValue({
      id: 9,
      status: "rolled_back",
    });
    renderPage();
    await screen.findByText("销售对话标签");
    await user.click(screen.getByRole("tab", { name: "发布监控" }));
    await screen.findByText("灰度流量 25%");

    expect(screen.getByText("可信 Monitor 自动晋级")).toBeVisible();
    expect(
      screen.getByText(/等待 Monitor 完成 25% 灰度.*自动进入管理员审批/),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /推进部署 9|推进至/ }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "回滚部署 9" }));
    await user.type(
      screen.getByRole("textbox", { name: "回滚原因" }),
      "关键标签召回率下降",
    );
    await user.click(screen.getByRole("button", { name: "确认回滚" }));
    await waitFor(() => {
      expect(mocks.rollbackDeployment).toHaveBeenCalledWith(
        9,
        "关键标签召回率下降",
        3,
      );
    });
  });

  it("requires a distinct rollback baseline from the same schema", async () => {
    const user = userEvent.setup();
    mocks.createDeployment.mockResolvedValue({
      id: 10,
      status: "shadow",
      revision: 1,
    });
    renderPage();
    await screen.findByText("销售对话标签");
    await user.click(screen.getByRole("tab", { name: "发布监控" }));
    await screen.findByText("灰度流量 25%");

    await user.click(screen.getByRole("button", { name: "创建影子部署" }));
    const deploymentDialog = screen.getByRole("dialog", {
      name: "创建影子部署",
    });
    await user.type(
      within(deploymentDialog).getByRole("spinbutton", {
        name: "部署抽取版本 ID",
      }),
      "42",
    );
    await user.type(
      within(deploymentDialog).getByRole("spinbutton", {
        name: "部署评估 ID",
      }),
      "7",
    );
    await user.click(
      within(deploymentDialog).getByRole("button", {
        name: "创建影子部署",
      }),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "候选、评估与回滚基线 ID 均必须填写正整数",
    );
    expect(mocks.createDeployment).not.toHaveBeenCalled();

    await user.type(
      within(deploymentDialog).getByRole("spinbutton", {
        name: "部署基线抽取版本 ID",
      }),
      "41",
    );
    expect(
      within(deploymentDialog).queryByRole("textbox", {
        name: "样本不足覆盖原因",
      }),
    ).not.toBeInTheDocument();
    expect(deploymentDialog).toHaveTextContent(
      "所有质量与样本量门禁均为硬门禁，不支持人工覆盖",
    );
    await user.click(
      within(deploymentDialog).getByRole("button", {
        name: "创建影子部署",
      }),
    );

    await waitFor(() => {
      expect(mocks.createDeployment).toHaveBeenCalledWith({
        tagger_version_id: 42,
        evaluation_run_id: 7,
        baseline_tagger_version_id: 41,
      });
    });
  });

  it("passes the current revision when an administrator approves production", async () => {
    const user = userEvent.setup();
    mocks.deployments.mockResolvedValueOnce({
      items: [
        {
          id: 9,
          tenant_id: "tenant-a",
          tagger_version_id: 42,
          evaluation_run_id: 7,
          baseline_tagger_version_id: 41,
          status: "awaiting_admin",
          traffic_percent: 25,
          revision: 4,
          promotion_paused: false,
          pause_reason: null,
          created_by: 1,
          approved_by: null,
          approved_at: null,
          rolled_back_by: null,
          rolled_back_at: null,
          rollback_reason: null,
          created_at: "2026-07-25T04:00:00Z",
          updated_at: "2026-07-25T04:00:00Z",
        },
      ],
      total: 1,
    });
    mocks.approveDeployment.mockResolvedValue({
      id: 9,
      status: "production",
      revision: 5,
    });
    renderPage();
    await screen.findByText("销售对话标签");
    await user.click(screen.getByRole("tab", { name: "发布监控" }));
    await user.click(
      await screen.findByRole("button", { name: "批准部署 9 上线" }),
    );

    await waitFor(() => {
      expect(mocks.approveDeployment).toHaveBeenCalledWith(9, 4);
    });
  });

  it("requires an audited conclusion before resuming a drift-paused deployment", async () => {
    const user = userEvent.setup();
    const pausedDeployment = deploymentFixture();
    mocks.deployments.mockResolvedValue({
      items: [pausedDeployment],
      total: 1,
    });
    mocks.resumeDeployment.mockResolvedValue({
      ...pausedDeployment,
      revision: 8,
      promotion_paused: false,
      pause_reason: null,
    });
    renderPage("/tag-governance?tab=deployments");

    expect(
      await screen.findByText(/distribution drift requires review/),
    ).toBeVisible();
    expect(screen.getByText("完成复核并恢复")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "完成复核并恢复部署 9" }),
    );

    const dialog = screen.getByRole("dialog", {
      name: "完成复核并恢复部署 #9",
    });
    const resumeButton = within(dialog).getByRole("button", {
      name: "确认恢复推进",
    });
    const reasonInput = within(dialog).getByRole("textbox", {
      name: "管理员复核结论 / 恢复理由",
    });
    expect(resumeButton).toBeDisabled();
    expect(
      within(dialog).getByText("管理员复核结论至少需要 8 个字符。"),
    ).toBeVisible();

    await user.type(reasonInput, "漂移复核完成");
    expect(resumeButton).toBeDisabled();
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "管理员复核结论至少需要 8 个字符",
    );
    expect(mocks.resumeDeployment).not.toHaveBeenCalled();

    await user.clear(reasonInput);
    await user.type(
      reasonInput,
      "已复核分布差异来源，确认业务结构变化且质量门禁稳定",
    );
    expect(resumeButton).toBeEnabled();
    await user.click(resumeButton);

    await waitFor(() => {
      expect(mocks.resumeDeployment).toHaveBeenCalledWith(
        9,
        "已复核分布差异来源，确认业务结构变化且质量门禁稳定",
        7,
      );
    });
    expect(
      await screen.findByText("部署 #9 已完成漂移复核并恢复自动推进"),
    ).toBeVisible();
    await waitFor(() => {
      expect(mocks.deployments.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it("shows drift resume only for eligible active deployment stages", async () => {
    mocks.deployments.mockResolvedValueOnce({
      items: [
        deploymentFixture({
          id: 21,
          status: "shadow",
          traffic_percent: 0,
        }),
        deploymentFixture({
          id: 22,
          status: "canary_5",
          traffic_percent: 5,
        }),
        deploymentFixture({
          id: 23,
          status: "awaiting_admin",
        }),
        deploymentFixture({
          id: 24,
          status: "production",
          traffic_percent: 100,
        }),
        deploymentFixture({
          id: 25,
          pause_reason: "quality gate requires review",
        }),
        deploymentFixture({
          id: 26,
          promotion_paused: false,
          pause_reason: null,
        }),
      ],
      total: 6,
    });
    renderPage("/tag-governance?tab=deployments");

    await screen.findByText("DEPLOYMENT #21");
    for (const id of [21, 22, 23]) {
      expect(
        screen.getByRole("button", {
          name: `完成复核并恢复部署 ${id}`,
        }),
      ).toBeVisible();
    }
    for (const id of [24, 25, 26]) {
      expect(
        screen.queryByRole("button", {
          name: `完成复核并恢复部署 ${id}`,
        }),
      ).not.toBeInTheDocument();
    }
    expect(
      screen.getByRole("button", { name: "批准部署 23 上线" }),
    ).toBeDisabled();
  });

  it("shows the existing refresh action when drift resume loses revision CAS", async () => {
    const user = userEvent.setup();
    const conflict = Object.assign(new Error("Request failed with status 409"), {
      response: { status: 409 },
    });
    mocks.deployments.mockResolvedValue({
      items: [deploymentFixture()],
      total: 1,
    });
    mocks.resumeDeployment.mockRejectedValue(conflict);
    renderPage("/tag-governance?tab=deployments");

    await user.click(
      await screen.findByRole("button", {
        name: "完成复核并恢复部署 9",
      }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "完成复核并恢复部署 #9",
    });
    await user.type(
      within(dialog).getByRole("textbox", {
        name: "管理员复核结论 / 恢复理由",
      }),
      "复核完成，确认恢复推进",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "确认恢复推进" }),
    );

    expect(
      await within(dialog).findByText(/部署已被其他操作更新/),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "刷新部署状态" }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", {
          name: "完成复核并恢复部署 #9",
        }),
      ).not.toBeInTheDocument();
      expect(mocks.deployments.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it("explains a stale rollback revision and offers a refresh", async () => {
    const user = userEvent.setup();
    const conflict = Object.assign(new Error("Request failed with status 409"), {
      response: { status: 409 },
    });
    mocks.rollbackDeployment.mockRejectedValue(conflict);
    renderPage();
    await screen.findByText("销售对话标签");
    await user.click(screen.getByRole("tab", { name: "发布监控" }));
    await user.click(await screen.findByRole("button", { name: "回滚部署 9" }));
    await user.type(
      screen.getByRole("textbox", { name: "回滚原因" }),
      "关键标签召回下降",
    );
    await user.click(screen.getByRole("button", { name: "确认回滚" }));

    expect(
      await screen.findByText(/部署已被其他操作更新/),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "刷新部署状态" })).toBeVisible();
  });

  it("creates a candidate version from a validated form", async () => {
    const user = userEvent.setup();
    mocks.createTaggerVersion.mockResolvedValue({
      id: 43,
      status: "draft",
      version: "tagger-2.4",
    });
    renderPage();
    await screen.findByText("销售对话标签");
    await user.click(screen.getByRole("tab", { name: "抽取版本" }));
    await screen.findByText("tagger-2.3");

    await user.click(screen.getByRole("button", { name: "创建候选版本" }));
    await user.clear(screen.getByRole("spinbutton", { name: "标签体系版本 ID" }));
    await user.type(
      screen.getByRole("spinbutton", { name: "标签体系版本 ID" }),
      "11",
    );
    await user.type(screen.getByRole("textbox", { name: "候选版本号" }), "tagger-2.4");
    await user.type(screen.getByRole("textbox", { name: "模型版本" }), "model-b");
    await user.click(screen.getByRole("button", { name: "保存候选" }));

    await waitFor(() => {
      expect(mocks.createTaggerVersion).toHaveBeenCalledWith({
        schema_version_id: 11,
        version: "tagger-2.4",
        engine: "hybrid",
        prompt_content: "",
        rule_bundle: {},
        model_version: "model-b",
        thresholds: {},
      });
    });
    expect(await screen.findByText("候选版本 tagger-2.4 已创建")).toBeVisible();
  });

  it("moves automatic optimization into the self-evolution workspace", async () => {
    const user = userEvent.setup();
    mocks.createOptimization.mockResolvedValue({
      id: 72,
      status: "queued",
      phase: "prepare",
      baseline_tagger_version_id: 42,
      gold_set_version_id: 7,
      cohort: { source: "eligible_feedback" },
      objective: { policy: "balanced" },
      search_budget: { max_trials: 24, sealed_holdout_queries: 1 },
      trigger: "manual",
      summary: { completed_trials: 0, trial_count: 24 },
      created_at: "2026-07-25T07:00:00Z",
      updated_at: "2026-07-25T07:00:00Z",
    });
    renderPage();
    await screen.findByText("销售对话标签");
    await user.click(screen.getByRole("tab", { name: "自进化" }));
    await screen.findByText("harness-2.3");

    await user.click(screen.getByRole("button", { name: "启动优化" }));
    expect(
      screen.queryByRole("spinbutton", { name: /版本 ID/ }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "启动优化运行" }));

    await waitFor(() => {
      expect(mocks.createOptimization).toHaveBeenCalledWith({
        cohort: { source: "eligible_feedback" },
        target_policy: { policy: "balanced" },
        search_budget: {
          max_trials: 24,
          sealed_holdout_queries: 1,
        },
      });
      expect(
        mocks.createOptimization.mock.calls.at(-1)?.[0],
      ).not.toHaveProperty("gold_set_version_id");
      expect(
        mocks.createOptimization.mock.calls.at(-1)?.[0],
      ).not.toHaveProperty("objective");
      expect(
        mocks.createOptimization.mock.calls.at(-1)?.[0],
      ).not.toHaveProperty("trigger");
    });
    expect(await screen.findByText("优化运行 #72 已进入队列")).toBeVisible();
  });

  it("reaches the async run index by clicking, not by typing a URL", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("销售对话标签");

    await user.click(screen.getByRole("tab", { name: "运行记录" }));

    const row = await screen.findByRole("row", { name: /#77/ });
    expect(within(row).getByText("标签抽取")).toBeInTheDocument();
    expect(within(row).getByText("12 / 40")).toBeInTheDocument();
    // 索引存在的意义就是不必先知道 job id：行里直接给出通往既有详情页的链接。
    expect(within(row).getByRole("link", { name: "查看详情" })).toHaveAttribute(
      "href",
      "/tag-runs/77",
    );
  });

  it("renders recoverable error and empty states", async () => {
    mocks.schemas.mockRejectedValueOnce(new Error("网络中断"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("网络中断");
    mocks.schemas.mockResolvedValueOnce({ items: [], total: 0 });
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(await screen.findByText("尚未建立标签体系")).toBeInTheDocument();
  });
});
