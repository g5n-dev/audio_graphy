/**
 * Graph Explorer page — AntV G6 v5 knowledge graph visualization.
 *
 * Features:
 * - Full graph explore with node type / min degree filters
 * - Click node to see entity detail panel
 * - Subgraph extraction (N-hop from selected entity)
 * - Force-directed layout
 */

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Card,
  Input,
  Select,
  InputNumber,
  Button,
  Typography,
  Spin,
  Empty,
  Descriptions,
  Tag,
  Space,
} from "@arco-design/web-react";
import { Graph } from "@antv/g6";
import { exploreGraph, getEntity } from "@/api/services";
import type { GraphNodeResponse, GraphEdgeResponse } from "@/types/api";

const { Title, Text } = Typography;

// Node type → color mapping
const NODE_TYPE_COLORS: Record<string, string> = {
  产品: "#1660bd",
  品牌: "#7b39ee",
  客户: "#e8590c",
  竞品: "#e03131",
  坐席: "#2f9e44",
  门店: "#1098ad",
  问题: "#f08c00",
  未知: "#868e96",
};

export default function GraphExplorerPage() {
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<Graph | null>(null);

  const [nodeType, setNodeType] = useState<string>("");
  const [minDegree, setMinDegree] = useState(0);
  const [limit, setLimit] = useState(200);
  const [selectedEntity, setSelectedEntity] = useState<string>("");

  // Fetch graph data
  const { data: graphData, isLoading } = useQuery({
    queryKey: ["graph", "explore", nodeType, minDegree, limit],
    queryFn: () =>
      exploreGraph({
        node_type: nodeType || undefined,
        min_degree: minDegree,
        limit,
      }),
  });

  // Fetch entity detail when selected
  const { data: entityDetail } = useQuery({
    queryKey: ["entity", selectedEntity],
    queryFn: () => getEntity(selectedEntity),
    enabled: !!selectedEntity,
  });

  // Initialize G6 graph
  useEffect(() => {
    if (!containerRef.current) return;

    const width = containerRef.current.clientWidth;
    const height = 600;

    const graph = new Graph({
      container: containerRef.current,
      width,
      height,
      layout: {
        type: "force",
        linkDistance: 80,
        nodeStrength: -50,
        edgeStrength: 0.1,
        preventOverlap: true,
        nodeSize: 30,
      },
      node: {
        style: {
          size: (d: { data?: { degree?: number } }) => {
            const deg = d.data?.degree ?? 1;
            return Math.min(40, Math.max(16, 16 + deg * 2));
          },
          fill: (d: { data?: { type?: string } }) =>
            NODE_TYPE_COLORS[d.data?.type ?? "未知"] ?? "#868e96",
          labelText: (d: { data?: { label?: string } }) => d.data?.label ?? "",
          labelFontSize: 10,
          labelPlacement: "bottom" as const,
        },
      },
      edge: {
        style: {
          stroke: "#c0c4cc",
          lineWidth: 1,
          endArrow: true,
          labelText: (d: { data?: { relation?: string } }) =>
            d.data?.relation ?? "",
          labelFontSize: 8,
          labelFill: "#86909c",
        },
      },
      behaviors: [
        "drag-canvas",
        "zoom-canvas",
        "drag-element",
        "click-select",
      ],
    });

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    graph.on("node:click", (evt: any) => {
      const nodeId = evt?.target?.id;
      if (nodeId) {
        setSelectedEntity(String(nodeId));
      }
    });

    graphRef.current = graph;

    return () => {
      graph.destroy();
      graphRef.current = null;
    };
  }, []);

  // Update graph data when fetched
  useEffect(() => {
    if (!graphRef.current || !graphData) return;

    const nodes = graphData.nodes.map((n: GraphNodeResponse) => ({
      id: n.id,
      data: {
        label: n.label,
        type: n.type,
        degree: n.degree,
        description: n.description,
      },
    }));

    const edges = graphData.edges.map((e: GraphEdgeResponse, i: number) => ({
      id: `edge-${i}`,
      source: e.source,
      target: e.target,
      data: {
        relation: e.relation,
        weight: e.weight,
      },
    }));

    graphRef.current.setData({ nodes, edges });
    graphRef.current.render();
  }, [graphData]);

  return (
    <div style={{ padding: 24 }}>
      <Title heading={4} style={{ marginBottom: 16 }}>
        知识图谱探索
      </Title>

      <Card style={{ marginBottom: 16 }}>
        <Space size="medium">
          <Input
            placeholder="节点类型筛选"
            value={nodeType}
            onChange={setNodeType}
            style={{ width: 150 }}
            allowClear
          />
          <Select
            placeholder="节点类型"
            value={nodeType || undefined}
            onChange={(v) => setNodeType(v ?? "")}
            style={{ width: 120 }}
            allowClear
          >
            {Object.entries(NODE_TYPE_COLORS).map(([type, color]) => (
              <Select.Option key={type} value={type}>
                <Tag color={undefined} style={{ background: color, color: "#fff", border: "none" }}>
                  {type}
                </Tag>
              </Select.Option>
            ))}
          </Select>
          <InputNumber
            placeholder="最小度数"
            value={minDegree}
            onChange={(v) => setMinDegree(Number(v ?? 0))}
            min={0}
            style={{ width: 120 }}
          />
          <InputNumber
            placeholder="节点上限"
            value={limit}
            onChange={(v) => setLimit(Number(v ?? 200))}
            min={1}
            max={2000}
            style={{ width: 120 }}
          />
        </Space>
      </Card>

      <div style={{ display: "flex", gap: 16 }}>
        <Card style={{ flex: 1, minWidth: 0 }} bodyStyle={{ padding: 0 }}>
          <div
            ref={containerRef}
            style={{
              width: "100%",
              height: 600,
              background: "#fafbfc",
              borderRadius: 4,
            }}
          >
            {isLoading && !graphData && (
              <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100%" }}>
                <Spin size={40} />
              </div>
            )}
            {!isLoading && graphData && graphData.nodes.length === 0 && (
              <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100%" }}>
                <Empty description="暂无图谱数据" />
              </div>
            )}
          </div>
          {graphData && (
            <div style={{ padding: "8px 16px", borderTop: "1px solid #e5e6eb", fontSize: 12, color: "#86909c" }}>
              {graphData.total_nodes} 节点 · {graphData.total_edges} 边
            </div>
          )}
        </Card>

        {selectedEntity && entityDetail && (
          <Card
            title="实体详情"
            style={{ width: 320 }}
            extra={
              <Button size="mini" onClick={() => setSelectedEntity("")}>
                关闭
              </Button>
            }
          >
            <Descriptions
              column={1}
              data={[
                { label: "名称", value: entityDetail.node.label },
                {
                  label: "类型",
                  value: (
                    <Tag style={{ background: NODE_TYPE_COLORS[entityDetail.node.type] ?? "#868e96", color: "#fff", border: "none" }}>
                      {entityDetail.node.type}
                    </Tag>
                  ),
                },
                { label: "度数", value: entityDetail.node.degree },
                { label: "描述", value: entityDetail.node.description || "-" },
                {
                  label: "关联实体",
                  value: `${entityDetail.neighbors.length} 个`,
                },
              ]}
            />
            {entityDetail.neighbors.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <Text style={{ fontSize: 12, color: "#86909c" }}>邻居节点:</Text>
                <div style={{ marginTop: 4, display: "flex", flexWrap: "wrap", gap: 4 }}>
                  {entityDetail.neighbors.slice(0, 10).map((n) => (
                    <Tag
                      key={n.id}
                      style={{ cursor: "pointer" }}
                      onClick={() => setSelectedEntity(n.id)}
                    >
                      {n.label}
                    </Tag>
                  ))}
                </div>
              </div>
            )}
          </Card>
        )}
      </div>
    </div>
  );
}
