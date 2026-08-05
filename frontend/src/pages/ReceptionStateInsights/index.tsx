import { useQuery } from "@tanstack/react-query";
import {
  IconBranch,
  IconClose,
  IconFullscreen,
  IconRefresh,
  IconZoomIn,
  IconZoomOut,
} from "@arco-design/web-react/icon";
import {
  FormEvent,
  KeyboardEvent,
  useEffect,
  useMemo,
  useState,
} from "react";
import { Link } from "react-router-dom";
import { getReceptionStateInsights } from "@/api/services";
import { formatPercent } from "@/components/dialogue/format";
import { InsightContextTabs } from "@/components/navigation/ContextNavigation";
import type {
  ReceptionScenario,
  ReceptionStateInsightsRequest,
  ReceptionStateStageInsight,
  ReceptionStateTransitionInsight,
} from "@/types/api";
import "./stateInsights.css";


interface FilterDraft {
  storeIds: string;
  agentNames: string;
  scenario: "" | ReceptionScenario;
  startedFrom: string;
  startedTo: string;
}

type GraphSelection =
  | { kind: "transition"; transition: ReceptionStateTransitionInsight }
  | { kind: "stage"; stage: ReceptionStateStageInsight };

interface PathGeometry {
  d: string;
  labelX: number;
  labelY: number;
}

const EMPTY_FILTERS: FilterDraft = {
  storeIds: "",
  agentNames: "",
  scenario: "",
  startedFrom: "",
  startedTo: "",
};

const STAGE_COLORS = [
  "blue",
  "cyan",
  "indigo",
  "amber",
  "green",
  "violet",
];
const MAX_VISUAL_STAGES = 6;
const MIN_STAGE_GAP = 158;
const STAGE_START_X = 104;
const CORE_Y = 360;

function splitCsv(value: string): string[] | undefined {
  const values = value
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
  return values.length > 0 ? [...new Set(values)] : undefined;
}

function optionalIsoDate(value: string): string | undefined {
  if (!value) return undefined;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    throw new Error("时间筛选格式无效。");
  }
  return new Date(timestamp).toISOString();
}

function toRequest(draft: FilterDraft): ReceptionStateInsightsRequest {
  const storeIds = splitCsv(draft.storeIds);
  const agentNames = splitCsv(draft.agentNames);
  if ((storeIds?.length ?? 0) > 50) {
    throw new Error("门店 ID 最多支持 50 项。");
  }
  if ((agentNames?.length ?? 0) > 50) {
    throw new Error("销售姓名最多支持 50 项。");
  }
  const startedFrom = optionalIsoDate(draft.startedFrom);
  const startedTo = optionalIsoDate(draft.startedTo);
  if (
    startedFrom &&
    startedTo &&
    Date.parse(startedTo) <= Date.parse(startedFrom)
  ) {
    throw new Error("结束时间必须晚于开始时间。");
  }
  return {
    ...(storeIds ? { store_id: storeIds } : {}),
    ...(agentNames ? { agent_name: agentNames } : {}),
    ...(draft.scenario ? { scenario: [draft.scenario] } : {}),
    ...(startedFrom ? { started_from: startedFrom } : {}),
    ...(startedTo ? { started_to: startedTo } : {}),
    transition_limit: 100,
  };
}

function pathTone(
  transition: ReceptionStateTransitionInsight,
): "danger" | "strong" | "normal" {
  const confidence = transition.average_confidence ?? 0;
  if (confidence < 0.5) return "danger";
  if (confidence >= 0.8) return "strong";
  return "normal";
}

function transitionGeometry(
  sourceIndex: number,
  targetIndex: number,
  gap: number,
  lane: number,
): PathGeometry {
  const sourceX = STAGE_START_X + sourceIndex * gap;
  const targetX = STAGE_START_X + targetIndex * gap;
  const span = Math.abs(sourceIndex - targetIndex);

  if (sourceIndex < targetIndex && span === 1) {
    return {
      d: `M ${sourceX + 79} ${CORE_Y} C ${sourceX + 103} ${CORE_Y} ${targetX - 98} ${CORE_Y} ${targetX - 76} ${CORE_Y}`,
      labelX: (sourceX + targetX) / 2,
      labelY: CORE_Y - 13,
    };
  }

  const arcY = Math.min(648, 532 + span * 18 + (lane % 3) * 18);
  const startX = sourceX + 60;
  const endX = targetX;
  return {
    d: `M ${startX} 486 C ${startX} ${arcY} ${endX} ${arcY} ${endX} 486`,
    labelX: (startX + endX) / 2,
    labelY: arcY + 16,
  };
}

function keyboardActivate(
  event: KeyboardEvent<SVGGElement>,
  action: () => void,
) {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    action();
  }
}

function averageOutgoingEvents(stage: ReceptionStateStageInsight): number {
  if (stage.reception_count <= 0) return 0;
  return stage.outgoing_count / stage.reception_count;
}

function markerForTone(tone: ReturnType<typeof pathTone>): string {
  if (tone === "danger") return "url(#ag-topology-arrow-danger)";
  if (tone === "strong") return "url(#ag-topology-arrow-strong)";
  return "url(#ag-topology-arrow-normal)";
}

function StagePill({
  label,
  y,
  tone,
}: {
  label: string;
  y: number;
  tone: "neutral" | "accent";
}) {
  const clipped = label.length > 8 ? `${label.slice(0, 8)}…` : label;
  return (
    <g
      className={`ag-topology-stage-pill is-${tone}`}
      transform={`translate(0 ${y})`}
    >
      <rect x="-58" y="-11" width="116" height="22" rx="6" />
      <text textAnchor="middle" y="4">
        {clipped}
      </text>
    </g>
  );
}

export default function ReceptionStateInsightsPage() {
  const [draft, setDraft] = useState<FilterDraft>(EMPTY_FILTERS);
  const [filters, setFilters] = useState<ReceptionStateInsightsRequest>({
    transition_limit: 100,
  });
  const [filterError, setFilterError] = useState<string | null>(null);
  const [selected, setSelected] = useState<GraphSelection | null>(null);
  const [zoom, setZoom] = useState(1);

  const query = useQuery({
    queryKey: ["reception-state-insights", filters],
    queryFn: () => getReceptionStateInsights(filters),
    retry: false,
  });

  const visualStages = useMemo(
    () => (query.data?.stages ?? []).slice(0, MAX_VISUAL_STAGES),
    [query.data?.stages],
  );
  const visualStageNames = useMemo(
    () => new Set(visualStages.map((stage) => stage.state)),
    [visualStages],
  );
  const visibleTransitions = useMemo(
    () =>
      (query.data?.transitions ?? []).filter(
        (transition) =>
          visualStageNames.has(transition.from_state) &&
          visualStageNames.has(transition.to_state),
      ),
    [query.data?.transitions, visualStageNames],
  );

  useEffect(() => {
    const firstKeyTransition = query.data?.transitions.find(
      (transition) =>
        visualStageNames.has(transition.from_state) &&
        visualStageNames.has(transition.to_state) &&
        (transition.average_confidence ?? 0) >= 0.8,
    );
    setSelected(
      firstKeyTransition
        ? { kind: "transition", transition: firstKeyTransition }
        : null,
    );
  }, [query.data, visualStageNames]);

  if (query.isPending) {
    return (
      <div className="ag-feature-loading" role="status">
        正在聚合接待状态路径…
      </div>
    );
  }

  if (query.isError || !query.data) {
    return (
      <div
        className="ag-feature-empty"
        role="alert"
        aria-label="聚合状态图暂不可用"
      >
        <h1>聚合状态图暂不可用</h1>
        <p>无法读取真实的跨接待状态转移数据。</p>
        <button type="button" onClick={() => query.refetch()}>
          重新加载
        </button>
      </div>
    );
  }

  const data = query.data;
  const graphWidth = Math.max(
    940,
    208 + Math.max(0, visualStages.length - 1) * MIN_STAGE_GAP,
  );
  const stageGap =
    visualStages.length <= 1
      ? 0
      : (graphWidth - 208) / (visualStages.length - 1);
  const stageIndex = new Map(
    visualStages.map((stage, index) => [stage.state, index]),
  );
  const orderedTransitions = [...visibleTransitions].sort(
    (left, right) =>
      Number(pathTone(left) === "strong") -
      Number(pathTone(right) === "strong"),
  );
  const keyPathShare =
    data.total_transitions > 0
      ? Math.min(
          1,
          data.transitions
            .filter(
              (transition) =>
                (transition.average_confidence ?? 0) >= 0.8,
            )
            .reduce((total, transition) => total + transition.count, 0) /
            data.total_transitions,
        )
      : 0;

  const selectedTransition =
    selected?.kind === "transition" ? selected.transition : null;
  const selectedStage = selected?.kind === "stage" ? selected.stage : null;
  const stageConnectedTransitions = selectedStage
    ? data.transitions.filter(
        (transition) =>
          transition.from_state === selectedStage.state ||
          transition.to_state === selectedStage.state,
      )
    : [];

  const submitFilters = (event: FormEvent) => {
    event.preventDefault();
    setFilterError(null);
    try {
      setSelected(null);
      setFilters(toRequest(draft));
    } catch (error) {
      setFilterError(
        error instanceof Error ? error.message : "筛选条件无效。",
      );
    }
  };

  const resetFilters = () => {
    setDraft(EMPTY_FILTERS);
    setFilters({ transition_limit: 100 });
    setFilterError(null);
    setSelected(null);
    setZoom(1);
  };

  return (
    <main className="ag-state-insights-page ag-state-topology">
      <InsightContextTabs />
      <header className="ag-state-insights-header">
        <div className="ag-topology-title">
          <span className="ag-eyebrow">CROSS-RECEPTION ANALYTICS · 对话洞察</span>
          <h1>跨接待状态流</h1>
          <p>沿阶段社区观察主路径、回退与异常跳转，并下钻到原始接待证据。</p>
        </div>
      </header>

      <div className={`ag-state-insights-layout${selected ? " has-detail" : ""}`}>
        <aside className="ag-state-filter-panel" aria-label="聚合筛选">
          <div className="ag-panel-heading">
            <strong>筛选条件</strong>
            <button type="button" onClick={resetFilters}>
              重置
            </button>
          </div>
          <form onSubmit={submitFilters}>
            <label>
              门店 ID
              <input
                aria-label="门店 ID"
                value={draft.storeIds}
                placeholder="S1,S2"
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    storeIds: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              销售姓名
              <input
                aria-label="销售姓名"
                value={draft.agentNames}
                placeholder="小林"
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    agentNames: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              业务场景
              <select
                aria-label="业务场景"
                value={draft.scenario}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    scenario: event.target.value as "" | ReceptionScenario,
                  }))
                }
              >
                <option value="">全部场景</option>
                <option value="gold">金店销售</option>
                <option value="automotive">汽车销售</option>
                <option value="custom">自定义</option>
              </select>
            </label>
            <label>
              开始时间
              <input
                aria-label="开始时间"
                aria-invalid={Boolean(filterError) || undefined}
                aria-describedby={filterError ? "ag-state-filter-error" : undefined}
                type="datetime-local"
                value={draft.startedFrom}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    startedFrom: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              结束时间
              <input
                aria-label="结束时间"
                aria-invalid={Boolean(filterError) || undefined}
                aria-describedby={filterError ? "ag-state-filter-error" : undefined}
                type="datetime-local"
                value={draft.startedTo}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    startedTo: event.target.value,
                  }))
                }
              />
            </label>
            <button className="ag-primary-action" type="submit">
              应用聚合筛选
            </button>
            {filterError && (
              <p
                id="ag-state-filter-error"
                className="ag-form-error"
                role="alert"
              >
                {filterError}
              </p>
            )}
          </form>

          <section
            className="ag-stage-legend"
            aria-labelledby="stage-legend-title"
          >
            <h2 id="stage-legend-title">阶段图例</h2>
            <ol>
              {visualStages.map((stage, index) => (
                <li key={stage.state}>
                  <i data-tone={STAGE_COLORS[index % STAGE_COLORS.length]}>
                    {index + 1}
                  </i>
                  <span>{stage.state}</span>
                  <strong>{stage.reception_count}</strong>
                </li>
              ))}
            </ol>
            {data.stages.length > visualStages.length && (
              <p>
                为保证交互性能，优先展示覆盖量最高的前{" "}
                {MAX_VISUAL_STAGES} 个阶段。
              </p>
            )}
          </section>

          <section className="ag-topology-line-legend" aria-label="连线说明">
            <h2>连线说明</h2>
            <p className="is-key">关键路径 · 置信度 ≥ 80%</p>
            <p className="is-normal">一般路径 · 置信度 50%–80%</p>
            <p className="is-danger">异常跳转 · 置信度 &lt; 50%</p>
          </section>
          <small className="ag-topology-generated-at">
            数据生成：{new Date(data.generated_at).toLocaleString()}
          </small>
        </aside>

        <section className="ag-state-flow-workbench">
          <div className="ag-state-flow-toolbar">
            <dl
              className="ag-topology-summary-strip"
              aria-label="聚合状态摘要"
            >
              <div>
                <dt>接待总数</dt>
                <dd>{data.total_receptions}</dd>
              </div>
              <div>
                <dt>可见关键路径占比</dt>
                <dd>{formatPercent(keyPathShare)}</dd>
              </div>
              <div>
                <dt>平均转移步数</dt>
                <dd>
                  {data.total_receptions
                    ? (data.total_transitions / data.total_receptions).toFixed(
                        1,
                      )
                    : "0.0"}
                </dd>
              </div>
            </dl>
            <div className="ag-topology-confidence-legend" aria-label="置信度图例">
              <i className="is-strong" /> ≥80%
              <i className="is-normal" /> 50%–80%
              <i className="is-danger" /> 异常
            </div>
          </div>

          {data.truncated && (
            <div className="ag-state-truncation-notice" role="status">
              <strong>聚合结果已按预算截断</strong>
              <span>
                阶段 {data.returned_stages ?? data.stages.length}/
                {data.stage_limit ?? data.stages.length}，路径{" "}
                {data.returned_transitions}/{data.transition_limit}。画布与占比仅基于当前返回数据；
                缩小筛选范围可获得更完整的路径。
              </span>
            </div>
          )}

          <div className="ag-state-flow-canvas">
            {visualStages.length === 0 ? (
              <div
                className="ag-state-flow-empty"
                role="status"
                aria-label="暂无符合条件的状态路径"
              >
                <strong>暂无符合条件的状态路径</strong>
                <span>调整门店、销售、场景或时间范围后重试。</span>
              </div>
            ) : (
              <svg
                viewBox={`0 0 ${graphWidth} 690`}
                role="group"
                aria-label={`聚合状态图谱，共 ${visualStages.length} 个阶段、${visibleTransitions.length} 条路径`}
                style={{ width: "100%", transform: `scale(${zoom})` }}
              >
                <defs>
                  <marker
                    id="ag-topology-arrow-strong"
                    markerWidth="9"
                    markerHeight="9"
                    refX="8"
                    refY="4.5"
                    orient="auto"
                  >
                    <path d="M0,0 L9,4.5 L0,9 Z" />
                  </marker>
                  <marker
                    id="ag-topology-arrow-normal"
                    markerWidth="8"
                    markerHeight="8"
                    refX="7"
                    refY="4"
                    orient="auto"
                  >
                    <path d="M0,0 L8,4 L0,8 Z" />
                  </marker>
                  <marker
                    id="ag-topology-arrow-danger"
                    markerWidth="8"
                    markerHeight="8"
                    refX="7"
                    refY="4"
                    orient="auto"
                  >
                    <path d="M0,0 L8,4 L0,8 Z" />
                  </marker>
                </defs>

                {orderedTransitions.map((transition, lane) => {
                  const sourceIndex = stageIndex.get(transition.from_state);
                  const targetIndex = stageIndex.get(transition.to_state);
                  if (sourceIndex === undefined || targetIndex === undefined) {
                    return null;
                  }
                  const tone = pathTone(transition);
                  const geometry = transitionGeometry(
                    sourceIndex,
                    targetIndex,
                    stageGap,
                    lane,
                  );
                  const isSelected =
                    selectedTransition?.from_state === transition.from_state &&
                    selectedTransition?.to_state === transition.to_state;
                  return (
                    <g
                      key={`${transition.from_state}-${transition.to_state}`}
                      className={`ag-state-path is-${tone}${isSelected ? " is-selected" : ""}`}
                      role="button"
                      tabIndex={0}
                      aria-label={`查看 ${transition.from_state} 到 ${transition.to_state} 的聚合证据`}
                      onClick={() =>
                        setSelected({ kind: "transition", transition })
                      }
                      onKeyDown={(event) =>
                        keyboardActivate(event, () =>
                          setSelected({ kind: "transition", transition }),
                        )
                      }
                    >
                      <path
                        className="ag-state-path-hit"
                        d={geometry.d}
                      />
                      <path
                        d={geometry.d}
                        markerEnd={markerForTone(tone)}
                      />
                    </g>
                  );
                })}

                {visualStages.map((stage, index) => {
                  const x = STAGE_START_X + index * stageGap;
                  const tone = STAGE_COLORS[index % STAGE_COLORS.length];
                  const isSelected =
                    selectedStage?.state === stage.state;
                  return (
                    <g
                      key={stage.state}
                      className={`ag-state-community${isSelected ? " is-selected" : ""}`}
                      data-tone={tone}
                      data-x={x}
                      transform={`translate(${x} 335)`}
                      role="button"
                      tabIndex={0}
                      aria-label={`查看 ${stage.state} 阶段证据`}
                      onClick={() => setSelected({ kind: "stage", stage })}
                      onKeyDown={(event) =>
                        keyboardActivate(event, () =>
                          setSelected({ kind: "stage", stage }),
                        )
                      }
                    >
                      <rect
                        className="ag-topology-community-field"
                        x="-70"
                        y="-174"
                        width="140"
                        height="326"
                        rx="70"
                      />
                      <circle
                        className="ag-topology-stage-number"
                        cx="-52"
                        cy="-211"
                        r="13"
                      />
                      <text
                        x="-52"
                        y="-207"
                        textAnchor="middle"
                        className="ag-stage-index"
                      >
                        {index + 1}
                      </text>
                      <text
                        x="-32"
                        y="-207"
                        textAnchor="start"
                        className="ag-stage-name"
                      >
                        {stage.state}
                      </text>
                      <text
                        x="-70"
                        y="-183"
                        textAnchor="start"
                        className="ag-stage-meta"
                      >
                        {/* 接待数不在这里重复——正下方的「阶段规模」药丸就是它。
                            13px 下这行是列间横向溢出的主犯:相邻列会串成一行。 */}
                        平均流出 {averageOutgoingEvents(stage).toFixed(2)} 次
                      </text>

                      <text
                        x="-58"
                        y="-145"
                        textAnchor="start"
                        className="ag-topology-section-label"
                      >
                        阶段规模
                      </text>
                      <StagePill
                        label={`${stage.reception_count} 次接待`}
                        y={-118}
                        tone="neutral"
                      />
                      <StagePill
                        label={`${stage.count} 条状态记录`}
                        y={-89}
                        tone="neutral"
                      />

                      <text
                        x="-58"
                        y="-55"
                        textAnchor="start"
                        className="ag-topology-section-label"
                      >
                        流转
                      </text>
                      <StagePill
                        label={`流入 ${stage.incoming_count}`}
                        y={-29}
                        tone="neutral"
                      />
                      <StagePill
                        label={`流出 ${stage.outgoing_count}`}
                        y={0}
                        tone="neutral"
                      />

                      <circle
                        className="ag-topology-core-halo"
                        cx="70"
                        cy="25"
                        r="11"
                      />
                      <circle
                        className="ag-topology-core"
                        cx="70"
                        cy="25"
                        r="7"
                      />

                      <text
                        x="-58"
                        y="45"
                        textAnchor="start"
                        className="ag-topology-section-label"
                      >
                        判定置信度
                      </text>
                      <StagePill
                        label={`置信度 ${formatPercent(stage.average_confidence)}`}
                        y={70}
                        tone="accent"
                      />
                      <StagePill
                        label={`人均流出 ${averageOutgoingEvents(stage).toFixed(1)}`}
                        y={98}
                        tone="accent"
                      />
                      <text
                        y="132"
                        textAnchor="middle"
                        className="ag-topology-sample-count"
                      >
                        关联样本 {stage.count}
                      </text>
                    </g>
                  );
                })}

                {/* 连线标签层。相邻列间距(≈stageGap)只比阶段气泡宽一条缝,
                    标签必然压进两侧气泡;SVG 按文档序作画,画在阶段层之后
                    才能压其上,再垫白底药丸保证可读。层不接事件——点击仍
                    落在下面的连线命中区上。 */}
                {orderedTransitions.map((transition, lane) => {
                  const sourceIndex = stageIndex.get(transition.from_state);
                  const targetIndex = stageIndex.get(transition.to_state);
                  if (sourceIndex === undefined || targetIndex === undefined) {
                    return null;
                  }
                  const tone = pathTone(transition);
                  const geometry = transitionGeometry(
                    sourceIndex,
                    targetIndex,
                    stageGap,
                    lane,
                  );
                  const label = `${transition.count} 次 · ${formatPercent(
                    transition.average_confidence ?? 0,
                  )}`;
                  const width = label.length * 6.9 + 14;
                  return (
                    <g
                      key={`label-${transition.from_state}-${transition.to_state}`}
                      className={`ag-state-path-label is-${tone}`}
                      pointerEvents="none"
                      aria-hidden="true"
                    >
                      <rect
                        x={geometry.labelX - width / 2}
                        y={geometry.labelY - 12}
                        width={width}
                        height={17}
                        rx={4}
                      />
                      <text
                        x={geometry.labelX}
                        y={geometry.labelY}
                        textAnchor="middle"
                      >
                        {label}
                      </text>
                    </g>
                  );
                })}
              </svg>
            )}

            {visualStages.length > 0 && (
              <div className="ag-graph-controls" aria-label="图谱视图控制">
                <button
                  type="button"
                  aria-label="缩小图谱"
                  onClick={() =>
                    setZoom((value) => Math.max(0.8, value - 0.1))
                  }
                >
                  <IconZoomOut />
                </button>
                <output aria-label="当前缩放">{Math.round(zoom * 100)}%</output>
                <button
                  type="button"
                  aria-label="放大图谱"
                  onClick={() =>
                    setZoom((value) => Math.min(1.4, value + 0.1))
                  }
                >
                  <IconZoomIn />
                </button>
                <button
                  type="button"
                  aria-label="适配可见图谱"
                  onClick={() => setZoom(1)}
                >
                  <IconFullscreen />
                </button>
                <button
                  type="button"
                  aria-label="重置图谱"
                  onClick={() => {
                    setZoom(1);
                    setSelected(null);
                  }}
                >
                  <IconRefresh />
                </button>
              </div>
            )}
          </div>
        </section>

        {selectedTransition && (
          <aside className="ag-state-path-detail" aria-label="路径证据">
            <header>
              <div>
                <span>
                  <IconBranch /> 路径证据
                </span>
                <h2>
                  {selectedTransition.from_state} →{" "}
                  {selectedTransition.to_state}
                </h2>
              </div>
              <button
                type="button"
                aria-label="关闭路径证据"
                onClick={() => setSelected(null)}
              >
                <IconClose />
              </button>
            </header>
            <dl>
              <div>
                <dt>转移次数</dt>
                <dd>{selectedTransition.count}</dd>
              </div>
              <div>
                <dt>置信度</dt>
                <dd>
                  {formatPercent(selectedTransition.average_confidence)}
                </dd>
              </div>
              <div>
                <dt>证据片段</dt>
                <dd>{selectedTransition.evidence_count}</dd>
              </div>
            </dl>
            <section>
              <h3>主要触发因素</h3>
              <ol>
                {selectedTransition.top_triggers.map((item) => (
                  <li key={item.trigger}>
                    <span>{item.trigger}</span>
                    <strong>{item.count}</strong>
                  </li>
                ))}
              </ol>
            </section>
            <section>
              <h3>样本接待</h3>
              <div className="ag-sample-receptions">
                {selectedTransition.sample_reception_ids.map((receptionId) => (
                  <Link
                    key={receptionId}
                    to={`/receptions/${receptionId}/graph?mode=state&from=${encodeURIComponent(selectedTransition.from_state)}&to=${encodeURIComponent(selectedTransition.to_state)}`}
                  >
                    查看接待 {receptionId} 详情
                  </Link>
                ))}
              </div>
            </section>
            <section className="ag-topology-trace">
              <h3>溯源链</h3>
              <p>自动语音转写 → 语义阶段识别 → 状态转移聚合 → 质量校验</p>
            </section>
          </aside>
        )}

        {selectedStage && (
          <aside className="ag-state-path-detail" aria-label="节点证据">
            <header>
              <div>
                <span>
                  <IconBranch /> 节点证据
                </span>
                <h2>{selectedStage.state}</h2>
              </div>
              <button
                type="button"
                aria-label="关闭节点证据"
                onClick={() => setSelected(null)}
              >
                <IconClose />
              </button>
            </header>
            <dl>
              <div>
                <dt>接待覆盖</dt>
                <dd>{selectedStage.reception_count}</dd>
              </div>
              <div>
                <dt>流入 / 流出</dt>
                <dd>
                  {selectedStage.incoming_count} / {selectedStage.outgoing_count}
                </dd>
              </div>
              <div>
                <dt>平均置信度</dt>
                <dd>{formatPercent(selectedStage.average_confidence)}</dd>
              </div>
            </dl>
            {/* A "frequent phrases and actions" section used to sit here, built
                from a hardcoded script rather than from the API — which returns
                only counts and confidence for a stage. Restore it when the
                backend can aggregate real utterances. */}
            <section>
              <h3>关联路径</h3>
              <ol>
                {stageConnectedTransitions.slice(0, 6).map((transition) => (
                  <li
                    key={`${transition.from_state}-${transition.to_state}`}
                  >
                    <button
                      type="button"
                      onClick={() =>
                        setSelected({ kind: "transition", transition })
                      }
                    >
                      {transition.from_state} → {transition.to_state}
                    </button>
                    <strong>{transition.count}</strong>
                  </li>
                ))}
              </ol>
            </section>
            <section className="ag-topology-trace">
              <h3>节点来源</h3>
              <p>基于 {selectedStage.count} 条状态事件聚合，可继续沿关联路径定位原始接待。</p>
            </section>
          </aside>
        )}
      </div>
    </main>
  );
}
