import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api/services", () => ({
  decidePromptPatches: vi.fn(),
  getPromptArtifactDiff: vi.fn(),
}));

import { decidePromptPatches, getPromptArtifactDiff } from "@/api/services";
import type { PromptArtifactDiff } from "@/types/api";

import { DiffPanel } from "./DiffPanel";

const mocks = {
  decide: decidePromptPatches as unknown as ReturnType<typeof vi.fn>,
  diff: getPromptArtifactDiff as unknown as ReturnType<typeof vi.fn>,
};

const HEADER = "基线规则：按 schema 判定标签。";
const PATCH_A = "规则一：出现明确金额才输出价格标签。";
const PATCH_B = "规则二：跨句证据需同时引用两个 segment。";
const ID_A = "a".repeat(32);
const ID_B = "b".repeat(32);

function diffPayload(overrides: Partial<PromptArtifactDiff> = {}): PromptArtifactDiff {
  return {
    artifact_id: 301,
    status: "draft",
    baseline_prompt: HEADER,
    candidate_prompt: [HEADER, PATCH_A, PATCH_B].join("\n\n"),
    patches: [
      {
        patch_id: ID_A,
        kind: "rule_clarification",
        origin: "builtin",
        ordinal: 1,
        body: PATCH_A,
        rationale: "聚类 tag_reasoning:price 共 7 例",
        target_tag_keys: ["price"],
        gradient_text: null,
        source_badcase_ids: [1],
        source_gold_label_ids: [],
      },
      {
        patch_id: ID_B,
        kind: "constraint_add",
        origin: "builtin",
        ordinal: 2,
        body: PATCH_B,
        rationale: "聚类 evidence:cross_sentence 共 5 例",
        target_tag_keys: ["evidence"],
        gradient_text: null,
        source_badcase_ids: [2],
        source_gold_label_ids: [],
      },
    ],
    demos: [],
    accepted_patch_ids: [ID_A, ID_B],
    // 真实量级：策略正文远小于包住它的固定开销，这正是旧口径会变号的形状。
    prompt_token_estimate: 394,
    fixed_token_delta: 149,
    input_budget_report: {
      prompt_tokens: 415,
      schema_tokens: 1_292,
      fixed_tokens: 1_707,
      usable_tokens: 10_800,
      headroom_tokens: 9_093,
      baseline_fixed_tokens: 1_558,
      baseline_headroom_tokens: 9_242,
      headroom_delta: -149,
      headroom_shrink_ratio: 0.0161,
      fits: true,
    },
    redaction_report: { demo_count: 0, by_redaction_mode: {} },
    ...overrides,
  };
}

function renderPanel(props: Partial<React.ComponentProps<typeof DiffPanel>> = {}) {
  const onArtifactCreated = vi.fn();
  const onClearArtifact = vi.fn();
  const onGoToCompile = vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <DiffPanel
          artifactId={301}
          isAdmin
          onArtifactCreated={onArtifactCreated}
          onClearArtifact={onClearArtifact}
          onGoToCompile={onGoToCompile}
          {...props}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { onArtifactCreated, onClearArtifact, onGoToCompile };
}

const DEMO = {
  demo_id: "d".repeat(32),
  gold_label_id: 5,
  subject_type: "dialogue_unit",
  subject_id: 42,
  rendered_text: "示例甲：客户询问优惠，顾问报出具体金额。",
  redaction_mode: "synthetic" as const,
  source_checksum: "e".repeat(64),
  reception_id: 7,
  segment_ids: [1, 2],
  recording_ids: [3],
};

beforeEach(() => {
  mocks.diff.mockReset().mockResolvedValue(diffPayload());
  mocks.decide.mockReset().mockResolvedValue({ id: 302, patches: [], demos: [] });
});

describe("DiffPanel", () => {
  it("guides the reviewer to pick an artifact first", async () => {
    const { onGoToCompile } = renderPanel({ artifactId: null });

    await userEvent.click(screen.getByRole("button", { name: "前往编译运行" }));

    expect(onGoToCompile).toHaveBeenCalledOnce();
  });

  it("shows the candidate and baseline token counts with their difference", async () => {
    renderPanel();

    expect(await screen.findByText("394 token")).toBeInTheDocument();
    expect(screen.getByText("1,707 token")).toBeInTheDocument();
    expect(screen.getByText("基线 1,558")).toBeInTheDocument();
    expect(screen.getByText("+149")).toBeInTheDocument();
  });

  it("colours a candidate that costs more as worse, never as a saving", async () => {
    // 这里守的是符号本身：候选比基线贵 149 token，界面绝不能把它涂成改进。
    renderPanel();

    expect(await screen.findByText("+149")).toHaveClass("is-worse");
    expect(screen.queryByText("+149")).not.toHaveClass("is-better");
  });

  it("shows a dash instead of a difference when the budget was never measured", async () => {
    mocks.diff.mockResolvedValue(diffPayload({ fixed_token_delta: null }));
    renderPanel();

    const dashes = await screen.findAllByText("—");
    expect(dashes.length).toBeGreaterThan(0);
    expect(dashes.every((node) => !node.classList.contains("is-better"))).toBe(true);
  });

  it("reports the headroom the candidate gives up", async () => {
    renderPanel();

    expect(await screen.findByText("-149")).toBeInTheDocument();
    expect(screen.getByText("1.6%")).toBeInTheDocument();
  });

  it("passes the input budget gate when the prompt fits", async () => {
    renderPanel();

    expect(
      await screen.findByRole("status", { name: "在输入预算内" }),
    ).toHaveTextContent("预算内");
  });

  it("flags a prompt that does not fit the input budget", async () => {
    mocks.diff.mockResolvedValue(
      diffPayload({
        input_budget_report: { ...diffPayload().input_budget_report, fits: false },
      }),
    );
    renderPanel();

    expect(
      await screen.findByRole("status", { name: "超出输入预算" }),
    ).toHaveTextContent("超出预算");
  });

  it("labels both sides of the comparison", async () => {
    renderPanel();

    expect(
      await screen.findByRole("heading", { name: "基线 Prompt" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "候选 Prompt" }),
    ).toBeInTheDocument();
  });

  it("attributes each added block to the patch that produced it", async () => {
    renderPanel();

    expect(await screen.findByText(/补丁 aaaaaaaa · 规则澄清/)).toBeInTheDocument();
    expect(screen.getByText(/补丁 bbbbbbbb · 新增约束/)).toBeInTheDocument();
  });

  it("says so plainly when attribution cannot be rebuilt", async () => {
    mocks.diff.mockResolvedValue(
      diffPayload({ candidate_prompt: "服务端换了渲染规则，完全对不上。" }),
    );
    renderPanel();

    expect(
      await screen.findByText(/本次差异无法归属到具体补丁/),
    ).toBeInTheDocument();
  });

  it("lists each inlined demo with its redaction mode and source", async () => {
    mocks.diff.mockResolvedValue(diffPayload({ demos: [DEMO] }));
    renderPanel();

    expect(await screen.findByText("合成改写")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看接待" })).toHaveAttribute(
      "href",
      "/receptions/7/workspace",
    );
  });

  it("stages a demo removal locally without calling the API", async () => {
    mocks.diff.mockResolvedValue(diffPayload({ demos: [DEMO] }));
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "剔除示例 dddddddd" }),
    );

    expect(screen.getByText("已标记剔除 1 条示例")).toBeInTheDocument();
    expect(mocks.decide).not.toHaveBeenCalled();
  });

  it("replays every unchanged patch when submitting a demo removal", async () => {
    // 这是最容易写错的地方：decisions 是最终采纳集，漏掉的补丁会被服务端移除。
    mocks.diff.mockResolvedValue(diffPayload({ demos: [DEMO] }));
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "剔除示例 dddddddd" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "提交剔除" }));

    await waitFor(() => expect(mocks.decide).toHaveBeenCalledOnce());
    expect(mocks.decide).toHaveBeenCalledWith(301, {
      decisions: [
        { patch_id: ID_A, decision: "accepted" },
        { patch_id: ID_B, decision: "accepted" },
      ],
      dropped_demo_ids: [DEMO.demo_id],
    });
  });

  it("replays a previously rejected patch as rejected, not as accepted", async () => {
    mocks.diff.mockResolvedValue(
      diffPayload({ demos: [DEMO], accepted_patch_ids: [ID_A] }),
    );
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "剔除示例 dddddddd" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "提交剔除" }));

    await waitFor(() => expect(mocks.decide).toHaveBeenCalledOnce());
    expect(mocks.decide.mock.calls[0][1].decisions).toEqual([
      { patch_id: ID_A, decision: "accepted" },
      { patch_id: ID_B, decision: "rejected" },
    ]);
  });

  it("switches to the new artifact after a removal succeeds", async () => {
    mocks.diff.mockResolvedValue(diffPayload({ demos: [DEMO] }));
    const { onArtifactCreated } = renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "剔除示例 dddddddd" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "提交剔除" }));

    await waitFor(() =>
      expect(onArtifactCreated).toHaveBeenCalledWith(
        expect.objectContaining({ id: 302 }),
      ),
    );
  });

  it("restores every staged removal on request", async () => {
    mocks.diff.mockResolvedValue(diffPayload({ demos: [DEMO] }));
    renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "剔除示例 dddddddd" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "全部还原" }));

    expect(screen.queryByText("已标记剔除 1 条示例")).not.toBeInTheDocument();
  });

  it("hides the removal control from a non-admin", async () => {
    mocks.diff.mockResolvedValue(diffPayload({ demos: [DEMO] }));
    renderPanel({ isAdmin: false });

    await screen.findByText("合成改写");
    expect(
      screen.queryByRole("button", { name: "剔除示例 dddddddd" }),
    ).not.toBeInTheDocument();
  });

  it("says the artifact is gone rather than showing an empty diff", async () => {
    mocks.diff.mockRejectedValue({
      isAxiosError: true,
      response: { status: 404, data: { error: { message: "not found" } } },
    });
    const { onClearArtifact } = renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "返回产物列表" }),
    );

    expect(onClearArtifact).toHaveBeenCalledOnce();
  });
});
