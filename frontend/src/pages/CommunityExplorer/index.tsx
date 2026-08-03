import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Button,
  Input,
  Pagination,
  Select,
  Spin,
  Tag,
  Typography,
} from "@arco-design/web-react";
import { IconCheckCircle } from "@arco-design/web-react/icon";
import {
  getTopicClusters,
  type TopicCluster,
} from "@/api/advancedGraph";
import { PanelState } from "@/components/PanelState";
import { getErrorCode } from "@/utils/errors";
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
/**
 * Communities drawn per page.
 *
 * The radial canvas only reads as a map while the ring stays sparse, so the
 * projection is bounded — but the bound is a page, not a cut: every community
 * the KPI counts is reachable through the pager below the canvas.
 */
export const TOPIC_CLUSTER_PAGE_SIZE = 8;

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

export default function CommunityExplorerPage() {
  const [searchParams] = useSearchParams();
  const [level, setLevel] = useState(0);
  const [selectedJobId, setSelectedJobId] = useState<number>();
  const [search, setSearch] = useState("");
  const [selectedClusterKey, setSelectedClusterKey] = useState("");
  const [page, setPage] = useState(1);
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

  // A summary that has not been generated yet is not a failed request: the
  // server is reporting that this job/level pair currently holds no clusters,
  // which is this panel's empty state rather than something to retry into.
  const summaryPending = isError && getErrorCode(error) === "SUMMARY_NOT_READY";
  const resultsError = isError && !summaryPending ? error : null;

  // The API filters the complete job-bound snapshot before this bounded
  // visual projection.  Local filtering here would search only the first
  // rendered communities and could hide valid server-side matches.
  const clusterCount = data?.clusters.length ?? 0;
  const pageCount = Math.max(1, Math.ceil(clusterCount / TOPIC_CLUSTER_PAGE_SIZE));
  // Clamped rather than reset: a refetch that returns a shorter snapshot must
  // not strand the user on a page that no longer exists.
  const currentPage = Math.min(page, pageCount);
  const pageOffset = (currentPage - 1) * TOPIC_CLUSTER_PAGE_SIZE;
  const visibleClusters = useMemo(
    () =>
      data?.clusters.slice(pageOffset, pageOffset + TOPIC_CLUSTER_PAGE_SIZE) ??
      [],
    [data, pageOffset],
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

  // GD-005 / GD-008: consume focus_community URL param.  GraphExplorerPage
  // routes such a link to this tab, so the community may sit on any page of
  // the projection — resolve it against the whole snapshot and page to it.
  useEffect(() => {
    const focusCommunity = searchParams.get("focus_community");
    if (!focusCommunity || !data || focusCommunityConsumed.current) return;
    // Matched on community id alone: an incoming link carries no level, so
    // assuming level 0 silently failed for anyone arriving on another level.
    const focusIndex = data.clusters.findIndex(
      (cluster) => String(cluster.community_id) === focusCommunity,
    );
    if (focusIndex < 0) return;
    focusCommunityConsumed.current = true;
    setPage(Math.floor(focusIndex / TOPIC_CLUSTER_PAGE_SIZE) + 1);
    setSelectedClusterKey(clusterKey(data.clusters[focusIndex]));
    // Scroll the detail panel into view after a tick
    window.setTimeout(() => {
      const detailPanel = document.querySelector(
        ".ag-topic-cluster-detail",
      ) as HTMLElement | null;
      detailPanel?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }, 150);
  }, [searchParams, data]);

  const selectLevel = (nextLevel: number) => {
    if (data?.job.id && selectedJobId === undefined) {
      setSelectedJobId(data.job.id);
    }
    setLevel(nextLevel);
    setPage(1);
  };

  const emptyCopy = summaryPending
    ? {
        title: "该层级摘要尚未就绪",
        description:
          "系统正在生成与该 Leiden 任务精确绑定的摘要，不会使用当前图谱重建旧任务结果。",
      }
    : debouncedSearch
      ? {
          title: "没有匹配的主题或实体",
          description: "可调整关键词，或切换聚类层级与任务快照后再试。",
        }
      : {
          title: "当前成功任务尚未生成该层级的主题摘要",
          description: "可切换其他聚类层级，或选择另一个成功任务快照。",
        };

  return (
    <section className="ag-topic-cluster-panel">
      {/* The toolbar stays mounted through every result state: it carries the
          only controls that can move off a level whose summary failed. */}
      <header className="ag-topic-cluster-toolbar">
        <div className="ag-topic-cluster-toolbar__filters">
          <Input.Search
            aria-label="搜索主题或成员"
            value={search}
            onChange={(value) => {
              setSearch(value);
              setPage(1);
            }}
            placeholder="搜索主题、实体或业务关键词"
            allowClear
          />
          <Select
            aria-label="选择 Leiden 聚类任务"
            value={selectedJobId ?? data?.job.id}
            loading={isLoading}
            onChange={(value) => {
              setSelectedJobId(Number(value));
              setPage(1);
            }}
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
          <Button
            size="small"
            loading={isFetching}
            onClick={() => void refetch()}
          >
            刷新
          </Button>
        </div>
        {data && (
          <div className="ag-topic-cluster-toolbar__meta">
            <Tag color="arcoblue">Leiden #{data.job.id}</Tag>
            <span>成功任务快照</span>
          </div>
        )}
      </header>

      <Spin
        loading={isFetching && !isLoading}
        className="ag-topic-cluster-spin"
      >
        <PanelState
          pending={isLoading}
          error={resultsError}
          empty={summaryPending || visibleClusters.length === 0}
          emptyTitle={emptyCopy.title}
          emptyDescription={emptyCopy.description}
          onRetry={() => void refetch()}
          pendingLabel="正在加载主题聚类…"
        >
          {data && (
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
                              {String(pageOffset + index + 1).padStart(2, "0")}
                            </span>
                            <span>
                              <strong>{cluster.title}</strong>
                              <small>{cluster.member_count} 个实体节点</small>
                            </span>
                          </button>
                          <div className="ag-topic-cluster-members">
                            {cluster.member_node_ids
                              .slice(0, 6)
                              .map((member) => (
                                <span key={member}>{member}</span>
                              ))}
                            {cluster.member_node_ids.length > 6 && (
                              <span>
                                +{cluster.member_node_ids.length - 6}
                              </span>
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
                    </>
                  )}
                </aside>
              </div>

              {/* Only shown once the snapshot exceeds one page: below that the
                  canvas already draws every community the KPI counts. */}
              {pageCount > 1 && (
              <nav
                className="ag-topic-cluster-pager"
                aria-label="主题社区分页"
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  flexWrap: "wrap",
                  gap: 12,
                  marginTop: 12,
                }}
              >
                <Text type="secondary">
                  已展示第 {pageOffset + 1}–{pageOffset + visibleClusters.length}{" "}
                  个社区 · 共 {clusterCount} 个
                </Text>
                <Pagination
                  size="small"
                  current={currentPage}
                  pageSize={TOPIC_CLUSTER_PAGE_SIZE}
                  total={clusterCount}
                  onChange={(nextPage) => setPage(nextPage)}
                />
              </nav>
              )}
            </>
          )}
        </PanelState>
      </Spin>
    </section>
  );
}
