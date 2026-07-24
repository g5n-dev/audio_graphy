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
 *   /communities  — M9 R2 T15 Leiden community explorer
 *   /prompts      — prompt management
 */

import { lazy, Suspense, useEffect, useState } from "react";
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
  IconCommon,
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
const SpeakerProfileListPage = lazy(() => import("@/pages/SpeakerProfile"));
const SpeakerProfileDetailPage = lazy(
  () => import("@/pages/SpeakerProfile/Detail"),
);
const TimeTravelPage = lazy(() => import("@/pages/TimeTravel"));
const CommunityExplorerPage = lazy(
  () => import("@/pages/CommunityExplorer"),
);
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
      { key: "/speakers", icon: <IconUser />, label: "说话人" },
      { key: "/communities", icon: <IconCommon />, label: "社区探索" },
      { key: "/time-travel", icon: <IconClockCircle />, label: "时间演化" },
    ],
  },
];

const menuItems = menuGroups.flatMap((group) => group.items);

function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const clearAuth = useAuthStore((s) => s.clearAuth);

  useEffect(() => {
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
  }, [location.pathname]);

  const selectedKey =
    menuItems.find(
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
            <span style={{ fontSize: 13, color: "#4e5969" }}>
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
      <Layout>
        <Sider
          className="ag-platform-sider"
          width={208}
          style={{
            background: "#fff",
            borderRight: "1px solid #e5e6eb",
          }}
        >
          <nav
            className="ag-platform-navigation"
            aria-label="平台功能导航"
          >
            {menuGroups.map((group) => (
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
        <Content
          className="ag-app-content"
          style={{
            background: "#f7f8fa",
            minHeight: "calc(100vh - 56px)",
          }}
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
              <Route path="/speakers/:id" element={<SpeakerProfileDetailPage />} />
              <Route path="/communities" element={<CommunityExplorerPage />} />
              <Route path="/time-travel" element={<TimeTravelPage />} />
              <Route path="/stats" element={<StatsPage />} />
              <Route path="/tag-insights" element={<TagInsightsPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </Content>
      </Layout>
    </Layout>
  );
}

export default function App() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const loadFromStorage = useAuthStore((s) => s.loadFromStorage);
  const location = useLocation();
  const [authHydrated, setAuthHydrated] = useState(false);

  useEffect(() => {
    loadFromStorage();
    setAuthHydrated(true);
  }, [loadFromStorage]);

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
