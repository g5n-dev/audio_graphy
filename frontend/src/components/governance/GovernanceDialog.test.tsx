import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { GovernanceDialog } from "./GovernanceDialog";

function renderDialog(overrides: Partial<Parameters<typeof GovernanceDialog>[0]> = {}) {
  const onClose = vi.fn();
  const onSubmit = vi.fn();
  render(
    <GovernanceDialog
      id="test-dialog-title"
      kicker="TEST"
      title="测试弹窗"
      pending={false}
      onClose={onClose}
      onSubmit={onSubmit}
      submitLabel="保存"
      pendingLabel="正在保存…"
      {...overrides}
    >
      <label>
        名称
        <input aria-label="名称" />
      </label>
    </GovernanceDialog>,
  );
  return { onClose, onSubmit };
}

describe("GovernanceDialog", () => {
  it("exposes itself as a modal dialog named by its title", () => {
    renderDialog();

    const dialog = screen.getByRole("dialog", { name: "测试弹窗" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("closes on Escape", () => {
    const { onClose } = renderDialog();

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    expect(onClose).toHaveBeenCalledOnce();
  });

  it("ignores Escape while a submission is in flight", () => {
    const { onClose } = renderDialog({ pending: true });

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    expect(onClose).not.toHaveBeenCalled();
  });

  it("submits without navigating away", async () => {
    const { onSubmit } = renderDialog();

    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(onSubmit).toHaveBeenCalledOnce();
  });

  it("closes from both the cancel and the close buttons", async () => {
    const { onClose } = renderDialog();

    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    await userEvent.click(screen.getByRole("button", { name: "关闭" }));

    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("announces an error through the alert role", () => {
    renderDialog({ error: "体系名称不能为空。" });

    expect(screen.getByRole("alert")).toHaveTextContent("体系名称不能为空。");
  });

  it("renders an error action alongside the message", async () => {
    const onRetry = vi.fn();
    renderDialog({
      error: "启动失败。",
      errorAction: (
        <button type="button" onClick={onRetry}>
          重试启动
        </button>
      ),
    });

    await userEvent.click(screen.getByRole("button", { name: "重试启动" }));

    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("marks the primary button as destructive when asked", () => {
    renderDialog({ danger: true, submitLabel: "确认回滚" });

    expect(screen.getByRole("button", { name: "确认回滚" })).toHaveClass("is-danger");
  });

  it("disables every control and shows the pending label while submitting", () => {
    renderDialog({ pending: true });

    expect(screen.getByRole("button", { name: "正在保存…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "关闭" })).toBeDisabled();
  });

  it("can disable submission while leaving the dialog interactive", () => {
    renderDialog({ submitDisabled: true });

    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消" })).toBeEnabled();
  });

  it("appends a caller sizing class to the dialog", () => {
    renderDialog({ className: "ag-schema-version-dialog" });

    expect(screen.getByRole("dialog")).toHaveClass(
      "ag-governance-dialog",
      "ag-schema-version-dialog",
    );
  });
});
