/**
 * Axios HTTP client with JWT interceptors.
 *
 * - Request interceptor: injects Authorization header from auth store.
 * - Response interceptor: on 401, clears token and redirects to /login.
 * - Base URL: /api/v1 (proxied by Vite dev server to backend:8000).
 */

import axios, { type AxiosInstance, type InternalAxiosRequestConfig } from "axios";

const BASE_URL = "/api/v1";

const httpClient: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 30_000,
  headers: {
    "Content-Type": "application/json",
  },
});

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
        // Token expired or invalid — clear and redirect
        localStorage.removeItem("ag_access_token");
        localStorage.removeItem("ag_refresh_token");
        localStorage.removeItem("ag_user_info");
        if (window.location.pathname !== "/login") {
          window.location.href = "/login";
        }
      }
    }
    return Promise.reject(error);
  },
);

export { httpClient };
