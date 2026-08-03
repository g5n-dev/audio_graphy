import {
  memo,
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Skeleton } from "@arco-design/web-react";
import { IconLeft, IconRight } from "@arco-design/web-react/icon";
import type {
  EntityId,
  ReceptionDialogueUnit,
  RecordingSpeakerRef,
  ReceptionTagAssignment,
  ReceptionWorkspaceResponse,
} from "@/types/api";
import { formatClock } from "./format";

interface TimelineBlock {
  id: string;
  start: number;
  end: number;
  label: string;
  tone: string;
  unitId?: EntityId;
  tag?: ReceptionTagAssignment;
  lane?: number;
}

interface MultiTrackTimelineProps {
  workspace: ReceptionWorkspaceResponse;
  currentTime: number;
  selectedUnitIds: Set<string>;
  selectedTagId: EntityId | null;
  onSeek: (timelineSecond: number) => void;
  onToggleUnit: (unit: ReceptionDialogueUnit) => void;
  onSelectTag: (tag: ReceptionTagAssignment) => void;
  /**
   * `recording_id:spk_N` → the speaker that label resolves to.
   *
   * Optional: when voiceprint linking has not run, the timeline keeps
   * showing the raw diarization labels rather than inventing identities.
   */
  speakerByLabel?: ReadonlyMap<string, RecordingSpeakerRef>;
}

interface TrackProps {
  label: string;
  viewStart: number;
  viewEnd: number;
  blocks: TimelineBlock[];
  selectedUnitIds: Set<string>;
  selectedTagId: EntityId | null;
  onSeek: (timelineSecond: number) => void;
  onToggleUnit?: (unitId: EntityId) => void;
  onSelectTag?: (tag: ReceptionTagAssignment) => void;
  /** 返回最近一次指针交互是否被判定为拖动；为 true 时应抑制按钮点击行为 */
  wasDragging?: () => boolean;
}

// 拖动 vs 点击判定阈值：指针抬起时总位移（取 |dx| 与 |dy| 的最大值）小于该值才视为点击。
// 参考 AurisFlow 调听台拖动手感：轻点标签可编辑，按住拖动则横向平移时间轴。
const DRAG_THRESHOLD = 5;

/**
 * 拖动平移的内部状态。
 * - pointerId: 当前正在追踪的指针 id（仅认首个指针，多指忽略后续）
 * - startX/startY: pointerdown 时的坐标，用于位移判定
 * - startScrollLeft: pointerdown 时的 scroller.scrollLeft，用于计算平移量
 * - moved: 是否已超过拖动阈值（true 表示这次交互被判定为「拖动」，点击行为应被抑制）
 */
interface DragPanState {
  pointerId: number;
  startX: number;
  startY: number;
  startScrollLeft: number;
  moved: boolean;
}

interface DragPan {
  /** scroller 当前是否处于拖动中（用于挂 is-dragging class、切换 cursor） */
  isDragging: boolean;
  /** 绑定到 scroller 容器上的 pointer 事件处理集合 */
  handlers: {
    onPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
  };
  /** 读取最近一次交互是否被判定为拖动（供按钮 onClick 检查以抑制误触发的点击） */
  wasDragging: () => boolean;
}

/**
 * useDragPan —— 在给定 scroller ref 上实现鼠标 + 触控统一的拖动横向平移。
 *
 * 行为说明：
 * - pointerdown 记录起点与初始 scrollLeft；仅追踪首个 pointerId，第二个指针直接忽略。
 * - pointermove 计算 deltaX，调用 scroller.scrollLeft = startScrollLeft - deltaX 平移；
 *   位移超过 DRAG_THRESHOLD 标记 moved=true。
 * - pointerup/pointercancel 清理状态（pointerup 保留 moved 标志一个事件循环，供合成 click 检查）。
 * - touch-action 由 CSS 控制（pan-y），纵向滚动交给浏览器，横向由本 hook 接管。
 * - 返回 isDragging 用于挂 `is-dragging` class，wasDragging() 供按钮 onClick 判定。
 *
 * 不接管键盘交互，键盘用户继续使用左右滚动按钮。
 */
function useDragPan(scrollerRef: RefObject<HTMLDivElement | null>): DragPan {
  const stateRef = useRef<DragPanState | null>(null);
  // wasDraggingRef：pointerup 时写入，用于在紧接着的合成 click 里判断是否需要抑制点击。
  // 用 setTimeout 在下一个事件循环清零，避免影响后续真正的点击。
  const wasDraggingRef = useRef(false);
  const [isDragging, setIsDragging] = useState(false);

  const onPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const scroller = scrollerRef.current;
      if (!scroller) return;
      // 仅认第一个指针；若已有进行中的拖动则忽略新指针（多指场景）
      if (stateRef.current !== null) return;
      // 主键（左键）或触控才触发；右键等不处理
      if (event.pointerType === "mouse" && event.button !== 0) return;

      const { clientX, clientY } = event;
      // 每次新的按下都重置拖动标志，确保上一次拖动不会影响本次交互的点击判定
      wasDraggingRef.current = false;
      stateRef.current = {
        pointerId: event.pointerId,
        startX: clientX,
        startY: clientY,
        startScrollLeft: scroller.scrollLeft,
        moved: false,
      };
      // Pointer Capture 在旧版 WebKit、部分嵌入式浏览器和 jsdom 中不存在。
      // window 级监听本身已经能保证拖出元素后继续收到事件，因此捕获能力只作增强。
      if (typeof scroller.setPointerCapture === "function") {
        scroller.setPointerCapture(event.pointerId);
      }
    },
    [scrollerRef],
  );

  // 使用原生 effect 绑定 move/up，避免 React 合成事件在拖出元素边界时丢失
  useEffect(() => {
    if (typeof window === "undefined") return;
    const handleMove = (event: PointerEvent) => {
      const state = stateRef.current;
      if (state === null || event.pointerId !== state.pointerId) return;
      const deltaX = event.clientX - state.startX;
      const deltaY = event.clientY - state.startY;
      // 超过阈值即视为拖动（一旦标记为拖动，本次交互不再回退为点击）
      if (!state.moved && Math.max(Math.abs(deltaX), Math.abs(deltaY)) >= DRAG_THRESHOLD) {
        state.moved = true;
        wasDraggingRef.current = true;
        setIsDragging(true);
      }
      if (state.moved) {
        const scroller = scrollerRef.current;
        if (scroller) {
          // 横向平移：指针右移则内容左移（scrollLeft 减小）
          scroller.scrollLeft = state.startScrollLeft - deltaX;
        }
      }
    };
    const endDrag = (event: PointerEvent) => {
      const state = stateRef.current;
      if (state === null || event.pointerId !== state.pointerId) return;
      const scroller = scrollerRef.current;
      if (
        scroller &&
        typeof scroller.releasePointerCapture === "function" &&
        (typeof scroller.hasPointerCapture !== "function" ||
          scroller.hasPointerCapture(event.pointerId))
      ) {
        scroller.releasePointerCapture(event.pointerId);
      }
      stateRef.current = null;
      setIsDragging(false);
      // 保留 wasDraggingRef 一个事件循环，供紧随其后的合成 click 检查
      window.setTimeout(() => {
        wasDraggingRef.current = false;
      }, 0);
    };
    window.addEventListener("pointermove", handleMove, { passive: true });
    window.addEventListener("pointerup", endDrag);
    window.addEventListener("pointercancel", endDrag);
    return () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", endDrag);
      window.removeEventListener("pointercancel", endDrag);
    };
  }, [scrollerRef]);

  const wasDragging = useCallback(() => wasDraggingRef.current, []);

  return { isDragging, handlers: { onPointerDown }, wasDragging };
}

function assignOverlapLanes(blocks: TimelineBlock[]): TimelineBlock[] {
  const laneEnds: number[] = [];
  return [...blocks]
    .sort((left, right) => left.start - right.start || left.end - right.end)
    .map((block) => {
      let lane = laneEnds.findIndex((end) => end <= block.start);
      if (lane === -1) {
        lane = laneEnds.length;
        laneEnds.push(block.end);
      } else {
        laneEnds[lane] = block.end;
      }
      return { ...block, lane };
    });
}

const TimelineTrack = memo(function TimelineTrack({
  label,
  viewStart,
  viewEnd,
  blocks,
  selectedUnitIds,
  selectedTagId,
  onSeek,
  onToggleUnit,
  onSelectTag,
  wasDragging,
}: TrackProps) {
  const duration = Math.max(viewEnd - viewStart, 0.1);
  const visibleBlocks = blocks.filter(
    (block) => block.end > viewStart && block.start < viewEnd,
  );
  const laneCount = Math.max(
    1,
    ...visibleBlocks.map((block) => (block.lane ?? 0) + 1),
  );

  return (
    <div className="ag-track" role="group" aria-label={`${label}轨道`}>
      <div className="ag-track__label">{label}</div>
      <div
        className="ag-track__lane"
        data-lane-count={laneCount}
        style={{ height: `${Math.max(32, laneCount * 28 + 4)}px` }}
      >
        {visibleBlocks.length === 0 ? (
          <span className="ag-track__empty">无数据</span>
        ) : (
          visibleBlocks.map((block) => {
            const clippedStart = Math.max(block.start, viewStart);
            const clippedEnd = Math.min(block.end, viewEnd);
            const left = Math.max(
              0,
              ((clippedStart - viewStart) / duration) * 100,
            );
            const width = Math.max(
              0.35,
              ((Math.max(clippedEnd - clippedStart, 0.01) / duration) *
                100),
            );
            const unitSelected =
              block.unitId !== undefined &&
              selectedUnitIds.has(String(block.unitId));
            const tagSelected =
              block.tag !== undefined &&
              selectedTagId !== null &&
              String(block.tag.id) === String(selectedTagId);
            const actionableTag = block.tag !== undefined && onSelectTag;
            const accessibleAction = actionableTag ? "编辑标签" : "定位";
            const activate = () => {
              // 拖动刚结束（位移≥阈值）时不触发点击行为，避免拖完松手误开编辑器
              if (wasDragging !== undefined && wasDragging()) return;
              onSeek(block.start);
              if (block.tag !== undefined && onSelectTag) {
                onSelectTag(block.tag);
              } else if (block.unitId !== undefined && onToggleUnit) {
                onToggleUnit(block.unitId);
              }
            };
            const handleKeyDown = (
              event: KeyboardEvent<HTMLButtonElement>,
            ) => {
              if (
                block.tag !== undefined &&
                onSelectTag &&
                event.key.toLocaleLowerCase() === "e"
              ) {
                event.preventDefault();
                activate();
              }
            };
            return (
              <button
                type="button"
                key={block.id}
                className={`ag-track__block ag-track__block--${block.tone}${unitSelected ? " is-unit-selected" : ""}${tagSelected ? " is-selected" : ""}`}
                style={{
                  left: `${left}%`,
                  width: `${Math.min(width, 100 - left)}%`,
                  top: `${4 + (block.lane ?? 0) * 28}px`,
                }}
                title={`${block.label} · ${formatClock(block.start)}–${formatClock(block.end)}`}
                aria-label={
                  block.tone === "source"
                    ? `定位 ${block.label}`
                    : `${accessibleAction} ${block.label}，${formatClock(block.start)} 到 ${formatClock(block.end)}`
                }
                aria-pressed={
                  actionableTag ||
                  (block.unitId !== undefined && onToggleUnit !== undefined)
                    ? tagSelected || unitSelected
                    : undefined
                }
                onClick={activate}
                onKeyDown={handleKeyDown}
              >
                <span>{block.label}</span>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
});

const TimelineRuler = memo(function TimelineRuler({
  viewStart,
  viewEnd,
  currentTime,
}: {
  viewStart: number;
  viewEnd: number;
  currentTime: number;
}) {
  const duration = Math.max(viewEnd - viewStart, 0.1);
  const labels = useMemo(
    () =>
      [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
        ratio,
        label: formatClock(viewStart + duration * ratio),
      })),
    [duration, viewStart],
  );
  const playhead = Math.min(
    Math.max(((currentTime - viewStart) / duration) * 100, 0),
    100,
  );
  return (
    <div className="ag-time-ruler">
      <div className="ag-track__label" aria-hidden="true">
        时间
      </div>
      <div className="ag-time-ruler__lane" aria-hidden="true">
        {labels.map(({ ratio, label }) => (
          <span key={ratio} style={{ left: `${ratio * 100}%` }}>
            {label}
          </span>
        ))}
        <span
          className="ag-time-ruler__playhead"
          style={{ left: `${playhead}%` }}
        />
      </div>
    </div>
  );
});

const WaveformTrack = memo(function WaveformTrack({
  peaks,
  viewStart,
  viewEnd,
  currentTime,
  onSeek,
  wasDragging,
}: {
  peaks: number[];
  viewStart: number;
  viewEnd: number;
  currentTime: number;
  onSeek: (timelineSecond: number) => void;
  /** 返回最近一次指针交互是否被判定为拖动；为 true 时应抑制波形点击 seek */
  wasDragging?: () => boolean;
}) {
  const duration = Math.max(viewEnd - viewStart, 0.1);
  const maximum = Math.max(1, ...peaks);
  const handleSeek = useCallback(
    (event: MouseEvent<HTMLButtonElement>) => {
      // 拖动刚结束（位移≥阈值）时不触发波形 seek，避免拖完松手误跳音频
      if (wasDragging !== undefined && wasDragging()) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      if (bounds.width <= 0) return;
      const ratio = Math.min(
        Math.max((event.clientX - bounds.left) / bounds.width, 0),
        1,
      );
      onSeek(viewStart + duration * ratio);
    },
    [duration, onSeek, viewStart, wasDragging],
  );
  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLButtonElement>) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const step = event.shiftKey ? 10 : 1;
      onSeek(
        Math.min(
          Math.max(
            currentTime + (event.key === "ArrowRight" ? step : -step),
            viewStart,
          ),
          viewEnd,
        ),
      );
    },
    [currentTime, onSeek, viewEnd, viewStart],
  );
  return (
    <div className="ag-wave-placeholder">
      <div className="ag-track__label">波形</div>
      {peaks.length > 0 ? (
        <button
          type="button"
          className="ag-wave-placeholder__bars"
          aria-label="真实音频波形，点击定位"
          title="由后端峰值数据绘制"
          onClick={handleSeek}
          onKeyDown={handleKeyDown}
        >
          {peaks.map((peak, index) => (
            <span
              key={`${index}-${peak}`}
              style={{
                height: `${Math.max((peak / maximum) * 100, 2)}%`,
              }}
            />
          ))}
        </button>
      ) : (
        <div
          className="ag-wave-placeholder__skeleton"
          role="status"
          aria-label="尚未生成音频波形"
        >
          <Skeleton
            className="ag-wave-placeholder__arco-skeleton"
            animation
            text={{ rows: 1, width: "100%" }}
          />
          <span>未生成波形</span>
        </div>
      )}
    </div>
  );
});

export const MultiTrackTimeline = memo(function MultiTrackTimeline({
  workspace,
  currentTime,
  selectedUnitIds,
  selectedTagId,
  onSeek,
  onToggleUnit,
  onSelectTag,
  speakerByLabel,
}: MultiTrackTimelineProps) {
  const [zoom, setZoom] = useState(1);
  const scrollerRef = useRef<HTMLDivElement>(null);
  // 拖动平移：鼠标 + 触控统一，拖动 vs 点击由 5px 阈值区分
  const { isDragging, handlers: dragHandlers, wasDragging } =
    useDragPan(scrollerRef);
  const viewStart = workspace.window.start_sec;
  const viewEnd = Math.max(workspace.window.end_sec, viewStart + 0.1);
  const viewDuration = viewEnd - viewStart;
  const baseLaneWidth = Math.max(620, Math.round(viewDuration * 2));
  const laneWidth = Math.min(8_000, Math.round(baseLaneWidth * zoom));
  const waveformPeaks = useMemo(
    () =>
      (workspace.waveform_peaks ?? []).filter(
        (peak) => Number.isFinite(peak) && peak >= 0,
      ),
    [workspace.waveform_peaks],
  );
  const unitMap = useMemo(
    () =>
      new Map(
        workspace.dialogue_units.map((unit) => [String(unit.id), unit]),
      ),
    [workspace.dialogue_units],
  );
  const sourceBlocks = useMemo<TimelineBlock[]>(
    () =>
      workspace.recordings.map((recording) => ({
        id: `source-${recording.id}`,
        start: recording.timeline_start_sec,
        end: recording.timeline_end_sec,
        label: recording.name,
        tone: "source",
      })),
    [workspace.recordings],
  );
  const speakerBlocks = useMemo<TimelineBlock[]>(
    () =>
      workspace.transcript_items.length > 0
        ? workspace.transcript_items.map((item) => {
            // Prefer the voiceprint-resolved speaker: the fallback role comes
            // from a keyword guess on the raw label, which is always "unknown"
            // for diarization output like "spk_0".
            const resolved = speakerByLabel?.get(
              `${item.recording_id}:${item.speaker_label}`,
            );
            const role = resolved?.speaker_role ?? item.speaker_role;
            const ambiguity = resolved?.ambiguity_tag ?? null;
            return {
              id: `speaker-${item.id}`,
              start: item.start_sec,
              end: item.end_sec,
              // The warning sign is what tells a reviewer this attribution is
              // provisional; without it a low-confidence merge looks certain.
              label: ambiguity
                ? `⚠ ${resolved?.display_name ?? item.speaker_label}`
                : (resolved?.display_name ?? item.speaker_label),
              tone:
                role === "agent"
                  ? "agent"
                  : role === "customer"
                    ? "customer"
                    : "unknown",
              unitId: item.dialogue_unit_id ?? undefined,
            };
          })
        : workspace.dialogue_units.flatMap((unit) =>
            (unit.speaker_refs ?? []).map((speaker, index) => ({
              id: `speaker-${unit.id}-${index}`,
              start: unit.start_sec,
              end: unit.end_sec,
              label: speaker,
              tone: "unknown",
              unitId: unit.id,
            })),
          ),
    [workspace.dialogue_units, workspace.transcript_items, speakerByLabel],
  );
  const topicBlocks = useMemo<TimelineBlock[]>(
    () =>
      workspace.dialogue_units.map((unit) => ({
        id: `topic-${unit.id}`,
        start: unit.start_sec,
        end: unit.end_sec,
        label: unit.topic ?? "未识别主题",
        tone: "topic",
        unitId: unit.id,
      })),
    [workspace.dialogue_units],
  );
  const stageBlocks = useMemo<TimelineBlock[]>(
    () =>
      workspace.dialogue_units.map((unit) => ({
        id: `stage-${unit.id}`,
        start: unit.start_sec,
        end: unit.end_sec,
        label: unit.business_stage ?? "未识别阶段",
        tone: "stage",
        unitId: unit.id,
      })),
    [workspace.dialogue_units],
  );
  const tagBlocks = useMemo<TimelineBlock[]>(
    () =>
      assignOverlapLanes(
        workspace.tag_assignments.flatMap((tag) => {
          const unit = unitMap.get(String(tag.dialogue_unit_id));
          return unit
            ? [
                {
                  id: `tag-${tag.id}`,
                  start: unit.start_sec,
                  end: unit.end_sec,
                  label: `${tag.label_key}: ${tag.label_value}`,
                  tone: tag.is_manual ? "manual" : "tag",
                  unitId: unit.id,
                  tag,
                },
              ]
            : [];
        }),
      ),
    [unitMap, workspace.tag_assignments],
  );

  const handleToggle = useCallback(
    (unitId: EntityId) => {
      const unit = unitMap.get(String(unitId));
      if (unit) onToggleUnit(unit);
    },
    [onToggleUnit, unitMap],
  );
  const scrollTimeline = (direction: -1 | 1) => {
    const scroller = scrollerRef.current;
    if (!scroller || typeof scroller.scrollBy !== "function") return;
    scroller.scrollBy({
      left: direction * Math.max(scroller.clientWidth * 0.7, 240),
      behavior: "smooth",
    });
  };

  const canvasStyle = {
    "--ag-timeline-lane-width": `${laneWidth}px`,
  } as CSSProperties;

  return (
    <section
      className="ag-timeline"
      aria-label="源录音与对话语义多轨时间轴"
    >
      <div className="ag-timeline__toolbar">
        <p>拖动或触控横向浏览，标签时间来自证据所属对话单元</p>
        <div>
          <button
            type="button"
            aria-label="时间轴向左滚动"
            onClick={() => scrollTimeline(-1)}
          >
            <IconLeft />
          </button>
          <label htmlFor="ag-timeline-zoom">
            <span>缩放</span>
            <input
              id="ag-timeline-zoom"
              type="range"
              min="1"
              max="4"
              step="0.5"
              value={zoom}
              aria-label="时间轴缩放"
              onChange={(event) => setZoom(Number(event.target.value))}
            />
            <output htmlFor="ag-timeline-zoom">{zoom}×</output>
          </label>
          <button
            type="button"
            aria-label="时间轴向右滚动"
            onClick={() => scrollTimeline(1)}
          >
            <IconRight />
          </button>
        </div>
      </div>
      <div
        ref={scrollerRef}
        className={`ag-timeline__scroller${isDragging ? " is-dragging" : ""}`}
        tabIndex={0}
        aria-label={`时间轴可滚动区域，当前窗口 ${formatClock(viewStart)} 到 ${formatClock(viewEnd)}`}
        onPointerDown={dragHandlers.onPointerDown}
      >
        <div className="ag-timeline__canvas" style={canvasStyle}>
          <WaveformTrack
            peaks={waveformPeaks}
            viewStart={viewStart}
            viewEnd={viewEnd}
            currentTime={currentTime}
            onSeek={onSeek}
            wasDragging={wasDragging}
          />
          <TimelineRuler
            viewStart={viewStart}
            viewEnd={viewEnd}
            currentTime={currentTime}
          />
          <TimelineTrack
            label="源录音"
            viewStart={viewStart}
            viewEnd={viewEnd}
            blocks={sourceBlocks}
            selectedUnitIds={selectedUnitIds}
            selectedTagId={selectedTagId}
            onSeek={onSeek}
            wasDragging={wasDragging}
          />
          <TimelineTrack
            label="说话人"
            viewStart={viewStart}
            viewEnd={viewEnd}
            blocks={speakerBlocks}
            selectedUnitIds={selectedUnitIds}
            selectedTagId={selectedTagId}
            onSeek={onSeek}
            onToggleUnit={handleToggle}
            wasDragging={wasDragging}
          />
          <TimelineTrack
            label="主题"
            viewStart={viewStart}
            viewEnd={viewEnd}
            blocks={topicBlocks}
            selectedUnitIds={selectedUnitIds}
            selectedTagId={selectedTagId}
            onSeek={onSeek}
            onToggleUnit={handleToggle}
            wasDragging={wasDragging}
          />
          <TimelineTrack
            label="业务阶段"
            viewStart={viewStart}
            viewEnd={viewEnd}
            blocks={stageBlocks}
            selectedUnitIds={selectedUnitIds}
            selectedTagId={selectedTagId}
            onSeek={onSeek}
            onToggleUnit={handleToggle}
            wasDragging={wasDragging}
          />
          <TimelineTrack
            label="标签"
            viewStart={viewStart}
            viewEnd={viewEnd}
            blocks={tagBlocks}
            selectedUnitIds={selectedUnitIds}
            selectedTagId={selectedTagId}
            onSeek={onSeek}
            onSelectTag={onSelectTag}
            wasDragging={wasDragging}
          />
        </div>
      </div>
    </section>
  );
});
