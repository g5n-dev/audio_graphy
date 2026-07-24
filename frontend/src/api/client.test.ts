import { beforeEach, describe, expect, it } from "vitest";
import { useAuthStore } from "@/stores/auth";
import { clearAuthAndRedirectToLogin } from "./client";

describe("HTTP client authentication recovery", () => {
  beforeEach(() => {
    window.location.hash = "";
    useAuthStore.getState().clearAuth();
  });

  it("clears both persisted and in-memory auth before using the HashRouter login route", () => {
    useAuthStore.getState().setAuth(
      "expired-access",
      "expired-refresh",
      {
        id: 1,
        name: "测试用户",
        email: "user@example.com",
        role: "admin",
        tenant_id: "tenant-a",
      },
    );

    clearAuthAndRedirectToLogin();

    expect(window.location.hash).toBe("#/login");
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(localStorage.getItem("ag_access_token")).toBeNull();
    expect(localStorage.getItem("ag_refresh_token")).toBeNull();
    expect(localStorage.getItem("ag_user_info")).toBeNull();
  });
});
