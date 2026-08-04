import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api/services", () => ({
  decidePromptPatches: vi.fn(),
  getPromptArtifactDiff: vi.fn(),
  listPromptGradients: vi.fn(),
}));

import {
  decidePromptPatches,
  getPromptArtifactDiff,
  listPromptGradients,
} from "@/api/services";
import type { PromptGradient } from "@/types/api";

import { GradientPanel } from "./GradientPanel";

const mocks = {
  decide: decidePromptPatches as unknown as ReturnType<typeof vi.fn>,
  diff: getPromptArtifactDiff as unknown as ReturnType<typeof vi.fn>,
  gradients: listPromptGradients as unknown as ReturnType<typeof vi.fn>,
};

const ID_A = "a".repeat(32);
const ID_B = "b".repeat(32);

function gradient(overrides: Partial<PromptGradient> = {}): PromptGradient {
  return {
    id: 1,
    artifact_id: 301,
    patch_id: ID_A,
    iteration: 1,
    source_badcase_id: 11,
    tag_key: "price",
    failure_stage: "tag_reasoning",
    failure_mode: "correct:missed_label",
    gradient_text: "当前规则只要求出现「优惠」字样，未跨句关联金额。",
    proposed_edit: "规则一：出现明确金额才输出价格标签。",
    decision: "pending",
    decided_by: null,
    decided_at: null,
    decision_note: null,
    evaluation: { source_badcase_count: 7 },
    ...overrides,
  };
}

const DIFF = {
  artifact_id: 301,
  status: "draft" as const,
  baseline_prompt: "基线",
  candidate_prompt: "基线",
  patches: [
    {
      patch_id: ID_A,
      kind: "rule_clarification" as const,
      origin: "builtin" as const,
      ordinal: 1,
      body: "规则一",
      rationale: "聚类共 7 例",
      target_tag_keys: ["price"],
      gradient_text: null,
      source_badcase_ids: [11],
      source_gold_label_ids: [],
    },
    {
      patch_id: ID_B,
      kind: "constraint_add" as const,
      origin: "builtin" as const,
      ordinal: 2,
      body: "规则二",
      rationale: "聚类共 5 例",
      target_tag_keys: ["evidence"],
      gradient_text: null,
      source_badcase_ids: [12],
      source_gold_label_ids: [],
    },
  ],
  demos: [],
  accepted_patch_ids: [ID_A, ID_B],
  prompt_token_estimate: 800,
  fixed_token_delta: 500,
  input_budget_report: {
    prompt_tokens: 600,
    schema_tokens: 200,
    fixed_tokens: 800,
    usable_tokens: 10_800,
    headroom_tokens: 10_000,
    baseline_fixed_tokens: 300,
    baseline_headroom_tokens: 10_500,
    headroom_delta: -500,
    headroom_shrink_ratio: 0.047,
    fits: true,
  },
  redaction_report: { demo_count: 0, by_redaction_mode: {} },
};

function renderPanel(props: Partial<React.ComponentProps<typeof GradientPanel>> = {}) {
  const onArtifactCreated = vi.fn();
  const onGoToCompile = vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <GradientPanel
        artifactId={301}
        isAdmin
        onArtifactCreated={onArtifactCreated}
        onGoToCompile={onGoToCompile}
        {...props}
      />
    </QueryClientProvider>,
  );
  return { onArtifactCreated, onGoToCompile };
}

beforeEach(() => {
  mocks.gradients.mockReset().mockResolvedValue({ items: [gradient()], total: 1 });
  mocks.diff.mockReset().mockResolvedValue(DIFF);
  mocks.decide.mockReset().mockResolvedValue({ id: 302, patches: [], demos: [] });
});

describe("GradientPanel", () => {
  it("presents each suggestion as failure, feedback, edit and effect", async () => {
    renderPanel();

    expect(await screen.findByText("① 失败样本")).toBeInTheDocument();
    expect(screen.getByText("② 评价反馈")).toBeInTheDocument();
    expect(screen.getByText("③ 修改建议")).toBeInTheDocument();
    expect(screen.getByText("④ 应用后效果")).toBeInTheDocument();
  });

  it("translates the failure stage and shows the diagnosis", async () => {
    renderPanel();

    expect(await screen.findByText("标签推理")).toBeInTheDocument();
    expect(screen.getByText(/未跨句关联金额/)).toBeInTheDocument();
  });

  it("takes the patch kind and target tags from the diff response", async () => {
    renderPanel();

    expect(await screen.findByText("规则澄清")).toBeInTheDocument();
    expect(screen.getByText("目标标签：price")).toBeInTheDocument();
  });

  it("says the effect has not been replayed instead of inventing metrics", async () => {
    mocks.gradients.mockResolvedValue({
      items: [gradient({ evaluation: {} })],
      total: 1,
    });
    renderPanel();

    expect(await screen.findByText(/尚未回放评估/)).toBeInTheDocument();
  });

  it("renders an unknown effect field rather than dropping it", async () => {
    mocks.gradients.mockResolvedValue({
      items: [gradient({ evaluation: { brand_new_metric: 0.42 } })],
      total: 1,
    });
    renderPanel();

    expect(await screen.findByText("brand_new_metric")).toBeInTheDocument();
  });

  it("formats a known effect field by its declared type", async () => {
    mocks.gradients.mockResolvedValue({
      items: [gradient({ evaluation: { macro_f1_delta: 0.022, support: 12 } })],
      total: 1,
    });
    renderPanel();

    expect(await screen.findByText("+2.2%")).toBeInTheDocument();
    expect(screen.getByText("Macro F1 变化")).toBeInTheDocument();
  });

  it("warns that a small cluster's effect is only indicative", async () => {
    mocks.gradients.mockResolvedValue({
      items: [gradient({ evaluation: { support: 4 } })],
      total: 1,
    });
    renderPanel();

    expect(await screen.findByText(/样本不足（4 例）/)).toBeInTheDocument();
  });

  it("says an unreplayed record is evidence volume, not effect", async () => {
    // 编译期记录的样本量长得很像指标；不点破的话，reviewer 会当成补丁已被验证。
    mocks.gradients.mockResolvedValue({
      items: [
        gradient({
          evaluation: {
            source_badcase_count: 6,
            cluster_support: 6,
            gradient_rounds: 2,
            replayed: false,
            low_confidence: true,
          },
        }),
      ],
      total: 1,
    });
    renderPanel();

    expect(await screen.findByText(/尚未回放评估，下列只是编译期记录的证据量/)).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("该聚类样本不足，效果仅供参考。");
    expect(screen.getByText("梯度轮次")).toBeInTheDocument();
  });

  it("keeps the control flags out of the metric list", async () => {
    // 渲染成一行「true」既没信息量，又会把真正的数字挤下去。
    mocks.gradients.mockResolvedValue({
      items: [gradient({ evaluation: { cluster_support: 12, replayed: false, low_confidence: false } })],
      total: 1,
    });
    renderPanel();

    await screen.findByText("聚类样本量");
    expect(screen.queryByText("replayed")).not.toBeInTheDocument();
    expect(screen.queryByText("low_confidence")).not.toBeInTheDocument();
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });

  it("highlights the side effects a patch had on other tags", async () => {
    mocks.gradients.mockResolvedValue({
      items: [
        gradient({
          evaluation: { support: 20, tag_key_deltas: { "price.quote": -0.008 } },
        }),
      ],
      total: 1,
    });
    renderPanel();

    expect(await screen.findByText("对其他标签的影响")).toBeInTheDocument();
    expect(screen.getByText("price.quote")).toBeInTheDocument();
    expect(screen.getByText("-0.8%")).toBeInTheDocument();
  });

  it("stages an acceptance locally and counts it", async () => {
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "接受补丁 aaaaaaaa" }),
    );

    expect(screen.getByRole("status")).toHaveTextContent("已暂存 1 项决策（接受 1 / 拒绝 0）");
    expect(mocks.decide).not.toHaveBeenCalled();
  });

  it("offers a reason field when a patch is rejected", async () => {
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "拒绝补丁 aaaaaaaa" }),
    );

    expect(screen.getByLabelText("拒绝理由")).toHaveAttribute("maxlength", "1000");
  });

  it("sends the rejection reason along with the decision", async () => {
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "拒绝补丁 aaaaaaaa" }),
    );
    await userEvent.type(screen.getByLabelText("拒绝理由"), "改写把中性提问也算成异议");
    await userEvent.click(screen.getByRole("button", { name: "提交决策" }));
    const dialog = await screen.findByRole("dialog", { name: "提交补丁决策" });
    await userEvent.click(within(dialog).getByRole("button", { name: "确认提交" }));

    await waitFor(() => expect(mocks.decide).toHaveBeenCalledOnce());
    expect(mocks.decide.mock.calls[0][1].decisions[0]).toEqual({
      patch_id: ID_A,
      decision: "rejected",
      note: "改写把中性提问也算成异议",
    });
  });

  it("takes back a staged decision when the same button is pressed again", async () => {
    // 收不回误点的拒绝，复核员就只能带着错误决策提交。
    renderPanel();

    const reject = await screen.findByRole("button", { name: "拒绝补丁 aaaaaaaa" });
    await userEvent.click(reject);
    expect(reject).toHaveAttribute("aria-pressed", "true");

    await userEvent.click(reject);

    expect(reject).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByText(/已暂存/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("拒绝理由")).not.toBeInTheDocument();
  });

  it("drops every staged decision at once", async () => {
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "接受补丁 aaaaaaaa" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "清空暂存" }));

    expect(screen.queryByText(/已暂存/)).not.toBeInTheDocument();
    expect(mocks.decide).not.toHaveBeenCalled();
  });

  it("replays undecided patches at their current state when submitting", async () => {
    // GradientPanel 只展示第一条梯度，但提交时必须带上 diff 里的全部补丁。
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "拒绝补丁 aaaaaaaa" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "提交决策" }));
    const dialog = await screen.findByRole("dialog", { name: "提交补丁决策" });
    await userEvent.click(within(dialog).getByRole("button", { name: "确认提交" }));

    await waitFor(() => expect(mocks.decide).toHaveBeenCalledOnce());
    expect(mocks.decide.mock.calls[0][1].decisions).toEqual([
      { patch_id: ID_A, decision: "rejected" },
      { patch_id: ID_B, decision: "accepted" },
    ]);
  });

  it("counts the patches a submission would remove before confirming", async () => {
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "拒绝补丁 aaaaaaaa" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "提交决策" }));

    const dialog = await screen.findByRole("dialog", { name: "提交补丁决策" });
    expect(dialog).toHaveTextContent("1");
    expect(dialog).toHaveTextContent(/条补丁\s*将不出现在新的 Prompt 里/);
  });

  it("clears the staged decisions after a successful submission", async () => {
    const { onArtifactCreated } = renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "接受补丁 aaaaaaaa" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "提交决策" }));
    const dialog = await screen.findByRole("dialog", { name: "提交补丁决策" });
    await userEvent.click(within(dialog).getByRole("button", { name: "确认提交" }));

    await waitFor(() =>
      expect(onArtifactCreated).toHaveBeenCalledWith(
        expect.objectContaining({ id: 302 }),
      ),
    );
    expect(screen.queryByText(/已暂存/)).not.toBeInTheDocument();
  });

  it("explains a conflict as someone else having updated the artifact", async () => {
    mocks.decide.mockRejectedValue({
      isAxiosError: true,
      response: { status: 409, data: { error: { message: "conflict" } } },
    });
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "接受补丁 aaaaaaaa" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "提交决策" }));
    const dialog = await screen.findByRole("dialog", { name: "提交补丁决策" });
    await userEvent.click(within(dialog).getByRole("button", { name: "确认提交" }));

    expect(
      await screen.findByText(/产物已被他人更新，请重新加载后再提交/),
    ).toBeInTheDocument();
  });

  it("puts the decision filter into the query so switching refetches", async () => {
    renderPanel();
    await waitFor(() =>
      expect(mocks.gradients).toHaveBeenCalledWith({ artifact_id: 301 }),
    );

    await userEvent.selectOptions(screen.getByLabelText("按决策筛选梯度"), "accepted");

    await waitFor(() =>
      expect(mocks.gradients).toHaveBeenCalledWith({
        artifact_id: 301,
        decision: "accepted",
      }),
    );
  });

  it("explains why a builtin compilation may have no gradients", async () => {
    mocks.gradients.mockResolvedValue({ items: [], total: 0 });
    renderPanel();

    expect(await screen.findByText("该产物没有梯度记录")).toBeInTheDocument();
  });

  it("hides the decision controls from a non-admin", async () => {
    renderPanel({ isAdmin: false });

    await screen.findByText("① 失败样本");
    expect(
      screen.queryByRole("button", { name: "接受补丁 aaaaaaaa" }),
    ).not.toBeInTheDocument();
  });

  it("guides the reviewer to pick an artifact first", async () => {
    const { onGoToCompile } = renderPanel({ artifactId: null });

    await userEvent.click(screen.getByRole("button", { name: "前往编译运行" }));

    expect(onGoToCompile).toHaveBeenCalledOnce();
  });
});
