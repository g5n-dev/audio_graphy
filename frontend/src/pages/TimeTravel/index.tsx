/**
 * TimeTravel page — M9 R2 T15.
 *
 * Inspector+ can pick a recording + an ISO timestamp and see all
 * bi-temporal edges alive at that moment. The page also exposes the
 * edge-history drawer: click any edge to see its audit-log events.
 */

import { useEffect, useState } from "react";
import {
  Card,
  DatePicker,
  Empty,
  InputNumber,
  Message,
  Spin,
  Table,
  Tag,
  Typography,
} from "@arco-design/web-react";
import dayjs, { type Dayjs } from "dayjs";
import {
  timeTravelEdges,
  edgeHistory,
  type EdgeOut,
  type EdgeEventOut,
} from "@/api/advancedGraph";

const { Title, Text } = Typography;

export default function TimeTravelPage() {
  const [recordingId, setRecordingId] = useState<number>(1);
  const [at, setAt] = useState<Dayjs | null>(dayjs());
  const [loading, setLoading] = useState(false);
  const [edges, setEdges] = useState<EdgeOut[]>([]);
  const [historyVisible, setHistoryVisible] = useState(false);
  const [history, setHistory] = useState<EdgeEventOut[]>([]);

  async function refresh() {
    if (!at) return;
    setLoading(true);
    try {
      const resp = await timeTravelEdges(recordingId, {
        at: at.toISOString(),
      });
      setEdges(resp.edges);
    } catch (err) {
      const e = err as { response?: { status?: number } };
      if (e.response?.status === 404) {
        Message.info("Advanced graph endpoints are disabled on this backend");
        setEdges([]);
      } else {
        Message.error("Failed to load edges");
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recordingId, at?.toISOString()]);

  async function openHistory(edge: EdgeOut) {
    const key = `${edge.source}|${edge.relation}|${edge.target}`;
    try {
      const resp = await edgeHistory(recordingId, key);
      setHistory(resp.events);
      setHistoryVisible(true);
    } catch {
      Message.error("Failed to load edge history");
    }
  }

  const columns = [
    { title: "Source", dataIndex: "source" },
    { title: "Relation", dataIndex: "relation" },
    { title: "Target", dataIndex: "target" },
    {
      title: "Confidence",
      dataIndex: "confidence",
      render: (_: unknown, row: EdgeOut) => <Tag>{row.confidence}</Tag>,
    },
    {
      title: "Valid at",
      dataIndex: "valid_at",
      render: (v: string | null) => (v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "—"),
    },
    {
      title: "Invalid at",
      dataIndex: "invalid_at",
      render: (v: string | null) => (v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "—"),
    },
    {
      title: "Action",
      key: "action",
      render: (_: unknown, row: EdgeOut) => (
        <a onClick={() => openHistory(row)}>History</a>
      ),
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <Title heading={4}>Time Travel Explorer</Title>
      <Card style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
          <div>
            <Text>Recording ID</Text>
            <InputNumber
              value={recordingId}
              onChange={(v) => setRecordingId(Number(v) || 1)}
              style={{ width: 120 }}
            />
          </div>
          <div>
            <Text>As-of</Text>
            <DatePicker
              showTime
              value={at ?? undefined}
              onChange={(v) => setAt(v ? dayjs(v) : null)}
              style={{ width: 220 }}
            />
          </div>
        </div>
      </Card>
      <Card title="Live edges">
        <Spin loading={loading}>
          {edges.length === 0 && !loading ? (
            <Empty description="No edges alive at this timestamp" />
          ) : (
            <Table
              columns={columns}
              data={edges}
              rowKey={(row) => `${row.source}|${row.relation}|${row.target}`}
              pagination={{ pageSize: 10 }}
            />
          )}
        </Spin>
      </Card>
      {historyVisible && (
        <Card title="Edge history (audit log)" style={{ marginTop: 16 }}>
          <Table
            columns={[
              { title: "Event", dataIndex: "event_type" },
              { title: "Actor", dataIndex: "actor" },
              {
                title: "Valid at",
                dataIndex: "valid_at",
                render: (v: string) => dayjs(v).format("YYYY-MM-DD HH:mm"),
              },
            ]}
            data={history}
            rowKey="id"
            pagination={false}
          />
        </Card>
      )}
    </div>
  );
}
