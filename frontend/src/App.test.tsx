import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/auth";

const graphModuleState = vi.hoisted(() => ({
  evaluations: 0,
  shouldThrow: false,
}));
let restoreExpectedGraphError: (() => void) | undefined;

function suppressExpectedGraphError() {
  const preventJSDOMReport = (event: ErrorEvent) => {
    if (
      event.error instanceof Error &&
      event.error.message === "sensitive-graph-stack"
    ) {
      event.preventDefault();
    }
  };
  window.addEventListener("error", preventJSDOMReport);
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  restoreExpectedGraphError = () => {
    window.removeEventListener("error", preventJSDOMReport);
    consoleError.mockRestore();
  };
}

vi.mock("@/pages/DashboardPage", () => ({
  default: () => <div>dashboard-route</div>,
}));

vi.mock("@/pages/GraphExplorerPage", async () => {
  graphModuleState.evaluations += 1;
  await new Promise((resolve) => setTimeout(resolve, 100));
  return {
    default: () => {
      if (graphModuleState.shouldThrow) {
        throw new Error("sensitive-graph-stack");
      }
      return <div>graph-route</div>;
    },
  };
});

vi.mock("@/pages/ReceptionWorkspace", () => ({
  default: () => <div>reception-workspace-route</div>,
}));

vi.mock("@/pages/ReceptionEntry", () => ({
  default: () => <div>reception-entry-route</div>,
}));

vi.mock("@/pages/ReceptionWorkspace/GraphView", () => ({
  default: () => <div>reception-graph-route</div>,
}));

vi.mock("@/pages/TagInsights", () => ({
  default: () => <div>tag-insights-route</div>,
}));

vi.mock("@/pages/TagGovernance", () => ({
  default: () => <div>tag-governance-route</div>,
}));

vi.mock("@/pages/TagReview", () => ({
  default: () => <div>tag-review-route</div>,
}));

vi.mock("@/pages/TagRunDetail", () => ({
  default: () => <div>tag-run-detail-route</div>,
}));

vi.mock("@/pages/ReceptionStateInsights", () => ({
  default: () => <div>reception-state-insights-route</div>,
}));

import App from "./App";

describe("App route loading", () => {
  afterEach(() => {
    cleanup();
    restoreExpectedGraphError?.();
    restoreExpectedGraphError = undefined;
    graphModuleState.shouldThrow = false;
    useAuthStore.getState().clearAuth();
    localStorage.clear();
  });

  it("loads a heavy route only when requested and exposes a loading state", async () => {
    useAuthStore.setState({
      token: "test-token",
      refreshToken: "test-refresh-token",
      user: {
        id: 1,
        name: "Test User",
        email: "test@example.com",
        role: "admin",
        tenant_id: "tenant-test",
      },
      isAuthenticated: true,
    });

    const dashboardView = render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByText("dashboard-route")).toBeInTheDocument();
    expect(graphModuleState.evaluations).toBe(0);
    dashboardView.unmount();

    render(
      <MemoryRouter initialEntries={["/graph"]}>
        <App />
      </MemoryRouter>,
    );

    expect(
      screen.getByRole("status", { name: "页面加载中" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("graph-route")).toBeInTheDocument();
    expect(graphModuleState.evaluations).toBe(1);
  });

  it.each([
    ["/receptions", "reception-entry-route"],
    ["/receptions/reception-42/workspace", "reception-workspace-route"],
    ["/receptions/reception-42/graph", "reception-graph-route"],
    ["/reception-flow", "reception-state-insights-route"],
    ["/tag-insights", "tag-insights-route"],
    ["/tag-governance", "tag-governance-route"],
    ["/tag-review", "tag-review-route"],
    ["/tag-runs/42", "tag-run-detail-route"],
  ])("routes %s to its lazy feature page", async (path, expectedText) => {
    useAuthStore.setState({
      token: "test-token",
      refreshToken: "test-refresh-token",
      user: {
        id: 1,
        name: "Test User",
        email: "test@example.com",
        role: "admin",
        tenant_id: "tenant-test",
      },
      isAuthenticated: true,
    });

    render(
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByText(expectedText)).toBeInTheDocument();
  });

  it("resets document scroll when navigating to another feature page", async () => {
    useAuthStore.setState({
      token: "test-token",
      refreshToken: "test-refresh-token",
      user: {
        id: 1,
        name: "Test User",
        email: "test@example.com",
        role: "admin",
        tenant_id: "tenant-test",
      },
      isAuthenticated: true,
    });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByText("dashboard-route")).toBeInTheDocument();
    document.documentElement.scrollTop = 116;
    document.body.scrollTop = 116;
    fireEvent.click(screen.getByText("标签洞察"));
    expect(await screen.findByText("tag-insights-route")).toBeInTheDocument();
    await waitFor(() => {
      expect(document.documentElement.scrollTop).toBe(0);
      expect(document.body.scrollTop).toBe(0);
    });
  });

  it("navigates from state paths to tag insights and the reception center through the grouped sidebar", async () => {
    useAuthStore.setState({
      token: "test-token",
      refreshToken: "test-refresh-token",
      user: {
        id: 1,
        name: "Test User",
        email: "test@example.com",
        role: "admin",
        tenant_id: "tenant-test",
      },
      isAuthenticated: true,
    });

    render(
      <MemoryRouter initialEntries={["/reception-flow"]}>
        <App />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("reception-state-insights-route"),
    ).toBeInTheDocument();
    const platformNavigation = screen.getByRole("navigation", {
      name: "平台功能导航",
    });
    const insightGroup = within(platformNavigation).getByRole("group", {
      name: "对话洞察",
    });
    expect(within(insightGroup).getByText("状态路径")).toBeInTheDocument();

    fireEvent.click(
      within(insightGroup).getByText("标签洞察"),
    );
    expect(await screen.findByText("tag-insights-route")).toBeInTheDocument();

    const receptionGroup = within(platformNavigation).getByRole("group", {
      name: "接待作业",
    });
    fireEvent.click(within(receptionGroup).getByText("接待中心"));
    expect(
      await screen.findByText("reception-entry-route"),
    ).toBeInTheDocument();
  });

  it("opens and dismisses the platform navigation as a mobile drawer", async () => {
    useAuthStore.setState({
      token: "test-token",
      refreshToken: "test-refresh-token",
      user: {
        id: 1,
        name: "Test User",
        email: "test@example.com",
        role: "admin",
        tenant_id: "tenant-test",
      },
      isAuthenticated: true,
    });

    render(
      <MemoryRouter initialEntries={["/receptions/reception-42/workspace"]}>
        <App />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("reception-workspace-route"),
    ).toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "打开平台导航" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(toggle);
    expect(toggle).toHaveAccessibleName("关闭平台导航");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(document.querySelector(".ag-platform-sider")).toHaveClass(
      "is-mobile-open",
    );

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "打开平台导航" }),
      ).toHaveAttribute("aria-expanded", "false");
    });
  });

  it("groups the platform sidebar around reception work, dialogue insights, and knowledge governance", async () => {
    useAuthStore.setState({
      token: "test-token",
      refreshToken: "test-refresh-token",
      user: {
        id: 1,
        name: "Test User",
        email: "test@example.com",
        role: "admin",
        tenant_id: "tenant-test",
      },
      isAuthenticated: true,
    });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByText("dashboard-route")).toBeInTheDocument();
    const sidebar = screen.getByRole("navigation", {
      name: "平台功能导航",
    });
    const receptionGroup = within(sidebar).getByRole("group", {
      name: "接待作业",
    });
    expect(within(receptionGroup).getByText("接待中心")).toBeInTheDocument();
    expect(within(receptionGroup).getByText("录音管理")).toBeInTheDocument();

    const insightGroup = within(sidebar).getByRole("group", {
      name: "对话洞察",
    });
    expect(within(insightGroup).getByText("状态路径")).toBeInTheDocument();
    expect(within(insightGroup).getByText("标签洞察")).toBeInTheDocument();

    const toolGroup = within(sidebar).getByRole("group", {
      name: "知识与治理",
    });
    expect(
      within(toolGroup).getByText("全域知识图谱"),
    ).toBeInTheDocument();
    expect(within(toolGroup).queryByText("社区探索")).not.toBeInTheDocument();
    expect(within(toolGroup).getByText("智能问答")).toBeInTheDocument();
    expect(within(toolGroup).getByText("标签治理")).toBeInTheDocument();
    expect(within(toolGroup).getByText("人工复核")).toBeInTheDocument();
    expect(within(toolGroup).getByText("说话人")).toBeInTheDocument();
  });

  it("hides privileged tag-governance navigation from agents", async () => {
    useAuthStore.setState({
      token: "test-token",
      refreshToken: "test-refresh-token",
      user: {
        id: 2,
        name: "Agent User",
        email: "agent@example.com",
        role: "agent",
        tenant_id: "tenant-test",
      },
      isAuthenticated: true,
    });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByText("dashboard-route")).toBeInTheDocument();
    const sidebar = screen.getByRole("navigation", {
      name: "平台功能导航",
    });
    expect(within(sidebar).queryByText("标签治理")).not.toBeInTheDocument();
    expect(within(sidebar).queryByText("人工复核")).not.toBeInTheDocument();
  });

  it.each(["/tag-governance", "/tag-review", "/tag-runs/42"])(
    "blocks direct privileged route access for agents: %s",
    async (path) => {
      useAuthStore.setState({
        token: "agent-token",
        refreshToken: "agent-refresh-token",
        user: {
          id: 2,
          name: "Agent User",
          email: "agent@example.com",
          role: "agent",
          tenant_id: "tenant-test",
        },
        isAuthenticated: true,
      });

      render(
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>,
      );

      expect(await screen.findByText("dashboard-route")).toBeInTheDocument();
      expect(screen.queryByText("tag-governance-route")).not.toBeInTheDocument();
      expect(screen.queryByText("tag-review-route")).not.toBeInTheDocument();
      expect(screen.queryByText("tag-run-detail-route")).not.toBeInTheDocument();
    },
  );

  it("redirects the retired communities route to the graph workspace", async () => {
    useAuthStore.setState({
      token: "test-token",
      refreshToken: "test-refresh-token",
      user: {
        id: 1,
        name: "Test User",
        email: "test@example.com",
        role: "admin",
        tenant_id: "tenant-test",
      },
      isAuthenticated: true,
    });

    render(
      <MemoryRouter initialEntries={["/communities"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByText("graph-route")).toBeInTheDocument();
  });

  it("hydrates persisted authentication before resolving a deep route", async () => {
    useAuthStore.getState().clearAuth();
    localStorage.setItem("ag_access_token", "persisted-token");
    localStorage.setItem("ag_refresh_token", "persisted-refresh");
    localStorage.setItem(
      "ag_user_info",
      JSON.stringify({
        id: 1,
        name: "Persisted User",
        email: "persisted@example.com",
        role: "admin",
        tenant_id: "tenant-test",
      }),
    );

    render(
      <MemoryRouter initialEntries={["/receptions/reception-42/workspace"]}>
        <App />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("reception-workspace-route"),
    ).toBeInTheDocument();
  });

  it("keeps the application shell visible and retries a failed route without exposing error details", async () => {
    suppressExpectedGraphError();
    graphModuleState.shouldThrow = true;
    useAuthStore.setState({
      token: "test-token",
      refreshToken: "test-refresh-token",
      user: {
        id: 1,
        name: "Test User",
        email: "test@example.com",
        role: "admin",
        tenant_id: "tenant-test",
      },
      isAuthenticated: true,
    });

    render(
      <MemoryRouter initialEntries={["/graph"]}>
        <App />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "页面加载失败" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "平台功能导航" }),
    ).toBeInTheDocument();
    expect(screen.getByText("AudioGraphy")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "返回首页" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/sensitive-graph-stack/)).not.toBeInTheDocument();

    graphModuleState.shouldThrow = false;
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));

    expect(await screen.findByText("graph-route")).toBeInTheDocument();
  });

  it("recovers automatically when navigating away from a failed route", async () => {
    suppressExpectedGraphError();
    graphModuleState.shouldThrow = true;
    useAuthStore.setState({
      token: "test-token",
      refreshToken: "test-refresh-token",
      user: {
        id: 1,
        name: "Test User",
        email: "test@example.com",
        role: "admin",
        tenant_id: "tenant-test",
      },
      isAuthenticated: true,
    });

    render(
      <MemoryRouter initialEntries={["/graph"]}>
        <App />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "页面加载失败" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回首页" }));

    expect(await screen.findByText("dashboard-route")).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "页面加载失败" }),
    ).not.toBeInTheDocument();
  });
});
