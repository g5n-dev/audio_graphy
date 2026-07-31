/**
 * Recordings list page — paginated table with filters.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Table, Card, Input, Select, Tag } from "@arco-design/web-react";
import { useNavigate } from "react-router-dom";
import { listRecordings } from "@/api/services";
import { PanelState } from "@/components/PanelState";
import type { RecordingListItem, RecordingStatus } from "@/types/api";

const statusOptions: RecordingStatus[] = [
  "queued",
  "processing",
  "indexed",
  "ready_no_speech",
  "failed",
  "archived",
];

const statusColorMap: Record<string, string> = {
  queued: "gray",
  processing: "blue",
  indexed: "green",
  ready_no_speech: "cyan",
  failed: "red",
  archived: "orange",
};

export default function RecordingsPage() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);
  const [storeId, setStoreId] = useState<string>("");
  const [status, setStatus] = useState<RecordingStatus | "">("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["recordings", page, pageSize, storeId, status],
    queryFn: () =>
      listRecordings({
        page,
        page_size: pageSize,
        store_id: storeId || undefined,
        status: status || undefined,
      }),
  });

  return (
    <div>
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">RECORDING REPOSITORY · 录音库</span>
          <h1>录音管理</h1>
          <p>按门店与状态筛选，进入单条录音查看分段转写与标签详情。</p>
        </div>
      </header>

      <div style={{ padding: 24 }}>
      <Card style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <Input
            placeholder="门店ID"
            value={storeId}
            // Narrowing the result set can leave `page` past the last page of
            // it, which renders as an empty table the user cannot navigate out
            // of — so every filter change returns to page 1.
            onChange={(val) => {
              setStoreId(val);
              setPage(1);
            }}
            style={{ width: 150 }}
            allowClear
          />
          <Select
            placeholder="状态"
            value={status || undefined}
            onChange={(val) => {
              setStatus((val ?? "") as RecordingStatus | "");
              setPage(1);
            }}
            style={{ width: 150 }}
            allowClear
          >
            {statusOptions.map((s) => (
              <Select.Option key={s} value={s}>
                {s}
              </Select.Option>
            ))}
          </Select>
        </div>
      </Card>

      <Card>
        <PanelState
          pending={isLoading}
          error={error}
          // Keyed on the total, not on the current page: a page that happens to
          // be empty while `total` is non-zero must keep rendering the table,
          // because the pagination control the user needs lives inside it.
          empty={(data?.total ?? 0) === 0}
          emptyTitle="暂无录音"
          emptyDescription="当前门店与状态筛选下没有录音，调整筛选条件后再试。"
          onRetry={() => void refetch()}
          pendingLabel="正在加载录音列表…"
        >
          <Table
            data={data?.items ?? []}
            rowKey="id"
            size="small"
            pagination={{
              current: page,
              pageSize,
              total: data?.total ?? 0,
              onChange: (p) => setPage(p),
              showTotal: true,
            }}
            onRow={(record: RecordingListItem) => ({
              onClick: () => navigate(`/recordings/${record.id}`),
              style: { cursor: "pointer" },
            })}
            columns={[
              { title: "ID", dataIndex: "id", width: 70 },
              { title: "门店", dataIndex: "store_id", width: 80 },
              { title: "坐席", dataIndex: "agent_name", width: 120 },
              {
                title: "状态",
                dataIndex: "status",
                width: 100,
                render: (val: string) => <Tag color={statusColorMap[val] ?? "gray"}>{val}</Tag>,
              },
              { title: "管线", dataIndex: "pipeline_state", width: 100 },
              {
                title: "录制时间",
                dataIndex: "recorded_at",
                width: 180,
                render: (val: string | null) =>
                  val ? new Date(val).toLocaleString("zh-CN") : "-",
              },
              {
                title: "索引时间",
                dataIndex: "indexed_at",
                width: 180,
                render: (val: string | null) =>
                  val ? new Date(val).toLocaleString("zh-CN") : "-",
              },
            ]}
          />
        </PanelState>
      </Card>
      </div>
    </div>
  );
}
