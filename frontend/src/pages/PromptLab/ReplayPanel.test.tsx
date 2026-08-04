import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api/services", () => ({
  listTagEvaluations: vi.fn(),
  promotePromptArtifact: vi.fn(),
}));

import { listTagEvaluations, promotePromptArtifact } from "@/api/services";
import type { PromptArtifactSummary, TagEvaluation } from "@/types/api";

import { ReplayPanel } from "./ReplayPanel";

const mocked = listTagEvaluations as unknown as ReturnType<typeof vi.fn>;
const mockedPromote = promotePromptArtifact as unknown as ReturnType<typeof vi.fn>;

function artifact(overrides: Partial<PromptArtifactSummary> = {}): PromptArtifactSummary {
  return {
    id: 302,
    compilation_id: 9001,
    optimization_run_id: null,
    baseline_tagger_version_id: 12,
    gold_set_version_id: 4,
    parent_artifact_id: 301,
    candidate_tagger_version_id: 77,
    compiler: "builtin",
    compiler_version: "builtin-proposer-v1",
    metric_version: "prompt-lab-metric-v1",
    status: "review",
    prompt_token_estimate: 820,
    accepted_patch_ids: [],
    input_budget_report: {
      prompt_tokens: 620,
      schema_tokens: 200,
      fixed_tokens: 820,
      usable_tokens: 10_800,
      headroom_tokens: 9_980,
      baseline_fixed_tokens: 300,
      baseline_headroom_tokens: 10_500,
      headroom_delta: -520,
      headroom_shrink_ratio: 0.049,
      fits: true,
    },
    redaction_report: { demo_count: 0, by_redaction_mode: {} },
    artifact_checksum: "c".repeat(64),
    created_at: "2026-08-03T02:00:00Z",
    ...overrides,
  };
}

function evaluation(overrides: Partial<TagEvaluation> = {}): TagEvaluation {
  return {
    id: 91,
    tenant_id: "chang_an",
    tagger_version_id: 77,
    baseline_tagger_version_id: 12,
    gold_set_version_id: 4,
    status: "completed",
    passed: true,
    metrics: {
      macro_f1: 0.842,
      critical_recall: 0.96,
      evidence_coverage: 0.99,
      error_rate: 0.004,
      precision: 0.88,
      recall: 0.81,
    },
    baseline_metrics: {
      macro_f1: 0.82,
      precision: 0.86,
      recall: 0.8,
    },
    supported_label_f1: { intent: 0.9, price: 0.61 },
    baseline_label_f1: { intent: 0.84, price: 0.72 },
    gates: [
      {
        code: "macro_f1",
        passed: true,
        actual: 0.842,
        threshold: 0.8,
        message: "整体 Macro F1 达标。",
      },
    ],
    started_at: "2026-08-03T03:00:00Z",
    finished_at: "2026-08-03T03:10:00Z",
    created_by: 1,
    created_at: "2026-08-03T03:00:00Z",
    updated_at: "2026-08-03T03:10:00Z",
    ...overrides,
  };
}

function renderPanel(props: Partial<React.ComponentProps<typeof ReplayPanel>> = {}) {
  const onGoToCompile = vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ReplayPanel
          artifact={artifact()}
          isAdmin
          onGoToCompile={onGoToCompile}
          {...props}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { onGoToCompile };
}

beforeEach(() => {
  mocked.mockReset().mockResolvedValue({ items: [evaluation()], total: 1 });
  mockedPromote.mockReset().mockResolvedValue({
    artifact: artifact({ candidate_tagger_version_id: 88, status: "accepted" }),
    candidate_tagger_version: {
      id: 88,
      version: "baseline-v1-lab-r1",
      status: "draft",
      origin: "prompt_lab",
      prompt_artifact_id: 302,
    },
  });
});

describe("ReplayPanel", () => {
  it("guides the reviewer to pick an artifact first", async () => {
    const { onGoToCompile } = renderPanel({ artifact: undefined });

    await screen.findByText("先在「编译运行」里选择一个产物。");
    expect(onGoToCompile).toBeDefined();
  });

  it("offers the admin a real promote CTA instead of a dead end", () => {
    renderPanel({ artifact: artifact({ candidate_tagger_version_id: null }) });

    expect(screen.getByText(/尚未晋级为候选抽取版本/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "晋级为候选版本" })).toBeInTheDocument();
    expect(screen.getByLabelText("版本后缀")).toBeInTheDocument();
    expect(screen.getByLabelText("变更说明")).toBeInTheDocument();
  });

  it("keeps the promote button disabled until both fields are valid", async () => {
    const user = userEvent.setup();
    renderPanel({ artifact: artifact({ candidate_tagger_version_id: null }) });

    const submit = screen.getByRole("button", { name: "晋级为候选版本" });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("版本后缀"), "r1");
    expect(submit).toBeDisabled();

    // 变更说明少于 8 字仍不可提交——与后端 min_length=8 同步。
    await user.type(screen.getByLabelText("变更说明"), "太短");
    expect(submit).toBeDisabled();
  });

  it("promotes and then shows the minted candidate with a link to governance", async () => {
    const user = userEvent.setup();
    renderPanel({ artifact: artifact({ candidate_tagger_version_id: null }) });

    await user.type(screen.getByLabelText("版本后缀"), "r1");
    await user.type(screen.getByLabelText("变更说明"), "采纳两条聚类补丁后的候选提示词");
    await user.click(screen.getByRole("button", { name: "晋级为候选版本" }));

    expect(await screen.findByRole("status")).toHaveTextContent("baseline-v1-lab-r1");
    expect(screen.getByRole("status")).toHaveTextContent("#88");
    expect(
      screen.getByRole("link", { name: "前往标签治理查看候选版本" }),
    ).toHaveAttribute("href", "/tag-governance?tab=taggers");
    expect(mockedPromote).toHaveBeenCalledWith(302, {
      version_suffix: "r1",
      change_summary: "采纳两条聚类补丁后的候选提示词",
    });
  });

  it("surfaces a promote conflict as advice, not a raw error", async () => {
    const user = userEvent.setup();
    mockedPromote.mockRejectedValue({
      isAxiosError: true,
      response: { status: 409, data: { detail: "tagger version already exists" } },
    });
    renderPanel({ artifact: artifact({ candidate_tagger_version_id: null }) });

    await user.type(screen.getByLabelText("版本后缀"), "r1");
    await user.type(screen.getByLabelText("变更说明"), "采纳两条聚类补丁后的候选提示词");
    await user.click(screen.getByRole("button", { name: "晋级为候选版本" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "版本号或配置与已有抽取版本冲突",
    );
  });

  it("tells a non-admin honestly that promotion needs an administrator", () => {
    renderPanel({
      artifact: artifact({ candidate_tagger_version_id: null }),
      isAdmin: false,
    });

    expect(screen.getByText(/需要管理员权限/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "晋级为候选版本" }),
    ).not.toBeInTheDocument();
  });

  it("links a promoted artifact to its candidate version in governance", async () => {
    renderPanel();

    expect(await screen.findByText("#77")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "在标签治理中查看" })).toHaveAttribute(
      "href",
      "/tag-governance?tab=taggers",
    );
  });

  it("orders the per-label table by the size of the change", async () => {
    renderPanel();

    const table = await screen.findByRole("table");
    const firstRow = within(table).getAllByRole("row")[1];
    // price 下降 11 个点，比 intent 上升 6 个点的幅度更大。
    expect(within(firstRow).getByText("price")).toBeInTheDocument();
  });

  it("marks precision and recall as aggregate rather than per-label", async () => {
    renderPanel();

    expect(
      await screen.findByText(/接口按标签只返回 F1；精确率与召回率为全量聚合值/),
    ).toBeInTheDocument();
  });

  it("opens the regressions by default and keeps the improvements collapsed", async () => {
    renderPanel();

    const regressions = (await screen.findByText(/从对变错/)).closest("details");
    const improvements = screen.getByText(/从错变对/).closest("details");
    expect(regressions).toHaveAttribute("open");
    expect(improvements).not.toHaveAttribute("open");
  });

  it("says the per-sample flip list is not something the API provides", async () => {
    renderPanel();

    expect(
      await screen.findByText(/逐样本的翻转清单需要评估接口返回样本级结果/),
    ).toBeInTheDocument();
  });

  it("lists each gate with its actual value against the threshold", async () => {
    renderPanel();

    expect(await screen.findByText("macro_f1")).toBeInTheDocument();
    expect(screen.getByText("84.2% / 阈值 80%")).toBeInTheDocument();
  });

  it("adds the input budget as a gate the evaluation itself cannot see", async () => {
    renderPanel();

    expect(await screen.findByText("input_budget")).toBeInTheDocument();
    expect(screen.getByText("820 / 10800 token")).toBeInTheDocument();
  });

  it("announces the overall verdict", async () => {
    renderPanel();

    expect(await screen.findByRole("status", { name: "门禁通过" })).toBeInTheDocument();
  });

  it("shows a blocked verdict when the evaluation did not pass", async () => {
    mocked.mockResolvedValue({ items: [evaluation({ passed: false })], total: 1 });
    renderPanel();

    expect(await screen.findByRole("status", { name: "门禁拦截" })).toBeInTheDocument();
  });

  it("shows the running status instead of blank metrics while an evaluation is in flight", async () => {
    mocked.mockResolvedValue({
      items: [evaluation({ status: "running" })],
      total: 1,
    });
    renderPanel();

    expect(await screen.findByText("运行中")).toBeInTheDocument();
  });

  it("points at the governance centre when no evaluation matches the candidate", async () => {
    mocked.mockResolvedValue({ items: [], total: 0 });
    renderPanel();

    expect(await screen.findByText("候选版本还没有评估结果")).toBeInTheDocument();
  });

  it("notes when an evaluation returned no per-label breakdown", async () => {
    mocked.mockResolvedValue({
      items: [evaluation({ supported_label_f1: undefined, baseline_label_f1: undefined })],
      total: 1,
    });
    renderPanel();

    expect(await screen.findByText("本次评估没有返回逐标签的 F1。")).toBeInTheDocument();
  });
});
