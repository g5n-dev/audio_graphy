/**
 * CommunityExplorer page — M9 R2 T15.
 *
 * Lists Leiden communities from the latest run, supports drill-down
 * into a community's children at the next hierarchy level, and a
 * search box that triggers /search/global.
 */

import { useEffect, useState } from "react";
import {
  Button,
  Card,
  Empty,
  Input,
  Message,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "@arco-design/web-react";
import {
  globalSearch,
  drillDown,
  type CommunityHit,
} from "@/api/advancedGraph";

const { Title } = Typography;

export default function CommunityExplorerPage() {
  const [query, setQuery] = useState("长安CS75");
  const [level] = useState(0);
  const [hits, setHits] = useState<CommunityHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [children, setChildren] = useState<Record<number, CommunityHit[]>>({});

  async function refresh() {
    setLoading(true);
    try {
      const resp = await globalSearch({
        query,
        level,
        top_k: 10,
      });
      setHits(resp.hits);
    } catch (err) {
      const e = err as { response?: { status?: number } };
      if (e.response?.status === 404) {
        Message.info("Advanced graph endpoints are disabled on this backend");
        setHits([]);
      } else {
        Message.error("Global search failed");
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onDrill(hit: CommunityHit) {
    try {
      const resp = await drillDown(hit.community_id, { level: hit.level });
      setChildren((c) => ({ ...c, [hit.community_id]: resp.children }));
    } catch {
      Message.error("Drill-down failed");
    }
  }

  const columns = [
    { title: "ID", dataIndex: "community_id", width: 80 },
    { title: "Title", dataIndex: "title" },
    { title: "Summary", dataIndex: "summary", ellipsis: true },
    {
      title: "Score",
      dataIndex: "score",
      render: (v: number) => <Tag color="arc-blue">{v.toFixed(3)}</Tag>,
    },
    { title: "Members", dataIndex: "member_count", width: 100 },
    {
      title: "Action",
      key: "action",
      render: (_: unknown, row: CommunityHit) => (
        <Space>
          <Button size="mini" onClick={() => onDrill(row)}>
            Drill down
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <Title heading={4}>Community Explorer</Title>
      <Card style={{ marginBottom: 16 }}>
        <Space>
          <Input
            value={query}
            onChange={(v) => setQuery(v)}
            placeholder="Search query"
            style={{ width: 320 }}
          />
          <Button type="primary" onClick={refresh} loading={loading}>
            Search
          </Button>
        </Space>
      </Card>
      <Card title={`Level-${level} communities`}>
        <Spin loading={loading}>
          {hits.length === 0 && !loading ? (
            <Empty description="No communities match this query" />
          ) : (
            <Table
              columns={columns}
              data={hits}
              rowKey={(row) => `${row.level}_${row.community_id}`}
              pagination={{ pageSize: 10 }}
            />
          )}
        </Spin>
      </Card>
      {Object.entries(children).map(([cid, items]) => (
        <Card
          key={cid}
          title={`Children of community #${cid}`}
          style={{ marginTop: 16 }}
        >
          <Table
            columns={[
              { title: "ID", dataIndex: "community_id" },
              { title: "Title", dataIndex: "title" },
              { title: "Summary", dataIndex: "summary", ellipsis: true },
              { title: "Members", dataIndex: "member_count" },
            ]}
            data={items}
            rowKey={(row) => `${row.level}_${row.community_id}`}
            pagination={false}
          />
        </Card>
      ))}
    </div>
  );
}
