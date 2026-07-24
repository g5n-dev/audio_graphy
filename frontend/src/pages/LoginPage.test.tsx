import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LoginPage from "./LoginPage";
import { useAuthStore } from "@/stores/auth";

vi.mock("@/api/services", () => ({
  login: vi.fn(),
}));

import { login } from "@/api/services";

const mockedLogin = login as unknown as ReturnType<typeof vi.fn>;

describe("LoginPage", () => {
  beforeEach(() => {
    mockedLogin.mockReset();
    useAuthStore.getState().clearAuth();
  });

  it("submits through the Arco Form callback and enters the app", async () => {
    const user = userEvent.setup();
    mockedLogin.mockResolvedValueOnce({
      access_token: "access-token",
      refresh_token: "refresh-token",
      token_type: "bearer",
      expires_in: 3_600,
      user: {
        id: 1,
        name: "质检员",
        email: "qa@example.com",
        role: "admin",
        tenant_id: "tenant-1",
      },
    });

    render(
      <MemoryRouter initialEntries={["/login"]}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/" element={<div>已进入系统</div>} />
        </Routes>
      </MemoryRouter>,
    );

    await user.type(
      screen.getByPlaceholderText("admin@changan.com"),
      "qa@example.com",
    );
    await user.type(
      screen.getByPlaceholderText("请输入密码"),
      "visual-password",
    );
    await user.click(screen.getByRole("button", { name: "登录" }));

    expect(mockedLogin).toHaveBeenCalledWith(
      "qa@example.com",
      "visual-password",
    );
    expect(await screen.findByText("已进入系统")).toBeInTheDocument();
    expect(localStorage.getItem("ag_access_token")).toBe("access-token");
  });
});
