/**
 * Stats page — tag statistics dashboard with multi-dimensional grouping.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Card, Select, Table, Tag } from "@arco-design/web-react";
import { getStats } from "@/api/services";

const groupByOptions = [
  { label: "标签路径", value: "tag_path" },
  { label: "标签值", value: "tag_value" },
  { label: "门店", value: "store_id" },
  { label: "坐席", value: "agent_name" },
];

export default function StatsPage() {
  const [groupBy, setGroupBy] = useState("tag_path");

  const { data, isLoading } = useQuery({
    queryKey: ["stats", groupBy],
    queryFn: () => getStats({ group_by: groupBy }),
  });

  return (
    <div>
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">TAG ANALYTICS · 标签分析</span>
          <h1>标签统计</h1>
          <p>按标签路径、标签值、门店或坐席多维度聚合统计。</p>
        </div>
      </header>

      <div style={{ padding: 24 }}>
      <Card style={{ marginBottom: 16 }}>
        <Select
          value={groupBy}
          onChange={setGroupBy}
          style={{ width: 200 }}
        >
          {groupByOptions.map((opt) => (
            <Select.Option key={opt.value} value={opt.value}>
              {opt.label}
            </Select.Option>
          ))}
        </Select>
      </Card>

      <Card>
        <Table
          data={data?.items ?? []}
          rowKey="group_key"
          loading={isLoading}
          size="small"
          pagination={false}
          columns={[
            { title: groupByOptions.find((o) => o.value === groupBy)?.label ?? "分组", dataIndex: "group_key" },
            { title: "标签数", dataIndex: "tag_count", width: 100 },
            { title: "通过", dataIndex: "pass_count", width: 80 },
            { title: "失败", dataIndex: "fail_count", width: 80 },
            {
              title: "通过率",
              dataIndex: "pass_rate",
              width: 100,
              render: (val: number) => (
                <Tag color={val >= 0.8 ? "green" : val >= 0.5 ? "orange" : "red"}>
                  {(val * 100).toFixed(1)}%
                </Tag>
              ),
            },
          ]}
        />
        {data && (
          <div style={{ marginTop: 12, color: "#86909c", fontSize: 14 }}>
            共 {data.total_records} 条标签记录
          </div>
        )}
      </Card>
      </div>
    </div>
  );
}
