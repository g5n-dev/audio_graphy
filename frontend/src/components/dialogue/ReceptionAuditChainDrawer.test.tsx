import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ReceptionAuditChainDrawer } from "./ReceptionAuditChainDrawer";
import type { AuditChainTarget } from "./ReceptionAuditChainDrawer";
import type { ReceptionProvenanceChain } from "@/types/api";

const TARGET: AuditChainTarget = {
  objectType: "dialogue_tag_assignment",
  objectRef: 77,
  title: "标签 stage.greeting",
};

const CHAIN: ReceptionProvenanceChain = {
  object_type: "dialogue_tag_assignment",
  object_ref: "77",
  total: 2,
  page: 1,
  page_size: 100,
  truncated: false,
  items: [
    {
      id: 1,
      object_type: "dialogue_tag_assignment",
      object_ref: "77",
      action: "derived",
      actor: "system",
      algorithm_version: "rules-v1",
      parent_refs: [],
      evidence_refs: [],
      occurred_at: "2026-07-23T01:00:00Z",
      detail: {},
    },
    {
      id: 2,
      object_type: "dialogue_tag_assignment",
      object_ref: "77",
      action: "edited",
      actor: "inspector@example.com",
      algorithm_version: "manual-v1",
      parent_refs: [],
      evidence_refs: [],
      occurred_at: "2026-07-23T02:00:00Z",
      detail: { reason: "客户当场改口，阶段判定有误" },
    },
  ],
};

describe("ReceptionAuditChainDrawer", () => {
  it("surfaces the edit reason recorded outside the loaded workspace window", () => {
    render(
      <ReceptionAuditChainDrawer
        target={TARGET}
        data={CHAIN}
        pending={false}
        error={null}
        onRetry={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "标签 stage.greeting · 完整审计链" }),
    ).toBeInTheDocument();
    expect(screen.getByText("共 2 条溯源事件")).toBeInTheDocument();
    expect(screen.getByText("自动推导")).toBeInTheDocument();
    expect(screen.getByText("人工编辑")).toBeInTheDocument();
    expect(
      screen.getByText("客户当场改口，阶段判定有误"),
    ).toBeInTheDocument();
    expect(screen.getByText("inspector@example.com")).toBeInTheDocument();
  });

  it("says how much of a truncated chain is on screen", () => {
    render(
      <ReceptionAuditChainDrawer
        target={TARGET}
        data={{ ...CHAIN, total: 240, truncated: true }}
        pending={false}
        error={null}
        onRetry={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("共 240 条溯源事件")).toBeInTheDocument();
    expect(screen.getByText("按时间正序显示前 2 条")).toBeInTheDocument();
  });

  it("reads the server's 404 as an empty chain rather than a failure", () => {
    const onRetry = vi.fn();
    render(
      <ReceptionAuditChainDrawer
        target={TARGET}
        pending={false}
        error={{
          response: {
            status: 404,
            data: {
              error: {
                code: "PROVENANCE_NOT_FOUND",
                message: "Provenance chain not found",
              },
            },
          },
        }}
        onRetry={onRetry}
        onClose={vi.fn()}
      />,
    );

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("暂无审计记录")).toBeInTheDocument();
    expect(
      screen.queryByText("Provenance chain not found"),
    ).not.toBeInTheDocument();
  });

  it("keeps a retry path when the chain genuinely fails to load", async () => {
    const onRetry = vi.fn();
    render(
      <ReceptionAuditChainDrawer
        target={TARGET}
        pending={false}
        error={{
          response: {
            status: 403,
            data: { error: { code: "FORBIDDEN", message: "盲审隔离期禁止查看" } },
          },
        }}
        onRetry={onRetry}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("盲审隔离期禁止查看");
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("closes through an accessible control", async () => {
    const onClose = vi.fn();
    render(
      <ReceptionAuditChainDrawer
        target={TARGET}
        data={CHAIN}
        pending={false}
        error={null}
        onRetry={vi.fn()}
        onClose={onClose}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "关闭完整审计链" }),
    );
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
