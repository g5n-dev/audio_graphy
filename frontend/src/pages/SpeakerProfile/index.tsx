/**
 * SpeakerProfile list page — M7 WS-3 T12.
 *
 * Lists all SpeakerNode rows for the current tenant. Each row shows:
 *   - Display name + SpeakerBadge (role + ambiguity)
 *   - voiceprint_hash (truncated to vp_xxxxxxxx, PIPL-compliant)
 *   - recordings_count
 *   - merge_strategy
 *   - first_seen
 *
 * Filters: speaker_role, ambiguity. Click → Detail.tsx.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Badge,
  Button,
  Card,
  Select,
  Table,
} from "@arco-design/web-react";
import { IconSafe } from "@arco-design/web-react/icon";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import dayjs from "dayjs";
import { parseFocusParam } from "@/utils/urlParams";
import { listSpeakers } from "@/api/speakers";
import { listSpeakerMergePending } from "@/api/advancedGraph";
import { PanelState } from "@/components/PanelState";
import { SpeakerBadge } from "@/components/SpeakerBadge";
import { VoiceprintQualityDrawer } from "@/components/VoiceprintQualityDrawer";
import { useVoiceprintPolicy } from "@/hooks/useVoiceprintPolicy";
import { getErrorMessage } from "@/utils/errors";
import type { SpeakerListItem } from "@/types/api";

const ROLE_ALL_SENTINEL = "";
const roleOptions: Array<{ label: string; value: string }> = [
  { label: "全部角色", value: ROLE_ALL_SENTINEL },
  { label: "坐席", value: "agent" },
  { label: "客户", value: "customer" },
  { label: "未知", value: "unknown" },
];

type AmbiguityFilterValue = "AMBIGUOUS" | "PENDING_REVIEW" | "none";

/**
 * Select.Option value must be string | number (not undefined).
 * We use "" as the "all" sentinel and convert back in the query call.
 */
const AMBIGUITY_ALL_SENTINEL = "";
const ambiguityOptions: Array<{ label: string; value: string }> = [
  { label: "全部", value: AMBIGUITY_ALL_SENTINEL },
  { label: "仅模糊合并", value: "AMBIGUOUS" },
  { label: "待审核", value: "PENDING_REVIEW" },
  { label: "非模糊", value: "none" },
];

/**
 * Read the `?focus=<type>:<id>` parameter other pages link here with.
 *
 * Recording focus narrows the roster server-side. Entity focus (a customer
 * or agent name from the graph) cannot: speaker display names are voiceprint
 * hashes, not people's names — so it only preselects the role and says where
 * the user came from, rather than silently returning everything.
 */
function useSpeakerFocus(): {
  label: string;
  recordingId?: number;
  role?: "agent" | "customer";
} | null {
  const [searchParams] = useSearchParams();
  const parsed = parseFocusParam(searchParams.get("focus"));
  if (!parsed) return null;
  if (parsed.type === "录音") {
    const recordingId = Number(parsed.id);
    if (!Number.isFinite(recordingId)) return null;
    return { label: `录音 #${recordingId}`, recordingId };
  }
  if (parsed.type === "客户") {
    return { label: `客户「${parsed.id}」`, role: "customer" };
  }
  if (parsed.type === "坐席") {
    return { label: `坐席「${parsed.id}」`, role: "agent" };
  }
  return { label: `${parsed.type}「${parsed.id}」` };
}

export default function SpeakerProfileListPage(): JSX.Element {
  const navigate = useNavigate();
  const focus = useSpeakerFocus();
  const [roleFilter, setRoleFilter] = useState<string>(ROLE_ALL_SENTINEL);
  const [ambiguityFilter, setAmbiguityFilter] = useState<string>(
    AMBIGUITY_ALL_SENTINEL,
  );
  const [qualityDrawerVisible, setQualityDrawerVisible] = useState(false);

  const policyQuery = useVoiceprintPolicy();
  const ambiguousRange = policyQuery.data
    ? {
        low: policyQuery.data.layer1.cosine_threshold,
        high: policyQuery.data.layer1.ambiguous_threshold,
      }
    : undefined;

  const pendingCountQuery = useQuery({
    queryKey: ["speaker-merge-pending", "global-count"],
    queryFn: () => listSpeakerMergePending({ status: "pending", limit: 1 }),
    refetchInterval: 30_000,
  });
  /**
   * A failed count must never be drawn as a badge-less "0": the badge is the
   * only hint on this page that merges are waiting for a human, and a wrong
   * automatic merge is exactly what that queue exists to catch. On failure the
   * badge becomes a "!" and an alert below offers a retry.
   */
  const pendingCountError = pendingCountQuery.error;
  const pendingTotal = pendingCountQuery.data?.total ?? 0;

  /**
   * Translate UI state → backend query param.
   * Empty string → undefined (no filter).
   */
  const roleParam: "agent" | "customer" | "unknown" | undefined =
    roleFilter !== ROLE_ALL_SENTINEL
      ? (roleFilter as "agent" | "customer" | "unknown")
      : // An entity focus implies its role until the user picks otherwise.
        focus?.role;
  const ambiguityParam: AmbiguityFilterValue | undefined =
    ambiguityFilter === AMBIGUITY_ALL_SENTINEL
      ? undefined
      : (ambiguityFilter as AmbiguityFilterValue);

  const speakersQuery = useQuery({
    queryKey: ["speakers", roleParam, ambiguityParam, focus?.recordingId],
    queryFn: () =>
      listSpeakers({
        speaker_role: roleParam,
        ambiguity: ambiguityParam,
        recording_id: focus?.recordingId,
        limit: 200,
      }),
  });
  const { data, isLoading } = speakersQuery;

  return (
    <div>
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">SPEAKER PROFILES · 说话人画像</span>
          <h1>说话人管理</h1>
          <p>按角色与合并状态筛选说话人，查看声纹信息与跨录音关系。</p>
        </div>
        <div className="ag-feature-header__actions">
          <Badge
            count={pendingTotal}
            text={pendingCountError ? "!" : undefined}
            maxCount={99}
          >
            <Button
              icon={<IconSafe />}
              onClick={() => setQualityDrawerVisible(true)}
            >
              声纹质量
            </Button>
          </Badge>
        </div>
      </header>

      <VoiceprintQualityDrawer
        visible={qualityDrawerVisible}
        onClose={() => setQualityDrawerVisible(false)}
      />

      <div style={{ padding: 24 }}>
      {pendingCountError ? (
        <Alert
          type="error"
          style={{ marginBottom: 16 }}
          title="待复核数量加载失败"
          content={`${getErrorMessage(pendingCountError)} 待复核数量未知，请重试后再判断是否有需要人工复核的合并。`}
          action={
            <Button
              size="small"
              onClick={() => void pendingCountQuery.refetch()}
            >
              重试
            </Button>
          }
        />
      ) : null}
      {focus ? (
        <Alert
          type="info"
          style={{ marginBottom: 16 }}
          closable
          content={
            focus.recordingId !== undefined ? (
              <span>
                仅显示 {focus.label} 中出现的说话人。
                <Link to="/speakers" style={{ marginLeft: 8 }}>
                  查看全部
                </Link>
              </span>
            ) : (
              <span>
                来自 {focus.label}。说话人档案以声纹标识命名，无法按姓名精确匹配，
                这里只按角色做了预筛选。
              </span>
            )
          }
        />
      ) : null}
      <Card style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <Select
            placeholder="角色"
            value={roleFilter}
            onChange={(v: string) => setRoleFilter(v ?? ROLE_ALL_SENTINEL)}
            style={{ width: 160 }}
            allowClear
          >
            {roleOptions.map((opt) => (
              <Select.Option key={opt.label} value={opt.value}>
                {opt.label}
              </Select.Option>
            ))}
          </Select>
          <Select
            placeholder="合并状态"
            value={ambiguityFilter}
            onChange={(v: string) =>
              setAmbiguityFilter(v ?? AMBIGUITY_ALL_SENTINEL)
            }
            style={{ width: 160 }}
            allowClear
          >
            {ambiguityOptions.map((opt) => (
              <Select.Option key={opt.label} value={opt.value}>
                {opt.label}
              </Select.Option>
            ))}
          </Select>
        </div>
      </Card>

      <Card>
        {/* A failed request must not render as an empty roster — PanelState
            surfaces the backend message instead. */}
        <PanelState
          pending={isLoading}
          error={speakersQuery.error}
          empty={(data?.items ?? []).length === 0}
          emptyTitle="暂无说话人"
          emptyDescription="录音完成声纹链路处理后，这里会显示跨录音的说话人档案。"
          onRetry={() => void speakersQuery.refetch()}
          pendingLabel="正在加载说话人…"
        >
        <Table
          data={data?.items ?? []}
          rowKey="id"
          size="small"
          pagination={{
            pageSize: 20,
            showTotal: true,
          }}
          onRow={(record: SpeakerListItem) => ({
            onClick: () => navigate(`/speakers/${record.id}`),
            style: { cursor: "pointer" },
          })}
          columns={[
            {
              title: "显示名",
              dataIndex: "display_name",
              width: 200,
              render: (val: string, record: SpeakerListItem) => (
                <span>
                  {val}
                  <SpeakerBadge
                    role={record.speaker_role}
                    ambiguity={record.ambiguity_tag}
                    size="small"
                    ambiguousRange={ambiguousRange}
                  />
                </span>
              ),
            },
            {
              title: "声纹Hash",
              dataIndex: "voiceprint_hash",
              width: 120,
              render: (val: string) => (
                <code style={{ fontSize: 14, color: "#86909c" }}>{val}</code>
              ),
            },
            {
              title: "录音数",
              dataIndex: "recordings_count",
              width: 80,
            },
            {
              title: "合并策略",
              dataIndex: "merge_strategy",
              width: 130,
              render: (val: string) => (
                <span style={{ fontSize: 14 }}>{val}</span>
              ),
            },
            {
              title: "置信度",
              dataIndex: "merge_confidence",
              width: 90,
              render: (val: number) => val.toFixed(3),
            },
            {
              title: "首次出现",
              dataIndex: "first_seen",
              width: 170,
              render: (val: string | null) =>
                val ? dayjs(val).format("YYYY-MM-DD HH:mm") : "-",
            },
            {
              title: "总发言秒数",
              dataIndex: "total_speech_sec",
              width: 110,
              render: (val: number) => `${val.toFixed(1)}s`,
            },
          ]}
        />
        </PanelState>
      </Card>
      </div>
    </div>
  );
}
