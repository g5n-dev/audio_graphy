/**
 * 编排画布 —— 处理流水线 DAG,左→右为数据流向。
 *
 * 连线走 SVG 贝塞尔,节点是绝对定位的 button:节点要 hover 态、键盘可达与
 * 点击态,DOM 元素比 SVG <foreignObject> 可靠。
 *
 * 布局按 links 拓扑分层算出来,不写死坐标——阶段增减时画布自己重排,避免
 * 后端拓扑变了而前端还画着旧图。
 */

import { useMemo } from "react";
import type { OrchestrationStage } from "@/types/api";

const NODE_W = 168;
const NODE_H = 84;
const COL_GAP = 220;
const ROW_GAP = 116;
const PAD_X = 28;
const PAD_Y = 24;

const STATE_META: Record<
  OrchestrationStage["state"],
  { label: string; dot: string }
> = {
  ok: { label: "就绪", dot: "#00b42a" },
  busy: { label: "有积压", dot: "#ff7d00" },
  mock: { label: "Mock", dot: "#86909c" },
};

/** 最长路径分层:节点的列 = 其所有前驱列的最大值 + 1。 */
function layered(
  stages: OrchestrationStage[],
  links: [string, string][],
): Map<string, { x: number; y: number }> {
  const column = new Map<string, number>();
  stages.forEach((stage) => column.set(stage.id, 0));
  // 阶段数固定在个位数,朴素松弛比拓扑排序更好读,代价可忽略。
  for (let pass = 0; pass < stages.length; pass += 1) {
    for (const [source, target] of links) {
      const next = (column.get(source) ?? 0) + 1;
      if (next > (column.get(target) ?? 0)) column.set(target, next);
    }
  }
  const rows = new Map<number, number>();
  const positions = new Map<string, { x: number; y: number }>();
  for (const stage of stages) {
    const col = column.get(stage.id) ?? 0;
    const row = rows.get(col) ?? 0;
    rows.set(col, row + 1);
    positions.set(stage.id, {
      x: PAD_X + col * COL_GAP,
      y: PAD_Y + row * ROW_GAP,
    });
  }
  // 每列各自垂直居中,列高不同也不会一头沉。
  const tallest = Math.max(...rows.values(), 1);
  for (const stage of stages) {
    const col = column.get(stage.id) ?? 0;
    const count = rows.get(col) ?? 1;
    const offset = ((tallest - count) * ROW_GAP) / 2;
    const position = positions.get(stage.id);
    if (position) position.y += offset;
  }
  return positions;
}

export function OrchestrationCanvas({
  stages,
  links,
  selectedId,
  onSelect,
}: {
  stages: OrchestrationStage[];
  links: [string, string][];
  selectedId: string | null;
  onSelect: (stage: OrchestrationStage) => void;
}) {
  const positions = useMemo(() => layered(stages, links), [stages, links]);
  const size = useMemo(() => {
    let width = 0;
    let height = 0;
    for (const { x, y } of positions.values()) {
      width = Math.max(width, x + NODE_W + PAD_X);
      height = Math.max(height, y + NODE_H + PAD_Y);
    }
    return { width, height };
  }, [positions]);

  const paths = useMemo(
    () =>
      links.flatMap(([source, target]) => {
        const from = positions.get(source);
        const to = positions.get(target);
        if (!from || !to) return [];
        const x0 = from.x + NODE_W;
        const y0 = from.y + NODE_H / 2;
        const x1 = to.x;
        const y1 = to.y + NODE_H / 2;
        const mid = (x0 + x1) / 2;
        return [
          {
            key: `${source}-${target}`,
            d: `M${x0} ${y0}C${mid} ${y0},${mid} ${y1},${x1} ${y1}`,
          },
        ];
      }),
    [links, positions],
  );

  return (
    <div className="ag-orchestration-canvas">
      <div
        className="ag-orchestration-canvas__stage"
        style={{ width: size.width, height: size.height }}
      >
        <div className="ag-orchestration-canvas__grid" aria-hidden="true" />
        <svg
          viewBox={`0 0 ${size.width} ${size.height}`}
          width={size.width}
          height={size.height}
          aria-hidden="true"
        >
          {paths.map((path) => (
            <path key={path.key} d={path.d} />
          ))}
        </svg>
        {stages.map((stage) => {
          const position = positions.get(stage.id);
          if (!position) return null;
          const meta = STATE_META[stage.state];
          return (
            <button
              key={stage.id}
              type="button"
              className="ag-orchestration-node"
              data-state={stage.state}
              data-selected={stage.id === selectedId || undefined}
              style={{
                left: position.x,
                top: position.y,
                width: NODE_W,
                height: NODE_H,
              }}
              aria-label={`${stage.name} · ${meta.label}${
                stage.queue ? ` · 积压 ${stage.queue}` : ""
              }`}
              onClick={() => onSelect(stage)}
            >
              <span className="ag-orchestration-node__title">
                <i aria-hidden="true" style={{ background: meta.dot }} />
                <strong>{stage.name}</strong>
              </span>
              <span className="ag-orchestration-node__svc">{stage.service}</span>
              <span className="ag-orchestration-node__foot">
                <small>{meta.label}</small>
                {/* 积压是真实计数:0 就说无积压,不编吞吐量。 */}
                <small data-warn={stage.queue > 0 || undefined}>
                  {stage.queue > 0 ? `积压 ${stage.queue}` : "无积压"}
                </small>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
