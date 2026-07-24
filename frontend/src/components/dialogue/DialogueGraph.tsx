import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import {
  IconFullscreen,
  IconRefresh,
  IconZoomIn,
  IconZoomOut,
} from "@arco-design/web-react/icon";
import type { ReceptionWorkspaceResponse } from "@/types/api";
import "./DialogueGraph.css";

export type DialogueGraphMode =
  | "relation"
  | "temporal"
  | "state"
  | "provenance";

export interface DialogueGraphNodeDetails {
  id: string;
  label: string;
  kind: string;
  community: string;
}

export interface DialogueGraphHighlightedTransition {
  fromState: string;
  toState: string;
}

interface DialogueGraphProps {
  workspace: ReceptionWorkspaceResponse;
  mode: DialogueGraphMode;
  highlightedTransition?: DialogueGraphHighlightedTransition | null;
  /** Called after pointer or keyboard activation so a host page can open details. */
  onNodeDetails?: (node: DialogueGraphNodeDetails) => void;
}

type VisualNode = DialogueGraphNodeDetails;

interface VisualEdge {
  source: string;
  target: string;
  label: string;
}

interface GraphData {
  nodes: VisualNode[];
  edges: VisualEdge[];
  emptyReason?: string;
  truncated: boolean;
}

interface Point {
  x: number;
  y: number;
}

interface VisualRegion {
  id: string;
  label: string;
  community: string;
  memberCommunities?: string[];
  visualGroup?: string;
  embedded?: boolean;
  x: number;
  y: number;
  width: number;
  height: number;
}

interface GraphLayout {
  positions: Map<string, Point>;
  regions: VisualRegion[];
}

const MAX_GRAPH_NODES = 36;
const HIGHLIGHTED_EDGE_COLOR = "#C94D32";
const GRAPH_VIEWBOX_WIDTH = 1000;
const GRAPH_VIEWBOX_HEIGHT = 640;
const MIN_ZOOM = 0.8;
const MAX_ZOOM = 1.4;
const ZOOM_STEP = 0.1;

function compareText(left: string | number, right: string | number): number {
  const leftText = String(left);
  const rightText = String(right);
  if (leftText === rightText) return 0;
  return leftText < rightText ? -1 : 1;
}

function communityToken(value: string): string {
  return (
    value
      .trim()
      .toLowerCase()
      .replace(/[^\p{L}\p{N}_-]+/gu, "-")
      .replace(/^-+|-+$/g, "") || "other"
  );
}

function stateNodeId(state: string): string {
  return `state-${state}`;
}

function finalizeGraph(
  nodes: VisualNode[],
  edges: VisualEdge[],
  emptyReason?: string,
  preferredNodeIds: string[] = [],
): GraphData {
  const uniqueNodes = [...new Map(nodes.map((node) => [node.id, node])).values()];
  const nodesById = new Map(uniqueNodes.map((node) => [node.id, node]));
  const preferredNodes = [...new Set(preferredNodeIds)]
    .map((nodeId) => nodesById.get(nodeId))
    .filter((node): node is VisualNode => Boolean(node));
  const preferredIds = new Set(preferredNodes.map((node) => node.id));
  const visibleNodes = [
    ...preferredNodes,
    ...uniqueNodes.filter((node) => !preferredIds.has(node.id)),
  ].slice(0, MAX_GRAPH_NODES);
  const nodeIds = new Set(visibleNodes.map((node) => node.id));
  return {
    nodes: visibleNodes,
    edges: edges.filter(
      (edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target),
    ),
    emptyReason,
    truncated: uniqueNodes.length > MAX_GRAPH_NODES,
  };
}

function buildRelationGraph(workspace: ReceptionWorkspaceResponse): GraphData {
  const receptionId = `reception-${workspace.reception.id}`;
  const nodes: VisualNode[] = [
    {
      id: receptionId,
      label: `接待 ${workspace.reception.id}`,
      kind: "reception",
      community: "reception-core",
    },
  ];
  const edges: VisualEdge[] = [];
  [...workspace.recordings]
    .sort(
      (left, right) =>
        left.sequence_no - right.sequence_no ||
        compareText(left.id, right.id),
    )
    .forEach((recording) => {
      const id = `recording-${recording.id}`;
      nodes.push({
        id,
        label: recording.name,
        kind: "recording",
        community: "recordings",
      });
      edges.push({ source: receptionId, target: id, label: "包含原音" });
    });
  [...workspace.dialogue_units]
    .sort(
      (left, right) =>
        left.unit_index - right.unit_index || compareText(left.id, right.id),
    )
    .forEach((unit) => {
      const id = `unit-${unit.id}`;
      nodes.push({
        id,
        label: unit.topic ?? `对话单元 ${unit.unit_index + 1}`,
        kind: "unit",
        community: "dialogue-units",
      });
      edges.push({ source: receptionId, target: id, label: "切分" });
    });
  [...workspace.tag_assignments]
    .sort(
      (left, right) =>
        compareText(left.group_key, right.group_key) ||
        compareText(left.label_key, right.label_key) ||
        compareText(left.id, right.id),
    )
    .forEach((tag) => {
      const id = `tag-${tag.id}`;
      const unitId = `unit-${tag.dialogue_unit_id}`;
      nodes.push({
        id,
        label: `${tag.label_key}: ${tag.label_value}`,
        kind: "tag",
        community: `tags-${communityToken(tag.label_key)}`,
      });
      edges.push({ source: unitId, target: id, label: "标注" });
    });
  return finalizeGraph(nodes, edges);
}

function buildTemporalGraph(workspace: ReceptionWorkspaceResponse): GraphData {
  const versionedUnits = workspace.dialogue_units.filter(
    (unit) => unit.version > 1 || unit.edit_status !== "auto",
  );
  if (versionedUnits.length === 0 && workspace.audit_events.length === 0) {
    return finalizeGraph([], [], "暂无跨版本差分或人工变更数据");
  }
  const nodes: VisualNode[] = [
    {
      id: `reception-v${workspace.reception.version}`,
      label: `接待 v${workspace.reception.version}`,
      kind: "reception",
      community: "reception-version",
    },
  ];
  const edges: VisualEdge[] = [];
  [...workspace.audit_events]
    .sort(
      (left, right) =>
        compareText(left.occurred_at, right.occurred_at) ||
        compareText(left.id, right.id),
    )
    .forEach((event) => {
      const id = `audit-${event.id}`;
      nodes.push({
        id,
        label: event.action,
        kind: "audit",
        community: "change-events",
      });
      edges.push({
        source: id,
        target: `reception-v${workspace.reception.version}`,
        label: "形成版本",
      });
    });
  [...versionedUnits]
    .sort(
      (left, right) =>
        left.unit_index - right.unit_index ||
        left.version - right.version ||
        compareText(left.id, right.id),
    )
    .forEach((unit) => {
      const id = `unit-${unit.id}-v${unit.version}`;
      nodes.push({
        id,
        label: `${unit.topic ?? `单元 ${unit.unit_index + 1}`} · v${unit.version}`,
        kind: unit.edit_status === "auto" ? "unit" : "manual",
        community: "changed-units",
      });
      edges.push({
        source: `reception-v${workspace.reception.version}`,
        target: id,
        label: unit.edit_status,
      });
    });
  return finalizeGraph(nodes, edges);
}

function buildStateGraph(
  workspace: ReceptionWorkspaceResponse,
  highlightedTransition?: DialogueGraphHighlightedTransition | null,
): GraphData {
  if (workspace.state_transitions.length === 0) {
    return finalizeGraph([], [], "暂无对话状态转移数据");
  }
  const nodes: VisualNode[] = [];
  const edges: VisualEdge[] = [];
  [...workspace.state_transitions]
    .sort(
      (left, right) =>
        left.sequence_no - right.sequence_no ||
        compareText(left.id, right.id),
    )
    .forEach((transition) => {
      const source = stateNodeId(transition.from_state);
      const target = stateNodeId(transition.to_state);
      nodes.push(
        {
          id: source,
          label: transition.from_state,
          kind: "state",
          community: "dialogue-states",
        },
        {
          id: target,
          label: transition.to_state,
          kind: "state",
          community: "dialogue-states",
        },
      );
      edges.push({
        source,
        target,
        label: `${transition.trigger} · ${Math.round(transition.confidence * 100)}%`,
      });
    });
  return finalizeGraph(
    nodes,
    edges,
    undefined,
    highlightedTransition
      ? [
          stateNodeId(highlightedTransition.fromState),
          stateNodeId(highlightedTransition.toState),
        ]
      : [],
  );
}

function buildProvenanceGraph(
  workspace: ReceptionWorkspaceResponse,
): GraphData {
  const nodes: VisualNode[] = [];
  const edges: VisualEdge[] = [];

  [...workspace.audit_events]
    .sort(
      (left, right) =>
        compareText(left.occurred_at, right.occurred_at) ||
        compareText(left.id, right.id),
    )
    .forEach((event) => {
      const eventId = `event-${event.id}`;
      const objectType = event.object_type ?? "object";
      const objectRef = event.object_ref ?? String(workspace.reception.id);
      const objectId = `${objectType}-${objectRef}`;
      nodes.push(
        {
          id: eventId,
          label: `${event.action} · ${event.actor ?? "系统"}`,
          kind: "audit",
          community: "events",
        },
        {
          id: objectId,
          label: `${objectType} ${objectRef}`,
          kind: objectType,
          community: "objects",
        },
      );
      edges.push({ source: eventId, target: objectId, label: event.action });

      (event.parent_refs ?? []).forEach((reference, index) => {
        const type =
          typeof reference.type === "string"
            ? reference.type
            : typeof reference.object_type === "string"
              ? reference.object_type
              : "parent";
        const rawId =
          reference.id ??
          reference.object_ref ??
          reference.recording_id ??
          reference.segment_id ??
          index;
        const version =
          typeof reference.version === "number" ||
          typeof reference.version === "string"
            ? String(reference.version)
            : null;
        const parentId = `${type}-${String(rawId)}${version ? `-v${version}` : ""}`;
        nodes.push({
          id: parentId,
          label: `${type} ${String(rawId)}${version ? ` · v${version}` : ""}`,
          kind: type,
          community: "sources",
        });
        edges.push({ source: parentId, target: eventId, label: "父项 / 来源" });
      });

      (event.evidence_refs ?? []).forEach((evidence) => {
        const evidenceId = `evidence-${event.id}-${evidence.ref_id}`;
        nodes.push({
          id: evidenceId,
          label:
            evidence.text_excerpt ??
            `${evidence.recording_id} @ ${(
              (evidence.timeline_start_ms ?? evidence.start_ms ?? 0) / 1000
            ).toFixed(1)}s`,
          kind: "evidence",
          community: "sources",
        });
        edges.push({ source: evidenceId, target: eventId, label: "证据" });
      });
    });

  if (nodes.length === 0) {
    return finalizeGraph([], [], "暂无持久化溯源事件");
  }
  return finalizeGraph(nodes, edges);
}

function buildGraph(
  workspace: ReceptionWorkspaceResponse,
  mode: DialogueGraphMode,
  highlightedTransition?: DialogueGraphHighlightedTransition | null,
): GraphData {
  if (mode === "relation") return buildRelationGraph(workspace);
  if (mode === "temporal") return buildTemporalGraph(workspace);
  if (mode === "state") {
    return buildStateGraph(workspace, highlightedTransition);
  }
  return buildProvenanceGraph(workspace);
}

function spread(index: number, count: number, start: number, end: number) {
  if (count <= 1) return Math.round((start + end) / 2);
  return Math.round(start + (index * (end - start)) / (count - 1));
}

function placeCommunityNodes(
  positions: Map<string, Point>,
  nodes: VisualNode[],
  region: VisualRegion,
) {
  if (nodes.length === 0) return;

  const centerX = region.x + region.width / 2;
  const centerY = region.y + region.height / 2;
  if (nodes.length === 1) {
    positions.set(nodes[0].id, { x: centerX, y: centerY });
    return;
  }

  if (nodes.length <= 6) {
    const radiusX = Math.max(34, region.width * 0.26);
    const radiusY = Math.max(26, region.height * 0.25);
    const startAngle = nodes.length === 2 ? Math.PI : -Math.PI * 0.72;
    const endAngle = nodes.length === 2 ? 0 : Math.PI * 0.72;
    nodes.forEach((node, index) => {
      const angle =
        nodes.length === 2
          ? index === 0
            ? startAngle
            : endAngle
          : startAngle +
            (index * (endAngle - startAngle)) / Math.max(1, nodes.length - 1);
      positions.set(node.id, {
        x: Math.round(centerX + Math.cos(angle) * radiusX),
        y: Math.round(centerY + Math.sin(angle) * radiusY),
      });
    });
    return;
  }

  const outerCount = Math.max(5, Math.ceil(nodes.length * 0.62));
  const innerCount = nodes.length - outerCount;
  nodes.forEach((node, index) => {
    const onOuterRing = index < outerCount;
    const ringIndex = onOuterRing ? index : index - outerCount;
    const ringCount = onOuterRing ? outerCount : innerCount;
    const angle =
      -Math.PI / 2 +
      (ringIndex * Math.PI * 2) / Math.max(1, ringCount);
    const radiusX = region.width * (onOuterRing ? 0.34 : 0.18);
    const radiusY = region.height * (onOuterRing ? 0.34 : 0.18);
    positions.set(node.id, {
      x: Math.round(centerX + Math.cos(angle) * radiusX),
      y: Math.round(centerY + Math.sin(angle) * radiusY),
    });
  });
}

function relationLayout(nodes: VisualNode[]): GraphLayout {
  const positions = new Map<string, Point>();
  const baseRegions: VisualRegion[] = [
    {
      id: "recordings",
      label: "源录音",
      community: "recordings",
      x: 44,
      y: 70,
      width: 220,
      height: 160,
    },
    {
      id: "reception-core",
      label: "本次接待（核心社群）",
      community: "reception-core",
      x: 342,
      y: 172,
      width: 316,
      height: 286,
    },
    {
      id: "dialogue-units",
      label: "对话单元",
      community: "dialogue-units",
      visualGroup: "reception-core",
      embedded: true,
      x: 374,
      y: 218,
      width: 252,
      height: 196,
    },
  ];
  const tagAnchors = [
    { x: 736, y: 60, width: 224, height: 164 },
    { x: 754, y: 260, width: 216, height: 172 },
    { x: 658, y: 454, width: 236, height: 150 },
    { x: 344, y: 470, width: 228, height: 136 },
    { x: 54, y: 412, width: 226, height: 164 },
    { x: 66, y: 244, width: 216, height: 150 },
  ];
  const tagCommunities = [
    ...new Set(
      nodes.filter((node) => node.kind === "tag").map((node) => node.community),
    ),
  ];
  const tagRegionLabel = (community: string): string => {
    const key = community.replace(/^tags-/, "");
    const labels: Record<string, string> = {
      intent: "成交信号",
      "intent-level": "成交信号",
      "next-step": "下一步行动",
      "sales-action": "销售动作",
      stage: "对话阶段",
      need: "客户需求",
      "customer-need": "客户需求",
      "need-occasion": "客户需求",
      preference: "商品偏好",
      "product-preference": "商品偏好",
      objection: "成交阻力",
      "objection-price": "价格异议",
      service: "服务承诺",
      competitor: "竞品对比",
    };
    return labels[key] ?? key.replaceAll("-", " ");
  };
  const hasOverflowCommunity = tagCommunities.length > tagAnchors.length;
  const dedicatedCommunities = hasOverflowCommunity
    ? tagCommunities.slice(0, tagAnchors.length - 1)
    : tagCommunities;
  const overflowCommunities = hasOverflowCommunity
    ? tagCommunities.slice(tagAnchors.length - 1)
    : [];
  const tagRegions: VisualRegion[] = dedicatedCommunities.map(
    (community, index) => ({
      id: community,
      label: tagRegionLabel(community),
      community,
      ...tagAnchors[index],
    }),
  );
  if (overflowCommunities.length > 0) {
    tagRegions.push({
      id: "tags-overflow",
      label: `其他目标标签（${overflowCommunities.length} 组）`,
      community: "tags-overflow",
      memberCommunities: overflowCommunities,
      ...tagAnchors[tagAnchors.length - 1],
    });
  }
  const regions = [...baseRegions, ...tagRegions];
  const reception = nodes.filter(
    (node) => node.community === "reception-core",
  );
  const recordings = nodes.filter(
    (node) => node.community === "recordings",
  );
  const units = nodes.filter(
    (node) => node.community === "dialogue-units",
  );
  placeCommunityNodes(positions, recordings, baseRegions[0]);
  placeCommunityNodes(positions, reception, baseRegions[1]);
  placeCommunityNodes(positions, units, baseRegions[2]);
  tagRegions.forEach((region) => {
    placeCommunityNodes(
      positions,
      nodes.filter((node) => nodeBelongsToRegion(node, region)),
      region,
    );
  });

  return { positions, regions };
}

function temporalLayout(nodes: VisualNode[]): GraphLayout {
  const positions = new Map<string, Point>();
  const regions: VisualRegion[] = [
    {
      id: "change-events",
      label: "变更事件",
      community: "change-events",
      x: 52,
      y: 184,
      width: 258,
      height: 236,
    },
    {
      id: "reception-version",
      label: "接待版本",
      community: "reception-version",
      x: 326,
      y: 142,
      width: 352,
      height: 316,
    },
    {
      id: "changed-units",
      label: "受影响单元",
      community: "changed-units",
      x: 716,
      y: 150,
      width: 246,
      height: 306,
    },
  ];
  placeCommunityNodes(
    positions,
    nodes.filter((node) => node.community === "change-events"),
    regions[0],
  );
  placeCommunityNodes(
    positions,
    nodes.filter((node) => node.community === "reception-version"),
    regions[1],
  );
  placeCommunityNodes(
    positions,
    nodes.filter((node) => node.community === "changed-units"),
    regions[2],
  );
  return { positions, regions };
}

function stateLayout(nodes: VisualNode[]): GraphLayout {
  const positions = new Map<string, Point>();
  const regions: VisualRegion[] = [
    {
      id: "dialogue-states",
      label: "销售对话状态流",
      community: "dialogue-states",
      x: 80,
      y: 76,
      width: 840,
      height: 484,
    },
  ];
  const columns = Math.min(6, nodes.length);
  const rows = Math.ceil(nodes.length / Math.max(1, columns));
  nodes.forEach((node, index) => {
    const column = index % columns;
    const row = Math.floor(index / columns);
    positions.set(node.id, {
      x: spread(column, columns, 105, 895),
      y: spread(row, rows, 225, 425),
    });
  });
  return { positions, regions };
}

function provenanceLayout(nodes: VisualNode[]): GraphLayout {
  const positions = new Map<string, Point>();
  const regions: VisualRegion[] = [
    {
      id: "sources",
      label: "原始证据 / 父项",
      community: "sources",
      x: 48,
      y: 150,
      width: 272,
      height: 304,
    },
    {
      id: "events",
      label: "处理事件",
      community: "events",
      x: 326,
      y: 142,
      width: 352,
      height: 316,
    },
    {
      id: "objects",
      label: "产出对象",
      community: "objects",
      x: 716,
      y: 174,
      width: 246,
      height: 260,
    },
  ];
  placeCommunityNodes(
    positions,
    nodes.filter((node) => node.community === "sources"),
    regions[0],
  );
  placeCommunityNodes(
    positions,
    nodes.filter((node) => node.community === "events"),
    regions[1],
  );
  placeCommunityNodes(
    positions,
    nodes.filter((node) => node.community === "objects"),
    regions[2],
  );
  return { positions, regions };
}

function buildLayout(nodes: VisualNode[], mode: DialogueGraphMode): GraphLayout {
  if (mode === "relation") return relationLayout(nodes);
  if (mode === "temporal") return temporalLayout(nodes);
  if (mode === "state") return stateLayout(nodes);
  return provenanceLayout(nodes);
}

function nodeBelongsToRegion(
  node: VisualNode,
  region: VisualRegion,
): boolean {
  return (
    node.community === region.community ||
    region.memberCommunities?.includes(node.community) === true
  );
}

function regionIdForNode(
  node: VisualNode,
  regions: VisualRegion[],
): string {
  const region = regions.find((candidate) =>
    nodeBelongsToRegion(node, candidate),
  );
  return region?.visualGroup ?? region?.id ?? node.community;
}

function communityLinks(
  nodes: VisualNode[],
  layout: GraphLayout,
  mode: DialogueGraphMode,
): Array<{ source: Point; target: Point; key: string }> {
  if (mode === "state") return [];

  return layout.regions.flatMap((region) => {
    const communityNodes = nodes.filter((node) =>
      nodeBelongsToRegion(node, region),
    );
    if (communityNodes.length < 2) return [];

    const links = communityNodes.slice(1).flatMap((node, index) => {
      const sourceNode = communityNodes[index];
      const source = sourceNode
        ? layout.positions.get(sourceNode.id)
        : undefined;
      const target = layout.positions.get(node.id);
      return source && target
        ? [{ source, target, key: `${region.id}-${index}` }]
        : [];
    });
    if (communityNodes.length > 2 && communityNodes.length <= 6) {
      const first = layout.positions.get(communityNodes[0]?.id ?? "");
      const last = layout.positions.get(
        communityNodes[communityNodes.length - 1]?.id ?? "",
      );
      if (first && last) {
        links.push({
          source: last,
          target: first,
          key: `${region.id}-close`,
        });
      }
    }
    return links;
  });
}

function edgePath(
  source: Point,
  target: Point,
  index: number,
  crossesCommunity: boolean,
): string {
  if (!crossesCommunity) {
    return `M ${source.x} ${source.y} L ${target.x} ${target.y}`;
  }
  const deltaX = target.x - source.x;
  const deltaY = target.y - source.y;
  const distance = Math.max(1, Math.hypot(deltaX, deltaY));
  const bend = 24 + (index % 3) * 8;
  const direction = index % 2 === 0 ? 1 : -1;
  const controlX =
    (source.x + target.x) / 2 -
    (deltaY / distance) * bend * direction;
  const controlY =
    (source.y + target.y) / 2 +
    (deltaX / distance) * bend * direction;
  return `M ${source.x} ${source.y} Q ${Math.round(controlX)} ${Math.round(
    controlY,
  )} ${target.x} ${target.y}`;
}

function displayLabel(label: string): string {
  return label.length > 14 ? `${label.slice(0, 13).trim()}…` : label;
}

function labelCardWidth(label: string): number {
  const visualUnits = [...label].reduce(
    (total, character) =>
      total + (/[\u2E80-\u9FFF]/u.test(character) ? 1.7 : 1),
    0,
  );
  return Math.min(132, Math.max(52, Math.round(visualUnits * 6.2 + 16)));
}

function representativeNodeIds(
  nodes: VisualNode[],
  layout: GraphLayout,
): Set<string> {
  const ids = new Set<string>();
  layout.regions.forEach((region) => {
    const communityNodes = nodes.filter((node) =>
      nodeBelongsToRegion(node, region),
    );
    const labelBudget =
      region.community === "dialogue-states" ? 10 : 5;
    if (communityNodes.length <= labelBudget) {
      communityNodes.forEach((node) => ids.add(node.id));
      return;
    }
    for (let index = 0; index < labelBudget; index += 1) {
      const representativeIndex = Math.floor(
        (index * communityNodes.length) / labelBudget,
      );
      const node = communityNodes[representativeIndex];
      if (node) ids.add(node.id);
    }
  });
  return ids;
}

function activateWithKeyboard(
  event: KeyboardEvent<SVGGElement>,
  activate: () => void,
) {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  activate();
}

export function DialogueGraph({
  workspace,
  mode,
  highlightedTransition,
  onNodeDetails,
}: DialogueGraphProps) {
  const graphRootRef = useRef<HTMLDivElement>(null);
  const [selection, setSelection] = useState<{
    mode: DialogueGraphMode;
    nodeId: string;
  } | null>(null);
  const [zoom, setZoom] = useState(1);
  const highlightedFromState = highlightedTransition?.fromState;
  const highlightedToState = highlightedTransition?.toState;
  const graph = useMemo(
    () =>
      buildGraph(
        workspace,
        mode,
        highlightedFromState && highlightedToState
          ? {
              fromState: highlightedFromState,
              toState: highlightedToState,
            }
          : null,
      ),
    [highlightedFromState, highlightedToState, mode, workspace],
  );
  const layout = useMemo(
    () => buildLayout(graph.nodes, mode),
    [graph.nodes, mode],
  );
  const ambientLinks = useMemo(
    () => communityLinks(graph.nodes, layout, mode),
    [graph.nodes, layout, mode],
  );
  const nodesById = useMemo(
    () => new Map(graph.nodes.map((node) => [node.id, node])),
    [graph.nodes],
  );
  const primaryCrossEdges = useMemo(() => {
    const pairs = new Set<string>();
    const indexes = new Set<number>();
    graph.edges.forEach((edge, index) => {
      const sourceNode = nodesById.get(edge.source);
      const targetNode = nodesById.get(edge.target);
      if (!sourceNode || !targetNode) return;
      const sourceRegion = regionIdForNode(sourceNode, layout.regions);
      const targetRegion = regionIdForNode(targetNode, layout.regions);
      if (sourceRegion === targetRegion) return;
      const pair = `${sourceRegion}->${targetRegion}`;
      if (pairs.has(pair)) return;
      pairs.add(pair);
      if (indexes.size >= 2) return;
      indexes.add(index);
    });
    return indexes;
  }, [graph.edges, layout.regions, nodesById]);
  const labelledNodeIds = useMemo(
    () => representativeNodeIds(graph.nodes, layout),
    [graph.nodes, layout],
  );
  const highlightedEdge =
    mode === "state" && highlightedTransition
      ? {
          source: stateNodeId(highlightedTransition.fromState),
          target: stateNodeId(highlightedTransition.toState),
        }
      : null;
  const hasHighlightedEdge = Boolean(
    highlightedEdge &&
      graph.edges.some(
        (edge) =>
          edge.source === highlightedEdge.source &&
          edge.target === highlightedEdge.target,
      ),
  );
  const accessibleGraphLabel = `${mode} 对话图谱，共 ${graph.nodes.length} 个节点、${graph.edges.length} 条关系${
    hasHighlightedEdge && highlightedTransition
      ? `，已高亮 ${highlightedTransition.fromState} 到 ${highlightedTransition.toState}`
      : ""
  }`;

  const activateNode = (node: VisualNode) => {
    setSelection({ mode, nodeId: node.id });
    onNodeDetails?.(node);
  };

  const changeZoom = (delta: number) => {
    setZoom((current) =>
      Math.min(
        MAX_ZOOM,
        Math.max(MIN_ZOOM, Number((current + delta).toFixed(1))),
      ),
    );
  };

  const toggleFullscreen = () => {
    const root = graphRootRef.current;
    if (!root) return;
    if (
      document.fullscreenElement &&
      typeof document.exitFullscreen === "function"
    ) {
      void document.exitFullscreen();
      return;
    }
    if (typeof root.requestFullscreen === "function") {
      void root.requestFullscreen();
    }
  };

  useEffect(() => {
    setZoom(1);
  }, [mode]);

  if (graph.nodes.length === 0) {
    return (
      <div className="ag-graph-empty" role="status">
        <strong>当前模式无可视化数据</strong>
        <span>{graph.emptyReason}</span>
      </div>
    );
  }

  return (
    <div
      ref={graphRootRef}
      className={`ag-dialogue-graph ag-dialogue-graph--${mode}`}
      data-layout-mode={mode}
      data-zoom={zoom.toFixed(1)}
    >
      {mode === "relation" && (
        <span className="ag-dialogue-graph__sr-label">目标标签</span>
      )}
      <svg
        viewBox={`0 0 ${GRAPH_VIEWBOX_WIDTH} ${GRAPH_VIEWBOX_HEIGHT}`}
        role="group"
        data-layout-mode={mode}
        data-zoom={zoom.toFixed(1)}
        aria-label={accessibleGraphLabel}
      >
        <defs>
          <marker
            id={`ag-arrow-${mode}`}
            markerWidth="8"
            markerHeight="8"
            refX="7"
            refY="4"
            orient="auto"
          >
            <path
              className="ag-graph-arrow"
              d="M0,0 L8,4 L0,8 Z"
            />
          </marker>
          <marker
            id={`ag-arrow-${mode}-highlighted`}
            markerWidth="10"
            markerHeight="10"
            refX="9"
            refY="5"
            orient="auto"
          >
            <path
              className="ag-graph-arrow ag-graph-arrow--highlighted"
              d="M0,0 L10,5 L0,10 Z"
            />
          </marker>
        </defs>
        {hasHighlightedEdge && highlightedTransition && (
          <desc>
            当前从聚合视图定位到 {highlightedTransition.fromState} 到{" "}
            {highlightedTransition.toState} 的状态转移。
          </desc>
        )}
        <g
          className="ag-dialogue-graph__viewport"
          transform={`translate(${GRAPH_VIEWBOX_WIDTH / 2} ${
            GRAPH_VIEWBOX_HEIGHT / 2
          }) scale(${zoom}) translate(${-GRAPH_VIEWBOX_WIDTH / 2} ${
            -GRAPH_VIEWBOX_HEIGHT / 2
          })`}
        >
          <g className="ag-graph-regions" aria-hidden="true">
            {layout.regions.map((region) => {
              const centerX = region.x + region.width / 2;
              const centerY = region.y + region.height / 2;
              const radiusX = region.width / 2;
              const radiusY = region.height / 2;
              const visualGroup = region.visualGroup ?? region.id;
              const nodeCount = graph.nodes.filter((node) =>
                regionIdForNode(node, layout.regions) === visualGroup,
              ).length;
              return (
                <g
                  key={region.id}
                  className={`ag-graph-community${
                    region.embedded
                      ? " ag-graph-community--embedded"
                      : ""
                  }`}
                  data-community={region.community}
                  data-node-count={nodeCount}
                >
                  {[26, 18, 10].map((offset) => (
                    <ellipse
                      key={offset}
                      className="ag-graph-community__contour"
                      cx={centerX}
                      cy={centerY}
                      rx={radiusX + offset}
                      ry={radiusY + offset}
                    />
                  ))}
                  <ellipse
                    className="ag-graph-community__surface"
                    cx={centerX}
                    cy={centerY}
                    rx={radiusX}
                    ry={radiusY}
                  />
                  <text
                    className="ag-graph-community__label"
                    x={centerX}
                    y={Math.max(28, region.y - 18)}
                    textAnchor="middle"
                  >
                    {region.label}
                  </text>
                  <text
                    className="ag-graph-community__count"
                    x={centerX}
                    y={Math.max(44, region.y - 2)}
                    textAnchor="middle"
                  >
                    {nodeCount} 个节点
                  </text>
                </g>
              );
            })}
          </g>

          <g className="ag-graph-community-links" aria-hidden="true">
            {ambientLinks.map((link) => (
              <line
                key={link.key}
                className="ag-graph-community-link"
                x1={link.source.x}
                y1={link.source.y}
                x2={link.target.x}
                y2={link.target.y}
              />
            ))}
          </g>

          <g className="ag-graph-edges" aria-hidden="true">
            {graph.edges.map((edge, index) => {
              const source = layout.positions.get(edge.source);
              const target = layout.positions.get(edge.target);
              const sourceNode = nodesById.get(edge.source);
              const targetNode = nodesById.get(edge.target);
              if (!source || !target || !sourceNode || !targetNode) return null;
              const crossesCommunity =
                regionIdForNode(sourceNode, layout.regions) !==
                regionIdForNode(targetNode, layout.regions);
              const isPrimaryCrossEdge =
                crossesCommunity && primaryCrossEdges.has(index);
              const isHighlighted =
                hasHighlightedEdge &&
                highlightedEdge?.source === edge.source &&
                highlightedEdge.target === edge.target;
              const midX = (source.x + target.x) / 2;
              const midY = (source.y + target.y) / 2;
              const highlightedLabel = displayLabel(edge.label);
              const visibleLabel = isHighlighted
                ? `${highlightedLabel} · 当前路径`
                : edge.label.slice(0, 20);
              const showLabel =
                isHighlighted ||
                mode !== "relation" ||
                edge.label === "包含原音";
              const edgeLabelWidth = labelCardWidth(visibleLabel);
              return (
                <g key={`${edge.source}-${edge.target}-${index}`}>
                  <path
                    className={`ag-graph-edge ${
                      crossesCommunity
                        ? "ag-graph-edge--cross-community"
                        : "ag-graph-edge--intra-community"
                    }${
                      isPrimaryCrossEdge
                        ? " ag-graph-edge--primary-bridge"
                        : crossesCommunity
                          ? " ag-graph-edge--secondary-bridge"
                          : ""
                    }${
                      isHighlighted ? " ag-graph-edge--highlighted" : ""
                    }`}
                    data-source={edge.source}
                    data-target={edge.target}
                    data-highlighted={isHighlighted}
                    d={edgePath(
                      source,
                      target,
                      index,
                      crossesCommunity,
                    )}
                    fill="none"
                    stroke={
                      isHighlighted ? HIGHLIGHTED_EDGE_COLOR : undefined
                    }
                    strokeWidth={isHighlighted ? 4 : undefined}
                    strokeDasharray={isHighlighted ? "none" : undefined}
                    markerEnd={
                      isHighlighted ||
                      isPrimaryCrossEdge ||
                      !crossesCommunity
                        ? `url(#ag-arrow-${mode}${
                            isHighlighted ? "-highlighted" : ""
                          })`
                        : undefined
                    }
                    style={
                      isHighlighted
                        ? {
                            stroke: HIGHLIGHTED_EDGE_COLOR,
                            strokeWidth: 4,
                            strokeDasharray: "none",
                          }
                        : undefined
                    }
                  >
                    <title>
                      {isHighlighted
                        ? `当前路径：${edge.label}`
                        : edge.label}
                    </title>
                  </path>
                  {showLabel && (
                    <g
                      className={`ag-graph-edge-label-card${
                        isHighlighted
                          ? " ag-graph-edge-label-card--highlighted"
                          : ""
                      }`}
                      transform={`translate(${midX} ${midY - 6})`}
                    >
                      <rect
                        x={-edgeLabelWidth / 2}
                        y="-10"
                        width={edgeLabelWidth}
                        height="20"
                        rx="5"
                      />
                      <text
                        className={`ag-graph-edge-label${
                          isHighlighted
                            ? " ag-graph-edge-label--highlighted"
                            : ""
                        }`}
                        dominantBaseline="middle"
                        textAnchor="middle"
                      >
                        {visibleLabel}
                      </text>
                    </g>
                  )}
                </g>
              );
            })}
          </g>

          <g className="ag-graph-nodes">
            {graph.nodes.map((node) => {
              const position = layout.positions.get(node.id);
              if (!position) return null;
              const selected =
                selection?.mode === mode && selection.nodeId === node.id;
              const label = displayLabel(node.label);
              const cardWidth = labelCardWidth(label);
              const isCoreNode = node.kind === "reception";
              const showNodeLabel =
                isCoreNode || selected || labelledNodeIds.has(node.id);
              const labelOnLeft = !isCoreNode && position.x > 735;
              const cardX = isCoreNode
                ? -cardWidth / 2
                : labelOnLeft
                  ? -cardWidth - 11
                  : 11;
              const textX = isCoreNode
                ? 0
                : labelOnLeft
                  ? -17
                  : 17;
              const cardY = isCoreNode ? 20 : -13;
              const textY = isCoreNode ? 31 : 0;
              return (
                <g
                  key={node.id}
                  role="button"
                  tabIndex={0}
                  focusable="true"
                  aria-label={`${node.label}，${node.kind} 节点`}
                  aria-pressed={selected}
                  data-node-id={node.id}
                  data-kind={node.kind}
                  data-community={node.community}
                  data-x={position.x}
                  data-y={position.y}
                  className={`ag-graph-node ag-graph-node--${communityToken(node.kind)}${
                    selected ? " ag-graph-node--selected" : ""
                  }`}
                  transform={`translate(${position.x} ${position.y})`}
                  onClick={() => activateNode(node)}
                  onKeyDown={(event) =>
                    activateWithKeyboard(event, () => activateNode(node))
                  }
                >
                  <circle
                    className="ag-graph-node__hit"
                    r={isCoreNode ? 23 : 18}
                  />
                  <circle
                    className="ag-graph-node__halo"
                    r={isCoreNode ? 18 : 9}
                  />
                  <circle
                    className="ag-graph-node__marker"
                    r={isCoreNode ? 9 : 5}
                  />
                  {showNodeLabel && (
                    <>
                      <rect
                        className="ag-graph-node__label-card"
                        x={cardX}
                        y={cardY}
                        width={cardWidth}
                        height="26"
                        rx="6"
                      />
                      <text
                        className="ag-graph-node__label"
                        x={textX}
                        y={textY}
                        textAnchor={
                          isCoreNode
                            ? "middle"
                            : labelOnLeft
                              ? "end"
                              : "start"
                        }
                        dominantBaseline="middle"
                      >
                        {label}
                      </text>
                    </>
                  )}
                  <title>{node.label}</title>
                </g>
              );
            })}
          </g>
        </g>
      </svg>

      <div
        className="ag-dialogue-graph__controls"
        role="toolbar"
        aria-label="图谱缩放控制"
      >
        <button
          type="button"
          aria-label="全屏查看图谱"
          title="全屏查看"
          onClick={toggleFullscreen}
        >
          <IconFullscreen />
        </button>
        <button
          type="button"
          aria-label="缩小图谱"
          title="缩小"
          disabled={zoom <= MIN_ZOOM}
          onClick={() => changeZoom(-ZOOM_STEP)}
        >
          <IconZoomOut />
        </button>
        <output aria-live="polite">{Math.round(zoom * 100)}%</output>
        <button
          type="button"
          aria-label="放大图谱"
          title="放大"
          disabled={zoom >= MAX_ZOOM}
          onClick={() => changeZoom(ZOOM_STEP)}
        >
          <IconZoomIn />
        </button>
        <button
          type="button"
          aria-label="重置图谱缩放"
          title="重置缩放"
          onClick={() => setZoom(1)}
        >
          <IconRefresh />
        </button>
      </div>

      {graph.truncated && (
        <p className="ag-graph-limit">
          为保证交互性能，本视图最多显示 {MAX_GRAPH_NODES} 个节点。
        </p>
      )}
    </div>
  );
}
