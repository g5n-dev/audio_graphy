import { Layout, Typography } from "@arco-design/web-react";
import { Routes, Route, Link } from "react-router-dom";

const { Header, Content, Footer } = Layout;
const { Title } = Typography;

export default function App() {
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header
        style={{
          display: "flex",
          alignItems: "center",
          padding: "0 24px",
          background: "#fff",
          borderBottom: "1px solid #e5e6eb",
        }}
      >
        <Title heading={5} style={{ margin: 0 }}>
          🎵 AudioGraphy
        </Title>
        <span style={{ marginLeft: 12, color: "#86909c", fontSize: 13 }}>
          M1.2 frontend stub — pages land in M6
        </span>
      </Header>
      <Content style={{ padding: 24 }}>
        <Routes>
          <Route
            path="/"
            element={
              <div style={{ padding: 48, textAlign: "center" }}>
                <Title heading={3}>AudioGraphy frontend (dev stub)</Title>
                <p style={{ color: "#86909c" }}>
                  React + Vite + Arco Design Web + AntV G6 · 已就位。
                  <br />
                  M1.2 仅验证 docker-compose 健康。M6 起接入页面与 API。
                </p>
                <p>
                  <Link to="/health">健康检查页</Link>
                </p>
              </div>
            }
          />
          <Route
            path="/health"
            element={
              <div style={{ padding: 48, textAlign: "center" }}>
                <Title heading={4}>✓ Frontend healthy</Title>
                <p style={{ color: "#86909c" }}>
                  Vite dev server reachable · HMR ready · API proxy /api →
                  backend:8000
                </p>
              </div>
            }
          />
        </Routes>
      </Content>
      <Footer style={{ textAlign: "center", color: "#86909c", fontSize: 12 }}>
        AudioGraphy · v0.1.0 · 2026-07
      </Footer>
    </Layout>
  );
}
