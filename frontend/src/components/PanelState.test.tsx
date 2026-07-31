import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PanelState } from "./PanelState";

function renderState(
  props: Partial<Parameters<typeof PanelState>[0]> = {},
  onRetry = vi.fn(),
) {
  render(
    <PanelState
      pending={false}
      error={null}
      empty={false}
      emptyTitle="暂无数据"
      emptyDescription="导入后再回来查看。"
      onRetry={onRetry}
      {...props}
    >
      <p>面板内容</p>
    </PanelState>,
  );
  return onRetry;
}

describe("PanelState", () => {
  it("announces the pending state with an overridable label", () => {
    renderState({ pending: true, pendingLabel: "正在加载概览数据…" });
    expect(screen.getByRole("status")).toHaveTextContent("正在加载概览数据…");
    expect(screen.queryByText("面板内容")).not.toBeInTheDocument();
  });

  it("reports a failed query as an alert with the backend message", async () => {
    const user = userEvent.setup();
    const onRetry = renderState({
      error: Object.assign(new Error("Request failed with status code 500"), {
        response: {
          status: 500,
          data: { error: { code: "internal_error", message: "统计服务不可用" } },
        },
      }),
    });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("数据加载失败");
    expect(alert).toHaveTextContent("统计服务不可用");
    expect(screen.queryByText("面板内容")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重新加载" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("keeps the empty state distinct from a failure", () => {
    renderState({ empty: true });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("暂无数据");
    expect(screen.getByText("导入后再回来查看。")).toBeInTheDocument();
  });

  it("renders children once the panel has data", () => {
    renderState();
    expect(screen.getByText("面板内容")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
