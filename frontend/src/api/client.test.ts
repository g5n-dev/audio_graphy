import axios, { AxiosError, type AxiosAdapter, type AxiosResponse } from "axios";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useAuthStore } from "@/stores/auth";
import { clearAuthAndRedirectToLogin, httpClient } from "./client";

const USER = {
  id: 1,
  name: "测试用户",
  email: "user@example.com",
  role: "admin",
  tenant_id: "tenant-a",
};

/** Build an axios adapter from a status/body callback.
 *
 * Stubbing at the adapter layer keeps the interceptors — which are what these
 * tests are about — fully in play, without pulling in a mocking library.
 */
function stubAdapter(handler: (url: string) => [number, unknown]): AxiosAdapter {
  return async (config) => {
    const [status, data] = handler(config.url ?? "");
    const response = {
      data,
      status,
      statusText: String(status),
      headers: {},
      config,
    } as AxiosResponse;
    if (status >= 400) {
      throw new AxiosError(`Request failed with status code ${status}`, String(status), config, {}, response);
    }
    return response;
  };
}

describe("HTTP client authentication recovery", () => {
  beforeEach(() => {
    window.location.hash = "";
    useAuthStore.getState().clearAuth();
  });

  it("clears both persisted and in-memory auth before using the HashRouter login route", () => {
    useAuthStore.getState().setAuth("expired-access", "expired-refresh", USER);

    clearAuthAndRedirectToLogin();

    expect(window.location.hash).toBe("#/login");
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(localStorage.getItem("ag_access_token")).toBeNull();
    expect(localStorage.getItem("ag_refresh_token")).toBeNull();
    expect(localStorage.getItem("ag_user_info")).toBeNull();
  });
});

describe("HTTP client token refresh", () => {
  const originalClientAdapter = httpClient.defaults.adapter;
  const originalAxiosAdapter = axios.defaults.adapter;

  beforeEach(() => {
    window.location.hash = "";
    useAuthStore.getState().clearAuth();
    useAuthStore.getState().setAuth("stale-access", "valid-refresh", USER);
  });

  afterEach(() => {
    httpClient.defaults.adapter = originalClientAdapter;
    axios.defaults.adapter = originalAxiosAdapter;
  });

  it("refreshes once on 401 and replays the original request", async () => {
    let attempts = 0;
    httpClient.defaults.adapter = stubAdapter(() => {
      attempts += 1;
      return attempts === 1 ? [401, {}] : [200, { items: [] }];
    });
    axios.defaults.adapter = stubAdapter(() => [200, { access_token: "fresh-access" }]);

    const resp = await httpClient.get("/recordings");

    expect(resp.status).toBe(200);
    expect(attempts).toBe(2);
    expect(localStorage.getItem("ag_access_token")).toBe("fresh-access");
    expect(window.location.hash).toBe("");
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
  });

  it("spends a single refresh for concurrent 401s", async () => {
    const seen = new Set<string>();
    httpClient.defaults.adapter = stubAdapter((url) => {
      if (seen.has(url)) return [200, {}];
      seen.add(url);
      return [401, {}];
    });
    let refreshCalls = 0;
    axios.defaults.adapter = stubAdapter(() => {
      refreshCalls += 1;
      return [200, { access_token: "fresh-access" }];
    });

    await Promise.all([
      httpClient.get("/parallel-a"),
      httpClient.get("/parallel-b"),
      httpClient.get("/parallel-c"),
    ]);

    // Three simultaneous 401s must not spend three refresh tokens, nor race
    // over which resulting access token gets stored.
    expect(refreshCalls).toBe(1);
  });

  it("logs out when the replayed request is still unauthorized", async () => {
    httpClient.defaults.adapter = stubAdapter(() => [401, {}]);
    axios.defaults.adapter = stubAdapter(() => [200, { access_token: "fresh-access" }]);

    await expect(httpClient.get("/recordings")).rejects.toThrow();

    expect(window.location.hash).toBe("#/login");
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it("logs out when the refresh token itself is rejected", async () => {
    httpClient.defaults.adapter = stubAdapter(() => [401, {}]);
    axios.defaults.adapter = stubAdapter(() => [401, {}]);

    await expect(httpClient.get("/recordings")).rejects.toThrow();

    expect(window.location.hash).toBe("#/login");
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it("logs out immediately when there is no refresh token to spend", async () => {
    localStorage.removeItem("ag_refresh_token");
    let refreshCalls = 0;
    httpClient.defaults.adapter = stubAdapter(() => [401, {}]);
    axios.defaults.adapter = stubAdapter(() => {
      refreshCalls += 1;
      return [200, { access_token: "never-used" }];
    });

    await expect(httpClient.get("/recordings")).rejects.toThrow();

    expect(refreshCalls).toBe(0);
    expect(window.location.hash).toBe("#/login");
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });
});
