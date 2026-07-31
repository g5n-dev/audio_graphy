import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Button,
  Empty,
  Input,
  Select,
  Spin,
  Tag,
  Typography,
} from "@arco-design/web-react";
import {
  IconCheckCircle,
  IconExclamationCircle,
} from "@arco-design/web-react/icon";
import {
  getTopicClusters,
  type TopicCluster,
} from "@/api/advancedGraph";
import { useDebouncedValue } from "@/pages/graphExplorerPerformance";
import "./communityExplorer.css";

const { Text } = Typography;

const CLUSTER_PALETTE = [
  { accent: "#2f6bda", tint: "#eef5ff", line: "#8cb8ff" },
  { accent: "#078b82", tint: "#eaf9f6", line: "#7fd6ca" },
  { accent: "#9b6500", tint: "#fff7e8", line: "#edbe68" },
  { accent: "#6f4bc8", tint: "#f4f0ff", line: "#b8a0ef" },
  { accent: "#bc3d69", tint: "#fff0f5", line: "#ee9db9" },
  { accent: "#267b50", tint: "#edf9f1", line: "#91d4ac" },
  { accent: "#b84a20", tint: "#fff1ec", line: "#f0a486" },
  { accent: "#3e6f91", tint: "#eef7fb", line: "#94c7dd" },
] as const;

const CANVAS_WIDTH = 1160;
const CANVAS_HEIGHT = 690;
const CENTER_X = CANVAS_WIDTH / 2;
const CENTER_Y = CANVAS_HEIGHT / 2;
export const TOPIC_CLUSTER_MEMBER_RENDER_LIMIT = 48;

function clusterPosition(index: number, total: number) {
  const safeTotal = Math.max(total, 1);
  const angle = -Math.PI / 2 + (index * Math.PI * 2) / safeTotal;
  const radiusX = safeTotal <= 2 ? 360 : 405;
  const radiusY = safeTotal <= 2 ? 205 : 245;
  return {
    x: CENTER_X + Math.cos(angle) * radiusX,
    y: CENTER_Y + Math.sin(angle) * radiusY,
  };
}

function connectionStyle(index: number, total: number) {
  const target = clusterPosition(index, total);
  const dx = target.x - CENTER_X;
  const dy = target.y - CENTER_Y;
  return {
    left: CENTER_X,
    top: CENTER_Y,
    width: Math.hypot(dx, dy),
    transform: `rotate(${Math.atan2(dy, dx)}rad)`,
  };
}

function clusterKey(cluster: TopicCluster) {
  return `${cluster.level}:${cluster.community_id}`;
}

function apiErrorCode(error: unknown): string | undefined {
  if (!error || typeof error !== "object" || !("response" in error)) {
    return undefined;
  }
  const response = error.response;
  if (!response || typeof response !== "object" || !("data" in response)) {
    return undefined;
  }
  const data = response.data;
  if (!data || typeof data !== "object" || !("error" in data)) {
    return undefined;
  }
  const envelope = data.error;
  if (!envelope || typeof envelope !== "object" || !("code" in envelope)) {
    return undefined;
  }
  return typeof envelope.code === "string" ? envelope.code : undefined;
}

export default function CommunityExplorerPage() {
  const [searchParams] = useSearchParams();
  const [level, setLevel] = useState(0);
  const [selectedJobId, setSelectedJobId] = useState<number>();
  const [search, setSearch] = useState("");
  const [selectedClusterKey, setSelectedClusterKey] = useState("");
  const focusCommunityConsumed = useRef(false);
  const debouncedSearch = useDebouncedValue(search.trim(), 250);

  const {
    data,
    isLoading,
    isFetching,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: [
      "graph",
      "topic-clusters",
      selectedJobId,
      level,
      debouncedSearch,
    ],
    queryFn: () =>
      getTopicClusters({
        job_id: selectedJobId,
        level,
        query: debouncedSearch || undefined,
      }),
    placeholderData: (previousData) => previousData,
  });

  // The API filters the complete job-bound snapshot before this bounded
  // visual projection.  Local filtering here would search only the first
  // rendered communities and could hide valid server-side matches.
  const visibleClusters = useMemo(
    () => data?.clusters.slice(0, 8) ?? [],
    [data],
  );

  const selectedCluster =
    visibleClusters.find(
      (cluster) => clusterKey(cluster) === selectedClusterKey,
    ) ?? visibleClusters[0];

  useEffect(() => {
    if (!data || selectedJobId !== undefined) return;
    // Bind every later level switch to the exact successful run resolved by
    // the first response, preventing a moving "latest" target mid-session.
    setSelectedJobId(data.job.id);
  }, [data, selectedJobId]);

  useEffect(() => {
    if (!selectedCluster) {
      setSelectedClusterKey("");
      return;
    }
    setSelectedClusterKey((current) =>
      visibleClusters.some((cluster) => clusterKey(cluster) === current)
        ? current
        : clusterKey(selectedCluster),
    );
  }, [selectedCluster, visibleClusters]);

  // GD-005 / GD-008: consume focus_community URL param
  useEffect(() => {
    const fc = searchParams.get("focus_community");
    if (!fc || !data || focusCommunityConsumed.current) return;
    const targetKey = `0:${fc}`; // Default level-0 for incoming community_id
    const foundCluster = data.clusters.find(
      (cluster) => clusterKey(cluster) === targetKey,
    );
    if (foundCluster) {
      focusCommunityConsumed.current = true;
      setSelectedClusterKey(targetKey);
      // Scroll the detail panel into view after a tick
      window.setTimeout(() => {
        const detailPanel = document.querySelector(
          ".ag-topic-cluster-detail",
        ) as HTMLElement | null;
        detailPanel?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }, 150);
    }
  }, [searchParams, data]);

  const selectLevel = (nextLevel: number) => {
    if (data?.job.id && selectedJobId === undefined) {
      setSelectedJobId(data.job.id);
    }
    setLevel(nextLevel);
  };

  if (isError) {
    const isSummaryPending = apiErrorCode(error) === "SUMMARY_NOT_READY";
    return (
      <section className="ag-topic-cluster-error" role="alert">
        <div className="ag-topic-cluster-error__icon" aria-hidden="true">
          <IconExclamationCircle />
        </div>
        <h2>
          {isSummaryPending
            ? "该层级摘要尚未就绪"
            : "主题聚类加载失败"}
        </h2>
        <p>
          {isSummaryPending
            ? "系统正在生成与该 Leiden 任务精确绑定的摘要，不会使用当前图谱重建旧任务结果。"
            : "无法读取已完成的聚类任务，请检查服务状态后重试。"}
        </p>
        <Button type="primary" loading={isFetching} onClick={() => void refetch()}>
          重新加载
        </Button>
      </section>
    );
  }

  return (
    <section className="ag-topic-cluster-panel">
      <header className="ag-topic-cluster-toolbar">
        <div className="ag-topic-cluster-toolbar__filters">
          <Input.Search
            aria-label="搜索主题或成员"
            value={search}
            onChange={setSearch}
            placeholder="搜索主题、实体或业务关键词"
            allowClear
          />
          <Select
            aria-label="选择 Leiden 聚类任务"
            value={selectedJobId ?? data?.job.id}
            loading={isLoading}
            onChange={(value) => setSelectedJobId(Number(value))}
            placeholder="选择成功任务"
          >
            {(data?.available_jobs ?? []).map((job) => (
              <Select.Option key={job.id} value={job.id}>
                任务 #{job.id} · {job.job_type === "full" ? "全量" : "增量"}
              </Select.Option>
            ))}
          </Select>
          <div className="ag-topic-cluster-levels" aria-label="聚类层级">
            {[0, 1, 2].map((item) => (
              <Button
                key={item}
                size="small"
                type={item === level ? "primary" : "secondary"}
                aria-pressed={item === level}
                onClick={() => selectLevel(item)}
              >
                层级 {item}
              </Button>
            ))}
          </div>
        </div>
        {data && (
          <div className="ag-topic-cluster-toolbar__meta">
            <Tag color="arcoblue">Leiden #{data.job.id}</Tag>
            <span>成功任务快照</span>
          </div>
        )}
      </header>

      <Spin loading={isLoading || isFetching} className="ag-topic-cluster-spin">
        {!data || visibleClusters.length === 0 ? (
          <div className="ag-topic-cluster-empty">
            <Empty
              description={
                search
                  ? "没有匹配的主题或实体"
                  : "当前成功任务尚未生成该层级的主题摘要"
              }
            />
          </div>
        ) : (
          <>
            <div className="ag-topic-cluster-kpis" aria-label="聚类概览">
              <div>
                <span>主题社区</span>
                <strong>{data.total_clusters}</strong>
              </div>
              <div>
                <span>覆盖实体</span>
                <strong>{data.total_members}</strong>
              </div>
              <div>
                <span>模块度</span>
                <strong>
                  {data.job.modularity === null
                    ? "—"
                    : data.job.modularity.toFixed(2)}
                </strong>
              </div>
              <div>
                <span>当前层级</span>
                <strong>L{data.level}</strong>
              </div>
            </div>

            <div className="ag-topic-cluster-layout">
              <div className="ag-topic-cluster-scroll">
                <div
                  className="ag-topic-cluster-canvas"
                  data-testid="topic-cluster-canvas"
                  style={{
                    width: CANVAS_WIDTH,
                    height: CANVAS_HEIGHT,
                  }}
                >
                  <div className="ag-topic-cluster-grid" aria-hidden="true" />
                  {visibleClusters.map((cluster, index) => {
                    const palette =
                      CLUSTER_PALETTE[index % CLUSTER_PALETTE.length];
                    return (
                      <div
                        key={`link-${clusterKey(cluster)}`}
                        className="ag-topic-cluster-link"
                        style={{
                          ...connectionStyle(index, visibleClusters.length),
                          background: `linear-gradient(90deg, #7ba9ff, ${palette.line})`,
                        }}
                        aria-hidden="true"
                      />
                    );
                  })}

                  <div
                    className="ag-topic-cluster-core"
                    style={{ left: CENTER_X, top: CENTER_Y }}
                  >
                    <span>TOPIC MAP</span>
                    <strong>主题聚类</strong>
                    <small>
                      {data.total_clusters} 社区 · {data.total_members} 实体
                    </small>
                  </div>

                  {visibleClusters.map((cluster, index) => {
                    const palette =
                      CLUSTER_PALETTE[index % CLUSTER_PALETTE.length];
                    const position = clusterPosition(
                      index,
                      visibleClusters.length,
                    );
                    const active =
                      selectedCluster &&
                      clusterKey(cluster) === clusterKey(selectedCluster);
                    return (
                      <article
                        key={clusterKey(cluster)}
                        className={`ag-topic-cluster-bubble${active ? " is-active" : ""}`}
                        style={{
                          left: position.x,
                          top: position.y,
                          "--cluster-accent": palette.accent,
                          "--cluster-tint": palette.tint,
                          "--cluster-line": palette.line,
                        } as React.CSSProperties}
                      >
                        <button
                          type="button"
                          className="ag-topic-cluster-bubble__heading"
                          aria-pressed={active}
                          onClick={() =>
                            setSelectedClusterKey(clusterKey(cluster))
                          }
                        >
                          <span className="ag-topic-cluster-bubble__hub">
                            {String(index + 1).padStart(2, "0")}
                          </span>
                          <span>
                            <strong>{cluster.title}</strong>
                            <small>{cluster.member_count} 个实体节点</small>
                          </span>
                        </button>
                        <div className="ag-topic-cluster-members">
                          {cluster.member_node_ids.slice(0, 6).map((member) => (
                            <span key={member}>{member}</span>
                          ))}
                          {cluster.member_node_ids.length > 6 && (
                            <span>+{cluster.member_node_ids.length - 6}</span>
                          )}
                        </div>
                      </article>
                    );
                  })}
                </div>
              </div>

              <aside className="ag-topic-cluster-detail" aria-live="polite">
                {selectedCluster && (
                  <>
                    <div className="ag-topic-cluster-detail__eyebrow">
                      社区 #{selectedCluster.community_id} · L
                      {selectedCluster.level}
                    </div>
                    <h3>{selectedCluster.title}</h3>
                    <Text>{selectedCluster.summary}</Text>
                    <div className="ag-topic-cluster-detail__stats">
                      <span>
                        <strong>{selectedCluster.member_count}</strong>
                        实体节点
                      </span>
                      <span>
                        <strong>{data.job.id}</strong>
                        来源任务
                      </span>
                    </div>
                    <h4>核心成员</h4>
                    <div className="ag-topic-cluster-detail__members">
                      {selectedCluster.member_node_ids
                        .slice(0, TOPIC_CLUSTER_MEMBER_RENDER_LIMIT)
                        .map((member) => (
                          <Tag key={member}>{member}</Tag>
                        ))}
                      {selectedCluster.member_node_ids.length >
                        TOPIC_CLUSTER_MEMBER_RENDER_LIMIT && (
                        <Tag>
                          为保持交互流畅，仅展示前{" "}
                          {TOPIC_CLUSTER_MEMBER_RENDER_LIMIT} 个 · 另有{" "}
                          {selectedCluster.member_node_ids.length -
                            TOPIC_CLUSTER_MEMBER_RENDER_LIMIT} 个成员
                        </Tag>
                      )}
                    </div>
                    <div className="ag-topic-cluster-detail__binding">
                      <IconCheckCircle aria-hidden="true" />
                      数据已锁定到成功任务，不混用其他分区结果
                    </div>

                    {/* GD-005: jump to graph with community focus */}
                    <div className="ag-topic-cluster-detail__graph-link">
                      <Link
                        to={`/graph?focus_community=${encodeURIComponent(
                          String(selectedCluster.community_id),
                        )}&view=clusters`}
                      >
                        <Button type="text" size="small">
                          在图谱中查看此社区 →
                        </Button>
                      </Link>
                    </div>
                  </>
                )}
              </aside>
            </div>
          </>
        )}
      </Spin>
    </section>
  );
}
