import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { statusLabel, statusTone } from "./format";
import { StatusChip } from "./StatusChip";

describe("statusTone", () => {
  it("treats published, production and completed as healthy", () => {
    expect(statusTone("published")).toBe("success");
    expect(statusTone("production")).toBe("success");
    expect(statusTone("completed")).toBe("success");
  });

  it("treats failure and rollback as dangerous", () => {
    expect(statusTone("failed")).toBe("danger");
    expect(statusTone("rolled_back")).toBe("danger");
  });

  it("treats pending approval and any canary stage as a warning", () => {
    expect(statusTone("awaiting_admin")).toBe("warning");
    expect(statusTone("canary_5")).toBe("warning");
    expect(statusTone("canary_25")).toBe("warning");
  });

  it("falls back to the informational tone for everything else", () => {
    expect(statusTone("draft")).toBe("info");
    expect(statusTone("brand_new_status")).toBe("info");
  });
});

describe("statusLabel", () => {
  it("translates known governance statuses", () => {
    expect(statusLabel("qualified")).toBe("已达标");
    expect(statusLabel("rolled_back")).toBe("已回滚");
  });

  it("returns an unknown status verbatim", () => {
    expect(statusLabel("brand_new_status")).toBe("brand_new_status");
  });

  it("lets a caller override a status whose meaning differs in its domain", () => {
    // rejected 在标签治理里是「未达标」，在 Prompt 实验室里是「已拒绝」。
    expect(statusLabel("rejected")).toBe("未达标");
    expect(statusLabel("rejected", { rejected: "已拒绝" })).toBe("已拒绝");
  });

  it("falls back to the default table for statuses the override does not mention", () => {
    expect(statusLabel("qualified", { rejected: "已拒绝" })).toBe("已达标");
  });
});

describe("StatusChip", () => {
  it("renders the translated label with its tone class", () => {
    render(<StatusChip status="production" />);

    const chip = screen.getByText("生产");
    expect(chip).toHaveClass("ag-governance-status", "is-success");
  });

  it("applies the caller's label override", () => {
    render(<StatusChip status="rejected" labels={{ rejected: "已拒绝" }} />);

    expect(screen.getByText("已拒绝")).toBeInTheDocument();
  });
});
