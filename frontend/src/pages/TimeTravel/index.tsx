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
        Message.info("该后端未启用高级图谱能力(ENABLE_ADVANCED_GRAPH)");
        setEdges([]);
      } else {
        Message.error("关系边加载失败");
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
    { title: "起点", dataIndex: "source" },
    { title: "关系", dataIndex: "relation" },
    { title: "终点", dataIndex: "target" },
    {
      title: "置信度",
      dataIndex: "confidence",
      render: (_: unknown, row: EdgeOut) => <Tag>{row.confidence}</Tag>,
    },
    {
      title: "生效于",
      dataIndex: "valid_at",
      render: (v: string | null) =>
        v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "—",
    },
    {
      title: "失效于",
      dataIndex: "invalid_at",
      render: (v: string | null) =>
        v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "—",
    },
    {
      title: "操作",
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
            <Text>录音</Text>
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
            <Text>时间基准</Text>
            <DatePicker
              showTime
              value={at ?? undefined}
              onChange={(v) => setAt(v ? dayjs(v) : null)}
              style={{ width: 220 }}
            />
          </div>
        </div>
      </Card>
      <Card title="该时刻生效的关系边">
        <Spin loading={loading}>
          {edges.length === 0 && !loading ? (
            // 双时态图谱的空是有含义的:这一时刻确实没有生效的边,
            // 而不是查询失败——文案要说清是「时间点」的问题。
            <div className="ag-timetravel-empty">
              <Empty description="该时间点没有生效中的关系边" />
              <p>关系边有生效与失效时间;把时间基准调到录音已索引之后再看。</p>
            </div>
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
        <Card title="边的变更历史(审计日志)" style={{ marginTop: 16 }}>
          <Table
            columns={[
              { title: "事件", dataIndex: "event_type" },
              { title: "操作者", dataIndex: "actor" },
              {
                title: "生效于",
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
