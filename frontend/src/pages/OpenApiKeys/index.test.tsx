import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import OpenApiKeysPage from "./index";

vi.mock("@/api/services", () => ({
  createApiKey: vi.fn(),
  listApiKeys: vi.fn(),
  revokeApiKey: vi.fn(),
}));

import { createApiKey, listApiKeys, revokeApiKey } from "@/api/services";

const mocks = {
  create: createApiKey as unknown as ReturnType<typeof vi.fn>,
  list: listApiKeys as unknown as ReturnType<typeof vi.fn>,
  revoke: revokeApiKey as unknown as ReturnType<typeof vi.fn>,
};

const KEY_ROW = {
  id: 3,
  name: "crm-sync",
  active: true,
  created_at: "2026-08-05T02:00:00Z",
  last_used_at: null,
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <OpenApiKeysPage />
    </QueryClientProvider>,
  );
}

describe("OpenApiKeysPage", () => {
  it("mints a key and reveals both secrets exactly once", async () => {
    const user = userEvent.setup();
    mocks.list.mockResolvedValue({ items: [], total: 0 });
    mocks.create.mockResolvedValue({
      key: KEY_ROW,
      api_key: "agk_deadbeef",
      webhook_secret: "f".repeat(64),
    });
    renderPage();

    await user.type(screen.getByLabelText("新密钥名称"), "crm-sync");
    await user.click(screen.getByRole("button", { name: "签发密钥" }));

    // 后端只存哈希、验签密钥由主密钥派生——这两个值错过就只能重新签发,
    // 所以必须显著展示并明说「只显示这一次」。
    const reveal = await screen.findByRole("alert");
    expect(reveal).toHaveTextContent("只显示这一次");
    expect(screen.getByText("agk_deadbeef")).toBeInTheDocument();
    expect(screen.getByText("f".repeat(64))).toBeInTheDocument();
    expect(mocks.create).toHaveBeenCalledWith("crm-sync");

    await user.click(screen.getByRole("button", { name: "我已保存,关闭" }));
    expect(screen.queryByText("agk_deadbeef")).not.toBeInTheDocument();
  });

  it("revoking takes two clicks and names the consequence", async () => {
    const user = userEvent.setup();
    mocks.list.mockResolvedValue({ items: [KEY_ROW], total: 1 });
    mocks.revoke.mockResolvedValue({ key: { ...KEY_ROW, active: false } });
    renderPage();

    const row = await screen.findByRole("row", { name: /crm-sync/ });
    await user.click(within(row).getByRole("button", { name: "吊销" }));
    // 单击不得直接吊销:第一次点击只进入确认态,且确认按钮说明后果。
    expect(mocks.revoke).not.toHaveBeenCalled();

    await user.click(
      within(row).getByRole("button", { name: "确认吊销 crm-sync" }),
    );
    await waitFor(() => expect(mocks.revoke).toHaveBeenCalledWith(3));
  });

  it("shows the error state with a retry when the list fails", async () => {
    mocks.list.mockRejectedValueOnce(new Error("boom"));
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("数据加载失败")).toBeInTheDocument();

    mocks.list.mockResolvedValue({ items: [KEY_ROW], total: 1 });
    const user = userEvent.setup();
    await user.click(within(alert).getByRole("button", { name: "重新加载" }));
    expect(await screen.findByText("crm-sync")).toBeInTheDocument();
  });

  it("renders a revoked key without a revoke action", async () => {
    mocks.list.mockResolvedValue({
      items: [{ ...KEY_ROW, active: false }],
      total: 1,
    });
    renderPage();
    const row = await screen.findByRole("row", { name: /crm-sync/ });
    expect(within(row).getByText("已吊销")).toBeInTheDocument();
    expect(
      within(row).queryByRole("button", { name: /吊销/ }),
    ).not.toBeInTheDocument();
  });
});
