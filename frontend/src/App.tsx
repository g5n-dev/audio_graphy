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
 *   /speakers/:id — speaker profile detail (M7)
 *   /prompts      — prompt management
 */

import { useEffect } from "react";
import { Routes, Route, Navigate, useLocation, useNavigate } from "react-router-dom";
import { Layout, Menu, Typography, Button, Space } from "@arco-design/web-react";
import {
  IconDashboard,
  IconFileAudio,
  IconBranch,
  IconMessage,
  IconStorage,
  IconUser,
} from "@arco-design/web-react/icon";
import { useAuthStore } from "@/stores/auth";
import LoginPage from "@/pages/LoginPage";
import DashboardPage from "@/pages/DashboardPage";
import RecordingsPage from "@/pages/RecordingsPage";
import RecordingDetailPage from "@/pages/RecordingDetailPage";
import GraphExplorerPage from "@/pages/GraphExplorerPage";
import QueryPage from "@/pages/QueryPage";
import StatsPage from "@/pages/StatsPage";
import SpeakerProfileListPage from "@/pages/SpeakerProfile";
import SpeakerProfileDetailPage from "@/pages/SpeakerProfile/Detail";

const { Header, Sider, Content } = Layout;
const { Title } = Typography;

const menuItems = [
  { key: "/", icon: <IconDashboard />, label: "仪表盘" },
  { key: "/recordings", icon: <IconFileAudio />, label: "录音管理" },
  { key: "/graph", icon: <IconBranch />, label: "知识图谱" },
  { key: "/query", icon: <IconMessage />, label: "智能问答" },
  { key: "/speakers", icon: <IconUser />, label: "说话人" },
  { key: "/stats", icon: <IconStorage />, label: "标签统计" },
];

function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const clearAuth = useAuthStore((s) => s.clearAuth);

  const selectedKey =
    menuItems.find(
      (m) =>
        m.key === location.pathname ||
        (m.key !== "/" && location.pathname.startsWith(m.key)),
    )?.key ?? "/";

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header
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
        <Space>
          <Title heading={5} style={{ margin: 0 }}>
            🎵 AudioGraphy
          </Title>
          <span style={{ color: "#86909c", fontSize: 12 }}>
            门店录音图谱检索与多级打标系统
          </span>
        </Space>
        {user && (
          <Space>
            <span style={{ fontSize: 13, color: "#4e5969" }}>
              {user.name} ({user.role})
            </span>
            <Button
              size="mini"
              type="text"
              onClick={() => {
                clearAuth();
                window.location.href = "/login";
              }}
            >
              退出
            </Button>
          </Space>
        )}
      </Header>
      <Layout>
        <Sider
          style={{
            background: "#fff",
            borderRight: "1px solid #e5e6eb",
            width: 200,
          }}
        >
          <Menu selectedKeys={[selectedKey]} style={{ borderRight: "none" }}>
            {menuItems.map((item) => (
              <Menu.Item
                key={item.key}
                onClick={() => navigate(item.key)}
              >
                {item.icon} {item.label}
              </Menu.Item>
            ))}
          </Menu>
        </Sider>
        <Content style={{ background: "#f7f8fa", minHeight: "calc(100vh - 56px)" }}>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/recordings" element={<RecordingsPage />} />
            <Route path="/recordings/:id" element={<RecordingDetailPage />} />
            <Route path="/graph" element={<GraphExplorerPage />} />
            <Route path="/query" element={<QueryPage />} />
            <Route path="/speakers" element={<SpeakerProfileListPage />} />
            <Route path="/speakers/:id" element={<SpeakerProfileDetailPage />} />
            <Route path="/stats" element={<StatsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}

export default function App() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const loadFromStorage = useAuthStore((s) => s.loadFromStorage);
  const location = useLocation();

  useEffect(() => {
    loadFromStorage();
  }, [loadFromStorage]);

  // Login page is always accessible
  if (location.pathname === "/login") {
    if (isAuthenticated) {
      return <Navigate to="/" replace />;
    }
    return <LoginPage />;
  }

  // All other routes require auth
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <AppLayout />;
}
