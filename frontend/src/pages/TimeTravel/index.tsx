/**
 * TimeTravel page — M9 R2 T15.
 *
 * Inspector+ can pick a recording + an ISO timestamp and see all
 * bi-temporal edges alive at that moment. The page also exposes the
 * edge-history drawer: click any edge to see its audit-log events.
 *
 * GD-002 / GD-003 / GD-009 (graph drilldown closed loop):
 *   - Recording picker upgraded from InputNumber to AutoComplete
 *     (pulls from listRecordings).
 *   - Edge Action column gains a "跳到录音" link.
 *   - Consumes `?recording=<id>` URL param to pre-fill the picker.
 */

import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  AutoComplete,
  Card,
  DatePicker,
  Empty,
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
import { listRecordings } from "@/api/services";

const { Text } = Typography;

export default function TimeTravelPage() {
  const [searchParams] = useSearchParams();
  const [recordingId, setRecordingId] = useState<number>(1);
  const [at, setAt] = useState<Dayjs | null>(dayjs());
  const [loading, setLoading] = useState(false);
  const [edges, setEdges] = useState<EdgeOut[]>([]);
  const [historyVisible, setHistoryVisible] = useState(false);
  const [history, setHistory] = useState<EdgeEventOut[]>([]);
  const [autoCompleteValue, setAutoCompleteValue] = useState<string>("");

  // GD-002: fetch recording list for the AutoComplete picker
  const { data: recordingsData } = useQuery({
    queryKey: ["recordings", "list", "time-travel"],
    queryFn: () => listRecordings({ page: 1, page_size: 500 }),
    staleTime: 60_000,
  });

  // Build AutoComplete options from the recording list
  const recordingOptions = useMemo(() => {
    if (!recordingsData?.items) return [];
    return recordingsData.items.map((rec) => ({
      label: `#${rec.id} · ${rec.store_id} · ${rec.agent_name} · ${rec.status}`,
      value: String(rec.id),
    }));
  }, [recordingsData]);

  // Sync AutoComplete display value with recordingId
  useEffect(() => {
    const matchedOption = recordingOptions.find(
      (opt) => opt.value === String(recordingId),
    );
    setAutoCompleteValue(matchedOption?.label ?? String(recordingId));
  }, [recordingId, recordingOptions]);

  // GD-009: consume `?recording=<id>` URL param to pre-fill the picker
  useEffect(() => {
    const recordingParam = searchParams.get("recording");
    if (recordingParam) {
      const num = Number(recordingParam);
      if (!Number.isNaN(num) && num > 0) {
        setRecordingId(num);
      }
    }
    // Run once on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
      render: (v: string | null) =>
        v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "—",
    },
    {
      title: "Invalid at",
      dataIndex: "invalid_at",
      render: (v: string | null) =>
        v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "—",
    },
    {
      title: "Action",
      key: "action",
      render: (_: unknown, row: EdgeOut) => (
        <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <a onClick={() => openHistory(row)}>History</a>
          {/* GD-003: jump to recording detail (Q4: no at param — EdgeOut
              has no recording_id and valid_at is ISO fact-time, not
              recording offset) */}
          <Link to={`/recordings/${recordingId}`}>跳到录音</Link>
        </span>
      ),
    },
  ];

  return (
    <div>
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">BI-TEMPORAL GRAPH · 双时态图谱</span>
          <h1>时间旅行浏览器</h1>
          <p>选择录音与时间点，查看该时刻存活的图谱边及其审计历史。</p>
        </div>
      </header>

      <div style={{ padding: 24 }}>
      <Card style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
          <div>
            <Text>Recording</Text>
            <AutoComplete
              value={autoCompleteValue}
              data={recordingOptions.map((opt) => opt.label)}
              onSelect={(value: string) => {
                const matched = recordingOptions.find(
                  (opt) => opt.label === value,
                );
                if (matched) {
                  setRecordingId(Number(matched.value));
                }
              }}
              onChange={setAutoCompleteValue}
              placeholder="搜索录音 #ID、门店或坐席"
              style={{ width: 300 }}
              allowClear
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
    </div>
  );
}
