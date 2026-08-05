/**
 * Main application shell — layout + routing.
 *
 * Routes:
 *   /login        — login page (no auth guard)
 *   /             — dashboard
 *   /recordings   — recordings list
 *   /recordings/:id — recording detail
 *   /graph        — graph explorer (AntV G6)
 *   /query        — natural language query
 *   /stats        — tag statistics
 *   /speakers     — speaker profile list (M7)
 *   /speakers/:id — speaker profile detail (M7 + M9 T15 pending merges)
 *   /receptions    — reception workspace entry
 *   /time-travel  — M9 R2 T15 bi-temporal explorer
 *   /communities  — compatibility redirect to /graph?view=clusters
 *   /tag-governance — tag schema/evaluation/deployment governance
 *   /tag-review   — human review workbench
 *   /prompt-lab   — 离线 Prompt 编译与人工复核
 *   /tag-runs/:id — tag extraction/recompute run detail
 *   *             — not-found view (unknown paths are never silently redirected)
 */

import {
  Component,
  lazy,
  Suspense,
  useEffect,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { Routes, Route, Navigate, useLocation, useNavigate } from "react-router-dom";
import {
  Layout,
  Menu,
  Typography,
  Button,
  Space,
  Spin,
} from "@arco-design/web-react";
import {
  IconDashboard,
  IconFileAudio,
  IconBranch,
  IconMessage,
  IconStorage,
  IconUser,
  IconClockCircle,
  IconMenu,
  IconThunderbolt,
} from "@arco-design/web-react/icon";
import { useAuthStore } from "@/stores/auth";
import "@/styles/immersiveGraphShell.css";

const LoginPage = lazy(() => import("@/pages/LoginPage"));
const DashboardPage = lazy(() => import("@/pages/DashboardPage"));
const RecordingsPage = lazy(() => import("@/pages/RecordingsPage"));
const RecordingDetailPage = lazy(() => import("@/pages/RecordingDetailPage"));
const GraphExplorerPage = lazy(() => import("@/pages/GraphExplorerPage"));
const QueryPage = lazy(() => import("@/pages/QueryPage"));
const StatsPage = lazy(() => import("@/pages/StatsPage"));
const OpenApiKeysPage = lazy(() => import("@/pages/OpenApiKeys"));
const SpeakerProfileListPage = lazy(() => import("@/pages/SpeakerProfile"));
const SpeakerProfileDetailPage = lazy(
  () => import("@/pages/SpeakerProfile/Detail"),
);
const TimeTravelPage = lazy(() => import("@/pages/TimeTravel"));
const ReceptionWorkspacePage = lazy(
  () => import("@/pages/ReceptionWorkspace"),
);
const ReceptionEntryPage = lazy(() => import("@/pages/ReceptionEntry"));
const ReceptionGraphPage = lazy(
  () => import("@/pages/ReceptionWorkspace/GraphView"),
);
const ReceptionStateInsightsPage = lazy(
  () => import("@/pages/ReceptionStateInsights"),
);
const TagInsightsPage = lazy(() => import("@/pages/TagInsights"));
const TagGovernancePage = lazy(() => import("@/pages/TagGovernance"));
const TagReviewPage = lazy(() => import("@/pages/TagReview"));
const TagRunDetailPage = lazy(() => import("@/pages/TagRunDetail"));
const PromptLabPage = lazy(() => import("@/pages/PromptLab"));

const { Header, Sider, Content } = Layout;
const { Title } = Typography;
const isSitesDemo = import.meta.env.VITE_SITES_DEMO === "true";

function RouteLoadingFallback() {
  return (
    <div
      role="status"
      aria-label="页面加载中"
      aria-live="polite"
      style={{
        minHeight: 240,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Spin tip="页面加载中…" />
    </div>
  );
}

// Shared shape for every full-page route message (crash, not found, forbidden)
// so a blocked route reads as the same kind of deliberate state, not a glitch.
const routeMessageStyle: CSSProperties = {
  minHeight: 360,
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  padding: 32,
  textAlign: "center",
};

const routeMessageTitleStyle: CSSProperties = {
  margin: "0 0 12px",
  color: "#1d2129",
  fontSize: 24,
};

const routeMessageBodyStyle: CSSProperties = {
  margin: "0 0 24px",
  color: "#86909c",
  maxWidth: 520,
};

interface RouteErrorBoundaryProps {
  children: ReactNode;
  routeKey: string;
  onReturnHome: () => void;
}

interface RouteErrorBoundaryState {
  hasError: boolean;
}

class RouteErrorBoundary extends Component<
  RouteErrorBoundaryProps,
  RouteErrorBoundaryState
> {
  state: RouteErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): RouteErrorBoundaryState {
    return { hasError: true };
  }

  componentDidUpdate(previousProps: RouteErrorBoundaryProps) {
    if (
      this.state.hasError &&
      previousProps.routeKey !== this.props.routeKey
    ) {
      this.setState({ hasError: false });
    }
  }

  private retry = () => {
    this.setState({ hasError: false });
  };

  render() {
    if (!this.state.hasError) {
      return this.props.children;
    }

    return (
      <section
        role="alert"
        aria-labelledby="route-error-title"
        style={routeMessageStyle}
      >
        <h1 id="route-error-title" style={routeMessageTitleStyle}>
          页面加载失败
        </h1>
        <p style={routeMessageBodyStyle}>
          当前功能暂时无法显示，请重新加载或返回首页。
        </p>
        <Space>
          <Button type="primary" onClick={this.retry}>
            重新加载
          </Button>
          <Button onClick={this.props.onReturnHome}>返回首页</Button>
        </Space>
      </section>
    );
  }
}

/**
 * Catch-all view for unknown paths.
 *
 * A mistyped URL or a stale deep link used to be redirected to the dashboard
 * without a word, which reads as "the app moved me for no reason". Naming the
 * path that failed keeps the shared-link case debuggable.
 */
function RouteNotFoundView() {
  const location = useLocation();
  const navigate = useNavigate();

  return (
    <section
      role="alert"
      aria-labelledby="route-not-found-title"
      style={routeMessageStyle}
    >
      <h1 id="route-not-found-title" style={routeMessageTitleStyle}>
        页面不存在
      </h1>
      <p style={routeMessageBodyStyle}>
        没有找到 <code>{location.pathname}</code> 对应的页面，链接可能已失效或地址有误。
      </p>
      <Space>
        <Button type="primary" onClick={() => navigate("/", { replace: true })}>
          返回首页
        </Button>
        <Button onClick={() => navigate(-1)}>返回上一页</Button>
      </Space>
    </section>
  );
}

/**
 * Permission boundary for the tag-governance routes.
 *
 * Every role can see the "进入标签治理中心" link on the tag-insights page, so a
 * viewer will land here through a legitimate in-product link. Silently sending
 * them to the dashboard made that link look broken; naming the roles that may
 * enter makes it an explicit permissions boundary instead.
 */
function TagGovernanceForbiddenView({ role }: { role?: string }) {
  const navigate = useNavigate();

  return (
    <section
      role="alert"
      aria-labelledby="route-forbidden-title"
      style={routeMessageStyle}
    >
      <h1 id="route-forbidden-title" style={routeMessageTitleStyle}>
        无标签治理权限
      </h1>
      <p style={routeMessageBodyStyle}>
        标签治理、人工复核与治理任务详情仅对管理员（admin）与质检员（inspector）开放
        {role ? `，当前账号角色为 ${role}` : ""}。如需进入，请联系管理员调整权限。
      </p>
      <Space>
        <Button type="primary" onClick={() => navigate("/tag-insights")}>
          查看标签洞察
        </Button>
        <Button onClick={() => navigate("/", { replace: true })}>返回首页</Button>
      </Space>
    </section>
  );
}

/**
 * 标签治理系路由的权限边界。
 *
 * 抽象自 /tag-governance、/tag-review、/tag-runs/:id 三份完全相同的内联三元——
 * 每加一条受保护路由就复制一次，等于把 canGovernTags 这个局部量的耦合也复制一次。
 * 刻意留在本文件内：挪进 src/components/ 会让 App 静态依赖一个新目录，
 * 是把整条懒加载链拽回首屏 chunk 的最快路径。
 */
function GovernedRoute({
  allowed,
  role,
  children,
}: {
  allowed: boolean;
  role?: string;
  children: ReactNode;
}) {
  return allowed ? <>{children}</> : <TagGovernanceForbiddenView role={role} />;
}

const menuGroups = [
  {
    label: "工作总览",
    items: [{ key: "/", icon: <IconDashboard />, label: "仪表盘" }],
  },
  {
    label: "接待作业",
    items: [
      { key: "/receptions", icon: <IconFileAudio />, label: "接待中心" },
      { key: "/recordings", icon: <IconFileAudio />, label: "录音管理" },
    ],
  },
  {
    label: "对话洞察",
    items: [
      { key: "/reception-flow", icon: <IconBranch />, label: "状态路径" },
      { key: "/tag-insights", icon: <IconStorage />, label: "标签洞察" },
    ],
  },
  {
    label: "知识与治理",
    items: [
      { key: "/graph", icon: <IconBranch />, label: "全域知识图谱" },
      { key: "/query", icon: <IconMessage />, label: "智能问答" },
      { key: "/stats", icon: <IconStorage />, label: "标签统计" },
      {
        key: "/open-api-keys",
        icon: <IconStorage />,
        label: "开放接口",
        requiresAdmin: true,
      },
      {
        key: "/tag-governance",
        icon: <IconStorage />,
        label: "标签治理",
        requiresInspector: true,
      },
      {
        key: "/tag-review",
        icon: <IconUser />,
        label: "人工复核",
        requiresInspector: true,
      },
      {
        key: "/prompt-lab",
        icon: <IconThunderbolt />,
        label: "提示词实验室",
        requiresInspector: true,
      },
      { key: "/speakers", icon: <IconUser />, label: "说话人" },
      { key: "/time-travel", icon: <IconClockCircle />, label: "时间演化" },
    ],
  },
];

function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const [mobileNavigationOpen, setMobileNavigationOpen] = useState(false);
  const user = useAuthStore((s) => s.user);
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const canGovernTags =
    user?.role === "admin" || user?.role === "inspector";
  const visibleMenuGroups = menuGroups.map((group) => ({
    ...group,
    items: group.items.filter(
      (item) =>
        "requiresAdmin" in item
          ? user?.role === "admin"
          : !("requiresInspector" in item) || canGovernTags,
    ),
  }));
  const visibleMenuItems = visibleMenuGroups.flatMap(
    (group) => group.items,
  );

  useEffect(() => {
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
    setMobileNavigationOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!mobileNavigationOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileNavigationOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [mobileNavigationOpen]);

  const selectedKey =
    visibleMenuItems.find(
      (m) =>
        m.key === location.pathname ||
        (m.key !== "/" && location.pathname.startsWith(m.key)),
    )?.key ?? "/";

  return (
    <Layout
      className="ag-app-shell"
      style={{ minHeight: "100vh" }}
    >
      <Header
        className="ag-app-header"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "0 24px",
          background: "#fff",
          borderBottom: "1px solid #e5e6eb",
          height: 56,
        }}
      >
        <Space className="ag-app-brand">
          <button
            type="button"
            className="ag-app-nav-toggle"
            aria-label={
              mobileNavigationOpen ? "关闭平台导航" : "打开平台导航"
            }
            aria-controls="ag-platform-navigation"
            aria-expanded={mobileNavigationOpen}
            onClick={() =>
              setMobileNavigationOpen((current) => !current)
            }
          >
            <IconMenu aria-hidden="true" />
          </button>
          <button
            type="button"
            className="ag-app-brand__mark"
            aria-label="返回 AudioGraphy 仪表盘"
            onClick={() => navigate("/")}
          >
            <IconBranch />
          </button>
          <span className="ag-app-brand__copy">
            <Title heading={5} style={{ margin: 0 }}>
              AudioGraphy
            </Title>
            <span>销售接待智能工作台</span>
          </span>
          {isSitesDemo && (
            <span className="ag-demo-data-badge" role="status">
              交互演示 · 模拟数据
            </span>
          )}
        </Space>
        {user && (
          <Space className="ag-app-account">
            <span style={{ fontSize: 14, color: "#4e5969" }}>
              {user.name} ({user.role})
            </span>
            <Button
              size="mini"
              type="text"
              onClick={() => {
                clearAuth();
                navigate("/login", { replace: true });
              }}
            >
              退出
            </Button>
          </Space>
        )}
      </Header>
      <Layout className="ag-app-body">
        <Sider
          className={`ag-platform-sider${mobileNavigationOpen ? " is-mobile-open" : ""}`}
          width={228}
          style={{
            background: "#fff",
            borderRight: "1px solid #e5e6eb",
          }}
        >
          <nav
            id="ag-platform-navigation"
            className="ag-platform-navigation"
            aria-label="平台功能导航"
          >
            {visibleMenuGroups.map((group) => (
              <section
                key={group.label}
                className="ag-platform-menu-group"
                role="group"
                aria-labelledby={`ag-menu-group-${group.label}`}
              >
                <h2 id={`ag-menu-group-${group.label}`}>{group.label}</h2>
                <Menu
                  className="ag-platform-menu"
                  selectedKeys={[selectedKey]}
                  style={{ borderRight: "none" }}
                >
                  {group.items.map((item) => (
                    <Menu.Item
                      key={item.key}
                      onClick={() => navigate(item.key)}
                    >
                      {item.icon} {item.label}
                    </Menu.Item>
                  ))}
                </Menu>
              </section>
            ))}
          </nav>
        </Sider>
        {mobileNavigationOpen && (
          <button
            type="button"
            className="ag-platform-backdrop"
            aria-label="关闭平台导航"
            onClick={() => setMobileNavigationOpen(false)}
          />
        )}
        <Content
          className="ag-app-content"
          style={{
            background: "#f7f8fa",
            minHeight: "calc(100vh - 56px)",
          }}
        >
          <RouteErrorBoundary
            routeKey={location.pathname}
            onReturnHome={() => navigate("/", { replace: true })}
          >
            <Suspense fallback={<RouteLoadingFallback />}>
              <Routes>
                <Route path="/" element={<DashboardPage />} />
                <Route path="/recordings" element={<RecordingsPage />} />
                <Route path="/recordings/:id" element={<RecordingDetailPage />} />
                <Route path="/receptions" element={<ReceptionEntryPage />} />
                <Route
                  path="/receptions/:id/workspace"
                  element={<ReceptionWorkspacePage />}
                />
                <Route
                  path="/receptions/:id/graph"
                  element={<ReceptionGraphPage />}
                />
                <Route
                  path="/reception-flow"
                  element={<ReceptionStateInsightsPage />}
                />
                <Route path="/graph" element={<GraphExplorerPage />} />
                <Route path="/query" element={<QueryPage />} />
                <Route path="/speakers" element={<SpeakerProfileListPage />} />
                <Route
                  path="/speakers/:id"
                  element={<SpeakerProfileDetailPage />}
                />
                <Route
                  path="/communities"
                  element={<Navigate to="/graph?view=clusters" replace />}
                />
                <Route path="/time-travel" element={<TimeTravelPage />} />
                <Route path="/stats" element={<StatsPage />} />
                <Route path="/tag-insights" element={<TagInsightsPage />} />
                <Route
                  path="/open-api-keys"
                  element={
                    <GovernedRoute
                      allowed={user?.role === "admin"}
                      role={user?.role}
                    >
                      <OpenApiKeysPage />
                    </GovernedRoute>
                  }
                />
                <Route
                  path="/tag-governance"
                  element={
                    <GovernedRoute allowed={canGovernTags} role={user?.role}>
                      <TagGovernancePage />
                    </GovernedRoute>
                  }
                />
                <Route
                  path="/tag-review"
                  element={
                    <GovernedRoute allowed={canGovernTags} role={user?.role}>
                      <TagReviewPage />
                    </GovernedRoute>
                  }
                />
                <Route
                  path="/tag-runs/:id"
                  element={
                    <GovernedRoute allowed={canGovernTags} role={user?.role}>
                      <TagRunDetailPage />
                    </GovernedRoute>
                  }
                />
                <Route
                  path="/prompt-lab"
                  element={
                    <GovernedRoute allowed={canGovernTags} role={user?.role}>
                      <PromptLabPage />
                    </GovernedRoute>
                  }
                />
                <Route path="*" element={<RouteNotFoundView />} />
              </Routes>
            </Suspense>
          </RouteErrorBoundary>
        </Content>
      </Layout>
    </Layout>
  );
}

export default function App() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const loadFromStorage = useAuthStore((s) => s.loadFromStorage);
  const setUser = useAuthStore((s) => s.setUser);
  const location = useLocation();
  const [authHydrated, setAuthHydrated] = useState(false);

  useEffect(() => {
    const restored = loadFromStorage();
    setAuthHydrated(true);
    if (!restored) return;

    // The persisted profile is whatever the login response said, possibly days
    // ago. Roles gate real actions (tag governance, review decisions), so a
    // role an admin has since changed must not survive in this session — the
    // store update re-renders the shell, no reload required.
    let cancelled = false;
    // Imported lazily on purpose: a static import would pull the whole API
    // service module — and axios with it — into the entry chunk, which is the
    // same first-paint cost the lazy route split exists to avoid.
    void import("@/api/services")
      .then(({ getMe }) => getMe())
      .then((user) => {
        if (!cancelled) setUser(user);
      })
      .catch(() => {
        // Deliberately silent: the token is still valid, only the cached
        // profile may be stale. Logging the user out over a failed refresh
        // would turn a transient network error into a forced re-login.
      });
    return () => {
      cancelled = true;
    };
  }, [loadFromStorage, setUser]);

  // Do not redirect a deep link before persisted auth has been restored.
  if (!authHydrated) {
    return <RouteLoadingFallback />;
  }

  // Login page is always accessible
  if (location.pathname === "/login") {
    if (isAuthenticated) {
      return <Navigate to="/" replace />;
    }
    return (
      <Suspense fallback={<RouteLoadingFallback />}>
        <LoginPage />
      </Suspense>
    );
  }

  // All other routes require auth
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <AppLayout />;
}
