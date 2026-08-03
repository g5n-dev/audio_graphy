/**
 * Graph Explorer page — AntV G6 v5 knowledge graph visualization.
 *
 * Features:
 * - Full graph explore with node type / min degree filters
 * - Click node to see entity detail panel
 * - Subgraph extraction (N-hop from selected entity)
 * - Bounded force layout with grid fallback for dense result sets
 */

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  AutoComplete,
  Card,
  InputNumber,
  Button,
  Typography,
  Spin,
  Empty,
  Space,
} from "@arco-design/web-react";
import { Graph } from "@antv/g6";
import { exploreGraph, getEntity } from "@/api/services";
import type { GraphNodeResponse, GraphEdgeResponse } from "@/types/api";
import {
  createBoundedGraphLayout,
  useDebouncedValue,
} from "./graphExplorerPerformance";
import CommunityExplorerPage from "./CommunityExplorer";
import { buildFocusParam, parseFocusParam } from "@/utils/urlParams";
import "./graphExplorerPage.css";

const { Text } = Typography;
export const MAX_RENDERED_GRAPH_EDGES = 5_000;

// ── Node type → route mapping (P0 hard-coded, per Q2 lock) ──

interface NodeRouteEntry {
  /** Button label text. */
  label: string;
  /** Build the target URL from the node id (id is NOT encoded here —
   *  buildFocusParam handles encoding). */
  to: (id: string) => string;
}

const NODE_TYPE_ROUTE_MAP: Record<string, NodeRouteEntry> = {
  "产品": {
    label: "在图谱中聚焦",
    to: (id: string) => `/graph?focus=${buildFocusParam("产品", id)}`,
  },
  "品牌": {
    label: "在图谱中聚焦",
    to: (id: string) => `/graph?focus=${buildFocusParam("品牌", id)}`,
  },
  "竞品": {
    label: "在图谱中聚焦",
    to: (id: string) => `/graph?focus=${buildFocusParam("竞品", id)}`,
  },
  "客户": {
    label: "查看说话人画像",
    to: (id: string) => `/speakers?focus=${buildFocusParam("客户", id)}`,
  },
  "坐席": {
    label: "查看说话人画像",
    to: (id: string) => `/speakers?focus=${buildFocusParam("坐席", id)}`,
  },
  "录音": {
    label: "查看录音详情",
    to: (id: string) =>
      `/recordings/${encodeURIComponent(id)}?focus=${buildFocusParam(
        "录音",
        id,
      )}`,
  },
  "门店": {
    label: "查看接待中心",
    to: (id: string) => `/receptions?focus=${buildFocusParam("门店", id)}`,
  },
  // 问题 / 未知 — no mapping, button is not rendered (silent degradation).
};

// Node type → color ramp (fill light, stroke mid, text dark)
const NODE_TYPE_RAMPS: Record<
  string,
  { fill: string; stroke: string; text: string; label: string }
> = {
  产品: { fill: "#E6F1FB", stroke: "#378ADD", text: "#0C447C", label: "产品" },
  品牌: { fill: "#EEEDFE", stroke: "#7F77DD", text: "#3C3489", label: "品牌" },
  客户: { fill: "#FAECE7", stroke: "#D85A30", text: "#712B13", label: "客户" },
  竞品: { fill: "#FCEBEB", stroke: "#E24B4A", text: "#791F1F", label: "竞品" },
  坐席: { fill: "#EAF3DE", stroke: "#639922", text: "#27500A", label: "坐席" },
  门店: { fill: "#E1F5EE", stroke: "#1D9E75", text: "#085041", label: "门店" },
  问题: { fill: "#FAEEDA", stroke: "#EF9F27", text: "#633806", label: "问题" },
  未知: { fill: "#F1EFE8", stroke: "#888780", text: "#444441", label: "未知" },
};

// Node type ramps are the single source of truth for type → color mapping.
// They also seed the type filter suggestions; the backend accepts any type
// string, so the filter stays free-text with these as known shortcuts.

interface GraphServiceStatus {
  label: string;
  dot: string;
  halo: string;
}

/** Graph service status derived from the explore query — never a literal. */
function describeGraphService(
  hasError: boolean,
  hasData: boolean,
): GraphServiceStatus {
  if (hasError) {
    return {
      label: "图谱服务连接失败",
      dot: "#f53f3f",
      halo: "rgba(245, 63, 63, 0.14)",
    };
  }
  if (!hasData) {
    return {
      label: "图谱服务连接中…",
      dot: "#ff7d00",
      halo: "rgba(255, 125, 0, 0.14)",
    };
  }
  return {
    label: "图谱服务已连接",
    dot: "#27a66f",
    halo: "rgba(39, 166, 111, 0.11)",
  };
}

function EntityRelationshipPanel() {
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<Graph | null>(null);
  const graphRenderedRef = useRef(false);
  const renderVersionRef = useRef(0);
  const selectedEntityRef = useRef<string | null>(null);
  const focusConsumedRef = useRef(false);
  const focusAlertTimer = useRef<number>();

  const [searchParams] = useSearchParams();
  const [nodeType, setNodeType] = useState<string>("");
  const [minDegree, setMinDegree] = useState(0);
  const [limit, setLimit] = useState(200);
  const [selectedEntity, setSelectedEntity] = useState<string>("");
  const [renderStatus, setRenderStatus] = useState<
    "idle" | "rendering" | "ready" | "error"
  >("idle");
  const [renderAttempt, setRenderAttempt] = useState(0);
  const [focusAlert, setFocusAlert] = useState<string | null>(null);
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
        edge_limit: MAX_RENDERED_GRAPH_EDGES,
      }),
  });

  const serviceStatus = describeGraphService(
    isGraphQueryError,
    Boolean(graphData),
  );

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
        type: "circle",
        state: {
          active: {
            fill: (d: { data?: { type?: string } }) =>
              NODE_TYPE_RAMPS[d.data?.type ?? "未知"]?.stroke ??
              "#888780",
            stroke: (d: { data?: { type?: string } }) =>
              NODE_TYPE_RAMPS[d.data?.type ?? "未知"]?.stroke ??
              "#888780",
            lineWidth: 2,
            shadowColor: (d: { data?: { type?: string } }) =>
              NODE_TYPE_RAMPS[d.data?.type ?? "未知"]?.stroke ?? "#888780",
            shadowBlur: 18,
          },
        },
        style: {
          size: (d: { data?: { degree?: number } }) => {
            const deg = d.data?.degree ?? 1;
            return Math.min(46, Math.max(22, 22 + deg * 2.4));
          },
          fill: (d: { data?: { type?: string } }) =>
            NODE_TYPE_RAMPS[d.data?.type ?? "未知"]?.fill ?? "#F1EFE8",
          stroke: (d: { data?: { type?: string } }) =>
            NODE_TYPE_RAMPS[d.data?.type ?? "未知"]?.stroke ?? "#888780",
          lineWidth: 2,
          shadowColor: "rgba(80, 127, 232, 0.18)",
          shadowBlur: 10,
          shadowOffsetX: 0,
          shadowOffsetY: 4,
          cursor: "pointer",
          labelText: (d: { data?: { label?: string } }) =>
            d.data?.label ?? "",
          labelFontSize: 14,
          labelFontWeight: 600,
          labelFill: (d: { data?: { type?: string } }) =>
            NODE_TYPE_RAMPS[d.data?.type ?? "未知"]?.text ?? "#444441",
          labelPlacement: "bottom" as const,
          labelBackground: true,
          labelBackgroundFill: "rgba(255, 255, 255, 0.92)",
          labelBackgroundRadius: 5,
          labelBackgroundLineWidth: 1,
          labelBackgroundStroke: "rgba(227, 234, 245, 0.7)",
          labelPadding: [4, 8],
        },
      },
      edge: {
        state: {
          active: {
            stroke: "#378ADD",
            lineWidth: 2,
            shadowColor: "rgba(55, 138, 221, 0.32)",
            shadowBlur: 8,
          },
        },
        style: {
          stroke: (d: { data?: { weight?: number } }) => {
            const w = d.data?.weight ?? 1;
            if (w >= 5) return "#7faedc";
            if (w >= 2) return "#a9c7e4";
            return "#c8d6e8";
          },
          lineWidth: (d: { data?: { weight?: number } }) => {
            const w = d.data?.weight ?? 1;
            return Math.min(3, Math.max(1, 0.8 + w * 0.4));
          },
          endArrow: true,
          endArrowSize: 6,
          endArrowFill: (d: { data?: { weight?: number } }) => {
            const w = d.data?.weight ?? 1;
            return w >= 2 ? "#7faedc" : "#c8d6e8";
          },
          labelText: (d: { data?: { relation?: string } }) =>
            d.data?.relation ?? "",
          labelFontSize: 12,
          labelFontWeight: 500,
          labelFill: "#5b6a87",
          labelBackground: true,
          labelBackgroundFill: "rgba(255, 255, 255, 0.88)",
          labelBackgroundRadius: 4,
          labelBackgroundLineWidth: 1,
          labelBackgroundStroke: "rgba(227, 234, 245, 0.55)",
          labelPadding: [2, 6],
        },
      },
      behaviors: [
        "drag-canvas",
        "zoom-canvas",
        "drag-element",
        "click-select",
        "hover-activate",
      ],
    });

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const handleNodeClick = (evt: any) => {
      const nodeId = evt?.target?.id;
      if (!nodeId) return;
      const id = String(nodeId);
      setSelectedEntity(id);
      selectedEntityRef.current = id;
      // Best-effort neighbor highlight — G6 v5 state API differs across builds.
      try {
        const related: string[] = [];
        const edgeData = (graph.getEdgeData?.() ?? []) as Array<{
          source: unknown;
          target: unknown;
        }>;
        for (const edge of edgeData) {
          const s = String(edge.source);
          const t = String(edge.target);
          if (s === id && !related.includes(t)) related.push(t);
          if (t === id && !related.includes(s)) related.push(s);
        }
        const targets = [id, ...related];
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (graph as any).setElementState?.(targets, "active");
      } catch {
        /* hover-activate behavior already handles neighbor highlight */
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

    const edges = graphData.edges
      .slice(0, MAX_RENDERED_GRAPH_EDGES)
      .map((e: GraphEdgeResponse, i: number) => ({
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

  // GD-008: consume focus URL param — highlight & select after graph loads
  useEffect(() => {
    const focusRaw = searchParams.get("focus");
    if (!focusRaw || !graphData || graphData.nodes.length === 0) return;
    if (focusConsumedRef.current) return;

    const parsed = parseFocusParam(focusRaw);
    if (!parsed) return;

    focusConsumedRef.current = true;

    const foundNode = graphData.nodes.find((n) => n.id === parsed.id);
    if (foundNode) {
      setSelectedEntity(foundNode.id);
      selectedEntityRef.current = foundNode.id;
      setFocusAlert(`已聚焦到 ${foundNode.label}`);

      // Best-effort highlight on graph once rendered
      const tryHighlight = () => {
        const g = graphRef.current;
        if (!g || !graphRenderedRef.current) return;
        try {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (g as any).setElementState?.([foundNode.id], "active");
        } catch {
          /* ignore */
        }
      };
      tryHighlight();
      // Retry once after a short delay for async render
      const retryTimer = window.setTimeout(tryHighlight, 400);

      clearTimeout(focusAlertTimer.current);
      focusAlertTimer.current = window.setTimeout(
        () => setFocusAlert(null),
        3000,
      );

      return () => {
        window.clearTimeout(retryTimer);
        clearTimeout(focusAlertTimer.current);
      };
    } else {
      setFocusAlert(
        `节点 "${parsed.id}" 不在当前筛选范围内，请调整筛选条件`,
      );
      clearTimeout(focusAlertTimer.current);
      focusAlertTimer.current = window.setTimeout(
        () => setFocusAlert(null),
        3000,
      );
      return () => clearTimeout(focusAlertTimer.current);
    }
  }, [searchParams, graphData]);

  return (
    <div className="ag-entity-graph-panel">
      <Card style={{ marginBottom: 16 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 16,
            flexWrap: "wrap",
          }}
        >
          <Space size="medium">
            {/* One control owns the node type filter: free text (the backend
                accepts any type string) with the known types as suggestions. */}
            <AutoComplete
              placeholder="节点类型筛选"
              inputProps={{ "aria-label": "节点类型筛选" }}
              value={nodeType}
              onChange={(value: string) => setNodeType(value ?? "")}
              style={{ width: 200 }}
              allowClear
            >
              {Object.entries(NODE_TYPE_RAMPS).map(([type, ramp]) => (
                <AutoComplete.Option key={type} value={type}>
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                      color: ramp.text,
                    }}
                  >
                    <span
                      style={{
                        display: "inline-block",
                        width: 10,
                        height: 10,
                        borderRadius: 3,
                        background: ramp.fill,
                        border: `1px solid ${ramp.stroke}`,
                      }}
                    />
                    {ramp.label}
                  </span>
                </AutoComplete.Option>
              ))}
            </AutoComplete>
            <InputNumber
              aria-label="最小节点度数"
              placeholder="最小度数"
              value={minDegree}
              onChange={(v) => setMinDegree(Number(v ?? 0))}
              min={0}
              style={{ width: 120 }}
            />
            <InputNumber
              aria-label="图谱节点上限"
              placeholder="节点上限"
              value={limit}
              onChange={(v) => setLimit(Number(v ?? 200))}
              min={1}
              max={2000}
              style={{ width: 120 }}
            />
          </Space>
          <span className="ag-global-graph-heading__status" role="status">
            <span
              aria-hidden="true"
              style={{
                background: serviceStatus.dot,
                boxShadow: `0 0 0 4px ${serviceStatus.halo}`,
              }}
            />
            {serviceStatus.label}
          </span>
        </div>
      </Card>

      {focusAlert && (
        <Alert
          type="info"
          closable
          content={focusAlert}
          className="ag-entity-graph-focus-alert"
          onClose={() => setFocusAlert(null)}
          style={{ marginBottom: 12 }}
        />
      )}

      <div style={{ display: "flex", gap: 16 }}>
        <Card style={{ flex: 1, minWidth: 0 }} bodyStyle={{ padding: 0 }}>
          <div
            ref={containerRef}
            data-testid="graph-explorer-canvas"
            className="ag-entity-graph-canvas"
            role="img"
            aria-label={
              graphData
                ? `实体关系图谱，共 ${graphData.total_nodes} 个节点、${graphData.total_edges} 条关系`
                : "实体关系图谱画布"
            }
          >
            <div className="ag-entity-graph-canvas__grid-fine" aria-hidden="true" />
            {graphData && (
              <div
                className="ag-entity-graph-canvas__meta"
                aria-live="polite"
              >
                <span aria-hidden="true" style={{ width: 8, height: 8, borderRadius: "50%", background: "#27a66f", boxShadow: "0 0 0 3px rgba(39, 166, 111, 0.18)" }} />
                实时拓扑 · <strong>{graphData.nodes.length.toLocaleString("zh-CN")}</strong> 节点 ·{" "}
                <strong>
                  {Math.min(
                    graphData.edges.length,
                    MAX_RENDERED_GRAPH_EDGES,
                  ).toLocaleString("zh-CN")}
                </strong>{" "}
                关系
              </div>
            )}
            {graphData && (
              <div
                className="ag-entity-graph-canvas__legend"
                aria-label="节点类型图例"
              >
                {Object.entries(NODE_TYPE_RAMPS).map(([type, ramp]) => (
                  <span key={type}>
                    <i style={{ background: ramp.fill, border: `1px solid ${ramp.stroke}` }} />
                    {ramp.label}
                  </span>
                ))}
              </div>
            )}
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
            <div style={{ padding: "10px 16px", borderTop: "1px solid #e5e6eb", fontSize: 14, color: "#86909c", background: "#fcfdff" }}>
              全图 {graphData.total_nodes.toLocaleString("zh-CN")} 节点 ·{" "}
              {graphData.total_edges.toLocaleString("zh-CN")} 边 · 当前筛选{" "}
              {graphData.nodes.length.toLocaleString("zh-CN")} 节点 ·{" "}
              {Math.min(
                graphData.edges.length,
                MAX_RENDERED_GRAPH_EDGES,
              ).toLocaleString("zh-CN")}{" "}
              关系
              {(graphData.edge_window.truncated ||
                graphData.edges.length > MAX_RENDERED_GRAPH_EDGES) &&
                ` · 当前画布展示 ${Math.min(
                  graphData.edges.length,
                  MAX_RENDERED_GRAPH_EDGES,
                ).toLocaleString("zh-CN")} / ${Math.max(
                  graphData.edge_window.total,
                  graphData.edges.length,
                ).toLocaleString("zh-CN")} 条筛选关系（服务端性能预算 ${Math.min(
                  graphData.edge_window.render_budget,
                  MAX_RENDERED_GRAPH_EDGES,
                ).toLocaleString("zh-CN")}，可通过筛选缩小范围）`}
            </div>
          )}
        </Card>

        {selectedEntity && entityDetail && (
          (() => {
            const ramp =
              NODE_TYPE_RAMPS[entityDetail.node.type] ?? NODE_TYPE_RAMPS["未知"];
            return (
              <Card
                className="ag-entity-detail-panel"
                style={{ width: 320 }}
                bodyStyle={{ padding: 18 }}
                extra={
                  <Button
                    size="mini"
                    onClick={() => {
                      setSelectedEntity("");
                      selectedEntityRef.current = null;
                    }}
                  >
                    关闭
                  </Button>
                }
              >
                <div className="ag-entity-detail-eyebrow">
                  ENTITY · {ramp.label.toUpperCase()}
                </div>
                <h3 className="ag-entity-detail-name">
                  {entityDetail.node.label}
                </h3>
                <p className="ag-entity-detail-desc">
                  {entityDetail.node.description || "暂无实体描述。"}
                </p>
                <div className="ag-entity-detail-stats">
                  <div>
                    <span>节点类型</span>
                    <span
                      className="ag-entity-detail-type-badge"
                      style={{
                        background: ramp.fill,
                        color: ramp.text,
                        border: `1px solid ${ramp.stroke}`,
                      }}
                    >
                      {ramp.label}
                    </span>
                  </div>
                  <div>
                    <span>关联度数</span>
                    <strong>{entityDetail.node.degree}</strong>
                  </div>
                </div>
                <div className="ag-entity-detail-neighbors">
                  <h4>
                    邻居节点 · {entityDetail.neighbors.length}
                  </h4>
                  {entityDetail.neighbors.length === 0 ? (
                    <Text style={{ color: "#86909c", fontSize: 14 }}>
                      此实体暂无直接邻居。
                    </Text>
                  ) : (
                    <div
                      style={{
                        display: "flex",
                        flexWrap: "wrap",
                        gap: 6,
                        marginTop: 6,
                      }}
                    >
                      {entityDetail.neighbors.slice(0, 14).map((n) => {
                        const nRamp = NODE_TYPE_RAMPS[n.type] ?? NODE_TYPE_RAMPS["未知"];
                        return (
                          <button
                            key={n.id}
                            type="button"
                            className="ag-entity-detail-neighbor"
                            onClick={() => setSelectedEntity(n.id)}
                            title={`${nRamp.label} · 关系 ${n.relation} · 权重 ${n.weight}`}
                            style={{ color: nRamp.text }}
                          >
                            <i style={{ background: nRamp.stroke }} />
                            <span
                              style={{
                                maxWidth: 110,
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                              }}
                            >
                              {n.label}
                            </span>
                          </button>
                        );
                      })}
                      {entityDetail.neighbors.length > 14 && (
                        <Text
                          style={{
                            color: "#86909c",
                            fontSize: 14,
                            alignSelf: "center",
                          }}
                        >
                          +{entityDetail.neighbors.length - 14}
                        </Text>
                      )}
                    </div>
                  )}
                </div>

                {/* GD-001: Jump-to button row — dynamic per node.type */}
                {NODE_TYPE_ROUTE_MAP[entityDetail.node.type] && (
                  <div className="ag-entity-detail-actions">
                    <Link
                      to={NODE_TYPE_ROUTE_MAP[entityDetail.node.type].to(
                        entityDetail.node.id,
                      )}
                      className="ag-entity-detail-action-link"
                    >
                      <Button type="text" size="small">
                        {NODE_TYPE_ROUTE_MAP[entityDetail.node.type].label} →
                      </Button>
                    </Link>
                  </div>
                )}
              </Card>
            );
          })()
        )}
      </div>
    </div>
  );
}

const GRAPH_TABS = [
  {
    id: "entities",
    label: "实体关系",
    description: "浏览实体、关系与邻居证据",
  },
  {
    id: "topics",
    label: "主题聚类",
    description: "查看成功 Leiden 任务的社区结构",
  },
] as const;

type GraphTabId = (typeof GRAPH_TABS)[number]["id"];

export default function GraphExplorerPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const topicClustersEnabled =
    import.meta.env.VITE_TOPIC_CLUSTERS_ENABLED !== "false";
  const availableTabs = topicClustersEnabled
    ? GRAPH_TABS
    : GRAPH_TABS.filter((tab) => tab.id === "entities");
  const requestedView = searchParams.get("view");
  const requestedTab: GraphTabId =
    topicClustersEnabled && requestedView === "clusters"
      ? "topics"
      : "entities";
  const [activeTab, setActiveTab] = useState<GraphTabId>(requestedTab);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    setActiveTab(requestedTab);
  }, [requestedTab]);

  // GD-008: focus_community param → auto-switch to topics tab
  useEffect(() => {
    const fc = searchParams.get("focus_community");
    if (fc && topicClustersEnabled && activeTab !== "topics") {
      setActiveTab("topics");
      const nextParams = new URLSearchParams(searchParams);
      nextParams.set("view", "clusters");
      setSearchParams(nextParams, { replace: true });
    }
    // Run once on mount only
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectTab = (index: number) => {
    const tab = availableTabs[index];
    if (!tab) return;
    setActiveTab(tab.id);
    const nextParams = new URLSearchParams(searchParams);
    if (tab.id === "topics") {
      nextParams.set("view", "clusters");
    } else {
      nextParams.delete("view");
    }
    setSearchParams(nextParams, { replace: true });
    window.requestAnimationFrame(() => tabRefs.current[index]?.focus());
  };

  const onTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    let nextIndex: number | undefined;
    if (event.key === "ArrowRight") {
      nextIndex = (index + 1) % availableTabs.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex =
        (index - 1 + availableTabs.length) % availableTabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = availableTabs.length - 1;
    }
    if (nextIndex === undefined) return;
    event.preventDefault();
    selectTab(nextIndex);
  };

  return (
    <main className="ag-global-graph-page">
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">GLOBAL KNOWLEDGE GRAPH · 全域图</span>
          <h1>全域知识图谱</h1>
          <p>在同一工作区切换实体关系与主题社区，保持筛选语义清晰且资源互不叠加。</p>
        </div>
        {/* The graph service status lives with the query that knows it — see
            EntityRelationshipPanel — so it can never contradict the canvas. */}
      </header>

      <div
        className="ag-global-graph-tabs"
        role="tablist"
        aria-label="全域知识图谱视图"
      >
        {availableTabs.map((tab, index) => {
          const selected = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              ref={(element) => {
                tabRefs.current[index] = element;
              }}
              id={`global-graph-tab-${tab.id}`}
              type="button"
              role="tab"
              aria-label={tab.label}
              aria-selected={selected}
              aria-controls={`global-graph-panel-${tab.id}`}
              tabIndex={selected ? 0 : -1}
              className={selected ? "is-active" : ""}
              onClick={() => selectTab(index)}
              onKeyDown={(event) => onTabKeyDown(event, index)}
            >
              <span>{tab.label}</span>
              <small>{tab.description}</small>
            </button>
          );
        })}
      </div>

      {activeTab === "entities" ? (
        <section
          id="global-graph-panel-entities"
          role="tabpanel"
          aria-labelledby="global-graph-tab-entities"
          tabIndex={0}
          className="ag-global-graph-panel"
        >
          <EntityRelationshipPanel />
        </section>
      ) : (
        <section
          id="global-graph-panel-topics"
          role="tabpanel"
          aria-labelledby="global-graph-tab-topics"
          tabIndex={0}
          className="ag-global-graph-panel"
        >
          <CommunityExplorerPage />
        </section>
      )}
    </main>
  );
}
