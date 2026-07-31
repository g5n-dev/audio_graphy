import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/auth";
import TagReviewPage from "./index";

vi.mock("@/api/services", () => ({
  adjudicateTagReview: vi.fn(),
  claimTagReview: vi.fn(),
  decideTagReview: vi.fn(),
  listTagSchemas: vi.fn(),
  listTagReviews: vi.fn(),
  releaseTagReview: vi.fn(),
}));

import {
  adjudicateTagReview,
  claimTagReview,
  decideTagReview,
  listTagSchemas,
  listTagReviews,
  releaseTagReview,
} from "@/api/services";

const mockedList = listTagReviews as unknown as ReturnType<typeof vi.fn>;
const mockedClaim = claimTagReview as unknown as ReturnType<typeof vi.fn>;
const mockedDecide = decideTagReview as unknown as ReturnType<typeof vi.fn>;
const mockedAdjudicate =
  adjudicateTagReview as unknown as ReturnType<typeof vi.fn>;
const mockedSchemas = listTagSchemas as unknown as ReturnType<typeof vi.fn>;
const mockedRelease = releaseTagReview as unknown as ReturnType<typeof vi.fn>;

const QUEUE = {
  items: [
    {
      id: 501,
      tenant_id: "tenant-a",
      batch_id: "review-low-confidence-1",
      reason: "low_confidence",
      subject_type: "dialogue_unit",
      subject_id: 77,
      reception_id: 101,
      tag_key: "intent",
      proposed_value: "browse",
      confidence: 0.58,
      queue_purpose: "active_learning",
      blind_mode: false,
      truth_tier: "T1",
      allowed_values: ["browse", "purchase"],
      sampling_probability: 0.25,
      status: "pending",
      claimed_by: null,
      assignee_user_id: null,
      evidence_refs: [
        {
          recording_id: 9,
          segment_id: 12,
          start_sec: 3.1,
          end_sec: 5.8,
          text_excerpt: "今天合适的话就签约",
        },
      ],
      created_at: "2026-07-25T01:00:00Z",
      updated_at: "2026-07-25T01:00:00Z",
    },
  ],
  total: 1,
};

const ADJUDICATION_QUEUE = {
  items: [
    {
      ...QUEUE.items[0],
      status: "claimed",
      claimed_by: 9,
      reason: "adjudication",
      queue_purpose: "adjudication",
      blind_mode: true,
      truth_tier: "t3",
      reviewer_round: 3,
    },
  ],
  total: 1,
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TagReviewPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TagReviewPage", () => {
  beforeEach(() => {
    useAuthStore.setState({
      token: "inspector-token",
      refreshToken: "inspector-refresh",
      user: {
        id: 9,
        name: "复核员",
        email: "reviewer@example.com",
        role: "inspector",
        tenant_id: "tenant-a",
      },
      isAuthenticated: true,
    });
    mockedList.mockReset();
    mockedClaim.mockReset();
    mockedDecide.mockReset();
    mockedAdjudicate.mockReset();
    mockedSchemas.mockReset();
    mockedRelease.mockReset();
    mockedList.mockResolvedValue(QUEUE);
    mockedSchemas.mockResolvedValue({ items: [], total: 0 });
    mockedClaim.mockResolvedValue({
      ...QUEUE.items[0],
      status: "claimed",
      claimed_by: 9,
    });
    mockedRelease.mockResolvedValue({
      ...QUEUE.items[0],
      status: "pending",
      claimed_by: null,
      claimed_at: null,
    });
    mockedDecide.mockResolvedValue({
      task: { ...QUEUE.items[0], status: "resolved" },
      fact: {
        id: 700,
        source: "manual",
        tag_key: "intent",
        tag_value: "purchase",
      },
    });
    mockedAdjudicate.mockResolvedValue({
      task: { ...QUEUE.items[0], status: "resolved" },
      fact: {
        id: 701,
        source: "manual",
        tag_key: "intent",
        tag_value: "browse",
      },
    });
  });

  it("keeps evidence, decision and audio trace in one review workspace", async () => {
    const view = renderPage();
    expect(
      await screen.findByRole("heading", { name: "人工复核工作台" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("今天合适的话就签约"),
    ).toBeInTheDocument();
    expect(screen.getByText("58%")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "跳转调听 00:03" }),
    ).toHaveAttribute(
      "href",
      "/receptions/101/workspace?recording=9&at=3100",
    );
    expect(view.container.querySelector(".ag-evidence-wave")).toBeNull();
    expect(
      screen.getByRole("radiogroup", { name: "复核结论" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "请先领取任务" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "放弃任务/释放领取" }),
    ).not.toBeInTheDocument();
  });

  it("does not invent an invalid audio route without reception context", async () => {
    mockedList.mockResolvedValueOnce({
      items: [{ ...QUEUE.items[0], reception_id: null }],
      total: 1,
    });
    renderPage();

    expect(
      await screen.findByText("今天合适的话就签约"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /跳转调听/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("缺少接待上下文，暂不能跳转调听"),
    ).toBeInTheDocument();
  });

  it("claims then corrects a tag with reason and evidence", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("今天合适的话就签约");

    await user.click(screen.getByRole("button", { name: "领取任务" }));
    await waitFor(() => expect(mockedClaim).toHaveBeenCalledWith(501));

    await user.click(screen.getByRole("radio", { name: "纠正标签" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "纠正后的标签值" }),
      "purchase",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "主要错误层" }),
      "tag_reasoning",
    );
    await user.click(
      screen.getByRole("checkbox", { name: "模型误判" }),
    );
    await user.click(
      screen.getByRole("checkbox", { name: "证据确认" }),
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "复核置信度" }),
      "0.9",
    );
    await user.type(
      screen.getByRole("textbox", { name: "复核备注" }),
      "客户明确表示签约",
    );
    await user.click(screen.getByRole("button", { name: "提交复核" }));

    await waitFor(() => {
      expect(mockedDecide).toHaveBeenCalledWith(501, {
        action: "correct",
        truth_state: "present",
        corrected_value: "purchase",
        primary_failure_stage: "tag_reasoning",
        reason_code: "model_misread",
        reason_codes: ["model_misread", "evidence_confirmed"],
        reviewer_confidence: 0.9,
        review_duration_ms: expect.any(Number),
        note: "客户明确表示签约",
        evidence_refs: QUEUE.items[0].evidence_refs,
      });
    });
    expect(await screen.findByText("复核已写入人工事实")).toBeVisible();
  });

  it("loads enum correction values from the task schema when the queue omits them", async () => {
    const user = userEvent.setup();
    const taskWithoutValues = {
      ...QUEUE.items[0],
      schema_version_id: 11,
      allowed_values: undefined,
    };
    mockedList.mockResolvedValueOnce({
      items: [taskWithoutValues],
      total: 1,
    });
    mockedClaim.mockResolvedValueOnce({
      ...taskWithoutValues,
      status: "claimed",
      claimed_by: 9,
    });
    mockedSchemas.mockResolvedValueOnce({
      items: [
        {
          id: 3,
          versions: [
            {
              id: 11,
              definitions: [
                {
                  key: "intent",
                  subject_types: ["dialogue_unit"],
                  allowed_values: ["browse", "purchase"],
                },
              ],
            },
          ],
        },
      ],
      total: 1,
    });
    renderPage();
    await screen.findByText("今天合适的话就签约");

    await user.click(screen.getByRole("button", { name: "领取任务" }));
    await waitFor(() => expect(mockedClaim).toHaveBeenCalledWith(501));
    await user.click(screen.getByRole("radio", { name: "纠正标签" }));

    const correction = screen.getByRole("combobox", {
      name: "纠正后的标签值",
    });
    expect(correction).toHaveDisplayValue("从 Schema 值域选择");
    expect(
      screen.getByRole("option", { name: "purchase" }),
    ).toBeInTheDocument();
  });

  it("does not preselect acceptance and requires an explicit conclusion", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("今天合适的话就签约");

    expect(
      screen.getByRole("radio", { name: "接受建议" }),
    ).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "领取任务" }));
    await waitFor(() => expect(mockedClaim).toHaveBeenCalledWith(501));
    await user.click(screen.getByRole("button", { name: "提交复核" }));

    expect(
      await screen.findByRole("alert"),
    ).toHaveTextContent("请选择复核结论");
    expect(mockedDecide).not.toHaveBeenCalled();
  });

  it("keeps a task claimed by another reviewer explicitly read-only", async () => {
    mockedList.mockResolvedValueOnce({
      items: [
        {
          ...QUEUE.items[0],
          status: "claimed",
          claimed_by: 88,
        },
      ],
      total: 1,
    });
    renderPage();

    expect(
      await screen.findByText(
        "该任务已由复核员 #88 领取，当前仅可查看，不能提交结论。",
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "非本人领取，仅可查看" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("radio", { name: "接受建议" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "放弃任务/释放领取" }),
    ).not.toBeInTheDocument();
    expect(mockedDecide).not.toHaveBeenCalled();
  });

  it("releases the current claim only after confirmation and refreshes the queue", async () => {
    const claimedBlindTask = {
      ...QUEUE.items[0],
      status: "claimed",
      claimed_by: 9,
      proposed_value: null,
      confidence: null,
      evidence_refs: [],
      blind_mode: true,
    };
    mockedList.mockResolvedValue({
      items: [claimedBlindTask],
      total: 1,
    });
    mockedRelease.mockResolvedValueOnce({
      ...claimedBlindTask,
      status: "pending",
      subject_id: null,
      reception_id: null,
      claimed_by: null,
      claimed_at: null,
    });
    const confirm = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    const user = userEvent.setup();
    renderPage();

    const releaseButton = await screen.findByRole("button", {
      name: "放弃任务/释放领取",
    });
    await user.click(releaseButton);
    expect(mockedRelease).not.toHaveBeenCalled();

    await user.click(releaseButton);
    await waitFor(() => expect(mockedRelease).toHaveBeenCalledWith(501));
    await waitFor(() =>
      expect(mockedList.mock.calls.length).toBeGreaterThanOrEqual(2),
    );
    expect(
      await screen.findByText("任务 #501 已释放，已返回待领取队列"),
    ).toBeVisible();
    expect(confirm).toHaveBeenCalledWith(
      "确认放弃任务 #501 并释放领取吗？任务将回到待领取队列，未提交的复核输入不会保留。",
    );
    expect(String(confirm.mock.calls.at(-1)?.[0])).not.toContain("browse");
    expect(screen.queryByText("browse")).not.toBeInTheDocument();
    expect(screen.queryByText("58%")).not.toBeInTheDocument();
    confirm.mockRestore();
  });

  it("shows the existing operation error when releasing a claim fails", async () => {
    mockedList.mockResolvedValue({
      items: [
        {
          ...QUEUE.items[0],
          status: "claimed",
          claimed_by: 9,
        },
      ],
      total: 1,
    });
    mockedRelease.mockRejectedValueOnce(new Error("释放领取失败，请稍后重试"));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderPage();

    await user.click(
      await screen.findByRole("button", {
        name: "放弃任务/释放领取",
      }),
    );

    expect(
      await screen.findByRole("alert"),
    ).toHaveTextContent("释放领取失败，请稍后重试");
    confirm.mockRestore();
  });

  it("keeps representative audits blind until the decision is submitted", async () => {
    const pendingBlindTask = {
      ...QUEUE.items[0],
      subject_id: null,
      reception_id: null,
      proposed_value: null,
      proposed_fact_id: null,
      schema_version_id: null,
      tagger_version_id: null,
      confidence: null,
      evidence_refs: [],
      created_by: null,
      queue_purpose: "representative_audit",
      blind_mode: true,
      truth_tier: "T3",
    };
    mockedList.mockResolvedValue({
      items: [pendingBlindTask],
      total: 1,
    });
    mockedClaim.mockResolvedValueOnce({
      ...pendingBlindTask,
      subject_id: 77,
      reception_id: 101,
      schema_version_id: 11,
      status: "claimed",
      claimed_by: 9,
    });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("随机质量审计")).toBeVisible();
    expect(screen.getByText("盲审模式")).toBeVisible();
    expect(screen.getAllByText("领取后揭示主体").length).toBeGreaterThan(0);
    expect(screen.queryByText(/dialogue_unit #77/)).not.toBeInTheDocument();
    expect(screen.queryByText("browse")).not.toBeInTheDocument();
    expect(screen.queryByText("58%")).not.toBeInTheDocument();
    expect(screen.queryByText("今天合适的话就签约")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "打开中立音频 / 转写上下文" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("radio", { name: "接受建议" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("radio", { name: "标注真实标签" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "领取任务" }));
    await waitFor(() => expect(mockedClaim).toHaveBeenCalledWith(501));
    expect(
      (await screen.findAllByText("dialogue_unit #77")).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByRole("link", { name: "打开中立音频 / 转写上下文" }),
    ).toHaveAttribute("href", "/receptions/101/workspace");
    expect(
      screen.getByText(
        "工作台会在本次盲审提交前脱敏标签结论、状态演化和语义溯源，仅保留中立的音频与转写。",
      ),
    ).toBeVisible();
    expect(screen.queryByText("browse")).not.toBeInTheDocument();
    expect(screen.queryByText("58%")).not.toBeInTheDocument();
    expect(screen.queryByText("今天合适的话就签约")).not.toBeInTheDocument();
  });

  it("does not offer neutral blind context without both claim and reception", async () => {
    const claimedWithoutReception = {
      ...QUEUE.items[0],
      status: "claimed",
      claimed_by: 9,
      reception_id: null,
      proposed_value: null,
      confidence: null,
      evidence_refs: [],
      blind_mode: true,
    };
    mockedList.mockResolvedValue({
      items: [claimedWithoutReception],
      total: 1,
    });
    renderPage();

    expect(await screen.findByText("盲审模式")).toBeVisible();
    expect(
      screen.queryByRole("link", { name: "打开中立音频 / 转写上下文" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("browse")).not.toBeInTheDocument();
    expect(screen.queryByText("今天合适的话就签约")).not.toBeInTheDocument();
  });

  it("can remove and crop evidence before escalating an uncertain case", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("今天合适的话就签约");

    await user.click(screen.getByRole("button", { name: "领取任务" }));
    await waitFor(() => expect(mockedClaim).toHaveBeenCalledWith(501));
    await user.click(screen.getByRole("radio", { name: "升级仲裁" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "主要错误层" }),
      "evidence",
    );
    await user.click(
      screen.getByRole("checkbox", { name: "保留证据 1" }),
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "复核置信度" }),
      "0.6",
    );
    await user.click(screen.getByRole("button", { name: "提交复核" }));

    await waitFor(() => {
      expect(mockedDecide).toHaveBeenCalledWith(501, {
        action: "escalate",
        truth_state: "uncertain",
        primary_failure_stage: "evidence",
        reason_code: "needs_adjudication",
        reason_codes: ["needs_adjudication"],
        reviewer_confidence: 0.6,
        review_duration_ms: expect.any(Number),
        evidence_refs: [],
      });
    });
  });

  it("records claimed review elapsed time as structured feedback", async () => {
    const user = userEvent.setup();
    const now = vi.spyOn(Date, "now").mockReturnValue(100_000);
    try {
      renderPage();
      await screen.findByText("今天合适的话就签约");

      await user.click(screen.getByRole("button", { name: "领取任务" }));
      await waitFor(() => expect(mockedClaim).toHaveBeenCalledWith(501));
      now.mockReturnValue(106_500);

      await user.click(screen.getByRole("radio", { name: "接受建议" }));
      await user.selectOptions(
        screen.getByRole("combobox", { name: "复核置信度" }),
        "0.9",
      );
      await user.click(screen.getByRole("button", { name: "提交复核" }));

      await waitFor(() => {
        expect(mockedDecide).toHaveBeenCalledWith(
          501,
          expect.objectContaining({
            action: "accept",
            truth_state: "present",
            review_duration_ms: 6_500,
          }),
        );
      });
    } finally {
      now.mockRestore();
    }
  });

  it("uses the explicit adjudication endpoint for arbitration tasks", async () => {
    const user = userEvent.setup();
    mockedList.mockResolvedValueOnce(ADJUDICATION_QUEUE);
    renderPage();
    await screen.findByText("分歧仲裁");

    expect(
      screen.queryByRole("radio", { name: "接受建议" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("radio", { name: "无法确定" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("radio", { name: "升级仲裁" }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("radio", { name: "标签不存在 / 不适用" }),
    );
    expect(
      screen.queryByRole("option", { name: "不适用于当前主体" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "标注真实标签" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "纠正后的标签值" }),
      "purchase",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "主要错误层" }),
      "tag_reasoning",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "复核置信度" }),
      "0.9",
    );
    await user.click(screen.getByRole("button", { name: "提交复核" }));

    await waitFor(() => {
      expect(mockedAdjudicate).toHaveBeenCalledWith(501, {
        action: "correct",
        truth_state: "present",
        corrected_value: "purchase",
        primary_failure_stage: "tag_reasoning",
        reason_code: "insufficient_evidence",
        reason_codes: ["insufficient_evidence"],
        reviewer_confidence: 0.9,
        review_duration_ms: expect.any(Number),
        evidence_refs: QUEUE.items[0].evidence_refs,
      });
      expect(mockedAdjudicate.mock.calls.at(-1)?.[1]).not.toHaveProperty(
        "truth_tier",
      );
      expect(mockedAdjudicate.mock.calls.at(-1)?.[1]).not.toHaveProperty(
        "annotator_round",
      );
      expect(mockedAdjudicate.mock.calls.at(-1)?.[1]).not.toHaveProperty(
        "adjudication",
      );
    });
    expect(mockedDecide).not.toHaveBeenCalled();
  });

  it("submits an arbitration rejection with an explicit definitive state", async () => {
    const user = userEvent.setup();
    mockedList.mockResolvedValueOnce(ADJUDICATION_QUEUE);
    renderPage();
    await screen.findByText("分歧仲裁");

    await user.click(
      screen.getByRole("radio", { name: "标签不存在 / 不适用" }),
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "标签状态" }),
      "not_applicable",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "主要错误层" }),
      "evidence",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "复核置信度" }),
      "0.9",
    );
    await user.click(screen.getByRole("button", { name: "提交复核" }));

    await waitFor(() => {
      expect(mockedAdjudicate).toHaveBeenCalledWith(
        501,
        expect.objectContaining({
          action: "reject",
          truth_state: "not_applicable",
          primary_failure_stage: "evidence",
        }),
      );
    });
    expect(mockedDecide).not.toHaveBeenCalled();
  });

  it("polls non-terminal review queues every three seconds", async () => {
    vi.useFakeTimers();
    try {
      renderPage();
      await vi.waitFor(() => {
        expect(mockedList).toHaveBeenCalledTimes(1);
        expect(mockedList).toHaveBeenLastCalledWith({ status: "active" });
      });
      await vi.advanceTimersByTimeAsync(3_000);
      await vi.waitFor(() =>
        expect(mockedList.mock.calls.length).toBeGreaterThanOrEqual(2),
      );
      expect(mockedList).toHaveBeenLastCalledWith({ status: "active" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps active work and semantic history as explicit queue modes", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("今天合适的话就签约");
    expect(mockedList).toHaveBeenLastCalledWith({ status: "active" });

    await user.selectOptions(
      screen.getByRole("combobox", { name: "任务状态" }),
      "resolved",
    );
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith({ status: "resolved" }),
    );

    await user.selectOptions(
      screen.getByRole("combobox", { name: "任务状态" }),
      "all",
    );
    await waitFor(() =>
      expect(mockedList).toHaveBeenLastCalledWith({ status: "all" }),
    );
  });

  it("provides retry and empty states", async () => {
    mockedList.mockRejectedValueOnce(new Error("队列不可用"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("队列不可用");
    mockedList.mockResolvedValueOnce({ items: [], total: 0 });
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(await screen.findByText("当前没有待复核任务")).toBeInTheDocument();
  });
});
