import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Link,
  useLocation,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  cancelReceptionAudioOperation,
  correctReceptionDialogueTag,
  createTagJob,
  getTagJob,
  createTagReviewBatch,
  createReceptionAudioOperation,
  createReceptionAudioPlan,
  decideTagReview,
  deriveReceptionDialogueTags,
  getReceptionAutomation,
  getReceptionAudioOperation,
  getReceptionProvenance,
  getTagFactLineage,
  getReceptionWorkspace,
  mergeDialogueUnits,
  mergeReceptionRecordings,
  listTagSchemas,
  runReceptionAutomation,
  segmentReception,
  splitDialogueUnit,
} from "@/api/services";
import { getRecordingSpeakers } from "@/api/speakers";
import { EvidenceAuditPanel } from "@/components/dialogue/EvidenceAuditPanel";
import { FloatingSubtitle } from "@/components/dialogue/FloatingSubtitle";
import { formatClock, formatPercent } from "@/components/dialogue/format";
import { MultiTrackTimeline } from "@/components/dialogue/MultiTrackTimeline";
import { LiveAudioCapturePanel } from "./LiveAudioCapturePanel";
import {
  TagAssignmentEditor,
  type TagCorrectionDraft,
} from "@/components/dialogue/TagAssignmentEditor";
import {
  ReceptionAuditChainDrawer,
  type AuditChainTarget,
} from "@/components/dialogue/ReceptionAuditChainDrawer";
import { TagFactLineageDrawer } from "@/components/dialogue/TagFactLineageDrawer";
import { ReceptionContextTabs } from "@/components/navigation/ContextNavigation";
import { useAuthStore } from "@/stores/auth";
import { getErrorMessage, getErrorStatus } from "@/utils/errors";
import type {
  DialogueEvidenceRef,
  DialogueTargetLabel,
  EntityId,
  ReceptionAudioPlanResponse,
  ReceptionDialogueUnit,
  ReceptionMergeMode,
  ReceptionRecordingItem,
  ReceptionTagAssignment,
  RecordingSpeakerRef,
  TagDefinition,
  TagJobStatus,
} from "@/types/api";
import { isTerminalTagJob, tagJobPollInterval } from "@/utils/tagJobs";

type AudioSourceId = string | null;

interface PendingSeek {
  sourceId: AudioSourceId;
  second: number;
}

interface SilenceGapPlayback {
  timelineStartSec: number;
  timelineEndSec: number;
  nextSourceId: string;
  nextSourceSecond: number;
  startedAtMs: number;
  resumeAfterGap: boolean;
}

type GeometryMutation =
  | "automation"
  | "audio"
  | "segmentation"
  | "dialogue-split"
  | "dialogue-merge";

const WORKSPACE_WINDOW_SIZE_SEC = 600;

const LEGACY_TARGET_LABELS: Array<{
  key: DialogueTargetLabel;
  label: string;
}> = [
  { key: "stage", label: "阶段" },
  { key: "intent", label: "意向" },
  { key: "objection", label: "异议" },
  { key: "next_step", label: "下一步" },
  { key: "compliance_risk", label: "合规风险" },
];

function stableJobKey(parts: string[]): string {
  let hash = 0x811c9dc5;
  for (const character of parts.join("|")) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 0x01000193);
  }
  return `workspace-tag-${(hash >>> 0).toString(16)}`;
}

function recordingId(recording: ReceptionRecordingItem): EntityId {
  return recording.recording_id ?? recording.id;
}

function mappingId(recording: ReceptionRecordingItem): EntityId {
  return recording.mapping_id ?? recording.id;
}

function recordingKey(recording: ReceptionRecordingItem): string {
  return String(recordingId(recording));
}

function mappingKey(recording: ReceptionRecordingItem): string {
  return String(mappingId(recording));
}

function sourceEnd(recording: ReceptionRecordingItem): number {
  return (
    recording.source_end_sec ??
    recording.source_start_sec +
      Math.max(recording.timeline_end_sec - recording.timeline_start_sec, 0)
  );
}

function clampSourceSecond(
  recording: ReceptionRecordingItem,
  second: number,
): number {
  return Math.min(
    Math.max(second, recording.source_start_sec),
    sourceEnd(recording),
  );
}

function windowStartForTimeline(second: number, duration: number): number {
  if (duration <= WORKSPACE_WINDOW_SIZE_SEC) return 0;
  const bounded = Math.min(
    Math.max(second, 0),
    Math.max(duration - Number.EPSILON, 0),
  );
  return (
    Math.floor(bounded / WORKSPACE_WINDOW_SIZE_SEC) *
    WORKSPACE_WINDOW_SIZE_SEC
  );
}

function audioOperationKey(
  receptionId: EntityId,
  version: number,
  planToken: string,
): string {
  return `workspace-audio-${String(receptionId)}-${version}-${stableJobKey([
    planToken,
  ]).replace("workspace-tag-", "")}`;
}

function tagCorrectionErrorMessage(error: unknown): string {
  const status = getErrorStatus(error);
  if (status === 409) {
    return "标签已被其他人更新。你的草稿仍保留，请刷新对照最新版本后再次确认。";
  }
  if (status === 403) {
    return "当前账号没有人工更正权限。草稿已保留，可复制后交由质检员处理。";
  }
  if (status === 422) {
    return "标签值、原因或证据不符合服务端规则，请检查标记字段。";
  }
  return `保存失败，草稿未丢失：${getErrorMessage(error)}`;
}

function idEquals(left: EntityId, right: EntityId): boolean {
  return String(left) === String(right);
}

function positiveNumericId(value: EntityId): number | null {
  const numeric = typeof value === "number" ? value : Number(value);
  return Number.isSafeInteger(numeric) && numeric > 0 ? numeric : null;
}

function canonicalFactId(modelRunId: string | null | undefined): number | null {
  if (!modelRunId?.startsWith("fact:")) return null;
  return positiveNumericId(modelRunId.slice("fact:".length));
}

/** 卡片标题跟着任务状态走：固定写「已入队」会把跑完和失败都说成排队中。 */
function tagJobHeadline(status: TagJobStatus): string {
  switch (status) {
    case "completed":
    case "succeeded":
      return "标签重算已完成，本页已刷新";
    case "failed":
      return "标签重算失败";
    case "cancelled":
      return "标签重算已取消";
    case "running":
      return "标签重算进行中";
    default:
      return "后台标签任务已入队";
  }
}

export default function ReceptionWorkspacePage() {
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const currentUser = useAuthStore((state) => state.user);
  const audioRef = useRef<HTMLAudioElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  // 悬浮字幕的时间与播放态一律来自 <audio> 本体:自己维护一份会和音频漂移。
  const [playbackSec, setPlaybackSec] = useState(0);
  const [playbackDuration, setPlaybackDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const deepLinkAppliedRef = useRef(false);
  const initializedReceptionRef = useRef<string | null>(null);
  const initializedWindowRef = useRef<string | null>(null);
  const initializedAudioDraftRef = useRef<string | null>(null);
  const playbackGrantRefreshRef = useRef(false);
  const pendingAutoPlayRef = useRef(false);
  const silenceGapRef = useRef<SilenceGapPlayback | null>(null);
  const geometryMutationRef = useRef<GeometryMutation | null>(null);
  const draggedMappingRef = useRef<string | null>(null);
  const previousAutomationStatusRef = useRef<string | null>(null);
  const [activeSourceId, setActiveSourceId] = useState<AudioSourceId>(null);
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const syncTime = () => setPlaybackSec(audio.currentTime);
    const syncMeta = () =>
      setPlaybackDuration(Number.isFinite(audio.duration) ? audio.duration : 0);
    const play = () => setIsPlaying(true);
    const pause = () => setIsPlaying(false);
    audio.addEventListener("timeupdate", syncTime);
    audio.addEventListener("loadedmetadata", syncMeta);
    audio.addEventListener("durationchange", syncMeta);
    audio.addEventListener("play", play);
    audio.addEventListener("pause", pause);
    audio.addEventListener("ended", pause);
    syncMeta();
    return () => {
      audio.removeEventListener("timeupdate", syncTime);
      audio.removeEventListener("loadedmetadata", syncMeta);
      audio.removeEventListener("durationchange", syncMeta);
      audio.removeEventListener("play", play);
      audio.removeEventListener("pause", pause);
      audio.removeEventListener("ended", pause);
    };
  }, [activeSourceId]);
  const [pendingSeek, setPendingSeek] = useState<PendingSeek | null>(null);
  const [timelineTime, setTimelineTime] = useState(0);
  const [silenceGap, setSilenceGap] =
    useState<SilenceGapPlayback | null>(null);
  const [windowStartSec, setWindowStartSec] = useState(0);
  const [selectedUnitIds, setSelectedUnitIds] = useState<Set<string>>(
    new Set(),
  );
  const [selectedTagId, setSelectedTagId] = useState<EntityId | null>(null);
  const [tagCorrectionError, setTagCorrectionError] = useState<string | null>(
    null,
  );
  const [lineageFactId, setLineageFactId] = useState<number | null>(null);
  const [auditChainTarget, setAuditChainTarget] =
    useState<AuditChainTarget | null>(null);
  const [mergeMode, setMergeMode] = useState<ReceptionMergeMode>("both");
  const [editReason, setEditReason] = useState("");
  const [operationStatus, setOperationStatus] = useState<string | null>(null);
  const [activeGeometryMutation, setActiveGeometryMutation] =
    useState<GeometryMutation | null>(null);
  const [sourceOrder, setSourceOrder] = useState<string[]>([]);
  const [sourceGapMs, setSourceGapMs] = useState<Record<string, number>>({});
  const [audioPlan, setAudioPlan] =
    useState<ReceptionAudioPlanResponse | null>(null);
  const [activeAudioOperationId, setActiveAudioOperationId] =
    useState<EntityId | null>(null);
  const [tagGroupKey, setTagGroupKey] = useState("reception-rules");
  const [tagGroupVersion, setTagGroupVersion] = useState("rules-v1");
  const [schemaVersionId, setSchemaVersionId] = useState<number | null>(null);
  const [targetLabels, setTargetLabels] = useState<Set<string>>(new Set());
  // 只存 ID：任务的状态由下面的查询跟进。存整个对象就等于把「已入队」
  // 那一刻的快照永久钉在页面上——重算跑完、失败、被取消，卡片都不会变。
  const [tagJobId, setTagJobId] = useState<number | null>(null);
  const initializedSchemaRef = useRef<number | null>(null);
  const initializedLegacyRef = useRef(false);

  const withGeometryMutation = useCallback(
    async <T,>(
      mutation: GeometryMutation,
      task: () => Promise<T>,
    ): Promise<T> => {
      if (geometryMutationRef.current !== null) {
        throw new Error(
          `已有${geometryMutationRef.current}操作进行中，请等待完成后再试`,
        );
      }
      geometryMutationRef.current = mutation;
      setActiveGeometryMutation(mutation);
      try {
        return await task();
      } finally {
        if (geometryMutationRef.current === mutation) {
          geometryMutationRef.current = null;
          setActiveGeometryMutation(null);
        }
      }
    },
    [],
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
    placeholderData: (previous) => previous,
  });
  // 重算任务由后台 worker 执行，工作台必须跟进到终态：跑完之后这一页的
  // 标签才是新的，而在此之前页面画的仍然是重算前的那一版。
  const tagJobQuery = useQuery({
    queryKey: ["tag-job", tagJobId],
    queryFn: () => getTagJob(tagJobId ?? 0),
    enabled: tagJobId !== null,
    retry: false,
    refetchInterval: (query) => tagJobPollInterval(query.state.data?.status),
  });
  const tagJob = tagJobQuery.data ?? null;
  const tagJobSettled = isTerminalTagJob(tagJob?.status);
  useEffect(() => {
    // 只在落到终态的那一次刷新工作台。写在 effect 里而不是 onSuccess，
    // 是因为轮询的每一轮都会 onSuccess，那样会变成 3 秒一次全量重取。
    if (tagJobSettled) {
      void queryClient.invalidateQueries({
        queryKey: ["reception-workspace", id],
      });
    }
  }, [id, queryClient, tagJobSettled]);
  const automationQuery = useQuery({
    queryKey: ["reception-automation", id],
    queryFn: () => getReceptionAutomation(id ?? ""),
    enabled: Boolean(id),
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "pending" || status === "running" ? 3_000 : false;
    },
  });
  const audioOperationQuery = useQuery({
    queryKey: [
      "reception-audio-operation",
      id,
      activeAudioOperationId,
    ],
    queryFn: () =>
      getReceptionAudioOperation(id ?? "", activeAudioOperationId ?? ""),
    enabled: Boolean(id && activeAudioOperationId !== null),
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status &&
        !["succeeded", "failed", "cancelled"].includes(status)
        ? 1_500
        : false;
    },
  });
  const schemasQuery = useQuery({
    queryKey: ["tag-schemas", "published"],
    queryFn: listTagSchemas,
    retry: false,
  });
  const lineageQuery = useQuery({
    queryKey: ["tag-fact-lineage", lineageFactId],
    queryFn: () => getTagFactLineage(lineageFactId ?? 0),
    enabled: lineageFactId !== null,
    retry: false,
  });
  const auditChainQuery = useQuery({
    queryKey: [
      "reception-provenance",
      auditChainTarget?.objectType,
      String(auditChainTarget?.objectRef ?? ""),
    ],
    queryFn: () =>
      getReceptionProvenance(auditChainTarget?.objectRef ?? "", {
        objectType: auditChainTarget?.objectType,
      }),
    enabled: auditChainTarget !== null,
    retry: false,
  });
  const workspace = workspaceQuery.data;

  // Resolve each recording's `spk_N` labels to canonical speakers, so the
  // timeline can show who is speaking and how firm that attribution is.
  // Fetched here rather than inside the timeline: the timeline is a
  // presentational component and its tests render it without a query client.
  const timelineRecordingIds = useMemo(
    () => [
      ...new Set(
        (workspace?.recordings ?? [])
          .map((recording) => Number(recording.recording_id))
          .filter((id) => Number.isFinite(id) && id > 0),
      ),
    ],
    [workspace?.recordings],
  );
  const recordingSpeakerQueries = useQueries({
    queries: timelineRecordingIds.map((id) => ({
      queryKey: ["recording-speakers", id],
      queryFn: () => getRecordingSpeakers(id),
      staleTime: 5 * 60_000,
      retry: false,
    })),
  });
  const recordingSpeakerStamp = recordingSpeakerQueries
    .map((query) => query.dataUpdatedAt)
    .join(",");
  // 至少一条录音解析失败。必须往下传：解析不出来时每个块都退回原始 spk_N 且不带 ⚠，
  // 于是「待复核的临时归属」和「已确认的归属」在这个视图里长得一模一样——
  // 而 ⚠ 存在的唯一理由就是不让低置信度的合并看起来像确定的。
  const speakerResolutionFailed = recordingSpeakerQueries.some((query) => query.isError);
  const speakerByLabel = useMemo(() => {
    const map = new Map<string, RecordingSpeakerRef>();
    for (const query of recordingSpeakerQueries) {
      const payload = query.data;
      if (!payload) continue;
      for (const ref of payload.items) {
        map.set(`${payload.recording_id}:${ref.source_speaker_label}`, ref);
      }
    }
    return map;
    // recordingSpeakerQueries is a fresh array each render; the stamp changes
    // only when one of the queries actually resolves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recordingSpeakerStamp]);
  const publishedSchemaVersions = useMemo(
    () =>
      (schemasQuery.data?.items ?? []).flatMap((schema) =>
        (schema.versions ?? [])
          .filter((version) => version.status === "published")
          .map((version) => ({
            schemaName: schema.name,
            schemaActive: schema.active_version_id === version.id,
            version,
          })),
      ),
    [schemasQuery.data],
  );
  const activeSchemaVersion = useMemo(
    () =>
      publishedSchemaVersions.find(
        (item) => item.version.id === schemaVersionId,
      ) ??
      publishedSchemaVersions.find((item) => item.schemaActive) ??
      publishedSchemaVersions[0] ??
      null,
    [publishedSchemaVersions, schemaVersionId],
  );
  const activeDefinitions = useMemo(
    () =>
      (activeSchemaVersion?.version.definitions ?? []).filter(
        (definition) =>
          definition.scenarios.length === 0 ||
          definition.scenarios.includes(
            workspace?.reception.scenario ?? "custom",
          ),
      ),
    [activeSchemaVersion, workspace?.reception.scenario],
  );
  const selectedTag = useMemo(
    () =>
      workspace?.tag_assignments.find(
        (tag) =>
          selectedTagId !== null &&
          String(tag.id) === String(selectedTagId),
      ) ?? null,
    [selectedTagId, workspace?.tag_assignments],
  );
  const selectedTagDefinition = useMemo(
    () =>
      selectedTag
        ? activeDefinitions.find(
            (definition) => definition.key === selectedTag.label_key,
          )
        : undefined,
    [activeDefinitions, selectedTag],
  );
  const hasLegacyWriteRole =
    currentUser?.role === "admin" || currentUser?.role === "inspector";
  const canManageAudio =
    workspace?.capabilities?.can_manage_audio ?? hasLegacyWriteRole;
  const canRunSegmentation =
    workspace?.capabilities?.can_run_segmentation ?? hasLegacyWriteRole;
  const canEditDialogue =
    workspace?.capabilities?.can_edit_dialogue ?? hasLegacyWriteRole;
  const canEditTags =
    workspace?.capabilities?.can_edit_tags ?? hasLegacyWriteRole;
  const supportsAudioPlans =
    workspace?.capabilities?.supports_audio_plans ?? false;
  const supportsAudioOperations =
    workspace?.capabilities?.supports_audio_operations ?? false;
  const canCancelAudioOperation =
    workspace?.capabilities?.can_cancel_audio_operation ?? false;
  const canStreamAudio =
    workspace?.capabilities?.can_stream_audio ?? false;
  const legacyFallback =
    schemasQuery.isSuccess && publishedSchemaVersions.length === 0;

  useEffect(() => {
    const nextVersionId = activeSchemaVersion?.version.id ?? null;
    if (nextVersionId === null) {
      initializedSchemaRef.current = null;
      setSchemaVersionId(null);
      setTargetLabels(new Set());
      return;
    }
    if (schemaVersionId !== nextVersionId) {
      setSchemaVersionId(nextVersionId);
    }
    if (initializedSchemaRef.current !== nextVersionId) {
      initializedSchemaRef.current = nextVersionId;
      setTargetLabels(
        new Set(activeDefinitions.map((definition) => definition.key)),
      );
    }
  }, [activeDefinitions, activeSchemaVersion, schemaVersionId]);

  useEffect(() => {
    if (!legacyFallback) {
      initializedLegacyRef.current = false;
      return;
    }
    if (!initializedLegacyRef.current) {
      initializedLegacyRef.current = true;
      setTargetLabels(
        new Set(LEGACY_TARGET_LABELS.map((definition) => definition.key)),
      );
    }
  }, [legacyFallback]);

  useEffect(() => {
    setWindowStartSec(0);
    initializedWindowRef.current = null;
    previousAutomationStatusRef.current = null;
  }, [id]);

  useEffect(() => {
    const state = location.state as { automationMessage?: unknown } | null;
    if (typeof state?.automationMessage === "string") {
      setOperationStatus(state.automationMessage);
    }
  }, [location.state]);

  useEffect(() => {
    const automation = automationQuery.data;
    const previousStatus = previousAutomationStatusRef.current;
    previousAutomationStatusRef.current = automation?.status ?? null;
    if (automation?.status === "failed") {
      setOperationStatus(
        `自动处理停在${automation.stage}阶段：${automation.last_error_message ?? "修复源数据后可从检查点重试"}`,
      );
    } else if (
      automation?.status === "ready" &&
      (previousStatus === "pending" || previousStatus === "running")
    ) {
      void queryClient.invalidateQueries({
        queryKey: ["reception-workspace", id],
      });
    }
  }, [automationQuery.data, id, queryClient]);

  useEffect(() => {
    const operation = audioOperationQuery.data;
    if (!operation) return;
    const percent = Math.round(Math.min(Math.max(operation.progress, 0), 1) * 100);
    if (operation.status === "succeeded") {
      setOperationStatus("音频任务已完成，活动时间线与播放产物正在刷新。");
      setAudioPlan(null);
      void queryClient.invalidateQueries({
        queryKey: ["reception-workspace", id],
      });
    } else if (operation.status === "failed") {
      // 幂等键由（接待, 版本, 计划 token）确定性派生：保留失败前的旧计划会让
      // “重试”携带同一个已终结的键。清空计划迫使用户重新生成预览，键随新
      // token 自然轮换，重试才会真正入队。
      setAudioPlan(null);
      setOperationStatus(
        `音频任务失败，源顺序草稿仍保留：${
          operation.error_message ??
          operation.error ??
          operation.error_code ??
          "请检查媒体兼容性后重试"
        }`,
      );
    } else if (operation.status === "cancelled") {
      setOperationStatus("音频任务已取消，当前活动时间线未改变。");
    } else {
      setOperationStatus(
        `音频任务 ${operation.status} · ${percent}%（可安全离开后返回查看）`,
      );
    }
  }, [audioOperationQuery.data, id, queryClient]);

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
            ? recordingKey(workspace.recordings[0])
            : null,
      );
    }
    if (initializedAudioDraftRef.current !== receptionIdentity) {
      initializedAudioDraftRef.current = receptionIdentity;
      const ordered = [...workspace.recordings].sort(
        (left, right) => left.sequence_no - right.sequence_no,
      );
      setSourceOrder(ordered.map(mappingKey));
      setSourceGapMs(
        Object.fromEntries(
          ordered.map((recording, index) => [
            mappingKey(recording),
            index === 0
              ? 0
              : Math.max(Math.round(recording.gap_before_sec * 1_000), 0),
          ]),
        ),
      );
      setAudioPlan(null);
      setActiveAudioOperationId(workspace.active_audio_operation?.id ?? null);
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
        (recording) => recordingKey(recording) === activeSourceId,
      ) ?? null,
    [activeSourceId, workspace],
  );
  const audioUrl =
    activeRecording?.audio_url ??
    (activeSourceId === null ? workspace?.reception.merged_audio_url : null);
  const orderedRecordings = useMemo(() => {
    if (!workspace) return [];
    const byMapping = new Map(
      workspace.recordings.map((recording) => [
        mappingKey(recording),
        recording,
      ]),
    );
    const ordered = sourceOrder
      .map((key) => byMapping.get(key))
      .filter(
        (recording): recording is ReceptionRecordingItem =>
          recording !== undefined,
      );
    const included = new Set(ordered.map(mappingKey));
    return [
      ...ordered,
      ...workspace.recordings
        .filter((recording) => !included.has(mappingKey(recording)))
        .sort((left, right) => left.sequence_no - right.sequence_no),
    ];
  }, [sourceOrder, workspace]);
  const committedRecordings = useMemo(
    () =>
      [...(workspace?.recordings ?? [])].sort(
        (left, right) => left.sequence_no - right.sequence_no,
      ),
    [workspace?.recordings],
  );
  const operationRecordings =
    mergeMode === "physical" ? committedRecordings : orderedRecordings;
  const draftTimelineDurationSec = useMemo(
    () =>
      operationRecordings.reduce((duration, recording, index) => {
        const sourceDuration = Math.max(
          sourceEnd(recording) - recording.source_start_sec,
          0,
        );
        const gap =
          index === 0
            ? 0
            : mergeMode === "physical"
              ? Math.max(recording.gap_before_sec, 0)
              : Math.max(sourceGapMs[mappingKey(recording)] ?? 0, 0) /
                1_000;
        return duration + gap + sourceDuration;
      }, 0),
    [mergeMode, operationRecordings, sourceGapMs],
  );

  const applyPendingSeek = useCallback(() => {
    if (playbackGrantRefreshRef.current) return;
    if (!pendingSeek || pendingSeek.sourceId !== activeSourceId) return;
    if (audioRef.current) {
      audioRef.current.currentTime = Math.max(pendingSeek.second, 0);
      if (pendingAutoPlayRef.current) {
        pendingAutoPlayRef.current = false;
        void audioRef.current.play().catch(() => {
          setOperationStatus(
            "已切换到下一段源录音；浏览器阻止自动续播，请点击播放继续。",
          );
        });
      }
    }
    setPendingSeek(null);
  }, [activeSourceId, pendingSeek]);

  useEffect(() => {
    applyPendingSeek();
  }, [applyPendingSeek]);

  const seekSource = useCallback(
    (sourceId: AudioSourceId, second: number) => {
      silenceGapRef.current = null;
      setSilenceGap(null);
      const targetRecording =
        sourceId === null
          ? null
          : workspace?.recordings.find(
              (recording) => recordingKey(recording) === sourceId,
            ) ?? null;
      const safeSecond = targetRecording
        ? clampSourceSecond(targetRecording, second)
        : Math.max(second, 0);
      if (sourceId === activeSourceId && audioRef.current) {
        audioRef.current.currentTime = safeSecond;
        return;
      }
      setPendingSeek({ sourceId, second: safeSecond });
      setActiveSourceId(sourceId);
    },
    [activeSourceId, workspace?.recordings],
  );

  const ensureTimelineWindow = useCallback(
    (second: number) => {
      if (!workspace) return;
      const targetStart = windowStartForTimeline(
        second,
        workspace.reception.duration_sec,
      );
      if (targetStart !== workspace.window.start_sec) {
        setWindowStartSec(targetStart);
      }
    },
    [workspace],
  );

  useEffect(() => {
    if (!silenceGap) return;
    let frameId = 0;
    const tick = (timestamp: number) => {
      if (silenceGapRef.current !== silenceGap) return;
      const elapsedSec = Math.max(
        (timestamp - silenceGap.startedAtMs) / 1_000,
        0,
      );
      const nextTimelineTime = Math.min(
        silenceGap.timelineStartSec + elapsedSec,
        silenceGap.timelineEndSec,
      );
      setTimelineTime(nextTimelineTime);
      ensureTimelineWindow(nextTimelineTime);
      if (nextTimelineTime >= silenceGap.timelineEndSec) {
        silenceGapRef.current = null;
        setSilenceGap(null);
        pendingAutoPlayRef.current = silenceGap.resumeAfterGap;
        seekSource(
          silenceGap.nextSourceId,
          silenceGap.nextSourceSecond,
        );
        return;
      }
      frameId = requestAnimationFrame(tick);
    };
    frameId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frameId);
  }, [ensureTimelineWindow, seekSource, silenceGap]);

  const seekTimeline = useCallback(
    (second: number) => {
      if (!workspace) return;
      const safeSecond = Math.min(
        Math.max(second, 0),
        workspace.reception.duration_sec,
      );
      setTimelineTime(safeSecond);
      ensureTimelineWindow(safeSecond);
      if (workspace.reception.merged_audio_url) {
        seekSource(null, safeSecond);
        return;
      }
      const source = workspace.recordings.find((recording, index, items) => {
        const isLast = index === items.length - 1;
        return (
          safeSecond >= recording.timeline_start_sec &&
          (safeSecond < recording.timeline_end_sec ||
            (isLast && safeSecond === recording.timeline_end_sec))
        );
      });
      if (source) {
        const sourceSecond =
          source.source_start_sec + safeSecond - source.timeline_start_sec;
        seekSource(recordingKey(source), sourceSecond);
      }
    },
    [ensureTimelineWindow, seekSource, workspace],
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
        idEquals(recordingId(recording), evidence.recording_id),
      );
      const sourceSecond = source
        ? clampSourceSecond(source, evidence.start_ms / 1000)
        : evidence.start_ms / 1000;
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
        seekSource(recordingKey(source), sourceSecond);
      }
    },
    [seekSource, seekTimeline, workspace],
  );

  useEffect(() => {
    if (!workspace || deepLinkAppliedRef.current) return;
    const recordingIdParam = searchParams.get("recording");
    const rawMilliseconds = searchParams.get("at");
    if (!recordingIdParam || rawMilliseconds === null) return;
    const milliseconds = Number(rawMilliseconds);
    if (!Number.isFinite(milliseconds) || milliseconds < 0) return;
    const source = workspace.recordings.find((recording) =>
      idEquals(recordingId(recording), recordingIdParam),
    );
    if (!source) return;
    deepLinkAppliedRef.current = true;
    const sourceSecond = clampSourceSecond(source, milliseconds / 1000);
    if (workspace.reception.merged_audio_url) {
      const mergedSecond =
        source.timeline_start_sec + sourceSecond - source.source_start_sec;
      setTimelineTime(Math.max(mergedSecond, 0));
      ensureTimelineWindow(mergedSecond);
      seekSource(null, mergedSecond);
    } else {
      const timelineSecond =
        source.timeline_start_sec + sourceSecond - source.source_start_sec;
      setTimelineTime(timelineSecond);
      ensureTimelineWindow(timelineSecond);
      seekSource(recordingKey(source), sourceSecond);
    }
  }, [ensureTimelineWindow, searchParams, seekSource, workspace]);

  const handleTimeUpdate = (resumeAfterBoundary: boolean) => {
    if (silenceGapRef.current) return;
    const sourceTime = audioRef.current?.currentTime ?? 0;
    if (activeRecording) {
      const end = sourceEnd(activeRecording);
      if (sourceTime >= end - 0.001) {
        const audio = audioRef.current;
        const shouldResume =
          resumeAfterBoundary || Boolean(audio && !audio.paused);
        if (audio && audio.currentTime !== end) {
          audio.currentTime = end;
        }
        const boundaryTimeline = activeRecording.timeline_end_sec;
        setTimelineTime(boundaryTimeline);
        ensureTimelineWindow(boundaryTimeline);
        const next = [...(workspace?.recordings ?? [])]
          .sort(
            (left, right) =>
              left.timeline_start_sec - right.timeline_start_sec,
          )
          .find(
            (recording) =>
              recordingKey(recording) !== recordingKey(activeRecording) &&
              recording.timeline_start_sec >=
                activeRecording.timeline_end_sec - 0.001,
        );
        if (next) {
          const gapDurationSec = Math.max(
            next.timeline_start_sec -
              activeRecording.timeline_end_sec,
            0,
          );
          if (shouldResume && gapDurationSec > 0.001) {
            audio?.pause();
            const gap: SilenceGapPlayback = {
              timelineStartSec: activeRecording.timeline_end_sec,
              timelineEndSec: next.timeline_start_sec,
              nextSourceId: recordingKey(next),
              nextSourceSecond: next.source_start_sec,
              startedAtMs: performance.now(),
              resumeAfterGap: true,
            };
            silenceGapRef.current = gap;
            setSilenceGap(gap);
            return;
          }
          pendingAutoPlayRef.current = shouldResume;
          setTimelineTime(next.timeline_start_sec);
          ensureTimelineWindow(next.timeline_start_sec);
          seekSource(recordingKey(next), next.source_start_sec);
        } else if (audio) {
          audio.pause();
        }
        return;
      }
      const nextTimelineTime =
        activeRecording.timeline_start_sec +
          sourceTime -
        activeRecording.source_start_sec;
      setTimelineTime(nextTimelineTime);
      ensureTimelineWindow(nextTimelineTime);
    } else {
      setTimelineTime(sourceTime);
      ensureTimelineWindow(sourceTime);
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
      setOperationStatus(`播放凭证刷新失败：${getErrorMessage(result.error)}`);
      return;
    }
    const refreshedRecording = result.data?.recordings.find(
      (recording) => recordingKey(recording) === activeSourceId,
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

  const handleGeometryFailure = async (
    operation: string,
    error: unknown,
  ) => {
    if (getErrorStatus(error) === 409) {
      setOperationStatus(
        `${operation}发生版本冲突；源顺序与编辑原因草稿已保留，正在刷新最新版本供你核对后重试。`,
      );
      await workspaceQuery.refetch();
      return;
    }
    setOperationStatus(`${operation}失败：${getErrorMessage(error)}`);
  };

  const automationMutation = useMutation({
    mutationFn: async () => {
      if (!id) throw new Error("接待 ID 不可用");
      return withGeometryMutation("automation", () =>
        runReceptionAutomation(id),
      );
    },
    onSuccess: async (result) => {
      setOperationStatus(
        result.status === "ready"
          ? "兼容自动处理完成：录音合并、对话切分与旧规则标签均已就绪。"
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
      setOperationStatus(`自动处理请求失败：${getErrorMessage(error)}`);
    },
  });

  const recordingMergeMutation = useMutation({
    mutationFn: async () => {
      if (!workspace || !id) throw new Error("接待数据尚未加载");
      const recordingIds = operationRecordings.map(
        (recording) => recordingId(recording),
      );
      return withGeometryMutation("audio", () =>
        mergeReceptionRecordings(id, {
          recording_ids: recordingIds,
          mode: mergeMode,
          expected_version: workspace.reception.version,
        }),
      );
    },
    onSuccess: async () => {
      setOperationStatus("录音合并任务已提交，正在刷新接待版本。");
      await refreshWorkspace();
    },
    onError: (error) => handleGeometryFailure("录音合并", error),
  });

  const audioPlanMutation = useMutation({
    mutationFn: async () => {
      if (!workspace || !id) throw new Error("接待数据尚未加载");
      return createReceptionAudioPlan(id, {
        sources: operationRecordings.map((recording, index) => ({
          mapping_id: mappingId(recording),
          gap_before_ms:
            index === 0
              ? 0
              : mergeMode === "physical"
                ? Math.max(
                    Math.round(recording.gap_before_sec * 1_000),
                    0,
                  )
                : Math.max(
                    sourceGapMs[mappingKey(recording)] ?? 0,
                    0,
                  ),
        })),
        expected_version: workspace.reception.version,
      });
    },
    onSuccess: (plan) => {
      setAudioPlan(plan);
      setOperationStatus(
        "服务端已验证时间线计划；提交前仍可调整顺序或空档。",
      );
    },
    onError: (error) => handleGeometryFailure("音频计划预览", error),
  });

  const audioOperationMutation = useMutation({
    mutationFn: async () => {
      if (!workspace || !id || !audioPlan) {
        throw new Error("请先生成有效的音频计划");
      }
      return withGeometryMutation("audio", () =>
        createReceptionAudioOperation(
          id,
          {
            plan_token: audioPlan.plan_token,
            mode: mergeMode,
            expected_version: workspace.reception.version,
          },
          audioOperationKey(
            workspace.reception.id,
            workspace.reception.version,
            audioPlan.plan_token,
          ),
        ),
      );
    },
    onSuccess: (operation) => {
      setActiveAudioOperationId(operation.id);
      // 服务端对同一幂等键可能重放一个已终结的旧任务；必须按返回状态提示，
      // 不能把任何返回都宣布为“已入队”。
      if (operation.status === "succeeded") {
        setAudioPlan(null);
        setOperationStatus(
          `音频任务 #${operation.id} 此前已成功完成，本次为幂等重放，正在刷新产物。`,
        );
        void queryClient.invalidateQueries({
          queryKey: ["reception-workspace", id],
        });
      } else if (
        operation.status === "failed" ||
        operation.status === "cancelled"
      ) {
        setAudioPlan(null);
        setOperationStatus(
          `音频任务 #${operation.id} 已处于 ${operation.status} 终态，未重新执行；请重新生成合并预览后再提交。`,
        );
      } else {
        setOperationStatus(
          `音频任务 #${operation.id} 已入队，当前活动版本在任务提交成功前保持不变。`,
        );
      }
    },
    onError: (error) => handleGeometryFailure("音频任务提交", error),
  });

  const cancelAudioOperationMutation = useMutation({
    mutationFn: async () => {
      if (!id || activeAudioOperationId === null) {
        throw new Error("没有可取消的音频任务");
      }
      return cancelReceptionAudioOperation(id, activeAudioOperationId);
    },
    onSuccess: (operation) => {
      queryClient.setQueryData(
        ["reception-audio-operation", id, activeAudioOperationId],
        operation,
      );
    },
    onError: (error) => {
      setOperationStatus(`取消音频任务失败：${getErrorMessage(error)}`);
    },
  });

  const segmentationMutation = useMutation({
    mutationFn: async () => {
      if (!workspace || !id) throw new Error("接待数据尚未加载");
      return withGeometryMutation("segmentation", () =>
        segmentReception(id, {
          expected_version: workspace.reception.version,
          replace_auto: workspace.window.total_dialogue_units > 0,
          algorithm_version: "dialogue-hybrid-v2",
        }),
      );
    },
    onSuccess: async () => {
      setOperationStatus("自动对话切分已完成，状态轨与溯源链正在刷新。");
      await refreshWorkspace();
    },
    onError: (error) => handleGeometryFailure("自动对话切分", error),
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
      return withGeometryMutation("dialogue-split", () =>
        splitDialogueUnit(id, unit.id, {
          split_at_sec: splitAt,
          expected_reception_version: workspace.reception.version,
          expected_unit_version: unit.version,
          reason: editReason.trim(),
        }),
      );
    },
    onSuccess: async () => {
      setOperationStatus("对话切分已保存，并写入审计记录。");
      setSelectedUnitIds(new Set());
      await refreshWorkspace();
    },
    onError: (error) => handleGeometryFailure("对话切分", error),
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
      return withGeometryMutation("dialogue-merge", () =>
        mergeDialogueUnits(id, unit.id, {
          other_unit_id: otherUnit.id,
          expected_reception_version: workspace.reception.version,
          expected_unit_version: unit.version,
          expected_other_unit_version: otherUnit.version,
          reason: editReason.trim(),
        }),
      );
    },
    onSuccess: async () => {
      setOperationStatus("相邻对话单元已合并，并写入审计记录。");
      setSelectedUnitIds(new Set());
      await refreshWorkspace();
    },
    onError: (error) => handleGeometryFailure("对话单元合并", error),
  });

  const tagJobMutation = useMutation({
    mutationFn: async () => {
      if (!workspace || !id) throw new Error("接待数据尚未加载");
      if (!activeSchemaVersion || schemaVersionId === null) {
        throw new Error("当前没有可用的已发布标签体系版本");
      }
      const availableKeys = new Set(
        activeDefinitions.map((definition) => definition.key),
      );
      const selectedLabels = [...targetLabels]
        .filter((key) => availableKeys.has(key))
        .sort();
      if (selectedLabels.length === 0) {
        throw new Error("至少选择一个已发布标签维度");
      }
      const receptionScopeId = /^\d+$/.test(id) ? Number(id) : id;
      return createTagJob(
        {
          job_type: "recompute",
          scope: {
            reception_ids: [receptionScopeId],
            label_keys: selectedLabels,
            schema_version_id: schemaVersionId,
            trigger: "manual_workspace_rerun",
          },
        },
        stableJobKey([
          "recompute",
          id,
          String(schemaVersionId),
          ...selectedLabels,
        ]),
      );
    },
    onSuccess: (job) => {
      setTagJobId(job.id);
      queryClient.setQueryData(["tag-job", job.id], job);
      setOperationStatus(
        `标签重算任务 #${job.id} 已入队；抽取、写入与重试由后台 worker 执行。`,
      );
    },
    onError: (error) => {
      setOperationStatus(`标签重算任务创建失败：${getErrorMessage(error)}`);
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
      const selectedLabels = LEGACY_TARGET_LABELS.map((item) => item.key).filter(
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
      setOperationStatus(`目标标签派生失败：${getErrorMessage(error)}`);
    },
  });

  const tagCorrectionMutation = useMutation({
    mutationFn: async (draft: TagCorrectionDraft) => {
      if (!workspace || !id || !selectedTag) {
        throw new Error("标签或接待数据尚未加载");
      }
      if (!activeSchemaVersion || !selectedTagDefinition) {
        // 旧版（非当前已发布 Schema）标签没有治理复核批次可挂靠，但后端
        // PATCH 自带 legacy 更正分支：同样乐观锁校验并写入审计，直接走它。
        return correctReceptionDialogueTag(id, selectedTag.id, {
          expected_reception_version: workspace.reception.version,
          expected_group_version: selectedTag.group_version,
          label_value: draft.labelValue,
          reason: draft.reason,
          evidence_ref_ids: draft.evidenceRefIds,
        });
      }
      const receptionId = positiveNumericId(workspace.reception.id);
      const dialogueUnitId = positiveNumericId(selectedTag.dialogue_unit_id);
      const proposedFactId = canonicalFactId(selectedTag.model_run_id);
      if (receptionId === null || dialogueUnitId === null) {
        throw new Error("接待或对话单元缺少可追溯的持久化 ID");
      }
      const evidenceRefs = selectedTag.evidence_refs
        .filter((evidence) => draft.evidenceRefIds.includes(evidence.ref_id))
        .map((evidence) => ({
          ref_id: evidence.ref_id,
          kind: evidence.kind,
          recording_id: evidence.recording_id,
          segment_id: evidence.segment_id ?? undefined,
          start_sec:
            (evidence.timeline_start_ms ??
              evidence.start_ms ??
              evidence.source_start_ms ??
              0) / 1_000,
          end_sec:
            (evidence.timeline_end_ms ??
              evidence.end_ms ??
              evidence.source_end_ms ??
              0) / 1_000,
          text_excerpt: evidence.text_excerpt ?? undefined,
        }));
      const batch = await createTagReviewBatch({
        reason: selectedTagDefinition.critical ? "critical" : "random",
        subjects: [
          {
            subject_type: "dialogue_unit",
            subject_id: dialogueUnitId,
            reception_id: receptionId,
            tag_key: selectedTag.label_key,
            proposed_value: selectedTag.label_value,
            proposed_fact_id: proposedFactId ?? undefined,
            schema_version_id: activeSchemaVersion.version.id,
            confidence: selectedTag.confidence ?? undefined,
            evidence_refs: evidenceRefs,
            priority: selectedTagDefinition.critical ? 100 : 50,
          },
        ],
      });
      const task = batch.items[0];
      if (!task) throw new Error("人工复核任务未创建");
      return decideTagReview(task.id, {
        action: "correct",
        corrected_value: draft.labelValue,
        reason_code: "manual_workspace_correction",
        note: draft.reason,
        evidence_refs: evidenceRefs,
      });
    },
    onSuccess: async () => {
      setTagCorrectionError(null);
      setSelectedTagId(null);
      setLineageFactId(null);
      setOperationStatus(
        "标签人工更正已写入治理事实，时间轴、证据、图谱与洞察正在同步。",
      );
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["reception-workspace", id],
        }),
        queryClient.invalidateQueries({
          queryKey: ["reception-tag-insights"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["reception-state-insights"],
        }),
        // The correction appends provenance events carrying the reason the
        // reviewer just typed; an open audit-chain drawer must show them.
        queryClient.invalidateQueries({
          queryKey: ["reception-provenance"],
        }),
        queryClient.invalidateQueries({ queryKey: ["graph"] }),
      ]);
    },
    onError: (error) => {
      const message = tagCorrectionErrorMessage(error);
      setTagCorrectionError(message);
      setOperationStatus(message);
    },
  });

  const audioOperationIsActive = Boolean(
    activeAudioOperationId !== null &&
      !["succeeded", "failed", "cancelled"].includes(
        audioOperationQuery.data?.status ?? "queued",
      ),
  );
  const geometryIsBusy =
    activeGeometryMutation !== null || audioOperationIsActive;

  const moveSource = useCallback(
    (key: string, delta: -1 | 1) => {
      const currentOrder =
        sourceOrder.length > 0
          ? sourceOrder
          : orderedRecordings.map(mappingKey);
      const index = currentOrder.indexOf(key);
      const nextIndex = index + delta;
      if (index < 0 || nextIndex < 0 || nextIndex >= currentOrder.length) {
        return;
      }
      const next = [...currentOrder];
      [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
      setSourceOrder(next);
      setAudioPlan(null);
    },
    [orderedRecordings, sourceOrder],
  );

  const moveSourceBefore = useCallback(
    (sourceKey: string, targetKey: string) => {
      if (sourceKey === targetKey) return;
      const currentOrder =
        sourceOrder.length > 0
          ? sourceOrder
          : orderedRecordings.map(mappingKey);
      const withoutSource = currentOrder.filter((key) => key !== sourceKey);
      const targetIndex = withoutSource.indexOf(targetKey);
      if (targetIndex < 0) return;
      withoutSource.splice(targetIndex, 0, sourceKey);
      setSourceOrder(withoutSource);
      setAudioPlan(null);
    },
    [orderedRecordings, sourceOrder],
  );

  const availableDialogueUnits = useMemo(() => {
    if (!workspace) return [];
    const candidates = [
      workspace.neighbors?.previous_dialogue_unit ?? null,
      ...workspace.dialogue_units,
      workspace.neighbors?.next_dialogue_unit ?? null,
    ].filter(
      (unit): unit is ReceptionDialogueUnit => unit !== null,
    );
    const byId = new Map(
      candidates.map((unit) => [String(unit.id), unit]),
    );
    return [...byId.values()].sort(
      (left, right) => left.unit_index - right.unit_index,
    );
  }, [workspace]);

  const selectTagForEditing = useCallback(
    (tag: ReceptionTagAssignment) => {
      setSelectedTagId(tag.id);
      setTagCorrectionError(null);
      setSelectedUnitIds(new Set([String(tag.dialogue_unit_id)]));
      const firstEvidence = tag.evidence_refs[0];
      if (firstEvidence) seekEvidence(firstEvidence);
    },
    [seekEvidence],
  );

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
    const selectedUnits = availableDialogueUnits.filter((unit) =>
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
    const selected = availableDialogueUnits.filter((unit) =>
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
    if (!canRunSegmentation || geometryIsBusy) return;
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
    setSelectedTagId(null);
    setTagCorrectionError(null);
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
          {getErrorMessage(workspaceQuery.error)}
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
          {canRunSegmentation &&
            (automationQuery.data?.status === "failed" ||
              automationQuery.isError) && (
            <button
              type="button"
              className="ag-header-action"
              disabled={geometryIsBusy || automationMutation.isPending}
              onClick={() => automationMutation.mutate()}
            >
              {automationMutation.isPending
                ? "正在提交兼容重跑…"
                : automationQuery.data?.status === "failed"
                  ? "手工从检查点重试（兼容）"
                  : "手工启动旧自动化（兼容）"}
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
            {operationRecordings.map((recording, index) => {
              const key = recordingKey(recording);
              const sourceMappingKey = mappingKey(recording);
              const active = activeSourceId === key;
              return (
                <li
                  key={sourceMappingKey}
                  className={active ? "is-active" : undefined}
                  draggable={
                    canManageAudio &&
                    mergeMode !== "physical" &&
                    !geometryIsBusy
                  }
                  onDragStart={() => {
                    draggedMappingRef.current = sourceMappingKey;
                  }}
                  onDragEnd={() => {
                    draggedMappingRef.current = null;
                  }}
                  onDragOver={(event) => {
                    if (
                      canManageAudio &&
                      mergeMode !== "physical" &&
                      !geometryIsBusy
                    ) {
                      event.preventDefault();
                    }
                  }}
                  onDrop={(event) => {
                    event.preventDefault();
                    const dragged =
                      mergeMode === "physical"
                        ? null
                        : draggedMappingRef.current;
                    if (dragged) {
                      moveSourceBefore(dragged, sourceMappingKey);
                    }
                    draggedMappingRef.current = null;
                  }}
                >
                  <span className="ag-recording-list__sequence">
                    <span>{index + 1}</span>
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
                    <em>
                      映射 #{sourceMappingKey} · 源 #{String(recordingId(recording))}
                    </em>
                  </button>
                  {canManageAudio && (
                    <div
                      className="ag-recording-list__order"
                      aria-label={`${recording.name}顺序与空档`}
                    >
                      <button
                        type="button"
                        aria-label={`将${recording.name}上移`}
                        disabled={
                          index === 0 ||
                          mergeMode === "physical" ||
                          geometryIsBusy
                        }
                        onClick={() => moveSource(sourceMappingKey, -1)}
                      >
                        ↑
                      </button>
                      <button
                        type="button"
                        aria-label={`将${recording.name}下移`}
                        disabled={
                          index === operationRecordings.length - 1 ||
                          mergeMode === "physical" ||
                          geometryIsBusy
                        }
                        onClick={() => moveSource(sourceMappingKey, 1)}
                      >
                        ↓
                      </button>
                      <label>
                        前置空档
                        <input
                          type="number"
                          min={0}
                          step={100}
                          value={
                            index === 0
                              ? 0
                              : mergeMode === "physical"
                                ? Math.max(
                                    Math.round(
                                      recording.gap_before_sec * 1_000,
                                    ),
                                    0,
                                  )
                                : sourceGapMs[sourceMappingKey] ?? 0
                          }
                          disabled={
                            index === 0 ||
                            mergeMode === "physical" ||
                            geometryIsBusy ||
                            !supportsAudioPlans
                          }
                          aria-label={`${recording.name}前静音空档（毫秒）`}
                          onChange={(event) => {
                            const gap = Math.max(
                              Number(event.target.value) || 0,
                              0,
                            );
                            setSourceGapMs((current) => ({
                              ...current,
                              [sourceMappingKey]: Math.round(gap),
                            }));
                            setAudioPlan(null);
                          }}
                        />
                        ms
                      </label>
                    </div>
                  )}
                </li>
              );
            })}
          </ol>

          {canManageAudio ? (
            <div className="ag-merge-controls">
              <div className="ag-merge-controls__row">
                <label>
                  录音合并模式
                  <select
                    aria-label="录音合并模式"
                    value={mergeMode}
                    disabled={geometryIsBusy}
                    onChange={(event) => {
                      setMergeMode(
                        event.target.value as ReceptionMergeMode,
                      );
                      setAudioPlan(null);
                    }}
                  >
                    <option value="logical">仅逻辑合并</option>
                    <option value="physical">仅生成物理音频</option>
                    <option value="both">逻辑 + 物理合并</option>
                  </select>
                </label>
                {supportsAudioPlans && supportsAudioOperations ? (
                  <button
                    type="button"
                    disabled={
                      operationRecordings.length < 1 ||
                      geometryIsBusy ||
                      audioPlanMutation.isPending
                    }
                    onClick={() => audioPlanMutation.mutate()}
                  >
                    {audioPlanMutation.isPending
                      ? "正在验证计划…"
                      : mergeMode === "physical"
                        ? "验证当前时间线物理产物"
                        : "生成合并预览"}
                  </button>
                ) : (
                  <button
                    type="button"
                    disabled={
                      operationRecordings.length < 2 ||
                      geometryIsBusy ||
                      recordingMergeMutation.isPending
                    }
                    onClick={() => recordingMergeMutation.mutate()}
                  >
                    按当前顺序重新合并全部 {operationRecordings.length} 段
                  </button>
                )}
              </div>
              <strong className="ag-merge-controls__duration">
                预计时间线 {formatClock(draftTimelineDurationSec)}
              </strong>
              <small
                className="ag-merge-controls__hint"
                title="顺序编辑使用映射 ID，播放和证据仍使用不可变源录音 ID。"
              >
                {mergeMode === "physical"
                  ? "仅按当前已提交时间线重建物理产物；来源顺序与空档在此模式下不可修改。"
                  : supportsAudioPlans
                    ? "提交前由服务端重新验证切片、空档和物理兼容性。"
                  : "兼容服务仅支持来源顺序；空档只读并沿用当前映射。"}
              </small>
              {audioPlan && (
                <div className="ag-audio-plan" role="status">
                  <strong>
                    计划总时长{" "}
                    {formatClock(audioPlan.total_duration_ms / 1_000)}
                  </strong>
                  <span>
                    时间线 revision {audioPlan.timeline_revision} ·{" "}
                    {audioPlan.physical_eligible
                      ? "可生成物理音频"
                      : "仅支持逻辑时间线"}
                  </span>
                  {audioPlan.warnings.length > 0 && (
                    <ul>
                      {audioPlan.warnings.map((warning) => (
                        <li key={warning}>{warning}</li>
                      ))}
                    </ul>
                  )}
                  <button
                    type="button"
                    disabled={
                      geometryIsBusy ||
                      audioOperationMutation.isPending ||
                      ((mergeMode === "physical" || mergeMode === "both") &&
                        !audioPlan.physical_eligible)
                    }
                    onClick={() => audioOperationMutation.mutate()}
                  >
                    提交音频任务
                  </button>
                </div>
              )}
              {activeAudioOperationId !== null && audioOperationQuery.data && (
                <div className="ag-audio-operation" role="status">
                  <span>
                    任务 #{activeAudioOperationId} ·{" "}
                    {audioOperationQuery.data.status} ·{" "}
                    {Math.round(audioOperationQuery.data.progress * 100)}%
                  </span>
                  {canCancelAudioOperation && audioOperationIsActive && (
                    <button
                      type="button"
                      disabled={cancelAudioOperationMutation.isPending}
                      onClick={() => cancelAudioOperationMutation.mutate()}
                    >
                      取消未提交任务
                    </button>
                  )}
                </div>
              )}
            </div>
          ) : (
            <p className="ag-merge-controls__readonly" role="note">
              当前账号仅可调听与查看证据
            </p>
          )}
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
                  {silenceGap
                    ? `静音空档 ${formatClock(silenceGap.timelineStartSec)}–${formatClock(silenceGap.timelineEndSec)}`
                    : activeRecording
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
              onTimeUpdate={() => handleTimeUpdate(false)}
              onEnded={() => handleTimeUpdate(true)}
              onError={handleAudioError}
              aria-label="接待录音播放器"
            >
              您的浏览器不支持音频播放。
            </audio>
          </div>

          {canStreamAudio && (
            <LiveAudioCapturePanel
              recordings={operationRecordings}
              disabled={geometryIsBusy}
              onCommitted={() => {
                void queryClient.invalidateQueries({
                  queryKey: ["reception-workspace", id],
                });
              }}
            />
          )}

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
            selectedTagId={selectedTagId}
            onSeek={seekTimeline}
            onToggleUnit={toggleUnit}
            onSelectTag={selectTagForEditing}
            speakerByLabel={speakerByLabel}
            speakerResolutionFailed={speakerResolutionFailed}
          />

          {selectedTag && canEditTags && (
            <TagAssignmentEditor
              tag={selectedTag}
              definition={selectedTagDefinition}
              isSaving={tagCorrectionMutation.isPending}
              error={tagCorrectionError}
              onCancel={() => {
                if (tagCorrectionMutation.isPending) return;
                setSelectedTagId(null);
                setTagCorrectionError(null);
                setLineageFactId(null);
              }}
              onSeekEvidence={seekEvidence}
              onViewLineage={setLineageFactId}
              onSubmit={(draft) => tagCorrectionMutation.mutate(draft)}
            />
          )}
          {lineageFactId !== null && (
            <TagFactLineageDrawer
              factId={lineageFactId}
              data={lineageQuery.data}
              pending={lineageQuery.isPending}
              error={lineageQuery.error}
              onRetry={() => void lineageQuery.refetch()}
              onClose={() => setLineageFactId(null)}
            />
          )}
          {auditChainTarget !== null && (
            <ReceptionAuditChainDrawer
              target={auditChainTarget}
              data={auditChainQuery.data}
              pending={auditChainQuery.isPending}
              error={auditChainQuery.error}
              onRetry={() => void auditChainQuery.refetch()}
              onClose={() => setAuditChainTarget(null)}
            />
          )}

          {(canRunSegmentation || canEditDialogue) && (
            <div className="ag-dialogue-toolbar">
              <div>
                <strong>对话编辑</strong>
                <span>已选 {selectedUnitIds.size} 个语义单元</span>
              </div>
              {canEditDialogue && (
                <label>
                  <span>编辑原因</span>
                  <input
                    value={editReason}
                    maxLength={500}
                    placeholder="必填，将写入溯源审计"
                    aria-label="对话编辑原因"
                    disabled={geometryIsBusy}
                    onChange={(event) => setEditReason(event.target.value)}
                  />
                </label>
              )}
              {(workspace.neighbors?.previous_dialogue_unit ||
                workspace.neighbors?.next_dialogue_unit) &&
                canEditDialogue && (
                  <div
                    className="ag-dialogue-neighbors"
                    aria-label="跨窗口相邻对话单元"
                  >
                    {[
                      workspace.neighbors?.previous_dialogue_unit,
                      workspace.neighbors?.next_dialogue_unit,
                    ]
                      .filter(
                        (unit): unit is ReceptionDialogueUnit =>
                          unit !== null && unit !== undefined,
                      )
                      .map((unit) => (
                        <button
                          type="button"
                          key={String(unit.id)}
                          aria-pressed={selectedUnitIds.has(String(unit.id))}
                          onClick={() => toggleUnit(unit)}
                        >
                          选择相邻单元 #{unit.unit_index + 1}
                        </button>
                      ))}
                  </div>
                )}
              <div className="ag-dialogue-toolbar__actions">
                {canRunSegmentation && (
                  <button
                    type="button"
                    disabled={
                      geometryIsBusy ||
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
                )}
                {canEditDialogue && (
                  <>
                    <button
                      type="button"
                      disabled={
                        geometryIsBusy ||
                        splitMutation.isPending ||
                        !editReason.trim()
                      }
                      onClick={splitAtCurrentTime}
                    >
                      在当前播放点切分
                    </button>
                    <button
                      type="button"
                      disabled={
                        geometryIsBusy ||
                        selectedUnitIds.size !== 2 ||
                        unitMergeMutation.isPending ||
                        !editReason.trim()
                      }
                      onClick={mergeSelectedUnits}
                    >
                      合并相邻对话
                    </button>
                  </>
                )}
              </div>
            </div>
          )}

          {canEditTags && (
            <section
              className="ag-tag-derive-panel"
              aria-labelledby="derive-target-tags-title"
            >
            <div className="ag-tag-derive-panel__heading">
              <div>
                <strong id="derive-target-tags-title">目标对话标签</strong>
                <span>
                  主链读取已发布 Schema 定义，使用达标抽取版本创建持久化后台任务。
                </span>
              </div>
              {activeSchemaVersion ? (
                <button
                  type="button"
                  disabled={
                    workspace.window.total_dialogue_units === 0 ||
                    targetLabels.size === 0 ||
                    tagJobMutation.isPending
                  }
                  onClick={() => tagJobMutation.mutate()}
                >
                  {tagJobMutation.isPending
                    ? "正在创建任务…"
                    : "创建标签重算任务"}
                </button>
              ) : legacyFallback ? (
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
                  {deriveTagsMutation.isPending
                    ? "正在派生…"
                    : "使用旧规则派生（兼容）"}
                </button>
              ) : null}
            </div>
            {schemasQuery.isPending && (
              <p className="ag-tag-governance-state" role="status">
                正在读取已发布标签体系…
              </p>
            )}
            {schemasQuery.isError && (
              <p className="ag-tag-governance-state is-error" role="alert">
                标签治理资产加载失败。为避免写入未受治理的标签，本次不自动降级到旧规则。
              </p>
            )}
            {activeSchemaVersion && (
              <>
                <div className="ag-tag-derive-panel__controls is-canonical">
                  <label>
                    已发布标签体系
                    <select
                      aria-label="已发布标签体系版本"
                      value={activeSchemaVersion.version.id}
                      onChange={(event) =>
                        setSchemaVersionId(Number(event.target.value))
                      }
                    >
                      {publishedSchemaVersions.map((item) => (
                        <option
                          key={item.version.id}
                          value={item.version.id}
                        >
                          {item.schemaName} · {item.version.version}
                        </option>
                      ))}
                    </select>
                  </label>
                  <p
                    className="ag-tag-derive-panel__hint"
                    role="note"
                  >
                    抽取版本由服务端按当前租户与所选 Schema 原子绑定。
                  </p>
                </div>
                <fieldset className="ag-tag-derive-panel__schema">
                  <legend>Schema 标签定义</legend>
                  {activeDefinitions.map((definition: TagDefinition) => (
                    <label
                      key={definition.key}
                      className="ag-canonical-definition"
                      title={definition.key}
                    >
                      <input
                        type="checkbox"
                        checked={targetLabels.has(definition.key)}
                        aria-label={`派生${definition.name}标签`}
                        onChange={() =>
                          setTargetLabels((current) => {
                            const next = new Set(current);
                            if (next.has(definition.key)) {
                              next.delete(definition.key);
                            } else {
                              next.add(definition.key);
                            }
                            return next;
                          })
                        }
                      />
                      <span className="ag-canonical-definition__label">
                        {definition.name}
                        {definition.critical && <b>关键</b>}
                        {definition.evidence_required && (
                          <span className="ag-canonical-definition__evidence">
                            需证据
                          </span>
                        )}
                      </span>
                    </label>
                  ))}
                </fieldset>
              </>
            )}
            {legacyFallback && (
              <>
                <p className="ag-legacy-tag-warning" role="note">
                  <strong>旧规则兼容模式</strong>
                  当前租户尚无已发布 Schema。以下固定五维仅用于旧数据兼容，不作为新标签主链。
                </p>
                <div className="ag-tag-derive-panel__controls is-legacy">
                  <label>
                    旧标签组
                    <input
                      aria-label="目标标签组"
                      value={tagGroupKey}
                      maxLength={64}
                      pattern="[\w.-]+"
                      onChange={(event) => setTagGroupKey(event.target.value)}
                    />
                  </label>
                  <label>
                    旧规则版本
                    <input
                      aria-label="目标标签规则版本"
                      value={tagGroupVersion}
                      maxLength={64}
                      pattern="[\w.-]+"
                      onChange={(event) => setTagGroupVersion(event.target.value)}
                    />
                  </label>
                  <fieldset>
                    <legend>旧五维派生</legend>
                    {LEGACY_TARGET_LABELS.map((item) => (
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
              </>
            )}
            {tagJob && (
              <div
                className="ag-tag-derive-result"
                role="status"
                aria-live="polite"
              >
                <div>
                  <strong>{tagJobHeadline(tagJob.status)}</strong>
                  <span>Schema #{schemaVersionId}</span>
                  {tagJob.total_items > 0 && (
                    <span>
                      {tagJob.completed_items} / {tagJob.total_items}
                    </span>
                  )}
                  {tagJob.last_error_message && (
                    <span>{tagJob.last_error_message}</span>
                  )}
                  <span>
                    {tagJob.tagger_version_id === null ||
                    tagJob.tagger_version_id === undefined
                      ? "服务端路由待绑定"
                      : `服务端已绑定 Tagger #${tagJob.tagger_version_id}`}
                  </span>
                </div>
                <Link to={`/tag-runs/${tagJob.id}`}>
                  查看标签任务 #{tagJob.id}
                </Link>
              </div>
            )}
            {legacyFallback && deriveTagsMutation.data && (
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
          )}

          <div
            ref={transcriptRef}
            className="ag-transcript"
            aria-label="对话转写"
          >
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
            selectedTagId={selectedTagId}
            canEdit={canEditTags}
            onEditTag={selectTagForEditing}
            onViewAuditChain={setAuditChainTarget}
          />
        </aside>
      </main>

      {/* 调听台很长:转写滚出视口后,这条给出「现在说到哪句」与跳转,
          时间与播放态直接来自上面的 <audio>。 */}
      <FloatingSubtitle
        lines={workspace.transcript_items.map((item) => ({
          atSec: item.start_sec,
          speaker: item.speaker_label,
          role:
            item.speaker_role === "agent" || item.speaker_role === "customer"
              ? item.speaker_role
              : "unknown",
          text: item.text,
        }))}
        currentSec={playbackSec}
        durationSec={playbackDuration}
        playing={isPlaying}
        anchorRef={transcriptRef}
        onSeek={(second) => {
          const audio = audioRef.current;
          if (audio) audio.currentTime = second;
        }}
        onTogglePlay={() => {
          const audio = audioRef.current;
          if (!audio) return;
          if (audio.paused) void audio.play().catch(() => undefined);
          else audio.pause();
        }}
        onBackToTranscript={() =>
          transcriptRef.current?.scrollIntoView({
            behavior: "smooth",
            block: "center",
          })
        }
      />
    </div>
  );
}
