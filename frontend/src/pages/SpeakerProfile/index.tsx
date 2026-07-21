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
  Card,
  Select,
  Table,
  Typography,
} from "@arco-design/web-react";
import { useNavigate } from "react-router-dom";
import dayjs from "dayjs";
import { listSpeakers } from "@/api/speakers";
import { SpeakerBadge } from "@/components/SpeakerBadge";
import type { SpeakerListItem } from "@/types/api";

const { Title } = Typography;

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

export default function SpeakerProfileListPage(): JSX.Element {
  const navigate = useNavigate();
  const [roleFilter, setRoleFilter] = useState<string>(ROLE_ALL_SENTINEL);
  const [ambiguityFilter, setAmbiguityFilter] = useState<string>(
    AMBIGUITY_ALL_SENTINEL,
  );

  /**
   * Translate UI state → backend query param.
   * Empty string → undefined (no filter).
   */
  const roleParam:
    | "agent"
    | "customer"
    | "unknown"
    | undefined =
    roleFilter === ROLE_ALL_SENTINEL
      ? undefined
      : (roleFilter as "agent" | "customer" | "unknown");
  const ambiguityParam: AmbiguityFilterValue | undefined =
    ambiguityFilter === AMBIGUITY_ALL_SENTINEL
      ? undefined
      : (ambiguityFilter as AmbiguityFilterValue);

  const { data, isLoading } = useQuery({
    queryKey: ["speakers", roleParam, ambiguityParam],
    queryFn: () =>
      listSpeakers({
        speaker_role: roleParam,
        ambiguity: ambiguityParam,
        limit: 200,
      }),
  });

  return (
    <div style={{ padding: 24 }}>
      <Title heading={4} style={{ marginBottom: 16 }}>
        说话人管理
      </Title>

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
        <Table
          data={data?.items ?? []}
          rowKey="id"
          loading={isLoading}
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
                  />
                </span>
              ),
            },
            {
              title: "声纹Hash",
              dataIndex: "voiceprint_hash",
              width: 120,
              render: (val: string) => (
                <code style={{ fontSize: 12, color: "#86909c" }}>{val}</code>
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
                <span style={{ fontSize: 12 }}>{val}</span>
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
      </Card>
    </div>
  );
}
