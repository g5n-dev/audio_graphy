import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { PromptLabReadiness } from "@/types/api";

import { ReadinessPanel } from "./ReadinessPanel";

function readiness(overrides: Partial<PromptLabReadiness> = {}): PromptLabReadiness {
  return {
    tenant_id: "chang_an",
    ready: false,
    gold_label_total: 120,
    silver_label_total: 400,
    feedback_total: 120,
    feedback_threshold: 200,
    domain_threshold: 30,
    frozen_gold_set_versions: 1,
    pending_artifacts: 0,
    annotation_hours_remaining: 2.5,
    domains: [
      {
        domain: "dialogue_unit:intent",
        gold_count: 80,
        silver_count: 200,
        feedback_count: 80,
        meets_threshold: true,
      },
      {
        domain: "reception:compliance_risk",
        gold_count: 12,
        silver_count: 40,
        feedback_count: 12,
        meets_threshold: false,
      },
    ],
    blockers: ["reviewed_feedback_below_200"],
    ...overrides,
  };
}

function renderPanel(overrides: Partial<PromptLabReadiness> | null = {}) {
  const onRetry = vi.fn();
  render(
    <MemoryRouter>
      <ReadinessPanel
        data={overrides === null ? undefined : readiness(overrides)}
        pending={false}
        error={null}
        onRetry={onRetry}
      />
    </MemoryRouter>,
  );
  return { onRetry };
}

describe("ReadinessPanel", () => {
  it("shows the review threshold as progress towards its target", () => {
    renderPanel();

    expect(
      screen.getByRole("progressbar", { name: "已复核反馈：120 / 200 条" }),
    ).toBeInTheDocument();
  });

  it("marks the overall precondition as blocked while blockers remain", () => {
    renderPanel();

    expect(screen.getByRole("status", { name: "前置条件未满足" })).toHaveTextContent(
      "尚不可编译",
    );
  });

  it("marks the precondition as met once nothing blocks it", () => {
    renderPanel({ ready: true, blockers: [] });

    expect(screen.getByRole("status", { name: "前置条件已满足" })).toHaveTextContent(
      "可以编译",
    );
  });

  it("lays the coverage matrix out by subject type and tag key", () => {
    renderPanel();

    const matrix = screen.getByRole("table", { name: "已复核样本覆盖矩阵" });
    expect(within(matrix).getByRole("rowheader", { name: "dialogue_unit" })).toBeInTheDocument();
    expect(within(matrix).getByRole("columnheader", { name: "intent" })).toBeInTheDocument();
  });

  it("says how many more samples a domain below the threshold still needs", () => {
    renderPanel();

    expect(
      screen.getByRole("cell", {
        name: "reception / compliance_risk：金标 12，银标 40，距门槛还差 18 条",
      }),
    ).toBeInTheDocument();
  });

  it("reports a domain that has cleared the threshold as met", () => {
    renderPanel();

    expect(
      screen.getByRole("cell", {
        name: "dialogue_unit / intent：金标 80，银标 200，已达门槛",
      }),
    ).toBeInTheDocument();
  });

  it("marks a combination with no reviewed samples as empty rather than zero", () => {
    renderPanel();

    expect(
      screen.getByRole("cell", {
        name: "dialogue_unit / compliance_risk：暂无已复核样本，距门槛还差 30 条",
      }),
    ).toHaveTextContent("—");
  });

  it("derives the colour thresholds from the payload instead of hard-coding them", () => {
    renderPanel({ domain_threshold: 100 });

    // 门槛提高后，原本达标的 80 条变成不达标。
    expect(
      screen.getByRole("cell", {
        name: "dialogue_unit / intent：金标 80，银标 200，距门槛还差 20 条",
      }),
    ).toBeInTheDocument();
  });

  it("translates a blocker code into copy with somewhere to go", () => {
    renderPanel();

    expect(screen.getByText(/已复核反馈不足 200 条/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "进入人工复核" })).toHaveAttribute(
      "href",
      "/tag-review",
    );
  });

  it("explains a per-domain blocker and links to the insights page", () => {
    renderPanel({
      blockers: ["domain_support_below_30:reception:compliance_risk"],
    });

    expect(
      screen.getByText(/组合 reception:compliance_risk 的已复核样本不足/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看标签洞察" })).toBeInTheDocument();
  });

  it("shows an unrecognised blocker code verbatim rather than dropping it", () => {
    renderPanel({ blockers: ["brand_new_backend_blocker"] });

    expect(screen.getByText("brand_new_backend_blocker")).toBeInTheDocument();
  });

  it("prices the remaining gap in human hours and says how it was estimated", () => {
    renderPanel();

    expect(screen.getByText(/2.5 小时/)).toBeInTheDocument();
    expect(screen.getByText(/按每条 5 分钟估算/)).toBeInTheDocument();
  });

  it("says every combination is covered once no annotation remains", () => {
    renderPanel({ annotation_hours_remaining: 0 });

    expect(screen.getByText("所有组合的样本量均已达标。")).toBeInTheDocument();
  });

  it("points at the review queue when nothing has been reviewed yet", () => {
    renderPanel({ domains: [] });

    expect(screen.getByText(/尚无任何已复核组合/)).toBeInTheDocument();
  });

  it("hides the cold-start section once there is nothing blocking", () => {
    renderPanel({ ready: true, blockers: [] });

    expect(screen.queryByText("还差什么")).not.toBeInTheDocument();
  });

  it("offers a retry when the readiness check itself failed", () => {
    const onRetry = vi.fn();
    render(
      <MemoryRouter>
        <ReadinessPanel
          data={undefined}
          pending={false}
          error={new Error("boom")}
          onRetry={onRetry}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});
