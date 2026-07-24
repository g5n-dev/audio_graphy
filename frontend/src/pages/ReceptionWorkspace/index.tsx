import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useLocation,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  deriveReceptionDialogueTags,
  getReceptionAutomation,
  getReceptionWorkspace,
  mergeDialogueUnits,
  mergeReceptionRecordings,
  runReceptionAutomation,
  segmentReception,
  splitDialogueUnit,
} from "@/api/services";
import { EvidenceAuditPanel } from "@/components/dialogue/EvidenceAuditPanel";
import { formatClock, formatPercent } from "@/components/dialogue/format";
import { MultiTrackTimeline } from "@/components/dialogue/MultiTrackTimeline";
import { ReceptionContextTabs } from "@/components/navigation/ContextNavigation";
import type {
  DialogueEvidenceRef,
  DialogueTargetLabel,
  EntityId,
  ReceptionDialogueUnit,
  ReceptionMergeMode,
} from "@/types/api";

type AudioSourceId = string | null;

interface PendingSeek {
  sourceId: AudioSourceId;
  second: number;
}

const WORKSPACE_WINDOW_SIZE_SEC = 600;

const TARGET_LABELS: Array<{
  key: DialogueTargetLabel;
  label: string;
}> = [
  { key: "stage", label: "阶段" },
  { key: "intent", label: "意向" },
  { key: "objection", label: "异议" },
  { key: "next_step", label: "下一步" },
  { key: "compliance_risk", label: "合规风险" },
];

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "接口暂不可用";
}

function idEquals(left: EntityId, right: EntityId): boolean {
  return String(left) === String(right);
}

export default function ReceptionWorkspacePage() {
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const audioRef = useRef<HTMLAudioElement>(null);
  const deepLinkAppliedRef = useRef(false);
  const initializedReceptionRef = useRef<string | null>(null);
  const initializedWindowRef = useRef<string | null>(null);
  const playbackGrantRefreshRef = useRef(false);
  const [activeSourceId, setActiveSourceId] = useState<AudioSourceId>(null);
  const [pendingSeek, setPendingSeek] = useState<PendingSeek | null>(null);
  const [timelineTime, setTimelineTime] = useState(0);
  const [windowStartSec, setWindowStartSec] = useState(0);
  const [selectedUnitIds, setSelectedUnitIds] = useState<Set<string>>(
    new Set(),
  );
  const [mergeMode, setMergeMode] = useState<ReceptionMergeMode>("both");
  const [editReason, setEditReason] = useState("");
  const [operationStatus, setOperationStatus] = useState<string | null>(null);
  const [tagGroupKey, setTagGroupKey] = useState("reception-rules");
  const [tagGroupVersion, setTagGroupVersion] = useState("rules-v1");
  const [targetLabels, setTargetLabels] = useState<Set<DialogueTargetLabel>>(
    new Set(TARGET_LABELS.map((item) => item.key)),
  );

  const workspaceQuery = useQuery({
    queryKey: ["reception-workspace", id, windowStartSec],
    queryFn: () =>
      getReceptionWorkspace(id ?? "", {
        window_start_sec: windowStartSec,
        window_size_sec: WORKSPACE_WINDOW_SIZE_SEC,
      }),
    enabled: Boolean(id),
    retry: false,
  });
  const automationQuery = useQuery({
    queryKey: ["reception-automation", id],
    queryFn: () => getReceptionAutomation(id ?? ""),
    enabled: Boolean(id),
    retry: false,
  });
  const workspace = workspaceQuery.data;

  useEffect(() => {
    setWindowStartSec(0);
    initializedWindowRef.current = null;
  }, [id]);

  useEffect(() => {
    const state = location.state as { automationMessage?: unknown } | null;
    if (typeof state?.automationMessage === "string") {
      setOperationStatus(state.automationMessage);
    }
  }, [location.state]);

  useEffect(() => {
    const automation = automationQuery.data;
    if (automation?.status === "failed") {
      setOperationStatus(
        `自动处理停在${automation.stage}阶段：${automation.last_error_message ?? "修复源数据后可从检查点重试"}`,
      );
    }
  }, [automationQuery.data]);

  useEffect(() => {
    if (!workspace) return;
    const receptionIdentity = String(workspace.reception.id);
    if (initializedReceptionRef.current !== receptionIdentity) {
      initializedReceptionRef.current = receptionIdentity;
      deepLinkAppliedRef.current = false;
      setMergeMode(workspace.reception.merge_mode);
      setActiveSourceId(
        workspace.reception.merged_audio_url
          ? null
          : workspace.recordings[0]
            ? String(workspace.recordings[0].id)
            : null,
      );
    }
    const windowIdentity = `${receptionIdentity}:${workspace.window.start_sec}`;
    if (initializedWindowRef.current !== windowIdentity) {
      initializedWindowRef.current = windowIdentity;
      setSelectedUnitIds(
        workspace.dialogue_units[0]
          ? new Set([String(workspace.dialogue_units[0].id)])
          : new Set(),
      );
    }
  }, [workspace]);

  const activeRecording = useMemo(
    () =>
      workspace?.recordings.find(
        (recording) => String(recording.id) === activeSourceId,
      ) ?? null,
    [activeSourceId, workspace],
  );
  const audioUrl =
    activeRecording?.audio_url ??
    (activeSourceId === null ? workspace?.reception.merged_audio_url : null);

  const applyPendingSeek = useCallback(() => {
    if (playbackGrantRefreshRef.current) return;
    if (!pendingSeek || pendingSeek.sourceId !== activeSourceId) return;
    if (audioRef.current) {
      audioRef.current.currentTime = Math.max(pendingSeek.second, 0);
    }
    setPendingSeek(null);
  }, [activeSourceId, pendingSeek]);

  useEffect(() => {
    applyPendingSeek();
  }, [applyPendingSeek]);

  const seekSource = useCallback(
    (sourceId: AudioSourceId, second: number) => {
      const safeSecond = Math.max(second, 0);
      if (sourceId === activeSourceId && audioRef.current) {
        audioRef.current.currentTime = safeSecond;
        return;
      }
      setPendingSeek({ sourceId, second: safeSecond });
      setActiveSourceId(sourceId);
    },
    [activeSourceId],
  );

  const seekTimeline = useCallback(
    (second: number) => {
      if (!workspace) return;
      const safeSecond = Math.min(
        Math.max(second, 0),
        workspace.reception.duration_sec,
      );
      setTimelineTime(safeSecond);
      if (workspace.reception.merged_audio_url) {
        seekSource(null, safeSecond);
        return;
      }
      const source = workspace.recordings.find(
        (recording) =>
          safeSecond >= recording.timeline_start_sec &&
          safeSecond <= recording.timeline_end_sec,
      );
      if (source) {
        const sourceSecond =
          source.source_start_sec + safeSecond - source.timeline_start_sec;
        seekSource(String(source.id), sourceSecond);
      }
    },
    [seekSource, workspace],
  );

  const seekEvidence = useCallback(
    (evidence: DialogueEvidenceRef) => {
      if (!workspace) return;
      if (typeof evidence.timeline_start_ms === "number") {
        seekTimeline(evidence.timeline_start_ms / 1000);
        return;
      }
      if (evidence.start_ms === null) return;
      const source = workspace.recordings.find((recording) =>
        idEquals(recording.id, evidence.recording_id),
      );
      const sourceSecond = evidence.start_ms / 1000;
      if (workspace.reception.merged_audio_url && source) {
        const mergedSecond =
          source.timeline_start_sec + sourceSecond - source.source_start_sec;
        setTimelineTime(Math.max(mergedSecond, 0));
        seekSource(null, mergedSecond);
        return;
      }
      if (source) {
        setTimelineTime(
          source.timeline_start_sec + sourceSecond - source.source_start_sec,
        );
        seekSource(String(source.id), sourceSecond);
      }
    },
    [seekSource, seekTimeline, workspace],
  );

  useEffect(() => {
    if (!workspace || deepLinkAppliedRef.current) return;
    const recordingId = searchParams.get("recording");
    const rawMilliseconds = searchParams.get("at");
    if (!recordingId || rawMilliseconds === null) return;
    const milliseconds = Number(rawMilliseconds);
    if (!Number.isFinite(milliseconds) || milliseconds < 0) return;
    const source = workspace.recordings.find((recording) =>
      idEquals(recording.id, recordingId),
    );
    if (!source) return;
    deepLinkAppliedRef.current = true;
    const sourceSecond = milliseconds / 1000;
    if (workspace.reception.merged_audio_url) {
      const mergedSecond =
        source.timeline_start_sec + sourceSecond - source.source_start_sec;
      setTimelineTime(Math.max(mergedSecond, 0));
      seekSource(null, mergedSecond);
    } else {
      setTimelineTime(
        source.timeline_start_sec + sourceSecond - source.source_start_sec,
      );
      seekSource(String(source.id), sourceSecond);
    }
  }, [searchParams, seekSource, workspace]);

  const handleTimeUpdate = () => {
    const sourceTime = audioRef.current?.currentTime ?? 0;
    if (activeRecording) {
      setTimelineTime(
        activeRecording.timeline_start_sec +
          sourceTime -
          activeRecording.source_start_sec,
      );
    } else {
      setTimelineTime(sourceTime);
    }
  };
  const handleAudioError = async () => {
    if (!audioUrl || playbackGrantRefreshRef.current) return;
    playbackGrantRefreshRef.current = true;
    setPendingSeek({
      sourceId: activeSourceId,
      second: audioRef.current?.currentTime ?? 0,
    });
    setOperationStatus("播放凭证可能已过期，正在安全刷新并恢复播放点。");
    const result = await workspaceQuery.refetch();
    if (result.isError) {
      playbackGrantRefreshRef.current = false;
      setPendingSeek(null);
      setOperationStatus(`播放凭证刷新失败：${errorMessage(result.error)}`);
      return;
    }
    const refreshedRecording = result.data?.recordings.find(
      (recording) => String(recording.id) === activeSourceId,
    );
    const refreshedUrl =
      refreshedRecording?.audio_url ??
      (activeSourceId === null
        ? result.data?.reception.merged_audio_url
        : null);
    if (!refreshedUrl || refreshedUrl === audioUrl) {
      playbackGrantRefreshRef.current = false;
      setPendingSeek(null);
      setOperationStatus("音频仍无法加载，请检查源文件格式或稍后重试。");
    } else {
      setOperationStatus("播放凭证已刷新，正在恢复原播放点。");
    }
  };
  const handleAudioMetadata = () => {
    playbackGrantRefreshRef.current = false;
    applyPendingSeek();
  };

  const refreshWorkspace = async () => {
    await queryClient.invalidateQueries({
      queryKey: ["reception-workspace", id],
    });
  };

  const automationMutation = useMutation({
    mutationFn: async () => {
      if (!id) throw new Error("接待 ID 不可用");
      return runReceptionAutomation(id);
    },
    onSuccess: async (result) => {
      setOperationStatus(
        result.status === "ready"
          ? "自动处理完成：录音合并、对话切分与五维目标标签均已就绪。"
          : `自动处理停在${result.stage}阶段：${result.last_error_message ?? "请检查源数据后重试"}`,
      );
      await Promise.all([
        refreshWorkspace(),
        queryClient.invalidateQueries({
          queryKey: ["reception-automation", id],
        }),
      ]);
    },
    onError: (error) => {
      setOperationStatus(`自动处理请求失败：${errorMessage(error)}`);
    },
  });

  const recordingMergeMutation = useMutation({
    mutationFn: async () => {
      if (!workspace || !id) throw new Error("接待数据尚未加载");
      const recordingIds = workspace.recordings.map(
        (recording) => recording.id,
      );
      return mergeReceptionRecordings(id, {
        recording_ids: recordingIds,
        mode: mergeMode,
        expected_version: workspace.reception.version,
      });
    },
    onSuccess: async () => {
      setOperationStatus("录音合并任务已提交，正在刷新接待版本。");
      await refreshWorkspace();
    },
    onError: (error) => {
      setOperationStatus(`录音合并失败：${errorMessage(error)}`);
    },
  });

  const segmentationMutation = useMutation({
    mutationFn: async () => {
      if (!workspace || !id) throw new Error("接待数据尚未加载");
      return segmentReception(id, {
        expected_version: workspace.reception.version,
        replace_auto: workspace.window.total_dialogue_units > 0,
        algorithm_version: "dialogue-hybrid-v1",
      });
    },
    onSuccess: async () => {
      setOperationStatus("自动对话切分已完成，状态轨与溯源链正在刷新。");
      await refreshWorkspace();
    },
    onError: (error) => {
      setOperationStatus(`自动对话切分失败：${errorMessage(error)}`);
    },
  });

  const splitMutation = useMutation({
    mutationFn: async ({
      unit,
      splitAt,
    }: {
      unit: ReceptionDialogueUnit;
      splitAt: number;
    }) => {
      if (!workspace || !id) throw new Error("接待数据尚未加载");
      return splitDialogueUnit(id, unit.id, {
        split_at_sec: splitAt,
        expected_reception_version: workspace.reception.version,
        expected_unit_version: unit.version,
        reason: editReason.trim(),
      });
    },
    onSuccess: async () => {
      setOperationStatus("对话切分已保存，并写入审计记录。");
      setSelectedUnitIds(new Set());
      await refreshWorkspace();
    },
    onError: (error) => {
      setOperationStatus(`对话切分失败：${errorMessage(error)}`);
    },
  });

  const unitMergeMutation = useMutation({
    mutationFn: async ({
      unit,
      otherUnit,
    }: {
      unit: ReceptionDialogueUnit;
      otherUnit: ReceptionDialogueUnit;
    }) => {
      if (!workspace || !id) throw new Error("接待数据尚未加载");
      return mergeDialogueUnits(id, unit.id, {
        other_unit_id: otherUnit.id,
        expected_reception_version: workspace.reception.version,
        expected_unit_version: unit.version,
        expected_other_unit_version: otherUnit.version,
        reason: editReason.trim(),
      });
    },
    onSuccess: async () => {
      setOperationStatus("相邻对话单元已合并，并写入审计记录。");
      setSelectedUnitIds(new Set());
      await refreshWorkspace();
    },
    onError: (error) => {
      setOperationStatus(`对话单元合并失败：${errorMessage(error)}`);
    },
  });

  const deriveTagsMutation = useMutation({
    mutationFn: async () => {
      if (!workspace || !id) throw new Error("接待数据尚未加载");
      const groupKey = tagGroupKey.trim();
      const groupVersion = tagGroupVersion.trim();
      if (!groupKey || !groupVersion) {
        throw new Error("标签组与规则版本不能为空");
      }
      const selectedLabels = TARGET_LABELS.map((item) => item.key).filter(
        (label) => targetLabels.has(label),
      );
      if (selectedLabels.length === 0) {
        throw new Error("至少选择一个目标标签维度");
      }
      return deriveReceptionDialogueTags(id, {
        group_key: groupKey,
        group_version: groupVersion,
        target_labels: selectedLabels,
        priority: 0,
      });
    },
    onSuccess: async (result) => {
      setOperationStatus(
        result.no_op
          ? `目标标签 ${result.group_key}@${result.group_version} 已是最新，无需重复写入。`
          : `目标标签派生完成：写入 ${result.assignment_count} 个，替换 ${result.superseded_count} 个旧版本。`,
      );
      await refreshWorkspace();
    },
    onError: (error) => {
      setOperationStatus(`目标标签派生失败：${errorMessage(error)}`);
    },
  });

  const toggleUnit = useCallback((unit: ReceptionDialogueUnit) => {
    setSelectedUnitIds((current) => {
      const next = new Set(current);
      const key = String(unit.id);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const splitAtCurrentTime = () => {
    if (!workspace) return;
    if (!editReason.trim()) {
      setOperationStatus("请先填写人工编辑原因，原因会进入溯源审计链。");
      return;
    }
    const selectedUnits = workspace.dialogue_units.filter((unit) =>
      selectedUnitIds.has(String(unit.id)),
    );
    const target =
      selectedUnits.find(
        (unit) => timelineTime > unit.start_sec && timelineTime < unit.end_sec,
      ) ??
      workspace.dialogue_units.find(
        (unit) => timelineTime > unit.start_sec && timelineTime < unit.end_sec,
      );
    if (!target) {
      setOperationStatus("当前播放点不在可切分的对话单元内部。");
      return;
    }
    splitMutation.mutate({ unit: target, splitAt: timelineTime });
  };

  const mergeSelectedUnits = () => {
    if (!workspace) return;
    if (!editReason.trim()) {
      setOperationStatus("请先填写人工编辑原因，原因会进入溯源审计链。");
      return;
    }
    const selected = workspace.dialogue_units.filter((unit) =>
      selectedUnitIds.has(String(unit.id)),
    );
    const adjacent = selected.every(
      (unit, index) =>
        index === 0 || unit.unit_index === selected[index - 1].unit_index + 1,
    );
    if (selected.length !== 2 || !adjacent) {
      setOperationStatus("请选择两个相邻的对话单元进行合并。");
      return;
    }
    unitMergeMutation.mutate({
      unit: selected[0],
      otherUnit: selected[1],
    });
  };
  const runAutomaticSegmentation = () => {
    if (!workspace) return;
    if (
      workspace.window.total_dialogue_units > 0 &&
      !window.confirm(
        "将替换现有的未标注自动切分单元。人工编辑、锁定或已标注单元会由后端拒绝覆盖。是否继续？",
      )
    ) {
      return;
    }
    segmentationMutation.mutate();
  };

  const navigateWindow = (startSec: number | null) => {
    if (startSec === null) return;
    setSelectedUnitIds(new Set());
    setTimelineTime(startSec);
    setWindowStartSec(startSec);
  };

  if (!id) {
    return (
      <div className="ag-feature-empty" role="alert">
        <h1>无法打开接待工作台</h1>
        <p>路由中缺少接待 ID。</p>
      </div>
    );
  }

  if (workspaceQuery.isPending) {
    return (
      <div className="ag-feature-loading" role="status" aria-live="polite">
        正在加载接待、录音和证据…
      </div>
    );
  }

  if (workspaceQuery.isError || !workspace) {
    return (
      <div className="ag-feature-empty" role="alert">
        <h1>接待工作台暂不可用</h1>
        <p>
          未获取到真实接待数据：
          {errorMessage(workspaceQuery.error)}
        </p>
        <button type="button" onClick={() => workspaceQuery.refetch()}>
          重新加载
        </button>
      </div>
    );
  }

  return (
    <div className="ag-reception-page">
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">
            {workspace.reception.scenario === "gold"
              ? "金店销售接待"
              : workspace.reception.scenario === "automotive"
                ? "汽车销售接待"
                : "自定义接待场景"}
          </span>
          <h1>接待调听工作台</h1>
          <p>
            接待 #{workspace.reception.id} · {workspace.reception.store_id} ·{" "}
            {workspace.reception.agent_name ?? "未关联销售"} · v
            {workspace.reception.version}
          </p>
        </div>
        <div className="ag-feature-header__actions">
          {automationQuery.data && (
            <span className="ag-automation-chip">
              自动化 {automationQuery.data.status} ·{" "}
              {automationQuery.data.stage} · 第{" "}
              {automationQuery.data.attempt_count} 次
            </span>
          )}
          {automationQuery.data?.status !== "ready" && (
            <button
              type="button"
              className="ag-header-action"
              disabled={automationMutation.isPending}
              onClick={() => automationMutation.mutate()}
            >
              {automationMutation.isPending
                ? "自动处理中…"
                : automationQuery.data?.status === "failed"
                  ? "从检查点重试"
                  : "运行自动分析"}
            </button>
          )}
          <span
            className={`ag-status ag-status--${workspace.reception.status}`}
          >
            {workspace.reception.status}
          </span>
        </div>
      </header>
      <ReceptionContextTabs receptionId={id} />

      {operationStatus && (
        <div className="ag-operation-status" role="status" aria-live="polite">
          {operationStatus}
        </div>
      )}

      <main className="ag-workbench">
        <aside
          className="ag-workbench__queue"
          role="region"
          aria-label="接待与短录音队列"
        >
          <div className="ag-section-title">
            <div>
              <h2>短录音队列</h2>
              <span>{workspace.recordings.length} 段源录音</span>
            </div>
            <span>{formatClock(workspace.reception.duration_sec)}</span>
          </div>

          {workspace.reception.merged_audio_url && (
            <button
              type="button"
              className={`ag-source-master${activeSourceId === null ? " is-active" : ""}`}
              aria-pressed={activeSourceId === null}
              onClick={() => seekSource(null, timelineTime)}
            >
              <span>合并接待音轨</span>
              <small>{workspace.reception.merge_mode}</small>
            </button>
          )}

          <ol className="ag-recording-list">
            {workspace.recordings.map((recording) => {
              const key = String(recording.id);
              const active = activeSourceId === key;
              return (
                <li key={key} className={active ? "is-active" : undefined}>
                  <span className="ag-recording-list__sequence">
                    <span>{recording.sequence_no + 1}</span>
                  </span>
                  <button
                    type="button"
                    className="ag-recording-list__listen"
                    aria-pressed={active}
                    onClick={() => seekSource(key, recording.source_start_sec)}
                  >
                    <strong>{recording.name}</strong>
                    <small>
                      {formatClock(recording.timeline_start_sec)}–
                      {formatClock(recording.timeline_end_sec)}
                    </small>
                    <em>
                      {recording.decision_source} ·{" "}
                      {formatPercent(recording.merge_confidence)}
                    </em>
                  </button>
                </li>
              );
            })}
          </ol>

          <div className="ag-merge-controls">
            <label>
              录音合并模式
              <select
                aria-label="录音合并模式"
                value={mergeMode}
                onChange={(event) =>
                  setMergeMode(event.target.value as ReceptionMergeMode)
                }
              >
                <option value="logical">仅逻辑合并</option>
                <option value="physical">仅生成物理音频</option>
                <option value="both">逻辑 + 物理合并</option>
              </select>
            </label>
            <button
              type="button"
              disabled={
                workspace.recordings.length < 2 ||
                recordingMergeMutation.isPending
              }
              onClick={() => recordingMergeMutation.mutate()}
            >
              按当前顺序重新合并全部 {workspace.recordings.length} 段
            </button>
            <small>
              此操作会重建完整源录音映射，不会静默移除队列中的录音。
            </small>
          </div>
        </aside>

        <section
          className="ag-workbench__main"
          role="region"
          aria-label="多轨时间轴与转写"
        >
          <div className="ag-player">
            <div className="ag-player__meta">
              <div>
                <span>
                  {activeRecording
                    ? `源录音 · ${activeRecording.name}`
                    : "接待合并音轨"}
                </span>
                <strong>
                  {formatClock(timelineTime)} /{" "}
                  {formatClock(workspace.reception.duration_sec)}
                </strong>
              </div>
              {!audioUrl && <em>当前录音没有可播放 URL</em>}
            </div>
            <audio
              ref={audioRef}
              key={audioUrl ?? "no-audio"}
              src={audioUrl ?? undefined}
              controls
              preload="metadata"
              onLoadedMetadata={handleAudioMetadata}
              onTimeUpdate={handleTimeUpdate}
              onError={handleAudioError}
              aria-label="接待录音播放器"
            >
              您的浏览器不支持音频播放。
            </audio>
          </div>

          {(workspace.window.has_previous ||
            workspace.window.has_next ||
            workspace.window.truncated) && (
            <nav
              className="ag-workspace-window-nav"
              aria-label="接待时间窗口"
            >
              <div>
                <strong>
                  当前窗口 {formatClock(workspace.window.start_sec)}–
                  {formatClock(workspace.window.end_sec)}
                </strong>
                <span>
                  对话 {workspace.window.dialogue_units.returned}/
                  {workspace.window.dialogue_units.total} · 转写{" "}
                  {workspace.window.transcript_items.returned}/
                  {workspace.window.transcript_items.total}
                </span>
                {workspace.window.truncated && (
                  <em>当前高密度窗口已按响应预算截断</em>
                )}
              </div>
              <div>
                <button
                  type="button"
                  aria-label="上一时间窗口"
                  disabled={
                    !workspace.window.has_previous ||
                    workspaceQuery.isFetching
                  }
                  onClick={() =>
                    navigateWindow(workspace.window.previous_start_sec)
                  }
                >
                  上一窗口
                </button>
                <button
                  type="button"
                  aria-label="下一时间窗口"
                  disabled={
                    !workspace.window.has_next || workspaceQuery.isFetching
                  }
                  onClick={() =>
                    navigateWindow(workspace.window.next_start_sec)
                  }
                >
                  下一窗口
                </button>
              </div>
            </nav>
          )}

          <MultiTrackTimeline
            workspace={workspace}
            currentTime={timelineTime}
            selectedUnitIds={selectedUnitIds}
            onSeek={seekTimeline}
            onToggleUnit={toggleUnit}
          />

          <div className="ag-dialogue-toolbar">
            <div>
              <strong>对话编辑</strong>
              <span>已选 {selectedUnitIds.size} 个语义单元</span>
            </div>
            <button
              type="button"
              disabled={
                segmentationMutation.isPending ||
                workspace.window.protected_dialogue_units > 0
              }
              title={
                workspace.window.protected_dialogue_units > 0
                  ? "已有标签、人工编辑或锁定单元，自动替换已受保护"
                  : undefined
              }
              onClick={runAutomaticSegmentation}
            >
              {workspace.window.total_dialogue_units > 0
                ? "重新运行自动切分"
                : "运行自动对话切分"}
            </button>
            <label>
              <span>编辑原因</span>
              <input
                value={editReason}
                maxLength={500}
                placeholder="必填，将写入溯源审计"
                aria-label="对话编辑原因"
                onChange={(event) => setEditReason(event.target.value)}
              />
            </label>
            <button
              type="button"
              disabled={splitMutation.isPending || !editReason.trim()}
              onClick={splitAtCurrentTime}
            >
              在当前播放点切分
            </button>
            <button
              type="button"
              disabled={
                selectedUnitIds.size !== 2 ||
                unitMergeMutation.isPending ||
                !editReason.trim()
              }
              onClick={mergeSelectedUnits}
            >
              合并相邻对话
            </button>
          </div>

          <section
            className="ag-tag-derive-panel"
            aria-labelledby="derive-target-tags-title"
          >
            <div className="ag-tag-derive-panel__heading">
              <div>
                <strong id="derive-target-tags-title">目标对话标签</strong>
                <span>基于已验证对话证据，按不可变规则版本写入数据库。</span>
              </div>
              <button
                type="button"
                disabled={
                  workspace.window.total_dialogue_units === 0 ||
                  targetLabels.size === 0 ||
                  !tagGroupKey.trim() ||
                  !tagGroupVersion.trim() ||
                  deriveTagsMutation.isPending
                }
                onClick={() => deriveTagsMutation.mutate()}
              >
                {deriveTagsMutation.isPending ? "正在派生…" : "派生目标标签"}
              </button>
            </div>
            <div className="ag-tag-derive-panel__controls">
              <label>
                标签组
                <input
                  aria-label="目标标签组"
                  value={tagGroupKey}
                  maxLength={64}
                  pattern="[\w.-]+"
                  onChange={(event) => setTagGroupKey(event.target.value)}
                />
              </label>
              <label>
                规则版本
                <input
                  aria-label="目标标签规则版本"
                  value={tagGroupVersion}
                  maxLength={64}
                  pattern="[\w.-]+"
                  onChange={(event) => setTagGroupVersion(event.target.value)}
                />
              </label>
              <fieldset>
                <legend>派生维度</legend>
                {TARGET_LABELS.map((item) => (
                  <label key={item.key}>
                    <input
                      type="checkbox"
                      checked={targetLabels.has(item.key)}
                      aria-label={`派生${item.label}标签`}
                      onChange={() =>
                        setTargetLabels((current) => {
                          const next = new Set(current);
                          if (next.has(item.key)) next.delete(item.key);
                          else next.add(item.key);
                          return next;
                        })
                      }
                    />
                    {item.label}
                  </label>
                ))}
              </fieldset>
            </div>
            {deriveTagsMutation.data && (
              <div
                className="ag-tag-derive-result"
                role="status"
                aria-live="polite"
              >
                <div>
                  <strong>
                    {deriveTagsMutation.data.group_key}@
                    {deriveTagsMutation.data.group_version}
                  </strong>
                  <span>
                    {deriveTagsMutation.data.no_op
                      ? "当前版本无变化"
                      : `写入 ${deriveTagsMutation.data.assignment_count} 个标签`}
                    {" · "}
                    缺失 {deriveTagsMutation.data.missing.length} 个
                  </span>
                </div>
                {deriveTagsMutation.data.assignments.length > 0 && (
                  <ul>
                    {deriveTagsMutation.data.assignments
                      .slice(0, 12)
                      .map((assignment) => (
                        <li key={assignment.id}>
                          {assignment.label_key} = {assignment.label_value}
                        </li>
                      ))}
                  </ul>
                )}
              </div>
            )}
          </section>

          <div className="ag-transcript" aria-label="对话转写">
            {workspace.transcript_items.length === 0 ? (
              <p className="ag-empty-inline">暂无转写数据</p>
            ) : (
              workspace.transcript_items.map((item) => (
                <button
                  type="button"
                  key={String(item.id)}
                  className={`ag-transcript__item ag-transcript__item--${item.speaker_role}`}
                  onClick={() => seekTimeline(item.start_sec)}
                  aria-label={`定位转写 ${formatClock(item.start_sec)} ${item.text}`}
                >
                  <time>{formatClock(item.start_sec)}</time>
                  <strong>{item.speaker_label}</strong>
                  <span>{item.text}</span>
                </button>
              ))
            )}
          </div>
        </section>

        <aside
          className="ag-workbench__evidence"
          role="region"
          aria-label="证据标签与审计"
        >
          <EvidenceAuditPanel
            workspace={workspace}
            onSeekEvidence={seekEvidence}
          />
        </aside>
      </main>
    </div>
  );
}
