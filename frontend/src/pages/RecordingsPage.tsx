/**
 * Recordings list page — paginated table with filters.
 *
 * Ingest entry point: admins register a server-side audio file via the
 * 导入录音 dialog (POST /recordings), then the list polls while any row is
 * still queued/processing so progress is visible without manual refresh.
 * Failed rows expose an admin-only retry (POST /recordings/{id}/reindex);
 * rows stuck in processing expose a force variant of the same call.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  DatePicker,
  Form,
  Input,
  Message,
  Modal,
  Select,
  Table,
  Tag,
  Typography,
} from "@arco-design/web-react";
import { useNavigate } from "react-router-dom";
import { createRecording, listRecordings, reindexRecording } from "@/api/services";
import { PanelState } from "@/components/PanelState";
import { useAuthStore } from "@/stores/auth";
import { getErrorMessage } from "@/utils/errors";
import { recordingsPollInterval } from "@/utils/recordingsPolling";
import { useEventStream } from "@/hooks/useEventStream";
import type {
  RecordingCreateRequest,
  RecordingListItem,
  RecordingStatus,
} from "@/types/api";

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

interface ImportFormValues {
  store_id: string;
  path: string;
  recorded_at?: string;
}

function ImportRecordingModal({
  visible,
  onClose,
}: {
  visible: boolean;
  onClose: () => void;
}) {
  const [form] = Form.useForm<ImportFormValues>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const importMutation = useMutation({
    mutationFn: (body: RecordingCreateRequest) => createRecording(body),
    onSuccess: (recording) => {
      Message.success(`录音 #${recording.id} 已登记，进入处理队列`);
      void queryClient.invalidateQueries({ queryKey: ["recordings"] });
      onClose();
      form.resetFields();
      // Land the operator on the detail page where the pipeline panel shows
      // live progress — otherwise the newly queued row is easy to lose in
      // the list.
      navigate(`/recordings/${recording.id}`);
    },
    onError: (error) => {
      Message.error(getErrorMessage(error, "登记录音失败"));
    },
  });

  return (
    <Modal
      title="导入录音"
      visible={visible}
      onCancel={onClose}
      onOk={() => {
        void form.validate().then((values) => {
          const body: RecordingCreateRequest = {
            store_id: values.store_id.trim(),
            path: values.path.trim(),
          };
          if (values.recorded_at) {
            // Arco DatePicker yields "YYYY-MM-DD HH:mm:ss"; the backend
            // parses it as a naive ISO 8601 timestamp.
            body.recorded_at = values.recorded_at.replace(" ", "T");
          }
          importMutation.mutate(body);
        });
      }}
      okText="登记录音"
      cancelText="取消"
      confirmLoading={importMutation.isPending}
      maskClosable={false}
    >
      <Typography.Paragraph type="secondary" style={{ marginBottom: 16 }}>
        此处登记的是服务器（后端工作目录 / 挂载卷）上的音频文件路径，
        不是本机上传：请先将文件放到服务端，再填写其路径。演示 mock
        数据位于后端工作目录下。
      </Typography.Paragraph>
      <Form form={form} layout="vertical" disabled={importMutation.isPending}>
        <Form.Item
          label="门店ID"
          field="store_id"
          rules={[{ required: true, message: "请输入门店ID" }]}
        >
          <Input placeholder="例如 store-001" />
        </Form.Item>
        <Form.Item
          label="服务端音频路径"
          field="path"
          rules={[{ required: true, message: "请输入服务端音频文件路径" }]}
          extra="相对后端工作目录的路径或卷内绝对路径，例如 mock_data/demo.wav"
        >
          <Input placeholder="mock_data/demo.wav" />
        </Form.Item>
        <Form.Item label="录制时间（可选）" field="recorded_at">
          <DatePicker showTime style={{ width: "100%" }} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

export default function RecordingsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // 事件流是加速器不是依赖:收到录音终态/失败事件立即刷新列表,
  // 5 秒轮询仍在,流断了页面照常工作。
  useEventStream(
    ["recording.indexed", "recording.ready_no_speech", "recording.failed"],
    () => {
      void queryClient.invalidateQueries({ queryKey: ["recordings"] });
    },
  );
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === "admin";
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);
  const [storeId, setStoreId] = useState<string>("");
  const [status, setStatus] = useState<RecordingStatus | "">("");
  const [importVisible, setImportVisible] = useState(false);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["recordings", page, pageSize, storeId, status],
    queryFn: () =>
      listRecordings({
        page,
        page_size: pageSize,
        store_id: storeId || undefined,
        status: status || undefined,
      }),
    // Keep progress visible while any row is queued/processing.
    refetchInterval: (query) => recordingsPollInterval(query.state.data?.items),
  });

  const reindexMutation = useMutation({
    mutationFn: ({ id, force }: { id: number; force: boolean }) =>
      reindexRecording(id, { force }),
    onSuccess: (result) => {
      Message.success(`录音 #${result.id} 已重新排队处理`);
      void queryClient.invalidateQueries({ queryKey: ["recordings"] });
      void queryClient.invalidateQueries({ queryKey: ["recording", result.id] });
    },
    onError: (mutationError) => {
      Message.error(getErrorMessage(mutationError, "重新处理失败"));
    },
  });

  return (
    <div>
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">RECORDING REPOSITORY · 录音库</span>
          <h1>录音管理</h1>
          <p>按门店与状态筛选，进入单条录音查看分段转写与标签详情。</p>
        </div>
        {isAdmin && (
          <Button type="primary" onClick={() => setImportVisible(true)}>
            导入录音
          </Button>
        )}
      </header>

      <ImportRecordingModal
        visible={importVisible}
        onClose={() => setImportVisible(false)}
      />

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
          emptyDescription={
            isAdmin
              ? "当前筛选下没有录音，可点击右上角「导入录音」登记服务端音频文件。"
              : "当前门店与状态筛选下没有录音，调整筛选条件后再试。"
          }
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
              ...(isAdmin
                ? [
                    {
                      title: "操作",
                      width: 110,
                      render: (_: unknown, record: RecordingListItem) => {
                        if (record.status === "failed") {
                          return (
                            <Button
                              size="mini"
                              type="outline"
                              status="danger"
                              loading={
                                reindexMutation.isPending &&
                                reindexMutation.variables?.id === record.id
                              }
                              onClick={(e) => {
                                // The row itself navigates on click; retry
                                // must stay on the list.
                                e.stopPropagation();
                                reindexMutation.mutate({
                                  id: record.id,
                                  force: false,
                                });
                              }}
                            >
                              重试
                            </Button>
                          );
                        }
                        if (record.status === "processing") {
                          return (
                            <Button
                              size="mini"
                              type="outline"
                              loading={
                                reindexMutation.isPending &&
                                reindexMutation.variables?.id === record.id
                              }
                              onClick={(e) => {
                                e.stopPropagation();
                                // force=true breaks the stale lease of a run
                                // stuck in processing and re-queues it.
                                reindexMutation.mutate({
                                  id: record.id,
                                  force: true,
                                });
                              }}
                            >
                              强制重跑
                            </Button>
                          );
                        }
                        return null;
                      },
                    },
                  ]
                : []),
            ]}
          />
        </PanelState>
      </Card>
      </div>
    </div>
  );
}
