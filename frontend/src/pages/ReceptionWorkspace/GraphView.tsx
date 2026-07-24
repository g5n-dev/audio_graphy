import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { getReceptionWorkspace } from "@/api/services";
import {
  DialogueGraph,
  type DialogueGraphNodeDetails,
  type DialogueGraphMode,
} from "@/components/dialogue/DialogueGraph";
import { formatClock } from "@/components/dialogue/format";
import { ReceptionContextTabs } from "@/components/navigation/ContextNavigation";
import "./GraphView.css";

const GRAPH_MODES: Array<{
  key: DialogueGraphMode;
  label: string;
  description: string;
}> = [
  {
    key: "relation",
    label: "关系图谱",
    description: "接待、源录音、对话单元和标签之间的业务关系。",
  },
  {
    key: "temporal",
    label: "时序差分",
    description: "接待版本、人工编辑与对话单元版本的变化路径。",
  },
  {
    key: "state",
    label: "状态转移",
    description: "销售阶段的有向转移、触发条件与置信度。",
  },
  {
    key: "provenance",
    label: "溯源 DAG",
    description: "原音/文本证据如何支持标签并归属到对话单元。",
  },
];

const MODE_COMMUNITIES: Record<
  DialogueGraphMode,
  Array<{ label: string; description: string; tone: string }>
> = {
  relation: [
    { label: "源录音", description: "多段短录音与时间轴", tone: "blue" },
    { label: "对话单元", description: "切分后的业务语义片段", tone: "cyan" },
    { label: "本次接待", description: "客户接待聚合中心", tone: "amber" },
    { label: "目标标签", description: "模型与人工标签结果", tone: "violet" },
  ],
  temporal: [
    { label: "变更事件", description: "模型、规则与人工操作", tone: "blue" },
    { label: "接待版本", description: "可回溯版本主线", tone: "amber" },
    { label: "受影响单元", description: "差分后的对话单元", tone: "violet" },
  ],
  state: [
    { label: "销售阶段", description: "单次接待状态节点", tone: "blue" },
    { label: "触发路径", description: "状态转移与置信度", tone: "cyan" },
    { label: "聚合参照", description: "可回到跨接待路径", tone: "amber" },
  ],
  provenance: [
    { label: "原始证据", description: "录音、文本与父项", tone: "blue" },
    { label: "处理事件", description: "算法、人工与时间", tone: "amber" },
    { label: "产出对象", description: "标签和对话单元", tone: "violet" },
  ],
};

function graphModeFromQuery(value: string | null): DialogueGraphMode {
  return GRAPH_MODES.some((item) => item.key === value)
    ? (value as DialogueGraphMode)
    : "relation";
}

export default function ReceptionGraphPage() {
  const { id } = useParams<{ id: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const [mode, setMode] = useState<DialogueGraphMode>(() =>
    graphModeFromQuery(searchParams.get("mode")),
  );
  const [selectedNode, setSelectedNode] =
    useState<DialogueGraphNodeDetails | null>(null);
  const query = useQuery({
    queryKey: ["reception-workspace", id],
    queryFn: () => getReceptionWorkspace(id ?? ""),
    enabled: Boolean(id),
    retry: false,
  });

  useEffect(() => {
    setMode(graphModeFromQuery(searchParams.get("mode")));
    setSelectedNode(null);
  }, [searchParams]);

  if (query.isPending) {
    return (
      <div className="ag-feature-loading" role="status">
        正在构建接待图谱…
      </div>
    );
  }
  if (query.isError || !query.data) {
    return (
      <div className="ag-feature-empty" role="alert">
        <h1>接待图谱暂不可用</h1>
        <p>接口未返回真实的接待、状态与证据数据。</p>
        <button type="button" onClick={() => query.refetch()}>
          重新加载
        </button>
      </div>
    );
  }

  const activeMode = GRAPH_MODES.find((item) => item.key === mode)!;
  const fromState = searchParams.get("from");
  const toState = searchParams.get("to");
  const workspaceWindow = query.data.window;
  const highlightedTransition =
    mode === "state" && fromState && toState
      ? { fromState, toState }
      : null;
  const windowCollections = [
    { label: "对话单元", collection: workspaceWindow.dialogue_units },
    { label: "目标标签", collection: workspaceWindow.tag_assignments },
    { label: "状态转移", collection: workspaceWindow.state_transitions },
    { label: "转写片段", collection: workspaceWindow.transcript_items },
    { label: "溯源事件", collection: workspaceWindow.provenance_events },
  ];
  const intentSignalCount =
    query.data.tag_assignments.filter(
      (tag) =>
        (tag.label_key ?? "").includes("intent") ||
        (tag.label_value ?? "").includes("成交"),
    ).length;

  const changeMode = (nextMode: DialogueGraphMode) => {
    setMode(nextMode);
    setSelectedNode(null);
    const nextSearchParams = new URLSearchParams(searchParams);
    nextSearchParams.set("mode", nextMode);
    if (nextMode !== "state") {
      nextSearchParams.delete("from");
      nextSearchParams.delete("to");
    }
    setSearchParams(nextSearchParams, { replace: true });
  };

  return (
    <div className="ag-graph-page">
      <header className="ag-graph-studio-header">
        <div className="ag-graph-studio-header__identity">
          <span className="ag-eyebrow">接待关系图谱</span>
          <strong>接待 #{query.data.reception.id}</strong>
          <span className="ag-graph-studio-header__live">
            实时 · v{query.data.reception.version}
          </span>
        </div>
        <div className="ag-graph-studio-header__summary">
          <span>{activeMode.label}</span>
          <strong>{activeMode.description}</strong>
        </div>
        <div className="ag-graph-studio-header__actions">
          <span>单次接待 · 关系、时序、状态与证据溯源</span>
        </div>
      </header>
      <ReceptionContextTabs receptionId={id ?? String(query.data.reception.id)} />

      <section className="ag-detail-graph-layout">
        <aside className="ag-detail-graph-sidebar" aria-label="图谱图例">
          <nav className="ag-graph-mode-tabs" aria-label="图谱模式">
            {GRAPH_MODES.map((item, index) => (
              <button
                type="button"
                key={item.key}
                aria-pressed={mode === item.key}
                onClick={() => changeMode(item.key)}
              >
                <span>{String(index + 1).padStart(2, "0")}</span>
                <strong>{item.label}</strong>
                <small>{item.description}</small>
              </button>
            ))}
          </nav>

          <section
            className="ag-graph-window-context"
            role="status"
            aria-label="当前图谱数据窗口"
          >
            <header>
              <span>当前窗口</span>
              <strong>
                {formatClock(workspaceWindow.start_sec)}–
                {formatClock(workspaceWindow.end_sec)} / 总时长{" "}
                {formatClock(workspaceWindow.reception_duration_sec)}
              </strong>
            </header>
            <dl>
              {windowCollections.map(({ label, collection }) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd data-truncated={collection.truncated || undefined}>
                    {collection.returned} / {collection.total} · 上限{" "}
                    {collection.limit}
                  </dd>
                </div>
              ))}
            </dl>
            {(workspaceWindow.has_previous || workspaceWindow.has_next) && (
              <p>
                当前仅展示完整接待的一个时间窗口 · 整场对话单元{" "}
                {workspaceWindow.total_dialogue_units}
              </p>
            )}
            {workspaceWindow.truncated && (
              <p>当前窗口内部分数据已截断，画布使用当前返回窗口。</p>
            )}
          </section>

          <header className="ag-detail-graph-sidebar__section-heading">
            <span>社区图例</span>
            <strong>{activeMode.label}</strong>
          </header>
          <ul>
            {MODE_COMMUNITIES[mode].map((community) => (
              <li key={community.label}>
                <i data-tone={community.tone} />
                <span>
                  <strong>{community.label}</strong>
                  <small>{community.description}</small>
                </span>
              </li>
            ))}
          </ul>
          <dl>
            <div>
              <dt>原始录音</dt>
              <dd>{query.data.recordings.length}</dd>
            </div>
            <div>
              <dt>对话单元</dt>
              <dd>
                {workspaceWindow.dialogue_units.returned} /{" "}
                {workspaceWindow.dialogue_units.total}
              </dd>
            </div>
            <div>
              <dt>目标标签</dt>
              <dd>
                {workspaceWindow.tag_assignments.returned} /{" "}
                {workspaceWindow.tag_assignments.total}
              </dd>
            </div>
            <div>
              <dt>状态转移</dt>
              <dd>
                {workspaceWindow.state_transitions.returned} /{" "}
                {workspaceWindow.state_transitions.total}
              </dd>
            </div>
          </dl>
        </aside>

        <div className="ag-graph-canvas" aria-live="polite">
          <header className="ag-graph-canvas__header">
            <span>INTELLIGENCE GRAPH / LIVE TOPOLOGY</span>
            <strong>{activeMode.label}</strong>
            <small>
              {workspaceWindow.dialogue_units.returned +
                workspaceWindow.tag_assignments.returned +
                query.data.recordings.length +
                1}{" "}
              节点 · 语义缩放
            </small>
          </header>
          {mode === "state" && fromState && toState && (
            <div className="ag-graph-path-context" role="status">
              <span>聚合路径定位</span>
              <strong>
                {fromState} → {toState}
              </strong>
              <Link to="/reception-flow">返回聚合路径</Link>
            </div>
          )}
          <DialogueGraph
            workspace={query.data}
            mode={mode}
            highlightedTransition={highlightedTransition}
            onNodeDetails={setSelectedNode}
          />
          <section className="ag-graph-canvas-clue" aria-label="高价值线索">
            <span>高价值线索</span>
            <strong>{intentSignalCount} 个意向信号</strong>
            <p>结合状态转移与证据片段，优先复核成交机会。</p>
          </section>
        </div>

        <aside className="ag-graph-node-inspector" aria-label="节点详情">
          <header>
            <span>节点研判</span>
            <strong>{selectedNode?.label ?? "选择一个节点"}</strong>
          </header>
          {selectedNode ? (
            <dl>
              <div>
                <dt>节点类型</dt>
                <dd>{selectedNode.kind}</dd>
              </div>
              <div>
                <dt>所属社区</dt>
                <dd>{selectedNode.community}</dd>
              </div>
              <div>
                <dt>节点标识</dt>
                <dd>{selectedNode.id}</dd>
              </div>
            </dl>
          ) : (
            <p>
              点击社区中的节点，查看类型、所属社区和可追溯标识；也可使用
              Tab 与回车键操作。
            </p>
          )}
          <section className="ag-graph-value-clue">
            <span>高价值线索</span>
            <strong>{intentSignalCount} 个意向信号</strong>
            <p>结合状态转移与证据片段，优先复核高意向、价格异议和后续动作。</p>
          </section>
          <Link to={`/receptions/${id}/workspace`}>
            在调听工作台查看时间轴与证据
          </Link>
        </aside>
      </section>
    </div>
  );
}
