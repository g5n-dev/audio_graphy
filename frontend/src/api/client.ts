/**
 * Axios HTTP client with JWT interceptors.
 *
 * - Request interceptor: injects Authorization header from auth store.
 * - Response interceptor: on 401, clears token and redirects to /login.
 * - Base URL: /api/v1 (proxied by Vite dev server to backend:8000).
 */

import axios, { type AxiosInstance, type InternalAxiosRequestConfig } from "axios";
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

// Response interceptor: handle 401 globally
httpClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (axios.isAxiosError(error)) {
      const status = error.response?.status;
      if (status === 401) {
        clearAuthAndRedirectToLogin();
      }
    }
    return Promise.reject(error);
  },
);

export { httpClient };
