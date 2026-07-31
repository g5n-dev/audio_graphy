/**
 * Axios HTTP client with JWT interceptors.
 *
 * - Request interceptor: injects Authorization header from auth store.
 * - Response interceptor: on 401, refreshes the access token once and replays
 *   the request; only a failed refresh clears the session and redirects.
 * - Base URL: /api/v1 (proxied by Vite dev server to backend:8000).
 */

import axios, {
  type AxiosInstance,
  type AxiosRequestConfig,
  type InternalAxiosRequestConfig,
} from "axios";
import { useAuthStore } from "@/stores/auth";

const BASE_URL = "/api/v1";

const httpClient: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 30_000,
  headers: {
    "Content-Type": "application/json",
  },
});

export function clearAuthAndRedirectToLogin(): void {
  useAuthStore.getState().clearAuth();
  if (window.location.hash !== "#/login") {
    // The application is hosted with HashRouter. A pathname redirect would ask
    // the static host for /login and can produce a deployment-level 404.
    window.location.hash = "/login";
  }
}

/** Requests carry this once they have been replayed, so a second 401 from the
 * same request logs out instead of looping. */
interface RetriableConfig extends InternalAxiosRequestConfig {
  _retriedAfterRefresh?: boolean;
}

/** In-flight refresh, shared by every request that got a 401 at the same time.
 *
 * Without this, a page that fires eight parallel queries on mount would spend
 * eight refresh tokens and race over which resulting access token gets stored.
 */
let refreshInFlight: Promise<string> | null = null;

async function refreshAccessToken(): Promise<string> {
  const refreshToken = localStorage.getItem("ag_refresh_token");
  if (!refreshToken) {
    throw new Error("no refresh token");
  }
  // A bare axios call: going through httpClient would re-enter this
  // interceptor if the refresh itself came back 401.
  const resp = await axios.post<{ access_token: string }>(
    `${BASE_URL}/auth/refresh`,
    { refresh_token: refreshToken },
    { headers: { "Content-Type": "application/json" }, timeout: 30_000 },
  );
  const token = resp.data?.access_token;
  if (!token) {
    throw new Error("refresh response had no access token");
  }
  useAuthStore.getState().setAccessToken(token);
  return token;
}

function refreshOnce(): Promise<string> {
  refreshInFlight ??= refreshAccessToken().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

// Request interceptor: attach JWT
httpClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token = localStorage.getItem("ag_access_token");
    if (token && config.headers) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    const requestId = crypto.randomUUID();
    config.headers["X-Request-ID"] = requestId;
    return config;
  },
  (error) => Promise.reject(error),
);

// Response interceptor: refresh once on 401, then replay
httpClient.interceptors.response.use(
  (response) => response,
  async (error: unknown) => {
    if (!axios.isAxiosError(error) || error.response?.status !== 401) {
      return Promise.reject(error);
    }

    const original = error.config as RetriableConfig | undefined;
    const isRefreshCall = original?.url?.includes("/auth/refresh");

    // No config to replay, already replayed once, or the refresh itself was
    // rejected — the session is genuinely over.
    if (!original || original._retriedAfterRefresh || isRefreshCall) {
      clearAuthAndRedirectToLogin();
      return Promise.reject(error);
    }

    try {
      const token = await refreshOnce();
      original._retriedAfterRefresh = true;
      original.headers.Authorization = `Bearer ${token}`;
      return await httpClient.request(original as AxiosRequestConfig);
    } catch {
      clearAuthAndRedirectToLogin();
      return Promise.reject(error);
    }
  },
);

export { httpClient };
