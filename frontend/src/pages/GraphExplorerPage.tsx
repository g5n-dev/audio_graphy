/**
 * Graph Explorer page — AntV G6 v5 knowledge graph visualization.
 *
 * Features:
 * - Full graph explore with node type / min degree filters
 * - Click node to see entity detail panel
 * - Subgraph extraction (N-hop from selected entity)
 * - Bounded force layout with grid fallback for dense result sets
 */

import { useEffect, useMemo, useRef, useState } from "react";
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
import {
  createBoundedGraphLayout,
  useDebouncedValue,
} from "./graphExplorerPerformance";

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
  const graphRenderedRef = useRef(false);
  const renderVersionRef = useRef(0);

  const [nodeType, setNodeType] = useState<string>("");
  const [minDegree, setMinDegree] = useState(0);
  const [limit, setLimit] = useState(200);
  const [selectedEntity, setSelectedEntity] = useState<string>("");
  const [renderStatus, setRenderStatus] = useState<
    "idle" | "rendering" | "ready" | "error"
  >("idle");
  const [renderAttempt, setRenderAttempt] = useState(0);
  const pendingFilters = useMemo(
    () => ({ nodeType, minDegree, limit }),
    [limit, minDegree, nodeType],
  );
  const filters = useDebouncedValue(pendingFilters);

  // Fetch graph data
  const {
    data: graphData,
    isError: isGraphQueryError,
    isFetching: isGraphQueryFetching,
    isLoading,
    refetch: refetchGraph,
  } = useQuery({
    queryKey: [
      "graph",
      "explore",
      filters.nodeType,
      filters.minDegree,
      filters.limit,
    ],
    queryFn: () =>
      exploreGraph({
        node_type: filters.nodeType || undefined,
        min_degree: filters.minDegree,
        limit: filters.limit,
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

    const container = containerRef.current;
    const width = Math.max(1, container.clientWidth);
    const height = Math.max(1, container.clientHeight || 600);

    const graph = new Graph({
      container,
      width,
      height,
      autoResize: false,
      layout: createBoundedGraphLayout(0),
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
    const handleNodeClick = (evt: any) => {
      const nodeId = evt?.target?.id;
      if (nodeId) {
        setSelectedEntity(String(nodeId));
      }
    };
    graph.on("node:click", handleNodeClick);

    graphRef.current = graph;
    graphRenderedRef.current = false;
    let active = true;
    let lastWidth = width;
    let lastHeight = height;
    const resizeObserver = new ResizeObserver((entries) => {
      const entry = entries.find((candidate) => candidate.target === container);
      if (!active || !entry) return;

      const nextWidth = Math.round(
        entry.contentRect.width || container.clientWidth,
      );
      const nextHeight = Math.round(
        entry.contentRect.height || container.clientHeight || 600,
      );
      if (
        nextWidth <= 0 ||
        nextHeight <= 0 ||
        (nextWidth === lastWidth && nextHeight === lastHeight)
      ) {
        return;
      }

      lastWidth = nextWidth;
      lastHeight = nextHeight;
      // setSize also persists the dimensions in G6's options. ResizeObserver
      // can fire before the first data response initializes the canvas, when
      // resize() alone is a no-op and the identical follow-up entry is
      // intentionally deduplicated.
      graph.setSize(nextWidth, nextHeight);
    });
    resizeObserver.observe(container);

    return () => {
      active = false;
      renderVersionRef.current += 1;
      resizeObserver.disconnect();
      graph.off("node:click", handleNodeClick);
      graph.destroy();
      if (graphRef.current === graph) {
        graphRef.current = null;
        graphRenderedRef.current = false;
      }
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

    const graph = graphRef.current;
    const renderVersion = renderVersionRef.current + 1;
    renderVersionRef.current = renderVersion;

    if (graphRenderedRef.current) {
      graph.stopLayout();
      graphRenderedRef.current = false;
    }

    graph.setLayout(createBoundedGraphLayout(nodes.length));
    graph.setData({ nodes, edges });
    setRenderStatus("rendering");

    // React StrictMode intentionally mounts, runs effects, and immediately
    // cleans up a probe instance before mounting the real one. Deferring the
    // first G6 render by one task lets that cleanup cancel the probe before G6
    // starts its asynchronous layout pipeline. This avoids a destroyed graph
    // reporting an error when cached query data is available synchronously.
    const renderStartTimer = window.setTimeout(() => {
      if (
        graphRef.current !== graph ||
        renderVersionRef.current !== renderVersion
      ) {
        return;
      }

      void (async () => {
        try {
          await graph.render();
          if (
            graphRef.current !== graph ||
            renderVersionRef.current !== renderVersion
          ) {
            return;
          }
          graphRenderedRef.current = true;
          setRenderStatus("ready");
        } catch {
          if (
            graphRef.current !== graph ||
            renderVersionRef.current !== renderVersion
          ) {
            return;
          }
          graphRenderedRef.current = false;
          setRenderStatus("error");
        }
      })();
    }, 0);

    return () => {
      window.clearTimeout(renderStartTimer);
    };
  }, [graphData, renderAttempt]);

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
            data-testid="graph-explorer-canvas"
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
            {isGraphQueryError && (
              <div
                role="alert"
                style={{
                  display: "flex",
                  flexDirection: "column",
                  justifyContent: "center",
                  alignItems: "center",
                  gap: 12,
                  height: "100%",
                  padding: 24,
                  textAlign: "center",
                }}
              >
                <Text style={{ fontWeight: 600 }}>图谱数据加载失败</Text>
                <Text style={{ color: "#86909c" }}>
                  无法获取图谱数据，请检查网络后重试。
                </Text>
                <Button
                  type="primary"
                  loading={isGraphQueryFetching}
                  onClick={() => void refetchGraph()}
                >
                  重新加载
                </Button>
              </div>
            )}
            {!isGraphQueryError && renderStatus === "error" && (
              <div
                role="alert"
                style={{
                  display: "flex",
                  flexDirection: "column",
                  justifyContent: "center",
                  alignItems: "center",
                  gap: 12,
                  height: "100%",
                  padding: 24,
                  textAlign: "center",
                }}
              >
                <Text style={{ fontWeight: 600 }}>图谱渲染失败</Text>
                <Text style={{ color: "#86909c" }}>
                  画布初始化未完成，可重新尝试渲染。
                </Text>
                <Button
                  type="primary"
                  onClick={() => setRenderAttempt((attempt) => attempt + 1)}
                >
                  重试渲染
                </Button>
              </div>
            )}
            {!isGraphQueryError &&
              renderStatus !== "error" &&
              !isLoading &&
              graphData &&
              graphData.nodes.length === 0 && (
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
