/**
 * Dashboard page — overview with quick stats and navigation cards.
 *
 * Shows: recording count, tag stats summary, prompt list, quick links.
 *
 * The three panels query independently, so each guards its own state: one
 * failing endpoint must not blank the two that loaded, and a failed count must
 * not render as "0" — that is indistinguishable from "no data".
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { Card, Grid, Statistic, Tag, Table } from "@arco-design/web-react";
import { Link } from "react-router-dom";
import { listRecordings, getStats, listPrompts } from "@/api/services";
import { PanelState } from "@/components/PanelState";
import { useAuthStore } from "@/stores/auth";

const { Row, Col } = Grid;

/** A count tile that stays honest when its query failed. */
function CountTile({
  title,
  value,
  query,
}: {
  title: string;
  value: number;
  query: Pick<UseQueryResult, "isLoading" | "isError">;
}) {
  if (query.isError) {
    return (
      <Card>
        <div role="alert">
          <div style={{ color: "#86909c", fontSize: 14 }}>{title}</div>
          <div style={{ color: "#f53f3f", fontSize: 20, fontWeight: 600 }}>—</div>
          <div style={{ color: "#f53f3f", fontSize: 12 }}>加载失败</div>
        </div>
      </Card>
    );
  }
  return (
    <Card>
      <Statistic title={title} value={query.isLoading ? "—" : value} />
    </Card>
  );
}

export default function DashboardPage() {
  const user = useAuthStore((s) => s.user);

  const recordingsQuery = useQuery({
    queryKey: ["recordings", "dashboard"],
    queryFn: () => listRecordings({ page: 1, page_size: 5 }),
  });

  const statsQuery = useQuery({
    queryKey: ["stats", "dashboard"],
    queryFn: () => getStats({ group_by: "tag_path" }),
  });

  const promptsQuery = useQuery({
    queryKey: ["prompts", "dashboard"],
    queryFn: () => listPrompts(),
  });

  const recordingsData = recordingsQuery.data;
  const statsData = statsQuery.data;
  const recentRecordings = recordingsData?.items ?? [];
  const topTagStats = (statsData?.items ?? []).slice(0, 5);

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
        <Row gutter={24} style={{ marginBottom: 20 }}>
          <Col span={8}>
            <CountTile
              title="录音总数"
              value={recordingsData?.total ?? 0}
              query={recordingsQuery}
            />
          </Col>
          <Col span={8}>
            <CountTile
              title="标签记录"
              value={statsData?.total_records ?? 0}
              query={statsQuery}
            />
          </Col>
          <Col span={8}>
            <CountTile
              title="活跃提示词"
              value={promptsQuery.data?.items.filter((p) => p.active).length ?? 0}
              query={promptsQuery}
            />
          </Col>
        </Row>

        <Row gutter={24}>
          <Col span={12}>
            <Card title="最近录音" extra={<Link to="/recordings">查看全部</Link>}>
              <PanelState
                pending={recordingsQuery.isLoading}
                error={recordingsQuery.error}
                empty={recentRecordings.length === 0}
                emptyTitle="暂无录音"
                emptyDescription="导入录音后，这里会显示最近处理的几条。"
                onRetry={() => void recordingsQuery.refetch()}
                pendingLabel="正在加载录音…"
              >
                <Table
                  data={recentRecordings}
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
                      render: (val: string | null) => val ?? "—",
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
              </PanelState>
            </Card>
          </Col>
          <Col span={12}>
            <Card title="标签统计" extra={<Link to="/stats">详情</Link>}>
              <PanelState
                pending={statsQuery.isLoading}
                error={statsQuery.error}
                empty={topTagStats.length === 0}
                emptyTitle="暂无标签统计"
                emptyDescription="完成打标后，这里会显示各标签的数量与通过率。"
                onRetry={() => void statsQuery.refetch()}
                pendingLabel="正在加载标签统计…"
              >
                <Table
                  data={topTagStats}
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
                      render: (val: number | null) =>
                        typeof val === "number" ? `${(val * 100).toFixed(1)}%` : "—",
                    },
                  ]}
                />
              </PanelState>
            </Card>
          </Col>
        </Row>
      </div>
    </div>
  );
}
