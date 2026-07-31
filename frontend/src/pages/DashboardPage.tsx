/**
 * Dashboard page — overview with quick stats and navigation cards.
 *
 * Shows: recording count, tag stats summary, prompt list, quick links.
 */

import { useQuery } from "@tanstack/react-query";
import { Card, Grid, Statistic, Spin, Tag, Table } from "@arco-design/web-react";
import { useNavigate } from "react-router-dom";
import { listRecordings, getStats, listPrompts } from "@/api/services";
import { useAuthStore } from "@/stores/auth";

const { Row, Col } = Grid;

export default function DashboardPage() {
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);

  const { data: recordingsData, isLoading: loadingRec } = useQuery({
    queryKey: ["recordings", "dashboard"],
    queryFn: () => listRecordings({ page: 1, page_size: 5 }),
  });

  const { data: statsData, isLoading: loadingStats } = useQuery({
    queryKey: ["stats", "dashboard"],
    queryFn: () => getStats({ group_by: "tag_path" }),
  });

  const { data: promptsData, isLoading: loadingPrompts } = useQuery({
    queryKey: ["prompts", "dashboard"],
    queryFn: () => listPrompts(),
  });

  const totalRecordings = recordingsData?.total ?? 0;
  const totalTags = statsData?.total_records ?? 0;
  const activePrompts = promptsData?.items.filter((p) => p.active).length ?? 0;

  const statusColorMap: Record<string, string> = {
    queued: "gray",
    processing: "blue",
    indexed: "green",
    ready_no_speech: "cyan",
    failed: "red",
    archived: "orange",
  };

  return (
    <div>
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">SYSTEM OVERVIEW · 系统概览</span>
          <h1>仪表盘</h1>
          <p>
            录音、标签与提示词的全局状态一览。
            {user && ` 欢迎, ${user.name} (${user.role} · ${user.tenant_id})`}
          </p>
        </div>
      </header>

      <div style={{ padding: 24 }}>
      <Spin loading={loadingRec && loadingStats && loadingPrompts} delay={200}>
        <Row gutter={24} style={{ marginBottom: 20 }}>
          <Col span={8}>
            <Card>
              <Statistic title="录音总数" value={totalRecordings} />
            </Card>
          </Col>
          <Col span={8}>
            <Card>
              <Statistic title="标签记录" value={totalTags} />
            </Card>
          </Col>
          <Col span={8}>
            <Card>
              <Statistic title="活跃提示词" value={activePrompts} />
            </Card>
          </Col>
        </Row>

        <Row gutter={24}>
          <Col span={12}>
            <Card
              title="最近录音"
              extra={
                <a onClick={() => navigate("/recordings")} style={{ cursor: "pointer" }}>
                  查看全部
                </a>
              }
            >
              <Table
                data={recordingsData?.items ?? []}
                rowKey="id"
                pagination={false}
                size="middle"
                columns={[
                  {
                    title: "ID",
                    dataIndex: "id",
                    width: 60,
                  },
                  {
                    title: "门店",
                    dataIndex: "store_id",
                    width: 80,
                  },
                  {
                    title: "坐席",
                    dataIndex: "agent_name",
                    width: 100,
                  },
                  {
                    title: "状态",
                    dataIndex: "status",
                    width: 100,
                    render: (val: string) => (
                      <Tag color={statusColorMap[val] ?? "gray"}>{val}</Tag>
                    ),
                  },
                ]}
              />
            </Card>
          </Col>
          <Col span={12}>
            <Card
              title="标签统计"
              extra={
                <a onClick={() => navigate("/stats")} style={{ cursor: "pointer" }}>
                  详情
                </a>
              }
            >
              <Table
                data={(statsData?.items ?? []).slice(0, 5)}
                rowKey="group_key"
                pagination={false}
                size="middle"
                columns={[
                  {
                    title: "标签路径",
                    dataIndex: "group_key",
                  },
                  {
                    title: "数量",
                    dataIndex: "tag_count",
                    width: 80,
                  },
                  {
                    title: "通过率",
                    dataIndex: "pass_rate",
                    width: 100,
                    render: (val: number) => `${(val * 100).toFixed(1)}%`,
                  },
                ]}
              />
            </Card>
          </Col>
        </Row>
      </Spin>
      </div>
    </div>
  );
}
