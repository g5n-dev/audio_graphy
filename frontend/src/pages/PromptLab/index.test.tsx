import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api/services", () => ({
  createPromptCompilation: vi.fn(),
  decidePromptPatches: vi.fn(),
  getPromptArtifactDiff: vi.fn(),
  getPromptLabReadiness: vi.fn(),
  listPromptArtifacts: vi.fn(),
  listPromptGradients: vi.fn(),
  listTaggerVersions: vi.fn(),
  listTagEvaluations: vi.fn(),
}));

import {
  decidePromptPatches,
  getPromptArtifactDiff,
  getPromptLabReadiness,
  listPromptArtifacts,
  listPromptGradients,
  listTagEvaluations,
  listTaggerVersions,
} from "@/api/services";
import { useAuthStore } from "@/stores/auth";

import PromptLabPage from "./index";

const mocks = {
  decide: decidePromptPatches as unknown as ReturnType<typeof vi.fn>,
  readiness: getPromptLabReadiness as unknown as ReturnType<typeof vi.fn>,
  artifacts: listPromptArtifacts as unknown as ReturnType<typeof vi.fn>,
  diff: getPromptArtifactDiff as unknown as ReturnType<typeof vi.fn>,
  gradients: listPromptGradients as unknown as ReturnType<typeof vi.fn>,
  taggers: listTaggerVersions as unknown as ReturnType<typeof vi.fn>,
  evaluations: listTagEvaluations as unknown as ReturnType<typeof vi.fn>,
};

const ARTIFACT = {
  id: 301,
  compilation_id: 9001,
  optimization_run_id: null,
  baseline_tagger_version_id: 12,
  gold_set_version_id: null,
  parent_artifact_id: null,
  candidate_tagger_version_id: null,
  compiler: "builtin" as const,
  compiler_version: "builtin-proposer-v1",
  metric_version: "prompt-lab-metric-v1",
  status: "draft" as const,
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
};

function renderPage(entry = "/prompt-lab") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function LocationProbe() {
    const location = useLocation();
    return (
      <output aria-label="当前查询">{`${location.pathname}${location.search}`}</output>
    );
  }
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <PromptLabPage />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useAuthStore.setState({
    token: "t",
    refreshToken: "r",
    user: {
      id: 1,
      name: "Admin",
      email: "admin@example.com",
      role: "admin",
      tenant_id: "chang_an",
    },
    isAuthenticated: true,
  });
  mocks.readiness.mockReset().mockResolvedValue({
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
  });
  mocks.artifacts.mockReset().mockResolvedValue({ items: [ARTIFACT], total: 1 });
  mocks.diff.mockReset().mockResolvedValue({
    artifact_id: 301,
    status: "draft",
    baseline_prompt: "基线",
    candidate_prompt: "基线",
    patches: [],
    demos: [],
    accepted_patch_ids: [],
    prompt_token_estimate: 300,
    fixed_token_delta: 0,
    input_budget_report: ARTIFACT.input_budget_report,
    redaction_report: { demo_count: 0, by_redaction_mode: {} },
  });
  mocks.decide.mockReset();
  mocks.gradients.mockReset().mockResolvedValue({ items: [], total: 0 });
  mocks.taggers.mockReset().mockResolvedValue({ items: [], total: 0 });
  mocks.evaluations.mockReset().mockResolvedValue({ items: [], total: 0 });
});

describe("PromptLabPage", () => {
  it("offers five real tabs and opens on the readiness view", () => {
    renderPage();

    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(5);
    expect(screen.getByRole("tab", { name: /数据就绪/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("moves between tabs with the arrow keys", async () => {
    renderPage();

    fireEvent.keyDown(screen.getByRole("tab", { name: /数据就绪/ }), {
      key: "ArrowRight",
    });

    expect(screen.getByRole("tab", { name: /编译运行/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("opens the tab named in the query string", () => {
    renderPage("/prompt-lab?tab=gradients");

    expect(screen.getByRole("tab", { name: /梯度与补丁/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("ignores an unknown tab in the query string", () => {
    renderPage("/prompt-lab?tab=nonsense");

    expect(screen.getByRole("tab", { name: /数据就绪/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("selects the artifact named in the query string", async () => {
    renderPage("/prompt-lab?tab=diff&artifact=301");

    expect(await screen.findByRole("heading", { name: "基线 Prompt" })).toBeInTheDocument();
    expect(mocks.diff).toHaveBeenCalledWith(301);
  });

  it.each(["0", "-1", "abc", "01", "99999999999"])(
    "ignores the malformed artifact parameter %s rather than requesting it",
    (raw) => {
      renderPage(`/prompt-lab?tab=diff&artifact=${raw}`);

      expect(mocks.diff).not.toHaveBeenCalled();
      expect(screen.getByText(/先在「编译运行」里选择一个产物/)).toBeInTheDocument();
    },
  );

  it("writes the selection into the URL so the view can be shared", async () => {
    renderPage("/prompt-lab?tab=compile");

    await userEvent.click(
      await screen.findByRole("button", { name: "查看产物 301 的差异" }),
    );

    expect(screen.getByLabelText("当前查询")).toHaveTextContent(
      "/prompt-lab?tab=diff&artifact=301",
    );
  });

  it("stays on the same route when switching tabs", async () => {
    renderPage();

    await userEvent.click(screen.getByRole("tab", { name: /回放对比/ }));

    expect(screen.getByLabelText("当前查询")).toHaveTextContent("/prompt-lab?tab=replay");
  });

  it("hides write actions from an inspector", async () => {
    useAuthStore.setState({
      user: {
        id: 2,
        name: "Inspector",
        email: "inspector@example.com",
        role: "inspector",
        tenant_id: "chang_an",
      },
    });
    renderPage("/prompt-lab?tab=compile");

    await screen.findByRole("button", { name: "查看产物 301 的差异" });
    expect(screen.queryByRole("button", { name: "发起编译" })).not.toBeInTheDocument();
  });

  it("shows write actions to an administrator", async () => {
    renderPage("/prompt-lab?tab=compile");

    expect(await screen.findByRole("button", { name: "发起编译" })).toBeInTheDocument();
  });

  it("links back to the governance centre and the review queue", () => {
    renderPage();

    expect(screen.getByRole("link", { name: "返回标签治理" })).toHaveAttribute(
      "href",
      "/tag-governance",
    );
    expect(screen.getByRole("link", { name: "进入人工复核" })).toHaveAttribute(
      "href",
      "/tag-review",
    );
  });

  it("mounts only the active panel", () => {
    renderPage();

    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
  });

  // 下面几条测的是外壳与面板之间的接线。接错了单个面板的测试全都照样绿，
  // 但用户会卡在旧产物上、或者在空态里没有出路。
  it("moves the selection to the artifact a re-materialisation produced", async () => {
    const patchId = "b".repeat(32);
    const demoId = "d".repeat(32);
    mocks.diff.mockResolvedValue({
      artifact_id: 301,
      status: "draft",
      baseline_prompt: "基线",
      candidate_prompt: `基线\n\n新增一句\n\n示例：\n\n示例甲`,
      patches: [
        {
          patch_id: patchId,
          ordinal: 1,
          kind: "instruction",
          body: "新增一句",
          rationale: "簇 c1 反复漏判",
          cluster_key: "c1",
          support: 12,
        },
      ],
      demos: [
        {
          demo_id: demoId,
          gold_label_id: 5,
          subject_type: "dialogue_unit",
          subject_id: 42,
          rendered_text: "示例甲",
          redaction_mode: "synthetic",
          source_checksum: "e".repeat(64),
          reception_id: 7,
          segment_ids: [1],
          recording_ids: [3],
        },
      ],
      accepted_patch_ids: [patchId],
      prompt_token_estimate: 320,
      fixed_token_delta: 20,
      input_budget_report: ARTIFACT.input_budget_report,
      redaction_report: { demo_count: 1, by_redaction_mode: { synthetic: 1 } },
    });
    mocks.decide.mockResolvedValue({ ...ARTIFACT, id: 302 });
    renderPage("/prompt-lab?tab=diff&artifact=301");

    await userEvent.click(
      await screen.findByRole("button", { name: `剔除示例 ${demoId.slice(0, 8)}` }),
    );
    await userEvent.click(screen.getByRole("button", { name: "提交剔除" }));

    await waitFor(() =>
      expect(screen.getByLabelText("当前查询")).toHaveTextContent(
        "/prompt-lab?tab=diff&artifact=302",
      ),
    );
  });

  it("gives the diff view a way out when no artifact is selected", async () => {
    renderPage("/prompt-lab?tab=diff");

    await userEvent.click(screen.getByRole("button", { name: "前往编译运行" }));

    expect(screen.getByRole("tab", { name: /编译运行/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("clears a stale artifact id when the diff endpoint reports it is gone", async () => {
    mocks.diff.mockRejectedValue(
      Object.assign(new Error("not found"), { response: { status: 404 } }),
    );
    renderPage("/prompt-lab?tab=diff&artifact=301");

    await userEvent.click(await screen.findByRole("button", { name: "返回产物列表" }));

    expect(screen.getByLabelText("当前查询")).toHaveTextContent("/prompt-lab?tab=diff");
    expect(screen.getByLabelText("当前查询")).not.toHaveTextContent("artifact=");
  });

  it("retries the readiness query from the error state", async () => {
    mocks.readiness.mockRejectedValue(new Error("boom"));
    renderPage();

    await screen.findByRole("button", { name: "重新加载" });
    mocks.readiness.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));

    await waitFor(() => expect(mocks.readiness).toHaveBeenCalled());
  });
});
