/**
 * Login page — email/password form.
 *
 * On success: stores JWT, redirects to dashboard.
 * On failure: shows error message via Arco Notification.
 */

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Form, Input, Button, Card, Typography, Notification } from "@arco-design/web-react";
import { IconLock, IconUser } from "@arco-design/web-react/icon";
import { login } from "@/api/services";
import { useAuthStore } from "@/stores/auth";

const { Title } = Typography;
const FormItem = Form.Item;
const IS_SITES_DEMO = import.meta.env.VITE_SITES_DEMO === "true";

export default function LoginPage() {
  const navigate = useNavigate();
  const setAuth = useAuthStore((s) => s.setAuth);
  const [loading, setLoading] = useState(false);
  const [email, setEmail] = useState(
    IS_SITES_DEMO ? "demo@example.com" : "",
  );
  const [password, setPassword] = useState(IS_SITES_DEMO ? "demo" : "");

  const handleSubmit = async () => {
    if (!email || !password) {
      Notification.warning({ title: "请填写邮箱和密码", content: " " });
      return;
    }
    setLoading(true);
    try {
      const resp = await login(email, password);
      setAuth(resp.access_token, resp.refresh_token, resp.user);
      Notification.success({ title: "登录成功", content: `欢迎, ${resp.user.name}` });
      navigate("/");
    } catch (err: unknown) {
      const msg =
        err && typeof err === "object" && "response" in err
          ? // eslint-disable-next-line @typescript-eslint/no-explicit-any
            ((err as any).response?.data?.error?.message ?? "登录失败")
          : "登录失败";
      Notification.error({ title: "登录失败", content: msg });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "#f7f8fa",
      }}
    >
      <Card style={{ width: 400, boxShadow: "0 2px 8px rgba(0,0,0,0.08)" }}>
        <div style={{ textAlign: "center", marginBottom: 24 }}>
          <Title heading={4} style={{ margin: 0 }}>
            AudioGraphy
          </Title>
          <p style={{ color: "#86909c", fontSize: 14, marginTop: 4 }}>
            门店录音图谱检索与多级打标系统
          </p>
          {IS_SITES_DEMO && (
            <p
              role="note"
              style={{
                margin: "12px 0 0",
                padding: "8px 10px",
                color: "#0e42d2",
                background: "#e8f3ff",
                border: "1px solid #bedaff",
                borderRadius: 4,
                fontSize: 14,
              }}
            >
              在线演示数据已就绪，直接点击登录即可体验。
            </p>
          )}
        </div>
        <Form layout="vertical" onSubmit={handleSubmit}>
          <FormItem label="邮箱">
            <Input
              prefix={<IconUser />}
              // Deliberately generic: a real-looking address here reads as a
              // preconfigured account, and there is none — the first user is
              // created with scripts/bootstrap_admin.py.
              placeholder="you@example.com"
              value={email}
              onChange={setEmail}
              size="large"
            />
          </FormItem>
          <FormItem label="密码">
            <Input.Password
              prefix={<IconLock />}
              placeholder="请输入密码"
              value={password}
              onChange={setPassword}
              size="large"
            />
          </FormItem>
          <FormItem>
            <Button
              type="primary"
              htmlType="submit"
              long
              size="large"
              loading={loading}
            >
              登录
            </Button>
          </FormItem>
        </Form>
      </Card>
    </div>
  );
}
