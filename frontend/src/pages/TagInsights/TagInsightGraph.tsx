import {
  useEffect,
  useId,
  useMemo,
  useState,
  type CSSProperties,
  type KeyboardEvent,
} from "react";
import {
  IconApps,
  IconEye,
  IconRefresh,
  IconZoomIn,
  IconZoomOut,
} from "@arco-design/web-react/icon";
import { Link } from "react-router-dom";
import { formatClock } from "@/components/dialogue/format";
import type {
  AnalyzeTagInsightsResponse,
  TagInsightEvidenceRef,
  TagInsightGroup,
  TagInsightMatrixRow,
} from "@/types/api";
import "./TagInsightGraph.css";

interface TagInsightGraphProps {
  result: AnalyzeTagInsightsResponse;
  receptionId?: string | number;
}

type GraphNodeKind = "focus" | "value" | "evidence";
type GraphNodeStatus = "agreement" | "conflict" | "neutral";
type GraphEdgeKind = "evidence-ownership" | "co-occurrence" | "difference";

interface GraphNode {
  id: string;
  kind: GraphNodeKind;
  status: GraphNodeStatus;
  label: string;
  subtitle: string;
  community: string;
  rowIndex: number;
  groupVersion?: string;
  evidence?: TagInsightEvidenceRef;
}

interface GraphEdge {
  id: string;
  source: string;
  target: string;
  kind: GraphEdgeKind;
  label: string;
}

interface GraphModel {
  nodes: GraphNode[];
  edges: GraphEdge[];
  wasLimited: boolean;
}

interface NodePosition {
  x: number;
  y: number;
}

interface CommunityLayout {
  key: string;
  color: CommunityColor;
  x: number;
  y: number;
  width: number;
  height: number;
  centerX: number;
  centerY: number;
}

interface CommunityColor {
  strong: string;
  soft: string;
  wash: string;
}

export const TAG_INSIGHT_GRAPH_NODE_BUDGET = 48;

const GRAPH_WIDTH = 1120;
const MIN_GRAPH_HEIGHT = 700;
const COMMUNITY_COLORS: CommunityColor[] = [
  { strong: "#246BCE", soft: "#DCEBFF", wash: "#F4F8FF" },
  { strong: "#087E73", soft: "#D6F4EE", wash: "#F1FBF8" },
  { strong: "#9B5D05", soft: "#FCE9C5", wash: "#FFF9ED" },
  { strong: "#6D4BC3", soft: "#EAE2FF", wash: "#F8F5FF" },
  { strong: "#B13D64", soft: "#FADCE7", wash: "#FFF5F8" },
  { strong: "#34786A", soft: "#DDF1EA", wash: "#F4FAF8" },
  { strong: "#2B78A0", soft: "#D9EEF7", wash: "#F3FAFD" },
  { strong: "#C15A28", soft: "#F7E0D5", wash: "#FFF6F1" },
];

const COMMUNITY_COLOR_INDEX: Record<string, number> = {
  "stage.greeting": 0,
  "stage.requirement": 1,
  "stage.presentation": 2,
  "stage.experience": 3,
  "objection.price": 4,
  "intent.level": 5,
  next_step: 6,
  "compliance.risk": 7,
};

function stableCommunityColor(key: string): CommunityColor {
  const preferredIndex = COMMUNITY_COLOR_INDEX[key];
  if (preferredIndex !== undefined) {
    return COMMUNITY_COLORS[preferredIndex];
  }
  let hash = 0;
  for (const character of key) {
    hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  }
  return COMMUNITY_COLORS[hash % COMMUNITY_COLORS.length];
}

const EDGE_APPEARANCE: Record<
  GraphEdgeKind,
  { color: string; dash?: string; marker: string }
> = {
  "evidence-ownership": {
    color: "#8CA2C0",
    marker: "ownership",
  },
  "co-occurrence": {
    color: "#0D9488",
    dash: "3 5",
    marker: "co-occurrence",
  },
  difference: {
    color: "#D45B3F",
    dash: "8 4",
    marker: "difference",
  },
};

function groupIdentity(group: TagInsightGroup): string {
  return group.group_id ?? `${group.group_key}@${group.version}`;
}

function groupVersionLabel(group: TagInsightGroup): string {
  return `${group.group_key}@${group.version}`;
}

function rowIdentity(row: TagInsightMatrixRow, rowIndex: number): string {
  return [
    row.target_id,
    row.window.start_ms,
    row.window.end_ms,
    row.label_key,
    rowIndex,
  ].join("::");
}

function evidenceIdentity(
  rowId: string,
  evidence: TagInsightEvidenceRef,
): string {
  return `evidence::${rowId}::${evidence.ref_id}`;
}

function addUniqueNode(nodes: Map<string, GraphNode>, node: GraphNode): void {
  if (!nodes.has(node.id)) nodes.set(node.id, node);
}

function addUniqueEdge(edges: Map<string, GraphEdge>, edge: GraphEdge): void {
  if (edge.source === edge.target || edges.has(edge.id)) return;
  edges.set(edge.id, edge);
}

function buildGraph(result: AnalyzeTagInsightsResponse): GraphModel {
  const candidateNodes = new Map<string, GraphNode>();
  const candidateEdges = new Map<string, GraphEdge>();
  const coOccurrenceNodeByToken = new Map<string, string>();

  result.matrix.forEach((row, rowIndex) => {
    const rowId = rowIdentity(row, rowIndex);
    const focusId = `focus::${rowId}`;
    const status: GraphNodeStatus = row.conflict
      ? "conflict"
      : row.missing_group_keys.length > 0 ||
          row.cells.some((cell) => cell.missing)
        ? "neutral"
        : "agreement";
    addUniqueNode(candidateNodes, {
      id: focusId,
      kind: "focus",
      status,
      label: row.label_key,
      subtitle: row.target_id,
      community: row.label_key,
      rowIndex,
    });
    row.merged.values.forEach((value) => {
      const token = `${row.label_key}=${value}`;
      if (!coOccurrenceNodeByToken.has(`__merged__::${token}`)) {
        coOccurrenceNodeByToken.set(`__merged__::${token}`, focusId);
      }
    });

    const valueNodes: Array<{ id: string; value: string }> = [];
    row.cells.forEach((cell, cellIndex) => {
      cell.assignments.forEach((assignment, assignmentIndex) => {
        const exactGroup = groupVersionLabel(cell.group);
        const valueId = [
          "value",
          rowId,
          groupIdentity(cell.group),
          assignment.value,
          cellIndex,
          assignmentIndex,
        ].join("::");
        addUniqueNode(candidateNodes, {
          id: valueId,
          kind: "value",
          status,
          label: assignment.value,
          subtitle: exactGroup,
          community: row.label_key,
          rowIndex,
          groupVersion: exactGroup,
        });
        const token = `${row.label_key}=${assignment.value}`;
        const groupKeys = new Set([
          groupIdentity(cell.group),
          cell.group.group_key,
          assignment.group_id,
          assignment.group_key,
        ]);
        groupKeys.forEach((groupKey) => {
          if (!groupKey) return;
          const lookupKey = `${groupKey}::${token}`;
          if (!coOccurrenceNodeByToken.has(lookupKey)) {
            coOccurrenceNodeByToken.set(lookupKey, valueId);
          }
        });
        valueNodes.push({ id: valueId, value: assignment.value });
        addUniqueEdge(candidateEdges, {
          id: `ownership::${focusId}::${valueId}`,
          source: focusId,
          target: valueId,
          kind: "evidence-ownership",
          label: "标签组赋值",
        });

        assignment.evidence_refs.forEach((evidence) => {
          const evidenceId = evidenceIdentity(rowId, evidence);
          const timecode = formatClock((evidence.start_ms ?? 0) / 1_000);
          addUniqueNode(candidateNodes, {
            id: evidenceId,
            kind: "evidence",
            status: "neutral",
            label: `${evidence.kind === "audio" ? "原音" : "文本"} ${timecode}`,
            subtitle: evidence.recording_id,
            community: row.label_key,
            rowIndex,
            evidence,
          });
          addUniqueEdge(candidateEdges, {
            id: `ownership::${valueId}::${evidenceId}`,
            source: valueId,
            target: evidenceId,
            kind: "evidence-ownership",
            label: "证据归属",
          });
        });
      });
    });

    if (row.conflict && valueNodes.length > 1) {
      const anchor = valueNodes[0];
      valueNodes.slice(1).forEach((candidate) => {
        if (candidate.value === anchor.value) return;
        addUniqueEdge(candidateEdges, {
          id: `difference::${anchor.id}::${candidate.id}`,
          source: anchor.id,
          target: candidate.id,
          kind: "difference",
          label: "组间差异",
        });
      });
    }
  });

  result.co_occurrences.forEach((occurrence) => {
    const source = coOccurrenceNodeByToken.get(
      `${occurrence.group_key}::${occurrence.left_label}`,
    );
    const target = coOccurrenceNodeByToken.get(
      `${occurrence.group_key}::${occurrence.right_label}`,
    );
    if (!source || !target) return;
    addUniqueEdge(candidateEdges, {
      id: [
        "co-occurrence",
        occurrence.group_key,
        occurrence.left_label,
        occurrence.right_label,
      ].join("::"),
      source,
      target,
      kind: "co-occurrence",
      label: `共现 ${occurrence.count}`,
    });
  });

  const allNodes = [...candidateNodes.values()];
  const nodes = allNodes.slice(0, TAG_INSIGHT_GRAPH_NODE_BUDGET);
  const visibleNodeIds = new Set(nodes.map((node) => node.id));
  const edges = [...candidateEdges.values()].filter(
    (edge) =>
      visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target),
  );

  return {
    nodes,
    edges,
    wasLimited: allNodes.length > nodes.length,
  };
}

function buildLayout(nodes: GraphNode[]): {
  positions: Map<string, NodePosition>;
  communities: CommunityLayout[];
  height: number;
  root: NodePosition;
} {
  const communityKeys = [...new Set(nodes.map((node) => node.community))];
  const positions = new Map<string, NodePosition>();
  const communitiesByKey = new Map(
    communityKeys.map((community) => [
      community,
      nodes.filter((node) => node.community === community),
    ]),
  );

  if (communityKeys.length === 0) {
    return {
      positions,
      communities: [],
      height: MIN_GRAPH_HEIGHT,
      root: { x: GRAPH_WIDTH / 2, y: MIN_GRAPH_HEIGHT / 2 },
    };
  }

  const graphHeight = communityKeys.length > 10 ? 900 : 760;
  const root: NodePosition =
    communityKeys.length === 1
      ? { x: 220, y: graphHeight / 2 }
      : { x: GRAPH_WIDTH / 2, y: graphHeight / 2 };

  const orbitalCenters = (count: number): NodePosition[] => {
    if (count === 1) {
      return [{ x: 710, y: graphHeight / 2 }];
    }
    if (count === 2) {
      return [
        { x: 230, y: graphHeight / 2 },
        { x: GRAPH_WIDTH - 230, y: graphHeight / 2 },
      ];
    }

    const horizontalRadius = count <= 4 ? 340 : 370;
    const verticalRadius = count <= 4 ? 238 : 270;
    return communityKeys.map((_, index) => {
      const angle = -Math.PI / 2 + (index / count) * Math.PI * 2;
      return {
        x: root.x + Math.cos(angle) * horizontalRadius,
        y: root.y + Math.sin(angle) * verticalRadius,
      };
    });
  };

  const centers = orbitalCenters(communityKeys.length);

  const communities: CommunityLayout[] = communityKeys.map(
    (community, communityIndex) => {
      const center = centers[communityIndex];
      const members = communitiesByKey.get(community) ?? [];
      const width =
        communityKeys.length === 1
          ? 700
          : Math.min(248, Math.max(190, 154 + members.length * 12));
      const height =
        communityKeys.length === 1
          ? 520
          : Math.min(198, Math.max(148, 118 + members.length * 10));
      const x = center.x - width / 2;
      const y = center.y - height / 2;
      const orderedMembers = [...members].sort(
        (left, right) =>
          ["focus", "value", "evidence"].indexOf(left.kind) -
          ["focus", "value", "evidence"].indexOf(right.kind),
      );

      orderedMembers.forEach((node, index) => {
        if (index === 0) {
          positions.set(node.id, center);
          return;
        }

        let ring = 1;
        let ringStart = 1;
        let ringCapacity = 6;
        while (index >= ringStart + ringCapacity) {
          ringStart += ringCapacity;
          ring += 1;
          ringCapacity = ring * 6;
        }
        const indexInRing = index - ringStart;
        const angle =
          -Math.PI / 2 +
          (indexInRing / ringCapacity) * Math.PI * 2 +
          (ring % 2 === 0 ? Math.PI / ringCapacity : 0);
        const radiusX = Math.min(
          width * 0.42,
          (communityKeys.length === 1 ? 74 : 42) * ring,
        );
        const radiusY = Math.min(
          height * 0.39,
          (communityKeys.length === 1 ? 58 : 34) * ring,
        );
        positions.set(node.id, {
          x: center.x + Math.cos(angle) * radiusX,
          y: center.y + Math.sin(angle) * radiusY,
        });
      });

      return {
        key: community,
        color: stableCommunityColor(community),
        x,
        y,
        width,
        height,
        centerX: center.x,
        centerY: center.y,
      };
    },
  );

  return { positions, communities, height: graphHeight, root };
}

function communityColor(
  communities: CommunityLayout[],
  community: string,
): CommunityColor {
  return (
    communities.find((item) => item.key === community)?.color ??
    COMMUNITY_COLORS[0]
  );
}

function nodeAppearance(
  node: GraphNode,
  color: CommunityColor,
): { fill: string; stroke: string } {
  if (node.kind === "focus") {
    return { fill: "#FFFFFF", stroke: color.strong };
  }
  if (node.kind === "evidence") {
    return { fill: "#FFFFFF", stroke: "#7386A5" };
  }
  if (node.status === "conflict") {
    return { fill: "#FFF1ED", stroke: "#C94D32" };
  }
  if (node.status === "agreement") {
    return { fill: "#EAF8F1", stroke: "#16805D" };
  }
  return { fill: color.soft, stroke: color.strong };
}

function nodeDimensions(kind: GraphNodeKind): {
  width: number;
  height: number;
} {
  if (kind === "focus") return { width: 34, height: 34 };
  if (kind === "value") return { width: 22, height: 22 };
  return { width: 18, height: 18 };
}

function clipLabel(value: string, maxLength: number): string {
  return value.length > maxLength
    ? `${value.slice(0, maxLength - 1)}…`
    : value;
}

function nodeAriaLabel(node: GraphNode): string {
  const status =
    node.status === "conflict"
      ? "冲突"
      : node.status === "agreement"
        ? "一致"
        : node.kind === "evidence"
          ? "证据"
          : "结果不完整";
  if (node.kind === "focus") {
    return `标签 ${node.label}，目标 ${node.subtitle}，${status}`;
  }
  if (node.kind === "value") {
    return `组版本 ${node.subtitle}，值 ${node.label}，${status}`;
  }
  return `${node.label}，录音 ${node.subtitle}`;
}

function collectRowEvidence(row: TagInsightMatrixRow): TagInsightEvidenceRef[] {
  const evidence = [
    ...row.cells.flatMap((cell) =>
      cell.assignments.flatMap((assignment) => assignment.evidence_refs),
    ),
    ...row.merged.evidence_refs,
  ];
  return [
    ...new Map(
      evidence.map((item) => [
        `${item.ref_id}::${item.recording_id}`,
        item,
      ]),
    ).values(),
  ];
}

function receptionIdFromTarget(
  explicitId: string | number | undefined,
  targetId: string,
): string {
  if (explicitId !== undefined) return String(explicitId);
  const persistedTarget = /^reception:([^/]+)\/unit:[^/]+$/.exec(targetId);
  return persistedTarget?.[1] ?? targetId;
}

function percentage(value: number | null): string {
  return value === null || !Number.isFinite(value)
    ? "—"
    : `${Math.round(value * 100)}%`;
}

const NODE_KIND_LABELS: Record<GraphNodeKind, string> = {
  focus: "目标标签",
  value: "组版本值",
  evidence: "原始证据",
};

const COMMUNITY_DISPLAY_NAMES: Record<string, string> = {
  "stage.greeting": "初次接触",
  "stage.requirement": "需求发现",
  "stage.presentation": "方案推荐",
  "stage.experience": "产品体验",
  "objection.price": "价格异议",
  "intent.level": "购买意向",
  next_step: "下一步计划",
  "compliance.risk": "合规风险",
};

function communityDisplayName(key: string): string {
  return COMMUNITY_DISPLAY_NAMES[key] ?? key;
}

function curvedEdgePath(
  source: NodePosition,
  target: NodePosition,
  edgeIndex: number,
  kind: GraphEdgeKind,
): { path: string; label: NodePosition } {
  const midpointX = (source.x + target.x) / 2;
  const midpointY = (source.y + target.y) / 2;
  const deltaX = target.x - source.x;
  const deltaY = target.y - source.y;
  const distance = Math.max(Math.hypot(deltaX, deltaY), 1);
  const bend =
    kind === "difference"
      ? 22
      : kind === "co-occurrence"
        ? 16
        : edgeIndex % 2 === 0
          ? 8
          : -8;
  const controlX = midpointX - (deltaY / distance) * bend;
  const controlY = midpointY + (deltaX / distance) * bend;
  return {
    path: `M ${source.x} ${source.y} Q ${controlX} ${controlY} ${target.x} ${target.y}`,
    label: {
      x: (source.x + 2 * controlX + target.x) / 4,
      y: (source.y + 2 * controlY + target.y) / 4,
    },
  };
}

function DetailPanel({
  node,
  row,
  receptionId,
}: {
  node: GraphNode | null;
  row: TagInsightMatrixRow | null;
  receptionId?: string | number;
}) {
  const evidence = row ? collectRowEvidence(row) : [];
  const rowIsIncomplete =
    row !== null &&
    (row.missing_group_keys.length > 0 ||
      row.cells.some((cell) => cell.missing));
  const stateLabel = row?.conflict
    ? "存在组间冲突"
    : rowIsIncomplete
      ? "存在组值缺失"
      : "多组结果一致";
  const stateClass = row?.conflict
    ? "is-conflict"
    : rowIsIncomplete
      ? "is-incomplete"
      : "is-agreement";
  return (
    <aside
      role="complementary"
      aria-label="标签节点详情"
      data-panel-width="narrow"
      className="tig-inspector"
    >
      {!node || !row ? (
        <div className="tig-inspector__empty">
          <span>标签对比与溯源</span>
          <h3>选择图谱节点</h3>
          <p>
            可单击节点，或使用 Tab 定位后按 Enter / 空格查看组版本、证据与调听入口。
          </p>
          <dl>
            <div>
              <dt>标签组</dt>
              <dd>多版本并排核验</dd>
            </div>
            <div>
              <dt>证据链</dt>
              <dd>时间码直达调听</dd>
            </div>
          </dl>
        </div>
      ) : (
        <>
          <header className="tig-inspector__header">
            <div>
              <span>标签对比与溯源</span>
              <strong className={`tig-state-pill ${stateClass}`}>
                {stateLabel}
              </strong>
            </div>
            <h3>{row.label_key}</h3>
            <p>
              {row.target_id} · {formatClock(row.window.start_ms / 1_000)}–
              {formatClock(row.window.end_ms / 1_000)}
            </p>
          </header>

          <div className="tig-comparison-table">
            <table>
              <thead>
                <tr>
                  <th>对比维度</th>
                  {row.cells.map((cell) => (
                    <th
                      key={groupIdentity(cell.group)}
                      className={
                        node.groupVersion === groupVersionLabel(cell.group)
                          ? "is-selected"
                          : undefined
                      }
                    >
                      {groupVersionLabel(cell.group)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th>标签值</th>
                  {row.cells.map((cell) => (
                    <td key={groupIdentity(cell.group)}>
                      {cell.missing || cell.assignments.length === 0 ? (
                        <span className="tig-missing-value">缺失</span>
                      ) : (
                        cell.assignments.map((assignment, index) => (
                          <strong key={`${assignment.value}-${index}`}>
                            {assignment.value}
                          </strong>
                        ))
                      )}
                    </td>
                  ))}
                </tr>
                <tr>
                  <th>置信度</th>
                  {row.cells.map((cell) => (
                    <td key={groupIdentity(cell.group)}>
                      {cell.assignments.length === 0
                        ? "—"
                        : cell.assignments
                            .map((assignment) =>
                              percentage(assignment.confidence),
                            )
                            .join(" / ")}
                    </td>
                  ))}
                </tr>
                <tr>
                  <th>来源</th>
                  {row.cells.map((cell) => (
                    <td key={groupIdentity(cell.group)}>
                      {cell.group.source === "manual" ? "人工复核" : "模型规则"}
                    </td>
                  ))}
                </tr>
              </tbody>
            </table>
          </div>

          <div className="tig-evidence-heading">
            <h4>证据时间码</h4>
            <span>{evidence.length} 段</span>
          </div>
          {evidence.length === 0 ? (
            <p className="tig-inspector__no-evidence">
              当前标签单元没有可回放证据。
            </p>
          ) : (
            <ul className="tig-evidence-list">
              {evidence.map((item) => {
                const startMs = item.start_ms ?? 0;
                const targetReceptionId = receptionIdFromTarget(
                  receptionId,
                  row.target_id,
                );
                return (
                  <li key={`${item.ref_id}-${item.recording_id}`}>
                    <div>
                      <strong>
                        {item.kind === "audio" ? "原音" : "文本"} ·{" "}
                        {item.recording_id}
                      </strong>
                      <span>{formatClock(startMs / 1_000)}</span>
                    </div>
                    {item.text_excerpt && (
                      <blockquote>{item.text_excerpt}</blockquote>
                    )}
                    <Link
                      to={`/receptions/${encodeURIComponent(targetReceptionId)}/workspace?recording=${encodeURIComponent(item.recording_id)}&at=${startMs}`}
                    >
                      到调听工作台定位
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
    </aside>
  );
}

export function TagInsightGraph({
  result,
  receptionId,
}: TagInsightGraphProps) {
  const titleId = useId();
  const descriptionId = useId();
  const markerId = useId().replaceAll(":", "");
  const gridId = `${markerId}-grid`;
  const graph = useMemo(() => buildGraph(result), [result]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(
    () =>
      graph.nodes.find(
        (node) => node.kind === "focus" && node.status === "conflict",
      )?.id ??
      graph.nodes.find((node) => node.kind === "focus")?.id ??
      null,
  );
  const [focusedNodeId, setFocusedNodeId] = useState<string | null>(null);
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [selectedCommunity, setSelectedCommunity] = useState("");
  const [onlyConflicts, setOnlyConflicts] = useState(false);
  const [visibleKinds, setVisibleKinds] = useState<ReadonlySet<GraphNodeKind>>(
    () => new Set(["focus", "value", "evidence"]),
  );
  const [zoom, setZoom] = useState(1);
  const communityKeys = useMemo(
    () => [...new Set(graph.nodes.map((node) => node.community))],
    [graph.nodes],
  );
  useEffect(() => {
    setSelectedNodeId((current) => {
      if (
        current !== null &&
        graph.nodes.some((node) => node.id === current)
      ) {
        return current;
      }
      return (
        graph.nodes.find(
          (node) => node.kind === "focus" && node.status === "conflict",
        )?.id ??
        graph.nodes.find((node) => node.kind === "focus")?.id ??
        null
      );
    });
  }, [graph.nodes]);
  useEffect(() => {
    if (
      selectedCommunity !== "" &&
      !communityKeys.includes(selectedCommunity)
    ) {
      setSelectedCommunity("");
    }
  }, [communityKeys, selectedCommunity]);
  const visibleNodes = useMemo(
    () =>
      graph.nodes.filter(
        (node) =>
          (selectedCommunity === "" ||
            node.community === selectedCommunity) &&
          (!onlyConflicts || result.matrix[node.rowIndex]?.conflict) &&
          visibleKinds.has(node.kind),
      ),
    [
      graph.nodes,
      onlyConflicts,
      result.matrix,
      selectedCommunity,
      visibleKinds,
    ],
  );
  const visibleNodeIds = useMemo(
    () => new Set(visibleNodes.map((node) => node.id)),
    [visibleNodes],
  );
  const visibleEdges = useMemo(
    () =>
      graph.edges.filter(
        (edge) =>
          visibleNodeIds.has(edge.source) &&
          visibleNodeIds.has(edge.target),
      ),
    [graph.edges, visibleNodeIds],
  );
  const layout = useMemo(() => buildLayout(visibleNodes), [visibleNodes]);
  const selectedNode =
    visibleNodes.find((node) => node.id === selectedNodeId) ?? null;
  const selectedRow =
    selectedNode === null
      ? null
      : (result.matrix[selectedNode.rowIndex] ?? null);
  const hoveredNode =
    visibleNodes.find((node) => node.id === hoveredNodeId) ?? null;
  const visibleCommunities = new Set(
    visibleNodes.map((node) => node.community),
  ).size;
  const visibleConflicts = new Set(
    visibleNodes
      .filter((node) => result.matrix[node.rowIndex]?.conflict)
      .map((node) => node.rowIndex),
  ).size;
  const targetIds = [
    ...new Set(
      visibleNodes.map(
        (node) => result.matrix[node.rowIndex]?.target_id ?? node.subtitle,
      ),
    ),
  ];

  if (graph.nodes.length === 0) {
    return (
      <section role="status" className="tig-empty">
        <strong>暂无可构建的标签关系</strong>
        <span>至少需要一个已对齐的目标/标签单元。</span>
      </section>
    );
  }

  const chooseNode = (nodeId: string) => {
    setSelectedNodeId(nodeId);
  };
  const handleNodeKeyDown = (
    event: KeyboardEvent<SVGGElement>,
    nodeId: string,
  ) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    chooseNode(nodeId);
  };
  const toggleNodeKind = (kind: GraphNodeKind) => {
    setVisibleKinds((current) => {
      const next = new Set(current);
      if (next.has(kind)) {
        if (next.size === 1) return current;
        next.delete(kind);
      } else {
        next.add(kind);
      }
      return next;
    });
    setSelectedNodeId(null);
  };
  const resetView = () => {
    setSelectedCommunity("");
    setOnlyConflicts(false);
    setVisibleKinds(new Set(["focus", "value", "evidence"]));
    setZoom(1);
    setSelectedNodeId(null);
  };
  const focusNodes = visibleNodes.filter((node) => node.kind === "focus");
  const chooseCentralTarget = () => {
    if (focusNodes[0]) chooseNode(focusNodes[0].id);
  };
  const handleCentralKeyDown = (event: KeyboardEvent<SVGGElement>) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    chooseCentralTarget();
  };

  return (
    <section aria-labelledby={titleId} className="tig-shell">
      <header className="tig-header">
        <div className="tig-header__title">
          <span>
            <IconApps aria-hidden="true" />
            TAG INTELLIGENCE / TRACE MODE
          </span>
          <h2 id={titleId}>
            多标签组对比与溯源图
          </h2>
          <p id={descriptionId}>
            以目标标签为中心，比较多版本组值、社区关系与原始证据链。
          </p>
        </div>
        <dl className="tig-telemetry">
          <div>
            <dt>社区</dt>
            <dd>{visibleCommunities}</dd>
          </div>
          <div>
            <dt>节点</dt>
            <dd>{visibleNodes.length}</dd>
          </div>
          <div>
            <dt>关系</dt>
            <dd>{visibleEdges.length}</dd>
          </div>
          <div className={visibleConflicts > 0 ? "is-alert" : undefined}>
            <dt>冲突单元</dt>
            <dd>{visibleConflicts}</dd>
          </div>
        </dl>
      </header>

      <div className="tig-filterbar">
        <label>
          <span>标签社区</span>
          <select
            aria-label="标签社区筛选"
            value={selectedCommunity}
            onChange={(event) => {
              setSelectedCommunity(event.target.value);
              setSelectedNodeId(null);
            }}
          >
            <option value="">全部社区（{communityKeys.length}）</option>
            {communityKeys.map((community) => (
              <option key={community} value={community}>
                {community}
              </option>
            ))}
          </select>
        </label>
        <div
          className="tig-kind-filter"
          role="group"
          aria-label="节点类型筛选"
        >
          {(Object.keys(NODE_KIND_LABELS) as GraphNodeKind[]).map((kind) => (
            <button
              key={kind}
              type="button"
              aria-pressed={visibleKinds.has(kind)}
              onClick={() => toggleNodeKind(kind)}
            >
              {NODE_KIND_LABELS[kind]}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="tig-conflict-filter"
          aria-label="只看冲突标签"
          aria-pressed={onlyConflicts}
          onClick={() => {
            setOnlyConflicts((current) => !current);
            setSelectedNodeId(null);
          }}
        >
          <IconEye aria-hidden="true" />
          只看冲突
        </button>
        <button
          type="button"
          className="tig-reset-button"
          aria-label="重置图谱筛选"
          onClick={resetView}
        >
          <IconRefresh aria-hidden="true" />
          重置
        </button>
      </div>

      <div className="tig-workbench">
        <div className="tig-canvas-shell">
          <div className="tig-canvas-viewport">
          <svg
            viewBox={`0 0 ${GRAPH_WIDTH} ${layout.height}`}
            role="group"
            aria-labelledby={`${titleId} ${descriptionId}`}
            data-graph-mode="tag-comparison"
            data-layout="radial-community"
            data-node-budget={TAG_INSIGHT_GRAPH_NODE_BUDGET}
            data-node-count={visibleNodes.length}
            data-edge-count={visibleEdges.length}
            data-zoom={`${Math.round(zoom * 100)}%`}
            className="tig-graph-svg"
            style={{
              width: `${zoom * 100}%`,
              minWidth: `${760 * zoom}px`,
            }}
          >
            <title>多标签组对比与溯源图</title>
            <desc>
              共有 {visibleNodes.length} 个节点和 {visibleEdges.length} 条关系。
              可使用 Tab 逐个选择节点。
            </desc>
            <defs>
              <pattern
                id={gridId}
                width="24"
                height="24"
                patternUnits="userSpaceOnUse"
              >
                <circle cx="1" cy="1" r="0.8" fill="#C8D5E6" />
              </pattern>
              {(Object.keys(EDGE_APPEARANCE) as GraphEdgeKind[]).map(
                (kind) => (
                  <marker
                    key={kind}
                    id={`${markerId}-${EDGE_APPEARANCE[kind].marker}`}
                    markerWidth="8"
                    markerHeight="8"
                    refX="7"
                    refY="4"
                    orient="auto"
                  >
                    <path
                      d="M0,0 L8,4 L0,8 Z"
                      fill={EDGE_APPEARANCE[kind].color}
                    />
                  </marker>
                ),
              )}
              <marker
                id={`${markerId}-community`}
                markerWidth="7"
                markerHeight="7"
                refX="6"
                refY="3.5"
                orient="auto"
              >
                <path d="M0,0 L7,3.5 L0,7 Z" fill="#6F8FB8" />
              </marker>
            </defs>

            <rect
              width={GRAPH_WIDTH}
              height={layout.height}
              fill="transparent"
            />
            <rect
              width={GRAPH_WIDTH}
              height={layout.height}
              fill={`url(#${gridId})`}
              opacity="0.42"
            />
            {layout.communities.map((community) => (
              <g
                key={community.key}
                data-community-region={community.key}
                aria-hidden="true"
              >
                <ellipse
                  data-community-field={community.key}
                  cx={community.centerX}
                  cy={community.centerY}
                  rx={community.width / 2}
                  ry={community.height / 2}
                  fill={community.color.wash}
                  stroke={community.color.soft}
                  fillOpacity="0.72"
                  strokeWidth="1"
                  strokeDasharray="4 7"
                />
                <ellipse
                  cx={community.centerX}
                  cy={community.centerY}
                  rx={community.width / 2 + 11}
                  ry={community.height / 2 + 11}
                  fill="none"
                  stroke={community.color.soft}
                  strokeOpacity="0.58"
                />
                <ellipse
                  cx={community.centerX}
                  cy={community.centerY}
                  rx={community.width / 2 + 21}
                  ry={community.height / 2 + 21}
                  fill="none"
                  stroke={community.color.soft}
                  strokeOpacity="0.25"
                />
                <text
                  x={community.centerX}
                  y={community.y - 11}
                  fill={community.color.strong}
                  className="tig-community__label"
                  textAnchor="middle"
                >
                  {clipLabel(communityDisplayName(community.key), 18)}
                </text>
                <text
                  x={community.centerX}
                  y={community.y + 4}
                  className="tig-community__meta"
                  textAnchor="middle"
                >
                  {community.key} ·{" "}
                  {
                    visibleNodes.filter(
                      (node) => node.community === community.key,
                    ).length
                  }{" "}
                  节点
                </text>
              </g>
            ))}

            <g aria-hidden="true" className="tig-central-links">
              {layout.communities.flatMap((community) =>
                visibleNodes
                  .filter(
                    (node) =>
                      node.community === community.key &&
                      node.kind === "focus",
                  )
                  .slice(0, 1)
                  .map((node) => {
                    const target = layout.positions.get(node.id);
                    if (!target) return null;
                    const row = result.matrix[node.rowIndex];
                    return (
                      <path
                        key={`central::${node.id}`}
                        d={`M ${layout.root.x} ${layout.root.y} Q ${(layout.root.x + target.x) / 2} ${(layout.root.y + target.y) / 2 - 10} ${target.x} ${target.y}`}
                        stroke={community.color.strong}
                        strokeDasharray={row?.conflict ? "7 6" : "3 8"}
                        markerEnd={`url(#${markerId}-community)`}
                        data-central-link={node.id}
                      />
                    );
                  }),
              )}
            </g>

            <g aria-label="图谱关系">
              {visibleEdges.map((edge, edgeIndex) => {
                const source = layout.positions.get(edge.source);
                const target = layout.positions.get(edge.target);
                if (!source || !target) return null;
                const appearance = EDGE_APPEARANCE[edge.kind];
                const curve = curvedEdgePath(
                  source,
                  target,
                  edgeIndex,
                  edge.kind,
                );
                return (
                  <g key={edge.id}>
                    <path
                      d={curve.path}
                      stroke={appearance.color}
                      strokeWidth={edge.kind === "difference" ? 1.8 : 1.2}
                      strokeDasharray={appearance.dash}
                      strokeOpacity={
                        edge.kind === "evidence-ownership" ? 0.64 : 0.88
                      }
                      fill="none"
                      markerEnd={`url(#${markerId}-${appearance.marker})`}
                      vectorEffect="non-scaling-stroke"
                      data-edge-kind={edge.kind}
                      data-source={edge.source}
                      data-target={edge.target}
                      className="tig-edge"
                    >
                      <title>{edge.label}</title>
                    </path>
                    {edge.kind !== "evidence-ownership" && (
                      <text
                        x={curve.label.x}
                        y={curve.label.y - 5}
                        fill={appearance.color}
                        className="tig-edge__label"
                        textAnchor="middle"
                        paintOrder="stroke"
                        stroke="#FBFDFF"
                        strokeWidth="4"
                      >
                        {edge.label}
                      </text>
                    )}
                  </g>
                );
              })}
            </g>

            <g
              data-central-target
              role="button"
              tabIndex={0}
              aria-label={`中心目标标签，${targetIds.length} 个对话目标`}
              onClick={chooseCentralTarget}
              onKeyDown={handleCentralKeyDown}
              className="tig-central-target"
            >
              <circle
                cx={layout.root.x}
                cy={layout.root.y}
                r="82"
                className="tig-central-target__halo"
              />
              <circle
                cx={layout.root.x}
                cy={layout.root.y}
                r="68"
                className="tig-central-target__orbit"
              />
              <circle
                cx={layout.root.x}
                cy={layout.root.y}
                r="55"
                className="tig-central-target__orbit tig-central-target__orbit--inner"
              />
              <circle
                cx={layout.root.x}
                cy={layout.root.y}
                r="46"
                className="tig-central-target__body"
              />
              <circle
                cx={layout.root.x}
                cy={layout.root.y - 19}
                r="5"
                className="tig-central-target__signal"
              />
              <text
                x={layout.root.x}
                y={layout.root.y - 5}
                textAnchor="middle"
                className="tig-central-target__eyebrow"
              >
                TARGET HUB
              </text>
              <text
                x={layout.root.x}
                y={layout.root.y + 12}
                textAnchor="middle"
                className="tig-central-target__title"
              >
                目标标签集合
              </text>
              <text
                x={layout.root.x}
                y={layout.root.y + 29}
                textAnchor="middle"
                className="tig-central-target__meta"
              >
                {targetIds.length} 目标 · {visibleCommunities} 社区
              </text>
            </g>

            <g aria-label="图谱节点">
              {visibleNodes.map((node) => {
                const position = layout.positions.get(node.id);
                if (!position) return null;
                const color = communityColor(
                  layout.communities,
                  node.community,
                );
                const appearance = nodeAppearance(node, color);
                const selected = selectedNodeId === node.id;
                const focused = focusedNodeId === node.id;
                const { width, height } = nodeDimensions(node.kind);
                const community = layout.communities.find(
                  (item) => item.key === node.community,
                );
                const isCommunityHub =
                  community !== undefined &&
                  Math.abs(position.x - community.centerX) < 1 &&
                  Math.abs(position.y - community.centerY) < 1;
                const labelOnLeft =
                  community !== undefined &&
                  position.x < community.centerX - 2;
                const labelOffset =
                  node.kind === "focus"
                    ? width / 2 + 8
                    : width / 2 + 6;
                const labelX = isCommunityHub
                  ? 0
                  : labelOnLeft
                    ? -labelOffset
                    : labelOffset;
                const labelAnchor = isCommunityHub
                  ? "middle"
                  : labelOnLeft
                    ? "end"
                    : "start";
                const nodeStyle = {
                  "--tig-node-fill": appearance.fill,
                  "--tig-node-stroke": appearance.stroke,
                  "--tig-node-accent": color.strong,
                } as CSSProperties;
                return (
                  <g
                    key={node.id}
                    role="button"
                    tabIndex={0}
                    aria-label={nodeAriaLabel(node)}
                    aria-pressed={selected}
                    data-node-id={node.id}
                    data-node-kind={node.kind}
                    data-node-status={node.status}
                    data-community={node.community}
                    data-group-version={node.groupVersion}
                    data-node-value={
                      node.kind === "value" ? node.label : undefined
                    }
                    data-evidence-ref={node.evidence?.ref_id}
                    data-x={position.x}
                    data-y={position.y}
                    data-node-width={width}
                    data-node-height={height}
                    transform={`translate(${position.x} ${position.y})`}
                    onClick={() => chooseNode(node.id)}
                    onKeyDown={(event) => handleNodeKeyDown(event, node.id)}
                    onMouseEnter={() => setHoveredNodeId(node.id)}
                    onMouseLeave={() => setHoveredNodeId(null)}
                    onFocus={() => {
                      setFocusedNodeId(node.id);
                      setHoveredNodeId(node.id);
                    }}
                    onBlur={() => {
                      setFocusedNodeId(null);
                      setHoveredNodeId(null);
                    }}
                    className={`tig-node tig-node--${node.kind} tig-node--${node.status}${selected ? " is-selected" : ""}${focused ? " is-focused" : ""}`}
                    style={nodeStyle}
                  >
                    {(selected || focused) && (
                      <circle
                        cx="0"
                        cy="0"
                        r={width / 2 + 5}
                        fill="none"
                        stroke={selected ? "#165DFF" : "#6C8DC2"}
                        strokeWidth="2"
                        strokeDasharray={focused && !selected ? "3 2" : undefined}
                        className="tig-node__selection"
                      />
                    )}
                    {node.kind === "focus" && (
                      <circle
                        cx="0"
                        cy="0"
                        r={width / 2 + 4}
                        className="tig-node__orbit"
                      />
                    )}
                    <circle
                      data-node-glyph
                      cx="0"
                      cy="0"
                      r={width / 2}
                      strokeWidth={node.kind === "focus" ? 2 : 1.4}
                      className="tig-node__body"
                    />
                    <circle
                      cx={width / 2 - 2}
                      cy={-height / 2 + 2}
                      r={node.kind === "focus" ? 3.5 : 2.5}
                      className="tig-node__signal"
                    />
                    {node.kind === "value" && (
                      <text
                        x={labelX}
                        y="-1"
                        textAnchor={labelAnchor}
                        dominantBaseline="middle"
                        className="tig-node__title"
                      >
                        {clipLabel(node.label, 10)}
                      </text>
                    )}
                    <title>{nodeAriaLabel(node)}</title>
                  </g>
                );
              })}
            </g>
          </svg>
          </div>

          {visibleNodes.length === 0 && (
            <div className="tig-filter-empty" role="status">
              当前筛选没有可显示的标签节点，请调整社区或冲突条件。
            </div>
          )}

          {hoveredNode && (
            <div
              className="tig-hovercard"
              role="status"
              aria-label="节点悬浮信息"
            >
              <span>{NODE_KIND_LABELS[hoveredNode.kind]}</span>
              <strong>{hoveredNode.label}</strong>
              <small>{hoveredNode.subtitle}</small>
            </div>
          )}

          <div
            className="tig-legend"
            role="group"
            aria-label="图谱关系图例"
          >
            <strong>图例</strong>
            <div>
              {(
                [
                  ["evidence-ownership", "证据归属"],
                  ["co-occurrence", "标签共现"],
                  ["difference", "组间差异"],
                ] as const
              ).map(([kind, label]) => (
                <span key={kind}>
                  <i
                    className={
                      EDGE_APPEARANCE[kind].dash ? "is-dashed" : undefined
                    }
                  />
                  {label}
                </span>
              ))}
            </div>
            <div className="tig-legend__states">
              <span>
                <i className="is-agreement" /> 一致
              </span>
              <span>
                <i className="is-conflict" /> 冲突节点
              </span>
              <span>
                <i className="is-neutral" /> 缺失/证据
              </span>
            </div>
          </div>

          <div
            className="tig-zoom"
            role="group"
            aria-label="图谱缩放控制"
          >
            <button
              type="button"
              aria-label="缩小标签图谱"
              disabled={zoom <= 0.8}
              onClick={() =>
                setZoom((current) => Math.max(0.8, current - 0.1))
              }
            >
              <IconZoomOut aria-hidden="true" />
            </button>
            <button
              type="button"
              aria-label="重置标签图谱缩放"
              onClick={() => setZoom(1)}
            >
              {Math.round(zoom * 100)}%
            </button>
            <button
              type="button"
              aria-label="放大标签图谱"
              disabled={zoom >= 1.5}
              onClick={() =>
                setZoom((current) =>
                  Math.min(1.5, Math.round((current + 0.1) * 10) / 10),
                )
              }
            >
              <IconZoomIn aria-hidden="true" />
            </button>
          </div>
        </div>

        <DetailPanel
          node={selectedNode}
          row={selectedRow}
          receptionId={receptionId}
        />
      </div>

      {(graph.wasLimited || result.truncated || result.matrix_truncated) && (
        <p role="note" className="tig-limit-note">
          为保证交互性能，图谱最多展示{" "}
          {TAG_INSIGHT_GRAPH_NODE_BUDGET} 个节点；当前视图已按返回顺序截断。
          {result.matrix_truncated
            ? " 标签矩阵也已达到返回上限，请缩小筛选范围后重新分析。"
            : " 完整返回数据仍可在标签矩阵中查看。"}
        </p>
      )}
    </section>
  );
}
