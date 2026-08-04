import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  IconCheckCircle,
  IconClockCircle,
  IconMinusCircle,
  IconSound,
  IconSync,
  IconUser,
} from "@arco-design/web-react/icon";
import {
  type FormEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link } from "react-router-dom";
import {
  adjudicateTagReview,
  claimTagReview,
  decideTagReview,
  listTagSchemas,
  listTagReviews,
  releaseTagReview,
} from "@/api/services";
import { useAuthStore } from "@/stores/auth";
import { getErrorMessage } from "@/utils/errors";
import type {
  DecideTagReviewRequest,
  TagFailureStage,
  TagReviewDecisionAction,
  TagReviewEvidenceRef,
  TagReviewTask,
} from "@/types/api";
import "../TagGovernance/tagGovernance.css";

const TERMINAL_REVIEW_STATUSES = new Set(["resolved", "skipped"]);

const REVIEW_PURPOSE_LABELS: Record<string, string> = {
  routine: "日常复核",
  active_learning: "主动学习",
  representative_audit: "随机质量审计",
  critical: "关键标签复核",
  gold: "金标构建",
  release_holdout: "发布集复核",
  adjudication: "分歧仲裁",
};

const FAILURE_STAGE_OPTIONS: Array<{
  value: TagFailureStage;
  label: string;
}> = [
  { value: "tag_reasoning", label: "标签推理 / Prompt" },
  { value: "evidence", label: "证据定位" },
  { value: "schema", label: "标签定义 / 值域" },
  { value: "fusion", label: "规则与模型融合" },
  { value: "asr", label: "ASR 转写" },
  { value: "vad", label: "VAD 语音检测" },
  { value: "speaker", label: "说话人识别" },
  { value: "boundary", label: "对话边界 / 切分" },
  { value: "insufficient_audio", label: "音质或信息不足" },
];

const REVIEW_REASON_OPTIONS = [
  { value: "model_misread", label: "模型误判" },
  { value: "evidence_confirmed", label: "证据确认" },
  { value: "taxonomy_mismatch", label: "标签定义不匹配" },
  { value: "insufficient_evidence", label: "证据不足" },
  { value: "transcript_error", label: "转写错误" },
  { value: "speaker_error", label: "说话人错误" },
  { value: "boundary_error", label: "边界错误" },
  { value: "poor_audio", label: "音质问题" },
] as const;

function compactPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return `${new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 1,
  }).format(value * 100)}%`;
}

function formatClock(seconds: number): string {
  const safeValue = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(safeValue / 60)).padStart(2, "0")}:${String(
    safeValue % 60,
  ).padStart(2, "0")}`;
}

function reviewStatus(status: TagReviewTask["status"]): string {
  return (
    {
      pending: "待领取",
      claimed: "复核中",
      resolved: "已完成",
      skipped: "已跳过",
    } satisfies Record<TagReviewTask["status"], string>
  )[status];
}

function reviewPurpose(task: TagReviewTask): string {
  const purpose =
    task.queue_purpose ??
    ({
      active_learning: "active_learning",
      random: "representative_audit",
      audit: "representative_audit",
      critical: "critical",
      gold: "gold",
      adjudication: "adjudication",
    } as Record<string, string>)[task.reason] ??
    "routine";
  return REVIEW_PURPOSE_LABELS[purpose] ?? task.reason;
}

/**
 * Whether anything in the queue can still change on its own.
 *
 * Shared by the poll interval and the toolbar copy so the page never claims a
 * refresh cadence it has already stopped running.
 */
function hasOpenReviewWork(items: TagReviewTask[] | undefined): boolean {
  return Boolean(
    items?.some((item) => !TERMINAL_REVIEW_STATUSES.has(item.status)),
  );
}

function isBlindReview(task: TagReviewTask): boolean {
  return Boolean(
    task.blind_mode && !TERMINAL_REVIEW_STATUSES.has(task.status),
  );
}

function isAdjudicationReview(task: TagReviewTask): boolean {
  return (
    task.reason === "adjudication" ||
    task.queue_purpose === "adjudication"
  );
}

function reviewTruthTier(task: TagReviewTask): "t1" | "t2" | "t3" {
  const explicitTier = String(task.truth_tier ?? "").toLowerCase();
  if (
    explicitTier === "t1" ||
    explicitTier === "t2" ||
    explicitTier === "t3"
  ) {
    return explicitTier;
  }
  if (isAdjudicationReview(task)) {
    return "t3";
  }
  if (
    ["random", "audit", "critical", "gold"].includes(task.reason) ||
    ["representative_audit", "critical", "gold", "release_holdout"].includes(
      task.queue_purpose ?? "",
    )
  ) {
    return "t2";
  }
  return "t1";
}

function ReviewStatusIcon({ status }: { status: TagReviewTask["status"] }) {
  const Icon =
    status === "pending"
      ? IconClockCircle
      : status === "claimed"
        ? IconUser
        : status === "resolved"
          ? IconCheckCircle
          : IconMinusCircle;
  return <Icon className={`is-${status}`} aria-hidden="true" />;
}

function displayTagValue(value: unknown, fallback: string): string {
  if (value === null || value === undefined) return fallback;
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function reviewSubjectLabel(task: TagReviewTask): string {
  if (task.subject_id === null) return "领取后揭示主体";
  return `${task.subject_type} #${task.subject_id}`;
}

function evidenceHref(
  receptionId: TagReviewTask["reception_id"],
  evidence: TagReviewEvidenceRef,
): string | null {
  if (receptionId === null || evidence.recording_id === undefined) return null;
  return `/receptions/${encodeURIComponent(
    String(receptionId),
  )}/workspace?recording=${encodeURIComponent(
    String(evidence.recording_id),
  )}&at=${Math.round(
    Math.max(0, evidence.start_sec ?? 0) * 1_000,
  )}`;
}

function ReviewQueue({
  items,
  selectedId,
  onSelect,
}: {
  items: TagReviewTask[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  return (
    <aside className="ag-review-queue" aria-label="复核任务队列">
      <header>
        <div>
          <strong>任务队列</strong>
          <span>{items.length} 条当前结果</span>
        </div>
        <IconSync className="ag-review-queue__sync" aria-hidden="true" />
      </header>
      <div>
        {items.map((task) => (
          <button
            type="button"
            key={task.id}
            className={selectedId === task.id ? "is-active" : undefined}
            aria-pressed={selectedId === task.id}
            onClick={() => onSelect(task.id)}
          >
            <span>
              <ReviewStatusIcon status={task.status} />
              <strong>{task.tag_key}</strong>
              <small>#{task.id}</small>
            </span>
            <span>
              <b>
                {isBlindReview(task)
                  ? "建议已隐藏"
                  : displayTagValue(task.proposed_value, "待补值")}
              </b>
              <small>{reviewStatus(task.status)}</small>
            </span>
            <span>
              {reviewPurpose(task)}
              <small>{reviewSubjectLabel(task)}</small>
            </span>
          </button>
        ))}
      </div>
    </aside>
  );
}

function EvidencePanel({ task }: { task: TagReviewTask }) {
  const evidence = task.evidence_refs ?? [];
  return (
    <section className="ag-review-evidence" aria-labelledby="review-evidence-title">
      <header>
        <div>
          <span className="ag-card-kicker">EVIDENCE</span>
          <h2 id="review-evidence-title">证据与原始语境</h2>
        </div>
        <span>{evidence.length} 条</span>
      </header>
      {evidence.length === 0 ? (
        <div className="ag-review-evidence__empty" role="status">
          <strong>当前任务未携带证据片段</strong>
          <span>可回到主体原始对话定位后，再提交人工判断。</span>
        </div>
      ) : (
        <ol>
          {evidence.map((item, index) => {
            const href = evidenceHref(task.reception_id, item);
            const startSeconds = Math.max(item.start_sec ?? 0, 0);
            const audioUrl =
              typeof item.audio_url === "string" && item.audio_url.trim()
                ? item.audio_url
                : null;
            return (
              <li key={`${String(item.segment_id ?? "evidence")}-${index}`}>
                <div className="ag-evidence-audio-context">
                  <IconSound aria-hidden="true" />
                  <span>
                    音频窗 {formatClock(startSeconds)}–
                    {formatClock(Math.max(item.end_sec ?? startSeconds, startSeconds))}
                  </span>
                </div>
                {audioUrl && (
                  <audio
                    controls
                    preload="metadata"
                    src={audioUrl}
                    aria-label={`证据音频 ${formatClock(startSeconds)}`}
                  />
                )}
                <blockquote>
                  {item.text_excerpt || "该证据仅包含音频时间窗。"}
                </blockquote>
                <footer>
                  <span>
                    录音 {String(item.recording_id ?? "—")} · 片段{" "}
                    {String(item.segment_id ?? "—")}
                  </span>
                  {href && (
                    <Link to={href}>
                      跳转调听 {formatClock(startSeconds)}
                    </Link>
                  )}
                  {!href && (
                    <span className="ag-review-evidence__unavailable">
                      缺少接待上下文，暂不能跳转调听
                    </span>
                  )}
                </footer>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function DecisionPanel({
  task,
  onDecision,
  pending,
  canSubmit,
  readOnlyMessage,
  valueDomainNotice,
  onReloadValueDomain,
}: {
  task: TagReviewTask;
  onDecision: (body: DecideTagReviewRequest) => void;
  pending: boolean;
  canSubmit: boolean;
  readOnlyMessage: string | null;
  valueDomainNotice: string | null;
  onReloadValueDomain: () => void;
}) {
  const blind = isBlindReview(task);
  const adjudication = isAdjudicationReview(task);
  const [action, setAction] = useState<TagReviewDecisionAction | null>(null);
  const [correctedValue, setCorrectedValue] = useState("");
  const [rejectedTruthState, setRejectedTruthState] = useState<
    "absent" | "not_applicable" | ""
  >("");
  const [failureStage, setFailureStage] = useState<TagFailureStage | "">("");
  const [reasonCodes, setReasonCodes] = useState<string[]>([]);
  const [reviewerConfidence, setReviewerConfidence] = useState("");
  const [evidenceRefs, setEvidenceRefs] = useState<TagReviewEvidenceRef[]>([]);
  const [excludedEvidence, setExcludedEvidence] = useState<Set<number>>(
    () => new Set(),
  );
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const reviewStartedAtRef = useRef(Date.now());
  const evidenceSignature = JSON.stringify(task.evidence_refs ?? []);

  useEffect(() => {
    reviewStartedAtRef.current = Date.now();
    setAction(null);
    setCorrectedValue("");
    setRejectedTruthState("");
    setFailureStage("");
    setReasonCodes([]);
    setReviewerConfidence("");
    setEvidenceRefs(
      (JSON.parse(evidenceSignature) as TagReviewEvidenceRef[]).map(
        (item) => ({ ...item }),
      ),
    );
    setExcludedEvidence(new Set());
    setNote("");
    setError(null);
  }, [adjudication, evidenceSignature, task.id, task.status]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit) {
      setError(readOnlyMessage ?? "当前任务不可提交。");
      return;
    }
    if (!action) {
      setError("请选择复核结论，系统不会默认接受模型建议。");
      return;
    }
    if (
      adjudication &&
      action !== "correct" &&
      action !== "reject"
    ) {
      setError("仲裁必须给出标签存在、不存在或不适用的最终结论。");
      return;
    }
    if (action === "correct" && valueDomainNotice) {
      setError(valueDomainNotice);
      return;
    }
    if (action === "correct" && !correctedValue.trim()) {
      setError("纠正标签时必须从当前 Schema 值域选择新的标签值。");
      return;
    }
    if (action === "reject" && !rejectedTruthState) {
      setError("请明确标签是不存在，还是不适用于当前主体。");
      return;
    }
    if (action !== "accept" && !failureStage) {
      setError("请选择主要错误层，便于将反馈送入正确的优化队列。");
      return;
    }
    const confidence = Number(reviewerConfidence);
    if (!Number.isFinite(confidence) || confidence <= 0 || confidence > 1) {
      setError("请选择复核置信度。");
      return;
    }
    const derivedReasons =
      reasonCodes.length > 0
        ? reasonCodes
        : action === "accept"
          ? ["evidence_confirmed"]
          : action === "escalate"
            ? ["needs_adjudication"]
            : ["insufficient_evidence"];
    const truthState =
      action === "accept" || action === "correct"
        ? "present"
        : action === "reject"
          ? rejectedTruthState || "uncertain"
          : "uncertain";
    const allowedValue = task.allowed_values?.find(
      (value) => displayTagValue(value, "") === correctedValue,
    );
    setError(null);
    onDecision({
      action,
      truth_state: truthState,
      ...(action === "correct"
        ? { corrected_value: allowedValue ?? correctedValue.trim() }
        : {}),
      ...(failureStage ? { primary_failure_stage: failureStage } : {}),
      reason_code: derivedReasons[0],
      reason_codes: derivedReasons,
      reviewer_confidence: confidence,
      review_duration_ms: Math.min(
        86_400_000,
        Math.max(0, Date.now() - reviewStartedAtRef.current),
      ),
      ...(note.trim() ? { note: note.trim() } : {}),
      evidence_refs: evidenceRefs.filter(
        (_item, index) => !excludedEvidence.has(index),
      ),
    });
  };

  const toggleReason = (reasonCode: string) => {
    setReasonCodes((current) =>
      current.includes(reasonCode)
        ? current.filter((item) => item !== reasonCode)
        : [...current, reasonCode],
    );
  };

  const updateEvidence = (
    index: number,
    patch: Partial<TagReviewEvidenceRef>,
  ) => {
    setEvidenceRefs((current) =>
      current.map((item, itemIndex) =>
        itemIndex === index ? { ...item, ...patch } : item,
      ),
    );
  };

  return (
    <section className="ag-review-decision" aria-labelledby="review-decision-title">
      <header>
        <span className="ag-card-kicker">HUMAN DECISION</span>
        <h2 id="review-decision-title">人工复核结论</h2>
        <p>
          提交后追加人工事实，不覆盖历史模型事实；系统会记录本次复核耗时。
        </p>
      </header>
      <form onSubmit={submit}>
        {readOnlyMessage && (
          <p className="ag-review-readonly-notice" role="status">
            {readOnlyMessage}
          </p>
        )}
        <fieldset
          className="ag-review-decision-fields"
          disabled={pending || !canSubmit}
        >
          <legend className="ag-review-decision-fields__legend">
            复核表单
          </legend>
          <fieldset>
          <legend>复核结论</legend>
          <div role="radiogroup" aria-label="复核结论">
            {!blind && !adjudication && (
              <label>
                <input
                  type="radio"
                  name="review-action"
                  value="accept"
                  aria-label="接受建议"
                  checked={action === "accept"}
                  onChange={() => setAction("accept")}
                />
                <span>
                  <strong>接受建议</strong>
                  <small>模型标签与证据一致</small>
                </span>
              </label>
            )}
            <label>
              <input
                type="radio"
                name="review-action"
                value="correct"
                aria-label={blind ? "标注真实标签" : "纠正标签"}
                checked={action === "correct"}
                onChange={() => setAction("correct")}
              />
              <span>
                <strong>{blind ? "标注真实标签" : "纠正标签"}</strong>
                <small>
                  {blind
                    ? "独立判断真实值，不参考模型答案"
                    : "写入新的人工标签值"}
                </small>
              </span>
            </label>
            <label>
              <input
                type="radio"
                name="review-action"
                value="reject"
                aria-label="标签不存在 / 不适用"
                checked={action === "reject"}
                onChange={() => setAction("reject")}
              />
              <span>
                <strong>标签不存在 / 不适用</strong>
                <small>记录显式负例或不适用状态</small>
              </span>
            </label>
            {!adjudication && (
              <>
                <label>
                  <input
                    type="radio"
                    name="review-action"
                    value="uncertain"
                    aria-label="无法确定"
                    checked={action === "uncertain"}
                    onChange={() => setAction("uncertain")}
                  />
                  <span>
                    <strong>无法确定</strong>
                    <small>信息不足，不把未知误当负例</small>
                  </span>
                </label>
                <label>
                  <input
                    type="radio"
                    name="review-action"
                    value="escalate"
                    aria-label="升级仲裁"
                    checked={action === "escalate"}
                    onChange={() => setAction("escalate")}
                  />
                  <span>
                    <strong>升级仲裁</strong>
                    <small>创建独立复核或第三人仲裁</small>
                  </span>
                </label>
              </>
            )}
          </div>
          </fieldset>
        {action === "reject" && (
          <label>
            标签状态
            <select
              aria-label="标签状态"
              value={rejectedTruthState}
              onChange={(event) =>
                setRejectedTruthState(
                  event.target.value as "absent" | "not_applicable" | "",
                )
              }
            >
              <option value="">请选择</option>
              <option value="absent">不存在</option>
              <option value="not_applicable">不适用于当前主体</option>
            </select>
          </label>
        )}
        {action === "correct" &&
          (valueDomainNotice ? (
            // Fail closed: a free-text field here would accept a value the
            // Schema does not allow, and the reviewer would only find out
            // downstream.
            <div className="ag-review-readonly-notice" role="alert">
              <span>{valueDomainNotice}</span>
              <button type="button" onClick={onReloadValueDomain}>
                重新加载值域
              </button>
            </div>
          ) : (
            <label>
              纠正后的标签值
              {task.allowed_values?.length ? (
                <select
                  aria-label="纠正后的标签值"
                  value={correctedValue}
                  onChange={(event) => setCorrectedValue(event.target.value)}
                >
                  <option value="">从 Schema 值域选择</option>
                  {task.allowed_values.map((value) => {
                    const displayValue = displayTagValue(value, "");
                    return (
                      <option value={displayValue} key={displayValue}>
                        {displayValue}
                      </option>
                    );
                  })}
                </select>
              ) : (
                <input
                  aria-label="纠正后的标签值"
                  value={correctedValue}
                  onChange={(event) => setCorrectedValue(event.target.value)}
                />
              )}
            </label>
          ))}
        <label>
          主要错误层
          <select
            aria-label="主要错误层"
            value={failureStage}
            onChange={(event) =>
              setFailureStage(event.target.value as TagFailureStage | "")
            }
          >
            <option value="">请选择</option>
            {FAILURE_STAGE_OPTIONS.map((option) => (
              <option value={option.value} key={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          复核置信度
          <select
            aria-label="复核置信度"
            value={reviewerConfidence}
            onChange={(event) => setReviewerConfidence(event.target.value)}
          >
            <option value="">请选择</option>
            <option value="0.6">较低 · 60%</option>
            <option value="0.8">中等 · 80%</option>
            <option value="0.9">较高 · 90%</option>
            <option value="1">确定 · 100%</option>
          </select>
        </label>
        <fieldset className="ag-review-reasons">
          <legend>原因（可多选）</legend>
          <div>
            {REVIEW_REASON_OPTIONS.map((reason) => (
              <label key={reason.value}>
                <input
                  type="checkbox"
                  checked={reasonCodes.includes(reason.value)}
                  onChange={() => toggleReason(reason.value)}
                />
                {reason.label}
              </label>
            ))}
          </div>
        </fieldset>
        <fieldset className="ag-review-evidence-editor">
          <legend>提交证据</legend>
          {evidenceRefs.map((evidence, index) => (
            <div key={`${String(evidence.segment_id ?? "new")}-${index}`}>
              <label>
                <input
                  type="checkbox"
                  aria-label={`保留证据 ${index + 1}`}
                  checked={!excludedEvidence.has(index)}
                  onChange={() =>
                    setExcludedEvidence((current) => {
                      const next = new Set(current);
                      if (next.has(index)) next.delete(index);
                      else next.add(index);
                      return next;
                    })
                  }
                />
                保留证据 {index + 1}
              </label>
              <label>
                起点（秒）
                <input
                  type="number"
                  min="0"
                  step="0.1"
                  aria-label={`证据 ${index + 1} 起点（秒）`}
                  value={evidence.start_sec ?? 0}
                  onChange={(event) =>
                    updateEvidence(index, {
                      start_sec: Number(event.target.value),
                    })
                  }
                />
              </label>
              <label>
                终点（秒）
                <input
                  type="number"
                  min="0"
                  step="0.1"
                  aria-label={`证据 ${index + 1} 终点（秒）`}
                  value={evidence.end_sec ?? evidence.start_sec ?? 0}
                  onChange={(event) =>
                    updateEvidence(index, {
                      end_sec: Number(event.target.value),
                    })
                  }
                />
              </label>
            </div>
          ))}
          <button
            type="button"
            className="is-secondary"
            onClick={() =>
              setEvidenceRefs((current) => [
                ...current,
                { start_sec: 0, end_sec: 0 },
              ])
            }
          >
            新增证据区间
          </button>
        </fieldset>
        <label>
          复核备注
          <textarea
            aria-label="复核备注"
            rows={3}
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
        </label>
        {error && (
          <p className="ag-inline-feedback is-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" disabled={pending || !canSubmit}>
          {pending
            ? "正在提交…"
            : task.status === "pending"
              ? "请先领取任务"
              : task.status === "claimed" && !canSubmit
                ? "非本人领取，仅可查看"
                : task.status !== "claimed"
                  ? "任务已结束"
              : "提交复核"}
        </button>
        </fieldset>
      </form>
    </section>
  );
}

export default function TagReviewPage() {
  const queryClient = useQueryClient();
  const currentUserId = useAuthStore((state) => state.user?.id ?? null);
  const [statusFilter, setStatusFilter] = useState("active");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [localTask, setLocalTask] = useState<TagReviewTask | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["tag-reviews", statusFilter],
    queryFn: () => listTagReviews({ status: statusFilter }),
    retry: false,
    refetchInterval: (currentQuery) =>
      hasOpenReviewWork(currentQuery.state.data?.items) ? 3_000 : false,
  });
  const schemasQuery = useQuery({
    queryKey: ["tag-governance", "schemas", "review-values"],
    queryFn: listTagSchemas,
    retry: false,
    staleTime: 5 * 60_000,
  });

  const items = useMemo(() => {
    const source = query.data?.items ?? [];
    if (statusFilter !== "active") return source;
    return source.filter(
      (item) => !TERMINAL_REVIEW_STATUSES.has(item.status),
    );
  }, [query.data?.items, statusFilter]);

  useEffect(() => {
    if (selectedId !== null && items.some((item) => item.id === selectedId)) {
      return;
    }
    setSelectedId(items[0]?.id ?? null);
    setLocalTask(null);
  }, [items, selectedId]);

  const selectedTask =
    localTask?.id === selectedId
      ? localTask
      : items.find((item) => item.id === selectedId) ?? null;
  const reviewTask = useMemo(() => {
    if (!selectedTask || selectedTask.allowed_values?.length) {
      return selectedTask;
    }
    const definition = schemasQuery.data?.items
      .flatMap((schema) => schema.versions ?? [])
      .find((version) => version.id === selectedTask.schema_version_id)
      ?.definitions.find(
        (item) =>
          item.key === selectedTask.tag_key &&
          item.subject_types.includes(selectedTask.subject_type),
      );
    if (!definition?.allowed_values.length) return selectedTask;
    return {
      ...selectedTask,
      allowed_values: definition.allowed_values,
    };
  }, [schemasQuery.data?.items, selectedTask]);

  // A corrected value is only submittable when its value domain is known: it
  // either travels with the task or comes from the published schema.  While the
  // schema query has not settled — and above all when it failed — the domain is
  // unknown, so correction is blocked instead of degrading into a free-text
  // field that happily accepts a value outside the schema.  A loaded schema
  // that simply carries no allowed values still means "free-form tag", and that
  // case keeps its text input.
  const valueDomainNotice =
    reviewTask && !reviewTask.allowed_values?.length && !schemasQuery.isSuccess
      ? schemasQuery.isError
        ? `标签值域加载失败（${getErrorMessage(
            schemasQuery.error,
          )}）：为避免写入值域外的标签值，暂不能纠正标签值。`
        : "正在加载标签值域，加载完成后才能纠正标签值。"
      : null;

  const claimMutation = useMutation({
    mutationFn: (id: number) => claimTagReview(id),
    onSuccess: (task) => {
      setLocalTask(task);
      setFeedback(`任务 #${task.id} 已领取`);
      queryClient.invalidateQueries({ queryKey: ["tag-reviews"] });
    },
  });
  const releaseMutation = useMutation({
    mutationFn: (id: number) => releaseTagReview(id),
    onSuccess: async (task) => {
      setLocalTask(task);
      setFeedback(`任务 #${task.id} 已释放，已返回待领取队列`);
      await queryClient.invalidateQueries({ queryKey: ["tag-reviews"] });
    },
  });
  const decisionMutation = useMutation({
    mutationFn: ({
      id,
      body,
      adjudication,
    }: {
      id: number;
      body: DecideTagReviewRequest;
      adjudication: boolean;
    }) =>
      adjudication
        ? adjudicateTagReview(id, body)
        : decideTagReview(id, body),
    onSuccess: (result) => {
      setLocalTask(result.task);
      setFeedback("复核已写入人工事实");
      queryClient.invalidateQueries({ queryKey: ["tag-reviews"] });
    },
  });
  const canSubmitSelectedTask = Boolean(
    reviewTask &&
      reviewTask.status === "claimed" &&
      currentUserId !== null &&
      reviewTask.claimed_by === currentUserId,
  );
  const reviewReadOnlyMessage =
    reviewTask?.status === "claimed" && !canSubmitSelectedTask
      ? `该任务已由${
          reviewTask.claimed_by === null
            ? "其他复核员"
            : `复核员 #${reviewTask.claimed_by}`
        } 领取，当前仅可查看，不能提交结论。`
      : null;
  const neutralReviewContextHref =
    reviewTask &&
    isBlindReview(reviewTask) &&
    canSubmitSelectedTask &&
    reviewTask.reception_id !== null
      ? `/receptions/${encodeURIComponent(
          String(reviewTask.reception_id),
        )}/workspace`
      : null;

  return (
    <main className="ag-review-page">
      <header className="ag-review-hero">
        <div>
          <span className="ag-eyebrow">HUMAN-IN-THE-LOOP</span>
          <h1>人工复核工作台</h1>
          <p>证据、原音、模型建议与人工决策保持在同一个上下文中。</p>
        </div>
        <div className="ag-review-hero__actions">
          <Link to="/tag-governance">返回标签治理</Link>
          <Link to="/tag-insights" className="is-secondary">
            查看洞察
          </Link>
        </div>
      </header>

      <section className="ag-review-toolbar" aria-label="复核筛选">
        <label>
          任务状态
          <select
            aria-label="任务状态"
            value={statusFilter}
            onChange={(event) => {
              setStatusFilter(event.target.value);
              setFeedback(null);
            }}
          >
            <option value="active">待处理</option>
            <option value="pending">待领取</option>
            <option value="claimed">复核中</option>
            <option value="resolved">已完成</option>
            <option value="all">全部</option>
          </select>
        </label>
        <span aria-live="polite">
          {query.isFetching
            ? "正在同步队列…"
            : hasOpenReviewWork(query.data?.items)
              ? "队列每 3 秒自动同步"
              : "队列无进行中任务，已暂停自动同步"}
        </span>
      </section>

      {query.isPending && (
        <div className="ag-governance-state" role="status">
          <span className="ag-governance-spinner" aria-hidden="true" />
          正在加载复核队列…
        </div>
      )}
      {query.isError && (
        <div className="ag-governance-state is-error" role="alert">
          <strong>复核队列加载失败</strong>
          <span>
            {query.error instanceof Error ? query.error.message : "接口暂不可用"}
          </span>
          <button type="button" onClick={() => void query.refetch()}>
            重新加载
          </button>
        </div>
      )}
      {!query.isPending && !query.isError && items.length === 0 && (
        <div className="ag-governance-state is-empty" role="status">
          <span className="ag-governance-empty-mark" aria-hidden="true">
            <IconCheckCircle />
          </span>
          <strong>当前没有待复核任务</strong>
          <span>新的低置信度、冲突或抽检任务会自动进入这里。</span>
        </div>
      )}
      {!query.isPending && !query.isError && items.length > 0 && (
        <div className="ag-review-workspace">
          <ReviewQueue
            items={items}
            selectedId={selectedId}
            onSelect={(id) => {
              setSelectedId(id);
              setLocalTask(null);
              setFeedback(null);
              claimMutation.reset();
              releaseMutation.reset();
              decisionMutation.reset();
            }}
          />
          {reviewTask && (
            <section className="ag-review-main" aria-label={`复核任务 ${reviewTask.id}`}>
              <header className="ag-review-task-head">
                <div>
                  <span className="ag-card-kicker">
                    {reviewSubjectLabel(reviewTask)}
                  </span>
                  <h2>{reviewTask.tag_key}</h2>
                  <p>
                    {reviewPurpose(reviewTask)} · 真值层{" "}
                    {reviewTruthTier(reviewTask).toUpperCase()} · 触发原因{" "}
                    {reviewTask.reason} · 优先级 {reviewTask.priority}
                    {/* 金标冻结按复核批次 ID 圈定 cohort，这里是复核员唯一能
                        看到它的地方；盲审任务在提交前由服务端置空，前端同样
                        不渲染，避免经由批次 ID 侧信道泄露抽样语义。 */}
                    {reviewTask.review_bundle_id &&
                      !isBlindReview(reviewTask) && (
                        <>
                          {" "}
                          · 复核批次{" "}
                          <code>{reviewTask.review_bundle_id}</code>
                        </>
                      )}
                  </p>
                </div>
                <div>
                  <span className={`ag-review-state is-${reviewTask.status}`}>
                    {reviewStatus(reviewTask.status)}
                  </span>
                  {reviewTask.status === "pending" && (
                    <button
                      type="button"
                      disabled={claimMutation.isPending}
                      onClick={() => claimMutation.mutate(reviewTask.id)}
                    >
                      {claimMutation.isPending ? "领取中…" : "领取任务"}
                    </button>
                  )}
                  {canSubmitSelectedTask && (
                    <button
                      type="button"
                      className="is-secondary"
                      disabled={
                        releaseMutation.isPending ||
                        decisionMutation.isPending
                      }
                      onClick={() => {
                        if (
                          !window.confirm(
                            `确认放弃任务 #${reviewTask.id} 并释放领取吗？任务将回到待领取队列，未提交的复核输入不会保留。`,
                          )
                        ) {
                          return;
                        }
                        setFeedback(null);
                        releaseMutation.reset();
                        releaseMutation.mutate(reviewTask.id);
                      }}
                    >
                      {releaseMutation.isPending
                        ? "释放中…"
                        : "放弃任务/释放领取"}
                    </button>
                  )}
                </div>
              </header>

              {feedback && (
                <p className="ag-inline-feedback is-success" role="status">
                  {feedback}
                </p>
              )}
              {(claimMutation.isError ||
                releaseMutation.isError ||
                decisionMutation.isError) && (
                <p className="ag-inline-feedback is-error" role="alert">
                  {(claimMutation.error ??
                    releaseMutation.error ??
                    decisionMutation.error) instanceof Error
                    ? (
                        (claimMutation.error ??
                          releaseMutation.error ??
                          decisionMutation.error) as Error
                      ).message
                    : "操作失败，请重试"}
                </p>
              )}

              {isBlindReview(reviewTask) ? (
                <section className="ag-review-blind-banner" role="status">
                  <strong>盲审模式</strong>
                  <span>
                    模型建议、置信度和历史结论将在提交后揭示，避免锚定偏差。
                  </span>
                  <small>
                    {reviewPurpose(reviewTask)} ·{" "}
                    {reviewTruthTier(reviewTask).toUpperCase()}
                  </small>
                </section>
              ) : (
                <section className="ag-review-proposal" aria-label="模型建议">
                  <div>
                    <span>模型建议</span>
                    <strong>
                      {displayTagValue(
                        reviewTask.proposed_value,
                        "无建议值",
                      )}
                    </strong>
                  </div>
                  <div>
                    <span>置信度</span>
                    <strong>{compactPercent(reviewTask.confidence)}</strong>
                  </div>
                  <div>
                    <span>抽取版本</span>
                    <strong>#{reviewTask.tagger_version_id ?? "—"}</strong>
                  </div>
                  <div>
                    <span>体系版本</span>
                    <strong>#{reviewTask.schema_version_id ?? "—"}</strong>
                  </div>
                </section>
              )}

              {neutralReviewContextHref && (
                <section
                  className="ag-review-neutral-context"
                  aria-label="中立音频 / 转写上下文"
                >
                  <div>
                    <IconSound aria-hidden="true" />
                    <span>
                      <strong>中立上下文已解锁</strong>
                      <small>
                        工作台会在本次盲审提交前脱敏标签结论、状态演化和语义溯源，仅保留中立的音频与转写。
                      </small>
                    </span>
                  </div>
                  <Link to={neutralReviewContextHref}>
                    打开中立音频 / 转写上下文
                  </Link>
                </section>
              )}

              <div className="ag-review-context-grid">
                <EvidencePanel task={reviewTask} />
                <DecisionPanel
                  task={reviewTask}
                  pending={
                    decisionMutation.isPending || releaseMutation.isPending
                  }
                  canSubmit={canSubmitSelectedTask}
                  readOnlyMessage={reviewReadOnlyMessage}
                  valueDomainNotice={valueDomainNotice}
                  onReloadValueDomain={() => void schemasQuery.refetch()}
                  onDecision={(body) =>
                    decisionMutation.mutate({
                      id: reviewTask.id,
                      body,
                      adjudication: isAdjudicationReview(reviewTask),
                    })
                  }
                />
              </div>
            </section>
          )}
        </div>
      )}
    </main>
  );
}
