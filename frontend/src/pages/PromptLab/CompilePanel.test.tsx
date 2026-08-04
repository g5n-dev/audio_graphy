import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api/services", () => ({
  createPromptCompilation: vi.fn(),
  listPromptArtifacts: vi.fn(),
  listTaggerVersions: vi.fn(),
}));

import {
  createPromptCompilation,
  listPromptArtifacts,
  listTaggerVersions,
} from "@/api/services";
import type { PromptArtifactSummary, PromptLabReadiness } from "@/types/api";

import { CompilePanel } from "./CompilePanel";

const mocks = {
  create: createPromptCompilation as unknown as ReturnType<typeof vi.fn>,
  list: listPromptArtifacts as unknown as ReturnType<typeof vi.fn>,
  taggers: listTaggerVersions as unknown as ReturnType<typeof vi.fn>,
};

function artifact(overrides: Partial<PromptArtifactSummary> = {}): PromptArtifactSummary {
  return {
    id: 301,
    compilation_id: 9001,
    optimization_run_id: null,
    baseline_tagger_version_id: 12,
    gold_set_version_id: null,
    parent_artifact_id: null,
    candidate_tagger_version_id: null,
    compiler: "builtin",
    compiler_version: "builtin-proposer-v1",
    metric_version: "prompt-lab-metric-v1",
    status: "draft",
    prompt_token_estimate: 820,
    accepted_patch_ids: ["a".repeat(32)],
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

const READY: PromptLabReadiness = {
  tenant_id: "chang_an",
  ready: true,
  gold_label_total: 400,
  silver_label_total: 0,
  feedback_total: 400,
  feedback_threshold: 200,
  domain_threshold: 30,
  frozen_gold_set_versions: 2,
  pending_artifacts: 1,
  annotation_hours_remaining: 0,
  domains: [],
  blockers: [],
};

function renderPanel(
  props: Partial<React.ComponentProps<typeof CompilePanel>> = {},
) {
  const onSelectArtifact = vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <CompilePanel
        isAdmin
        readiness={READY}
        selectedArtifactId={null}
        onSelectArtifact={onSelectArtifact}
        {...props}
      />
    </QueryClientProvider>,
  );
  return { onSelectArtifact };
}

beforeEach(() => {
  mocks.create.mockReset().mockResolvedValue({ compilation_id: 9002, job_id: 55 });
  mocks.list.mockReset().mockResolvedValue({ items: [artifact()], total: 1 });
  mocks.taggers
    .mockReset()
    .mockResolvedValue({ items: [{ id: 12, version: "baseline-v1" }], total: 1 });
});

async function openDialog() {
  await userEvent.click(screen.getByRole("button", { name: "发起编译" }));
  return screen.getByRole("dialog", { name: "发起 Prompt 编译" });
}

describe("CompilePanel", () => {
  it("disables compilation and explains why when preconditions are unmet", async () => {
    renderPanel({ readiness: { ...READY, ready: false, blockers: ["x"] } });

    expect(screen.getByRole("button", { name: "发起编译" })).toBeDisabled();
    expect(await screen.findByText(/编译前置条件尚未满足/)).toBeInTheDocument();
  });

  it("hides every write action from a non-admin", async () => {
    renderPanel({ isAdmin: false });

    await waitFor(() => expect(mocks.list).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "发起编译" })).not.toBeInTheDocument();
  });

  it("offers only the two redaction modes the API accepts", async () => {
    renderPanel();
    const dialog = await openDialog();

    const select = within(dialog).getByLabelText("示例脱敏方式");
    const options = within(select).getAllByRole("option").map((o) => o.getAttribute("value"));
    expect(options).toEqual(["synthetic", "masked"]);
    expect(options).not.toContain("verbatim");
  });

  it("offers only the three demo counts the API accepts", async () => {
    renderPanel();
    const dialog = await openDialog();

    const select = within(dialog).getByLabelText("内联示例条数");
    expect(within(select).getAllByRole("option").map((o) => o.getAttribute("value"))).toEqual(
      ["0", "2", "4"],
    );
  });

  // 每条都逐字对应 backend/audio_graphy/schemas/prompt_lab.py 的 Field 约束。
  // 漏掉一条的代价是用户填完整个表单、点了提交，才吃到一个 422。
  it.each([
    ["补丁上限", "99", "补丁上限必须是 1 到 32 之间的整数。"],
    ["最小簇支撑", "101", "最小簇支撑必须是 1 到 100 之间的整数。"],
    ["Prompt token 上限", "100", "Prompt token 上限必须是 512 到 8192 之间的整数。"],
    ["调用次数上限", "1001", "调用次数上限必须是 1 到 1000 之间的整数。"],
    ["Token 上限", "999", "Token 上限必须是 1000 到 50000000 之间的整数。"],
    ["耗时上限", "59", "耗时上限必须是 60 到 7200 之间的整数。"],
    // 后端只有 ge=1，没有上界——提示语就不该编一个出来。
    ["成本上限", "0", "成本上限必须是不小于 1 的整数。"],
  ])("rejects %s=%s before calling the API", async (label, raw, message) => {
    renderPanel();
    const dialog = await openDialog();

    await userEvent.selectOptions(within(dialog).getByLabelText("基线抽取版本"), "12");
    const field = within(dialog).getByLabelText(label);
    await userEvent.clear(field);
    await userEvent.type(field, raw);
    await userEvent.click(within(dialog).getByRole("button", { name: "发起编译" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("rejects a cost ceiling that JavaScript cannot represent exactly", async () => {
    renderPanel();
    const dialog = await openDialog();

    await userEvent.selectOptions(within(dialog).getByLabelText("基线抽取版本"), "12");
    const field = within(dialog).getByLabelText("成本上限");
    await userEvent.clear(field);
    // BIGINT 列存得下这个值，但 Number() 会把它取整成 …992 再发出去。
    await userEvent.type(field, "9007199254740993");
    await userEvent.click(within(dialog).getByRole("button", { name: "发起编译" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "成本上限必须是不小于 1 的整数。",
    );
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("carries every non-default selection into the request body", async () => {
    renderPanel();
    const dialog = await openDialog();

    await userEvent.selectOptions(within(dialog).getByLabelText("基线抽取版本"), "12");
    await userEvent.selectOptions(within(dialog).getByLabelText("内联示例条数"), "4");
    await userEvent.selectOptions(within(dialog).getByLabelText("示例脱敏方式"), "masked");
    await userEvent.selectOptions(
      within(dialog).getByLabelText("效率封套"),
      "token_reduction_v1",
    );
    const support = within(dialog).getByLabelText("最小簇支撑");
    await userEvent.clear(support);
    await userEvent.type(support, "5");
    await userEvent.click(within(dialog).getByRole("button", { name: "发起编译" }));

    await waitFor(() => expect(mocks.create).toHaveBeenCalled());
    expect(mocks.create.mock.calls[0][0]).toMatchObject({
      compiler: {
        demo_count: 4,
        redaction_mode: "masked",
        min_cluster_support: 5,
        efficiency_policy: "token_reduction_v1",
      },
    });
  });

  it("requires a baseline before it will submit", async () => {
    renderPanel();
    const dialog = await openDialog();

    await userEvent.click(within(dialog).getByRole("button", { name: "发起编译" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("请选择一个基线抽取版本。");
  });

  it("presents the budget as a ceiling, not as an estimated bill", async () => {
    renderPanel();
    const dialog = await openDialog();

    expect(within(dialog).getByText("预算上限（非预估账单）")).toBeInTheDocument();
    expect(within(dialog).getByText("≤ ¥2.00")).toBeInTheDocument();
    expect(within(dialog).getByText("≤ 30 分钟")).toBeInTheDocument();
  });

  it("derives the per-call token ceiling from the total and the call count", async () => {
    renderPanel();
    const dialog = await openDialog();

    // 1,500,000 / 120 = 12,500
    expect(within(dialog).getByText("≤ 12,500 token")).toBeInTheDocument();
  });

  it("announces that the job is queued after a successful submission", async () => {
    renderPanel();
    const dialog = await openDialog();

    await userEvent.selectOptions(within(dialog).getByLabelText("基线抽取版本"), "12");
    await userEvent.click(within(dialog).getByRole("button", { name: "发起编译" }));

    expect(await screen.findByRole("status")).toHaveTextContent("编译任务已入队");
    expect(mocks.create).toHaveBeenCalledOnce();
  });

  it("surfaces a creation failure inside the dialog", async () => {
    mocks.create.mockRejectedValue(new Error("boom"));
    renderPanel();
    const dialog = await openDialog();

    await userEvent.selectOptions(within(dialog).getByLabelText("基线抽取版本"), "12");
    await userEvent.click(within(dialog).getByRole("button", { name: "发起编译" }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("puts the status filter into the query so switching refetches", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.list).toHaveBeenCalledWith({ limit: 50 }));

    await userEvent.selectOptions(screen.getByLabelText("按状态筛选产物"), "draft");

    await waitFor(() =>
      expect(mocks.list).toHaveBeenCalledWith({ status: "draft", limit: 50 }),
    );
  });

  it("shows the compiler, metric version and accepted patch count on each card", async () => {
    renderPanel();

    expect(await screen.findByText(/builtin-proposer-v1/)).toBeInTheDocument();
    expect(screen.getByText(/1 条已采纳/)).toBeInTheDocument();
  });

  it("flags an artifact that does not fit the input budget", async () => {
    mocks.list.mockResolvedValue({
      items: [
        artifact({
          input_budget_report: { ...artifact().input_budget_report, fits: false },
        }),
      ],
      total: 1,
    });
    renderPanel();

    expect(await screen.findByText("超出单次输入预算")).toBeInTheDocument();
  });

  it("notes when an artifact came from an earlier review", async () => {
    mocks.list.mockResolvedValue({
      items: [artifact({ parent_artifact_id: 300 })],
      total: 1,
    });
    renderPanel();

    expect(await screen.findByText(/由 #300 的复核决策派生/)).toBeInTheDocument();
  });

  it("selects an artifact when its card is activated", async () => {
    const { onSelectArtifact } = renderPanel();

    await userEvent.click(
      await screen.findByRole("button", { name: "查看产物 301 的差异" }),
    );

    expect(onSelectArtifact).toHaveBeenCalledWith(301);
  });

  it("invites a first compilation when the list is empty", async () => {
    mocks.list.mockResolvedValue({ items: [], total: 0 });
    renderPanel();

    expect(await screen.findByText("尚无编译产物")).toBeInTheDocument();
  });
});
