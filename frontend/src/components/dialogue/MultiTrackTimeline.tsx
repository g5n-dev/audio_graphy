import {
  memo,
  type MouseEvent,
  useCallback,
  useMemo,
} from "react";
import type {
  EntityId,
  ReceptionDialogueUnit,
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
}

interface MultiTrackTimelineProps {
  workspace: ReceptionWorkspaceResponse;
  currentTime: number;
  selectedUnitIds: Set<string>;
  onSeek: (timelineSecond: number) => void;
  onToggleUnit: (unit: ReceptionDialogueUnit) => void;
}

interface TrackProps {
  label: string;
  duration: number;
  blocks: TimelineBlock[];
  selectedUnitIds: Set<string>;
  onSeek: (timelineSecond: number) => void;
  onToggleUnit?: (unitId: EntityId) => void;
}

const TimelineTrack = memo(function TimelineTrack({
  label,
  duration,
  blocks,
  selectedUnitIds,
  onSeek,
  onToggleUnit,
}: TrackProps) {
  return (
    <div className="ag-track" role="group" aria-label={`${label}轨道`}>
      <div className="ag-track__label">{label}</div>
      <div className="ag-track__lane">
        {blocks.length === 0 ? (
          <span className="ag-track__empty">无数据</span>
        ) : (
          blocks.map((block) => {
            const left = Math.max(0, (block.start / duration) * 100);
            const width = Math.max(
              0.8,
              (Math.max(block.end - block.start, 0.01) / duration) * 100,
            );
            const selected =
              block.unitId !== undefined &&
              selectedUnitIds.has(String(block.unitId));
            return (
              <button
                type="button"
                key={block.id}
                className={`ag-track__block ag-track__block--${block.tone}${selected ? " is-selected" : ""}`}
                style={{
                  left: `${left}%`,
                  width: `${Math.min(width, 100 - left)}%`,
                }}
                title={`${block.label} · ${formatClock(block.start)}–${formatClock(block.end)}`}
                aria-pressed={
                  block.unitId !== undefined && onToggleUnit
                    ? selected
                    : undefined
                }
                onClick={() => {
                  onSeek(block.start);
                  if (block.unitId !== undefined && onToggleUnit) {
                    onToggleUnit(block.unitId);
                  }
                }}
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
  duration,
  currentTime,
}: {
  duration: number;
  currentTime: number;
}) {
  const labels = useMemo(
    () =>
      [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
        ratio,
        label: formatClock(duration * ratio),
      })),
    [duration],
  );
  return (
    <div className="ag-time-ruler" aria-hidden="true">
      {labels.map(({ ratio, label }) => (
        <span key={ratio} style={{ left: `${ratio * 100}%` }}>
          {label}
        </span>
      ))}
      <i style={{ left: `${Math.min((currentTime / duration) * 100, 100)}%` }} />
    </div>
  );
});

const WaveformTrack = memo(function WaveformTrack({
  peaks,
  duration,
  onSeek,
}: {
  peaks: number[];
  duration: number;
  onSeek: (timelineSecond: number) => void;
}) {
  const maximum = Math.max(1, ...peaks);
  const handleSeek = useCallback(
    (event: MouseEvent<HTMLButtonElement>) => {
      const bounds = event.currentTarget.getBoundingClientRect();
      if (bounds.width <= 0) return;
      const ratio = Math.min(
        Math.max((event.clientX - bounds.left) / bounds.width, 0),
        1,
      );
      onSeek(duration * ratio);
    },
    [duration, onSeek],
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
          <i aria-hidden="true" />
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
  onSeek,
  onToggleUnit,
}: MultiTrackTimelineProps) {
  const duration = Math.max(workspace.reception.duration_sec, 0.1);
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
        ? workspace.transcript_items.map((item) => ({
            id: `speaker-${item.id}`,
            start: item.start_sec,
            end: item.end_sec,
            label: item.speaker_label,
            tone:
              item.speaker_role === "agent"
                ? "agent"
                : item.speaker_role === "customer"
                  ? "customer"
                  : "unknown",
            unitId: item.dialogue_unit_id ?? undefined,
          }))
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
    [workspace.dialogue_units, workspace.transcript_items],
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
              },
            ]
          : [];
      }),
    [unitMap, workspace.tag_assignments],
  );

  const handleToggle = useCallback(
    (unitId: EntityId) => {
      const unit = unitMap.get(String(unitId));
      if (unit) onToggleUnit(unit);
    },
    [onToggleUnit, unitMap],
  );

  return (
    <div className="ag-timeline" aria-label="源录音与对话语义多轨时间轴">
      <WaveformTrack
        peaks={waveformPeaks}
        duration={duration}
        onSeek={onSeek}
      />
      <TimelineRuler duration={duration} currentTime={currentTime} />
      <TimelineTrack
        label="源录音"
        duration={duration}
        blocks={sourceBlocks}
        selectedUnitIds={selectedUnitIds}
        onSeek={onSeek}
      />
      <TimelineTrack
        label="说话人"
        duration={duration}
        blocks={speakerBlocks}
        selectedUnitIds={selectedUnitIds}
        onSeek={onSeek}
        onToggleUnit={handleToggle}
      />
      <TimelineTrack
        label="主题"
        duration={duration}
        blocks={topicBlocks}
        selectedUnitIds={selectedUnitIds}
        onSeek={onSeek}
        onToggleUnit={handleToggle}
      />
      <TimelineTrack
        label="业务阶段"
        duration={duration}
        blocks={stageBlocks}
        selectedUnitIds={selectedUnitIds}
        onSeek={onSeek}
        onToggleUnit={handleToggle}
      />
      <TimelineTrack
        label="标签"
        duration={duration}
        blocks={tagBlocks}
        selectedUnitIds={selectedUnitIds}
        onSeek={onSeek}
        onToggleUnit={handleToggle}
      />
    </div>
  );
});
