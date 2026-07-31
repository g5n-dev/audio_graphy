/**
 * Auth store (Zustand).
 *
 * Manages JWT tokens and user info with localStorage persistence.
 */

import { create } from "zustand";
import type { UserInfo } from "@/types/api";

interface AuthState {
  token: string | null;
  refreshToken: string | null;
  user: UserInfo | null;
  isAuthenticated: boolean;
  setAuth: (token: string, refreshToken: string, user: UserInfo) => void;
  /** Replace only the access token, keeping the refresh token and user.
   *
   * `POST /auth/refresh` returns a new access token and nothing else, so
   * `setAuth` cannot express the result of a refresh.
   */
  setAccessToken: (token: string) => void;
  clearAuth: () => void;
  loadFromStorage: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  refreshToken: null,
  user: null,
  isAuthenticated: false,

  setAuth: (token: string, refreshToken: string, user: UserInfo) => {
    localStorage.setItem("ag_access_token", token);
    localStorage.setItem("ag_refresh_token", refreshToken);
    localStorage.setItem("ag_user_info", JSON.stringify(user));
    set({ token, refreshToken, user, isAuthenticated: true });
  },

  setAccessToken: (token: string) => {
    localStorage.setItem("ag_access_token", token);
    set({ token });
  },

  clearAuth: () => {
    localStorage.removeItem("ag_access_token");
    localStorage.removeItem("ag_refresh_token");
    localStorage.removeItem("ag_user_info");
    set({ token: null, refreshToken: null, user: null, isAuthenticated: false });
  },

  loadFromStorage: () => {
    const token = localStorage.getItem("ag_access_token");
    const refreshToken = localStorage.getItem("ag_refresh_token");
    const userRaw = localStorage.getItem("ag_user_info");
    if (token && userRaw) {
      try {
        const user = JSON.parse(userRaw) as UserInfo;
        set({ token, refreshToken, user, isAuthenticated: true });
      } catch {
        set({ token: null, refreshToken: null, user: null, isAuthenticated: false });
      }
    }
  },
}));
